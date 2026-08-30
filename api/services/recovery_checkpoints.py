"""Quota-free publication checkpoints for the conservative recovery experiment."""
from datetime import datetime, timezone
from typing import Optional
import math

CHECKPOINT_HOURS = (48, 96, 168, 336)


def should_run_checkpoint_review(scheduler_paused: bool) -> bool:
    """Manual scheduler pause is authoritative for all scheduler work."""
    return not bool(scheduler_paused)


def normalize_rate(value, unit: str) -> Optional[float]:
    """Normalize an explicitly-unit-tagged rate to percentage points."""
    if unit not in {"fraction", "percent"}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number * 100.0 if unit == "fraction" else number


def classify_checkpoint(stats: Optional[dict]) -> str:
    if not stats:
        return "metrics_unavailable"
    impressions = stats.get("impressions")
    if "ctr" in stats:
        ctr = normalize_rate(stats.get("ctr"), stats.get("ctr_unit", "percent"))
    elif "impressionsClickThroughRate" in stats:
        ctr = normalize_rate(stats.get("impressionsClickThroughRate"), stats.get("ctr_unit"))
    else:
        ctr = None
    retention = stats.get("average_view_percentage", stats.get("averageViewPercentage"))
    retention_unit = stats.get("retention_unit", "percent")
    retention = normalize_rate(retention, retention_unit) if retention is not None else None
    try:
        impressions = float(impressions)
        if not math.isfinite(impressions) or impressions < 0 or ctr is None:
            return "metrics_unavailable"
    except (TypeError, ValueError):
        return "metrics_unavailable"
    if impressions < 100:
        return "low_impressions"
    if ctr < 2.0:
        return "low_ctr"
    if retention is not None:
        if retention < 20.0:
            return "early_retention_drop"
    return "diagnostic_ok"


def _parse(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def run_due_checkpoints(db, now=None) -> int:
    """Emit at most one durable alert per video/checkpoint and never call YT."""
    now = now or datetime.now(timezone.utc)
    created = 0
    videos = []
    offset = 0
    while True:
        try:
            page = db.get_videos(status="published", limit=500, offset=offset)
        except TypeError:
            page = db.get_videos(status="published", limit=500)
        videos.extend(page or [])
        if len(page or []) < 500:
            break
        offset += 500
    for video in videos or []:
        if not db.is_recovery_enabled(video.get("channel_id"), video.get("canal", "")):
            continue
        published = _parse(video.get("published_at"))
        if not published:
            continue
        age_hours = (now - published).total_seconds() / 3600
        for hours in CHECKPOINT_HOURS:
            if age_hours < hours or (hours == 336 and age_hours < 336):
                continue
            if db.has_recovery_checkpoint(video["id"], hours):
                continue
            # The 14-day review is conditional: only investigate videos that
            # had a non-normal 7-day signal; otherwise the 7-day checkpoint is
            # the terminal review for this experiment.
            if hours == 336 and not db.has_recovery_checkpoint(video["id"], 168):
                continue
            stats = db.get_video_latest_stats(video["id"])
            classification = classify_checkpoint(stats)
            next_hours = next((h for h in CHECKPOINT_HOURS if h > hours), None)
            recommendation = {
                "metrics_unavailable": "No hay métricas fiables; reintentar sin consumir cuota.",
                "low_impressions": "Revisar distribución/visibilidad; no cambiar packaging automáticamente.",
                "low_ctr": "Candidato de packaging para revisión manual; no aplicar cambios automáticamente.",
                "early_retention_drop": "Candidato de tema/gancho; revisar retención y guion.",
                "diagnostic_ok": "Mantener pacing conservador y observar el siguiente checkpoint.",
            }[classification]
            metadata = {
                "checkpoint_hours": hours,
                "execution_time": now.isoformat(),
                "metrics_available": classification != "metrics_unavailable",
                "retention_available": bool(stats and (
                    "average_view_percentage" in stats or "averageViewPercentage" in stats
                )),
                "metrics": stats or {},
                "classification": classification,
                "recommendation": recommendation,
                "next_checkpoint_hours": next_hours,
            }
            alert_id = db.create_recovery_alert(
                entity_id=video["id"], channel_id=video.get("channel_id"),
                alert_type=f"recovery_checkpoint_{hours}h", severity="info",
                title=f"Recovery {hours}h — {video.get('canal', 'canal')}",
                message=f"Checkpoint del vídeo {video['id']} ejecutado; clasificación: {classification}",
                metadata=metadata,
            )
            if alert_id:
                created += 1
    return created
