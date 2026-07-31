"""Monitor router — lifecycle events, alerts, health dashboard, system metrics, and live logs."""

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from typing import Optional
from pathlib import Path
import json
import time
import asyncio
import logging
import shutil
from api.deps import get_db
from api.services.lifecycle_monitor import (
    acknowledge_alert,
    resolve_alert,
    resolve_all_alerts,
    check_all_health,
)

router = APIRouter()
logger = logging.getLogger("autotube.monitor")
ws_clients: list[WebSocket] = []

# In-memory TTL cache for monitor dashboard
_MONITOR_CACHE: dict = {}
_MONITOR_CACHE_TTL = 15  # seconds — monitor needs more freshness than dashboard
_MONITOR_CACHE_LOCK = None  # lazy import to avoid issues at module level

def _get_monitor_lock():
    global _MONITOR_CACHE_LOCK
    if _MONITOR_CACHE_LOCK is None:
        import threading
        _MONITOR_CACHE_LOCK = threading.Lock()
    return _MONITOR_CACHE_LOCK

# ── System metrics helpers ─────────────────────────────────────

def _get_system_metrics() -> dict:
    """Collect CPU, RAM, disk, and uptime metrics."""
    import os
    metrics: dict = {}

    try:
        import psutil
        # CPU
        metrics["cpu_percent"] = psutil.cpu_percent(interval=0.3)
        metrics["cpu_count"] = psutil.cpu_count()
        metrics["cpu_per_core"] = psutil.cpu_percent(interval=None, percpu=True)
        # RAM
        mem = psutil.virtual_memory()
        metrics["ram_total_mb"] = mem.total // (1024 * 1024)
        metrics["ram_used_mb"] = mem.used // (1024 * 1024)
        metrics["ram_available_mb"] = mem.available // (1024 * 1024)
        metrics["ram_percent"] = mem.percent
        # Load
        metrics["load_1m"], metrics["load_5m"], metrics["load_15m"] = psutil.getloadavg()
    except ImportError:
        # Fallback: /proc parsing (no psutil)
        try:
            with open("/proc/loadavg") as f:
                parts = f.read().strip().split()
                metrics["load_1m"] = float(parts[0])
                metrics["load_5m"] = float(parts[1])
                metrics["load_15m"] = float(parts[2])
        except Exception:
            metrics["cpu_percent"] = -1
        try:
            with open("/proc/meminfo") as f:
                meminfo = {}
                for line in f:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meminfo[k.strip()] = int(v.strip().split()[0])
                total_kb = meminfo.get("MemTotal", 0)
                avail_kb = meminfo.get("MemAvailable", 0)
                metrics["ram_total_mb"] = total_kb // 1024
                metrics["ram_available_mb"] = avail_kb // 1024
                metrics["ram_used_mb"] = (total_kb - avail_kb) // 1024
                metrics["ram_percent"] = round(((total_kb - avail_kb) / total_kb) * 100, 1) if total_kb else 0
        except Exception:
            metrics["ram_total_mb"] = metrics["ram_available_mb"] = metrics["ram_used_mb"] = 0
            metrics["ram_percent"] = 0

    # Disk
    try:
        du_output = shutil.disk_usage("output")
        metrics["disk_output_free_gb"] = round(du_output.free / (1024**3), 1)
        metrics["disk_output_total_gb"] = round(du_output.total / (1024**3), 1)
    except Exception:
        metrics["disk_output_free_gb"] = -1
        metrics["disk_output_total_gb"] = -1
    try:
        du_logs = shutil.disk_usage("logs")
        metrics["disk_logs_free_gb"] = round(du_logs.free / (1024**3), 1)
        metrics["disk_logs_total_gb"] = round(du_logs.total / (1024**3), 1)
    except Exception:
        metrics["disk_logs_free_gb"] = -1
        metrics["disk_logs_total_gb"] = -1

    # Uptime
    try:
        with open("/proc/uptime") as f:
            metrics["uptime_seconds"] = float(f.read().split()[0])
    except Exception:
        metrics["uptime_seconds"] = -1

    metrics["collected_at"] = time.time()
    return metrics


