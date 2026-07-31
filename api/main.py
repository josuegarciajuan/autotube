"""Autotube v2 FastAPI Application.

Serves the React SPA and REST API for the multi-channel video management panel.
"""
import sys
import os
import json
import time
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

sys.path.insert(0, str(Path(__file__).parent.parent))

import mimetypes
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, RedirectResponse, Response

from api.deps import get_db
from api.progress import get_progress_manager
from api.routers import channels, videos, scenes, jobs, schedules, sources, voices, dashboard, system, ws as ws_router
from api.routers import auth, planning, shorts
from api.routers import monetization, milestones, analytics
from api.routers import promotion, gamification, social_accounts
from api.routers import monitor as monitor_router
from api.routers import insights
from database.db_extended import migrate_v2, ExtendedDatabase
from database.db import init_db
from config.settings import TOKENS_DIR, DATABASE_PATH, STATS_ENABLED

logger = logging.getLogger("autotube.main")

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
        _formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )
        _formatter.converter = time.gmtime  # UTC — same as DB CURRENT_TIMESTAMP
        fh.setFormatter(_formatter)
        root_logger.addHandler(fh)
    logging.getLogger("autotube").info("File logging enabled → logs/api.log")
    
    # ── Port pre-flight guard ─────────────────────────────────
    # If port 8000 is already bound (a previous instance still holds it),
    # uvicorn will crash with [Errno 98] AFTER the lifespan startup runs —
    # which would create orphan videos/jobs on every failed restart.
    # Abort BEFORE touching the DB so a doomed restart leaves no garbage.
    #
    # DETOUR: Skip the guard when running under uvicorn's StatReload.
    # During hot-reload, uvicorn spawns a new child process while the parent
    # still holds the port. The parent is guaranteed to release it before the
    # child's uvicorn server starts accepting connections. Running the port
    # probe in the child causes a false-positive crash loop.
    #
    # Detection: StatReload sets WERKZEUG_RUN_MAIN=true OR starts the child
    # via multiprocessing, which sets _UVICORN_RELOADING=1 in some versions.
    # We probe the general case: if the parent process is a uvicorn instance
    # AND the current process is a direct child (reload child), skip the guard.
    import os as _os
    import time as _time
    import socket as _socket
    import psutil as _psutil

    _skip_guard = False
    try:
        _parent = _psutil.Process(_os.getppid())
        _parent_cmd = " ".join(_parent.cmdline())
        # Check if parent is uvicorn with reload AND we are a direct child
        if "uvicorn" in _parent_cmd and "--reload" in _parent_cmd:
            _skip_guard = True
            logging.getLogger("autotube.startup").info(
                "Port guard SKIPPED — running under uvicorn StatReload (parent=%s %s)",
                _parent.pid, _parent_cmd[:80],
            )
    except Exception:
        pass

    if not _skip_guard:
        _bind_host = _os.getenv("API_HOST", "0.0.0.0")
        _bind_port = int(_os.getenv("API_PORT", "8000"))
        _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        _bound = False
        for _attempt in range(10):
            try:
                _probe.bind((_bind_host, _bind_port))
                _bound = True
                break
            except OSError:
                if _attempt < 9:
                    _wait = 0.5 * (2 ** _attempt)  # 0.5, 1, 2, 4, 8, 16, 32, 64, 128
                    logging.getLogger("autotube.startup").warning(
                        "Port %s:%d still in use (attempt %d/10) — retrying in %.1fs...",
                        _bind_host, _bind_port, _attempt + 1, _wait,
                    )
                    _time.sleep(_wait)
        if not _bound:
            logging.getLogger("autotube.startup").critical(
                "Port %s:%d still in use after 10 retries — another instance is running. "
                "Aborting startup BEFORE DB/slot init to avoid orphan jobs.",
                _bind_host, _bind_port,
            )
        _probe.close()
        if not _bound:
            raise SystemExit(1)

    # Startup
    init_db()
    migrate_v2()

    # Restore stats collection state from DB (survives server restarts)
    _unpin_stats_state("stats_collection_state")

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
    
    # ── Clean up orphaned ffmpeg/edge-tts/yt-dlp processes from prior runs ──
    try:
        from api.services.generation_service import _kill_orphaned_ffmpeg
        _kill_orphaned_ffmpeg()
        logging.getLogger("autotube.startup").info("Orphan process cleanup completed")
    except Exception as exc:
        logging.getLogger("autotube.startup").warning("Orphan cleanup skipped: %s", exc)
    
    # ── Clean up stuck channel_insights from prior runs ──
    # If the server was restarted mid-analysis, insight rows remain 'processing'
    # and the frontend polls them infinitely. Fail them on startup.
    try:
        from database.db_extended import ExtendedDatabase
        _insight_db = ExtendedDatabase()
        _cleanup_orphaned_insights(
            _insight_db,
            logging.getLogger("autotube.startup"),
            timeout_minutes=10,  # shorter on startup: anything still processing is definitely dead
        )
    except Exception as exc:
        logging.getLogger("autotube.startup").warning("Insight orphan cleanup skipped: %s", exc)

    # ── PAUSE GATE: skip all auto-planning/dispatch when operator paused scheduling ──
    _scheduler_paused = False
    try:
        from database.db_extended import ExtendedDatabase
        _paused_db = ExtendedDatabase()
        _scheduler_paused = _paused_db.get_system_state("scheduler_paused") == "true"
    except Exception:
        pass

    if not _scheduler_paused:
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
    
    if not _scheduler_paused:
        # ── Defer heavy startup work to background threads ──
        # Planning and shorts generation do synchronous I/O (DB, ffmpeg,
        # Kokoro TTS, DeepSeek API) which blocks the asyncio event loop.
        # Running them in the lifespan prevents uvicorn from accepting HTTP
        # connections for minutes.  Defer to run_in_executor so the API
        # starts responding immediately while startup work proceeds in parallel.
        import asyncio as _asyncio
        
        async def _startup_heavy_tasks():
            await _asyncio.sleep(5)  # Brief pause to let API stabilize first
            _loop = _asyncio.get_running_loop()
            _logger = logging.getLogger("autotube.startup")
            
            # Planning engine
            try:
                from api.services.planning_service import compute_and_store_horizon
                from database.db_extended import ExtendedDatabase
                _db = ExtendedDatabase()
                result = await _loop.run_in_executor(
                    None, lambda: compute_and_store_horizon(horizon_days=7, db=_db)
                )
                _logger.info("Planning engine: 7-day horizon planned (%d slots, %d days)",
                             result.get("total_slots", 0), result.get("days_planned", 0))
            except Exception as exc:
                _logger.warning("Planning engine init skipped: %s", exc)

            # Shorts scheduler
            try:
                from api.services.shorts_scheduler import generate_upcoming_shorts
                result = await _loop.run_in_executor(
                    None, lambda: generate_upcoming_shorts(days=7)
                )
                total = sum(int(v.split()[0]) for v in result.values() if v and v[0].isdigit())
                _logger.info("Shorts scheduler: 7-day plan generated (%d slots)", total)
            except Exception as exc:
                _logger.warning("Shorts scheduler init skipped: %s", exc)
        
        _startup_tasks = _asyncio.create_task(_startup_heavy_tasks())
        logging.getLogger("autotube.startup").info(
            "Deferred startup tasks: planning + shorts will run in background"
        )
    else:
        logging.getLogger("autotube.startup").warning(
            "SCHEDULER PAUSED — auto-recovery, planning, and shorts scheduling skipped"
        )
    
    # Launch schedule checker in background
    import asyncio
    schedule_task = asyncio.create_task(_schedule_checker_loop())
    
    # Launch health monitor checker in background
    health_monitor_task = asyncio.create_task(_health_monitor_loop())
    
    # Launch publish verification checker in background
    publish_verify_task = asyncio.create_task(_publish_verify_loop())
    
    yield
    
    # Shutdown
    schedule_task.cancel()
    health_monitor_task.cancel()
    publish_verify_task.cancel()
    try:
        await schedule_task
    except asyncio.CancelledError:
        pass
    try:
        await health_monitor_task
    except asyncio.CancelledError:
        pass
    try:
        await publish_verify_task
    except asyncio.CancelledError:
        pass


