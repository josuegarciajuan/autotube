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
from api.routers import cross_platform as cross_platform_router
from api.routers import monitor as monitor_router
from api.routers import insights
from api.routers import view_gap as view_gap_router
from api.routers import quota as quota_router
from api.routers import redistribution as redistribution_router
from api.routers import pacing as pacing_router
from database.db_extended import migrate_v2, ExtendedDatabase
from database.db import init_db
from config.settings import (TOKENS_DIR, DATABASE_PATH, STATS_ENABLED, STATS_AUTO_COLLECT,
                             YT_REMEDIATION_MODE, THUMBNAIL_VERIFY_ENABLED,
                             UPLOAD_HEALTH_CHECKER_ENABLED)

logger = logging.getLogger("autotube.main")

# ── Lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ⛔ Invariante: STATS_AUTO_COLLECT NUNCA debe activarse.
    # Si alguien lo cambia en settings.py o .env, el servidor se niega a arrancar.
    if STATS_AUTO_COLLECT:
        raise SystemExit(
            "FATAL: STATS_AUTO_COLLECT=True. Esto consume quota de YouTube API "
            "innecesariamente. Debe ser False siempre. La recoleccion de stats "
            "solo se activa manualmente desde el dashboard."
        )
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
    
    # ── Systemd watchdog ping ─────────────────────────────────
    # If running under systemd with WatchdogSec set, send periodic
    # pings so systemd knows the process is alive. Without this,
    # WatchdogSec would kill the service after 90s even if healthy.
    #
    # ago 2026: los pings se mandan desde un HILO daemon dedicado, no desde el
    # event loop. Bajo saturación de CPU (load alto, servicio con Nice=10) el
    # event loop puede congelarse durante minutos; una task asyncio dejaría de
    # pingear y systemd mataría el proceso con SIGABRT → ventanas de 503 en el
    # panel (Apache → proxy → backend). Un hilo con time.sleep/Event.wait(30)
    # necesita microsegundos por wake y sobrevive a la inanición de CPU.
    _watchdog_thread = None
    _watchdog_stop = None
    _sd = None  # exposed at lifespan scope for inline pings during sync startup
    try:
        from systemd import daemon as _sd
        import threading as _threading_wd
        if _sd.booted():
            _sd.notify("READY=1")
            _wlogger = logging.getLogger("autotube.watchdog")
            _wlogger.info("systemd watchdog initialized (thread interval=30s, WatchdogSec=600s)")

            def _watchdog_ping_loop(stop_event):
                while not stop_event.is_set():
                    try:
                        # Do not report the service healthy if the critical
                        # scheduler has stopped heartbeating.
                        from api.services.lifecycle_monitor import (
                            get_task_heartbeat_age, TASK_TIMEOUTS,
                        )
                        _age = get_task_heartbeat_age("schedule_checker")
                        if _age is not None and _age > TASK_TIMEOUTS["schedule_checker"]:
                            _wlogger.critical(
                                "systemd watchdog withheld: schedule_checker heartbeat stale"
                            )
                            return
                        _sd.notify("WATCHDOG=1")
                    except Exception:
                        pass
                    stop_event.wait(30)

            _watchdog_stop = _threading_wd.Event()
            _watchdog_thread = _threading_wd.Thread(
                target=_watchdog_ping_loop,
                args=(_watchdog_stop,),
                name="systemd-watchdog-ping",
                daemon=True,
            )
            _watchdog_thread.start()
    except ImportError:
        pass  # not running under systemd or python3-systemd not installed
    
    # ── Inline watchdog helper ─────────────────────────────────
    # Allows sending WATCHDOG pings from synchronous startup blocks
    # (init_db, migrate_v2, config sync, orphan cleanup) so systemd
    # doesn't kill the process if these blocks exceed the ping interval.
    def _ping_watchdog_now():
        if _sd is not None:
            try:
                _sd.notify("WATCHDOG=1")
            except Exception:
                pass
    
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
    _ping_watchdog_now()  # keep systemd watchdog alive during sync startup

    # ── Integrity check: PRAGMA quick_check (Fase cuota ago 2026) ──
    # La noche del 15-ago la DB dio "database disk image is malformed" durante
    # ~2h y dejó los loops de recuperación caídos. Detectar corrupción al
    # arranque permite alertar en vez de fallar en silencio.
    try:
        import sqlite3 as _sq3
        from config.settings import DATABASE_PATH
        _qc = _sq3.connect(str(DATABASE_PATH), timeout=10)
        _qc_result = _qc.execute("PRAGMA quick_check(1)").fetchone()
        _qc.close()
        if _qc_result and _qc_result[0] != "ok":
            logging.getLogger("autotube.startup").critical(
                "⚠️ DB integrity check FAILED: %s — repair with "
                "'sqlite3 autotube.db .recover' if errors persist", _qc_result[0],
            )
        else:
            logging.getLogger("autotube.startup").info("DB integrity check: ok")
    except Exception as _qc_exc:
        logging.getLogger("autotube.startup").warning(
            "DB integrity check skipped: %s", _qc_exc,
        )

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
    _ping_watchdog_now()  # config sync can be slow on first run
    
    # ── Clean up orphaned ffmpeg/edge-tts/yt-dlp processes from prior runs ──
    try:
        from api.services.generation_service import _kill_orphaned_ffmpeg
        _kill_orphaned_ffmpeg()
        logging.getLogger("autotube.startup").info("Orphan process cleanup completed")
    except Exception as exc:
        logging.getLogger("autotube.startup").warning("Orphan cleanup skipped: %s", exc)
    
    # ── Clean up stuck channel_insights from prior runs ──
    # If the server was restarted, all in-memory threads are killed.
    # Any processing row is guaranteed dead — fail them immediately.
    try:
        from database.db_extended import ExtendedDatabase
        _insight_db = ExtendedDatabase()
        count = _insight_db.fail_all_processing_insights(
            "Server restart — all analysis threads killed"
        )
        if count > 0:
            logging.getLogger("autotube.startup").warning(
                "Insight orphan cleanup: %d processing rows force-failed "
                "(server restart killed all threads)", count
            )
        else:
            logging.getLogger("autotube.startup").info("Insight orphan check: no stale processing rows")
    except Exception as exc:
        logging.getLogger("autotube.startup").warning("Insight orphan cleanup skipped: %s", exc)

    # ── PAUSE GATE: skip all auto-planning/dispatch when operator paused scheduling ──
    _scheduler_paused = False
    _startup_tasks = None  # fix ago 2026: debe cancelarse en shutdown para no colgar el apagado
    try:
        from database.db_extended import ExtendedDatabase
        _paused_db = ExtendedDatabase()
        _scheduler_paused = _paused_db.get_system_state("scheduler_paused") == "true"
    except Exception:
        pass

    if not _scheduler_paused:
        # ── Defer all heavy startup work to background ──
        # auto_recover_on_startup, reconnect_active_workers, planning, and
        # shorts generation all do synchronous I/O or async operations that
        # block the lifespan. Running them in the background allows uvicorn
        # to start accepting connections within seconds instead of minutes.
        import asyncio as _asyncio
        
        async def _startup_heavy_tasks():
            await _asyncio.sleep(3)  # Brief pause to let API stabilize first
            _loop = _asyncio.get_running_loop()
            _logger = logging.getLogger("autotube.startup")
            
            # ── Auto-recover failed/interrupted videos from previous run ──
            try:
                from api.services.generation_service import auto_recover_on_startup
                await auto_recover_on_startup()
                _logger.info("Auto-recovery completed")
            except Exception as exc:
                _logger.warning("Auto-recovery skipped: %s", exc)

            # ── Reconnect to running worker subprocesses ──
            try:
                from api.services.generation_service import reconnect_active_workers
                await reconnect_active_workers()
                _logger.info("Worker reconnection completed")
            except Exception as exc:
                _logger.warning("Worker reconnection skipped: %s", exc)

            # ── Spam-block holds: colchón 6h + retener publicaciones programadas ──
            # Asegura que los canales bloqueados por spam no publiquen nada hasta
            # expirar el bloqueo + colchón (reprograma publishAt ya agendados).
            try:
                from api.services.spam_mitigation import ensure_spam_holds
                from database.db_extended import ExtendedDatabase as _SpamDB
                _holds = await _loop.run_in_executor(
                    None, lambda: ensure_spam_holds(_SpamDB())
                )
                if _holds.get("buffer_extended") or _holds.get("held"):
                    _logger.warning(
                        "Spam holds: colchón extendido en %d canal(es), %d publicaciones retenidas (%s)",
                        _holds.get("buffer_extended", 0),
                        _holds.get("held", 0),
                        ", ".join(_holds.get("channels", [])),
                    )
            except Exception as exc:
                _logger.warning("Spam holds skipped: %s", exc)
            
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
                
                # v14: Cleanup excessive pending slots for today to prevent spam
                from api.services.shorts_scheduler import cleanup_excessive_shorts_slots
                cleanup_result = await _loop.run_in_executor(
                    None, cleanup_excessive_shorts_slots,
                )
                if cleanup_result.get("cancelled_native", 0) > 0 or cleanup_result.get("cancelled_clip", 0) > 0:
                    _logger.warning(
                        "Shorts cleanup: cancelled %d native + %d clip excess slots",
                        cleanup_result.get("cancelled_native", 0),
                        cleanup_result.get("cancelled_clip", 0),
                    )
            except Exception as exc:
                _logger.warning("Shorts scheduler init skipped: %s", exc)
        
        _startup_tasks = _asyncio.create_task(_startup_heavy_tasks())
        logging.getLogger("autotube.startup").info(
            "Deferred startup tasks: auto-recover + worker reconnect + planning + shorts will run in background"
        )
    else:
        # Check if paused due to quota exhaustion and quota has already reset.
        # When the API restarts after a deployment/SIGKILL, the recovery loop
        # may not have had a chance to auto-resume. We catch that here.
        _is_quota_pause = False
        _quota_reset_passed = False
        try:
            # Fase cuota: migrar el breaker global antiguo a su clave por proyecto
            _paused_db.backfill_project_quota_breakers()
            _is_quota_pause = _paused_db.get_system_state("quota_exhausted_at") not in (None, "")
            if _is_quota_pause:
                _reset_info = _paused_db.get_quota_reset_time()
                if _reset_info.get("exhausted") and _reset_info.get("reset_at_utc"):
                    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                    _reset_at = _dt.fromisoformat(_reset_info["reset_at_utc"])
                    if _reset_at.tzinfo is None:
                        _reset_at = _reset_at.replace(tzinfo=_tz.utc)
                    _now = _dt.now(_tz.utc)
                    _quota_reset_passed = _now >= _reset_at + _td(minutes=15)
        except Exception:
            pass

        if _quota_reset_passed:
            _paused_db.clear_quota_exhausted()
            logging.getLogger("autotube.startup").warning(
                "Quota reset already passed — scheduler auto-resumed at startup"
            )
        else:
            logging.getLogger("autotube.startup").warning(
                "SCHEDULER PAUSED — auto-recovery, planning, and shorts scheduling skipped"
            )
    
    # Launch schedule checker in background
    import asyncio
    def _supervised_loop(_name, _factory):
        from api.services.task_watchdog import supervise_loop
        _on_stale = None
        if _name == "schedule_checker":
            # A stale scheduler may still have a synchronous to_thread
            # operation running. Restarting only that asyncio task could
            # duplicate uploads/dispatches, so recover at process level.
            def _restart_api_after_stale(task_name):
                logging.getLogger("autotube.task_watchdog").critical(
                    "Critical loop '%s' stale; requesting controlled API restart",
                    task_name,
                )
                import os as _os
                import signal as _signal
                _os.kill(_os.getpid(), _signal.SIGTERM)
            _on_stale = _restart_api_after_stale
        return asyncio.create_task(
            supervise_loop(_name, _factory, on_stale=_on_stale)
        )

    schedule_task = _supervised_loop("schedule_checker", _schedule_checker_loop)
    
    # Launch health monitor checker in background
    health_monitor_task = _supervised_loop("health_monitor", _health_monitor_loop)
    
    # Launch publish verification checker in background
    publish_verify_task = _supervised_loop("publish_verify", _publish_verify_loop)
    
    # Launch upload health checker in background (processing status monitoring)
    # This loop intentionally exits when the feature flag is disabled; do not
    # turn that expected no-op into a bounded restart sequence.
    health_checker_task = (
        _supervised_loop("upload_health_checker", _upload_health_checker_loop)
        if UPLOAD_HEALTH_CHECKER_ENABLED
        else asyncio.create_task(_upload_health_checker_loop())
    )
    
    # ── Shorts backfill PERMANENTLY DISABLED (see AGENTS.md invariant) ──
    # New shorts already include long-form links via build_short_description() at upload time.
    # The backfill loop consumed ~36k YT API quota units/day on already-linked shorts.
    # shorts_backfill_task = asyncio.create_task(_shorts_backfill_loop())
    shorts_backfill_task = None
    
    # Launch quota recovery loop (auto-resume scheduler after 6h)
    quota_recovery_task = _supervised_loop("quota_recovery", _quota_recovery_loop)

    # Launch gradual resume phase loop (avance autónomo de fases post-strike)
    resume_phase_task = _supervised_loop("resume_phase", _resume_phase_loop)

    # Launch social redistribution loop (backfill espejo progresivo)
    redistribution_task = _supervised_loop("redistribution", _redistribution_loop)

    # ── Cobertura de publicación (ago 2026): enforcer de la programación ──
    # Audita cada canal libre y dispara el repack si algún día próximo queda sin
    # cubrir (garantiza que "lo planeado se cumpla"). Loop independiente de 10 min.
    publish_coverage_task = _supervised_loop("publish_coverage", _publish_coverage_loop)

    # ── Estado real de publicación de shorts (ago 2026) ──
    # Reconcilia la verdad externa (yt-dlp + RSS, 0 cuota) de los shorts recientes
    # en las columnas derivadas (yt_visibility / yt_checked_at). La barra y los
    # endpoints leen esa verdad, no el status optimista que se marcaba al subir.
    yt_state_task = _supervised_loop("yt_state_reconcile", _yt_state_reconcile_loop)

    yield
    
    # Shutdown
    # fix ago 2026: apagado NO bloqueante. Los loops corren operaciones
    # síncronas vía run_in_executor (threads que no se pueden cancelar), por lo
    # que `await task` podía colgarse >60s y systemd mataba el servicio con
    # SIGKILL ("Failed with result 'timeout'"). Se cancela todo y se espera con
    # timeout acotado — el proceso sale en <10s.
    if _watchdog_stop is not None:
        _watchdog_stop.set()  # detiene el hilo daemon del watchdog
    _shutdown_tasks = [
        schedule_task, health_monitor_task, publish_verify_task,
        health_checker_task, quota_recovery_task, resume_phase_task,
        redistribution_task,
        publish_coverage_task,
        yt_state_task,
    ]
    if _startup_tasks is not None:
        _shutdown_tasks.append(_startup_tasks)
    for _t in _shutdown_tasks:
        try:
            _t.cancel()
        except Exception:
            pass
    try:
        await asyncio.wait_for(
            asyncio.gather(*_shutdown_tasks, return_exceptions=True),
            timeout=8,
        )
    except asyncio.TimeoutError:
        logging.getLogger("autotube.startup").warning(
            "Shutdown: %d task(s) did not cancel within 8s — proceeding (threads del run_in_executor no son cancelables)",
            len(_shutdown_tasks),
        )
    shorts_backfill_task = None  # permanently disabled

    # ── WAL checkpoint at shutdown (Fase cuota ago 2026) ──
    # Evita WALs gigantes/corrupción al matar el proceso con cambios sin
    # checkpoint. Best-effort — nunca debe bloquear el apagado.
    try:
        import sqlite3 as _sq3
        from config.settings import DATABASE_PATH
        _wconn = _sq3.connect(str(DATABASE_PATH), timeout=5)
        _wconn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _wconn.close()
        logging.getLogger("autotube.startup").info("WAL checkpoint done at shutdown")
    except Exception as _wal_exc:
        logging.getLogger("autotube.startup").warning(
            "WAL checkpoint failed at shutdown: %s", _wal_exc,
        )


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
        from api.services.lifecycle_monitor import touch_task_heartbeat
        touch_task_heartbeat("queue_consumer")
    except Exception:
        pass
    
    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        
        # Find next queued job
        next_job = db.get_next_queued_job()
        if not next_job:
            return
        
        # Guard: don't dispatch if this channel already has a RUNNING job
        # Uses get_running_job_for_channel (not get_active_job_for_channel) so
        # a queued marathon job does not block itself from being dispatched.
        active_for_channel = db.get_running_job_for_channel(next_job["channel_id"])
        if active_for_channel:
            logger.debug("Queue consumer: channel %d already has running job #%d — skipping",
                        next_job["channel_id"], active_for_channel["id"])
            return
        
        # Global guard: defer dispatch if a long-form job is running
        # Uses count_running_longform_jobs (not count_active_longform_jobs)
        # so queued marathon jobs do not block the consumer.
        if db.count_running_longform_jobs() > 0:
            logger.debug("Queue consumer deferred: %d running long-form job(s) — retrying next tick",
                        db.count_running_longform_jobs())
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
        
        action = next_job.get("action", "generate_and_upload")

        logger.info(
            "Queue consumer: dispatching job #%d (channel_id=%d, action=%s)",
            next_job["id"], next_job["channel_id"], action,
        )

        if action == "reassemble":
            from api.services.generation_service import _run_reassembly_job
            asyncio.create_task(_run_reassembly_job(
                job_id=next_job["id"],
                video_id=next_job["video_id"],
            ))
        else:
            from api.services.generation_service import start_generation_job, start_generation_job_subprocess, USE_SUBPROCESS_WORKER

            if USE_SUBPROCESS_WORKER:
                asyncio.create_task(start_generation_job_subprocess(
                    job_id=next_job["id"],
                    channel_id=next_job["channel_id"],
                    video_id=next_job["video_id"],
                    action=action,
                ))
            else:
                asyncio.create_task(start_generation_job(
                    job_id=next_job["id"],
                    channel_id=next_job["channel_id"],
                    video_id=next_job["video_id"],
                    action=action,
                ))
        
    except Exception as e:
        logger.error("Queue consumer error: %s", e)


