"""Channel management router."""
import json
import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from fastapi.concurrency import run_in_threadpool
from api.deps import get_db
from api.schemas.models import ChannelCreate, ChannelUpdate, ChannelResponse, ChannelConfigUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


def _parse_config(ch: dict | None) -> None:
    """Parse config_json string → dict so the frontend gets usable JSON."""
    if ch and isinstance(ch.get("config_json"), str):
        try:
            ch["config_json"] = json.loads(ch["config_json"])
        except (json.JSONDecodeError, TypeError):
            ch["config_json"] = {}


def _build_channel_config(data) -> dict:
    """Build initial config_json for a new channel from defaults + identity."""
    import importlib
    try:
        defaults = importlib.import_module("config.defaults")
    except ImportError:
        defaults = None

    config = {}
    if defaults:
        for name, value in vars(defaults).items():
            if name.startswith("_"):
                continue
            if isinstance(value, (dict, list, str, int, float, bool, tuple, type(None))):
                config[name] = value

    config["CANAL_NAME"] = data.slug
    config["CANAL_DISPLAY_NAME"] = data.name
    config["CANAL_INITIALS"] = _derive_initials(data.name)

    yt_handle = getattr(data, "youtube_handle", None)
    if yt_handle:
        config["YOUTUBE_HANDLE"] = yt_handle
        config["YOUTUBE_CHANNEL_URL"] = f"https://www.youtube.com/{yt_handle}"

    user_config = data.config.model_dump() if data.config else {}
    for k, v in user_config.items():
        if v is not None:
            config[k] = v

    return config


def _derive_initials(name: str) -> str:
    """Derive a 2-3 letter abbreviation from channel name."""
    words = name.strip().split()
    if len(words) == 1:
        return words[0][:3].upper()
    return "".join(w[0].upper() for w in words[:3])


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
    
    # Build initial config from defaults + channel identity
    config_dict = _build_channel_config(data)
    
    ch_id = db.create_channel(data.name, data.slug, config_dict)
    if ch_id is None:
        raise HTTPException(400, "Channel name or slug already exists")
    
    # Set google_account if provided
    google_account = getattr(data, 'google_account', None)
    if google_account:
        db.update_channel(ch_id, google_account=google_account)
    
    # ── Auto-create 10 thematic playlists for new channel ──
    try:
        from pipeline.youtube_playlists import create_playlists_for_channel
        create_playlists_for_channel(data.slug)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "Auto-create playlists failed for new channel '%s': %s", data.slug, e
        )
    
    ch = db.get_channel(ch_id)
    if ch:
        ch["created_at"] = str(ch.get("created_at", ""))
        ch["updated_at"] = str(ch.get("updated_at", ""))
        ch = _migrate_channel_fields(ch)
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
        for k in ("banner_url", "avatar_url", "description", "yt_channel_id", "yt_channel_url", "yt_studio_url"):
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
async def collect_channel_stats(channel_id: int, background_tasks: BackgroundTasks):
    """Recolecta stats de YouTube para un solo canal (videos, shorts, canal).

    Útil para refrescar stats de un canal específico sin ejecutar
    la recolección global de todos los canales.

    Ahora asíncrono: lanza la recolección en background y devuelve inmediatamente.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    async def _run():
        from pipeline.youtube_stats import YouTubeStatsFetcher
        import logging
        logger = logging.getLogger("autotube.stats")
        fetcher = YouTubeStatsFetcher(ch["slug"])
        result = fetcher.collect_and_store(db)
        logger.info(
            "Stats collected for %s: %s videos, %s shorts, channel=%s",
            ch["slug"],
            result.get("videos_updated", 0),
            result.get("shorts_updated", 0),
            result.get("channel_updated", False),
        )

    background_tasks.add_task(_run)
    return {
        "ok": True,
        "message": f"Recolección iniciada para {ch['name']} ({ch['slug']})",
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
                        playlist_id: int = None, source_mode: str = None):
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    videos = db.get_videos(channel_id=channel_id, status=status, limit=limit,
                            offset=offset, playlist_id=playlist_id,
                            source_mode=source_mode)
    _TIMESTAMP_COLS = (
        "created_at", "uploaded_at", "published_at", "published_verified_at",
        "published_retry_at", "target_public_at", "scheduled_upload_at",
        "generation_started_at", "generation_finished_at",
    )
    for v in videos:
        for k in _TIMESTAMP_COLS:
            if v.get(k):
                v[k] = str(v[k])
        if v.get("timing_data") and isinstance(v["timing_data"], str):
            try:
                v["timing_data"] = json.loads(v["timing_data"])
            except (json.JSONDecodeError, TypeError):
                v["timing_data"] = None
    return videos


@router.delete("/{channel_id}/videos/cleanup-errors")
def cleanup_error_videos(channel_id: int, older_than_days: int = 7, dry_run: bool = False):
    """Delete videos with status='error' older than X days.

    Set dry_run=true to preview the count without deleting anything.
    Set older_than_days to control the age threshold (default 7).
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    if dry_run:
        count = db.count_error_videos(channel_id, older_than_days)
        return {"ok": True, "dry_run": True, "would_delete": count}

    deleted = db.cleanup_error_videos(channel_id, older_than_days)
    return {"ok": True, "deleted": deleted}


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


