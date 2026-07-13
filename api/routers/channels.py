"""Channel management router."""
import json
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from api.deps import get_db
from api.schemas.models import ChannelCreate, ChannelUpdate, ChannelResponse, ChannelConfigUpdate

router = APIRouter()


def _parse_config(ch: dict | None) -> None:
    """Parse config_json string → dict so the frontend gets usable JSON."""
    if ch and isinstance(ch.get("config_json"), str):
        try:
            ch["config_json"] = json.loads(ch["config_json"])
        except (json.JSONDecodeError, TypeError):
            ch["config_json"] = {}


@router.get("")
def list_channels(active_only: bool = False):
    db = get_db()
    channels = db.get_channels(active_only=active_only)
    for ch in channels:
        ch["created_at"] = str(ch.get("created_at", ""))
        ch["updated_at"] = str(ch.get("updated_at", ""))
        _parse_config(ch)
    return channels


@router.post("")
def create_channel(data: ChannelCreate):
    db = get_db()
    config_dict = data.config.model_dump() if data.config else {}
    ch_id = db.create_channel(data.name, data.slug, config_dict)
    if ch_id is None:
        raise HTTPException(400, "Channel name or slug already exists")
    
    # ── Auto-create 10 thematic playlists for new channel ──
    try:
        from pipeline.youtube_playlists import create_playlists_for_channel
        create_playlists_for_channel(data.slug)
    except Exception as e:
        # Non-fatal: channel is usable even without playlists
        import logging
        logging.getLogger(__name__).warning(
            "Auto-create playlists failed for new channel '%s': %s", data.slug, e
        )
    
    ch = db.get_channel(ch_id)
    if ch:
        ch["created_at"] = str(ch.get("created_at", ""))
        ch["updated_at"] = str(ch.get("updated_at", ""))
    return ch


@router.get("/stats-summary")
def get_all_channels_stats_summary():
    """Devuelve el último snapshot de estadísticas de todos los canales desde la BD.
    
    Rápido, no requiere autenticación YouTube (usa los snapshots guardados cada 6h).
    Incluye: total_views, subscribers, video_count, estimated_minutes_watched.
    """
    db = get_db()
    stats = db.get_all_channels_latest_stats()
    # Convert estimated_minutes_watched to hours for easy display
    for s in stats:
        emw = s.get("estimated_minutes_watched") or 0
        s["estimated_hours_watched"] = round(emw / 60.0, 1) if emw else 0
    return {"ok": True, "channels": stats}


@router.get("/{channel_id}")
def get_channel(channel_id: int):
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    ch["created_at"] = str(ch.get("created_at", ""))
    ch["updated_at"] = str(ch.get("updated_at", ""))
    _parse_config(ch)
    return ch