async def _publish_verify_loop():
    """Background loop: check if warming videos have been auto-published by YouTube.

    Runs every 5 minutes independently of frontend polling. For every warming
    (uploaded_private) video whose target_public_at has passed, and for orphaned
    'uploaded' videos, it triggers a quota-FREE wall-scrape verification thread
    (public RSS feed) that transitions them to 'published' when detected, or
    raises a system alert after a grace window if still not public.

    This ensures videos transition from 'warming' → 'published' even when nobody
    has the PipelineView open in the frontend — and does NOT consume YouTube
    API quota (so it keeps working while the API quota is exhausted).
    """
    import asyncio, logging
    logger = logging.getLogger("autotube.publish_verify")
    
    await asyncio.sleep(60)  # Let API stabilize first
    
    logger.info("Publish verify loop started (interval: 5 min, quota-free scraping)")
    
    while True:
        try:
            from api.services.lifecycle_monitor import touch_task_heartbeat as _tth
            _tth("publish_verify")
            # Pause gate: skip only when the operator explicitly paused the
            # scheduler. Scraping consumes NO quota, so we intentionally ignore
            # quota-exhaustion and remediation mode here (unlike the old
            # API-based verification, which was gated off by quota and left
            # warming videos stuck in 'uploaded_private' forever).
            try:
                from database.db_extended import ExtendedDatabase
                _db = ExtendedDatabase()
                if _db.get_system_state("scheduler_paused") == "true":
                    await asyncio.sleep(60)
                    continue
            except Exception:
                pass
            
            db = get_db()
            # Offload to thread pool — get_pipeline_status() makes sync sqlite3
            # calls that can block the event loop during lock contention.
            data = await asyncio.to_thread(db.get_pipeline_status)
            warming = data.get("warming", [])
            orphaned = data.get("orphaned", [])
            
            # ── Trigger publish verification ─────────────────────────────
            # v24 regression fix: the perf commit 912a6c6 removed these triggers
            # from get_pipeline_status() (to avoid spawning threads on every 60s
            # frontend poll), claiming this loop already handled them — but the
            # loop never actually called them. This left uploaded_private videos
            # stuck in 'warming' forever. Restore the triggers here, in the
            # 5-minute background loop, so verification still runs without
            # hammering the API from frontend polling.
            try:
                from database.db_extended import (
                    _maybe_trigger_publish_verification,
                    _maybe_trigger_orphaned_verification,
                )
                for row in warming:
                    _maybe_trigger_publish_verification(dict(row))
                for row in orphaned:
                    d = dict(row)
                    vid = d.get("video_id")
                    ch_slug = d.get("channel_slug", "")
                    yt_id = d.get("yt_video_id", "")
                    if vid and yt_id and ch_slug:
                        _maybe_trigger_orphaned_verification(vid, ch_slug, yt_id)
            except Exception as trig_exc:
                logger.warning("Publish verify trigger error: %s", trig_exc)
            
            if warming:
                logger.debug("Publish verify ping: %d warming video(s)", len(warming))
        except Exception as exc:
            logger.warning("Publish verify error: %s", exc)
        
        await asyncio.sleep(300)  # Every 5 minutes


async def _yt_state_reconcile_loop():
    """Background loop: reconcile the REAL publication state of recent shorts.

    Runs every 5 minutes (0 YouTube Data API quota — uses yt-dlp + public RSS).
    Updates derived columns (yt_visibility / yt_checked_at / published_at) so the
    UI/endpoints can distinguish "programado" from "publicado" and surface
    silently-removed or stuck-private shorts instead of trusting the optimistic
    status written at upload time.
    """
    import asyncio, logging
    logger = logging.getLogger("autotube.yt_state_reconcile")

    await asyncio.sleep(90)  # Let API + other loops stabilize first

    while True:
        try:
            from api.services.lifecycle_monitor import touch_task_heartbeat as _tth
            _tth("yt_state_reconcile")
            from api.services.yt_state_reconciler import reconcile_recent_shorts
            from database.db_extended import ExtendedDatabase
            summary = await asyncio.to_thread(reconcile_recent_shorts, ExtendedDatabase())
            if summary.get("checked", 0) > 0:
                logger.info(
                    "YT state reconcile: checked=%d updated=%d (pub=%d priv=%d age=%d removed=%d stuck=%d err=%d)",
                    summary.get("checked", 0), summary.get("updated", 0),
                    summary.get("public", 0), summary.get("private", 0),
                    summary.get("age_restricted", 0), summary.get("removed", 0),
                    summary.get("stuck", 0), summary.get("errors", 0),
                )
        except Exception as exc:
            logger.warning("YT state reconcile error: %s", exc)

        await asyncio.sleep(300)  # Every 5 minutes


async def _upload_health_checker_loop():
    """Background loop: process due upload health checks (YouTube processing monitoring).

    Runs every 5 minutes. Each check verifies a video's processingStatus via
    YouTube API and auto-retries uploads if encoding failed.
    Also performs periodic cleanup of old checks every 6 hours.
    """
    import asyncio, logging, time
    logger = logging.getLogger("autotube.health_checker")

    # ── Quota pruning (ago 2026): desactivado por defecto desde .env ──
    # UPLOAD_HEALTH_CHECKER_ENABLED=false → el task existe pero retorna de
    # inmediato (el shutdown del lifespan no necesita cambios).
    if not UPLOAD_HEALTH_CHECKER_ENABLED:
        return

    await asyncio.sleep(120)  # Let API stabilize first
    logger.info("Upload health checker loop started (interval: 5 min)")

    last_cleanup = time.time()

    while True:
        try:
            from api.services.lifecycle_monitor import touch_task_heartbeat as _tth
            _tth("upload_health_checker")
            # ── Quota guard: skip health checks when YouTube API is exhausted ──
            from database.db_extended import ExtendedDatabase
            _hdb = ExtendedDatabase()
            if YT_REMEDIATION_MODE or _hdb.all_channels_quota_exhausted():
                await asyncio.sleep(300)
                continue

            from api.services.upload_health_checker import process_due_checks, cleanup_old_checks

            result = await asyncio.to_thread(process_due_checks)
            if result and (result.get("processed", 0) > 0 or result.get("failed_detected", 0) > 0):
                logger.info(
                    "Health checks: %d processed, %d failed detected, %d retried, %d errors",
                    result.get("processed", 0), result.get("failed_detected", 0),
                    result.get("retried", 0), result.get("errors", 0),
                )

            # Periodic cleanup every 6 hours
            if time.time() - last_cleanup > 21600:
                await asyncio.to_thread(cleanup_old_checks, max_age_days=7)
                last_cleanup = time.time()

        except Exception as exc:
            logger.warning("Upload health checker error: %s", exc)

        await asyncio.sleep(300)  # Every 5 minutes


