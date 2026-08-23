"""Social redistribution API router.

Máquina A (espejo): backfill progresivo del catálogo a Rumble/Dailymotion/Facebook.
Máquina B (embudo): teasers a Bluesky/Mastodon (vía video_lifecycle).

Endpoints:
- status / start / pause / resume / enqueue (backfill por canal)
- social-stats por canal y por vídeo
- /social-stats/collect (recolección manual, 0 cuota YouTube)
"""

import json
import logging

from fastapi import APIRouter, HTTPException

from api.deps import get_db
from config.config_bridge import get_channel_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Redistribution"])

PAUSE_KEY = "redistribution_paused_{channel_id}"


def _channel_or_404(db, channel_id: int) -> dict:
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, f"Channel {channel_id} not found")
    return ch


def _redistribution_cfg(channel_id: int) -> dict:
    """Read SOCIAL_REDISTRIBUTION from the channel's effective config."""
    ch = _channel_or_404(get_db(), channel_id)
    slug = ch.get("slug", "")
    try:
        cfg = get_channel_config(slug)
        rd = getattr(cfg, "SOCIAL_REDISTRIBUTION", None)
        if isinstance(rd, dict):
            return rd
    except Exception as exc:
        logger.warning("redistribution config error for %s: %s", slug, exc)
    return {}


def _get_platforms(channel_id: int) -> tuple[list[str], list[str]]:
    cfg = _redistribution_cfg(channel_id)
    espejo = [p for p in cfg.get("enabled_platforms", []) if p in
              ("rumble", "dailymotion", "facebook")]
    embudo = [p for p in cfg.get("embudo_platforms", []) if p in
              ("bluesky", "mastodon")]
    return espejo, embudo


# ── Backfill control ───────────────────────────────────────────


@router.get("/channels/{channel_id}/redistribution/status")
def redistribution_status(channel_id: int):
    """Estado del backfill por canal: plataformas, cola, ritmo, backoff."""
    db = get_db()
    _channel_or_404(db, channel_id)
    espejo, embudo = _get_platforms(channel_id)
    cfg = _redistribution_cfg(channel_id)

    daily_cap = cfg.get("daily_cap", {})
    paused = bool(db.get_system_state(PAUSE_KEY.format(channel_id=channel_id)))

    platforms = []
    for platform in espejo + embudo:
        pending = db.get_redistribution_backlog(channel_id, platform, limit=1)
        state = db.get_redistribution_state(channel_id, platform)
        accts = db.get_enabled_social_accounts(channel_id)
        has_account = any(a["platform"] == platform for a in accts)
        platforms.append({
            "platform": platform,
            "type": "espejo" if platform in espejo else "embudo",
            "has_account": has_account,
            "pending_count": len(pending) if pending else 0,
            "daily_cap": daily_cap.get(platform, 3),
            "warmup_until": (state or {}).get("warmup_until"),
            "backoff_until": (state or {}).get("backoff_until"),
            "last_publish_at": (state or {}).get("last_publish_at"),
        })

    total_pending = sum(p["pending_count"] for p in platforms)
    return {
        "enabled": bool(espejo or embudo),
        "paused": paused,
        "espejo": espejo,
        "embudo": embudo,
        "warmup_days": cfg.get("warmup_days", 7),
        "warmup_daily_cap": cfg.get("warmup_daily_cap", 1),
        "backlog_direction": cfg.get("backlog_direction", "oldest"),
        "total_pending": total_pending,
        "platforms": platforms,
    }


@router.post("/channels/{channel_id}/redistribution/start")
def redistribution_start(channel_id: int, video_ids: list[int] = None):
    """Arranca el backfill: encola el catálogo publicado como cola pendiente."""
    db = get_db()
    _channel_or_404(db, channel_id)
    espejo, embudo = _get_platforms(channel_id)
    platforms = list(dict.fromkeys(espejo + embudo))
    if not platforms:
        raise HTTPException(400, "No redistribution platforms enabled for this channel")

    created = db.enqueue_redistribution_backlog(channel_id, platforms, video_ids)
    db.set_system_state(PAUSE_KEY.format(channel_id=channel_id), "")
    logger.info("[Redistribution] channel %s: enqueued %d rows (%s)",
                channel_id, created, ",".join(platforms))
    return {"ok": True, "enqueued": created, "platforms": platforms}


