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

# Cooldown: don't re-create alerts that were recently resolved by the user.
# After bulk-resolving alerts, the health check will suppress recreation
# for this many hours. After cooldown expires, stale alerts will reappear.
ALERT_RESOLVE_COOLDOWN_HOURS = 24

# Failed-video error messages that are transient/intentional and should NOT
# generate a critical alert (case-insensitive substring match against
# generation_jobs.error_msg). E.g. "Aborted by re-test" (A/B re-run) and
# "Server restarted — old process no longer exists" (video gets re-queued).
TRANSIENT_SKIP_PATTERNS = (
    "re-test",
    "server restarted",
    "old process no longer exists",
)

# Failed-video error messages that indicate an environment/resource problem
# (OOM, memory guard) rather than a content pipeline failure. These are still
# actionable but demoted from critical to warning to avoid alert fatigue.
ENV_DEMOTE_PATTERNS = (
    "ram insuficiente",
    "oom",
    "memory guard",
)


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

    # ── Check 5.5: A/B tests stuck in rotation phase (v31) ──
    created += _check_ab_test_stuck(db)

    # ── Check 6: Auto-resolve alerts for completed entities ──
    resolved += _auto_resolve_completed(db)

    # ── Check 7: Platform auth/token failures (Facebook, Rumble) ──
    created += _check_platform_auth_errors(db)

    # ── Check 8: Auto-resolve platform token alerts when uploads succeed ──
    resolved += _auto_resolve_platform_tokens(db)

    # ── Check 9: LLM credits low/exhausted ──
    created += _check_llm_credits(db)

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
            # ── Cooldown: suppress alert if recently resolved by user ──
            recently_resolved = conn.execute(
                """SELECT id FROM pipeline_alerts
                   WHERE entity_type = ? AND entity_id IS ? AND alert_type = ?
                     AND resolved = 1
                     AND resolved_at > datetime('now', ?)
                   LIMIT 1""",
                (entity_type, entity_id, alert_type,
                 f'-{ALERT_RESOLVE_COOLDOWN_HOURS} hours'),
            ).fetchone()
            if recently_resolved:
                return None

            # Dedup: don't create same alert for same entity if unresolved
            existing = conn.execute(
                """SELECT id FROM pipeline_alerts
                   WHERE entity_type = ? AND entity_id IS ?
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


def resolve_all_alerts(db, severity: Optional[str] = None) -> int:
    """Bulk-resolve unresolved alerts, optionally filtered by severity.
    Returns the number of rows updated."""
    try:
        with db._connect() as conn:
            if severity:
                sql = """UPDATE pipeline_alerts
                         SET resolved = 1, resolved_at = datetime('now'),
                             acknowledged = 1
                         WHERE resolved = 0 AND severity = ?"""
                cur = conn.execute(sql, (severity,))
            else:
                sql = """UPDATE pipeline_alerts
                         SET resolved = 1, resolved_at = datetime('now'),
                             acknowledged = 1
                         WHERE resolved = 0"""
                cur = conn.execute(sql)
            conn.commit()
            return cur.rowcount
    except Exception:
        return 0


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
            # Join the LATEST failed generation_jobs row per video. A video can
            # accumulate many failed job rows (retries, restarts); a plain LEFT
            # JOIN picks an arbitrary row and produces misleading alert messages
            # (e.g. "Unknown error" when the real cause was an OOM abort).
            rows = conn.execute(
                """SELECT v.id, v.channel_id, v.canal, v.progress_phase,
                          g.error_msg, g.id as job_id
                   FROM videos v
                   JOIN generation_jobs g ON g.video_id = v.id
                     AND g.id = (
                         SELECT MAX(g2.id) FROM generation_jobs g2
                         WHERE g2.video_id = v.id AND g2.status = 'failed'
                     )
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
                error_lower = error_msg.lower()

                # Skip transient/intentional failures (no alert).
                if any(p in error_lower for p in TRANSIENT_SKIP_PATTERNS):
                    continue

                # Demote environment/resource failures to warning.
                severity = "critical"
                if any(p in error_lower for p in ENV_DEMOTE_PATTERNS):
                    severity = "warning"

                first_line = error_msg.split('\n')[0].strip()
                short_msg = first_line[:100] + "..." if len(first_line) > 100 else first_line
                created += _maybe_create_alert(
                    db, conn, "video", row["id"], row["channel_id"],
                    "failed", severity,
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
                     -- v26: Only alert if target_public_at has already passed.
                     -- Videos with future targets are correctly waiting for YouTube's
                     -- native scheduledPublishTime to fire; these are NOT stuck.
                     AND (v.target_public_at IS NULL
                          OR v.target_public_at < datetime('now'))
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


def _check_ab_test_stuck(db) -> int:
    """Alert on A/B tests stuck in rotation phase >72h without second check.
    
    Detects videos that had their title or thumbnail changed via A/B testing
    but haven't progressed to the second check phase within 72 hours. This
    usually indicates the ABTestWorker is not running or has crashed.
    """
    created = 0
    try:
        with db._connect() as conn:
            rows = conn.execute("""
                SELECT vab.id, vab.video_id, vab.channel_id, vab.phase,
                       vab.yt_video_id, vab.title_rotated_at, vab.thumbnail_rotated_at,
                       v.titulo_final, ch.slug as channel_slug
                FROM video_ab_tests vab
                JOIN videos v ON vab.video_id = v.id
                JOIN channels ch ON vab.channel_id = ch.id
                WHERE vab.phase IN ('title_rotated', 'thumbnail_rotated')
                  AND (
                    (vab.phase = 'title_rotated' AND vab.title_rotated_at IS NOT NULL AND vab.title_rotated_at < datetime('now', '-72 hours'))
                    OR
                    (vab.phase = 'thumbnail_rotated' AND vab.thumbnail_rotated_at IS NOT NULL AND vab.thumbnail_rotated_at < datetime('now', '-72 hours'))
                  )
            """).fetchall()
            
            for row in rows:
                row_dict = dict(row)
                video_id = row_dict["video_id"]
                channel_id = row_dict["channel_id"]
                phase = row_dict["phase"]
                yt_id = row_dict.get("yt_video_id", "?")
                rotated_at = row_dict.get("title_rotated_at") or row_dict.get("thumbnail_rotated_at") or "?"
                what_changed = "título" if phase == "title_rotated" else "miniatura"
                
                create_alert(
                    db,
                    entity_type='video',
                    entity_id=video_id,
                    channel_id=channel_id,
                    alert_type='ab_test_stuck',
                    severity='warning',
                    title=f'A/B test stuck en {phase} para video {video_id}',
                    message=(
                        f'El test del video {video_id} (YT: {yt_id}) '
                        f'lleva >72h en fase {phase} desde {rotated_at}. '
                        f'Se cambió el {what_changed} pero nunca se ejecutó el second check. '
                        f'Verificar que ENABLE_AB_TESTING=true y que el scheduler está corriendo.'
                    ),
                )
                created += 1
    except Exception as exc:
        logger.warning("AB test stuck check failed: %s", exc)
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
                      AND v.status IN ('uploaded', 'ready', 'awaiting_upload')
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

            # Resolve publish_delayed alerts for videos that are now public.
            # These alerts were created while the video was stuck in
            # uploaded_private, but the video later published (natively via
            # YouTube scheduledPublishTime or a manual resume). Without this,
            # they linger unresolved forever.
            alerts = conn.execute(
                """SELECT pa.id, pa.entity_id, pa.alert_type
                   FROM pipeline_alerts pa
                   JOIN videos v ON v.id = pa.entity_id AND pa.entity_type = 'video'
                   WHERE pa.resolved = 0
                     AND v.status IN ('published', 'uploaded', 'ready')
                     AND pa.alert_type = 'publish_delayed'"""
            ).fetchall()

            for alert in alerts:
                conn.execute(
                    """UPDATE pipeline_alerts
                       SET resolved = 1, resolved_at = datetime('now'),
                           message = message || ' [Auto-resolved: video published]'
                       WHERE id = ?""",
                    (alert["id"],),
                )
                resolved += 1

            # Resolve publish_not_detected alerts for videos that did finally
            # go public (e.g. a previously-unlisted orphan that was published
            # manually). These are only resolved once status = 'published';
            # an 'uploaded'/unlisted video is still genuinely unconfirmed.
            alerts = conn.execute(
                """SELECT pa.id, pa.entity_id, pa.alert_type
                   FROM pipeline_alerts pa
                   JOIN videos v ON v.id = pa.entity_id AND pa.entity_type = 'video'
                   WHERE pa.resolved = 0
                     AND v.status = 'published'
                     AND pa.alert_type = 'publish_not_detected'"""
            ).fetchall()

            for alert in alerts:
                conn.execute(
                    """UPDATE pipeline_alerts
                       SET resolved = 1, resolved_at = datetime('now'),
                           message = message || ' [Auto-resolved: video published]'
                       WHERE id = ?""",
                    (alert["id"],),
                )
                resolved += 1

            conn.commit()
    except Exception as exc:
        logger.warning("Auto-resolve failed: %s", exc)
    return resolved


def _check_platform_auth_errors(db) -> int:
    """Alert when cross-platform uploads fail with authentication errors.

    Scans platform_videos for recent failures matching token expiration,
    invalid credentials, or permission-denied patterns (HTTP 401/403).
    Creates one alert per channel+platform combination via system alerts
    with entity_id = channel_id for per-channel dedup.
    """
    created = 0
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT pv.channel_id, pv.platform, pv.error_message,
                          pv.created_at, ch.name as channel_name, ch.slug
                   FROM platform_videos pv
                   JOIN channels ch ON ch.id = pv.channel_id
                   WHERE pv.status = 'failed'
                     AND (
                         pv.error_message LIKE '%token%'
                         OR pv.error_message LIKE '%401%'
                         OR pv.error_message LIKE '%403%'
                         OR pv.error_message LIKE '%auth%'
                         OR pv.error_message LIKE '%expired%'
                         OR pv.error_message LIKE '%permission%'
                         OR pv.error_message LIKE '%invalid%'
                         OR pv.error_message LIKE '%Not authenticated%'
                         OR pv.error_message LIKE '%credentials%'
                     )
                     AND pv.created_at > datetime('now', '-14 days')
                   ORDER BY pv.created_at DESC
                """
            ).fetchall()

            for row in rows:
                alert_type = f"platform_token_expired_{row['platform']}"
                channel_id = row["channel_id"]

                # Dedup: already an unresolved alert for this channel+platform?
                existing = conn.execute(
                    """SELECT id FROM pipeline_alerts
                       WHERE entity_type = 'system'
                         AND entity_id = ?
                         AND alert_type = ?
                         AND resolved = 0
                       LIMIT 1""",
                    (channel_id, alert_type),
                ).fetchone()
                if existing:
                    # Update message to reflect latest error
                    conn.execute(
                        "UPDATE pipeline_alerts SET message = ? WHERE id = ?",
                        (_build_platform_token_message(row), existing["id"]),
                    )
                    continue

                # Was there a successful upload AFTER this failure?
                success = conn.execute(
                    """SELECT id FROM platform_videos
                       WHERE channel_id = ? AND platform = ?
                         AND status = 'published'
                         AND uploaded_at > ?
                       LIMIT 1""",
                    (channel_id, row["platform"], row["created_at"]),
                ).fetchone()
                if success:
                    continue  # already fixed

                # Create alert
                platform_label = row["platform"].title()
                ch_name = row["channel_name"] or "?"
                title = f"Token de {platform_label} expirado/revocado — {ch_name}"
                message = _build_platform_token_message(row)
                metadata = {
                    "platform": row["platform"],
                    "channel_slug": row["slug"],
                    "last_error": row["error_message"][:500] if row["error_message"] else "",
                }

                created += _maybe_create_alert(
                    db, conn,
                    entity_type="system",
                    entity_id=channel_id,
                    channel_id=channel_id,
                    alert_type=alert_type,
                    severity="critical",
                    title=title,
                    message=message,
                    metadata=metadata,
                )

            conn.commit()
    except Exception as exc:
        logger.warning("Platform auth check failed: %s", exc)
    return created


def _build_platform_token_message(row: dict) -> str:
    """Build a human-readable alert message for a platform auth failure."""
    platform = row.get("platform", "?").title()
    ch_name = row.get("channel_name", "?")
    error = (row.get("error_message") or "Error de autenticación desconocido")[:300]
    ch_id = row.get("channel_id", 0)

    return (
        f"El último intento de publicar en {platform} para el canal "
        f"«{ch_name}» falló con un error de autenticación.\n\n"
        f"Error: {error}\n\n"
        f"🔧 Acción requerida: renovar el token/API key en "
        f"Canales → {ch_name} → Cuentas Sociales → {platform}.\n"
        f"Luego probar la conexión con el botón «Probar».\n\n"
        f"Las siguientes generaciones de video NO se publicarán "
        f"en {platform} hasta que el token sea renovado y validado."
    )


def _auto_resolve_platform_tokens(db) -> int:
    """Auto-resolve platform token alerts when a subsequent upload succeeds.

    If a cross-platform upload publishes successfully AFTER the alert
    was created, the token issue has been fixed and we auto-resolve.
    """
    resolved = 0
    try:
        with db._connect() as conn:
            alerts = conn.execute(
                """SELECT id, channel_id, alert_type, created_at
                   FROM pipeline_alerts
                   WHERE alert_type LIKE 'platform_token_expired_%'
                     AND resolved = 0
                """
            ).fetchall()

            for alert in alerts:
                alert_type = alert["alert_type"]
                platform = alert_type.replace("platform_token_expired_", "")
                channel_id = alert["channel_id"]

                # Check for successful upload after alert was created
                success = conn.execute(
                    """SELECT id FROM platform_videos
                       WHERE channel_id = ? AND platform = ?
                         AND status = 'published'
                         AND uploaded_at > ?
                       LIMIT 1""",
                    (channel_id, platform, alert["created_at"]),
                ).fetchone()

                if success:
                    conn.execute(
                        """UPDATE pipeline_alerts
                           SET resolved = 1, resolved_at = datetime('now'),
                               message = message || '\n[Auto-resuelto: nuevo upload exitoso en ' || ? || ']'
                           WHERE id = ?""",
                        (platform, alert["id"]),
                    )
                    resolved += 1
                    logger.info(
                        "Auto-resolved %s alert #%d (channel %s published to %s)",
                        alert_type, alert["id"], channel_id, platform,
                    )

            conn.commit()
    except Exception as exc:
        logger.warning("Platform token auto-resolve failed: %s", exc)
    return resolved


def _maybe_create_alert(db, conn, entity_type, entity_id, channel_id,
                        alert_type, severity, title, message, metadata) -> int:
    """Create alert if not already existing for this entity+type. Returns 1 if created, 0 if dup."""
    # ── Cooldown: suppress alert if recently resolved by user ──
    recently_resolved = conn.execute(
        """SELECT id FROM pipeline_alerts
           WHERE entity_type = ? AND entity_id IS ? AND alert_type = ?
             AND resolved = 1
             AND resolved_at > datetime('now', ?)
           LIMIT 1""",
        (entity_type, entity_id, alert_type,
         f'-{ALERT_RESOLVE_COOLDOWN_HOURS} hours'),
    ).fetchone()
    if recently_resolved:
        return 0

    # Dedup: don't create same alert for same entity if unresolved
    existing = conn.execute(
        """SELECT id FROM pipeline_alerts
           WHERE entity_type = ? AND entity_id IS ? AND alert_type = ? AND resolved = 0
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
# Check 9: LLM credits low/exhausted
# ═══════════════════════════════════════════════════════════════

def _check_llm_credits(db) -> int:
    """Check DeepSeek balance and OpenAI quota errors every 12h.
    
    Creates alerts when:
    - DeepSeek balance < threshold (llm_credit_low, severity: warning)
    - DeepSeek balance = 0 or error (llm_credit_exhausted, severity: critical)
    - OpenAI errors detected (llm_credit_exhausted, severity: critical)
    
    Returns number of alerts created.
    """
    created = 0
    try:
        from api.services.llm_credit_checker import check_all_llm_credits
        status = check_all_llm_credits(db)
    except Exception as exc:
        logger.warning("LLM credit check failed: %s", exc)
        return 0

    try:
        with db._connect() as conn:
            # ── DeepSeek ──
            ds = status.get("deepseek")
            if ds:
                ds_status = ds.get("status", "unknown")
                if ds_status == "exhausted":
                    existing = conn.execute(
                        """SELECT id FROM pipeline_alerts
                           WHERE entity_type = 'system' AND alert_type = 'llm_credit_exhausted'
                             AND title LIKE '%DeepSeek%' AND resolved = 0 LIMIT 1"""
                    ).fetchone()
                    if not existing:
                        balance = ds.get("balance_usd", 0)
                        created += _maybe_create_alert(
                            db, conn,
                            entity_type="system", entity_id=0,
                            alert_type="llm_credit_exhausted",
                            severity="critical",
                            title="DeepSeek sin créditos — generación de scripts detenida",
                            message=(
                                f"La cuenta de DeepSeek se ha quedado SIN créditos.\n\n"
                                f"Saldo actual: ${balance:.2f} USD.\n\n"
                                f"🔧 Acción requerida: recargar créditos en platform.deepseek.com.\n"
                                f"Hasta entonces, las generaciones de video FALLARÁN "
                                f"(DeepSeek es el proveedor principal de scripts)."
                            ),
                            metadata={"provider": "deepseek", "balance_usd": balance},
                        )
                elif ds_status == "low":
                    existing = conn.execute(
                        """SELECT id FROM pipeline_alerts
                           WHERE entity_type = 'system' AND alert_type = 'llm_credit_low'
                             AND title LIKE '%DeepSeek%' AND resolved = 0 LIMIT 1"""
                    ).fetchone()
                    if not existing:
                        balance = ds.get("balance_usd", 0)
                        created += _maybe_create_alert(
                            db, conn,
                            entity_type="system", entity_id=0,
                            alert_type="llm_credit_low",
                            severity="warning",
                            title="DeepSeek créditos bajos — recargar pronto",
                            message=(
                                f"La cuenta de DeepSeek tiene créditos bajos.\n\n"
                                f"Saldo actual: ${balance:.2f} USD "
                                f"(umbral de aviso: $2.00 USD).\n\n"
                                f"🔧 Acción recomendada: recargar créditos en "
                                f"platform.deepseek.com para evitar interrupción "
                                f"de las generaciones automáticas."
                            ),
                            metadata={"provider": "deepseek", "balance_usd": balance},
                        )

            # ── OpenAI ──
            oa = status.get("openai")
            if oa:
                oa_status = oa.get("status", "unknown")
                if oa_status == "exhausted":
                    existing = conn.execute(
                        """SELECT id FROM pipeline_alerts
                           WHERE entity_type = 'system' AND alert_type = 'llm_credit_exhausted'
                             AND title LIKE '%OpenAI%' AND resolved = 0 LIMIT 1"""
                    ).fetchone()
                    if not existing:
                        err_count = oa.get("error_count_7d", 0)
                        last_err = oa.get("last_error", "")[:300]
                        created += _maybe_create_alert(
                            db, conn,
                            entity_type="system", entity_id=0,
                            alert_type="llm_credit_exhausted",
                            severity="critical",
                            title="OpenAI sin créditos/quota — fallback de scripts caído",
                            message=(
                                f"La API de OpenAI está devolviendo errores de cuota/creditos.\n\n"
                                f"Errores detectados (7d): {err_count}\n"
                                f"Último error: {last_err}\n\n"
                                f"🔧 Acción requerida: verificar billing en "
                                f"platform.openai.com o añadir método de pago.\n"
                                f"Sin OpenAI, el fallback automático (DeepSeek → OpenAI) "
                                f"no funcionará."
                            ),
                            metadata={"provider": "openai", "error_count_7d": err_count},
                        )

            conn.commit()
    except Exception as exc:
        logger.warning("LLM credit alert check failed: %s", exc)

    return created


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