async def _health_monitor_loop():
    """Periodic health check: scans for stuck/failed entities and generates alerts.
    Also broadcasts system snapshots to monitor WebSocket clients."""
    await asyncio.sleep(30)  # Give API time to fully start before first scan
    logger.info("Health monitor loop started (interval: 90s)")
    while True:
        try:
            from api.services.lifecycle_monitor import touch_task_heartbeat as _tth
            _tth("health_monitor")
            from api.services.lifecycle_monitor import check_all_health
            db = get_db()
            # Offload to thread pool — check_all_health() makes sync sqlite3
            # calls that can block the event loop during lock contention.
            result = await asyncio.to_thread(check_all_health, db)
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


def _recover_quota_once(db, *, now_utc=None, dispatch_uploads=None) -> list:
    """Recover exhausted QUOTA PROJECTS after their own PT reset.

    Fase cuota (ago 2026): recovery POR PROYECTO. Cada proyecto GCP (cuenta)
    se recupera de forma independiente cuando supera su propio reset PT + 15min
    de margen. Un proyecto recuperado deja de bloquear SOLO sus canales; los
    demás siguen pausados hasta su propio reset.

    Reservation expiry runs on every pass, including when no breaker is active.
    In remediation mode the backlog is deliberately left untouched: the
    uploader dispatcher remains the sole admission path once an operator
    disables remediation.

    Returns: list of recovered project ids (empty list = nothing recovered).
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    expired = db.expire_youtube_quota_reservations()
    if expired:
        logger.info("Quota recovery: released %d stale quota reservation(s)", expired)

    now_utc = now_utc or _dt.now(_tz.utc)
    recovered: list = []
    try:
        exhausted_projects = db.get_exhausted_projects()
    except Exception:
        exhausted_projects = []
    if not exhausted_projects:
        return recovered

    for proj in exhausted_projects:
        try:
            reset_info = db.get_quota_reset_time(project_id=proj)
        except Exception:
            continue
        if not reset_info.get("exhausted") or not reset_info.get("reset_at_utc"):
            continue
        try:
            reset_at = _dt.fromisoformat(reset_info["reset_at_utc"])
            if reset_at.tzinfo is None:
                reset_at = reset_at.replace(tzinfo=_tz.utc)
        except (ValueError, TypeError):
            continue
        if now_utc < reset_at + _td(minutes=15):
            continue  # this project still within its safety buffer
        db.clear_quota_exhausted(project_id=proj)
        recovered.append(proj)
        logger.info("Quota recovery: project '%s' cleared (reset reached)", proj)

    if recovered:
        if YT_REMEDIATION_MODE:
            logger.warning(
                "Quota recovery cleared %d project breaker(s); remediation mode "
                "remains active, so no upload was dispatched.",
                len(recovered),
            )
        elif dispatch_uploads is not None:
            dispatch_uploads()
    return recovered


async def _resume_phase_loop():
    """Background loop: avance autónomo de las fases de reanudación post-strike.

    Las fases (0=bloqueado → 1=1 long cada 2 días → 2=1 long/día) se aplicaban
    solo en el arranque de la API (ensure_spam_holds). Este loop las re-evalúa
    cada 6 h para que un canal avance de fase (ej. día 6 → Fase 2) SIN necesidad
    de reiniciar ni ejecutar comandos. replan=False: el planning engine ya
    regenera el horizonte por su cuenta.
    """
    import asyncio as _asyncio

    _logger = logging.getLogger("autotube.resume_phase")

    await _asyncio.sleep(300)  # Let API stabilize first

    _logger.info("Resume-phase loop started (check every 6h, avance autónomo de fases)")

    while True:
        try:
            from api.services.lifecycle_monitor import touch_task_heartbeat as _tth
            _tth("resume_phase")
            from database.db_extended import ExtendedDatabase
            from api.services.gradual_resume import apply_resume_phases
            _db = ExtendedDatabase()

            def _apply_once():
                return apply_resume_phases(_db, replan=False)

            result = await _asyncio.wait_for(
                _asyncio.to_thread(_apply_once),
                timeout=90,
            )
            changed = [a for a in (result or {}).get("applied", []) if _resume_phase_is_active(a)]
            if changed:
                _logger.warning(
                    "Resume-phase tick: %d canal(es) con fase activa (%s)",
                    len(changed),
                    ", ".join(f"{a['slug']}=fase{a['phase']}" for a in changed),
                )
        except Exception as exc:
            _logger.warning("Resume-phase tick failed (no crítico): %s", exc)
        await _asyncio.sleep(6 * 3600)


def _resume_phase_is_active(entry: dict) -> bool:
    """Return whether a resume result represents an active numeric phase.

    Explicit delivery policies intentionally report ``phase=None``.  Treating
    that value as an integer caused the supervised loop to crash on ``None > 0``.
    """
    if not isinstance(entry, dict):
        return False
    phase = entry.get("phase")
    return isinstance(phase, (int, float)) and not isinstance(phase, bool) and phase > 0


async def _quota_recovery_loop():
    """Background loop: recover after the PT reset and safety buffer.
    
    When YouTube API quota is exhausted, the uploader auto-pauses the scheduler
    and records the timestamp. After 6 hours (quota resets at midnight PST),
    this loop automatically resumes scheduling so videos can be uploaded again.
    
    Can also be resumed manually via POST /api/system/scheduler-resume
    which will preempt this loop (it checks scheduler_paused on each iteration).
    """
    import asyncio as _asyncio, logging
    from datetime import datetime as _dt, timezone as _tz
    
    _logger = logging.getLogger("autotube.quota_recovery")
    
    await _asyncio.sleep(120)  # Let API stabilize first
    
    _logger.info("Quota recovery loop started (check every 30 min, auto-resume at midnight PT + safety buffer)")

    while True:
        try:
            from api.services.lifecycle_monitor import touch_task_heartbeat as _tth
            _tth("quota_recovery")
            from database.db_extended import ExtendedDatabase
            _db = ExtendedDatabase()

            def _dispatch_backlog():
                from api.services.upload_scheduler import dispatch_due_uploads
                return dispatch_due_uploads(db=_db)

            # ── Log de cada iteración (antes el loop quedaba mudo y era
            #     imposible diagnosticar por qué no recuperaba) ──
            try:
                _ex = _db.is_quota_exhausted()
                _ex_projects = _db.get_exhausted_projects()
                _logger.debug(
                    "Quota recovery tick: exhausted=%s projects=%s",
                    _ex, _ex_projects,
                )
            except Exception:
                pass

            # Timeout: si una llamada DB se bloquea, el loop no debe quedarse
            # colgado silenciosamente (incidente 15-ago).
            recovered = await _asyncio.wait_for(
                _asyncio.to_thread(
                    _recover_quota_once,
                    _db,
                    now_utc=_dt.now(_tz.utc),
                    dispatch_uploads=_dispatch_backlog,
                ),
                timeout=120,
            )
            # `recovered` es ahora la lista de proyectos GCP recuperados.
            if recovered or not _db.is_quota_exhausted():
                try:
                    if not YT_REMEDIATION_MODE:
                        if recovered:
                            _logger.info(
                                "Quota recovery complete — %d project(s) re-enabled: %s",
                                len(recovered), ", ".join(recovered),
                            )

                        # Resolve quota alerts of the RECOVERED projects only
                        # (cada cuenta mantiene su propia alerta quota_exhausted;
                        # un proyecto que sigue agotado conserva la suya).
                        try:
                            from api.services.quota_tracker import project_entity_id
                            with _db._connect() as _conn:
                                for _proj in recovered:
                                    _eid = project_entity_id(_proj)
                                    if _eid:
                                        _conn.execute(
                                            """UPDATE pipeline_alerts
                                               SET resolved = 1, resolved_at = datetime('now')
                                               WHERE alert_type = 'quota_exhausted'
                                                 AND entity_type = 'system'
                                                 AND entity_id = ? AND resolved = 0""",
                                            (_eid,),
                                        )
                                # Si ya no queda ningún proyecto agotado, limpiar
                                # también alertas stale (episodios previos /
                                # entidades legacy con entity_id desconocido).
                                if not _db.is_quota_exhausted():
                                    _conn.execute(
                                        """UPDATE pipeline_alerts
                                           SET resolved = 1, resolved_at = datetime('now')
                                           WHERE alert_type IN ('quota_exhausted', 'quota_warning')
                                             AND resolved = 0"""
                                    )
                                _conn.commit()
                        except Exception:
                            pass

                        # Log lifecycle event
                        try:
                            from api.services.lifecycle_monitor import log_event as _le
                            _le(_db, entity_type='system', entity_id=0, channel_id=None,
                                event='quota_recovered', status='info',
                                message='Quota auto-recovered (midnight PT reached)')
                        except Exception:
                            pass

                        # Broadcast update to WebSocket clients
                        try:
                            from api.routers.monitor import broadcast_monitor_update
                            await broadcast_monitor_update({
                                "type": "quota_recovered",
                                "message": "Quota auto-recovered after midnight PT reset — uploads re-enabled",
                            })
                        except Exception:
                            pass

                except Exception as _resume_exc:
                    _logger.warning("Quota recovery post-reset handling failed: %s", _resume_exc, exc_info=True)
        except Exception as _exc:
            _logger.warning("Quota recovery loop error: %s", _exc, exc_info=True)
        
        await _asyncio.sleep(1800)  # Check every 30 minutes


async def _redistribution_loop():
    """Background loop: drena la cola espejo (Máquina A) a ritmo seguro.

    Una subida por tick (10 min), respetando warm-up, cap diario y backoff
    por plataforma. Las subidas se hacen vía API (Rumble/Dailymotion/Facebook)
    — no compiten por RAM con la generación long-form.
    """
    import asyncio as _asyncio

    _logger = logging.getLogger("autotube.redistribution")

    await _asyncio.sleep(180)  # Let API stabilize first

    _logger.info("Redistribution loop started (1 upload/tick, cada 10 min)")

    while True:
        try:
            from api.services.lifecycle_monitor import touch_task_heartbeat as _tth
            _tth("redistribution")
            from database.db_extended import ExtendedDatabase
            from api.services.redistribution_worker import redistribution_tick
            _db = ExtendedDatabase()

            def _tick_once():
                return redistribution_tick(_db)

            result = await _asyncio.wait_for(
                _asyncio.to_thread(_tick_once),
                timeout=600,
            )
            if result.get("uploaded"):
                _logger.info(
                    "Redistribution tick: %s",
                    ", ".join(result["uploaded"]),
                )
            elif result.get("channels_checked"):
                # Log solo cada cierto ruido: skip si nada que subir
                if len(result.get("skipped", [])) <= 3:
                    _logger.debug("Redistribution tick: %s", result)
        except Exception as _exc:
            _logger.warning("Redistribution tick failed (no crítico): %s", _exc)
        await _asyncio.sleep(600)  # cada 10 minutos


async def _publish_coverage_loop():
    """Background loop: garantiza la cobertura diaria de publicación.

    Cada 10 min audita que cada canal libre tenga su cuota de publicaciones
    programadas en los próximos días y dispara el repack del canal si hay huecos
    (ver api.services.publish_coverage.ensure_daily_publish_coverage). No consume
    cuota salvo los videos.update del repack (acotados por quota_gate).
    """
    import asyncio as _asyncio

    _logger = logging.getLogger("autotube.publish_coverage_loop")

    await _asyncio.sleep(120)  # Let API stabilize first

    _logger.info("Publish coverage loop started (cada 10 min)")

    while True:
        try:
            from api.services.lifecycle_monitor import touch_task_heartbeat as _tth
            _tth("publish_coverage")
            from database.db_extended import ExtendedDatabase
            from api.services.publish_coverage import ensure_daily_publish_coverage
            _db = ExtendedDatabase()

            def _coverage_once():
                return ensure_daily_publish_coverage(_db)

            result = await _asyncio.wait_for(
                _asyncio.to_thread(_coverage_once),
                timeout=600,
            )
            if result.get("repacked"):
                _logger.info(
                    "Publish coverage: %d canal(es) repackeados — %s",
                    result["repacked"],
                    ", ".join(
                        f"{slug}={r['reason']}"
                        for slug, r in (result.get("channels") or {}).items()
                        if r.get("triggered")
                    ),
                )
        except Exception as _exc:
            _logger.warning("Publish coverage tick failed (no crítico): %s", _exc)
        await _asyncio.sleep(600)  # cada 10 minutos


async def _shorts_backfill_loop():
    """Background loop: gradually add long-form links to short descriptions.

    Processes shorts in small batches (15 every 30 min) to stay well under
    YouTube API quota. Survives restarts via system_state persistence.
    Runs until all published shorts have been linked, then idles.
    """
    import asyncio, logging, time
    logger = logging.getLogger("autotube.backfill")

    await asyncio.sleep(120)  # Let API stabilize first

    logger.info("Shorts backfill loop started (batch=15, interval=30 min)")

    while True:
        try:
            from api.services.shorts_backfill_service import (
                run_backfill_batch, is_backfill_complete, get_backfill_status,
            )

            if is_backfill_complete():
                # Still log occasionally so we know the loop is alive
                if not hasattr(_shorts_backfill_loop, "_idle_count"):
                    _shorts_backfill_loop._idle_count = 0
                _shorts_backfill_loop._idle_count += 1
                if _shorts_backfill_loop._idle_count % 12 == 1:  # ~every 6h
                    logger.debug("Backfill loop idle — all shorts already linked")
                await asyncio.sleep(1800)  # 30 min
                continue

            result = await asyncio.to_thread(run_backfill_batch)

            if result["updated"] > 0 or result["errors"] > 0:
                logger.info(
                    "Backfill batch: %d updated, %d errors | status=%s",
                    result["updated"], result["errors"],
                    get_backfill_status(),
                )

            if result["done"]:
                logger.info("Backfill complete! All shorts now have long-form links.")
                # Continue the loop in case new shorts are published later
                await asyncio.sleep(1800)
                continue

            await asyncio.sleep(1800)  # 30 min between batches

        except Exception as exc:
            logger.warning("Shorts backfill error: %s", exc)
            await asyncio.sleep(600)  # 10 min on error, then retry


def _quota_exhausted_safe() -> bool:
    """Check if YouTube quota is exhausted, catching all errors safely.
    
    Returns True if quota IS exhausted (skip API calls).
    Returns False if quota is OK or if the check itself failed (fail open).
    """
    try:
        from database.db_extended import ExtendedDatabase
        _qdb = ExtendedDatabase()
        return _qdb.is_quota_exhausted()
    except Exception:
        return False  # fail open — allow calls if DB is unreachable


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
    last_recovery_check = 0
    last_shorts_recovery_check = 0
    last_smart_replan = 0
    last_slot_calculation = 0
    last_overlap_verify = 0  # v23: periodic overlap verification
    last_power_word_analysis = 0
    last_marathon_check = 0
    try:
        stored = _sched_db.get_system_state("last_marathon_check")
        if stored:
            last_marathon_check = float(stored)
    except Exception:
        pass
    last_view_gap_check = 0
    last_standalone_dispatch = 0  # standalone shorts auto-dispatch
    last_collab_run = 0  # daily collaboration engine
    last_ab_test_check = 0  # v31: A/B test sequential optimization
    first_run = True

    # Restore last_view_gap_check from DB
    try:
        stored = _sched_db.get_system_state("last_view_gap_check")
        if stored:
            last_view_gap_check = float(stored)
    except Exception:
        pass

    while True:
        try:
            from api.services.lifecycle_monitor import touch_task_heartbeat as _tth
            _tth("schedule_checker")
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

            # ── Pause gates ──
            # scheduler_paused: manual operator pause → blocks EVERYTHING
            # quota_exhausted_at: YouTube API quota exhausted (per PROJECT).
            #   _quota_exhausted     = algún proyecto agotado (solo informativo)
            #   _all_quota_exhausted = TODOS los proyectos agotados → gates globales
            # Generation (planned_slots, marathons, queue_consumer) continues in both modes
            # to build up backlog while waiting for quota reset.
            _paused_manual = False
            _quota_exhausted = False
            _all_quota_exhausted = False
            try:
                _paused_manual = _sched_db.get_system_state("scheduler_paused") == "true"
                _quota_exhausted = _sched_db.is_quota_exhausted()
                _all_quota_exhausted = _sched_db.all_channels_quota_exhausted()
            except Exception:
                pass

            now = time.time()

            if not _paused_manual:
                local_hour = time.localtime().tm_hour

                # ════════════════════════════════════════════════════════════
                # Phase A: generation + planning (always active, no YT API)
                # ════════════════════════════════════════════════════════════
                # v40: _process_due_schedules() retirado del loop — la tabla
                # content_schedules está vacía en producción y la planificación
                # la gobiernan planned_slots (planning_service). Las funciones
                # se conservan (deprecadas) por si algún script legacy las usa.
                long_dispatched = await _process_planned_slots()

                # ── Shorts interleaving: when long-form is blocked (pipelining
                #     guard or no slots), try an extra shorts dispatch to fill
                #     the gap. Per-channel project guards inside decide.
                if not long_dispatched:
                    if not _all_quota_exhausted:
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

                # ════════════════════════════════════════════════════════════
                # Marathon check: always runs — marathon service dispatches
                # as generate_only if quota is exhausted, generate_and_upload otherwise.
                # ════════════════════════════════════════════════════════════
                if now - last_marathon_check > 3600:  # every 60 min
                    try:
                        from api.services.marathon_service import check_and_dispatch_marathon
                        marathon_result = await asyncio.to_thread(
                            check_and_dispatch_marathon, _sched_db
                        )
                        if marathon_result:
                            logger.info(
                                "[MARATHON] Dispatched: channel=%s duration=%dmin sections=%d backlog=%d",
                                marathon_result.get("channel", "?"),
                                marathon_result.get("marathon_config", {}).get("duration_target", 60),
                                marathon_result.get("marathon_config", {}).get("num_sections", 12),
                                marathon_result.get("backlog", 0),
                            )
                    except Exception as exc:
                        logger.debug("Marathon check: %s", exc)
                    last_marathon_check = now
                    try:
                        _sched_db.set_system_state("last_marathon_check", str(now))
                    except Exception:
                        pass

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

                # ── v23: Overlap safety net — verify no publish-time collisions every 15 min ──
                if now - last_overlap_verify > 900:
                    try:
                        from api.services.upload_scheduler import verify_no_overlaps
                        result = await asyncio.to_thread(
                            verify_no_overlaps, db=_sched_db, auto_fix=True
                        )
                        if result and (result.get("fixed", 0) > 0 or result.get("remaining", 0) > 0):
                            logger.info(
                                "Overlap verify: %d fixed, %d remaining, %d errors",
                                result.get("fixed", 0), result.get("remaining", 0),
                                result.get("errors", 0),
                            )
                    except Exception as exc:
                        logger.debug("Overlap verify: %s", exc)
                    last_overlap_verify = now

                # ── Quota excess pruning: once per Pacific day, after PT reset + 15min buffer ──
                # v40: el exceso de cuota ya NO se poda en smart_replan (30 min);
                # se poda una vez por día tras el reset de medianoche PT. El guard
                # por día PT hace el paso idempotente.
                try:
                    from datetime import datetime as _dt_prune, timezone as _tz_prune, timedelta as _td_prune
                    from zoneinfo import ZoneInfo as _ZI
                    from api.services.planning_service import _cancel_excess_pending_by_quota
                    _now_utc = _dt_prune.now(_tz_prune.utc)
                    _pt_now = _now_utc.astimezone(_ZI("America/Los_Angeles"))
                    _pt_day = _pt_now.date().isoformat()
                    _last_pruned = _sched_db.get_system_state("last_quota_prune_pt_day")
                    if _last_pruned != _pt_day:
                        _pt_midnight = _dt_prune.combine(
                            _pt_now.date(), _dt_prune.min.time(), tzinfo=_ZI("America/Los_Angeles")
                        )
                        if _now_utc >= _pt_midnight.astimezone(_tz_prune.utc) + _td_prune(minutes=15):
                            _pruned = await asyncio.to_thread(_cancel_excess_pending_by_quota, _sched_db)
                            _sched_db.set_system_state("last_quota_prune_pt_day", _pt_day)
                            if _pruned:
                                logger.info(
                                    "Quota excess prune (PT midnight): cancelled %d pending slot(s)",
                                    _pruned,
                                )
                except Exception as exc:
                    logger.debug("Quota excess prune: %s", exc)

                # Auto-recovery: replan missing publications every 60 minutes
                if now - last_recovery_check > 3600:
                    await _process_recovery_planner()
                    last_recovery_check = now

                # Smart replan: every 30 min during active hours (10:00-23:00)
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

                # ── A/B Testing: sequential title/thumbnail optimization (v31) ──
                # Runs every 60 minutes. Quota-aware: reads local DB, only hits
                # YT API if data is stale. Skip only if ALL projects are exhausted.
                if not _all_quota_exhausted and now - last_ab_test_check > 3600:
                    try:
                        from config.settings import ENABLE_AB_TESTING
                        if ENABLE_AB_TESTING:
                            from api.services.ab_test_worker import ABTestWorker
                            _ab = ABTestWorker(_sched_db)
                            await asyncio.to_thread(_ab.run_cycle)
                    except Exception as exc:
                        logger.warning("AB test cycle error: %s", exc)
                    last_ab_test_check = now

                # Shorts auto-recovery: rebalance shorts every 120 minutes (v14: reduced from 60min)
                if now - last_shorts_recovery_check > 7200:
                    await _process_shorts_recovery_planner()
                    last_shorts_recovery_check = now

                # ── Horizon replan / top-up (v40 + Fase 1 continua) ──
                # Modo fábrica continua: top-up incremental cada 15 min (no borra
                # slots, extiende hacia delante). Modo clásico: replan completo
                # con gate de 24h desde el último replan (manual o automático).
                try:
                    from api.services.planning_service import (
                        compute_and_store_horizon, get_last_replan_ts,
                        HORIZON_REPLAN_INTERVAL_HOURS, continuous_generation_enabled,
                        top_up_horizon,
                    )
                    if continuous_generation_enabled(_sched_db):
                        _last_topup_ts = 0.0
                        try:
                            _raw_tu = _sched_db.get_system_state("last_horizon_topup_ts")
                            if _raw_tu:
                                _last_topup_ts = float(_raw_tu)
                        except (TypeError, ValueError):
                            _last_topup_ts = 0.0
                        if now - _last_topup_ts >= 15 * 60:
                            result = await asyncio.to_thread(top_up_horizon, db=_sched_db)
                            try:
                                _sched_db.set_system_state("last_horizon_topup_ts", str(time.time()))
                            except Exception:
                                pass
                            logger.info(
                                "Horizon top-up: +%d slots, %d día(s) planificados",
                                result.get("added", 0), result.get("days_planned", 0),
                            )
                    else:
                        if now - get_last_replan_ts(_sched_db) >= HORIZON_REPLAN_INTERVAL_HOURS * 3600:
                            result = await asyncio.to_thread(
                                compute_and_store_horizon, horizon_days=7, db=_sched_db
                            )
                            logger.info(
                                "Horizon replan (24h gate): %d slots, %d days",
                                result.get("total_slots", 0), result.get("days_planned", 0),
                            )
                except Exception as exc:
                    logger.debug("Horizon replan: %s", exc)

                # ── Daily shorts ensure (v40): independent daily check — once per
                # local day, persisted in system_state("last_shorts_daily_ensure").
                try:
                    _today_local = time.strftime("%Y-%m-%d")
                    _last_shorts_ensure = _sched_db.get_system_state("last_shorts_daily_ensure")
                    if _last_shorts_ensure != _today_local:
                        from api.services.shorts_scheduler import ensure_today_shorts_scheduled
                        await asyncio.to_thread(ensure_today_shorts_scheduled)
                        _sched_db.set_system_state("last_shorts_daily_ensure", _today_local)
                except Exception as exc:
                    logger.debug("Daily shorts ensure: %s", exc)

                # ── Backlog TTL diario (Fase 4): señalar vídeos con >14 días en cola ──
                try:
                    _last_ttl = _sched_db.get_system_state("last_backlog_ttl_sweep")
                    if _last_ttl != _today_local:
                        from api.services.planning_service import sweep_stale_backlog
                        result = await asyncio.to_thread(sweep_stale_backlog, db=_sched_db)
                        _sched_db.set_system_state("last_backlog_ttl_sweep", _today_local)
                        if result.get("flagged", 0):
                            logger.info("Backlog TTL sweep: %d vídeo(s) señalados", result["flagged"])
                except Exception as exc:
                    logger.debug("Backlog TTL sweep: %s", exc)

                # ── Transición automática de perfil (Fase 4 bis): strike → recovery
                # → normal tras N días sin strikes (kill-switch: auto_pacing_transition) ──
                try:
                    _last_apt = _sched_db.get_system_state("last_auto_pacing_check")
                    if _last_apt != _today_local:
                        from api.services.pacing_profile import auto_transition_profile
                        apt = await asyncio.to_thread(auto_transition_profile, db=_sched_db)
                        _sched_db.set_system_state("last_auto_pacing_check", _today_local)
                        if apt.get("transitioned"):
                            logger.info(
                                "Auto-pacing: %s → %s (%.0f días limpios)",
                                apt.get("from"), apt.get("to"), apt.get("clean_days") or 0,
                            )
                except Exception as exc:
                    logger.debug("Auto-pacing transition: %s", exc)

                # ════════════════════════════════════════════════════════════
                # Phase B: YT API-dependent operations (gated by quota)
                # Fase cuota (ago 2026): gate solo cuando TODOS los proyectos
                # están agotados — los guards internos son per-channel/proyecto.
                # ════════════════════════════════════════════════════════════
                if not _all_quota_exhausted and not YT_REMEDIATION_MODE:
                    # Primary shorts + upload dispatch
                    await _process_shorts_slots()
                    await _process_upload_slots()

                    # ── Thumbnail verification (v24) ──
                    # Fase cuota: el gate per-channel/proyecto vive DENTRO del
                    # servicio (should_skip_thumbnail_verify resuelve el proyecto
                    # del video). El gate global al 50% se eliminó porque
                    # bloqueaba la verificación de TODOS los canales cuando un
                    # solo proyecto estaba al 50% (medición global incorrecta).
                    # Quota pruning (ago 2026): desactivado por defecto (flag env).
                    if THUMBNAIL_VERIFY_ENABLED:
                        try:
                            from api.services.thumbnail_verification_service import run_thumbnail_verification_cycle
                            await run_thumbnail_verification_cycle(db=_sched_db)
                        except Exception as _tv_exc:
                            logger.debug("Thumbnail verification cycle: %s", _tv_exc)

                    # Standalone shorts: auto-dispatch trending topics every 120 min (10:00-23:00)
                    if 10 <= local_hour <= 23 and now - last_standalone_dispatch > 7200:
                        try:
                            from api.services.shorts_scheduler import dispatch_standalone_shorts_daily
                            await asyncio.to_thread(dispatch_standalone_shorts_daily)
                        except Exception as exc:
                            logger.debug("Standalone shorts dispatch: %s", exc)
                        last_standalone_dispatch = now

                    # Collaboration engine: daily at 3:00-4:00 UTC (off-peak)
                    if 3 <= local_hour <= 4 and now - last_collab_run > 82800:  # ~23h
                        try:
                            from pipeline.collaboration_engine import run_all_channels_collab
                            await asyncio.to_thread(run_all_channels_collab)
                            logger.debug("Collaboration round completed")
                        except Exception as exc:
                            logger.debug("Collaboration run: %s", exc)
                        last_collab_run = now

                    # ── Optimal publish slots: calculate once per day ──
                    if now - last_slot_calculation > 86400:
                        await _calculate_optimal_slots()
                        last_slot_calculation = now

                    # ── Daily view gap monitor ──
                    if STATS_ENABLED and now - last_view_gap_check > 86400:
                        try:
                            from api.services.view_gap_monitor import ViewGapMonitor
                            logger.info("Starting daily view gap check...")
                            monitor = ViewGapMonitor()
                            db = get_db()
                            results = await asyncio.to_thread(monitor.check_all_channels, db)
                            last_view_gap_check = now
                            try:
                                _sched_db.set_system_state("last_view_gap_check", str(int(now)))
                            except Exception:
                                pass
                            if results:
                                checked = results.get("channels_checked", 0)
                                gaps = results.get("gaps_detected", 0)
                                registered = results.get("videos_registered", 0)
                                logger.info(
                                    "View gap check: %d channels, %d gaps detected, %d videos auto-registered",
                                    checked, gaps, registered,
                                )
                                if gaps > 0:
                                    try:
                                        from api.routers.monitor import broadcast_monitor_update
                                        await broadcast_monitor_update({
                                            "type": "view_gap_alert",
                                            "gaps_detected": gaps,
                                            "channels_checked": checked,
                                            "videos_registered": registered,
                                        })
                                    except Exception:
                                        pass
                        except Exception as e:
                            logger.warning("Daily view gap check failed: %s", e)
                else:
                    # ── Quota exhausted: log periodically (once per hour) ──
                    if not hasattr(_schedule_checker_loop, '_last_qex_log') or \
                       now - _schedule_checker_loop._last_qex_log > 3600:
                        try:
                            _ex_projects = _sched_db.get_exhausted_projects()
                        except Exception:
                            _ex_projects = []
                        logger.info(
                            "⏸️ Quota API YouTube agotada (proyectos: %s) — "
                            "generación activa, subidas pausadas solo en esos proyectos. "
                            "Recuperación automática al reset de medianoche (PT).",
                            ", ".join(_ex_projects) or "?",
                        )
                        _schedule_checker_loop._last_qex_log = now

            await _detect_and_clean_orphans()
            
            # Collect YouTube stats every 6 hours
            now = time.time()
            if STATS_AUTO_COLLECT and now - last_stats_collection > 21600:  # 6 hours
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
        if _db.get_system_state("scheduler_paused") == "true" or YT_REMEDIATION_MODE:
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
    """[DEPRECATED] Dispatch due planned_slots using the per-channel adaptive schedule engine.

    ⚠️  DEPRECATED (v26): this wrapper calls `schedule_engine.dispatch_next_due_slot`,
    which is legacy and no longer invoked by the checker loop. The active
    dispatcher is `_process_planned_slots` → `planning_service.process_planned_slots`.
    Kept for backward compatibility; do NOT wire this back into the checker loop.

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
        # ── Cobertura diaria de shorts (auditoría 1/día, alerta si canal seco) ──
        try:
            from api.services.shorts_scheduler import check_shorts_daily_coverage
            await asyncio.to_thread(check_shorts_daily_coverage, _db)
        except Exception as _cov_exc:
            logger.debug("Shorts coverage check skip: %s", _cov_exc)
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


