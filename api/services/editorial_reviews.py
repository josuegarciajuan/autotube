"""Quota-free, non-mutating editorial reviews for published long-form videos.

Reviews are deliberately small: durable checkpoints, visible alerts, and a
read-only integrity/performance report.  They never delete, hide, retitle, or
otherwise change a historical video.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.config_bridge import get_channel_config
from api.services.lifecycle_monitor import emit_alert

logger = logging.getLogger("autotube.editorial_reviews")

REVIEW_KINDS = {"48h": timedelta(hours=48), "7d": timedelta(days=7), "14d": timedelta(days=14)}
DEFAULT_RULES = {
    "enabled": False,
    "max_attempts": 3,
    "retry_minutes": 60,
    "min_impressions": 100,
    "min_ctr_percent": 4.0,
    "min_retention_percent": 35.0,
    "stats_max_age_days": 7,
}


def ensure_review_schema(db) -> None:
    with db._connect() as conn:
        conn.executescript((Path(__file__).resolve().parents[2] / "database" / "schema_v50.sql").read_text())
        conn.commit()


def review_config(slug: str) -> dict:
    raw = getattr(get_channel_config(slug), "EDITORIAL_RECOVERY_REVIEW", {}) or {}
    return {**DEFAULT_RULES, **raw}


def validate_new_video(slug: str, topic: str, title: str, thumbnail_path: str) -> dict:
    """Validate new packaging without changing any existing record."""
    rules = review_config(slug)
    failures = []
    for term in rules.get("blocked_terms", []):
        if term.lower() in f"{topic} {title}".lower():
            failures.append(f"término bloqueado: {term}")
    required = rules.get("required_title_keywords", [])
    if required and not any(word.lower() in title.lower() for word in required):
        failures.append("el título no contiene una keyword editorial requerida")
    if rules.get("require_thumbnail", False) and (not thumbnail_path or not Path(thumbnail_path).is_file()):
        failures.append("miniatura ausente o no accesible")
    return {"allowed": not failures, "reasons": failures}


def _parse_time(value, fallback=None):
    if not value:
        return fallback or datetime.now(timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return fallback or datetime.now(timezone.utc)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def schedule_video_reviews(db, video_id: int, now=None) -> int:
    """Create the three checkpoints for one newly uploaded video, idempotently."""
    ensure_review_schema(db)
    now = now or datetime.now(timezone.utc)
    with db._connect() as conn:
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not video:
            return 0
        channel = conn.execute("SELECT slug FROM channels WHERE id = ?", (video["channel_id"],)).fetchone()
        if not channel or not review_config(channel["slug"]).get("enabled"):
            return 0
        base = _parse_time(video["published_at"] or video["uploaded_at"] or video["created_at"], now)
        created = 0
        rules = review_config(channel["slug"])
        for kind, delay in REVIEW_KINDS.items():
            cur = conn.execute(
                """INSERT OR IGNORE INTO editorial_reviews
                   (video_id, channel_id, review_kind, status, due_at)
                   VALUES (?, ?, ?, 'scheduled', ?)""",
                (video_id, video["channel_id"], kind, (base + delay).astimezone(timezone.utc).isoformat()),
            )
            if cur.rowcount:
                conn.execute("UPDATE editorial_reviews SET max_attempts=? WHERE id=?",
                             (int(rules["max_attempts"]), cur.lastrowid))
            created += cur.rowcount
        conn.commit()
        return created


def schedule_daily_audits(db, now=None) -> int:
    """Schedule one channel audit per enabled channel for the current UTC day."""
    ensure_review_schema(db)
    now = now or datetime.now(timezone.utc)
    day = now.date().isoformat()
    created = 0
    with db._connect() as conn:
        for channel in conn.execute("SELECT id, slug FROM channels").fetchall():
            if not review_config(channel["slug"]).get("enabled"):
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO editorial_reviews
                   (channel_id, review_kind, review_date, status, due_at)
                   VALUES (?, 'daily', ?, 'scheduled', ?)""",
                (channel["id"], day, now.isoformat()),
            )
            created += cur.rowcount
        conn.commit()
    return created


def classify_performance(stats: dict, cfg) -> dict:
    rules = {**DEFAULT_RULES, **(getattr(cfg, "EDITORIAL_RECOVERY_REVIEW", {}) or {})}
    if not stats:
        return {"classification": "insufficient_data_manual_collection", "reason": "No hay stats recientes; solicitar recolección manual."}
    impressions = int(stats.get("impressions", 0) or 0)
    if impressions <= 0:
        return {"classification": "no_impressions", "reason": "Sin impresiones; no concluir sobre CTR."}
    if impressions < int(rules["min_impressions"]):
        return {"classification": "insufficient_data_manual_collection", "reason": "Muestra de impresiones insuficiente."}
    if float(stats.get("ctr", 0) or 0) < float(rules["min_ctr_percent"]):
        return {"classification": "low_ctr", "reason": "Impresiones suficientes, CTR bajo."}
    retention = stats.get("average_view_percentage")
    if retention is None:
        duration = float(stats.get("average_view_duration", 0) or 0)
        retention = (duration / float(stats.get("duration_seconds", 1) or 1)) * 100
    if float(retention or 0) < float(rules["min_retention_percent"]):
        return {"classification": "low_retention", "reason": "CTR correcto, retención baja."}
    return {"classification": "healthy", "reason": "CTR y retención dentro de los umbrales."}