@router.post("/channels/{channel_id}/redistribution/pause")
def redistribution_pause(channel_id: int):
    db = get_db()
    _channel_or_404(db, channel_id)
    db.set_system_state(PAUSE_KEY.format(channel_id=channel_id), "1")
    return {"ok": True, "paused": True}


@router.post("/channels/{channel_id}/redistribution/resume")
def redistribution_resume(channel_id: int):
    db = get_db()
    _channel_or_404(db, channel_id)
    db.set_system_state(PAUSE_KEY.format(channel_id=channel_id), "")
    return {"ok": True, "paused": False}


@router.post("/channels/{channel_id}/redistribution/enqueue")
def redistribution_enqueue(channel_id: int, video_ids: list[int] = None):
    """Encuela vídeos concretos (o todo el catálogo) sin arrancar el loop."""
    db = get_db()
    _channel_or_404(db, channel_id)
    espejo, embudo = _get_platforms(channel_id)
    platforms = list(dict.fromkeys(espejo + embudo))
    created = db.enqueue_redistribution_backlog(channel_id, platforms, video_ids)
    return {"ok": True, "enqueued": created}


@router.get("/channels/{channel_id}/redistribution/backlog")
def redistribution_backlog(channel_id: int, platform: str = None, limit: int = 50):
    """Cola pendiente legible (con título del vídeo) por canal/plataforma."""
    db = get_db()
    _channel_or_404(db, channel_id)
    rows = db.get_redistribution_backlog(channel_id, platform, limit=min(limit, 200))
    # Attach video titles
    for row in rows:
        v = db.get_video(row["video_id"])
        row["video_title"] = (v or {}).get("titulo_final") or (v or {}).get("title") or ""
        row["yt_video_id"] = (v or {}).get("yt_video_id") or ""
        row["queue_order"] = row.get("queue_order") or row["id"]
    rows.sort(key=lambda r: (r.get("queue_order") is None, r.get("queue_order") or 0))
    return rows


# ── Stats ──────────────────────────────────────────────────────


@router.get("/channels/{channel_id}/social-stats")
def channel_social_stats(channel_id: int):
    """Stats agregados por red para el canal (vistas, likes, comentarios...)."""
    db = get_db()
    _channel_or_404(db, channel_id)
    return {
        "channel_id": channel_id,
        "per_platform": db.get_channel_social_stats(channel_id),
    }


@router.get("/channels/{channel_id}/videos-social-stats")
def channel_videos_social_stats(channel_id: int):
    """Bulk: stats por red de TODOS los vídeos del canal en una sola query.

    Devuelve {video_id: [{platform, platform_video_id, platform_video_url,
    status, views, likes, comments, reposts, uploaded_at}, ...]} para pintar
    en el listado de vídeos sin N+1.
    """
    db = get_db()
    _channel_or_404(db, channel_id)
    return db.get_channel_videos_social_stats(channel_id)


@router.get("/videos/{video_id}/social-stats")
def video_social_stats(video_id: int):
    """Stats por red de un vídeo concreto + histórico reciente."""
    db = get_db()
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(404, f"Video {video_id} not found")
    per_platform = db.get_video_social_stats(video_id)
    for row in per_platform:
        pid = db.get_platform_video(video_id, row["platform"])
        if pid:
            row["history"] = db.get_platform_video_stats_history(pid["id"], limit=30)
        else:
            row["history"] = []
    return {"video_id": video_id, "per_platform": per_platform}


@router.post("/social-stats/collect")
def social_stats_collect(channel_id: int = None):
    """Recolección manual de stats sociales (APIs gratuitas, 0 cuota YouTube)."""
    db = get_db()
    try:
        from api.services.social_stats_collector import collect_channel_stats
        result = collect_channel_stats(db, channel_id)
        return {"ok": True, "results": result}
    except Exception as exc:
        logger.exception("Social stats collection failed")
        raise HTTPException(500, f"Stats collection failed: {exc}")
