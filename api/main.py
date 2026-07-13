"""Autotube v2 FastAPI Application.

Serves the React SPA and REST API for the multi-channel video management panel.
"""
import sys
import json
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

sys.path.insert(0, str(Path(__file__).parent.parent))

import mimetypes
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, RedirectResponse

from api.deps import get_db
from api.progress import get_progress_manager
from api.routers import channels, videos, scenes, jobs, schedules, sources, voices, dashboard, system, ws as ws_router
from api.routers import auth, planning, shorts
from api.routers import monetization, milestones, analytics
from api.routers import promotion
from database.db_extended import migrate_v2, ExtendedDatabase
from database.db import init_db
from config.settings import TOKENS_DIR, DATABASE_PATH, STATS_ENABLED


# ── Lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── File-based logging (adds FileHandler to root logger) ──
    LOG_DIR = Path(__file__).parent.parent / "logs"
    LOG_DIR.mkdir(exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)  # Ensure INFO+ messages reach handlers
    if not any(isinstance(h, logging.FileHandler) and 
               str(LOG_DIR / "api.log") in getattr(h, 'baseFilename', '')
               for h in root_logger.handlers):
        fh = logging.FileHandler(LOG_DIR / "api.log")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
        root_logger.addHandler(fh)
    logging.getLogger("autotube").info("File logging enabled → logs/api.log")
    
    # ── Port pre-flight guard ─────────────────────────────────
    # If port 8000 is already bound (a previous instance still holds it),
    # uvicorn will crash with [Errno 98] AFTER the lifespan startup runs —
    # which would create orphan videos/jobs on every failed restart.
    # Abort BEFORE touching the DB so a doomed restart leaves no garbage.
    import os as _os
    import socket as _socket
    _bind_host = _os.getenv("API_HOST", "0.0.0.0")
    _bind_port = int(_os.getenv("API_PORT", "8000"))
    _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    try:
        _probe.bind((_bind_host, _bind_port))
    except OSError:
        logging.getLogger("autotube.startup").critical(
            "Port %s:%d already in use — another instance is running. "
            "Aborting startup BEFORE DB/slot init to avoid orphan jobs.",
            _bind_host, _bind_port,
        )
        _probe.close()
        raise SystemExit(1)
    finally:
        _probe.close()

    # Startup
    init_db()
    migrate_v2()

    # Auto-sync channel configs from Python modules → DB
    try:
        from config.config_bridge import sync_all_configs_to_db
        synced = sync_all_configs_to_db()
        logger = logging.getLogger("autotube.startup")
        logger.info("Config sync: %d channel(s) synced → %s", len(synced), synced)
        # Validate visual configs (catches out-of-range values from DB or config files)
        try:
            from config.config_validator import validate_all_channels
            val_warnings = validate_all_channels()
            if val_warnings:
                logger.warning("Config validation: %d warning(s):", len(val_warnings))
                for w in val_warnings:
                    logger.warning("  %s", w)
            else:
                logger.info("Config validation: all channels within safe ranges")
        except Exception as val_exc:
            logger.warning("Config validation skipped: %s", val_exc)
    except Exception as exc:
        logging.getLogger("autotube.startup").warning("Config sync skipped: %s", exc)
    
    # Auto-recover failed/interrupted videos from previous run
    try:
        from api.services.generation_service import auto_recover_on_startup
        await auto_recover_on_startup()
        logging.getLogger("autotube.startup").info("Auto-recovery completed")
    except Exception as exc:
        logging.getLogger("autotube.startup").warning(
            "Auto-recovery skipped: %s", exc
        )

    # Reconnect to running worker subprocesses that survived an API restart
    try:
        from api.services.generation_service import reconnect_active_workers
        await reconnect_active_workers()
    except Exception as exc:
        logging.getLogger("autotube.startup").warning(
            "Worker reconnection skipped: %s", exc
        )
    
    # ── Dynamic daily schedule generation (planning_service) ──
    # Generates slots per day based on per-channel config (videos_per_day).
    # Only creates slots for days that don't have any yet.
    try:
        from datetime import date as _dt_su, timedelta as _td_su
        from api.services.planning_service import compute_and_store_slots, ensure_today_planned
        from database.db_extended import ExtendedDatabase
        _db = ExtendedDatabase()
        ensure_today_planned(db=_db)
        logger = logging.getLogger("autotube.startup")
        # Ensure next 6 days also have slots (only if they don't exist)
        for offset in range(1, 7):
            day_str = (_dt_su.today() + _td_su(days=offset)).isoformat()
            existing = _db.get_planned_slots(date_key=day_str)
            if not existing:
                compute_and_store_slots(day_str, db=_db)
        logger.info("Planning engine: 7-day slots ensured")
    except Exception as exc:
        logging.getLogger("autotube.startup").warning(
            "Planning engine init skipped: %s", exc
        )

    # ── Shorts scheduler: 5 shorts/day/channel (3 native + 2 clip) ──
    try:
        from api.services.shorts_scheduler import generate_upcoming_shorts
        result = generate_upcoming_shorts(days=7)
        logger = logging.getLogger("autotube.startup")
        total = sum(int(v.split()[0]) for v in result.values() if v and v[0].isdigit())
        logger.info("Shorts scheduler: 7-day plan generated (%d slots)", total)
    except Exception as exc:
        logging.getLogger("autotube.startup").warning(
            "Shorts scheduler init skipped: %s", exc
        )
    
    # Launch schedule checker in background
    import asyncio
    schedule_task = asyncio.create_task(_schedule_checker_loop())
    
    yield
    
    # Shutdown
    schedule_task.cancel()
    try:
        await schedule_task
    except asyncio.CancelledError:
        pass


