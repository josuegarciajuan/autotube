"""Frescura en publicación — refresca título y thumbnail de vídeos que llevan
demasiado tiempo en cola antes de subirlos.

Con la fábrica continua la cola (awaiting_upload) puede acumular vídeos durante
días/semanas: un título/thumbnail generados al crear el vídeo quedan
desactualizados respecto a tendencias cuando finalmente se publica. Este módulo
regenera el título (LLM, reutilizando el guion + keywords del script) y el
thumbnail (ThumbnailMaker v2) justo antes de la subida.

Hook: upload_scheduler.dispatch_due_uploads, antes de despachar un vídeo
antiguo. No bloquea la subida si el refresco falla (fail-open).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("autotube.freshness")


def freshness_threshold_days() -> int:
    """Días en cola a partir de los cuales un vídeo se considera 'stale'."""
    try:
        from config.settings import FRESHNESS_REFRESH_DAYS
        return max(1, int(FRESHNESS_REFRESH_DAYS or 7))
    except Exception:
        return 7


def video_age_days(db, video_id: int) -> float | None:
    """Días transcurridos desde la creación del vídeo, o None si no se pudo."""
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT (julianday('now') - julianday(created_at)) AS age_days "
                "FROM videos WHERE id = ?",
                (video_id,),
            ).fetchone()
        if row and row["age_days"] is not None:
            return float(row["age_days"])
    except Exception:
        pass
    return None


def is_stale(db, video_id: int, max_age_days: int | None = None) -> bool:
    """True si el vídeo lleva más de ``max_age_days`` en la cola."""
    if max_age_days is None:
        max_age_days = freshness_threshold_days()
    age = video_age_days(db, video_id)
    return age is not None and age > max_age_days


def refresh_stale_video(db, video_id: int) -> dict:
    """Regenera título + thumbnail del vídeo si lleva demasiado tiempo en cola.

    Devuelve {'refreshed': bool, 'new_title': str|None, 'thumbnail': bool,
              'reason': str}. Fail-open: nunca lanza.
    """
    v = None
    try:
        v = db.get_video(video_id)
    except Exception:
        pass
    if not v:
        return {"refreshed": False, "reason": "no video", "new_title": None, "thumbnail": False}
    if not is_stale(db, video_id):
        return {"refreshed": False, "reason": "not stale", "new_title": None, "thumbnail": False}

    canal = v.get("canal") or ""
    if not canal:
        return {"refreshed": False, "reason": "no canal", "new_title": None, "thumbnail": False}

    # 1. Cargar script (guion + keywords) para regenerar el título
    guion = ""
    keywords: list = []
    try:
        script = db.get_script(v.get("script_id")) if v.get("script_id") else None
        if script:
            guion = script.get("guion") or ""
            kw_raw = script.get("keywords_json") or "[]"
            if isinstance(kw_raw, str):
                keywords = json.loads(kw_raw) if kw_raw.strip() else []
            else:
                keywords = kw_raw or []
    except Exception as exc:
        logger.debug("Freshness: script load failed for #%d: %s", video_id, exc)
    if not guion:
        return {"refreshed": False, "reason": "no guion", "new_title": None, "thumbnail": False}

    # 2. Título nuevo (LLM) reutilizando el mismo generador de metadata
    new_title = None
    titles: list = []
    try:
        from pipeline.metadata_generator import MetadataGenerator
        from config.config_bridge import get_channel_config
        cfg = get_channel_config(canal)
        meta = MetadataGenerator(cfg).generate({"guion": guion, "keywords": keywords})
        new_title = (meta or {}).get("selected_title")
        titles = (meta or {}).get("titles") or []
    except Exception as exc:
        logger.warning("[%s] Freshness: título no regenerado (#%d): %s", canal, video_id, exc)
    if not new_title:
        return {"refreshed": False, "reason": "title regen failed", "new_title": None, "thumbnail": False}

    # 3. Persistir nuevo título
    try:
        db.update_video(
            video_id,
            titulo_final=new_title,
            title_options=json.dumps(titles, ensure_ascii=False),
        )
        logger.info("[%s] Frescura: vídeo #%d título regenerado → %s", canal, video_id, new_title[:60])
    except Exception as exc:
        logger.warning("[%s] Frescura: no se pudo persistir título (#%d): %s", canal, video_id, exc)

    # 4. Thumbnail nuevo (el thumbnail incrusta el texto del título)
    thumb_ok = False
    try:
        import asyncio
        from api.services.thumbnail_service import regenerate_thumbnail_for_video
        res = asyncio.run(regenerate_thumbnail_for_video(video_id))
        thumb_ok = bool(res)
    except Exception as exc:
        logger.warning("[%s] Frescura: thumbnail no regenerado (#%d): %s", canal, video_id, exc)

    return {"refreshed": True, "new_title": new_title, "thumbnail": thumb_ok,
            "reason": "refreshed"}
