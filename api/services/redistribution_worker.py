"""Redistribution worker — Máquina A (espejo): backfill progresivo del catálogo.

Drena la cola pendiente de platform_videos a ritmo controlado por plataforma:
- Warm-up lento (warmup_daily_cap) durante warmup_days para cuentas nuevas.
- Régimen estable (daily_cap) después.
- Backoff exponencial ante 429/403 (backoff_until en redistribution_state).
- Respeto del espaciado global entre subidas (45 min, mismo criterio anti-spam).

Una sola subida a la vez (el loop es secuencial) → no compite por RAM con la
generación long-form y nunca satura una plataforma.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

PAUSE_KEY = "redistribution_paused_{channel_id}"
MIN_INTERVAL_MIN = 15          # mínimo entre subidas a una misma plataforma
MAX_UPLOADS_PER_TICK = 1       # una subida por tick global (sequencial)

_ESPEJO_PLATFORMS = ("rumble", "dailymotion", "facebook")


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (ValueError, TypeError):
        return None


def _platform_daily_cap(cfg: dict, state: dict | None, now: datetime) -> int:
    """Warm-up cap antes de warmup_until, régimen después."""
    warmup_days = int(cfg.get("warmup_days", 7) or 7)
    warmup_cap = int(cfg.get("warmup_daily_cap", 1) or 1)
    daily_cap = cfg.get("daily_cap", {})
    platform = (state or {}).get("platform", "")
    cap = int(daily_cap.get(platform, 3) or 3)

    warmup_until = _parse_ts((state or {}).get("warmup_until"))
    if warmup_until is None:
        # First activation: seed warm-up window
        return warmup_cap, True
    if now < warmup_until:
        return warmup_cap, False
    return cap, False


def redistribution_tick(db) -> dict:
    """Process ONE pending espejo upload across all channels (sequencial).

    Returns summary dict for logging/UI.
    """
    now = datetime.now(timezone.utc)
    channels = db.get_channels() or []
    summary = {"channels_checked": 0, "uploaded": [], "skipped": []}

    for ch in channels:
        channel_id = ch.get("id")
        slug = ch.get("slug", "")
        if not channel_id:
            continue
        summary["channels_checked"] += 1

        if db.get_system_state(PAUSE_KEY.format(channel_id=channel_id)):
            continue

        # Read config
        try:
            from config.config_bridge import get_channel_config
            cfg_obj = get_channel_config(slug)
            rd = getattr(cfg_obj, "SOCIAL_REDISTRIBUTION", None)
        except Exception:
            rd = None
        if not isinstance(rd, dict):
            continue
        espejo = [p for p in rd.get("enabled_platforms", []) if p in _ESPEJO_PLATFORMS]
        if not espejo:
            continue

        accounts = db.get_enabled_social_accounts(channel_id)
        account_platforms = {a["platform"] for a in accounts}

        for platform in espejo:
            if platform not in account_platforms:
                continue

            state = db.get_redistribution_state(channel_id, platform)

            # Backoff activo
            backoff_until = _parse_ts((state or {}).get("backoff_until"))
            if backoff_until and backoff_until > now:
                summary["skipped"].append(f"{slug}/{platform}:backoff")
                continue

            # Espaciado mínimo entre subidas a la misma plataforma
            last_pub = _parse_ts((state or {}).get("last_publish_at"))
            if last_pub and (now - last_pub) < timedelta(minutes=MIN_INTERVAL_MIN):
                continue

            # Cap diario
            cap, seed_warmup = _platform_daily_cap(rd, state, now)
            published_today = db.count_platform_published_today(channel_id, platform)
            if published_today >= cap:
                summary["skipped"].append(f"{slug}/{platform}:cap({published_today}/{cap})")
                continue

            if seed_warmup:
                warmup_until = (now + timedelta(days=int(rd.get("warmup_days", 7)))).isoformat()
                db.upsert_redistribution_state(channel_id, platform,
                                               warmup_until=warmup_until)
                logger.info("[Redistribution] %s/%s warm-up until %s", slug, platform, warmup_until)

            # Siguiente pendiente
            next_row = db.get_redistribution_backlog(channel_id, platform, limit=1)
            if not next_row:
                continue
            row = next_row[0]
            video_id = row["video_id"]

            # Fichero existe?
            video = db.get_video(video_id)
            vpath = (video or {}).get("video_path") or ""
            if not vpath or not os.path.exists(vpath):
                db.update_platform_video(row["id"], status="failed",
                                         error_message="Video file missing on disk",
                                         attempts=row.get("attempts", 0) + 1)
                summary["skipped"].append(f"{slug}/{platform}:missing-file")
                continue

            # Publicar
            try:
                from api.services.publishers.platform_manager import PlatformPublishManager
                import json as _json
                tags = []
                try:
                    tags_json = video.get("tags_json", "[]")
                    if tags_json:
                        tags = _json.loads(tags_json) if isinstance(tags_json, str) else tags_json
                except Exception:
                    pass
                yt_url = video.get("yt_url", "") or (
                    f"https://youtube.com/watch?v={video.get('yt_video_id', '')}"
                    if video.get("yt_video_id") else ""
                )
                mgr = PlatformPublishManager(slug, channel_id, db)
                result = mgr.publish_to_platform(
                    video_id=video_id, platform=platform,
                    video_data={"video_path": vpath},
                    metadata={
                        "title": video.get("titulo_final", ""),
                        "description": video.get("description", ""),
                        "tags": tags,
                        "thumbnail_path": video.get("thumbnail_path"),
                    },
                )
                # Nota: publish_to_platform devuelve UploadResult; el manager ya
                # actualiza platform_videos. Estado del backfill:
                if result.success:
                    db.upsert_redistribution_state(channel_id, platform,
                                                   last_publish_at=now.isoformat())
                    summary["uploaded"].append(f"{slug}/{platform}/v{video_id}")
                    logger.info("[Redistribution] %s/%s v%s OK → %s",
                                slug, platform, video_id, result.platform_video_url)
                else:
                    err = (result.error or "unknown")[:300]
                    if any(k in err.lower() for k in ("429", "rate", "too many")):
                        backoff = (now + timedelta(hours=6)).isoformat()
                        db.upsert_redistribution_state(channel_id, platform,
                                                       backoff_until=backoff)
                        logger.warning("[Redistribution] %s/%s rate-limited → backoff 6h: %s",
                                       slug, platform, err)
                    summary["skipped"].append(f"{slug}/{platform}:fail:{err[:80]}")
            except Exception as exc:
                logger.warning("[Redistribution] %s/%s publish error: %s", slug, platform, exc)
                summary["skipped"].append(f"{slug}/{platform}:exc:{str(exc)[:80]}")
            break  # una subida por tick

    return summary