@router.get("/{channel_id}/view-gap")
def get_channel_view_gap(channel_id: int):
    """Get view gap data for a specific channel.

    Returns coverage %, gap vs YouTube total, and DB-tracked breakdown
    (long-form views + shorts views). Falls back to latest stats snapshot
    if the ViewGapMonitor has not yet persisted state.
    """
    import json
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    slug = ch.get("slug", "")
    stored = db.get_system_state(f"view_gap_{slug}")

    if stored:
        try:
            data = json.loads(stored)
            return {"ok": True, **data}
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback: compute from latest stats
    known = db.get_db_known_views_sum(channel_id)
    latest = db.get_channel_latest_stats(channel_id)
    if latest:
        yt_t = int(latest.get("total_views", 0))
        coverage = round(known["total"] / yt_t * 100, 1) if yt_t > 0 else 100.0
        return {
            "ok": True,
            "gap": max(0, yt_t - known["total"]),
            "delta": 0,
            "yt_total_views": yt_t,
            "yt_video_count": int(latest.get("video_count", 0)),
            "db_total_views": known["total"],
            "db_longform_views": known["longform_views"],
            "db_shorts_views": known["shorts_views"],
            "db_video_count": known["video_count"],
            "coverage_pct": coverage,
            "last_checked": latest.get("fetched_at", ""),
        }

    return {"ok": True, "error": "No data available"}


@router.get("/{channel_id}/shorts-stats")
def get_channel_shorts_stats(channel_id: int):
    """Estadísticas agregadas de Shorts del canal (conteos + métricas YouTube)."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    stats = db.get_channel_shorts_stats(channel_id)
    return {"ok": True, "shorts_stats": stats}


@router.get("/{channel_id}/analytics/short-types")
def get_short_type_comparison(channel_id: int, days: int = 30):
    """Comparativa native vs clip shorts: views, subs, retention, CTR.

    Args:
        channel_id: Channel ID.
        days: Lookback window for performance analysis (default 30).
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    native = db.get_short_type_stats(channel_id, "native", days)
    clip = db.get_short_type_stats(channel_id, "clip", days)

    # ── Ratio summary ──
    total_shorts = native["total_shorts"] + clip["total_shorts"]
    native_pct = round(native["total_shorts"] / max(total_shorts, 1) * 100, 1)

    return {
        "ok": True,
        "channel_id": channel_id,
        "channel_slug": ch.get("slug", ""),
        "days": days,
        "total_shorts": total_shorts,
        "native_pct": native_pct,
        "native": native,
        "clip": clip,
        "comparison": {
            "views_ratio": (
                round(native["avg_views"] / max(clip["avg_views"], 1), 2)
                if native["avg_views"] and clip["avg_views"] else None
            ),
            "subs_per_short_ratio": (
                round(native["subs_per_short"] / max(clip["subs_per_short"], 0.001), 2)
                if native["subs_per_short"] and clip["subs_per_short"] else None
            ),
            "retention_ratio": (
                round(native["avg_view_duration"] / max(clip["avg_view_duration"], 0.1), 2)
                if native["avg_view_duration"] and clip["avg_view_duration"] else None
            ),
        },
    }


