"""Operaciones de la YouTube Data API ejecutadas desde la VPS del agente.

IMPORTANTE (aislamiento): este módulo NO toca la base de datos del server
principal. Solo realiza operaciones de RED hacia YouTube desde la IP de la VPS
(egress). El bookkeeping de la DB (estado del vídeo, cuota, spacing, strikes)
lo hace el server principal con el RESULTADO devuelto por el agente.

Reutiliza ``pipeline.youtube_uploader.YouTubeUploader`` solo para la autenticación
y el servicio googleapiclient (token refresh incluido); la subida se hace aquí
con el servicio para no arrastrar las side-effects de DB del uploader.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from googleapiclient.http import MediaFileUpload

from egress_agent.config import AgentConfig

logger = logging.getLogger("egress_agent.youtube_api")

_SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def _service(cfg: AgentConfig, api: str = "youtube", version: str = "v3"):
    """Construye un servicio googleapiclient autenticado (token local del agente).

    El refresh del token sale por la red de la VPS (egress correcto).
    """
    from pipeline.youtube_uploader import YouTubeUploader

    uploader = YouTubeUploader(account_name=cfg.slug, channel_slug=cfg.slug)
    # _get_service() autentica (carga pickle + refresh) y construye youtube v3.
    if api == "youtube" and version == "v3":
        return uploader._get_service()

    # Para youtubeAnalytics, reutilizamos las credenciales refrescadas.
    from googleapiclient.discovery import build
    import httplib2

    creds = uploader._credentials
    http = creds.authorize(httplib2.Http())
    return build(api, version, credentials=creds, http=http, cache_discovery=False)


def get_channel_info(cfg: AgentConfig) -> dict:
    svc = _service(cfg)
    resp = svc.channels().list(part="snippet,statistics", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        return {"ok": True, "result": None}
    it = items[0]
    return {
        "ok": True,
        "result": {
            "id": it.get("id"),
            "title": it["snippet"].get("title", ""),
            "handle": it["snippet"].get("customUrl", ""),
            "subscriber_count": it.get("statistics", {}).get("subscriberCount", "0"),
        },
    }


def upload_video(cfg: AgentConfig, video_path: str, meta: dict) -> dict:
    """Sube un vídeo desde la VPS. Devuelve {ok, video_id, url, warnings|error}.

    ``meta`` acepta: title, description, tags, category_id, language, privacy,
    publish_at, self_declared_made_for_kids, embeddable, public_stats_viewable.
    """
    svc = _service(cfg)
    path = Path(video_path)
    if not path.exists():
        return {"ok": False, "error": f"Vídeo no encontrado: {video_path}"}

    tags = [str(t)[:30] for t in (meta.get("tags") or [])][:60]
    body = {
        "snippet": {
            "title": str(meta.get("title", ""))[:100],
            "description": str(meta.get("description", ""))[:5000],
            "tags": tags,
            "categoryId": str(meta.get("category_id", "22")),
            "defaultLanguage": meta.get("language", "es"),
            "defaultAudioLanguage": meta.get("language", "es"),
        },
        "status": {
            "privacyStatus": "private" if meta.get("publish_at") else meta.get("privacy", "public"),
            "selfDeclaredMadeForKids": bool(meta.get("self_declared_made_for_kids", False)),
            "embeddable": bool(meta.get("embeddable", True)),
            "publicStatsViewable": bool(meta.get("public_stats_viewable", True)),
        },
    }
    if meta.get("publish_at"):
        body["status"]["publishAt"] = meta["publish_at"]

    media = MediaFileUpload(str(path), mimetype="video/*", chunksize=256 * 1024, resumable=True)
    request = svc.videos().insert(part="snippet,status", body=body, media_body=media)
    response = _resumable(request)
    if not response:
        return {"ok": False, "error": "Subida resumable no devolvió respuesta"}

    video_id = response.get("id", "")
    if not video_id:
        return {"ok": False, "error": "YouTube no devolvió video_id"}

    result = {"ok": True, "video_id": video_id,
              "url": f"https://www.youtube.com/watch?v={video_id}", "warnings": []}

    thumb = meta.get("thumbnail_path")
    if thumb and Path(thumb).exists():
        if not _set_thumbnail(svc, video_id, thumb):
            result["warnings"].append({"type": "thumbnail", "field": "thumbnail",
                                       "reason": "Thumbnail upload failed", "ready": False})
    return result


def set_thumbnail(cfg: AgentConfig, video_id: str, thumb_path: str) -> dict:
    svc = _service(cfg)
    if not Path(thumb_path).exists():
        return {"ok": False, "error": f"Thumbnail no existe: {thumb_path}"}
    ok = _set_thumbnail(svc, video_id, thumb_path)
    return {"ok": ok, "result": ok}


def _set_thumbnail(svc, video_id: str, thumb_path: str) -> bool:
    try:
        svc.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumb_path), mimetype="image/jpeg"),
        ).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("thumbnail set failed for %s: %s", video_id, exc)
        return False


def _resumable(request, timeout_retries: int = 8) -> Optional[dict]:
    """Ejecuta un request resumable con reintentos y backoff (sin cuota extra local)."""
    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
        except Exception as exc:  # noqa: BLE001
            # googleapiclient no expone de forma trivial el quitarse el 403 de
            # cuota de forma aislada aquí; re-intentamos por transitoriedad.
            logger.warning("resumable chunk error: %s", exc)
            time.sleep(2)
            timeout_retries -= 1
            if timeout_retries <= 0:
                raise
    return response


def set_publish_at(cfg: AgentConfig, video_id: str, publish_at: str) -> dict:
    svc = _service(cfg)
    body = {"id": video_id, "status": {"privacyStatus": "private", "publishAt": publish_at}}
    svc.videos().update(part="status", body=body).execute()
    return {"ok": True, "result": {"updated": True, "publish_at": publish_at}}


def set_privacy(cfg: AgentConfig, video_id: str, privacy: str) -> dict:
    if privacy not in ("public", "unlisted", "private"):
        return {"ok": False, "error": f"privacy inválido: {privacy}"}
    svc = _service(cfg)
    svc.videos().update(part="status", body={"id": video_id, "status": {"privacyStatus": privacy}}).execute()
    return {"ok": True, "result": {"updated": True, "privacy": privacy}}


def update_description(cfg: AgentConfig, video_id: str, description: str) -> dict:
    svc = _service(cfg)
    item = svc.videos().list(part="snippet", id=video_id).execute().get("items", [])
    if not item:
        return {"ok": False, "error": f"video {video_id} no encontrado"}
    current_title = item[0]["snippet"].get("title", "")
    category_id = item[0]["snippet"].get("categoryId", "22")
    svc.videos().update(
        part="snippet",
        body={"id": video_id, "snippet": {"title": current_title,
                                          "description": description[:5000],
                                          "categoryId": category_id}},
    ).execute()
    return {"ok": True, "result": {"updated": True}}


# ── Registro de operaciones genéricas para /api/call ────────────
_OP_REGISTRY = {
    "get_channel_info": get_channel_info,
    "set_publish_at": set_publish_at,
    "set_privacy": set_privacy,
    "update_description": update_description,
}


def run_api_operation(cfg: AgentConfig, op: str, params: dict) -> dict:
    if op == "upload":
        return upload_video(cfg, params.get("video_path", ""), params.get("meta", {}))
    fn = _OP_REGISTRY.get(op)
    if fn is None:
        return {"ok": False, "error": f"operación desconocida: {op}"}
    try:
        return fn(cfg, **params.get("kwargs", {}))
    except Exception as exc:  # noqa: BLE001
        logger.exception("api op %s failed", op)
        return {"ok": False, "error": str(exc)[:1000]}
