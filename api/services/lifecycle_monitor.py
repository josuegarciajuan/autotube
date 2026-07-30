"""Lifecycle Monitor — unified event logging + proactive alerts for videos and shorts.

Usage:
    from api.services.lifecycle_monitor import log_event, check_all_health

    # From anywhere in the pipeline:
    log_event(db, entity_type='video', entity_id=1434, channel_id=7,
              event='phase_completed', phase='script', status='completed',
              message='Script generated (34 escenas, 42 bloques)',
              metadata={'duration_ms': 45230})

    # Health check (called periodically by API background task):
    health = check_all_health(db)
    # Returns dict with active alerts created, resolved, etc.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("autotube.lifecycle")

# ── UTC timestamp helper ──────────────────────────────────────
def _utcnow():
    """Return current UTC datetime as naive (consistent with SQLite CURRENT_TIMESTAMP)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ── Phase timeout thresholds (minutes) ──────────────────────────
PHASE_TIMEOUTS = {
    ('video', 'scrape'): 30,
    ('video', 'script'): 60,
    ('video', 'tts'): 240,       # TTS can be slow with many scenes
    ('video', 'media'): 30,
    ('video', 'video'): 180,     # MoviePy renders can take a while
    ('video', 'metadata'): 15,
    ('video', 'upload'): 60,
    ('short', 'script'): 30,
    ('short', 'tts'): 60,
    ('short', 'media'): 30,
    ('short', 'render'): 45,
    ('short', 'upload'): 30,
    ('short', 'extract'): 20,
}

# Videos stuck in uploaded_private (not yet public) for too long
PUBLISH_DELAY_THRESHOLD_HOURS = 48  # warn if uploaded_private > 48h

# Shorts stuck without progressing
SHORT_STUCK_THRESHOLD_MIN = 60


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