# ── Orphan detection config ─────────────────────────────────
ORPHAN_TIMEOUT_MINUTES = 180  # Jobs older than this without progress are declared orphaned (3h)


async def _queue_consumer():
    """Process queued generation jobs sequentially, one at a time.
    
    Only dispatches when:
    - No job is currently running
    - Enough RAM is available (4 GB minimum)
    
    Jobs bypassing the queue (manual dashboard generation) are started
    directly via the API endpoint and skip this consumer.
    """
    import logging
    logger = logging.getLogger("autotube.queue")
    
    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        
        # Find next queued job
        next_job = db.get_next_queued_job()
        if not next_job:
            return
        
        # Guard: don't dispatch if this channel already has an active job
        active_for_channel = db.get_active_job_for_channel(next_job["channel_id"])
        if active_for_channel:
            logger.debug("Queue consumer: channel %d already has active job #%d — skipping",
                        next_job["channel_id"], active_for_channel["id"])
            return
        
        # RAM gate: need at least 4 GB free
        try:
            from pipeline.ram_governor import is_ram_ok_for_dispatch
            if not is_ram_ok_for_dispatch():
                logger.info(
                    "Queue consumer: insufficient RAM — skipping dispatch for job #%d",
                    next_job["id"],
                )
                return
        except ImportError:
            pass  # ram_governor not available — proceed
        
        logger.info(
            "Queue consumer: dispatching job #%d (channel_id=%d, action=%s)",
            next_job["id"], next_job["channel_id"], next_job.get("action", "?"),
        )
        
        from api.services.generation_service import start_generation_job, start_generation_job_subprocess, USE_SUBPROCESS_WORKER
        
        if USE_SUBPROCESS_WORKER:
            asyncio.create_task(start_generation_job_subprocess(
                job_id=next_job["id"],
                channel_id=next_job["channel_id"],
                video_id=next_job["video_id"],
                action=next_job.get("action", "generate_and_upload"),
            ))
        else:
            asyncio.create_task(start_generation_job(
                job_id=next_job["id"],
                channel_id=next_job["channel_id"],
                video_id=next_job["video_id"],
                action=next_job.get("action", "generate_and_upload"),
            ))
        
    except Exception as e:
        logger.error("Queue consumer error: %s", e)