# ── Orphan detection config ─────────────────────────────────
ORPHAN_TIMEOUT_MINUTES = 60  # Jobs stuck for >1h without heartbeat are declared orphaned


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
        
        # Global guard: defer dispatch if a long-form job is running
        if db.count_active_longform_jobs() > 0:
            logger.debug("Queue consumer deferred: %d active long-form job(s) — retrying next tick",
                        db.count_active_longform_jobs())
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


async def _publish_verify_loop():
    """Background loop: check if warming videos have been auto-published by YouTube.
    
    Runs every 5 minutes independently of frontend polling. Each invocation calls
    get_pipeline_status() which triggers _maybe_trigger_publish_verification() for
    any video whose target_public_at has passed.
    
    This ensures videos transition from 'warming' → 'published' even when nobody
    has the PipelineView open in the frontend.
    """
    import asyncio, logging
    logger = logging.getLogger("autotube.publish_verify")
    
    await asyncio.sleep(60)  # Let API stabilize first
    
    logger.info("Publish verify loop started (interval: 5 min)")
    
    while True:
        try:
            # Pause gate: skip when scheduler is paused
            try:
                from database.db_extended import ExtendedDatabase
                _db = ExtendedDatabase()
                if _db.get_system_state("scheduler_paused") == "true":
                    await asyncio.sleep(60)
                    continue
            except Exception:
                pass
            
            db = get_db()
            data = db.get_pipeline_status()
            warming = data.get("warming", [])
            if warming:
                logger.debug("Publish verify ping: %d warming video(s)", len(warming))
        except Exception as exc:
            logger.warning("Publish verify error: %s", exc)
        
        await asyncio.sleep(300)  # Every 5 minutes