@router.get("/{channel_id}/videos-aggregate-stats")
def get_channel_videos_aggregate_stats(channel_id: int):
    """Vistas/likes/comentarios agregados de vídeos largos del canal (desde BD)."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    stats = db.get_channel_videos_aggregate(channel_id)
    return {"ok": True, "videos_stats": stats}


# ── Marathon Analytics ──────────────────────────────────────────

@router.get("/{channel_id}/analytics/marathons")
def get_channel_marathon_analytics(channel_id: int):
    """Rendimiento de maratones del canal: watch hours, CTR, top títulos."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    try:
        with db._connect() as conn:
            # Total marathons uploaded
            total = conn.execute(
                """SELECT COUNT(*) as cnt FROM videos
                   WHERE channel_id = ? AND is_marathon = 1 AND yt_video_id IS NOT NULL""",
                (channel_id,),
            ).fetchone()

            # Marathon watch hours (from latest stats snapshot per video)
            watch = conn.execute(
                """SELECT COALESCE(SUM(vsh.estimated_minutes_watched), 0) as total_min,
                          COUNT(DISTINCT vsh.video_id) as videos_with_stats
                   FROM videos v
                   LEFT JOIN video_stats_history vsh ON vsh.video_id = v.id
                   WHERE v.channel_id = ? AND v.is_marathon = 1 AND v.yt_video_id IS NOT NULL""",
                (channel_id,),
            ).fetchone()

            # Average CTR (impressions not normally tracked — estimate from views if available)
            ctr_row = conn.execute(
                """SELECT COALESCE(AVG(vsh.views), 0) as avg_views
                   FROM videos v
                   LEFT JOIN video_stats_history vsh ON vsh.video_id = v.id
                   WHERE v.channel_id = ? AND v.is_marathon = 1 AND v.yt_video_id IS NOT NULL""",
                (channel_id,),
            ).fetchone()

            # Top 5 marathon titles by views
            top_titles = conn.execute(
                """SELECT v.titulo_final as title,
                          v.yt_video_id,
                          COALESCE(MAX(vsh.views), 0) as views,
                          COALESCE(MAX(vsh.likes), 0) as likes,
                          COALESCE(MAX(vsh.comments), 0) as comments
                   FROM videos v
                   LEFT JOIN video_stats_history vsh ON vsh.video_id = v.id
                   WHERE v.channel_id = ? AND v.is_marathon = 1 AND v.yt_video_id IS NOT NULL
                     AND v.titulo_final IS NOT NULL AND v.titulo_final != ''
                   GROUP BY v.id
                   ORDER BY views DESC
                   LIMIT 5""",
                (channel_id,),
            ).fetchall()

            # Comparison: marathon vs normal video average views
            comp = conn.execute(
                """SELECT
                     COALESCE(AVG(CASE WHEN v.is_marathon = 1 THEN vsh.views END), 0) as avg_marathon_views,
                     COALESCE(AVG(CASE WHEN v.is_marathon = 0 THEN vsh.views END), 0) as avg_normal_views,
                     COALESCE(AVG(CASE WHEN v.is_marathon = 1 THEN v.duracion_seg END), 0) as avg_marathon_duration,
                     COALESCE(AVG(CASE WHEN v.is_marathon = 0 THEN v.duracion_seg END), 0) as avg_normal_duration
                   FROM videos v
                   LEFT JOIN video_stats_history vsh ON vsh.video_id = v.id
                   WHERE v.channel_id = ? AND v.yt_video_id IS NOT NULL""",
                (channel_id,),
            ).fetchone()

            total_marathons = total["cnt"] if total else 0
            total_watch_min = watch["total_min"] if watch else 0
            videos_with_stats = watch["videos_with_stats"] if watch else 0

            return {
                "ok": True,
                "marathon_analytics": {
                    "total_marathons": total_marathons,
                    "total_watch_hours": round(total_watch_min / 60, 1),
                    "videos_with_stats": videos_with_stats,
                    "avg_views": round(ctr_row["avg_views"], 1) if ctr_row else 0,
                    "top_titles": [
                        {
                            "title": r["title"],
                            "views": r["views"],
                            "likes": r["likes"],
                            "comments": r["comments"],
                        }
                        for r in (top_titles or [])
                    ],
                    "comparison": {
                        "avg_marathon_views": round(comp["avg_marathon_views"], 1) if comp else 0,
                        "avg_normal_views": round(comp["avg_normal_views"], 1) if comp else 0,
                        "avg_marathon_duration_min": round((comp["avg_marathon_duration"] or 0) / 60, 1) if comp else 0,
                        "avg_normal_duration_min": round((comp["avg_normal_duration"] or 0) / 60, 1) if comp else 0,
                    },
                },
            }

    except Exception as exc:
        logger.error("Marathon analytics query failed for channel %d: %s", channel_id, exc)
        raise HTTPException(500, f"Failed to fetch marathon analytics: {exc}")


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