def _get_worker_process_ram_mb(pid: Optional[int]) -> Optional[int]:
    """Get RSS memory of a process by PID in MB."""
    if not pid:
        return None
    try:
        import psutil
        proc = psutil.Process(pid)
        return proc.memory_info().rss // (1024 * 1024)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# REST Endpoints
# ═══════════════════════════════════════════════════════════════

@router.get("/monitor/dashboard")
def get_monitor_dashboard():
    """Overall monitoring dashboard — active counts + alert summary."""
    import time as time_mod
    lock = _get_monitor_lock()
    with lock:
        if "dashboard" in _MONITOR_CACHE:
            entry = _MONITOR_CACHE["dashboard"]
            if time_mod.time() - entry["ts"] < _MONITOR_CACHE_TTL:
                return entry["data"]

    db = get_db()
    try:
        with db._connect() as conn:
            # Active video counts
            videos_generating = conn.execute(
                "SELECT COUNT(*) as c FROM videos WHERE status = 'generating'"
            ).fetchone()["c"]
            videos_awaiting_upload = conn.execute(
                "SELECT COUNT(*) as c FROM videos WHERE status = 'awaiting_upload'"
            ).fetchone()["c"]
            videos_uploaded_private = conn.execute(
                "SELECT COUNT(*) as c FROM videos WHERE status = 'uploaded_private'"
            ).fetchone()["c"]
            videos_error = conn.execute(
                "SELECT COUNT(*) as c FROM videos WHERE status = 'error' AND created_at > datetime('now', '-7 days')"
            ).fetchone()["c"]

            # Active short counts
            shorts_rendering = conn.execute(
                "SELECT COUNT(*) as c FROM shorts WHERE status = 'rendering'"
            ).fetchone()["c"]
            shorts_uploading = conn.execute(
                "SELECT COUNT(*) as c FROM shorts WHERE status = 'uploading'"
            ).fetchone()["c"]
            shorts_failed = conn.execute(
                "SELECT COUNT(*) as c FROM shorts WHERE status = 'failed' AND created_at > datetime('now', '-7 days')"
            ).fetchone()["c"]

            # Alert counts
            alerts_critical = conn.execute(
                "SELECT COUNT(*) as c FROM pipeline_alerts WHERE severity = 'critical' AND resolved = 0"
            ).fetchone()["c"]
            alerts_warning = conn.execute(
                "SELECT COUNT(*) as c FROM pipeline_alerts WHERE severity = 'warning' AND resolved = 0"
            ).fetchone()["c"]
            alerts_info = conn.execute(
                "SELECT COUNT(*) as c FROM pipeline_alerts WHERE severity = 'info' AND resolved = 0"
            ).fetchone()["c"]
            alerts_total = alerts_critical + alerts_warning + alerts_info
            alerts_unacknowledged = conn.execute(
                "SELECT COUNT(*) as c FROM pipeline_alerts WHERE acknowledged = 0 AND resolved = 0"
            ).fetchone()["c"]

            # Health score (0-100): capped per-category penalties
            # Errors > 0 cost 15 pts, each additional error costs 1 pt up to max penalty
            # Plus 5 pts per critical alert, 2 pts per warning
            error_penalty = min(60, 15 + videos_error * 1) if videos_error > 0 else 0
            short_penalty = min(30, 10 + shorts_failed * 2) if shorts_failed > 0 else 0
            alert_penalty = min(30, alerts_critical * 5 + alerts_warning * 2)
            health = max(0, 100 - error_penalty - short_penalty - alert_penalty)

            # Active generating videos details
            active_videos = [
                dict(r) for r in conn.execute(
                    """SELECT v.id, v.canal, v.progress_phase, v.progress, v.titulo_final,
                              g.id as job_id, g.last_heartbeat_at
                       FROM videos v
                       LEFT JOIN generation_jobs g ON g.video_id = v.id AND g.status = 'running'
                       WHERE v.status = 'generating'
                       ORDER BY v.id DESC LIMIT 10"""
                ).fetchall()
            ]

            # Active short details
            active_shorts = [
                dict(r) for r in conn.execute(
                    """SELECT s.id, s.type, s.status, s.title,
                              ss.scheduled_at
                       FROM shorts s
                       LEFT JOIN shorts_planned_slots ss ON ss.short_id = s.id AND ss.status = 'running'
                       WHERE s.status IN ('extracting', 'rendering', 'uploading')
                       ORDER BY s.id DESC LIMIT 10"""
                ).fetchall()
            ]

            # Recent events (last 20)
            recent_events = [
                dict(r) for r in conn.execute(
                    """SELECT * FROM lifecycle_events
                       ORDER BY created_at DESC LIMIT 20"""
                ).fetchall()
            ]

            # Quick stats
            generated_today = conn.execute(
                """SELECT COUNT(*) as c FROM videos
                   WHERE date(created_at) = date('now', 'localtime')
                     AND status IN ('generating', 'awaiting_upload', 'uploading',
                                    'uploaded', 'uploaded_private', 'published', 'ready')"""
            ).fetchone()["c"]

            # Success rate (last 7 days)
            total_7d = conn.execute(
                "SELECT COUNT(*) as c FROM videos WHERE created_at > datetime('now', '-7 days')"
            ).fetchone()["c"]
            success_7d = conn.execute(
                """SELECT COUNT(*) as c FROM videos
                   WHERE created_at > datetime('now', '-7 days')
                     AND status IN ('uploaded', 'published', 'ready', 'uploaded_private')"""
            ).fetchone()["c"]
            success_rate = round(success_7d / total_7d * 100, 1) if total_7d > 0 else 0

            # Average generation time
            avg_gen = conn.execute(
                """SELECT AVG(
                       (julianday(generation_finished_at) - julianday(generation_started_at)) * 1440
                   ) as avg_min
                   FROM videos
                   WHERE generation_started_at IS NOT NULL
                     AND generation_finished_at IS NOT NULL
                     AND timing_data IS NOT NULL"""
            ).fetchone()
            avg_gen_min = round(avg_gen["avg_min"]) if avg_gen and avg_gen["avg_min"] else None

            # Next scheduled slot
            next_slot = conn.execute(
                """SELECT ps.channel_id, c.name as channel_name, c.slug,
                          ps.target_upload_at, ps.target_public_at
                   FROM planned_slots ps
                   JOIN channels c ON c.id = ps.channel_id
                   WHERE ps.status = 'pending'
                   ORDER BY COALESCE(ps.target_upload_at, ps.target_public_at, ps.scheduled_at) ASC
                   LIMIT 1"""
            ).fetchone()

            result = {
                "health_score": health,
                "videos": {
                    "generating": videos_generating,
                    "awaiting_upload": videos_awaiting_upload,
                    "uploaded_private": videos_uploaded_private,
                    "error": videos_error,
                    "active": active_videos,
                },
                "shorts": {
                    "rendering": shorts_rendering,
                    "uploading": shorts_uploading,
                    "failed": shorts_failed,
                    "active": active_shorts,
                },
                "alerts": {
                    "total": alerts_total,
                    "critical": alerts_critical,
                    "warning": alerts_warning,
                    "info": alerts_info,
                    "unacknowledged": alerts_unacknowledged,
                },
                "recent_events": recent_events,
                "quick_stats": {
                    "generated_today": generated_today,
                    "success_rate_7d": success_rate,
                    "total_7d": total_7d,
                    "avg_generation_minutes": avg_gen_min,
                    "next_slot": {
                        "channel": next_slot["channel_name"] if next_slot else None,
                        "slug": next_slot["slug"] if next_slot else None,
                        "at": (next_slot["target_upload_at"] or next_slot["target_public_at"]) if next_slot else None,
                    } if next_slot else None,
                },
            }
            with _get_monitor_lock():
                _MONITOR_CACHE["dashboard"] = {"data": result, "ts": time_mod.time()}
            return result
    except Exception as exc:
        logger.error("Monitor dashboard error: %s", exc)
        return {"health_score": 0, "error": str(exc)}