async def _health_monitor_loop():
    """Periodic health check: scans for stuck/failed entities and generates alerts.
    Also broadcasts system snapshots to monitor WebSocket clients."""
    await asyncio.sleep(30)  # Give API time to fully start before first scan
    logger.info("Health monitor loop started (interval: 90s)")
    while True:
        try:
            from api.services.lifecycle_monitor import check_all_health
            db = get_db()
            result = check_all_health(db)
            if result.get("alerts_created", 0) > 0:
                logger.info("Health check: %d new alerts created", result["alerts_created"])
            # Always broadcast system snapshot to monitor WS clients
            try:
                from api.routers.monitor import broadcast_status_snapshot, broadcast_monitor_update
                # Health alert broadcast
                if result.get("alerts_created", 0) > 0:
                    await broadcast_monitor_update({
                        "type": "health_update",
                        "alerts_created": result["alerts_created"],
                        "alerts_resolved": result.get("alerts_resolved", 0),
                    })
                # Periodic system snapshot
                await broadcast_status_snapshot()
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Health monitor error: %s", exc)
        await asyncio.sleep(90)  # Check every 90 seconds


async def _schedule_checker_loop():
    """Background loop that checks schedules, planned slots, orphans, and collects YouTube stats."""
    import asyncio, logging, time
    logger = logging.getLogger("autotube.scheduler")
    
    logger.info("Schedule checker loop started (checks every 5 min)")
    
    # Restore last_stats_collection from DB to avoid immediate re-collection after restart
    last_stats_collection = 0
    try:
        from database.db_extended import ExtendedDatabase
        _sched_db = ExtendedDatabase()
        raw = _sched_db.get_system_state("last_stats_collection")
        if raw:
            last_stats_collection = float(raw)
            logger.info("Restored last_stats_collection from DB: %.0f (age: %.0fh)",
                        last_stats_collection, (time.time() - last_stats_collection) / 3600)
    except Exception:
        pass
    
    last_lifecycle_check = time.time()
    last_midnight_check = time.time()
    last_recovery_check = 0
    last_shorts_recovery_check = 0
    last_smart_replan = 0
    last_slot_calculation = 0
    last_power_word_analysis = 0
    first_run = True

    while True:
        try:
            if first_run:
                first_run = False
                sleep_sec = 0  # immediate first run
            else:
                # ── Adaptive sleep: faster tick when behind schedule ──
                import time as _t_sleep
                try:
                    past_due = int(_sched_db.get_system_state("past_due_slots") or "0")
                except Exception:
                    past_due = 0
                if past_due >= 3:
                    sleep_sec = 60   # urgent catch-up: 1 min
                elif past_due >= 1:
                    sleep_sec = 120  # catch-up: 2 min
                else:
                    sleep_sec = 300  # normal: 5 min
                await asyncio.sleep(sleep_sec)

            # ── Pause gate: skip ALL scheduling when operator paused ──
            _paused = False
            try:
                _paused = _sched_db.get_system_state("scheduler_paused") == "true"
            except Exception:
                pass

            now = time.time()

            if not _paused:
                await _process_due_schedules()
                await _process_shorts_slots()
                await _process_upload_slots()
                long_dispatched = await _process_planned_slots()
                
                # ── Shorts interleaving: when long-form is blocked (pipelining
                #     guard or no slots), try an extra shorts dispatch to fill
                #     the gap. Shorts run independently and don't compete for RAM.
                if not long_dispatched:
                    await _process_shorts_slots()
                    # ── Never-dry guard: if still no dispatch and pipeline
                    #     is running empty, force horizon replan.
                    try:
                        from api.services.planning_service import _ensure_never_dry
                        if _ensure_never_dry(_sched_db):
                            logger.info("Never-dry: emergency replan triggered")
                    except Exception as exc:
                        logger.debug("Never-dry check: %s", exc)
                
                await _queue_consumer()

                # ── Update catch-up state for adaptive sleep ──
                # Combined count of long-form AND shorts past-due slots.
                # Previously only counted planned_slots (long-form), which
                # meant a shorts-only backlog never triggered catch-up mode.
                # Now both queues contribute to the adaptive tick speed.
                try:
                    past_due_long = _sched_db.count_past_due_slots()
                    past_due_shorts = _sched_db.count_past_due_shorts_slots()
                    past_due = past_due_long + past_due_shorts
                    _sched_db.set_system_state("past_due_slots", str(past_due))
                except Exception:
                    pass

                # Process video lifecycle promotion actions every 15 minutes
                if now - last_lifecycle_check > 900:
                    await _process_lifecycle_actions()
                    last_lifecycle_check = now

                # Auto-recovery: replan missing publications every 60 minutes
                if now - last_recovery_check > 3600:
                    await _process_recovery_planner()
                    last_recovery_check = now
                
                # Smart replan: every 30 min during active hours (10:00-23:00)
                local_hour = time.localtime().tm_hour
                if 10 <= local_hour <= 23 and now - last_smart_replan > 1800:
                    try:
                        from api.services.planning_service import smart_replan
                        result = await asyncio.to_thread(smart_replan, db=_sched_db)
                        if result and (result.get("cancelled_count", 0) > 0 or result.get("overcapacity_warn")):
                            logger.info(
                                "Smart replan: cancelled=%d, channels=%s, overcapacity=%s, pending=%d",
                                result.get("cancelled_count", 0),
                                result.get("channels_adjusted", []),
                                result.get("overcapacity_warn", False),
                                result.get("pending_total", 0),
                            )
                    except Exception as exc:
                        logger.debug("Smart replan: %s", exc)
                    last_smart_replan = now

                # Shorts auto-recovery: rebalance shorts every 60 minutes
                if now - last_shorts_recovery_check > 3600:
                    await _process_shorts_recovery_planner()
                    last_shorts_recovery_check = now

                # ── Optimal publish slots: calculate once per day ──
                if now - last_slot_calculation > 86400:
                    await _calculate_optimal_slots()
                    last_slot_calculation = now

                # Regenerate the schedule forecast at midnight (daily)
                if now - last_midnight_check > 3600:
                    try:
                        from database.db_extended import ExtendedDatabase
                        _mid_db = ExtendedDatabase()
                        from api.services.planning_service import compute_and_store_horizon
                        result = await asyncio.to_thread(
                            compute_and_store_horizon, horizon_days=7, db=_mid_db
                        )
                        logger.info("Midnight horizon replan: %d slots, %d days",
                                     result.get("total_slots", 0), result.get("days_planned", 0))
                        from api.services.shorts_scheduler import ensure_today_shorts_scheduled
                        await asyncio.to_thread(ensure_today_shorts_scheduled)
                        last_midnight_check = now
                    except Exception as exc:
                        logger.debug("Midnight schedule refresh: %s", exc)

            await _detect_and_clean_orphans()
            
            # Collect YouTube stats every 6 hours
            now = time.time()
            if STATS_ENABLED and now - last_stats_collection > 21600:  # 6 hours
                await _collect_youtube_stats()
                last_stats_collection = now
                # Persist so a restart doesn't trigger immediate re-collection
                try:
                    _sched_db.set_system_state("last_stats_collection", str(int(now)))
                except Exception:
                    pass

            # ── Weekly power word analysis ──────────────────────────
            # Runs once per week (every 7 days = 604800 seconds).
            # Analyzes historical title performance and regenerates
            # each channel's TITLE_POWER_WORDS list.
            if STATS_ENABLED and now - last_power_word_analysis > 604800:  # 7 days
                try:
                    from pipeline.power_word_analyzer import analyze_all_channels
                    logger.info("Starting weekly power word analysis...")
                    results = await asyncio.to_thread(analyze_all_channels)
                    last_power_word_analysis = now
                    try:
                        _sched_db.set_system_state("last_power_word_analysis", str(int(now)))
                    except Exception:
                        pass
                    if results:
                        for r in results:
                            if r.get("error"):
                                logger.warning("Power word analysis failed for %s: %s",
                                             r.get("channel_slug"), r["error"])
                            else:
                                logger.info(
                                    "Power words updated for %s: %d words (was %d)",
                                    r.get("channel_slug"), r.get("new_count"), r.get("previous_count"),
                                )
                        logger.info("Weekly power word analysis: %d/%d channels updated",
                                   sum(1 for r in results if not r.get("error")), len(results))
                    else:
                        logger.info("Weekly power word analysis: no channels processed")
                except Exception as e:
                    logger.warning("Weekly power word analysis failed: %s", e)
                
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Schedule checker error: {e}")
            await asyncio.sleep(60)


