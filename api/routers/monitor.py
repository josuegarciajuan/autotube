"""Monitor router — lifecycle events, alerts, and health dashboard."""

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from typing import Optional
import json
import asyncio
import logging
from api.deps import get_db
from api.services.lifecycle_monitor import (
    acknowledge_alert,
    resolve_alert,
    check_all_health,
)

router = APIRouter()
logger = logging.getLogger("autotube.monitor")
ws_clients: list[WebSocket] = []


# ═══════════════════════════════════════════════════════════════
# REST Endpoints
# ═══════════════════════════════════════════════════════════════

@router.get("/monitor/dashboard")
def get_monitor_dashboard():
    """Overall monitoring dashboard — active counts + alert summary."""
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

            return {
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
            }
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
                # Client can send commands if needed
                if data == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except asyncio.TimeoutError:
                # Send keepalive ping
                try:
                    await websocket.send_text(json.dumps({"type": "keepalive"}))
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