def _process_due_schedules_sync() -> list[dict]:
    """Synchronous DB work for schedule dispatch. Returns list of dispatch payloads.

    ⚠️  DEPRECATED (v26): the `content_schedules` cron table is empty in
    production and this legacy scheduler is not used — planning is driven by
    `planned_slots` (planning_service) + `shorts_planned_slots`. Kept for
    backward compatibility; do NOT rely on it for scheduling.

    Offloaded to thread pool by _process_due_schedules() so that sqlite3.connect()
    (with busy_timeout=30000) and ExtendedDatabase calls do not block the asyncio
    event loop.
    """
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
        return []
    
    dispatch_payloads = []
    
    for row in rows:
        s = dict(row)
        logger.info(f"Running schedule #{s['id']}: {s['channel_slug']} action={s['action']}")
        
        # ── Per-channel guard: skip if this channel already has a running job ──
        active_for_channel = db.get_active_job_for_channel(s["channel_id"])
        if active_for_channel:
            logger.debug("Schedule #%d skipped: channel %d already has active job #%d",
                        s["id"], s["channel_id"], active_for_channel["id"])
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
                    conn.execute(
                        "UPDATE content_schedules SET last_run_at = ?, active = 0, video_id = ? WHERE id = ?",
                        (now, video_id, s["id"]),
                    )
                conn.commit()
            # ── End dispatch critical section ────────────────────
            
            dispatch_payloads.append({
                "job_id": job_id,
                "channel_id": s["channel_id"],
                "video_id": video_id,
                "action": s["action"],
                "content_id": s.get("content_id"),
            })
            
        except Exception as e:
            logger.error(f"Schedule #{s['id']} failed: {e}")
            conn.execute(
                "UPDATE content_schedules SET next_run_at = datetime('now', 'localtime', '+1 hour') WHERE id = ?",
                (s["id"],),
            )
            conn.commit()
    
    conn.close()
    return dispatch_payloads