def _get_available_ram_gb() -> float:
    """Return available RAM in GB, preferring SC_AVPHYS_PAGES, falling back to psutil."""
    try:
        import os
        avail_bytes = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        return avail_bytes / (1024 ** 3)
    except Exception:
        try:
            import psutil
            return psutil.virtual_memory().available / (1024 ** 3)
        except ImportError:
            return 99.0  # assume OK


async def _process_planned_slots():
    """Process due planned_slots using the dynamic planning engine.
    
    Returns True if a slot was dispatched, False otherwise (blocked/idle).
    """
    import logging
    logger = logging.getLogger("autotube.planner")
    try:
        from database.db_extended import ExtendedDatabase
        _db = ExtendedDatabase()
        if _db.get_system_state("scheduler_paused") == "true":
            return False  # Operator paused scheduling — skip dispatch
        
        # ── Early RAM gate: skip dispatch if a render is active and RAM is low ──
        # Prevents flooding the DB with error/blocked video/job records when
        # the render worker consumes most available RAM. The gate in
        # start_generation_job_subprocess() still acts as safety net, but this
        # avoids creating records that would be immediately rejected.
        try:
            active_count = _db.count_active_longform_jobs()
            if active_count >= 1:
                avail_gb = _get_available_ram_gb()
                if avail_gb < 3.0:
                    logger.debug(
                        "Planner skip: %d active long-form job(s) + %.1f GB free RAM (need >= 3 GB)",
                        active_count, avail_gb,
                    )
                    return False
        except Exception:
            pass  # Non-critical — let the subprocess guard handle it
    except Exception:
        pass
    try:
        from api.services.planning_service import process_planned_slots as dispatch
        # Capture the event loop BEFORE entering the thread pool.
        # process_planned_slots runs via asyncio.to_thread (thread pool
        # has no event loop), but internally schedules async tasks via
        # asyncio.create_task. Passing the main loop lets it use
        # run_coroutine_threadsafe instead.
        loop = asyncio.get_running_loop()
        result = await asyncio.to_thread(dispatch, loop=loop)
        if result:
            logger.info(
                "Planning dispatched: slot=%d job=%d video=%d channel=%s",
                result["slot_id"], result["job_id"], result["video_id"], result["channel_slug"],
            )
            return True
        return False
    except Exception as e:
        logger.error("Planning dispatch error: %s", e)
        return False