async def _schedule_checker_loop():
    """Background loop that checks schedules, planned slots, orphans, and collects YouTube stats."""
    import asyncio, logging, time
    logger = logging.getLogger("autotube.scheduler")
    
    logger.info("Schedule checker loop started (checks every 5 min)")
    
    last_stats_collection = 0
    last_lifecycle_check = time.time()
    last_midnight_check = time.time()
    last_recovery_check = 0
    first_run = True

    while True:
        try:
            if first_run:
                first_run = False
            else:
                await asyncio.sleep(300)  # Check every 5 minutes after first run

            await _process_due_schedules()
            await _process_planned_slots()    # Dynamic planning engine (primary dispatcher)
            await _process_shorts_slots()     # Shorts scheduled dispatch
            await _queue_consumer()           # Process queued generation jobs sequentially
            await _detect_and_clean_orphans()
            
            # Collect YouTube stats every 6 hours
            now = time.time()
            if STATS_ENABLED and now - last_stats_collection > 21600:  # 6 hours
                await _collect_youtube_stats()
                last_stats_collection = now
            
            # Process video lifecycle promotion actions every 15 minutes
            if now - last_lifecycle_check > 900:  # 15 minutes
                await _process_lifecycle_actions()
                last_lifecycle_check = now
            
            # Auto-recovery: replan missing publications every 60 minutes
            if now - last_recovery_check > 3600:  # 60 minutes
                await _process_recovery_planner()
                last_recovery_check = now
            
            # Regenerate the schedule forecast at midnight (daily)
            if now - last_midnight_check > 3600:  # Check once per hour
                from datetime import date as _date
                import logging as _logging
                _logger = _logging.getLogger("autotube.midnight")
                today = _date.today().isoformat()
                try:
                    from database.db_extended import ExtendedDatabase
                    _mid_db = ExtendedDatabase()
                    from api.services.planning_service import ensure_today_planned, compute_and_store_slots
                    # Ensure today has slots (only creates if none exist; won't touch completed/running)
                    ensure_today_planned(db=_mid_db)
                    # Also ensure today has shorts slots
                    from api.services.shorts_scheduler import ensure_today_shorts_scheduled
                    ensure_today_shorts_scheduled()
                    # Pre-generate the +7th day to keep the 7-day forecast window
                    from datetime import date as _dt, timedelta as _td
                    future_day = (_dt.today() + _td(days=7)).isoformat()
                    # Only generate if the day has ZERO slots of any kind (avoid duplication)
                    existing = _mid_db.get_planned_slots(date_key=future_day)
                    if not existing:
                        compute_and_store_slots(future_day, db=_mid_db)
                        _logger.info("Schedule extended to %s", future_day)
                    # Extend shorts forecast too
                    existing_shorts = _mid_db.get_shorts_planned_slots(date_key=future_day)
                    if not existing_shorts:
                        from api.services.shorts_scheduler import compute_daily_shorts_slots, persist_daily_shorts_slots
                        shorts_slots = compute_daily_shorts_slots(future_day, db=_mid_db)
                        persist_daily_shorts_slots(future_day, shorts_slots, db=_mid_db)
                        _logger.info("Shorts schedule extended to %s (%d slots)", future_day, len(shorts_slots))
                    last_midnight_check = now
                except Exception as exc:
                    logger.debug("Midnight schedule refresh: %s", exc)
                
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Schedule checker error: {e}")
            await asyncio.sleep(60)


async def _process_planned_slots():
    """Process due planned_slots using the dynamic planning engine."""
    import logging
    logger = logging.getLogger("autotube.planner")
    try:
        from api.services.planning_service import process_planned_slots as dispatch
        result = dispatch()
        if result:
            logger.info(
                "Planning dispatched: slot=%d job=%d video=%d channel=%s",
                result["slot_id"], result["job_id"], result["video_id"], result["channel_slug"],
            )
    except Exception as e:
        logger.error("Planning dispatch error: %s", e)


async def _process_recovery_planner():
    """Check for channels behind daily target and replan missing slots.

    Runs every 60 min. Only active between 10:00-23:00 local time.
    Delegates to api.services.recovery_planner.auto_recover_missing_publications.
    """
    import asyncio
    import logging
    logger = logging.getLogger("autotube.recovery")
    try:
        from api.services.recovery_planner import auto_recover_missing_publications
        result = await asyncio.to_thread(auto_recover_missing_publications)
        if result and result.get("recovered_count", 0) > 0:
            logger.info(
                "Recovery: %d slot(s) created across %d channel(s): %s",
                result["recovered_count"],
                len(result.get("channels_affected", [])),
                ", ".join(result.get("channels_affected", [])),
            )
    except Exception as e:
        logger.error("Recovery planner error: %s", e)


async def _process_smart_slots():
    """Dispatch due planned_slots using the per-channel adaptive schedule engine.

    Uses per-channel average creation times, 6-day fair rotation,
    jitter (±15 min), and 15% buffer. Dispatches ONE slot at a time
    (only when no job is running).
    """
    import logging
    logger = logging.getLogger("autotube.smart_slots")
    try:
        from api.services.schedule_engine import dispatch_next_due_slot
        result = dispatch_next_due_slot()
        if result:
            logger.info(
                "Smart slot dispatched: slot=%d job=%d video=%d channel=%s",
                result["slot_id"], result["job_id"], result["video_id"], result["channel_slug"],
            )
    except Exception as e:
        logger.error("Smart slot dispatch error: %s", e)