async def _process_due_schedules():
    """Find and execute due schedules (async wrapper).

    ⚠️  DEPRECATED (v26, v40): backed by the empty `content_schedules` cron table;
    planning is driven by planned_slots. Kept for backward compatibility —
    possible legacy scripts may still call it, but the checker loop NO LONGER
    invokes it (removed in v40).

    All synchronous DB operations (sqlite3.connect, ExtendedDatabase methods,
    threading.Lock acquisition) are offloaded to a thread pool so that none of
    them block the asyncio event loop — preventing watchdog timeouts during
    SQLite lock contention with the worker process.
    """
    import asyncio, logging
    logger = logging.getLogger("autotube.scheduler")
    
    dispatch_payloads = await asyncio.to_thread(_process_due_schedules_sync)
    
    if not dispatch_payloads:
        return
    
    # Fire and forget the generation (don't await) — must stay in event loop
    from api.services.generation_service import start_generation_job, start_generation_job_subprocess, USE_SUBPROCESS_WORKER
    
    for payload in dispatch_payloads:
        if USE_SUBPROCESS_WORKER:
            asyncio.create_task(
                start_generation_job_subprocess(
                    job_id=payload["job_id"],
                    channel_id=payload["channel_id"],
                    video_id=payload["video_id"],
                    action=payload["action"],
                )
            )
        else:
            asyncio.create_task(
                start_generation_job(
                    job_id=payload["job_id"],
                    channel_id=payload["channel_id"],
                    video_id=payload["video_id"],
                    action=payload["action"],
                    content_id=payload.get("content_id"),
                )
            )


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
        logger.error("Orphan detection failed: %s", exc, exc_info=True)


# ── Stuck insight detection config ────────────────────────
# v20.1: use heartbeat-based detection (3 min stale = dead)
INSIGHT_ORPHAN_HEARTBEAT_SECONDS = 180  # 3 min without heartbeat → orphan
INSIGHT_ORPHAN_LEGACY_MINUTES = 15      # fallback for rows without heartbeat column

def _cleanup_orphaned_insights(db, logger,
                                heartbeat_seconds=INSIGHT_ORPHAN_HEARTBEAT_SECONDS,
                                legacy_minutes=INSIGHT_ORPHAN_LEGACY_MINUTES):
    """Mark stuck channel_insights rows as failed.

    Uses heartbeat-based detection (v20.1): if heartbeat_at is older than
    heartbeat_seconds, the analysis thread is dead/hung.
    Falls back to generated_at for legacy rows without heartbeat column.
    """
    import logging as _log_mod
    if logger is None:
        logger = _log_mod.getLogger("autotube.insights")

    try:
        with db._connect() as conn:
            # Primary: heartbeat-based detection
            cursor = conn.execute(
                """UPDATE channel_insights
                   SET status = 'failed',
                       error_msg = 'Analysis timed out — no heartbeat for ' || ? || 's'
                   WHERE status = 'processing'
                     AND heartbeat_at IS NOT NULL
                     AND heartbeat_at < datetime('now', ?)""",
                (str(heartbeat_seconds), f'-{heartbeat_seconds} seconds'),
            )
            count_hb = cursor.rowcount

            # Fallback: legacy rows without heartbeat_at (use generated_at)
            cursor = conn.execute(
                """UPDATE channel_insights
                   SET status = 'failed',
                       error_msg = 'Analysis timed out — auto-cleaned (legacy, ' || ? || ' min)'
                   WHERE status = 'processing'
                     AND heartbeat_at IS NULL
                     AND generated_at < datetime('now', ?)""",
                (str(legacy_minutes), f'-{legacy_minutes} minutes'),
            )
            count_legacy = cursor.rowcount
            conn.commit()

            total = count_hb + count_legacy
            if total > 0:
                logger.warning(
                    "Orphaned insights cleaned: %d (heartbeat) + %d (legacy) → failed",
                    count_hb, count_legacy,
                )
            else:
                logger.debug("Orphan check: no stale insights found")
    except Exception as exc:
        logger.error("Insight orphan cleanup failed: %s", exc, exc_info=True)


# ── Filesystem retry helper with exponential backoff ────────
# Used by ghost worker detection and temp file cleanup to
# survive transient filesystem errors (NFS stalls, /proc races,
# disk I/O contention).
#
# Expected error cases:
#   OSError        — stale file handle, /proc entry disappeared mid-read,
#                    disk full, filesystem remounted readonly.
#   IOError        — Python 2 compat alias; treated same as OSError.
#   PermissionError — file exists but process lacks read/write access;
#                     typically from container capability restrictions.
#   FileNotFoundError — /proc/<pid>/cmdline vanished between pgrep and open;
#                       normal race condition on fast-dying processes.
#
# Non-blocking: failures are logged at DEBUG with full traceback and
# the caller continues with the next item.  No exception propagates up
# to the orphan detection loop — the checker must keep running every 5 min.