async def _process_upload_slots():
    """F2: Dispatch upload jobs for videos awaiting upload (generate_only completed).
    
    Called every 5 min by the checker loop.
    Uploads can run in parallel with generation (network vs CPU).
    """
    import logging
    logger = logging.getLogger("autotube.upload")
    try:
        from api.services.upload_scheduler import dispatch_due_uploads
        loop = asyncio.get_running_loop()
        result = await asyncio.to_thread(dispatch_due_uploads, loop)
        if result:
            logger.info(
                "Upload dispatched: video=%d job=%d channel=%s pub=%s",
                result["video_id"], result["job_id"], result["channel_slug"],
                (result.get("target_public_at") or "?")[:16] if result.get("target_public_at") else "?",
            )
    except Exception as e:
        logger.error("Upload dispatch error: %s", e)


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


async def _process_shorts_recovery_planner():
    """Check for channels behind/ahead daily shorts target and rebalance.

    Runs every 60 min. Only active between 10:00-23:00 local time.
    Delegates to api.services.shorts_recovery_planner.auto_recover_shorts.
    """
    import asyncio
    import logging
    logger = logging.getLogger("autotube.shorts_recovery")
    try:
        from api.services.shorts_recovery_planner import auto_recover_shorts
        result = await asyncio.to_thread(auto_recover_shorts)
        total = (result.get("recovered_count", 0) +
                 result.get("cancelled_count", 0))
        if total > 0:
            logger.info(
                "Shorts recovery: +%d added, -%d cancelled "
                "across %d channel(s): %s",
                result.get("recovered_count", 0),
                result.get("cancelled_count", 0),
                len(result.get("channels_affected", [])),
                ", ".join(result.get("channels_affected", [])),
            )
    except Exception as e:
        logger.error("Shorts recovery planner error: %s", e)


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
        result = await asyncio.to_thread(dispatch_next_due_slot)
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
        from database.db_extended import ExtendedDatabase
        _db = ExtendedDatabase()
        if _db.get_system_state("scheduler_paused") == "true":
            return  # Operator paused scheduling — skip dispatch
    except Exception:
        pass
    try:
        from api.services.shorts_scheduler import dispatch_next_due_shorts_slot
        # Capture the event loop BEFORE entering the thread pool.
        # dispatch_next_due_shorts_slot runs via asyncio.to_thread (thread pool
        # has no event loop), but internally calls asyncio.create_task which
        # requires one. Passing the main loop lets it use
        # run_coroutine_threadsafe instead.
        loop = asyncio.get_running_loop()
        result = await asyncio.to_thread(dispatch_next_due_shorts_slot, loop=loop)
        if result:
            logger.info(
                "Shorts slot dispatched: slot=%d channel=%s type=%s",
                result["slot_id"], result["channel_slug"], result["short_type"],
            )
    except Exception as e:
        logger.error("Shorts dispatch error: %s", e)