async def _process_shorts_slots():
    """Dispatch due shorts slots using shorts_scheduler.

    Dispatches ONE short at a time (only when no job is running).
    For clip shorts, waits for source long video to be completed.
    """
    import logging
    logger = logging.getLogger("autotube.shorts_scheduler")
    try:
        from api.services.shorts_scheduler import dispatch_next_due_shorts_slot
        result = dispatch_next_due_shorts_slot()
        if result:
            logger.info(
                "Shorts slot dispatched: slot=%d channel=%s type=%s",
                result["slot_id"], result["channel_slug"], result["short_type"],
            )
    except Exception as e:
        logger.error("Shorts dispatch error: %s", e)


async def _process_due_schedules():
    """Find and execute due schedules."""
    import sqlite3, logging
    from datetime import datetime
    from config.settings import DATABASE_PATH
    logger = logging.getLogger("autotube.scheduler")
    
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT cs.*, c.slug as channel_slug FROM content_schedules cs "
        "JOIN channels c ON cs.channel_id = c.id "
        "WHERE cs.active = 1 AND datetime(cs.next_run_at) <= datetime(?)",
        (now,),
    ).fetchall()
    
    if not rows:
        conn.close()
        return
    
    for row in rows:
        s = dict(row)
        logger.info(f"Running schedule #{s['id']}: {s['channel_slug']} action={s['action']}")
        
        # ── Per-channel guard: skip if this channel already has a running job ──
        active_for_channel = db.get_active_job_for_channel(s["channel_id"])
        if active_for_channel:
            logger.debug("Schedule #%d skipped: channel %d already has active job #%d",
                        s["id"], s["channel_id"], active_for_channel["id"])
            # Push next_run_at forward 5 min to avoid tight retry loop
            try:
                conn.execute(
                    "UPDATE content_schedules SET next_run_at = datetime('now', 'localtime', '+5 minutes') "
                    "WHERE id = ?", (s["id"],)
                )
                conn.commit()
            except Exception:
                pass
            continue
        
        try:
            # Launch the generation job
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
            
            # Create a video record
            cursor = conn.execute(
                "INSERT INTO videos (canal, channel_id, video_path, status, progress, created_at) VALUES (?, ?, '', 'generating', 0, CURRENT_TIMESTAMP)",
                (s["channel_slug"], s["channel_id"]),
            )
            conn.commit()
            video_id = cursor.lastrowid
            
            # Create job record
            job_id = db.create_job(s["channel_id"], s["action"], video_id)
            
            # Update schedule: calculate next run
            if s["schedule_type"] == "recurring":
                # Sanitize interval_h (must be a positive integer) to prevent SQL injection
                try:
                    interval_h = int(s["interval_h"])
                    if interval_h <= 0:
                        interval_h = 1
                except (ValueError, TypeError):
                    interval_h = 1
                conn.execute(
                    "UPDATE content_schedules SET last_run_at = ?, "
                    "next_run_at = datetime('now', 'localtime', '+' || ? || ' hours'), video_id = ? WHERE id = ?",
                    (now, str(interval_h), video_id, s["id"]),
                )
            else:
                # One-time: deactivate after run
                conn.execute(
                    "UPDATE content_schedules SET last_run_at = ?, active = 0, video_id = ? WHERE id = ?",
                    (now, video_id, s["id"]),
                )
            conn.commit()
            
            # Fire and forget the generation (don't await)
            import asyncio
            from api.services.generation_service import start_generation_job, start_generation_job_subprocess, USE_SUBPROCESS_WORKER
            
            if USE_SUBPROCESS_WORKER:
                asyncio.create_task(
                    start_generation_job_subprocess(
                        job_id=job_id,
                        channel_id=s["channel_id"],
                        video_id=video_id,
                        action=s["action"],
                    )
                )
            else:
                asyncio.create_task(
                    start_generation_job(
                        job_id=job_id,
                        channel_id=s["channel_id"],
                        video_id=video_id,
                        action=s["action"],
                        content_id=s.get("content_id"),
                    )
                )
            
        except Exception as e:
            logger.error(f"Schedule #{s['id']} failed: {e}")
            conn.execute(
                "UPDATE content_schedules SET next_run_at = datetime('now', 'localtime', '+1 hour') WHERE id = ?",
                (s["id"],),
            )
            conn.commit()
    
    conn.close()


async def _detect_and_clean_orphans():
    """Detect orphaned generation jobs/videos and mark them as failed.
    
    Runs every 5 minutes as part of the schedule checker loop.
    Declares a job/video orphaned if stuck for > ORPHAN_TIMEOUT_MINUTES (60 min).
    
    Offloaded to thread pool to avoid blocking the async event loop.
    """
    import asyncio, logging
    logger = logging.getLogger("autotube.orphans")
    
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _detect_and_clean_orphans_sync)
    except Exception as exc:
        logger.error("Orphan detection failed: %s", exc)