@router.get("/monitor/alerts")
def get_alerts(
    status: Optional[str] = Query(None, description="active, acknowledged, resolved"),
    severity: Optional[str] = Query(None, description="critical, warning, info"),
    entity_type: Optional[str] = Query(None, description="video, short, system"),
    channel_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """Get pipeline alerts with optional filters."""
    db = get_db()
    try:
        with db._connect() as conn:
            conditions = []
            params = []

            if status == "active":
                conditions.append("resolved = 0")
            elif status == "acknowledged":
                conditions.append("acknowledged = 1")
            elif status == "resolved":
                conditions.append("resolved = 1")

            if severity:
                conditions.append("severity = ?")
                params.append(severity)
            if entity_type:
                conditions.append("entity_type = ?")
                params.append(entity_type)
            if channel_id:
                conditions.append("channel_id = ?")
                params.append(channel_id)

            where_clause = " AND ".join(conditions) if conditions else "1=1"
            params.append(limit)

            rows = conn.execute(
                f"""SELECT pa.*, 
                           CASE WHEN pa.entity_type = 'video' THEN v.titulo_final
                                WHEN pa.entity_type = 'short' THEN s.title
                                ELSE NULL END as entity_title,
                           CASE WHEN pa.entity_type = 'video' THEN v.canal
                                ELSE NULL END as entity_slug
                    FROM pipeline_alerts pa
                    LEFT JOIN videos v ON pa.entity_type = 'video' AND pa.entity_id = v.id
                    LEFT JOIN shorts s ON pa.entity_type = 'short' AND pa.entity_id = s.id
                    WHERE {where_clause}
                    ORDER BY 
                        CASE pa.severity WHEN 'critical' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
                        pa.created_at DESC
                    LIMIT ?""",
                params,
            ).fetchall()

            return {"alerts": [dict(r) for r in rows]}
    except Exception as exc:
        logger.error("Get alerts error: %s", exc)
        return {"alerts": [], "error": str(exc)}


@router.post("/monitor/alerts/{alert_id}/acknowledge")
def ack_alert(alert_id: int):
    """Mark alert as acknowledged."""
    db = get_db()
    success = acknowledge_alert(db, alert_id)
    return {"ok": success, "alert_id": alert_id}


@router.post("/monitor/alerts/{alert_id}/resolve")
def res_alert(alert_id: int):
    """Mark alert as resolved."""
    db = get_db()
    success = resolve_alert(db, alert_id)
    return {"ok": success, "alert_id": alert_id}


@router.post("/monitor/alerts/resolve-all")
def res_all_alerts(
    severity: Optional[str] = Query(None, description="Filter: critical, warning, info"),
):
    """Bulk-resolve all unresolved alerts, optionally filtered by severity."""
    db = get_db()
    count = resolve_all_alerts(db, severity=severity)
    return {"ok": True, "resolved": count}


@router.get("/monitor/events")
def get_events(
    entity_type: Optional[str] = Query(None, description="video or short"),
    entity_id: Optional[int] = Query(None),
    channel_id: Optional[int] = Query(None),
    event: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """Get lifecycle events with filters."""
    db = get_db()
    try:
        with db._connect() as conn:
            conditions = []
            params = []

            if entity_type:
                conditions.append("entity_type = ?")
                params.append(entity_type)
            if entity_id:
                conditions.append("entity_id = ?")
                params.append(entity_id)
            if channel_id:
                conditions.append("channel_id = ?")
                params.append(channel_id)
            if event:
                conditions.append("event = ?")
                params.append(event)

            where_clause = " AND ".join(conditions) if conditions else "1=1"
            params.append(limit)

            rows = conn.execute(
                f"""SELECT * FROM lifecycle_events
                    WHERE {where_clause}
                    ORDER BY created_at DESC
                    LIMIT ?""",
                params,
            ).fetchall()

            return {"events": [dict(r) for r in rows]}
    except Exception as exc:
        logger.error("Get events error: %s", exc)
        return {"events": [], "error": str(exc)}


@router.post("/monitor/health-check")
def trigger_health_check():
    """Manually trigger a health check scan."""
    db = get_db()
    result = check_all_health(db)
    return result


# ═══════════════════════════════════════════════════════════════
# System metrics endpoint
# ═══════════════════════════════════════════════════════════════

@router.get("/monitor/system")
def get_system_metrics_endpoint():
    """Real-time system metrics: CPU, RAM, disk, uptime."""
    try:
        metrics = _get_system_metrics()
        return {"ok": True, **metrics}
    except Exception as exc:
        logger.error("System metrics error: %s", exc)
        return {"ok": False, "error": str(exc)}


# ═══════════════════════════════════════════════════════════════
# Status bar (lightweight — what the fixed header bar polls)
# ═══════════════════════════════════════════════════════════════

@router.get("/monitor/status-bar")
def get_status_bar():
    """Lightweight endpoint for the fixed header status bar."""
    db = get_db()
    try:
        with db._connect() as conn:
            active_long = conn.execute(
                "SELECT COUNT(*) as c FROM generation_jobs WHERE status = 'running'"
            ).fetchone()["c"]
            active_shorts = conn.execute(
                "SELECT COUNT(*) as c FROM shorts WHERE status IN ('extracting','rendering','uploading')"
            ).fetchone()["c"]
            critical_alerts = conn.execute(
                "SELECT COUNT(*) as c FROM pipeline_alerts WHERE severity = 'critical' AND resolved = 0"
            ).fetchone()["c"]
            # RAM (quick)
            from pipeline.ram_governor import available_mb
            ram_available = available_mb()
            from pipeline.ram_governor import available_mb
            ram_free = ram_available if ram_available > 0 else None

            return {
                "workers": active_long + active_shorts,
                "long_running": active_long,
                "shorts_running": active_shorts,
                "ram_available_mb": ram_free,
                "critical_alerts": critical_alerts,
            }
    except Exception as exc:
        logger.error("Status bar error: %s", exc)
        return {"workers": 0, "ram_available_mb": None, "critical_alerts": 0, "error": str(exc)}


# ═══════════════════════════════════════════════════════════════
# Active workers endpoint
# ═══════════════════════════════════════════════════════════════

@router.get("/monitor/workers")
def get_active_workers():
    """Detailed info on all active generation workers (long-form + shorts)."""
    db = get_db()
    try:
        workers = []
        with db._connect() as conn:
            # Long-form jobs
            long_rows = conn.execute(
                """SELECT g.id as job_id, g.video_id, g.status, g.progress, g.phase,
                          g.pipeline_phase, g.started_at, g.last_heartbeat_at, g.retry_count,
                          g.worker_pid, g.action, g.error_msg,
                          v.canal, v.titulo_final, v.progress_phase, v.progress as video_progress,
                          v.timing_data, v.channel_id
                   FROM generation_jobs g
                   LEFT JOIN videos v ON v.id = g.video_id
                   WHERE g.status = 'running'
                   ORDER BY g.id DESC"""
            ).fetchall()

            for row in long_rows:
                elapsed_s = 0
                if row["started_at"]:
                    try:
                        from datetime import datetime
                        started = datetime.strptime(row["started_at"][:19], "%Y-%m-%d %H:%M:%S")
                        elapsed_s = max(0, (datetime.utcnow() - started).total_seconds())
                    except Exception:
                        pass

                workers.append({
                    "type": "long",
                    "job_id": row["job_id"],
                    "video_id": row["video_id"],
                    "channel": row["canal"],
                    "channel_id": row["channel_id"],
                    "title": row["titulo_final"],
                    "status": row["status"],
                    "progress": row["progress"] or row["video_progress"] or 0,
                    "phase": row["phase"] or row["progress_phase"] or "unknown",
                    "pipeline_phase": row["pipeline_phase"],
                    "action": row["action"],
                    "started_at": row["started_at"],
                    "elapsed_seconds": int(elapsed_s),
                    "worker_pid": row["worker_pid"],
                    "worker_ram_mb": _get_worker_process_ram_mb(row["worker_pid"]),
                    "retry_count": row["retry_count"],
                    "error_msg": row["error_msg"],
                })

            # Shorts jobs
            short_rows = conn.execute(
                """SELECT s.id, s.title, s.status, s.type, s.channel_id,
                          ss.job_id, ss.scheduled_at,
                          c.slug as canal
                   FROM shorts s
                   LEFT JOIN shorts_planned_slots ss ON ss.short_id = s.id AND ss.status = 'running'
                   LEFT JOIN channels c ON c.id = s.channel_id
                   WHERE s.status IN ('extracting', 'rendering', 'uploading')
                   ORDER BY s.id DESC"""
            ).fetchall()

            for row in short_rows:
                workers.append({
                    "type": "short",
                    "short_id": row["id"],
                    "job_id": row["job_id"],
                    "channel": row["canal"],
                    "channel_id": row["channel_id"],
                    "title": row["title"],
                    "status": row["status"],
                    "short_type": row["type"],
                    "progress": 0,
                    "phase": row["status"],
                    "started_at": row["scheduled_at"],
                    "elapsed_seconds": 0,
                    "worker_pid": None,
                    "worker_ram_mb": None,
                })

        return {"ok": True, "workers": workers}
    except Exception as exc:
        logger.error("Active workers error: %s", exc)
        return {"ok": False, "workers": [], "error": str(exc)}


# ═══════════════════════════════════════════════════════════════
# Live log streaming (SSE)
# ═══════════════════════════════════════════════════════════════

@router.get("/monitor/logs/{job_id}/stream")
async def stream_worker_logs(job_id: int, since_line: int = 0, tail_lines: int = 200):
    """SSE stream of worker_{job_id}.log in real time (tail -f style)."""
    log_path = Path(f"logs/worker_{job_id}.log")

    async def event_stream():
        line_no = since_line
        if not log_path.exists():
            yield f"data: {json.dumps({'line': f'[monitor] Log file not found: {log_path}', 'line_no': 0, 'level': 'WARNING'})}\n\n"
            return

        try:
            # Read existing tail lines first
            with open(log_path, "r") as f:
                lines = f.readlines()
                start = max(0, len(lines) - tail_lines)
                for i, line in enumerate(lines[start:], start=start):
                    ln = i - (len(lines) - tail_lines) if len(lines) > tail_lines else i
                    parsed = _parse_log_line(line)
                    yield f"data: {json.dumps({'line': line.rstrip(), 'line_no': ln, **parsed})}\n\n"
                    line_no = i + 1

            # Tail new lines
            with open(log_path, "r") as f:
                f.seek(0, 2)  # end of file
                while True:
                    line = f.readline()
                    if line:
                        parsed = _parse_log_line(line)
                        yield f"data: {json.dumps({'line': line.rstrip(), 'line_no': line_no, **parsed})}\n\n"
                        line_no += 1
                    else:
                        await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            yield f"data: {json.dumps({'line': f'[monitor] Stream error: {exc}', 'line_no': line_no, 'level': 'ERROR'})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _parse_log_line(line: str) -> dict:
    """Extract log level from a line if present."""
    result = {"level": "INFO"}
    for lvl in ("CRITICAL", "ERROR", "WARNING", "DEBUG"):
        if lvl in line:
            result["level"] = lvl
            break
    return result


# ═══════════════════════════════════════════════════════════════
# ETA estimation endpoint
# ═══════════════════════════════════════════════════════════════

@router.get("/monitor/eta/{job_id}")
def get_job_eta(job_id: int):
    """Estimate remaining time for an active job based on historical phase timings."""
    db = get_db()
    try:
        with db._connect() as conn:
            job = conn.execute(
                "SELECT video_id, phase, progress, started_at FROM generation_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not job:
                return {"ok": False, "error": "Job not found"}

            # Get current phase timing_data if available
            video = conn.execute(
                "SELECT timing_data, progress_phase, progress FROM videos WHERE id = ?",
                (job["video_id"],),
            ).fetchone()

            eta_seconds = None
            current_phase = job["phase"] or (video["progress_phase"] if video else None)
            current_progress = job["progress"] or (video["progress"] if video else 0)

            if video and video["timing_data"]:
                try:
                    timing = json.loads(video["timing_data"])
                    if isinstance(timing, dict) and "phases" in timing:
                        history = timing["phases"]
                        # Look at average phase durations from historical data
                        avg_total_min = 0
                        phase_count = 0
                        for pname, pdata in history.items():
                            dur = pdata.get("duration_ms", 0) if isinstance(pdata, dict) else pdata
                            if isinstance(dur, (int, float)) and dur > 0:
                                avg_total_min += dur / 1000 / 60
                                phase_count += 1
                        if avg_total_min > 0:
                            # Rough ETA based on remaining progress
                            remaining_pct = max(0, 100 - current_progress) / 100
                            eta_seconds = int(avg_total_min * 60 * remaining_pct)
                except Exception:
                    pass

            # Fallback: average all completed video times
            if eta_seconds is None:
                avg = conn.execute(
                    """SELECT AVG(
                           (julianday(COALESCE(generation_finished_at, datetime('now')))
                          - julianday(generation_started_at)) * 86400
                       ) as avg_s
                       FROM videos
                       WHERE status IN ('uploaded', 'published', 'ready', 'uploaded_private')
                         AND generation_started_at IS NOT NULL
                         AND generation_finished_at IS NOT NULL
                         AND timing_data IS NOT NULL"""
                ).fetchone()
                if avg and avg["avg_s"] and avg["avg_s"] > 0:
                    remaining_pct = max(0, 100 - current_progress) / 100
                    eta_seconds = int(avg["avg_s"] * remaining_pct)

            # Calculate elapsed
            elapsed_s = 0
            if job["started_at"]:
                try:
                    from datetime import datetime
                    started = datetime.strptime(job["started_at"][:19], "%Y-%m-%d %H:%M:%S")
                    elapsed_s = max(0, (datetime.utcnow() - started).total_seconds())
                except Exception:
                    pass

            return {
                "ok": True,
                "job_id": job_id,
                "current_phase": current_phase,
                "current_progress": current_progress,
                "elapsed_seconds": int(elapsed_s),
                "eta_seconds": eta_seconds,
                "eta_minutes": round(eta_seconds / 60, 1) if eta_seconds else None,
            }
    except Exception as exc:
        logger.error("ETA error: %s", exc)
        return {"ok": False, "error": str(exc)}


# ═══════════════════════════════════════════════════════════════
# WebSocket
# ═══════════════════════════════════════════════════════════════

@router.websocket("/ws/monitor")
async def ws_monitor(websocket: WebSocket):
    """Real-time monitoring updates WebSocket."""
    await websocket.accept()
    ws_clients.append(websocket)
    logger.info("Monitor WS client connected (%d total)", len(ws_clients))
    try:
        while True:
            try:
                # Keep alive — ping every 30s
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                # Client can request specific data via commands
                if data == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
                elif data == "status":
                    # Client requested a fresh status snapshot
                    await websocket.send_text(json.dumps({
                        "type": "status_snapshot",
                        "data": get_status_bar(),
                    }))
                    await websocket.send_text(json.dumps({
                        "type": "system_snapshot",
                        "data": _get_system_metrics(),
                    }))
            except asyncio.TimeoutError:
                # Send keepalive + lightweight status snapshot
                try:
                    status = get_status_bar()
                    await websocket.send_text(json.dumps({
                        "type": "keepalive",
                        "status": status,
                    }))
                except Exception:
                    break
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        if websocket in ws_clients:
            ws_clients.remove(websocket)
        logger.info("Monitor WS client disconnected (%d remaining)", len(ws_clients))


async def broadcast_monitor_update(data: dict):
    """Broadcast a monitoring update to all connected WS clients."""
    disconnected = []
    for ws in ws_clients:
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in ws_clients:
            ws_clients.remove(ws)


async def broadcast_status_snapshot():
    """Periodic status snapshot broadcast for all WS clients."""
    try:
        status = get_status_bar()
        system = _get_system_metrics()
        await broadcast_monitor_update({
            "type": "snapshot",
            "status": status,
            "system": {
                "cpu_percent": system.get("cpu_percent"),
                "ram_available_mb": system.get("ram_available_mb"),
                "ram_percent": system.get("ram_percent"),
                "disk_output_free_gb": system.get("disk_output_free_gb"),
                "uptime_seconds": system.get("uptime_seconds"),
            },
        })
    except Exception:
        pass
