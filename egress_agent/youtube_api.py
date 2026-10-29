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
import os
import time
from datetime import datetime, timedelta
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

# Timeout de socket del transporte googleapiclient (lecturas de Analytics).
_GOOGLE_HTTP_TIMEOUT_S = int(os.environ.get("GOOGLE_HTTP_TIMEOUT_S", "30"))


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
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    creds = uploader._credentials
    # google-auth moderno NO expone credentials.authorize(); el wrapper válido
    # es AuthorizedHttp. googleapiclient rechaza `http` + `credentials` juntos:
    # se pasa SOLO el transporte autorizado (con timeout de socket acotado).
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=_GOOGLE_HTTP_TIMEOUT_S))
    return build(api, version, http=http, cache_discovery=False)


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


def _iso_duration_to_sec(dur: str) -> int:
    """Convierte ISO 8601 (PT#H#M#S) a segundos."""
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", dur or "")
    if not m:
        return 0
    h, mn, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mn * 60 + s


# ── Playlists ──────────────────────────────────────────────────
def create_playlist(cfg: AgentConfig, title: str, description: str = "") -> dict:
    svc = _service(cfg)
    body = {
        "snippet": {"title": str(title)[:150], "description": str(description)[:500]},
        "status": {"privacyStatus": "private"},
    }
    resp = svc.playlists().insert(part="snippet,status", body=body).execute()
    return {"ok": True, "result": {"playlist_id": resp.get("id"),
                                   "title": resp.get("snippet", {}).get("title", "")}}