def _detect_and_clean_orphans_sync():
    """Synchronous orphan detection logic (runs in thread pool).
    
    Also prunes old cancelled/skipped planned slots to keep the DB clean.
    """
    import logging
    logger = logging.getLogger("autotube.orphans")
    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        result = db.cleanup_orphaned_jobs(timeout_minutes=ORPHAN_TIMEOUT_MINUTES)
        
        if result["jobs_failed"] == 0 and result["videos_reset"] == 0:
            logger.debug("Orphan check: all clear")
        
        # Prune old cancelled/skipped slots (older than yesterday)
        prune_result = db.prune_old_slots()
        if prune_result.get("planned_slots_deleted") or prune_result.get("shorts_planned_slots_deleted"):
            logger.info(
                "Slot prune: %d planned + %d shorts deleted",
                prune_result["planned_slots_deleted"],
                prune_result["shorts_planned_slots_deleted"],
            )
    except Exception as exc:
        logger.error("Orphan detection failed: %s", exc)


async def _process_lifecycle_actions():
    """Process due video lifecycle promotion actions for all active channels."""
    import logging
    logger = logging.getLogger("autotube.lifecycle")
    
    try:
        from config.settings import LIFECYCLE_ENABLED
        if not LIFECYCLE_ENABLED:
            return
        
        from database.db_extended import ExtendedDatabase
        from pipeline.video_lifecycle import VideoLifecycleManager
        
        db = ExtendedDatabase()
        channels = db.get_channels(active_only=True)
        
        for ch in channels:
            slug = ch.get("slug")
            if not slug:
                continue
            try:
                mgr = VideoLifecycleManager(slug)
                result = mgr.process_due_actions()
                if result.get("processed", 0) > 0:
                    logger.info(
                        "Lifecycle [%s]: processed=%d succeeded=%d failed=%d",
                        slug, result["processed"], result["succeeded"], result["failed"],
                    )
            except Exception as exc:
                logger.warning("Lifecycle [%s] error: %s", slug, exc)
    except Exception as exc:
        logger.error("Lifecycle processor error: %s", exc)


import time as _time_module

# Tracks the state of on-demand stats collection so the UI can show
# real progress/result feedback (survives page reloads).
STATS_COLLECTION_STATE = {
    "status": "idle",          # idle | running | success | error
    "started_at": None,        # epoch seconds
    "finished_at": None,       # epoch seconds
    "channels": [],            # per-channel summaries
    "error": None,             # error message if status == error
}