def _retry_fs_op(op_fn, max_retries=3, base_delay=0.5, label=""):
    """Retry a filesystem operation with exponential backoff.
    
    Args:
        op_fn: Callable that performs the filesystem operation.
        max_retries: Maximum attempts (default 3 → 0.5s, 1s, 2s delays).
        base_delay: Starting delay in seconds; doubles each attempt.
        label: Human-readable label for debug logs.
    
    Returns:
        The result of op_fn() on success.
    
    Raises:
        OrphanCleanupError (non-blocking): After all retries exhausted.
    """
    import time as _time
    last_exc = None
    for attempt in range(max_retries):
        try:
            return op_fn()
        except (OSError, IOError, PermissionError, FileNotFoundError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                _orphan_logger.debug(
                    "fs retry %d/%d for %s after %.1fs: %s",
                    attempt + 1, max_retries, label, delay, exc,
                    exc_info=True,
                )
                _time.sleep(delay)
    # All retries exhausted — raise non-blocking exception
    raise OrphanCleanupError(
        f"Filesystem operation '{label}' failed after {max_retries} attempts: {last_exc}"
    ) from last_exc


# ── Non-blocking exception class ────────────────────────────
# Raised by retry helpers and cleanup functions when a filesystem
# operation fails after all retries.  This is NOT a fatal error —
# the orphan detection loop catches it per-item and continues.
# The 'blocking' attribute is explicitly False to signal the
# caller that execution should proceed.

class OrphanCleanupError(Exception):
    """Non-blocking exception for orphan cleanup failures.
    
    Raised when a single file/process operation fails after all
    retries.  The orphan detection loop continues with the next
    item — this exception does NOT abort the entire phase.
    """
    def __init__(self, message: str, blocking: bool = False):
        super().__init__(message)
        self.blocking = blocking


# ── Module-level orphan logger (shared by helpers) ──────────
_orphan_logger = logging.getLogger("autotube.orphans")


def _read_cmdline_with_retry(pid: int) -> str:
    """Read /proc/<pid>/cmdline with pre-validation and retry.
    
    Expected error cases:
      - FileNotFoundError: Process died between pgrep and open
        (normal race condition — skip silently).
      - PermissionError: Container/capability restrictions
        (process belongs to a different namespace).
    
    Returns cmdline string on success, empty string on permanent failure.
    """
    import os as _os
    _proc_path = f'/proc/{pid}/cmdline'
    
    # Pre-validate: check existence and readability before attempting open.
    # Avoids a noisy FileNotFoundError traceback for processes that died
    # between the pgrep scan and this read.
    if not _os.path.exists(_proc_path):
        _orphan_logger.debug(
            "Ghost worker PID %d: /proc/cmdline vanished before read "
            "(process died — normal race condition)", pid,
        )
        return ""
    if not _os.access(_proc_path, _os.R_OK):
        _orphan_logger.debug(
            "Ghost worker PID %d: /proc/cmdline not readable "
            "(permission denied — possible namespace isolation)", pid,
        )
        return ""
    
    def _read():
        with open(_proc_path, 'r') as f:
            return f.read().replace('\x00', ' ')
    
    try:
        return _retry_fs_op(_read, max_retries=3, base_delay=0.3,
                            label=f"read /proc/{pid}/cmdline")
    except OrphanCleanupError as exc:
        _orphan_logger.debug(
            "Ghost worker PID %d: cmdline read failed after retries: %s",
            pid, exc,
            exc_info=True,
        )
        return ""


def _detect_ghost_workers(db, logger):
    """Kill worker processes whose --job-id does not exist in generation_jobs.
    
    These are truly orphaned: the worker subprocess is alive but its job row
    was deleted from the DB (channel deletion cascade, manual cleanup, etc).
    Without this detector, they would run forever doing work that disappears.
    
    Expected error cases (per PID, non-blocking — each is skipped):
      - FileNotFoundError / OSError: /proc/<pid>/cmdline vanished (process died
        between pgrep scan and file open — normal race).
      - PermissionError: process belongs to a different container namespace.
      - ProcessLookupError: process already dead when sending signal.
      - ValueError: garbage PID from pgrep output (should never happen).
      - subprocess.TimeoutExpired: pgrep hung (process table contention).
    """
    import os as _os
    import re as _re
    import signal as _signal
    import subprocess as _subprocess
    import time as _time
    
    # ── External connection: pgrep with retry ────────────────
    for attempt in range(3):
        try:
            result = _subprocess.run(
                ["pgrep", "-f", "full_pipeline_worker.py"],
                capture_output=True, text=True, timeout=5,
            )
            break
        except _subprocess.TimeoutExpired as exc:
            logger.debug(
                "Ghost worker: pgrep timed out (attempt %d/3): %s",
                attempt + 1, exc,
                exc_info=True,
            )
            if attempt < 2:
                _time.sleep(0.5 * (2 ** attempt))
        except Exception as exc:
            logger.debug(
                "Ghost worker: pgrep failed (attempt %d/3): %s",
                attempt + 1, exc,
                exc_info=True,
            )
            if attempt < 2:
                _time.sleep(0.5 * (2 ** attempt))
    else:
        logger.debug("Ghost worker detection scan skipped: pgrep failed 3 times")
        return
    
    if result.returncode != 0 or not result.stdout.strip():
        return
    
    for pid_str in result.stdout.strip().split('\n'):
        pid_str = pid_str.strip()
        if not pid_str:
            continue
        try:
            pid = int(pid_str)
            # ── File operation: read /proc/cmdline with retry+validation ──
            cmdline = _read_cmdline_with_retry(pid)
            if not cmdline:
                continue
            
            match = _re.search(r'--job-id (\d+)', cmdline)
            if not match:
                continue
            job_id = int(match.group(1))
            
            # ── External connection: DB query (handled by db.get_job) ──
            try:
                job = db.get_job(job_id)
            except Exception as exc:
                logger.debug(
                    "Ghost worker PID %d: DB query for job %d failed: %s",
                    pid, job_id, exc,
                    exc_info=True,
                )
                continue
            
            if job is None:
                # Also check if it exists in the running set (reconnect_active_workers
                # may have already marked it failed but row still exists)
                logger.warning(
                    "GHOST WORKER DETECTED: PID %d (job %d) — "
                    "job row deleted from DB. Killing...", pid, job_id
                )
                # ── External operation: kill with SIGTERM → SIGKILL ──
                try:
                    _os.kill(pid, _signal.SIGTERM)
                    _time.sleep(2)
                    # Force kill if still alive
                    try:
                        _os.kill(pid, _signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # expected: process already exited
                except ProcessLookupError:
                    pass  # expected: process died between detection and kill
                except OSError as exc:
                    logger.debug(
                        "Ghost worker PID %d: kill failed: %s",
                        pid, exc,
                        exc_info=True,
                    )
        except (ValueError, ProcessLookupError, FileNotFoundError) as exc:
            logger.debug(
                "Ghost worker: per-PID error for PID %s: %s",
                pid_str, exc,
                exc_info=True,
            )
            # Non-blocking: skip this PID, continue with next
            continue
        except Exception as exc:
            logger.debug(
                "Ghost worker: unexpected error for PID %s: %s",
                pid_str, exc,
                exc_info=True,
            )
            continue


def _cleanup_orphaned_temp_files(db, orphaned_jobs: list, orphaned_videos: list, logger):
    """Delete temporary files belonging to orphaned jobs and videos from disk.
    
    For each orphaned job/video, deletes:
      - Scene assets (video clips, images, AI-generated scenes)
      - MP3 narration and CTA audio files
      - Timestamps JSON and subtitles SRT files
      - Local MP4 video file
    
    Preserves:
      - Thumbnails (panel display)
      - channel configuration files
    
    Each file deletion is validated (existence + write permission) before
    attempting, retried with exponential backoff (max 3 attempts), and
    logged at DEBUG with full stack trace on failure.
    
    Expected error cases (per file, non-blocking):
      - FileNotFoundError: file already deleted by another cleanup job.
      - PermissionError: directory ownership changed, readonly filesystem.
      - OSError: stale NFS handle, disk error, filesystem remounted.
    
    Non-blocking: a single file deletion failure logs the error and moves
    to the next file.  No exception propagates to the orphan detection loop.
    
    Args:
        db: Database connection.
        orphaned_jobs: List of dicts with job_id, video_id, channel_slug.
        orphaned_videos: List of dicts with video_id, channel_slug.
        logger: Logger instance.
    
    Returns:
        dict: {"files_deleted": int, "bytes_freed": int, "errors": list}
    """
    import os as _os
    import json as _json
    from pathlib import Path
    
    result = {"files_deleted": 0, "bytes_freed": 0, "errors": []}
    
    # Collect all orphaned video IDs (from both jobs and standalone videos)
    video_ids_to_clean = set()
    for job in orphaned_jobs:
        vid = job.get("video_id")
        if vid:
            video_ids_to_clean.add(vid)
    for vid_info in orphaned_videos:
        vid = vid_info.get("video_id")
        if vid:
            video_ids_to_clean.add(vid)
    
    if not video_ids_to_clean:
        logger.debug("Orphan temp file cleanup: no video IDs to clean")
        return result
    
    logger.info(
        "Orphan temp file cleanup: scanning %d orphaned video(s) for stale files",
        len(video_ids_to_clean),
    )
    
    for video_id in video_ids_to_clean:
        try:
            video = db.get_video(video_id)
            if not video:
                logger.debug("Orphan temp cleanup: video %d not found in DB (already pruned)", video_id)
                continue
            
            # ── 1. Delete local MP4 video file ──────────────
            vp = video.get("video_path", "")
            if vp:
                result = _safe_delete_file(vp, logger, result, "MP4")
            
            # ── 2. Delete main narration MP3 ────────────────
            audio_path = video.get("audio_path", "")
            if audio_path:
                result = _safe_delete_file(audio_path, logger, result, "narration MP3")
            
            # ── 3. Delete CTA audio + derived files ─────────
            cp_raw = video.get("checkpoint_data", "{}")
            try:
                cp = _json.loads(cp_raw) if isinstance(cp_raw, str) else cp_raw
                cta_path = cp.get("tts", {}).get("cta_audio_path", "")
                if cta_path:
                    result = _delete_audio_group(cta_path, logger, result, "CTA")
            except (_json.JSONDecodeError, TypeError) as exc:
                logger.debug("Orphan temp cleanup: bad checkpoint_data for video %d: %s", video_id, exc)
            
            # ── 4. Delete scene asset files ─────────────────
            try:
                scenes = db.get_scenes(video_id)
                for scene in scenes:
                    image_path = scene.get("image_path", "")
                    if not image_path:
                        continue
                    # Extract actual file path(s) from image_path column
                    paths = _extract_paths_from_image_path(image_path)
                    for fp in paths:
                        result = _safe_delete_file(fp, logger, result, "scene asset")
                        # Also delete AI metadata sidecar
                        p = Path(fp)
                        if p.suffix.lower() in (".jpg", ".jpeg", ".png"):
                            json_sidecar = p.with_suffix(".pollo.json")
                            result = _safe_delete_file(str(json_sidecar), logger, result, "AI metadata")
            except Exception as exc:
                logger.debug(
                    "Orphan temp cleanup: could not query scenes for video %d: %s",
                    video_id, exc,
                    exc_info=True,
                )
            
        except Exception as exc:
            logger.debug(
                "Orphan temp cleanup: error for video %d: %s",
                video_id, exc,
                exc_info=True,
            )
            result["errors"].append({
                "video_id": video_id,
                "error": str(exc),
            })
            # Non-blocking: continue with next video
    
    if result["files_deleted"] > 0:
        logger.info(
            "Orphan temp file cleanup: deleted %d file(s), freed %.1f MB",
            result["files_deleted"],
            result["bytes_freed"] / (1024 * 1024),
        )
    if result["errors"]:
        logger.warning(
            "Orphan temp file cleanup: %d error(s) during deletion",
            len(result["errors"]),
        )
    
    return result


def _safe_delete_file(file_path: str, logger, result: dict, label: str) -> dict:
    """Delete a single file with pre-validation, retry, and non-blocking error.
    
    Expected error cases:
      - FileNotFoundError: already deleted (normal — no action).
      - PermissionError: no write permission on file or parent directory.
      - OSError: disk error, stale NFS handle.
    
    Returns updated result dict.
    """
    import os as _os
    
    if not file_path or not isinstance(file_path, str):
        return result
    
    # ── Pre-validation: existence ──
    if not _os.path.exists(file_path):
        logger.debug("Orphan cleanup: %s not found (already deleted): %s", label, file_path)
        return result
    
    # ── Pre-validation: write permission ──
    # For deletion we need write permission on the parent directory, not the file.
    # Check both as a safety net.
    parent_dir = _os.path.dirname(file_path)
    if parent_dir and _os.path.exists(parent_dir) and not _os.access(parent_dir, _os.W_OK):
        logger.warning(
            "Orphan cleanup: cannot delete %s — parent dir %s is not writable: %s",
            label, parent_dir, file_path,
        )
        result["errors"].append({
            "file": file_path,
            "error": f"Parent directory not writable: {parent_dir}",
        })
        return result
    
    def _do_delete():
        size = _os.path.getsize(file_path) if _os.path.isfile(file_path) else 0
        _os.remove(file_path)
        return size
    
    try:
        size = _retry_fs_op(_do_delete, max_retries=3, base_delay=0.5,
                            label=f"delete {label}: {file_path}")
        logger.debug("Orphan cleanup: deleted %s: %s (%.1f KB)", label, file_path, size / 1024)
        result["files_deleted"] += 1
        result["bytes_freed"] += size
    except OrphanCleanupError as exc:
        logger.debug(
            "Orphan cleanup: could not delete %s %s after retries: %s",
            label, file_path, exc,
            exc_info=True,
        )
        result["errors"].append({
            "file": file_path,
            "error": str(exc),
        })
    except Exception as exc:
        logger.debug(
            "Orphan cleanup: unexpected error deleting %s %s: %s",
            label, file_path, exc,
            exc_info=True,
        )
        result["errors"].append({
            "file": file_path,
            "error": str(exc),
        })
    
    return result


def _delete_audio_group(audio_path: str, logger, result: dict, label: str) -> dict:
    """Delete an audio file and its derived timestamps JSON + subtitles SRT.
    
    Returns updated result dict.
    """
    from pathlib import Path
    ap = Path(audio_path)
    result = _safe_delete_file(str(ap), logger, result, f"{label} MP3")
    
    stem_dir = ap.parent
    stem = ap.stem
    for suffix in ["_timestamps.json", "_subtitles.srt"]:
        derived = stem_dir / f"{stem}{suffix}"
        result = _safe_delete_file(str(derived), logger, result, f"{label} {suffix.lstrip('_')}")
    return result


def _extract_paths_from_image_path(image_path_value) -> list:
    """Extract actual file paths from image_path, handling both formats.
    
    Format A (newer): plain string like 'output/video_clips/pexels_abc123.mp4'
    Format B (legacy): dict repr like
        {'path': PosixPath('output/video_clips/pexels_video_27239437.mp4'), ...}
    """
    import re as _re
    if not image_path_value:
        return []
    
    s = str(image_path_value).strip()
    
    # Plain path string — already normalized
    if not s.startswith("{"):
        return [s]
    
    # Legacy dict repr — try to extract path
    match = _re.search(r"PosixPath\('([^']+)'\)", s)
    if match:
        return [match.group(1)]
    
    match = _re.search(r"'path'\s*:\s*'([^']+)'", s)
    if match:
        return [match.group(1)]
    
    return []


def _detect_and_clean_orphans_sync():
    """Synchronous orphan detection logic (runs in thread pool).
    
    Detects orphaned jobs, ghost workers, stuck insights, and stale temp files.
    Also prunes old cancelled/skipped planned slots.
    
    If accumulated failures exceed a threshold, generates a pipeline alert
    via the lifecycle monitor for operator visibility.
    """
    import logging
    logger = logging.getLogger("autotube.orphans")
    accumulated_errors = []
    
    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        result = db.cleanup_orphaned_jobs(timeout_minutes=ORPHAN_TIMEOUT_MINUTES)
        
        # Collect any per-item errors from the DB-level cleanup
        if result.get("errors"):
            accumulated_errors.extend(result["errors"])
        
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
        
        # ── Orphaned temp file cleanup ──
        # After marking jobs/videos as orphaned in the DB, delete their
        # temporary files from disk to free storage space.  This is
        # non-blocking — file deletion failures are logged and skipped.
        orphaned_jobs = result.get("details", [])
        orphaned_videos = [
            d for d in orphaned_jobs
            if d.get("type") in ("orphan_video",)
        ]
        temp_cleanup_result = _cleanup_orphaned_temp_files(
            db, orphaned_jobs, orphaned_videos, logger,
        )
        if temp_cleanup_result.get("errors"):
            accumulated_errors.extend(temp_cleanup_result["errors"])
        
        # Prune old cancelled/skipped slots (older than yesterday)
        prune_result = db.prune_old_slots()
        if prune_result.get("planned_slots_deleted") or prune_result.get("shorts_planned_slots_deleted"):
            logger.info(
                "Slot prune: %d planned + %d shorts deleted",
                prune_result["planned_slots_deleted"],
                prune_result["shorts_planned_slots_deleted"],
            )
        
        # ── Draft cleanup ─────────────────────────────────
        # Drafts accumulate when planned slots are created but
        # generation never completes. Prune old drafts and cap
        # per-channel maximum to prevent scheduler pollution.
        try:
            draft_deleted = db.cleanup_drafts(max_age_hours=72, max_per_channel=10)
            if draft_deleted:
                logger.info("Draft cleanup: %d stale drafts deleted", draft_deleted)
        except Exception as e:
            logger.warning("Draft cleanup skipped: %s", e)

        # ── Awaiting-script zombie cleanup (v24) ──────────
        # Videos stuck in 'awaiting_script' have been abandoned by the
        # planning system — no script was ever generated for them.
        # These accumulate when replans create slots that never dispatch.
        # Clean up any older than 48h to prevent DB bloat.
        try:
            with db._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM videos WHERE status = 'awaiting_script' "
                    "AND created_at < datetime('now', '-48 hours')"
                )
                deleted_zombies = cursor.rowcount
                conn.commit()
            if deleted_zombies:
                logger.info(
                    "Awaiting-script cleanup: %d zombie videos deleted (stuck >48h)",
                    deleted_zombies,
                )
        except Exception as e:
            logger.debug("Awaiting-script cleanup skipped: %s", e)

        # ── Glass Box: recover orphaned videos with complete .mp4 ──
        # Final safety net — rescues videos that were fully generated but
        # abandoned due to retry exhaustion, server restarts, or transient failures.
        # Runs as part of the orphan cleanup cycle to avoid adding more overhead.
        try:
            from api.services.glass_box import glass_box_recover_orphaned_videos
            result = glass_box_recover_orphaned_videos(db=db)
            if result.get("recovered", 0) > 0:
                logger.info(
                    "Glass Box: %d videos rescued from error → awaiting_upload "
                    "(skipped=%d, errors=%d)",
                    result["recovered"], result.get("skipped", 0), result.get("errors", 0),
                )
        except Exception as gb_exc:
            logger.debug("Glass Box recovery skipped: %s", gb_exc)

        # ── Alert generation on accumulated failures ─────────
        # If multiple errors accumulated across phases, raise a
        # pipeline alert so operators can investigate.
        ALERT_THRESHOLD = 3  # generate alert if ≥3 errors accumulated
        if len(accumulated_errors) >= ALERT_THRESHOLD:
            try:
                from api.services.lifecycle_monitor import create_alert
                error_summary = "\n".join(
                    f"- {e.get('file', e.get('error', str(e)))}"
                    for e in accumulated_errors[:10]  # cap at 10 entries
                )
                create_alert(
                    db,
                    entity_type="system",
                    alert_type="orphan",
                    severity="warning",
                    title=f"Orphan cleanup: {len(accumulated_errors)} errors accumulated",
                    message=f"Errors during orphan detection cycle:\n{error_summary}",
                    metadata={
                        "error_count": len(accumulated_errors),
                        "errors": accumulated_errors[:20],
                        "jobs_failed": result.get("jobs_failed", 0),
                        "videos_reset": result.get("videos_reset", 0),
                        "temp_files_deleted": temp_cleanup_result.get("files_deleted", 0),
                        "temp_bytes_freed": temp_cleanup_result.get("bytes_freed", 0),
                    },
                )
                logger.warning(
                    "Orphan cleanup alert generated: %d errors accumulated",
                    len(accumulated_errors),
                )
            except Exception as alert_exc:
                logger.debug(
                    "Orphan cleanup: failed to create alert: %s",
                    alert_exc,
                    exc_info=True,
                )
    except Exception as exc:
        logger.error("Orphan detection failed: %s", exc)
        logger.debug("Orphan detection traceback:", exc_info=True)


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
_STATS_COLLECTION_TIMEOUT = 900  # seconds — auto-reset to error if stuck "running" > 15 min (bumped from 300: a full scrape-mode run with the incremental window can legitimately exceed 5 min)

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


def _collect_youtube_stats(deep: bool = False, force: bool = False, use_data_api: bool = True):
    """Collect YouTube stats for all active channels.

    SYNC function on purpose: it performs blocking I/O (YouTube Data API,
    Analytics API, yt-dlp scraping). Starlette's BackgroundTasks runs sync
    functions in a thread pool, keeping the event loop free so the API keeps
    responding while stats are being collected. If this were `async def`, the
    blocking calls would freeze the entire API for the duration of the run.

    Args:
        deep: If True, also collects CTR, traffic sources, demographics,
              and retention % per video (extra API quota cost).
        force: If True, force Data API usage even if the channel's project
               quota is exhausted (instead of auto-switching to scrape mode).
        use_data_api: Global override. True (default): each channel decides by
              its OWN project breaker (Fase cuota ago 2026). False: everything
              runs in scrape-only mode (legacy callers).

    Fase cuota (ago 2026): la decisión de usar Data API es POR CANAL. Cada
    canal consulta el breaker de su proyecto GCP (is_quota_exhausted_for_channel),
    así un proyecto agotado (ej. tracatrack) no arrastra a los canales de la
    otra cuenta (burrianacasa2026) a modo scraping.
    """
    import logging
    logger = logging.getLogger("autotube.stats")

    STATS_COLLECTION_STATE.update({
        "status": "running",
        "started_at": _time_module.time(),
        "finished_at": None,
        "channels": [],
        "error": None,
        "scrape_mode": False,  # se rellena por canal abajo
    })
    _pin_stats_state("stats_collection_state")

    try:
        from database.db_extended import ExtendedDatabase
        from pipeline.youtube_stats import YouTubeStatsFetcher
        from pipeline.monetization import calc_video_revenue, calc_channel_revenue_total
        
        db = ExtendedDatabase()
        channels = db.get_channels(active_only=True)
        any_scrape_mode = False

        for ch in channels:
            slug = ch["slug"]
            token_path = TOKENS_DIR / f"{slug}.pickle"
            # ── Per-channel Data API decision ──
            # Cada canal decide según el breaker de SU proyecto GCP. force=True
            # salta el breaker (uso manual del Data API a pesar de la alerta).
            channel_use_data_api = use_data_api and (
                force or not db.is_quota_exhausted_for_channel(slug)
            )
            if channel_use_data_api and not token_path.exists():
                STATS_COLLECTION_STATE["channels"].append({
                    "slug": slug, "ok": False, "skipped": True,
                    "reason": "no token",
                })
                continue
            
            try:
                fetcher = YouTubeStatsFetcher(slug)
                result = fetcher.collect_and_store(
                    db, deep=deep, use_data_api=channel_use_data_api
                )
                if result.get("scrape_mode"):
                    any_scrape_mode = True
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
                    "quota_exhausted": result.get("quota_exhausted", False),
                    "analytics_fallback_videos": result.get("analytics_fallback_videos", 0),
                    "scrape_mode": result.get("scrape_mode", False),
                    "scrape_fallback_videos": result.get("scrape_fallback_videos", 0),
                    "scrape_fallback_shorts": result.get("scrape_fallback_shorts", 0),
                    "channel_scraped": result.get("channel_scraped", False),
                    "deep": deep,
                    "impressions_stored": result.get("impressions_stored", 0),
                    "ctr_stored": result.get("ctr_stored", 0),
                    "traffic_stored": result.get("traffic_stored", 0),
                    "retention_stored": result.get("retention_stored", 0),
                    "demographics_stored": result.get("demographics_stored", 0),
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
            "scrape_mode": any_scrape_mode,
        })
        _pin_stats_state("stats_collection_state")
        # Force dashboard cache invalidation so the frontend sees fresh data
        try:
            from api.routers.dashboard import invalidate_dashboard_cache
            invalidate_dashboard_cache()
        except Exception:
            pass
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

