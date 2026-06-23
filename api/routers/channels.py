"""Channel management router."""
import json
from fastapi import APIRouter, HTTPException
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
    ch = db.get_channel(ch_id)
    if ch:
        ch["created_at"] = str(ch.get("created_at", ""))
        ch["updated_at"] = str(ch.get("updated_at", ""))
    return ch


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
def list_channel_videos(channel_id: int, status: str = None, limit: int = 50, offset: int = 0):
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    videos = db.get_videos(channel_id=channel_id, status=status, limit=limit, offset=offset)
    for v in videos:
        for k in ("created_at", "uploaded_at"):
            if v.get(k):
                v[k] = str(v[k])
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

    Reads ``config/canal1_config.py`` (or equivalent) and writes its
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
    """Obtiene estadísticas en tiempo real del canal desde YouTube."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    from pipeline.youtube_stats import YouTubeStatsFetcher

    fetcher = YouTubeStatsFetcher(ch["slug"])
    if not fetcher.authenticate():
        return {"error": "No autenticado", "stats": {}}

    stats = fetcher.get_channel_stats()
    return {"ok": True, "stats": stats}