async def _collect_youtube_stats():
    """Collect YouTube stats for all active channels with valid tokens."""
    import logging
    logger = logging.getLogger("autotube.stats")

    STATS_COLLECTION_STATE.update({
        "status": "running",
        "started_at": _time_module.time(),
        "finished_at": None,
        "channels": [],
        "error": None,
    })

    try:
        from database.db_extended import ExtendedDatabase
        from pipeline.youtube_stats import YouTubeStatsFetcher
        from pipeline.monetization import calc_video_revenue, calc_channel_revenue_total
        
        db = ExtendedDatabase()
        channels = db.get_channels(active_only=True)
        
        for ch in channels:
            slug = ch["slug"]
            token_path = TOKENS_DIR / f"{slug}.pickle"
            if not token_path.exists():
                STATS_COLLECTION_STATE["channels"].append({
                    "slug": slug, "ok": False, "skipped": True,
                    "reason": "no token",
                })
                continue
            
            try:
                fetcher = YouTubeStatsFetcher(slug)
                result = fetcher.collect_and_store(db)
                logger.info(
                    "Stats collected for %s: %s videos, channel=%s",
                    slug,
                    result.get("videos_updated", 0),
                    result.get("channel_updated", False),
                )
                STATS_COLLECTION_STATE["channels"].append({
                    "slug": slug,
                    "ok": "error" not in result,
                    "videos_updated": result.get("videos_updated", 0),
                    "shorts_updated": result.get("shorts_updated", 0),
                    "channel_updated": result.get("channel_updated", False),
                    "error": result.get("error"),
                })
                
                # Calculate and store estimated revenue for this channel
                if ch.get("cpm_min") and ch.get("cpm_max"):
                    cpm_min = ch["cpm_min"]
                    cpm_max = ch["cpm_max"]
                    
                    # Per-video revenue
                    videos = db.get_videos(channel_id=ch["id"], limit=200)
                    for v in videos:
                        if v.get("yt_video_id"):
                            latest_stats = db.get_video_stats_history(v["id"], days=7)
                            if latest_stats:
                                views = latest_stats[-1].get("views", 0) or 0
                                rev_min, rev_max = calc_video_revenue(views, cpm_min, cpm_max)
                                # Update the latest stats entry with revenue
                                import sqlite3
                                with db._connect() as conn:
                                    conn.execute(
                                        """UPDATE video_stats_history
                                           SET estimated_revenue_min = ?, estimated_revenue_max = ?
                                           WHERE id = ?""",
                                        (rev_min, rev_max, latest_stats[-1]["id"]),
                                    )
                                    conn.commit()
                    
                    # Channel-level revenue snapshot
                    revenue = calc_channel_revenue_total(db, ch["id"])
                    import sqlite3
                    with db._connect() as conn:
                        conn.execute(
                            """UPDATE channel_stats_history
                               SET estimated_revenue_min = ?, estimated_revenue_max = ?
                               WHERE id = (
                                   SELECT MAX(id) FROM channel_stats_history WHERE channel_id = ?
                               )""",
                            (round(revenue.get("total_min", 0), 2),
                             round(revenue.get("total_max", 0), 2),
                             ch["id"]),
                        )
                        conn.commit()
                    logger.info(
                        "Revenue calculated for %s: $%.2f — $%.2f",
                        slug,
                        revenue.get("total_min", 0),
                        revenue.get("total_max", 0),
                    )
                
                # Check for newly achieved milestones
                try:
                    from pipeline.milestones import check_and_record_milestones
                    new_milestones = check_and_record_milestones(db, ch["id"])
                    if new_milestones > 0:
                        logger.info(
                            "Milestones: %d new achieved for %s",
                            new_milestones, slug,
                        )
                except Exception as me:
                    logger.warning("Milestone check failed for %s: %s", slug, me)
                    
            except Exception as exc:
                logger.error("Stats collection failed for %s: %s", slug, exc)
                STATS_COLLECTION_STATE["channels"].append({
                    "slug": slug, "ok": False, "error": str(exc),
                })

        STATS_COLLECTION_STATE.update({
            "status": "success",
            "finished_at": _time_module.time(),
        })
    except Exception as exc:
        logger.error("Stats collector error: %s", exc)
        STATS_COLLECTION_STATE.update({
            "status": "error",
            "finished_at": _time_module.time(),
            "error": str(exc),
        })


# ── App ──────────────────────────────────────────────────────

app = FastAPI(
    title="Autotube Panel API",
    version="2.0.0",
    description="Multi-channel YouTube automation panel",
    lifespan=lifespan,
)

# CORS - allow embedding in CRM iframe
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Routers ──────────────────────────────────────────────

app.include_router(auth.router, prefix="/api", tags=["Auth"])
app.include_router(channels.router, prefix="/api/channels", tags=["Channels"])
app.include_router(videos.router, prefix="/api/videos", tags=["Videos"])
app.include_router(scenes.router, prefix="/api/scenes", tags=["Scenes"])
app.include_router(jobs.router, prefix="/api/jobs", tags=["Jobs"])
app.include_router(schedules.router, prefix="/api/schedules", tags=["Schedules"])
app.include_router(planning.router, prefix="/api/planning", tags=["Planning"])
app.include_router(sources.router, prefix="/api/sources", tags=["Sources"])
app.include_router(voices.router, prefix="/api", tags=["Voices"])
app.include_router(dashboard.router, prefix="/api", tags=["Dashboard"])
app.include_router(system.router, prefix="/api", tags=["System"])
app.include_router(shorts.router, tags=["Shorts"])
app.include_router(monetization.router, prefix="/api", tags=["Monetization"])
app.include_router(milestones.router, prefix="/api", tags=["Milestones"])
app.include_router(analytics.router, prefix="/api", tags=["Analytics"])
app.include_router(promotion.router, tags=["Promotion"])