async def _calculate_optimal_slots():
    """Calculate optimal publish slots for all channels using YouTube Analytics data.
    
    Runs once per day. Fetches viewer activity by hour from YT Analytics API,
    crosses with historical DB performance, and computes 3 optimal time slots
    (per channel, per content type: long-form and shorts).
    
    If slots change significantly (>1h), triggers replanning:
    - Long-form: updates target_public_at for pending planned_slots
    - Shorts: regenerates shorts_planned_slots for the 7-day horizon
    """
    import logging
    logger = logging.getLogger("autotube.optimal_slots")
    try:
        from api.services.optimal_slots_calculator import calculate_and_replan_all
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        
        result = calculate_and_replan_all(db)
        
        channels = result.get("channels_processed", 0)
        slots = result.get("slots_calculated", 0)
        replanned = result.get("channels_replanned", 0)
        long_r = result.get("long_replanned", 0)
        shorts_r = result.get("shorts_replanned", 0)
        
        logger.info(
            "Optimal slots daily calc: %d channels, %d slots stored. "
            "Replanned: %d channels (%d long, %d shorts)",
            channels, slots, replanned, long_r, shorts_r,
        )
        
        # Log per-channel details
        for slug, detail in result.get("details", {}).items():
            if detail.get("error"):
                logger.warning("  %s: ERROR — %s", slug, detail["error"])
            else:
                long_s = detail.get("long_slots", [])
                short_s = detail.get("short_slots", [])
                niche = detail.get("niche", {})
                logger.info(
                    "  %s: long=%s short=%s niche=%s changed=(long:%s short:%s)",
                    slug,
                    [f"{s['hour']:02d}:00 (c={s.get('confidence',0):.2f})" for s in long_s],
                    [f"{s['hour']:02d}:00 (c={s.get('confidence',0):.2f})" for s in short_s],
                    niche.get("description", "unknown") if niche else "unknown",
                    detail.get("long_changed", False),
                    detail.get("shorts_changed", False),
                )
                
    except Exception as e:
        logger.error("Optimal slots calculation failed: %s", e)


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
        
        # ── Global guard: skip if a long-form job is running ──
        if db.count_active_longform_jobs() > 0:
            logger.debug("Schedule #%d deferred: %d active long-form job(s) running globally",
                        s["id"], db.count_active_longform_jobs())
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
            # ── Enter dispatch critical section ──────────────────
            from api.services.generation_service import _DISPATCH_LOCK
            
            with _DISPATCH_LOCK:
                # Re-check global guard under lock (belts-and-suspenders)
                from database.db_extended import ExtendedDatabase
                _db2 = ExtendedDatabase()
                if _db2.count_active_longform_jobs() > 0:
                    logger.debug("Schedule #%d deferred (under lock): active long-form job detected", s["id"])
                    try:
                        conn.execute(
                            "UPDATE content_schedules SET next_run_at = datetime('now', 'localtime', '+5 minutes') "
                            "WHERE id = ?", (s["id"],)
                        )
                        conn.commit()
                    except Exception:
                        pass
                    continue
                
                # Launch the generation job
                db2 = ExtendedDatabase()
                
                # Create a video record
                cursor = conn.execute(
                    "INSERT INTO videos (canal, channel_id, video_path, status, progress, created_at) VALUES (?, ?, '', 'generating', 0, CURRENT_TIMESTAMP)",
                    (s["channel_slug"], s["channel_id"]),
                )
                conn.commit()
                video_id = cursor.lastrowid
                
                # Create job record — mark running IMMEDIATELY (close TOCTOU race)
                job_id = db2.create_job(s["channel_id"], s["action"], video_id)
                db2.update_job(job_id, status="running")
                
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
            # ── End dispatch critical section ────────────────────
            
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


# ── Stuck insight detection config ────────────────────────
INSIGHT_ORPHAN_TIMEOUT_MINUTES = 30  # Insights stuck >30min without completion are declared orphaned


def _cleanup_orphaned_insights(db, logger, timeout_minutes=INSIGHT_ORPHAN_TIMEOUT_MINUTES):
    """Mark stuck channel_insights rows as failed if they've been 'processing' too long.
    
    Threads in the single-thread _INSIGHTS_EXECUTOR can hang on LLM API calls
    or get killed on server restart, leaving rows with status='processing' forever.
    The frontend polls these rows every 3s with no timeout, so a stuck row
    causes an infinite loading screen.
    """
    import logging as _log_mod
    if logger is None:
        logger = _log_mod.getLogger("autotube.insights")
    
    try:
        cleaned = db.execute(
            """UPDATE channel_insights
               SET status = 'failed',
                   error_msg = 'Analysis timed out — auto-cleaned by orphan detector (' || ? || ' min timeout)'
               WHERE status = 'processing'
                 AND generated_at < datetime('now', ?)""",
            (str(timeout_minutes), f'-{timeout_minutes} minutes')
        )
        count = db.total_changes
        if count > 0:
            db.commit()
            logger.warning("Orphaned insights cleaned: %d stale processing rows → failed", count)
    except Exception as exc:
        logger.debug("Insight orphan cleanup skipped (non-critical): %s", exc)