# ── Contenido no cacheable por defecto ──────────────────────────
# Garantiza que TODA respuesta sin Cache-Control explícito (API JSON,
# HTML, etc.) se sirva como no-store: un recargo de la página siempre
# muestra los últimos cambios aplicados. Las respuestas que YA definen
# su política se respetan tal cual (assets hasheados con cache inmutable,
# media con NO_CACHE_*, SSE con no-cache, thumbnails con ETag).
class NoCacheMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    headers.append((b"cache-control", b"no-store, no-cache, must-revalidate"))
                    headers.append((b"pragma", b"no-cache"))
                    headers.append((b"expires", b"0"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)

app.add_middleware(NoCacheMiddleware)

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
app.include_router(cross_platform_router.router, prefix="", tags=["Cross-Platform"])
app.include_router(monitor_router.router, prefix="/api", tags=["Monitor"])
app.include_router(insights.router, prefix="/api/channels", tags=["Insights AI"])
app.include_router(view_gap_router.router, prefix="/api", tags=["View Gap"])
app.include_router(quota_router.router, prefix="", tags=["Quota"])
app.include_router(redistribution_router.router, prefix="", tags=["Redistribution"])
app.include_router(pacing_router.router, tags=["Pacing"])

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
async def trigger_stats_collection(background_tasks: BackgroundTasks, deep: bool = False, force: bool = False):
    """Trigger on-demand YouTube stats collection for all active channels.

    Query params:
        deep: If true, also collects CTR, traffic sources, demographics, and
              retention % (consumes ~7 extra YouTube Analytics API quota units
              per channel). Default false = basic stats only.
        force: If True, force Data API usage even if quota is exhausted
               (instead of auto-switching to scrape mode).

    Runs _collect_youtube_stats() as a background task and returns immediately.
    Poll GET /api/stats/collect/status to know when it finishes and its result.

    Fase cuota (ago 2026): la decisión de usar Data API es POR CANAL — cada
    canal consulta el breaker de SU proyecto GCP. Un proyecto agotado ya NO
    arrastra a los canales de la otra cuenta a modo scraping.
    """
    _reset_stale_collection_state()  # auto-recover from stuck "running"
    if STATS_COLLECTION_STATE["status"] == "running":
        return {
            "ok": False,
            "message": "Ya hay una recoleccion en curso",
            "state": STATS_COLLECTION_STATE,
        }
    background_tasks.add_task(
        _collect_youtube_stats, deep=deep, force=force
    )
    return {
        "ok": True,
        "message": (
            "Recoleccion de stats iniciada (cada canal decide según la cuota de su cuenta)"
        ),
        "state": STATS_COLLECTION_STATE,
        "deep": deep,
        "force": force,
    }


@app.get("/api/stats/collect/status")
async def stats_collection_status():
    """Return current/last state of the on-demand stats collection."""
    _reset_stale_collection_state()  # auto-recover from stuck "running"
    return STATS_COLLECTION_STATE


# ═══════════════════════════════════════════════════════════════════
# A/B Testing Endpoints (v31 — Sequential title/thumbnail optimization)
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/videos/{video_id}/ab-test/trigger")
async def trigger_ab_test(video_id: int, phase: str = None, channel_id: int = None):
    """Manually trigger an A/B test cycle for a specific video.

    Args:
        video_id: Database video ID.
        phase: Optional phase override. One of:
            None (auto) — let the worker decide the phase
            'first_check' — force first CTR evaluation
            'second_check' — force post-change comparison
            'rotate_title' — force title rotation
            'rotate_thumbnail' — force thumbnail rotation to next variant
        channel_id: Optional — if provided, used for channel slug resolution.

    Returns:
        dict with status, message, and video details.
    """
    import logging as _logging
    _log = _logging.getLogger("autotube.ab_test")

    if not video_id:
        return {"status": "error", "message": "video_id is required"}

    from datetime import datetime as _dt, timezone as _tz

    try:
        import sqlite3
        from database.db_extended import ExtendedDatabase
        from api.services.ab_test_worker import ABTestWorker

        db = ExtendedDatabase()
        worker = ABTestWorker(db)

        # Fetch the A/B test record via db connection
        row_dict = None
        with db._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT vab.*, v.*, ch.slug as channel_slug "
                "FROM video_ab_tests vab "
                "JOIN videos v ON vab.video_id = v.id "
                "JOIN channels ch ON vab.channel_id = ch.id "
                "WHERE vab.video_id = ?",
                (video_id,),
            ).fetchone()
            if row:
                row_dict = dict(row)

        if not row_dict:
            return {"status": "error", "message": f"No A/B test record found for video {video_id}"}

        current_phase = row_dict.get("phase", "unknown")
        yt_video_id = row_dict.get("yt_video_id", "")

        now = _dt.now(_tz.utc)

        if phase == "first_check" or (phase is None and current_phase == "pending"):
            _log.info("[AB API] Triggering first_check for video %s", video_id)
            worker._process_first_check(row_dict, now)
            # Re-fetch updated state
            new_phase = "unknown"
            with db._connect() as conn:
                conn.row_factory = sqlite3.Row
                updated = conn.execute(
                    "SELECT * FROM video_ab_tests WHERE video_id = ?", (video_id,)
                ).fetchone()
                if updated:
                    new_phase = dict(updated).get("phase", "unknown")
            return {
                "status": "ok",
                "message": f"First check processed. Phase: {current_phase} → {new_phase}",
                "video_id": video_id,
                "yt_video_id": yt_video_id,
                "phase": new_phase,
            }

        elif phase == "second_check" or (phase is None and current_phase in ("title_rotated", "thumbnail_rotated")):
            _log.info("[AB API] Triggering second_check for video %s", video_id)
            worker._process_second_check(row_dict, now)
            new_phase = "unknown"
            winner = ""
            with db._connect() as conn:
                conn.row_factory = sqlite3.Row
                updated = conn.execute(
                    "SELECT * FROM video_ab_tests WHERE video_id = ?", (video_id,)
                ).fetchone()
                if updated:
                    updated_dict = dict(updated)
                    new_phase = updated_dict.get("phase", "unknown")
                    winner = updated_dict.get("winner_title", "")
            return {
                "status": "ok",
                "message": f"Second check processed. Phase: {current_phase} → {new_phase}",
                "video_id": video_id,
                "yt_video_id": yt_video_id,
                "phase": new_phase,
                "winner_title": winner,
            }

        elif phase == "rotate_title":
            title_v1 = row_dict.get("title_v1", "")
            ctr_v1 = row_dict.get("ctr_v1", 0) or 0
            _log.info("[AB API] Triggering title rotation for video %s", video_id)
            worker._rotate_title(row_dict, title_v1, ctr_v1)
            return {
                "status": "ok",
                "message": "Title rotation triggered",
                "video_id": video_id,
                "yt_video_id": yt_video_id,
            }

        elif phase:
            return {"status": "error", "message": f"Unknown phase: {phase}. Use: first_check, second_check, rotate_title, rotate_thumbnail"}

        else:
            return {
                "status": "info",
                "message": f"No action needed. Current phase: {current_phase}",
                "video_id": video_id,
                "yt_video_id": yt_video_id,
                "phase": current_phase,
            }

    except Exception as exc:
        _log.error("[AB API] Trigger failed: %s", exc)
        return {"status": "error", "message": str(exc)}