def log_event(db, *,
              entity_type: str,
              entity_id: int,
              channel_id: Optional[int] = None,
              event: str = 'info',
              phase: Optional[str] = None,
              status: str = 'info',
              message: Optional[str] = None,
              metadata: Optional[dict] = None):
    """Log a lifecycle event for a video or short.

    This is the single entry point for all monitoring. Call it from
    pipeline workers, uploaders, schedulers, and generation service.

    Args:
        db: ExtendedDatabase instance
        entity_type: 'video' or 'short'
        entity_id: videos.id or shorts.id
        channel_id: channels.id (optional)
        event: event name e.g. 'phase_started', 'upload_completed'
        phase: pipeline phase e.g. 'scrape', 'script', 'tts'
        status: 'started', 'completed', 'failed', 'warning', 'info'
        message: human-readable description
        metadata: dict with extra info (duration_ms, yt_video_id, etc.)
    """
    try:
        with db._connect() as conn:
            conn.execute(
                """INSERT INTO lifecycle_events
                   (entity_type, entity_id, channel_id, event, phase, status, message, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entity_type,
                    entity_id,
                    channel_id,
                    event,
                    phase,
                    status,
                    message,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to log lifecycle event: %s", exc)


def check_all_health(db) -> dict:
    """Scan all active entities and generate/update alerts.

    Called periodically by the API background health task.
    Returns summary dict of actions taken.
    """
    created = 0
    resolved = 0

    # ── Check 1: Videos stuck in a phase ──
    created += _check_video_phase_stuck(db)

    # ── Check 2: Shorts stuck in a phase ──
    created += _check_short_stuck(db)

    # ── Check 3: Videos failed without alert ──
    created += _check_video_failed_unalerted(db)

    # ── Check 4: Shorts failed without alert ──
    created += _check_short_failed_unalerted(db)

    # ── Check 5: Videos uploaded_private too long ──
    created += _check_publish_delayed(db)

    # ── Check 6: Auto-resolve alerts for completed entities ──
    resolved += _auto_resolve_completed(db)

    logger.info(
        "Health check: %d alerts created, %d resolved",
        created, resolved,
    )
    return {"alerts_created": created, "alerts_resolved": resolved}


def create_alert(db, *,
                 entity_type: str,
                 entity_id: Optional[int] = None,
                 channel_id: Optional[int] = None,
                 alert_type: str,
                 severity: str = 'warning',
                 title: str,
                 message: Optional[str] = None,
                 metadata: Optional[dict] = None) -> Optional[int]:
    """Create a pipeline alert. Returns alert id or None if duplicate."""
    try:
        with db._connect() as conn:
            # Dedup: don't create same alert for same entity if unresolved
            existing = conn.execute(
                """SELECT id FROM pipeline_alerts
                   WHERE entity_type = ? AND entity_id = ?
                     AND alert_type = ? AND resolved = 0
                   ORDER BY created_at DESC LIMIT 1""",
                (entity_type, entity_id, alert_type),
            ).fetchone()
            if existing:
                # Update message if changed
                conn.execute(
                    "UPDATE pipeline_alerts SET message = ?, metadata_json = ? WHERE id = ?",
                    (message, json.dumps(metadata) if metadata else None, existing["id"]),
                )
                conn.commit()
                return None  # Not new

            cursor = conn.execute(
                """INSERT INTO pipeline_alerts
                   (entity_type, entity_id, channel_id, alert_type, severity, title, message, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entity_type, entity_id, channel_id,
                    alert_type, severity, title, message,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            conn.commit()
            alert_id = cursor.lastrowid
            logger.info("Alert #%d: [%s] %s (%s/%s)", alert_id, severity, title, entity_type, entity_id)
            return alert_id
    except Exception as exc:
        logger.error("Failed to create alert: %s", exc)
        return None


def acknowledge_alert(db, alert_id: int) -> bool:
    """Mark alert as acknowledged by user."""
    try:
        with db._connect() as conn:
            conn.execute(
                "UPDATE pipeline_alerts SET acknowledged = 1 WHERE id = ?",
                (alert_id,),
            )
            conn.commit()
        return True
    except Exception:
        return False


def resolve_alert(db, alert_id: int) -> bool:
    """Mark alert as resolved."""
    try:
        with db._connect() as conn:
            conn.execute(
                """UPDATE pipeline_alerts
                   SET resolved = 1, resolved_at = datetime('now')
                   WHERE id = ?""",
                (alert_id,),
            )
            conn.commit()
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# Internal health checks
# ═══════════════════════════════════════════════════════════════

def _check_video_phase_stuck(db) -> int:
    """Alert on videos stuck in generating status with no progress."""
    created = 0
    try:
        with db._connect() as conn:
            # Get videos that are in 'generating' status and haven't had
            # a lifecycle event in the last N minutes (varies by phase)
            rows = conn.execute(
                """SELECT v.id, v.canal, v.channel_id, v.progress_phase, v.progress,
                          g.id as job_id, g.last_heartbeat_at, g.pipeline_phase
                   FROM videos v
                   LEFT JOIN generation_jobs g ON g.video_id = v.id AND g.status = 'running'
                   WHERE v.status = 'generating'
                   ORDER BY v.id"""
            ).fetchall()

            for row in rows:
                phase = row["progress_phase"] or "unknown"
                entity_type = "video"
                timeout = PHASE_TIMEOUTS.get((entity_type, phase), 60)

                # Check last lifecycle event for this phase
                last_event = conn.execute(
                    """SELECT created_at, status FROM lifecycle_events
                       WHERE entity_type = 'video' AND entity_id = ?
                         AND phase = ? AND event LIKE '%started%'
                       ORDER BY created_at DESC LIMIT 1""",
                    (row["id"], phase),
                ).fetchone()

                if not last_event:
                    # No start event logged yet — check job heartbeat
                    if row["last_heartbeat_at"]:
                        try:
                            hb = datetime.strptime(row["last_heartbeat_at"], "%Y-%m-%d %H:%M:%S")
                            stuck_min = (_utcnow() - hb).total_seconds() / 60
                            if stuck_min > timeout:
                                created += _maybe_create_alert(
                                    db, conn, "video", row["id"], row["channel_id"],
                                    "stuck", "warning",
                                    f"Video #{row['id']} stuck in phase '{phase}'",
                                    f"No progress for {int(stuck_min)} min (timeout: {timeout} min)",
                                    {"phase": phase, "stuck_minutes": int(stuck_min), "job_id": row["job_id"]},
                                )
                        except (ValueError, TypeError):
                            pass
                else:
                    # Check if start was too long ago
                    try:
                        started_at = datetime.strptime(last_event["created_at"][:19], "%Y-%m-%d %H:%M:%S")
                        elapsed_min = (_utcnow() - started_at).total_seconds() / 60
                        if elapsed_min > timeout and last_event["status"] == "started":
                            created += _maybe_create_alert(
                                db, conn, "video", row["id"], row["channel_id"],
                                "stuck", "warning",
                                f"Video #{row['id']} stuck in phase '{phase}'",
                                f"Phase started {int(elapsed_min)} min ago (timeout: {timeout} min)",
                                {"phase": phase, "elapsed_minutes": int(elapsed_min)},
                            )
                    except (ValueError, TypeError):
                        pass
    except Exception as exc:
        logger.warning("Video stuck check failed: %s", exc)
    return created


def _check_short_stuck(db) -> int:
    """Alert on shorts stuck in rendering/generating."""
    created = 0
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT s.id, s.channel_id, s.status, s.type,
                          ss.scheduled_at
                   FROM shorts s
                   LEFT JOIN shorts_planned_slots ss ON ss.short_id = s.id AND ss.status = 'running'
                   WHERE s.status IN ('extracting', 'rendering', 'uploading')
                   ORDER BY s.id"""
            ).fetchall()

            for row in rows:
                status_val = row["status"]
                phase_map = {
                    "extracting": "extract",
                    "rendering": "render",
                    "uploading": "upload",
                }
                phase = phase_map.get(status_val, "render")
                timeout = PHASE_TIMEOUTS.get(("short", phase), 60)

                last_event = conn.execute(
                    """SELECT created_at, status FROM lifecycle_events
                       WHERE entity_type = 'short' AND entity_id = ?
                         AND event LIKE '%started%'
                       ORDER BY created_at DESC LIMIT 1""",
                    (row["id"],),
                ).fetchone()

                if last_event:
                    try:
                        started_at = datetime.strptime(last_event["created_at"][:19], "%Y-%m-%d %H:%M:%S")
                        elapsed_min = (_utcnow() - started_at).total_seconds() / 60
                        if elapsed_min > timeout and last_event["status"] == "started":
                            created += _maybe_create_alert(
                                db, conn, "short", row["id"], row["channel_id"],
                                "stuck", "warning",
                                f"Short #{row['id']} stuck in '{status_val}'",
                                f"Stuck for {int(elapsed_min)} min (timeout: {timeout} min)",
                                {"status": status_val, "elapsed_minutes": int(elapsed_min)},
                            )
                    except (ValueError, TypeError):
                        pass
    except Exception as exc:
        logger.warning("Short stuck check failed: %s", exc)
    return created


def _check_video_failed_unalerted(db) -> int:
    """Alert on videos with error status that don't have an alert yet."""
    created = 0
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT v.id, v.channel_id, v.canal, v.progress_phase,
                          g.error_msg, g.id as job_id
                   FROM videos v
                   LEFT JOIN generation_jobs g ON g.video_id = v.id
                   WHERE v.status = 'error'
                     AND v.id NOT IN (
                         SELECT entity_id FROM pipeline_alerts
                         WHERE entity_type = 'video' AND resolved = 0
                         AND alert_type = 'failed'
                     )
                     AND v.created_at > datetime('now', '-7 days')
                   ORDER BY v.id DESC LIMIT 20"""
            ).fetchall()

            for row in rows:
                error_msg = row["error_msg"] or "Unknown error"
                # Truncate for title
                short_msg = error_msg[:80] + "..." if len(error_msg) > 80 else error_msg
                created += _maybe_create_alert(
                    db, conn, "video", row["id"], row["channel_id"],
                    "failed", "critical",
                    f"Video #{row['id']} failed: {short_msg}",
                    error_msg,
                    {"phase": row["progress_phase"], "job_id": row["job_id"]},
                )
    except Exception as exc:
        logger.warning("Video failed check failed: %s", exc)
    return created


def _check_short_failed_unalerted(db) -> int:
    """Alert on failed shorts without alerts."""
    created = 0
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT s.id, s.channel_id, s.type, s.error_message
                   FROM shorts s
                   WHERE s.status = 'failed'
                     AND s.id NOT IN (
                         SELECT entity_id FROM pipeline_alerts
                         WHERE entity_type = 'short' AND resolved = 0
                         AND alert_type = 'failed'
                     )
                     AND s.created_at > datetime('now', '-7 days')
                   ORDER BY s.id DESC LIMIT 20"""
            ).fetchall()

            for row in rows:
                error_msg = row["error_message"] or "Unknown error"
                short_msg = error_msg[:80] + "..." if len(error_msg) > 80 else error_msg
                created += _maybe_create_alert(
                    db, conn, "short", row["id"], row["channel_id"],
                    "failed", "critical",
                    f"Short #{row['id']} ({row['type']}) failed: {short_msg}",
                    error_msg,
                    {"type": row["type"]},
                )
    except Exception as exc:
        logger.warning("Short failed check failed: %s", exc)
    return created


def _check_publish_delayed(db) -> int:
    """Warn about videos stuck in uploaded_private state too long."""
    created = 0
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT v.id, v.channel_id, v.titulo_final, v.uploaded_at, v.target_public_at
                   FROM videos v
                   WHERE v.status = 'uploaded_private'
                     AND v.uploaded_at < datetime('now', ?)
                     AND v.id NOT IN (
                         SELECT entity_id FROM pipeline_alerts
                         WHERE entity_type = 'video' AND resolved = 0
                         AND alert_type = 'publish_delayed'
                     )
                   ORDER BY v.uploaded_at""",
                (f'-{PUBLISH_DELAY_THRESHOLD_HOURS} hours',),
            ).fetchall()

            for row in rows:
                title = row["titulo_final"] or f"Video #{row['id']}"
                created += _maybe_create_alert(
                    db, conn, "video", row["id"], row["channel_id"],
                    "publish_delayed", "warning",
                    f"Video '{title[:60]}' no se ha publicado",
                    f"Subido como privado hace >{PUBLISH_DELAY_THRESHOLD_HOURS}h. Target: {row['target_public_at'] or 'N/A'}",
                    {"uploaded_at": row["uploaded_at"], "target_public_at": row["target_public_at"]},
                )
    except Exception as exc:
        logger.warning("Publish delayed check failed: %s", exc)
    return created


def _auto_resolve_completed(db) -> int:
    """Auto-resolve alerts for entities that have since completed successfully."""
    resolved = 0
    try:
        with db._connect() as conn:
            # Resolve alerts for videos that are now uploaded/published
            alerts = conn.execute(
                """SELECT pa.id, pa.entity_id, pa.alert_type
                   FROM pipeline_alerts pa
                   JOIN videos v ON v.id = pa.entity_id AND pa.entity_type = 'video'
                   WHERE pa.resolved = 0
                     AND v.status IN ('uploaded', 'ready')
                     AND pa.alert_type IN ('stuck', 'timeout')"""
            ).fetchall()

            for alert in alerts:
                conn.execute(
                    """UPDATE pipeline_alerts
                       SET resolved = 1, resolved_at = datetime('now'),
                           message = message || ' [Auto-resolved: video completed]'
                       WHERE id = ?""",
                    (alert["id"],),
                )
                resolved += 1

            # Resolve alerts for shorts that are now published
            alerts = conn.execute(
                """SELECT pa.id, pa.entity_id, pa.alert_type
                   FROM pipeline_alerts pa
                   JOIN shorts s ON s.id = pa.entity_id AND pa.entity_type = 'short'
                   WHERE pa.resolved = 0
                     AND s.status = 'published'
                     AND pa.alert_type IN ('stuck', 'timeout')"""
            ).fetchall()

            for alert in alerts:
                conn.execute(
                    """UPDATE pipeline_alerts
                       SET resolved = 1, resolved_at = datetime('now'),
                           message = message || ' [Auto-resolved: short published]'
                       WHERE id = ?""",
                    (alert["id"],),
                )
                resolved += 1

            conn.commit()
    except Exception as exc:
        logger.warning("Auto-resolve failed: %s", exc)
    return resolved


def _maybe_create_alert(db, conn, entity_type, entity_id, channel_id,
                        alert_type, severity, title, message, metadata) -> int:
    """Create alert if not already existing for this entity+type. Returns 1 if created, 0 if dup."""
    existing = conn.execute(
        """SELECT id FROM pipeline_alerts
           WHERE entity_type = ? AND entity_id = ? AND alert_type = ? AND resolved = 0
           LIMIT 1""",
        (entity_type, entity_id, alert_type),
    ).fetchone()
    if existing:
        # Update the existing alert
        conn.execute(
            "UPDATE pipeline_alerts SET message = ?, metadata_json = ? WHERE id = ?",
            (message, json.dumps(metadata) if metadata else None, existing["id"]),
        )
        return 0

    conn.execute(
        """INSERT INTO pipeline_alerts
           (entity_type, entity_id, channel_id, alert_type, severity, title, message, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (entity_type, entity_id, channel_id, alert_type, severity, title, message,
         json.dumps(metadata) if metadata else None),
    )
    return 1


# ═══════════════════════════════════════════════════════════════
# Convenience helper for pipeline workers
# ═══════════════════════════════════════════════════════════════

def log_phase_start(db, entity_type: str, entity_id: int, phase: str,
                    channel_id: Optional[int] = None, message: Optional[str] = None):
    """Shorthand for logging phase start."""
    log_event(db,
              entity_type=entity_type, entity_id=entity_id, channel_id=channel_id,
              event=f'{phase}_started', phase=phase, status='started',
              message=message or f'{phase.title()} phase started')


def log_phase_end(db, entity_type: str, entity_id: int, phase: str,
                  channel_id: Optional[int] = None, message: Optional[str] = None,
                  duration_ms: Optional[int] = None, extra_meta: Optional[dict] = None):
    """Shorthand for logging phase completion."""
    meta = extra_meta or {}
    if duration_ms is not None:
        meta['duration_ms'] = duration_ms
    log_event(db,
              entity_type=entity_type, entity_id=entity_id, channel_id=channel_id,
              event=f'{phase}_completed', phase=phase, status='completed',
              message=message or f'{phase.title()} phase completed',
              metadata=meta if meta else None)


def log_phase_error(db, entity_type: str, entity_id: int, phase: str,
                    error: str, channel_id: Optional[int] = None,
                    extra_meta: Optional[dict] = None):
    """Shorthand for logging phase failure."""
    meta = extra_meta or {}
    meta['error'] = error
    log_event(db,
              entity_type=entity_type, entity_id=entity_id, channel_id=channel_id,
              event=f'{phase}_failed', phase=phase, status='failed',
              message=error,
              metadata=meta)