def _detect_ghost_workers(db, logger):
    """Kill worker processes whose --job-id does not exist in generation_jobs.
    
    These are truly orphaned: the worker subprocess is alive but its job row
    was deleted from the DB (channel deletion cascade, manual cleanup, etc).
    Without this detector, they would run forever doing work that disappears.
    """
    import os as _os
    import re as _re
    import signal as _signal
    import subprocess as _subprocess
    
    try:
        result = _subprocess.run(
            ["pgrep", "-f", "full_pipeline_worker.py"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        
        for pid_str in result.stdout.strip().split('\n'):
            pid_str = pid_str.strip()
            if not pid_str:
                continue
            try:
                pid = int(pid_str)
                # Read cmdline to extract --job-id
                with open(f'/proc/{pid}/cmdline', 'r') as f:
                    cmdline = f.read().replace('\x00', ' ')
                match = _re.search(r'--job-id (\d+)', cmdline)
                if not match:
                    continue
                job_id = int(match.group(1))
                
                # Check if job exists in DB
                job = db.get_job(job_id)
                if job is None:
                    # Also check if it exists in the running set (reconnect_active_workers
                    # may have already marked it failed but row still exists)
                    logger.warning(
                        "GHOST WORKER DETECTED: PID %d (job %d) — "
                        "job row deleted from DB. Killing...", pid, job_id
                    )
                    _os.kill(pid, _signal.SIGTERM)
                    import time as _time
                    _time.sleep(2)
                    # Force kill if still alive
                    try:
                        _os.kill(pid, _signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            except (ValueError, ProcessLookupError, FileNotFoundError):
                pass  # process already gone
    except Exception as exc:
        logger.debug("Ghost worker detection scan failed (non-critical): %s", exc)


def _detect_and_clean_orphans_sync():
    """Synchronous orphan detection logic (runs in thread pool).
    
    Also prunes old cancelled/skipped planned slots and detects ghost workers
    (worker processes whose job_id was deleted from the DB).
    """
    import logging
    logger = logging.getLogger("autotube.orphans")
    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        result = db.cleanup_orphaned_jobs(timeout_minutes=ORPHAN_TIMEOUT_MINUTES)
        
        if result["jobs_failed"] == 0 and result["videos_reset"] == 0:
            logger.debug("Orphan check: all clear")
        
        # ── Ghost worker detection ──
        # Detect worker processes whose --job-id no longer exists in the DB.
        # These are truly orphaned: the process is alive but its DB row was
        # deleted (e.g., channel deletion cascade, manual cleanup).
        _detect_ghost_workers(db, logger)
        
        # ── Stuck insight cleanup ──
        # channel_insights rows stuck in 'processing' (thread hang, server restart)
        # cause the frontend InsightsTab to show an infinite loading screen.
        _cleanup_orphaned_insights(db, logger)
        
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
        
        # ── Pre-count: how many pending actions exist across all channels? ──
        try:
            all_due = db.get_due_lifecycle_actions()
            total_pending = len(all_due)
            # Count go_public actions specifically
            go_public_pending = sum(1 for a in all_due if a.get("action_type") == "go_public")
        except Exception:
            total_pending, go_public_pending = 0, 0
        
        grand_total = {"processed": 0, "succeeded": 0, "failed": 0}
        
        for ch in channels:
            slug = ch.get("slug")
            if not slug:
                continue
            try:
                mgr = VideoLifecycleManager(slug)
                result = mgr.process_due_actions()
                grand_total["processed"] += result.get("processed", 0)
                grand_total["succeeded"] += result.get("succeeded", 0)
                grand_total["failed"] += result.get("failed", 0)
            except Exception as exc:
                logger.warning("Lifecycle [%s] error: %s", slug, exc)
        
        # ── Heartbeat summary every cycle ──
        if grand_total["processed"] > 0:
            logger.info(
                "🔄 Lifecycle checker: %d acciones procesadas (%d ok, %d fallos) "
                "| %d pendientes totales (go_public=%d)",
                grand_total["processed"], grand_total["succeeded"], grand_total["failed"],
                total_pending, go_public_pending,
            )
        elif total_pending > 0:
            logger.debug(
                "🔄 Lifecycle checker: 0 vencidas ahora | %d pendientes futuras (go_public=%d)",
                total_pending, go_public_pending,
            )
        else:
            # Only log once every ~30 min to avoid noise
            import time as _t
            if not hasattr(_process_lifecycle_actions, "_last_empty_log") or \
               _t.time() - _process_lifecycle_actions._last_empty_log > 1800:
                logger.debug("🔄 Lifecycle checker: sin acciones pendientes")
                _process_lifecycle_actions._last_empty_log = _t.time()
    except Exception as exc:
        logger.error("Lifecycle processor error: %s", exc)


import time as _time_module

# ── Stats collection timeout watchdog ───────────────────────────
_STATS_COLLECTION_TIMEOUT = 300  # seconds — auto-reset to error if stuck "running" > 5 min

# Tracks the state of on-demand stats collection so the UI can show
# real progress/result feedback (survives page reloads + server restarts).
STATS_COLLECTION_STATE = {
    "status": "idle",          # idle | running | success | error
    "started_at": None,        # epoch seconds
    "finished_at": None,       # epoch seconds
    "channels": [],            # per-channel summaries
    "error": None,             # error message if status == error
}


def _pin_stats_state(key: str):
    """Sync STATS_COLLECTION_STATE → system_state table so it survives restarts."""
    try:
        from database.db_extended import ExtendedDatabase
        _db = ExtendedDatabase()
        import json as _json
        _db.set_system_state(key, _json.dumps(STATS_COLLECTION_STATE, default=str))
    except Exception:
        pass


def _unpin_stats_state(key: str):
    """Restore STATS_COLLECTION_STATE from system_state table on startup."""
    try:
        from database.db_extended import ExtendedDatabase
        _db = ExtendedDatabase()
        raw = _db.get_system_state(key)
        if raw:
            import json as _json
            saved = _json.loads(raw)
            if saved.get("status") == "running":
                # Stuck "running" means the server crashed mid-collection — mark as error
                STATS_COLLECTION_STATE.update({
                    "status": "error",
                    "started_at": saved.get("started_at"),
                    "finished_at": _time_module.time(),
                    "channels": saved.get("channels", []),
                    "error": "Interrupted by server restart",
                })
            else:
                STATS_COLLECTION_STATE.update(saved)
    except Exception:
        pass


def _reset_stale_collection_state():
    """If STATS_COLLECTION_STATE has been 'running' longer than the timeout,
    auto-reset to error so the watchdog unlocks the dashboard button."""
    if STATS_COLLECTION_STATE["status"] != "running":
        return False
    started = STATS_COLLECTION_STATE.get("started_at")
    if not started:
        return False
    elapsed = _time_module.time() - started
    if elapsed > _STATS_COLLECTION_TIMEOUT:
        STATS_COLLECTION_STATE.update({
            "status": "error",
            "finished_at": _time_module.time(),
            "error": f"Timed out after {elapsed:.0f}s",
        })
        _pin_stats_state("stats_collection_state")
        return True
    return False


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
    _pin_stats_state("stats_collection_state")

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
                    "Stats collected for %s: %s videos, %s shorts, %s analytics, channel=%s",
                    slug,
                    result.get("videos_updated", 0),
                    result.get("shorts_updated", 0),
                    result.get("analytics_updated", 0),
                    result.get("channel_updated", False),
                )
                STATS_COLLECTION_STATE["channels"].append({
                    "slug": slug,
                    "ok": "error" not in result,
                    "videos_updated": result.get("videos_updated", 0),
                    "shorts_updated": result.get("shorts_updated", 0),
                    "analytics_updated": result.get("analytics_updated", 0),
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
        _pin_stats_state("stats_collection_state")
    except Exception as exc:
        logger.error("Stats collector error: %s", exc)
        STATS_COLLECTION_STATE.update({
            "status": "error",
            "finished_at": _time_module.time(),
            "error": str(exc),
        })
        _pin_stats_state("stats_collection_state")


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
app.include_router(gamification.router, prefix="/api", tags=["Gamification"])
app.include_router(social_accounts.router, prefix="/api/channels", tags=["Social Media"])
app.include_router(monitor_router.router, prefix="/api", tags=["Monitor"])
app.include_router(insights.router, prefix="/api/channels", tags=["Insights AI"])

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
    _reset_stale_collection_state()  # auto-recover from stuck "running"
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
    _reset_stale_collection_state()  # auto-recover from stuck "running"
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
    
    return FileResponse(video_path, media_type="video/mp4", headers=NO_CACHE_MEDIA)


@app.get("/api/thumbnail/{video_id}")
async def serve_thumbnail(video_id: int, request: Request):
    """Serve the thumbnail for a video with aggressive anti-cache headers + ETag.

    The ETag is built from the file's mtime + size so any regeneration
    (which creates a new file or overwrites with different content) will
    produce a new ETag, forcing the browser to re-fetch.
    """
    db = get_db()
    v = db.get_video(video_id)
    if not v or not v.get("thumbnail_path"):
        raise HTTPException(404, "Thumbnail not found")

    thumb_path = resolve_media_path(v["thumbnail_path"])
    if thumb_path is None:
        raise HTTPException(404, "Thumbnail file not found on disk")

    # ── Build ETag from file mtime + size ──────────────────
    stat = os.stat(thumb_path)
    etag = f'"{stat.st_mtime:.0f}-{stat.st_size}"'

    # ── Check If-None-Match for conditional 304 ─────────────
    if_none = request.headers.get("if-none-match", "")
    if if_none and if_none.strip('"') == etag.strip('"'):
        return Response(status_code=304)

    # ── Aggressive anti-cache: ETag + zero-age + no-store ──
    headers = {
        **NO_CACHE_MEDIA,
        "ETag": etag,
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    }

    return FileResponse(thumb_path, media_type="image/jpeg", headers=headers)


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