@app.get("/api/videos/{video_id}/ab-test/status")
async def get_ab_test_status(video_id: int):
    """Get current A/B test status and history for a video.

    Returns:
        dict with current phase, title variants, CTR data, thumbnail paths,
        and accumulated formula learnings for the channel.
    """
    if not video_id:
        return {"status": "error", "message": "video_id is required"}

    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

        # Get A/B test record
        conn = db._get_conn()
        row = conn.execute(
            "SELECT vab.*, v.yt_video_id as v_yt, v.titulo_final, v.created_at as v_created, "
            "ch.slug as channel_slug, ch.name as channel_name "
            "FROM video_ab_tests vab "
            "JOIN videos v ON vab.video_id = v.id "
            "JOIN channels ch ON vab.channel_id = ch.id "
            "WHERE vab.video_id = ?",
            (video_id,),
        ).fetchone()

        if not row:
            return {"status": "not_found", "message": f"No A/B test record for video {video_id}"}

        row_dict = dict(row)

        # Build response
        result = {
            "status": "ok",
            "video_id": video_id,
            "yt_video_id": row_dict.get("v_yt") or row_dict.get("yt_video_id"),
            "channel": row_dict.get("channel_slug"),
            "phase": row_dict.get("phase"),
            "title_v1": row_dict.get("title_v1"),
            "title_v2": row_dict.get("title_v2"),
            "ctr_v1": row_dict.get("ctr_v1"),
            "ctr_v2": row_dict.get("ctr_v2"),
            "impressions_v1": row_dict.get("impressions_v1"),
            "impressions_v2": row_dict.get("impressions_v2"),
            "retention_v1": row_dict.get("retention_v1"),
            "retention_v2": row_dict.get("retention_v2"),
            "winner_title": row_dict.get("winner_title"),
            "thumbnail_variant_active": row_dict.get("thumbnail_variant_active"),
            "timestamps": {
                "first_checked": row_dict.get("first_checked_at"),
                "title_rotated": row_dict.get("title_rotated_at"),
                "thumbnail_rotated": row_dict.get("thumbnail_rotated_at"),
                "second_checked": row_dict.get("second_checked_at"),
                "completed": row_dict.get("completed_at"),
                "created": row_dict.get("created_at"),
            },
        }

        # Parse thumbnail paths
        try:
            import json
            paths_json = row_dict.get("thumbnail_variant_paths", "[]") or "[]"
            result["thumbnail_variant_paths"] = json.loads(paths_json)
        except Exception:
            result["thumbnail_variant_paths"] = []

        # Get channel learnings
        channel_id = row_dict.get("channel_id")
        if channel_id:
            learnings = conn.execute(
                "SELECT formula_type, total_tests, total_wins, avg_ctr_improvement "
                "FROM title_formula_performance WHERE channel_id = ? "
                "ORDER BY total_wins DESC",
                (channel_id,),
            ).fetchall()
            result["formula_learnings"] = [dict(l) for l in learnings] if learnings else []

        return result

    except Exception as exc:
        import logging as _logging
        _logging.getLogger("autotube.ab_test").error("Status fetch failed: %s", exc)
        return {"status": "error", "message": str(exc)}


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
    import mimetypes as _mimetypes
    
    # Content-hashed file extensions — safe to cache indefinitely
    _IMMUTABLE_EXTS = frozenset({".js", ".css", ".mjs", ".woff", ".woff2", ".ttf", ".otf", ".png", ".svg", ".webp", ".ico", ".avif"})
    _IMMUTABLE_CACHE = {
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    
    class SmartCacheStaticFiles(StaticFiles):
        """Serve static files with appropriate cache headers.

        Content-hashed assets (JS, CSS, fonts, images) get immutable
        1-year cache.  Everything else (HTML fragments, robots.txt, etc.)
        gets no-cache to ensure freshness.
        """
        async def __call__(self, scope, receive, send):
            # Determine cache policy from the requested path
            raw_path = scope.get("path", "")
            _, ext = os.path.splitext(raw_path)
            if ext.lower() in _IMMUTABLE_EXTS:
                cache_headers = _IMMUTABLE_CACHE
            else:
                cache_headers = NO_CACHE
            
            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    headers = dict(message.get("headers", []))
                    for k, v in cache_headers.items():
                        headers[k.encode()] = v.encode()
                    message["headers"] = [(k, v) for k, v in headers.items()]
                await send(message)
            await super().__call__(scope, receive, send_wrapper)
    
    # ── Mount static assets at /assets and /autotube/assets (vite base path) ──
    static_assets = SmartCacheStaticFiles(directory=STATIC_DIR / "assets")
    app.mount("/assets", static_assets, name="assets")
    app.mount("/autotube/assets", static_assets, name="autotube_assets")
    
    # ── Also serve root-level files under /autotube/ path ──
    # HEAD explícito: validadores/proxies de caché usan HEAD para comprobar
    # frescura; sin él devolvían 405.
    @app.api_route("/autotube/{path:path}", methods=["GET", "HEAD"])
    async def autotube_spa_fallback(path: str):
        """Serve SPA under /autotube/ base path — matches vite's base config."""
        if path.startswith("api/"):
            raise HTTPException(404, "API endpoint not found")
        if path.startswith("assets/"):
            raise HTTPException(404, "Not found")
        full_path = STATIC_DIR / path
        if full_path.exists() and full_path.is_file():
            return FileResponse(full_path, headers=NO_CACHE)
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)
    
    @app.api_route("/{path:path}", methods=["GET", "HEAD"])
    async def spa_fallback(path: str):
        """Serve React SPA — all non-API routes go to index.html."""
        if path.startswith("api/"):
            raise HTTPException(404, "API endpoint not found")
        full_path = STATIC_DIR / path
        if full_path.exists() and full_path.is_file():
            return FileResponse(full_path, headers=NO_CACHE)
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)
    
    @app.api_route("/", methods=["GET", "HEAD"])
    async def root():
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)