# ── Optimal Publish Slots (v10) ─────────────────────────────────

@router.get("/{channel_id}/optimal-slots")
def get_optimal_slots(channel_id: int):
    """Get the calculated optimal publish slots for a channel.

    Returns 3 slots per content type (long-form / shorts) with:
    - target hour/min, score, confidence
    - audience focus (spain / latam / blend)
    - data sources used for calculation
    - usage stats (how many times used, avg result views)
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    long_slots = db.get_optimal_slots(channel_id, "long")
    short_slots = db.get_optimal_slots(channel_id, "short")

    def _format_slot(s):
        return {
            "rank": s["slot_rank"],
            "target_hour": s["target_hour"],
            "target_minute": s["target_minute"],
            "timezone": s["timezone"],
            "score": s["score"],
            "confidence": s["confidence"],
            "audience_focus": s["audience_focus"],
            "calculated_at": s["calculated_at"],
            "used_count": s["used_count"],
            "avg_views_result": s["avg_views_result"],
            "data_sources": json.loads(s.get("data_sources", "{}")),
        }

    return {
        "ok": True,
        "channel_id": channel_id,
        "channel_name": ch.get("name", ""),
        "long": [_format_slot(s) for s in long_slots],
        "shorts": [_format_slot(s) for s in short_slots],
        "has_data": bool(long_slots or short_slots),
    }


@router.post("/{channel_id}/recalculate-slots")
async def recalculate_slots(channel_id: int, background_tasks: BackgroundTasks):
    """Force recalculate optimal publish slots for a channel now.
    
    Triggers the full calculation pipeline (YT Analytics + DB historical)
    and replans pending slots if they changed. Runs async in background.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    async def _run():
        from api.services.optimal_slots_calculator import OptimalSlotsCalculator
        calc = OptimalSlotsCalculator(db)
        result = calc.calculate_for_channel(channel_id, ch["slug"])
        if result.get("long_changed") or result.get("shorts_changed"):
            calc._replan_channel(channel_id, ch["slug"], result)

    background_tasks.add_task(_run)
    return {
        "ok": True,
        "message": f"Recalculation triggered for {ch['name']}. Check /optimal-slots for results.",
    }


# ── Timing Dashboard (v11) ──────────────────────────────────────