# WebSocket
@app.websocket("/ws/progress/{job_id}")
async def ws_progress(ws: WebSocket, job_id: int):
    await ws.accept()
    mgr = get_progress_manager()
    mgr.subscribe(job_id, ws)

    # ── Send initial state on connect ───────────────────────
    # Clients connecting mid-generation need the current progress
    # immediately, since broadcast() only fires on changes.
    try:
        db = get_db()
        job = db.get_job(job_id)
        if job:
            video_id = job.get("video_id")
            video = db.get_video(video_id) if video_id else None
            phase = (
                video.get("progress_phase", job.get("phase", ""))
                if video else job.get("phase", "")
            )
            progress = (
                video.get("progress", job.get("progress", 0))
                if video else (job.get("progress", 0) or 0)
            )
            phase_messages = {
                "scrape": "Buscando nuevo contenido...",
                "script": "Generando guion con IA...",
                "tts": "Generando voz con IA (TTS)...",
                "media": "Buscando imagenes y videos...",
                "video": "Ensamblando video (MoviePy)...",
                "metadata": "Generando metadatos SEO...",
                "upload": "Subiendo a YouTube...",
                "inicio": "Iniciando pipeline...",
            }
            message = phase_messages.get(
                phase,
                f"Fase: {phase}" if phase else "Generando...",
            )
            status = "failed" if job.get("status") == "failed" else \
                     "completed" if job.get("status") in ("completed",) else \
                     "running"
            data = {
                "job_id": job_id,
                "status": status,
                "progress": progress,
                "phase": phase,
                "message": message,
            }
            if video_id:
                data["video_id"] = video_id
            await ws.send_json(data)
    except Exception:
        pass  # If DB fails, client will get updates via broadcast later

    try:
        while True:
            # Keep connection alive, client sends pings
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        mgr.unsubscribe(job_id, ws)


# ── Dashboard stats ──────────────────────────────────────────

@app.get("/api/stats")
def get_stats():
    db = get_db()
    stats = db.get_dashboard_stats()
    return stats


@app.post("/api/stats/collect")
async def trigger_stats_collection(background_tasks: BackgroundTasks):
    """Trigger on-demand YouTube stats collection for all active channels.

    Runs _collect_youtube_stats() as a background task and returns immediately.
    Poll GET /api/stats/collect/status to know when it finishes and its result.
    """
    if STATS_COLLECTION_STATE["status"] == "running":
        return {
            "ok": False,
            "message": "Ya hay una recoleccion en curso",
            "state": STATS_COLLECTION_STATE,
        }
    background_tasks.add_task(_collect_youtube_stats)
    return {
        "ok": True,
        "message": "Recoleccion de stats iniciada para los canales activos",
        "state": STATS_COLLECTION_STATE,
    }


@app.get("/api/stats/collect/status")
async def stats_collection_status():
    """Return current/last state of the on-demand stats collection."""
    return STATS_COLLECTION_STATE


@app.get("/api/logs")
def get_logs(channel_id: int = None, limit: int = 30):
    db = get_db()
    logs = db.get_pipeline_logs(channel_id=channel_id, limit=limit)
    return logs


# ── Static file serving (output videos, images, thumbnails) ──

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_ROOT = PROJECT_ROOT / "output"

# No-cache headers for media files to prevent stale thumbnails/videos
NO_CACHE_MEDIA = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def resolve_media_path(stored: str) -> Path | None:
    """Resolve a stored path (absolute, relative, or hybrid) to a filesystem Path.

    Handles three formats found in the DB:
      1. Absolute:  /root/autotube/output/videos/foo.mp4
      2. Relative:  output/videos/foo.mp4
      3. Other abs: /tmp/foo.mp4 (not under project — try as-is)

    Returns a Path if the file exists on disk, None otherwise.
    """
    if not stored or not isinstance(stored, str):
        return None

    raw = Path(stored)
    if raw.exists():
        return raw

    # Try project-root-relative
    rel = PROJECT_ROOT / stored
    if rel.exists():
        return rel

    # Try output-root-relative (for paths stored as 'videos/foo.mp4' without 'output/')
    out_rel = OUTPUT_ROOT / stored
    if out_rel.exists():
        return out_rel

    return None


@app.get("/api/static/{file_path:path}")
async def serve_static(file_path: str):
    """Serve files from output dir. Handles paths like 'output/...' or absolute."""
    full_path = resolve_media_path(file_path)
    if full_path is None:
        # Fallback: try candidates like the legacy logic
        candidates = [
            OUTPUT_ROOT / file_path,
            PROJECT_ROOT / file_path,
        ]
        if file_path.startswith("/"):
            candidates.append(Path(file_path))
        for p in candidates:
            if p.exists() and p.is_file():
                full_path = p
                break

    if full_path is None:
        raise HTTPException(404, f"File not found: {file_path}")

    return FileResponse(full_path, headers=NO_CACHE_MEDIA)