def fetch_external_video_state(video: dict) -> dict:
    """Read a video's public state with yt-dlp; no YouTube Data API calls."""
    try:
        from yt_dlp import YoutubeDL
        with YoutubeDL({"quiet": True, "skip_download": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(video.get("yt_url") or video.get("yt_video_id"), download=False)
        return {"available": True, "visibility": "public", "title": info.get("title", "")}
    except Exception as exc:
        text = str(exc).lower()
        if any(term in text for term in ("not available", "removed", "private", "does not exist")):
            return {"available": False, "visibility": "removed", "error": str(exc)}
        raise


def _integrity_findings(conn, video, external):
    findings = []
    duplicate = conn.execute(
        "SELECT COUNT(*) AS count FROM videos WHERE yt_video_id = ? AND yt_video_id IS NOT NULL",
        (video["yt_video_id"],),
    ).fetchone()["count"]
    if duplicate > 1:
        findings.append({"type": "duplicate_youtube_id", "count": duplicate})
    if video["status"] in ("uploaded", "uploaded_private", "published") and not video["yt_video_id"]:
        findings.append({"type": "orphan_local_record"})
    if not video["thumbnail_path"] or not Path(video["thumbnail_path"]).is_file():
        findings.append({"type": "thumbnail_missing"})
    if external.get("visibility") != "public" or not external.get("available", True):
        findings.append({"type": "external_state", "visibility": external.get("visibility", "unknown")})
    return findings


def _run_video_review(db, row, now):
    with db._connect() as conn:
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (row["video_id"],)).fetchone()
    video_dict = dict(video)
    external = fetch_external_video_state(video_dict)
    with db._connect() as conn:
        channel = conn.execute("SELECT slug FROM channels WHERE id = ?", (video["channel_id"],)).fetchone()
        stats = conn.execute(
            """SELECT * FROM video_stats_history
               WHERE video_id = ? AND fetched_at >= datetime('now', ?)
               ORDER BY fetched_at DESC LIMIT 1""",
            (video["id"], f"-{int(review_config(channel['slug'])['stats_max_age_days'])} days"),
        ).fetchone()
    cfg = get_channel_config(channel["slug"])
    stats_dict = dict(stats) if stats else {}
    if stats_dict:
        stats_dict["duration_seconds"] = video_dict.get("duracion_seg") or stats_dict.get("duration_seconds", 1)
    result = {
        "review_kind": row["review_kind"],
        "integrity_findings": _integrity_findings(conn, video, external),
        "external_state": external,
        "performance": classify_performance(stats_dict, cfg),
    }
    return result


def _run_daily_review(db, row):
    with db._connect() as conn:
        videos = conn.execute(
            "SELECT * FROM videos WHERE channel_id=? AND yt_video_id IS NOT NULL",
            (row["channel_id"],),
        ).fetchall()
        duplicate_ids = conn.execute(
            """SELECT yt_video_id, COUNT(*) AS count FROM videos
               WHERE channel_id=? AND yt_video_id IS NOT NULL
               GROUP BY yt_video_id HAVING COUNT(*) > 1""", (row["channel_id"],)
        ).fetchall()
    findings = [{"type": "duplicate_youtube_id", "youtube_id": r[0], "count": r[1]} for r in duplicate_ids]
    findings.extend({"type": "thumbnail_missing", "video_id": v["id"]}
                    for v in videos if not v["thumbnail_path"] or not Path(v["thumbnail_path"]).is_file())
    return {"videos_checked": len(videos), "integrity_findings": findings,
            "stats_policy": "manual_collection_only"}


def _alert(db, row, success, result=None, error=None):
    payload = result or {"error": error}
    entity_type = "video" if row["video_id"] is not None else "channel"
    emit_alert(db, entity_type=entity_type, entity_id=row["video_id"] or row["channel_id"], channel_id=row["channel_id"],
               alert_type="editorial_review_success" if success else "editorial_review_failed",
               severity="info" if success else "critical",
               title=f"Revisión editorial {row['review_kind']} {'completada' if success else 'fallida'}",
               message=json.dumps(payload, ensure_ascii=False, default=str), metadata=payload)


def process_due_reviews(db, now=None) -> dict:
    ensure_review_schema(db)
    now = now or datetime.now(timezone.utc)
    result = {"processed": 0, "succeeded": 0, "failed": 0}
    with db._connect() as conn:
        rows = conn.execute(
            """SELECT * FROM editorial_reviews
               WHERE status IN ('scheduled', 'failed') AND due_at <= ?
                 AND attempts < max_attempts ORDER BY due_at""",
            (now.isoformat(),),
        ).fetchall()
    for row in rows:
        try:
            with db._connect() as conn:
                conn.execute("UPDATE editorial_reviews SET status='running', attempts=attempts+1 WHERE id=?", (row["id"],))
                conn.commit()
            value = _run_daily_review(db, row) if row["video_id"] is None else _run_video_review(db, row, now)
            with db._connect() as conn:
                conn.execute("UPDATE editorial_reviews SET status='succeeded', result_json=?, completed_at=? WHERE id=?",
                             (json.dumps(value, ensure_ascii=False, default=str), now.isoformat(), row["id"]))
                conn.commit()
            _alert(db, row, True, value)
            result["succeeded"] += 1
        except Exception as exc:
            retry_at = now + timedelta(minutes=60)
            with db._connect() as conn:
                retry_delay = 60
                if row["video_id"] is not None:
                    channel = conn.execute("SELECT slug FROM channels WHERE id=?", (row["channel_id"],)).fetchone()
                    if channel:
                        retry_delay = int(review_config(channel["slug"])["retry_minutes"])
                retry_at = now + timedelta(minutes=retry_delay)
                conn.execute("UPDATE editorial_reviews SET status='failed', last_error=?, due_at=? WHERE id=?",
                             (str(exc), retry_at.isoformat(), row["id"]))
                conn.commit()
            _alert(db, row, False, error=str(exc))
            logger.exception("Editorial review failed for row %s", row["id"])
            result["failed"] += 1
        result["processed"] += 1
    return result