@router.get("/{channel_id}/timing-dashboard")
def get_timing_dashboard(channel_id: int, days: int = Query(default=90, ge=7, le=365)):
    """Aggregated timing dashboard for the Horarios tab.

    Returns optimal slots, planning config, execution history,
    and aggregate stats in a single call.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    # ── Planning config ──
    cfg = db.get_channel_planning_config(channel_id)
    if not cfg:
        cfg = {}

    config_out = {
        "publish_mode": cfg.get("publish_mode", "immediate"),
        "publish_target_hour": cfg.get("publish_target_hour"),
        "publish_jitter_min": cfg.get("publish_jitter_min", 20),
        "publish_warmup_min": cfg.get("publish_warmup_min", 120),
        "publish_timezone": cfg.get("publish_timezone", "Europe/Madrid"),
        "publish_window_spread_min": cfg.get("publish_window_spread_min",
                                              cfg.get("publish_jitter_min", 20)),
        "upload_windows": cfg.get("upload_windows", [
            {"start": 9, "end": 11}
        ]),
        "generation_lead_hours": cfg.get("generation_lead_hours", 36),
    }

    # ── Optimal slots ──
    long_slots_raw = db.get_optimal_slots(channel_id, "long")
    short_slots_raw = db.get_optimal_slots(channel_id, "short")

    def _fmt_slot(s):
        return {
            "rank": s["slot_rank"],
            "target_hour": s["target_hour"],
            "target_minute": s["target_minute"],
            "timezone": s.get("timezone", config_out["publish_timezone"]),
            "score": s["score"],
            "confidence": s["confidence"],
            "audience_focus": s.get("audience_focus", "blend"),
            "calculated_at": s.get("calculated_at"),
            "used_count": s.get("used_count", 0),
            "avg_views_result": s.get("avg_views_result", 0),
            "data_sources": json.loads(s.get("data_sources", "{}")),
        }

    optimal_slots = {
        "long": [_fmt_slot(s) for s in long_slots_raw],
        "shorts": [_fmt_slot(s) for s in short_slots_raw],
        "has_data": bool(long_slots_raw or short_slots_raw),
    }

    # ── Execution history ──
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    all_videos = db.get_videos(channel_id=channel_id, limit=200)

    execution_history = []
    for v in all_videos:
        is_short = v.get("status") == "short"
        uploaded = v.get("uploaded_at")
        published = v.get("published_at")
        target_public = v.get("target_public_at")

        # Only include videos with at least one timing event
        if not (uploaded or published or target_public):
            continue

        # Filter by date range
        latest_event = published or uploaded or target_public
        if isinstance(latest_event, str) and latest_event < since:
            continue

        execution_history.append({
            "video_id": v["id"],
            "titulo_final": v.get("titulo_final", ""),
            "is_short": is_short,
            "status": v.get("status", ""),
            "uploaded_at": _to_str(uploaded),
            "target_public_at": _to_str(target_public),
            "published_at": _to_str(published),
            "publish_mode": v.get("publish_mode", "immediate"),
            "peak_source": v.get("peak_source"),
        })

    # Sort by most recent event first
    execution_history.sort(
        key=lambda x: x["published_at"] or x["uploaded_at"] or x["target_public_at"] or "",
        reverse=True,
    )

    # ── Aggregate stats ──
    scheduled_videos = [e for e in execution_history
                        if e["publish_mode"] == "scheduled" and e["published_at"]]
    total_published = len([e for e in execution_history if e["published_at"]])
    total_scheduled = len(scheduled_videos)

    # % within target window (±jitter)
    jitter_min = config_out["publish_jitter_min"]
    target_hour = config_out.get("publish_target_hour")
    warmup_min = config_out["publish_warmup_min"]

    within_window = 0
    total_warmups = 0
    sum_warmup_min = 0.0

    for e in execution_history:
        if not e["published_at"] or not e["target_public_at"] or not target_hour:
            continue
        try:
            pub_dt = _parse_dt(e["published_at"])
            target_dt = _parse_dt(e["target_public_at"])
            if pub_dt and target_dt:
                diff_min = abs((pub_dt - target_dt).total_seconds()) / 60.0
                if diff_min <= jitter_min:
                    within_window += 1
        except Exception:
            pass

        # Calculate real warmup = published_at - uploaded_at
        if e["published_at"] and e["uploaded_at"]:
            try:
                pub_dt = _parse_dt(e["published_at"])
                up_dt = _parse_dt(e["uploaded_at"])
                if pub_dt and up_dt:
                    wu_min = (pub_dt - up_dt).total_seconds() / 60.0
                    if wu_min > 0 and wu_min < 10080:  # sanity check (< 1 week)
                        total_warmups += 1
                        sum_warmup_min += wu_min
            except Exception:
                pass

    pct_within = round(within_window / max(total_scheduled, 1) * 100, 1)
    avg_warmup = round(sum_warmup_min / max(total_warmups, 1), 1)

    stats = {
        "total_published": total_published,
        "total_scheduled": total_scheduled,
        "avg_warmup_actual_min": avg_warmup if total_warmups > 0 else None,
        "pct_within_window": pct_within,
    }

    return {
        "ok": True,
        "channel_id": channel_id,
        "channel_name": ch.get("name", ""),
        "config": config_out,
        "optimal_slots": optimal_slots,
        "execution_history": execution_history,
        "stats": stats,
    }


def _to_str(val) -> str | None:
    """Safely convert a datetime or string to ISO string."""
    if val is None:
        return None
    return str(val) if isinstance(val, str) else val.isoformat()


def _parse_dt(val) -> datetime | None:
    """Parse an ISO timestamp string."""
    if not val:
        return None
    try:
        # Handle timezone offsets
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
