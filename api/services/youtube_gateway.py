"""Fachada única de interacción con YouTube/Google por canal.

Regla dura
----------
Un canal gestionado (flag DB o registro egress) DEBE usar su gateway. Si el
gateway no está disponible, se lanza ``EgressAgentUnavailableError``. NUNCA se
ejecuta la ruta local para un canal gestionado.

Los canales no gestionados conservan exactamente el comportamiento local actual.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from api.services.egress_client import (
    EgressAgent,
    EgressAgentUnavailableError,
    get_egress_client,
    is_egress_managed,
)

logger = logging.getLogger(__name__)


def client_for(slug: str) -> Optional[EgressAgent]:
    """Cliente del gateway o None si el canal no es gestionado."""
    return get_egress_client(slug)


def managed(slug: str) -> bool:
    return bool(slug) and is_egress_managed(slug)


def _call(slug: str, op: str, **kwargs) -> Optional[dict]:
    """Ejecuta una op en el gateway. ``None`` si el canal no es gestionado."""
    client = client_for(slug)
    if client is None:
        return None
    res = client.api_call(op, {"kwargs": kwargs})
    if not res.get("ok"):
        raise EgressAgentUnavailableError(
            f"gateway {slug}: {op} falló: {res.get('error', 'sin detalle')}"
        )
    return res.get("result", {})


def _local_or_delegate(slug: str, op: str, kwargs: dict,
                       local_fn: Optional[Callable[[], Any]] = None):
    result = _call(slug, op, **kwargs)
    if result is not None:
        return result
    if local_fn is None:
        raise RuntimeError(f"'{op}' sin ruta local definida para {slug}")
    return local_fn()


# ── Mutaciones de vídeo ─────────────────────────────────────────
def set_publish_at(slug: str, video_id: str, publish_at: str,
                   local_fn=None) -> dict:
    return _local_or_delegate(
        slug, "set_publish_at",
        {"video_id": video_id, "publish_at": publish_at}, local_fn,
    )


def set_privacy(slug: str, video_id: str, privacy: str, local_fn=None) -> dict:
    return _local_or_delegate(
        slug, "set_privacy", {"video_id": video_id, "privacy": privacy}, local_fn,
    )


def update_description(slug: str, video_id: str, description: str,
                       local_fn=None) -> dict:
    return _local_or_delegate(
        slug, "update_description",
        {"video_id": video_id, "description": description}, local_fn,
    )


def set_title(slug: str, video_id: str, title: str, local_fn=None) -> dict:
    return _local_or_delegate(
        slug, "set_title", {"video_id": video_id, "title": title}, local_fn,
    )


def set_thumbnail(slug: str, video_id: str, thumbnail_path: str,
                  local_fn=None) -> dict:
    client = client_for(slug)
    if client is not None:
        return client.set_thumbnail(video_id, thumbnail_path)
    if local_fn is None:
        raise RuntimeError(f"set_thumbnail sin ruta local definida para {slug}")
    return local_fn()


def get_video_status(slug: str, video_id: str, local_fn=None) -> dict:
    return _local_or_delegate(
        slug, "get_video_status", {"video_id": video_id}, local_fn,
    )


# ── Playlists / comentarios / canal ─────────────────────────────
def create_playlist(slug: str, title: str, description: str = "",
                    local_fn=None) -> dict:
    return _local_or_delegate(
        slug, "create_playlist", {"title": title, "description": description},
        local_fn,
    )


def list_playlists(slug: str, local_fn=None) -> list:
    result = _call(slug, "list_playlists")
    if result is not None:
        return result
    if local_fn is None:
        raise RuntimeError(f"list_playlists sin ruta local definida para {slug}")
    return local_fn()


def add_video_to_playlist(slug: str, playlist_id: str, video_id: str,
                          local_fn=None) -> dict:
    return _local_or_delegate(
        slug, "add_video_to_playlist",
        {"playlist_id": playlist_id, "video_id": video_id}, local_fn,
    )


def list_comments(slug: str, video_id: str, local_fn=None) -> list:
    result = _call(slug, "list_comments", video_id=video_id)
    if result is not None:
        return result
    if local_fn is None:
        raise RuntimeError(f"list_comments sin ruta local definida para {slug}")
    return local_fn()


def post_comment(slug: str, video_id: str, text: str, local_fn=None) -> dict:
    return _local_or_delegate(
        slug, "post_comment", {"video_id": video_id, "text": text}, local_fn,
    )


def reply_comment(slug: str, parent_id: str, text: str, local_fn=None) -> dict:
    return _local_or_delegate(
        slug, "reply_comment", {"parent_id": parent_id, "text": text}, local_fn,
    )


def get_channel_info(slug: str, local_fn=None) -> dict:
    return _local_or_delegate(slug, "get_channel_info", {}, local_fn)


def update_channel_metadata(slug: str, **kwargs) -> dict:
    return _call(slug, "update_channel_metadata", **kwargs) or {}


# ── Lecturas Google / YouTube ───────────────────────────────────
def fetch_google(slug: str, url: str, timeout: int = 30) -> str:
    """GET de texto a un dominio Google/YouTube desde la IP del canal."""
    client = client_for(slug)
    if client is not None:
        res = client.fetch(url, timeout=timeout)
        if not res.get("ok"):
            raise EgressAgentUnavailableError(
                f"fetch_google {slug} falló: {res.get('error', 'sin detalle')}"
            )
        return (res.get("result") or {}).get("text", "")

    import requests
    resp = requests.get(url, timeout=timeout, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
        "Accept-Language": "es-ES,es;q=0.9",
    })
    resp.raise_for_status()
    return resp.text[:500_000]


def fetch_bytes(slug: str, url: str) -> Optional[bytes]:
    """Descarga binaria (thumbnail servido) desde la IP del canal."""
    client = client_for(slug)
    if client is not None:
        return client.fetch_bytes(url)
    import requests
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            return None
        return resp.content
    except Exception:  # noqa: BLE001
        return None


def classify_video(slug: str, yt_id: str) -> str:
    """Visibilidad externa (0 cuota) desde la IP del canal."""
    client = client_for(slug)
    if client is not None:
        res = client.ytdlp("classify", {"yt_id": yt_id})
        if not res.get("ok"):
            raise EgressAgentUnavailableError(
                f"classify {slug} falló: {res.get('error', 'sin detalle')}"
            )
        return ((res.get("result") or {}).get("visibility") or "unknown")
    from api.services.yt_state_reconciler import classify_video_visibility
    return classify_video_visibility(yt_id)


def rss_public_ids(slug: str, channel_id: str) -> dict:
    """IDs públicos recientes del canal (RSS) desde la IP del canal."""
    client = client_for(slug)
    if client is not None:
        res = client.ytdlp("rss", {"channel_id": channel_id})
        if not res.get("ok"):
            raise EgressAgentUnavailableError(
                f"rss {slug} falló: {res.get('error', 'sin detalle')}"
            )
        return res.get("result") or {}
    from pipeline.youtube_wall_scraper import fetch_channel_public_video_ids
    return fetch_channel_public_video_ids(channel_id)


def watch_status(slug: str, yt_id: str) -> str:
    """Estado de la watch page desde la IP del canal."""
    client = client_for(slug)
    if client is not None:
        res = client.ytdlp("watch_status", {"yt_id": yt_id})
        if not res.get("ok"):
            raise EgressAgentUnavailableError(
                f"watch_status {slug} falló: {res.get('error', 'sin detalle')}"
            )
        return ((res.get("result") or {}).get("status") or "unknown")
    # Camino local solo para canales NO gestionados.
    from scripts.check_video_removals import watch_page_status
    return watch_page_status(yt_id)


def download_clip(slug: str, url: str, section: str, dest_path: str) -> str:
    """Descarga una sección de vídeo desde la IP del canal."""
    client = client_for(slug)
    if client is not None:
        return client.download_clip(url, section, dest_path)

    import subprocess
    from pathlib import Path
    cmd = [
        "yt-dlp", "--download-sections", str(section),
        "-f", "best[height<=720]/best", "--no-playlist",
        "--socket-timeout", "30", "--retries", "5",
        "--fragment-retries", "5", "--force-overwrites", "--no-warnings",
        "-o", dest_path, url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not Path(dest_path).exists():
        raise RuntimeError((proc.stderr or "yt-dlp clip falló")[-500:])
    return dest_path