@router.put("/{channel_id}")
def update_channel(channel_id: int, data: ChannelUpdate):
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    
    config_dict = data.config.model_dump() if data.config else None
    db.update_channel(
        channel_id,
        name=data.name,
        slug=data.slug,
        config=config_dict,
        active=data.active,
    )
    # Update new profile fields directly
    import sqlite3
    with db._connect() as conn:
        fields, values = [], []
        for k in ("banner_url", "avatar_url", "description", "yt_channel_id", "yt_channel_url"):
            v = getattr(data, k, None)
            if v is not None:
                fields.append(f"{k} = ?")
                values.append(v)
        if fields:
            fields.append("updated_at = CURRENT_TIMESTAMP")
            values.append(channel_id)
            conn.execute(f"UPDATE channels SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()
    
    ch = db.get_channel(channel_id)
    if ch:
        for k in ("created_at", "updated_at"):
            if ch.get(k): ch[k] = str(ch[k])
    _parse_config(ch)
    return ch


@router.put("/{channel_id}/profile")
def update_channel_profile(channel_id: int, data: ChannelConfigUpdate):
    """Update channel profile fields (name, description, banner, avatar, yt url)."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    
    import sqlite3
    with db._connect() as conn:
        fields, values = [], []
        updates = data.model_dump(exclude_none=True)
        for k, v in updates.items():
            fields.append(f"{k} = ?")
            values.append(v)
        if not fields:
            raise HTTPException(400, "No fields to update")
        fields.append("updated_at = CURRENT_TIMESTAMP")
        values.append(channel_id)
        conn.execute(f"UPDATE channels SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
    
    ch = db.get_channel(channel_id)
    if ch:
        for k in ("created_at", "updated_at"):
            if ch.get(k): ch[k] = str(ch[k])
    return ch


@router.post("/{channel_id}/sync-youtube")
def sync_youtube(channel_id: int):
    """Sincroniza metadatos del canal a YouTube (descripción, keywords, país, idioma).

    Lo que SÍ se sube por API:
        - snippet.description
        - brandingSettings.channel.keywords
        - brandingSettings.channel.country
        - brandingSettings.channel.defaultLanguage

    Lo que NO se sube (requiere YouTube Studio):
        - Nombre del canal (tied to Google account)
        - Banner (2560x1440)
        - Avatar (800x800)
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    from pipeline.youtube_channel_manager import YouTubeChannelManager
    from config.config_bridge import get_channel_config

    cfg = get_channel_config(ch["slug"])
    
    mgr = YouTubeChannelManager(ch["slug"])
    if not mgr.authenticate():
        raise HTTPException(401, "No autenticado. Ejecuta /auth-start primero o verifica el token.")

    # Actualizar lo que se puede por API
    result = mgr.update_channel_metadata(
        description=getattr(cfg, "CHANNEL_ABOUT_SECTION", ""),
        keywords=getattr(cfg, "CHANNEL_KEYWORDS", []),
        country="ES",
        language="es",
    )

    # Generar reporte de lo que falta
    unuploadable = mgr.get_unuploadable_report()

    return {
        "ok": "error" not in result,
        "api_updated": result.get("updated_fields", []),
        "api_error": result.get("error"),
        "manual_setup_required": unuploadable["manual_fields"],
        "instructions": unuploadable["instructions"],
    }


@router.post("/{channel_id}/collect-stats")
def collect_channel_stats(channel_id: int):
    """Recolecta stats de YouTube para un solo canal (videos, shorts, canal).

    Útil para refrescar stats de un canal específico sin ejecutar
    la recolección global de todos los canales.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    from pipeline.youtube_stats import YouTubeStatsFetcher

    fetcher = YouTubeStatsFetcher(ch["slug"])
    result = fetcher.collect_and_store(db)

    if "error" in result:
        raise HTTPException(502, result["error"])

    return {
        "ok": True,
        "slug": ch["slug"],
        "videos_updated": result.get("videos_updated", 0),
        "shorts_updated": result.get("shorts_updated", 0),
        "channel_updated": result.get("channel_updated", False),
    }


@router.post("/{channel_id}/generate-profile")
def generate_channel_profile(channel_id: int):
    """Generate banner, avatar, and description for a channel via Pollo AI.

    Reads the channel config for the description text and uses Pollo AI
    to create a 16:9 banner and 1:1 avatar image.  Saves images to
    ``output/thumbnails/{slug}/`` and updates the channel record.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    from pipeline.channel_profile_generator import generate_channel_profile as gen_profile

    try:
        profile = gen_profile(ch["slug"])
    except Exception as exc:
        raise HTTPException(500, f"Profile generation failed: {exc}")

    db.update_channel_profile_fields(
        channel_id,
        description=profile.get("description"),
        banner_url=profile.get("banner_url"),
        avatar_url=profile.get("avatar_url"),
    )

    ch = db.get_channel(channel_id)
    if ch:
        ch["created_at"] = str(ch.get("created_at", ""))
        ch["updated_at"] = str(ch.get("updated_at", ""))
    return {"ok": True, "channel": ch, "profile": profile}


@router.delete("/{channel_id}")
def delete_channel(channel_id: int):
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    db.delete_channel(channel_id)
    return {"ok": True}


@router.get("/{channel_id}/videos")
def list_channel_videos(channel_id: int, status: str = None, limit: int = 50, offset: int = 0,
                        playlist_id: int = None):
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    videos = db.get_videos(channel_id=channel_id, status=status, limit=limit,
                            offset=offset, playlist_id=playlist_id)
    for v in videos:
        for k in ("created_at", "uploaded_at"):
            if v.get(k):
                v[k] = str(v[k])
        if v.get("timing_data") and isinstance(v["timing_data"], str):
            try:
                v["timing_data"] = json.loads(v["timing_data"])
            except (json.JSONDecodeError, TypeError):
                v["timing_data"] = None
    return videos


@router.get("/{channel_id}/content")
def list_channel_content(channel_id: int, limit: int = 50, unused_only: bool = True):
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    import sqlite3
    with db._connect() as conn:
        canal = ch["slug"]
        q = "SELECT * FROM raw_content WHERE canal = ?"
        if unused_only:
            q += " AND used = 0"
        q += " ORDER BY scraped_at DESC LIMIT ?"
        rows = conn.execute(q, (canal, limit)).fetchall()
    result = [dict(r) for r in rows]
    for r in result:
        r["scraped_at"] = str(r.get("scraped_at", ""))
        r["used"] = bool(r.get("used", False))
    return result


@router.post("/{channel_id}/sync-config")
def sync_channel_config(channel_id: int):
    """Sync config_json from the Python config module to the DB.

    Reads ``config/canal2_config.py`` (or equivalent) and writes its
    serialisable attributes into ``channels.config_json``.  The config
    bridge cache is invalidated so the next pipeline run picks up the
    latest values.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    from config.config_bridge import sync_config_to_db
    updated = sync_config_to_db(ch["slug"])
    if updated is None:
        raise HTTPException(400, f"Could not sync config for slug '{ch['slug']}'")

    _parse_config(updated)
    return {"ok": True, "channel": updated}


@router.put("/{channel_id}/config")
def update_channel_config(channel_id: int, data: dict):
    """Update channel config_json directly from the panel editor."""
    from config.config_bridge import get_channel_config
    import json as _json

    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    
    config = data.get("config", {})
    db.update_channel(channel_id, config=config)

    # Invalidate cache
    from config.config_bridge import _config_cache
    _config_cache.pop(ch["slug"], None)

    ch = db.get_channel(channel_id)
    _parse_config(ch)
    return {"ok": True, "channel": ch}


@router.get("/{channel_id}/manual-setup")
def get_manual_setup(channel_id: int):
    """Devuelve todo lo que hay que configurar manualmente en YouTube Studio.

    Incluye archivos generados (banner, avatar) e instrucciones paso a paso.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    from pipeline.youtube_channel_manager import YouTubeChannelManager

    mgr = YouTubeChannelManager(ch["slug"])
    report = mgr.get_unuploadable_report()

    return report


@router.get("/{channel_id}/youtube-stats")
def get_channel_youtube_stats(channel_id: int):
    """Estadísticas del canal desde la base de datos (snapshot más reciente).

    Ya no consume YouTube API quota — usa los datos cacheados por
    'Recolectar stats' o post-upload. Incluye descripción del canal.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    db_stats = db.get_channel_latest_stats(channel_id)
    if db_stats:
        stats = {
            "subscriberCount": str(db_stats.get("subscribers", 0)),
            "viewCount": str(db_stats.get("total_views", 0)),
            "videoCount": str(db_stats.get("video_count", 0)),
        }
        emw = db_stats.get("estimated_minutes_watched")
        if emw is not None and emw not in (0, 0.0, "0"):
            stats["estimatedMinutesWatched"] = str(emw)
        # Revenue estimates
        rev_min = db_stats.get("estimated_revenue_min")
        rev_max = db_stats.get("estimated_revenue_max")
        if rev_min is not None and rev_min != 0:
            stats["estimatedRevenueMin"] = round(float(rev_min), 2)
        if rev_max is not None and rev_max != 0:
            stats["estimatedRevenueMax"] = round(float(rev_max), 2)
        stats["_from_db"] = True
    else:
        stats = {
            "subscriberCount": "0",
            "viewCount": "0",
            "videoCount": "0",
            "_from_db": True,
            "_empty": True,
        }

    # Channel description from DB
    stats["channelDescription"] = ch.get("description", "") or ""

    # Convert minutes to hours for convenience
    emw = stats.get("estimatedMinutesWatched")
    if emw is not None and emw not in (0, "0"):
        stats["estimatedHoursWatched"] = round(float(emw) / 60.0, 1)
    else:
        stats["estimatedHoursWatched"] = 0

    return {"ok": True, "stats": stats}


@router.get("/{channel_id}/shorts-stats")
def get_channel_shorts_stats(channel_id: int):
    """Estadísticas agregadas de Shorts del canal (conteos + métricas YouTube)."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    stats = db.get_channel_shorts_stats(channel_id)
    return {"ok": True, "shorts_stats": stats}


@router.get("/{channel_id}/videos-aggregate-stats")
def get_channel_videos_aggregate_stats(channel_id: int):
    """Vistas/likes/comentarios agregados de vídeos largos del canal (desde BD)."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    stats = db.get_channel_videos_aggregate(channel_id)
    return {"ok": True, "videos_stats": stats}


# ── Channel Templates ──────────────────────────────────────────

@router.post("/{channel_id}/templates/{segment_type}/generate")
async def generate_template(channel_id: int, segment_type: str):
    """Regenerate a template segment (intro/cta/outro) for a channel."""
    if segment_type not in ("intro", "cta", "outro"):
        raise HTTPException(status_code=400, detail="segment_type must be intro, cta, or outro")

    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    from config.config_bridge import get_channel_config
    from pipeline.template_generator import TemplateGenerator

    config = get_channel_config(ch["slug"])
    gen = TemplateGenerator(ch["slug"], config)

    try:
        if segment_type == "intro":
            video_path = await run_in_threadpool(gen.generate_intro)
        elif segment_type == "cta":
            video_path = await run_in_threadpool(gen.generate_cta)
        else:
            video_path = await run_in_threadpool(gen.generate_outro)

        if video_path:
            db.upsert_channel_template(
                channel_id, segment_type,
                video_path=str(video_path),
                config_json=json.dumps({"generated_by": "template_generator"})
            )
            return {"status": "ok", "segment_type": segment_type, "path": str(video_path)}
        else:
            raise HTTPException(status_code=500, detail="Template generation failed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{channel_id}/templates")
async def get_templates(channel_id: int):
    """Get all templates status for a channel."""
    db = get_db()
    templates = db.get_channel_templates(channel_id)
    # Ensure all 3 types are represented (with None for missing)
    result = {"intro": None, "cta": None, "outro": None}
    for t in templates:
        result[t["segment_type"]] = {
            "video_path": t["video_path"],
            "generated_at": str(t["generated_at"]) if t.get("generated_at") else None,
        }
    return result


# ═══════════════════════════════════════════════════════════════════
# Scheduled publishing mode toggle
# ═══════════════════════════════════════════════════════════════════

@router.post("/{channel_id}/scheduled-mode/toggle")
def toggle_scheduled_publishing(channel_id: int):
    """Toggle scheduled publishing mode for a channel.

    Returns the new mode ('immediate' or 'scheduled').
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    import json
    config = json.loads(ch.get("config_json", "{}")) if isinstance(ch.get("config_json"), str) else (ch.get("config_json") or {})

    current_mode = config.get("PUBLISH_MODE", "immediate")
    new_mode = "scheduled" if current_mode == "immediate" else "immediate"

    config["PUBLISH_MODE"] = new_mode
    db.update_channel(channel_id, config=config)

    return {
        "ok": True,
        "channel_id": channel_id,
        "publish_mode": new_mode,
        "previous_mode": current_mode,
    }


@router.get("/{channel_id}/scheduled-mode")
def get_scheduled_mode(channel_id: int):
    """Get the current publishing mode for a channel."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    import json
    config = json.loads(ch.get("config_json", "{}")) if isinstance(ch.get("config_json"), str) else (ch.get("config_json") or {})

    return {
        "channel_id": channel_id,
        "publish_mode": config.get("PUBLISH_MODE", "immediate"),
        "channel_name": ch.get("name", ""),
    }


@router.get("/{channel_id}/peak-info")
def get_channel_peak_info(channel_id: int):
    """Get the computed peak publication window for a channel.

    Returns peak hour, secondary peaks, niche, and source
    (config vs heuristic). Does NOT schedule anything.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    cfg = db.get_channel_planning_config(channel_id)
    if not cfg:
        raise HTTPException(404, "Channel planning config not found")

    from pipeline.publish_scheduler import get_channel_peak_info as calc_peak
    info = calc_peak(cfg)

    return {
        "channel_id": channel_id,
        "channel_name": ch.get("name", ""),
        "publish_mode": cfg.get("publish_mode", "immediate"),
        "peak_hour": info["peak_hour"],
        "secondary_peaks": info["secondary_peaks"],
        "jitter_min": info["jitter_min"],
        "timezone": info["timezone"],
        "warmup_min": info["warmup_min"],
        "source": info["source"],
        "niche": info["niche"],
    }
