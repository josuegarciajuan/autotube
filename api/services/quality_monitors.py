"""Zero-quota quality monitors: duration drift and video-phase regressions.

Both are idempotent (one active alert per channel/type) and resolve themselves
when the condition disappears. Kill-switches:
  * ``DURATION_DRIFT_ALERT_ENABLED=false``
  * ``VIDEO_PHASE_REGRESSION_ALERT_ENABLED=false``
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("autotube.quality_monitors")


def _enabled(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _active_channels(db) -> list[dict]:
    try:
        return [dict(c) for c in db.get_channels(active_only=True)]
    except Exception:
        return []


def check_duration_drift(db, days: int = 14, min_sample: int = 5,
                         max_outside_ratio: float = 0.20) -> list[dict]:
    """Alert when recent long-forms fall outside the channel's duration policy."""
    if not _enabled("DURATION_DRIFT_ALERT_ENABLED"):
        return []
    from config.config_bridge import get_channel_config
    from config.duration_policy import resolve_duration_range

    results: list[dict] = []
    for ch in _active_channels(db):
        channel_id = ch["id"]
        slug = ch["slug"]
        try:
            with db._connect() as conn:
                rows = conn.execute(
                    """SELECT id, duracion_seg FROM videos
                       WHERE channel_id = ? AND yt_video_id IS NOT NULL
                         AND duracion_seg IS NOT NULL AND duracion_seg > 0
                         AND uploaded_at >= datetime('now', ?)""",
                    (channel_id, f"-{int(days)} days"),
                ).fetchall()
            durations = [int(r["duracion_seg"]) for r in rows]
            if len(durations) < min_sample:
                db.resolve_alerts(alert_type="duration_drift", channel_id=channel_id)
                continue
            policy = resolve_duration_range(get_channel_config(slug))
            lo, hi = policy.min_sec, policy.max_sec
            outside = [d for d in durations if d < lo or d > hi]
            ratio = len(outside) / len(durations)
            if ratio > max_outside_ratio:
                db.create_alert(
                    entity_type="channel", entity_id=channel_id,
                    channel_id=channel_id, alert_type="duration_drift",
                    severity="warning",
                    title=f"Deriva de duración — {ch.get('name', slug)}",
                    message=(
                        f"{len(outside)}/{len(durations)} largos recientes "
                        f"({ratio:.0%}) fuera del objetivo {lo // 60}-{hi // 60} min "
                        f"en los últimos {days} días."
                    ),
                    metadata={
                        "window_days": days,
                        "sample": len(durations),
                        "outside": len(outside),
                        "outside_ratio": round(ratio, 3),
                        "target_min_sec": lo,
                        "target_max_sec": hi,
                        "policy_source": policy.source,
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                results.append({"channel": slug, "status": "alert",
                                "outside": len(outside), "sample": len(durations)})
            else:
                db.resolve_alerts(alert_type="duration_drift", channel_id=channel_id)
                results.append({"channel": slug, "status": "ok",
                                "outside": len(outside), "sample": len(durations)})
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("duration drift check failed for %s: %s", slug, exc)
    return results


def check_video_phase_regression(db, hours: int = 24) -> list[dict]:
    """Alert when the video phase produced any error in the last N hours."""
    if not _enabled("VIDEO_PHASE_REGRESSION_ALERT_ENABLED"):
        return []
    results: list[dict] = []
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT canal, COUNT(*) n
                   FROM pipeline_log
                   WHERE phase = 'video' AND status = 'error'
                     AND created_at >= datetime('now', ?)
                   GROUP BY canal""",
                (f"-{int(hours)} hours",),
            ).fetchall()
        by_canal = {r["canal"]: int(r["n"]) for r in rows}
    except Exception as exc:  # noqa: BLE001
        logger.debug("video phase query failed: %s", exc)
        return []
    for ch in _active_channels(db):
        slug = ch["slug"]
        count = by_canal.get(slug, 0)
        try:
            if count:
                db.create_alert(
                    entity_type="channel", entity_id=ch["id"],
                    channel_id=ch["id"], alert_type="video_phase_regression",
                    severity="critical" if count >= 3 else "warning",
                    title=f"Regresión en fase video — {ch.get('name', slug)}",
                    message=f"{count} error(es) de fase video en las últimas {hours}h.",
                    metadata={
                        "hours": hours,
                        "errors": count,
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                results.append({"channel": slug, "status": "alert", "errors": count})
            else:
                db.resolve_alerts(alert_type="video_phase_regression",
                                  channel_id=ch["id"])
                results.append({"channel": slug, "status": "ok", "errors": 0})
        except Exception as exc:  # noqa: BLE001
            logger.debug("video regression check failed for %s: %s", slug, exc)
    return results


def run_quality_monitors(db) -> dict:
    """Run both monitors; safe to call from a supervised loop."""
    return {
        "duration_drift": check_duration_drift(db),
        "video_phase_regression": check_video_phase_regression(db),
    }