def add_video_to_playlist(cfg: AgentConfig, playlist_id: str, video_id: str) -> dict:
    svc = _service(cfg)
    body = {"snippet": {"playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
    svc.playlistItems().insert(part="snippet", body=body).execute()
    return {"ok": True, "result": {"added": True}}


def list_playlists(cfg: AgentConfig) -> dict:
    svc = _service(cfg)
    resp = svc.playlists().list(part="snippet,contentDetails", mine=True, maxResults=50).execute()
    items = [{"id": i["id"], "title": i.get("snippet", {}).get("title", ""),
              "item_count": i.get("contentDetails", {}).get("itemCount", 0)}
             for i in resp.get("items", [])]
    return {"ok": True, "result": items}


# ── Comentarios (Data API) ─────────────────────────────────────
def post_comment(cfg: AgentConfig, video_id: str, text: str) -> dict:
    svc = _service(cfg)
    body = {"snippet": {"videoId": video_id,
                        "topLevelComment": {"snippet": {"textOriginal": str(text)[:5000]}}}}
    resp = svc.commentThreads().insert(part="snippet", body=body).execute()
    return {"ok": True, "result": {"comment_id": resp.get("id")}}


def reply_comment(cfg: AgentConfig, parent_id: str, text: str) -> dict:
    svc = _service(cfg)
    body = {"snippet": {"parentId": parent_id, "textOriginal": str(text)[:5000]}}
    resp = svc.comments().insert(part="snippet", body=body).execute()
    return {"ok": True, "result": {"comment_id": resp.get("id")}}


def list_comments(cfg: AgentConfig, video_id: str) -> dict:
    svc = _service(cfg)
    resp = svc.commentThreads().list(part="snippet", videoId=video_id, maxResults=50).execute()
    items = [{"id": i["id"],
              "text": i["snippet"]["topLevelComment"]["snippet"].get("textOriginal", ""),
              "author": i["snippet"]["topLevelComment"]["snippet"].get("authorDisplayName", "")}
             for i in resp.get("items", [])]
    return {"ok": True, "result": items}


# ── Metadata (videos.update) ───────────────────────────────────
def update_video_metadata(cfg: AgentConfig, video_id: str, title: str = None,
                          description: str = None, tags: list = None,
                          category_id: str = None) -> dict:
    svc = _service(cfg)
    item = svc.videos().list(part="snippet", id=video_id).execute().get("items", [])
    if not item:
        return {"ok": False, "error": f"video {video_id} no encontrado"}
    sn = item[0]["snippet"]
    new = {"id": video_id, "snippet": {
        "title": (str(title) if title is not None else sn.get("title", ""))[:100],
        "description": (str(description) if description is not None else sn.get("description", ""))[:5000],
        "categoryId": str(category_id if category_id is not None else sn.get("categoryId", "22")),
    }}
    if tags is not None:
        new["snippet"]["tags"] = [str(t)[:30] for t in tags][:60]
    svc.videos().update(part="snippet", body=new).execute()
    return {"ok": True, "result": {"updated": True}}


# ── Channel metadata (channels.update) ─────────────────────────
def update_channel_metadata(cfg: AgentConfig, description: str = None,
                            keywords: list = None, country: str = None,
                            language: str = None) -> dict:
    svc = _service(cfg)
    item = svc.channels().list(part="snippet,brandingSettings", mine=True).execute().get("items", [])
    if not item:
        return {"ok": False, "error": "no channel para el token"}
    ch = item[0]
    sn = dict(ch.get("snippet", {}))
    if description is not None:
        sn["description"] = str(description)[:5000]
    if keywords is not None:
        sn["tags"] = list(keywords)[:500]
    if country is not None or language is not None:
        sn["country"] = str(country) if country is not None else sn.get("country", "")
        sn["defaultLanguage"] = str(language) if language is not None else sn.get("defaultLanguage", "")
    svc.channels().update(part="snippet", body={"id": ch["id"], "snippet": sn}).execute()
    return {"ok": True, "result": {"updated": True}}


# ── collect_stats (fetch crudo, sin DB) ────────────────────────
def _analytics_rows(asvc, ids: list, days: int, errors: dict) -> dict:
    """Analytics por contenido (long-forms y shorts) en lotes de 200.

    Usa la MISMA ventana y métricas que el camino local
    (`pipeline/youtube_stats.py::get_all_videos_analytics`) para que el
    resultado de la flota sea funcionalmente equivalente.
    """
    out: dict = {}
    if not ids:
        return out
    start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    end_date = datetime.utcnow().strftime("%Y-%m-%d")
    metrics = (
        "estimatedMinutesWatched,averageViewDuration,subscribersGained,"
        "averageViewPercentage,views,likes,comments"
    )
    for i in range(0, len(ids), 200):
        batch = ids[i:i + 200]
        try:
            resp = asvc.reports().query(
                ids="channel==MINE",
                startDate=start_date,
                endDate=end_date,
                metrics=metrics,
                dimensions="video",
                filters="video==" + ",".join(batch),
                maxResults=200,
            ).execute()
            for row in resp.get("rows", []) or []:
                out[row[0]] = {
                    "estimatedMinutesWatched": float(row[1] or 0),
                    "averageViewDuration": float(row[2] or 0),
                    "subscribersGained": int(float(row[3] or 0)),
                    "averageViewPercentage": float(row[4] or 0),
                    "analyticsViews": int(float(row[5] or 0)),
                    "likes": int(float(row[6] or 0)),
                    "comments": int(float(row[7] or 0)),
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("collect_stats analytics batch failed: %s", exc)
            errors["analytics"] = str(exc)[:300]
    return out


def collect_stats(cfg: AgentConfig, video_ids: list = None,
                  short_ids: list = None, days: int = 365,
                  deep: bool = False) -> dict:
    """Recolecta stats crudas desde la IP del agente (videos + shorts + canal).

    NO escribe en ninguna DB: devuelve el payload para que el server principal
    lo almacene en su DB. Cubre el 'Recolectar stats' de un canal gestionado.

    v2 (oct 2026): incluye shorts y métricas Analytics por contenido + serie
    diaria, con la misma ventana (365d) que el camino local. Antes pedía la
    métrica inválida `viewsEstimatedMinutes` (400) y no cubría shorts.
    """
    svc = _service(cfg)
    result = {
        "version": 2,
        "videos": {},
        "shorts": {},
        "channel": {},
        "watch_time_min": None,
        "analytics": {},
        "daily": [],
        "errors": {},
    }

    def _fetch_public(ids: list, target: dict, label: str) -> None:
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            try:
                resp = svc.videos().list(part="statistics,contentDetails",
                                         id=",".join(batch)).execute()
            except Exception as exc:  # noqa: BLE001
                logger.warning("collect_stats %s batch failed: %s", label, exc)
                result["errors"][label] = str(exc)[:300]
                continue
            for it in resp.get("items", []):
                st = it.get("statistics", {})
                cd = it.get("contentDetails", {})
                target[it["id"]] = {
                    "views": int(st.get("viewCount", 0) or 0),
                    "likes": int(st.get("likeCount", 0) or 0),
                    "comments": int(st.get("commentCount", 0) or 0),
                    "duration_sec": _iso_duration_to_sec(cd.get("duration", "")),
                }

    video_ids = [v for v in (video_ids or []) if v]
    short_ids = [s for s in (short_ids or []) if s]
    _fetch_public(video_ids, result["videos"], "videos")
    _fetch_public(short_ids, result["shorts"], "shorts")

    try:
        cresp = svc.channels().list(part="statistics,snippet", mine=True).execute()
        c = cresp["items"][0]
        cs = c.get("statistics", {})
        result["channel"] = {
            "subscribers": int(cs.get("subscriberCount", 0) or 0),
            "total_views": int(cs.get("viewCount", 0) or 0),
            "video_count": int(cs.get("videoCount", 0) or 0),
            "title": c.get("snippet", {}).get("title", ""),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("collect_stats channel failed: %s", exc)
        result["errors"]["channel"] = str(exc)[:300]

    try:
        asvc = _service(cfg, "youtubeAnalytics", "v2")
        # Watch-time acumulado de canal en la MISMA ventana móvil que local.
        start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        end_date = datetime.utcnow().strftime("%Y-%m-%d")
        r = asvc.reports().query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics="estimatedMinutesWatched",
        ).execute()
        rows = r.get("rows", []) or []
        if rows:
            result["watch_time_min"] = float(rows[0][0] or 0)

        # Serie diaria (paginada: 200 filas por página).
        start_index = 1
        by_date: dict = {}
        while True:
            dr = asvc.reports().query(
                ids="channel==MINE",
                startDate=start_date,
                endDate=end_date,
                metrics="estimatedMinutesWatched,subscribersGained",
                dimensions="day",
                sort="day",
                maxResults=200,
                startIndex=start_index,
            ).execute()
            drows = dr.get("rows", []) or []
            for row in drows:
                by_date[str(row[0])] = {
                    "date": row[0],
                    "estimatedMinutesWatched": float(row[1] or 0),
                    "subscribersGained": int(float(row[2] or 0)),
                }
            if len(drows) < 200:
                break
            start_index += 200
        result["daily"] = [by_date[d] for d in sorted(by_date)]

        # Analytics por contenido: long-forms + shorts en el mismo pool.
        analytics_ids = list(dict.fromkeys(video_ids + short_ids))
        result["analytics"] = _analytics_rows(
            asvc, analytics_ids, days, result["errors"]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("collect_stats analytics failed: %s", exc)
        result["errors"]["analytics"] = str(exc)[:300]

    return {"ok": True, "result": result}


# ── Registro de operaciones genéricas para /api/call ────────────
_OP_REGISTRY = {
    "get_channel_info": get_channel_info,
    "set_publish_at": set_publish_at,
    "set_privacy": set_privacy,
    "update_description": update_description,
    "create_playlist": create_playlist,
    "add_video_to_playlist": add_video_to_playlist,
    "list_playlists": list_playlists,
    "post_comment": post_comment,
    "reply_comment": reply_comment,
    "list_comments": list_comments,
    "update_video_metadata": update_video_metadata,
    "update_channel_metadata": update_channel_metadata,
    "collect_stats": collect_stats,
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