@app.get("/api/video-file/{video_id}")
async def serve_video_file(video_id: int, request: Request):
    """Stream a video file with range request support for seekable playback.
    
    If the local mp4 was deleted after upload (Fase F), redirect to YouTube.
    """
    db = get_db()
    v = db.get_video(video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    
    stored_path = v.get("video_path", "")
    video_path = resolve_media_path(stored_path) if stored_path else None
    
    # Local file was deleted after upload — redirect to YouTube
    if video_path is None or not video_path.exists():
        if v.get("yt_video_id"):
            yt_url = v.get("yt_url") or f"https://youtube.com/watch?v={v['yt_video_id']}"
            return RedirectResponse(url=yt_url, status_code=302)
        raise HTTPException(404, "Video file not found on disk")
    
    file_size = video_path.stat().st_size

    file_size = video_path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        # Parse range header
        start_str, end_str = range_header.replace("bytes=", "").split("-")
        start = int(start_str)
        end = int(end_str) if end_str else file_size - 1
        chunk_size = end - start + 1

        async def ranged_stream():
            with open(video_path, "rb") as f:
                f.seek(start)
                bytes_sent = 0
                while bytes_sent < chunk_size:
                    buf = f.read(min(65536, chunk_size - bytes_sent))
                    if not buf:
                        break
                    bytes_sent += len(buf)
                    yield buf

        return StreamingResponse(
            ranged_stream(),
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(chunk_size),
                **NO_CACHE_MEDIA,
            },
        )
    else:
        return FileResponse(video_path, media_type="video/mp4", headers=NO_CACHE_MEDIA)


@app.get("/api/thumbnail/{video_id}")
async def serve_thumbnail(video_id: int):
    """Serve the thumbnail for a video with no-cache headers."""
    db = get_db()
    v = db.get_video(video_id)
    if not v or not v.get("thumbnail_path"):
        raise HTTPException(404, "Thumbnail not found")

    thumb_path = resolve_media_path(v["thumbnail_path"])
    if thumb_path is None:
        raise HTTPException(404, "Thumbnail file not found on disk")

    return FileResponse(thumb_path, media_type="image/jpeg", headers=NO_CACHE_MEDIA)


@app.get("/api/channels/{channel_id}/templates/{segment_type}/file")
async def serve_template_file(channel_id: int, segment_type: str, request: Request):
    """Stream a channel template mini-video (intro/cta/outro) with range support."""
    if segment_type not in ("intro", "cta", "outro"):
        raise HTTPException(400, "segment_type must be intro, cta, or outro")

    db = get_db()
    tpl = db.get_channel_template(channel_id, segment_type)
    if not tpl or not tpl.get("video_path"):
        raise HTTPException(404, "Template not found")

    video_path = resolve_media_path(tpl["video_path"])
    if video_path is None or not video_path.exists():
        raise HTTPException(404, "Template file not found on disk")

    file_size = video_path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        start_str, end_str = range_header.replace("bytes=", "").split("-")
        start = int(start_str)
        end = int(end_str) if end_str else file_size - 1
        chunk_size = end - start + 1

        async def ranged_stream():
            with open(video_path, "rb") as f:
                f.seek(start)
                bytes_sent = 0
                while bytes_sent < chunk_size:
                    buf = f.read(min(65536, chunk_size - bytes_sent))
                    if not buf:
                        break
                    bytes_sent += len(buf)
                    yield buf

        return StreamingResponse(
            ranged_stream(),
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(chunk_size),
                **NO_CACHE_MEDIA,
            },
        )

    return FileResponse(video_path, media_type="video/mp4", headers=NO_CACHE_MEDIA)


# ── Static files (React SPA) ─────────────────────────────────

STATIC_DIR = Path(__file__).parent.parent / "frontend" / "dist"
NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}

if STATIC_DIR.exists():
    # Static files with no-cache headers
    class NoCacheStaticFiles(StaticFiles):
        async def __call__(self, scope, receive, send):
            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    headers = dict(message.get("headers", []))
                    headers[b"cache-control"] = b"no-cache, no-store, must-revalidate"
                    headers[b"pragma"] = b"no-cache"
                    headers[b"expires"] = b"0"
                    message["headers"] = [(k, v) for k, v in headers.items()]
                await send(message)
            await super().__call__(scope, receive, send_wrapper)
    
    app.mount("/assets", NoCacheStaticFiles(directory=STATIC_DIR / "assets"), name="assets")
    
    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        """Serve React SPA — all non-API routes go to index.html."""
        if path.startswith("api/"):
            raise HTTPException(404, "API endpoint not found")
        full_path = STATIC_DIR / path
        if full_path.exists() and full_path.is_file():
            return FileResponse(full_path, headers=NO_CACHE)
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)
    
    @app.get("/")
    async def root():
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)
