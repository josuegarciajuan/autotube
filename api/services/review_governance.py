"""Opt-in review governance for newly uploaded long-form videos.

This module is intentionally observational: it never edits slots, privacy, or
canonical video status.  Activation is controlled by channel config_json plus
the per-channel system_state activation timestamp.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

REVIEW_OFFSETS = (("d2", 2), ("d7", 7), ("d14", 14), ("d21", 21), ("d30", 30))
CRITICAL_REVIEW_ALERTS = {"review_visibility_mismatch", "review_exact_duplicate"}


def _parse(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def activation_at(db, channel_id: int, slug: str, config) -> datetime | None:
    if not bool(getattr(config, "REVIEW_GOVERNANCE_ENABLED", False)):
        return None
    value = db.get_system_state(f"review_governance_activation_at_{slug}")
    return _parse(value)


def is_newly_activated_video(db, video: dict, config) -> bool:
    channel_id = video.get("channel_id")
    if not channel_id:
        return False
    slug = video.get("canal") or video.get("channel_slug")
    if not slug:
        with db._connect() as conn:
            row = conn.execute("SELECT slug FROM channels WHERE id=?", (channel_id,)).fetchone()
        slug = row["slug"] if row else ""
    active = activation_at(db, int(channel_id), slug, config)
    created = _parse(video.get("created_at"))
    return bool(active and created and created >= active)


def schedule_review_tasks(db, video_id: int, channel_id: int, uploaded_at=None) -> int:
    base = _parse(uploaded_at) or datetime.now(timezone.utc)
    created = 0
    with db._connect() as conn:
        for kind, days in REVIEW_OFFSETS:
            cur = conn.execute(
                """INSERT OR IGNORE INTO video_review_tasks
                   (video_id, channel_id, review_kind, due_at)
                   VALUES (?, ?, ?, ?)""",
                (video_id, channel_id, kind, (base + timedelta(days=days)).isoformat()),
            )
            created += cur.rowcount
        conn.commit()
    return created


def _emit(db, video, alert_type, severity, title, message, metadata=None):
    from api.services.lifecycle_monitor import emit_alert
    return emit_alert(db, entity_type="video", entity_id=video["id"],
                      channel_id=video.get("channel_id"), alert_type=alert_type,
                      severity=severity, title=title, message=message,
                      metadata=metadata or {})


def process_due_reviews(db, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    result = {"processed": 0, "alerts": 0}
    with db._connect() as conn:
        rows = conn.execute(
            """SELECT r.*, v.yt_video_id, v.status, v.privacy_status,
                      v.yt_visibility, v.yt_checked_at, v.titulo_final
               FROM video_review_tasks r JOIN videos v ON v.id=r.video_id
               WHERE r.status='pending' AND r.due_at <= ? ORDER BY r.due_at LIMIT 100""",
            (now.isoformat(),),
        ).fetchall()
    for row in rows:
        video = dict(row)
        # External state is populated by the quota-free reconciler. Missing is
        # distinct from zero/unknown and always becomes a manual review alert.
        if not video.get("yt_visibility"):
            result["alerts"] += bool(_emit(
                db, video, "review_visibility_missing", "warning",
                f"Revisión {video['review_kind']}: estado externo ausente",
                "No hay estado externo cacheado; revisar manualmente en Studio.",
            ))
        elif video["yt_visibility"] != (video.get("privacy_status") or "") and video.get("privacy_status"):
            result["alerts"] += bool(_emit(
                db, video, "review_visibility_mismatch", "critical",
                f"Revisión {video['review_kind']}: discrepancia de visibilidad",
                f"BD={video.get('privacy_status')} externo={video.get('yt_visibility')}; no se cambia automáticamente.",
                {"db_privacy": video.get("privacy_status"), "external": video.get("yt_visibility")},
            ))
        with db._connect() as conn:
            conn.execute("UPDATE video_review_tasks SET status='completed', completed_at=datetime('now') WHERE id=?", (row["id"],))
            conn.commit()
        result["processed"] += 1
    return result


def _thumbnail_digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, TypeError):
        return ""


def find_exact_duplicate_groups(db, channel_id=None):
    """Return only groups with an exact non-empty metadata+thumbnail key."""
    with db._connect() as conn:
        rows = conn.execute(
            """SELECT id, channel_id, yt_video_id, titulo_final, description,
                      tags_json, thumbnail_path, privacy_status, status
               FROM videos WHERE yt_video_id IS NOT NULL AND yt_video_id != ''
                 AND (? IS NULL OR channel_id=?) ORDER BY id""",
            (channel_id, channel_id),
        ).fetchall()
    groups = {}
    for raw in rows:
        row = dict(raw)
        thumb = _thumbnail_digest(row.get("thumbnail_path"))
        title = (row.get("titulo_final") or "").strip()
        desc = (row.get("description") or "").strip()
        tags = row.get("tags_json") or ""
        if not (title and desc and tags and thumb):
            continue
        key = hashlib.sha256(json.dumps([title, desc, tags, thumb], ensure_ascii=False).encode()).hexdigest()
        groups.setdefault(key, []).append(row)
    return [items for items in groups.values() if len(items) > 1]


def privatize_exact_duplicate(db, keep_video_id: int, duplicate_video_id: int, confirm: str) -> dict:
    if confirm != "PRIVATIZAR_DUPLICADO_EXACTO":
        raise ValueError("Explicit confirmation required")
    groups = find_exact_duplicate_groups(db)
    group = next((g for g in groups if any(v["id"] == duplicate_video_id for v in g)), None)
    if not group or not any(v["id"] == keep_video_id for v in group):
        raise ValueError("Videos are not an exact confirmed duplicate pair")
    duplicate = next(v for v in group if v["id"] == duplicate_video_id)
    keep = next(v for v in group if v["id"] == keep_video_id)
    if duplicate.get("yt_video_id") == keep.get("yt_video_id"):
        raise ValueError("Cannot privatize the same YouTube video")
    with db._connect() as conn:
        def views(video_id):
            row = conn.execute("SELECT MAX(views) AS views FROM video_stats_history WHERE video_id=?", (video_id,)).fetchone()
            return row["views"] if row and row["views"] is not None else None
        keep_views, duplicate_views = views(keep_video_id), views(duplicate_video_id)
    if keep_views is None or duplicate_views is None or duplicate_views >= keep_views:
        raise ValueError("Only the exact duplicate with fewer confirmed views may be privatized")
    from pipeline.youtube_uploader import YouTubeUploader
    # This is the only mutating path and is never called by audit/monitor.
    with db._connect() as conn:
        row = conn.execute("SELECT google_account FROM channels WHERE id=?", (duplicate["channel_id"],)).fetchone()
    uploader = YouTubeUploader(account_name=(row["google_account"] if row and row["google_account"] else "default"),
                               db=db, channel_slug=_channel_slug(db, duplicate["channel_id"]))
    uploader.set_privacy(duplicate["yt_video_id"], "private")
    return {"ok": True, "kept": keep_video_id, "privatized": duplicate_video_id}


def _channel_slug(db, channel_id):
    with db._connect() as conn:
        row = conn.execute("SELECT slug FROM channels WHERE id=?", (channel_id,)).fetchone()
    if not row:
        raise ValueError("Channel not found")
    return row["slug"]
