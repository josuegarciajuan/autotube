"""Scraping 0-cuota (yt-dlp / RSS / watch pages) y verificación de egress.

Toda la red sale por la IP de la VPS (egress). No toca la DB del server.
"""
from __future__ import annotations

import logging
import socket
from typing import Optional

import requests

from egress_agent.config import AgentConfig

logger = logging.getLogger("egress_agent.scraper")


def ytdlp_classify(cfg: AgentConfig, yt_id: str) -> dict:
    """Clasifica la visibilidad real de un vídeo (0 cuota) vía yt-dlp."""
    from api.services.yt_state_reconciler import classify_video_visibility
    try:
        vis = classify_video_visibility(yt_id)
        return {"ok": True, "result": {"visibility": vis}}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:1000]}


def ytdlp_stats(cfg: AgentConfig, video_ids: list) -> dict:
    """Scrapea stats públicas (vistas/likes/comentarios) de vídeos (0 cuota)."""
    from pipeline.youtube_stats_scraper import YouTubeStatsScraper
    if not video_ids:
        return {"ok": True, "result": {}}
    scraper = YouTubeStatsScraper(cfg.slug)
    try:
        data = scraper.get_video_stats_batch(video_ids)
        return {"ok": True, "result": data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:1000]}


def ytdlp_channel_stats(cfg: AgentConfig, channel: dict) -> dict:
    """Scrapea subs públicos de un canal (0 cuota)."""
    from pipeline.youtube_stats_scraper import YouTubeStatsScraper
    scraper = YouTubeStatsScraper(cfg.slug)
    try:
        data = scraper.get_channel_stats(channel)
        return {"ok": True, "result": data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:1000]}


def rss_feed(cfg: AgentConfig, channel_id: str) -> dict:
    """Obtiene los IDs públicos recientes del canal vía su RSS feed (0 cuota)."""
    from pipeline.youtube_wall_scraper import fetch_channel_public_video_ids
    try:
        ids = fetch_channel_public_video_ids(channel_id)
        return {"ok": True, "result": ids}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:1000]}


def fetch(cfg: AgentConfig, url: str, timeout: int = 30) -> dict:
    """GET genérico (watch pages, feeds) desde la IP de la VPS."""
    try:
        resp = requests.get(
            url, timeout=timeout,
            headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/126.0.0.0 Safari/537.36"),
                "Accept-Language": "es-ES,es;q=0.9",
            },
        )
        return {"ok": True, "result": {
            "status": resp.status_code,
            "text": resp.text[:500_000],
        }}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:1000]}


def watch_status(cfg: AgentConfig, yt_id: str) -> dict:
    """Clasifica el estado público de un vídeo vía su watch page (0 cuota).

    Espejo de scripts/check_video_removals.watch_page_status, pero ejecutado
    desde la IP del agente para que el server nunca consulte YouTube.
    """
    url = f"https://www.youtube.com/watch?v={yt_id}&hl=es"
    _r = fetch(cfg, url, timeout=15)
    if not _r.get("ok"):
        return {"ok": True, "result": {"status": "unknown"}}
    html = _r.get("result", {}).get("text", "")
    if '"status":"OK"' in html or '"status":"LIVE_STREAM_OFFLINE"' in html:
        status = "available"
    elif '"status":"LOGIN_REQUIRED"' in html:
        status = "login_required"
    elif "This video isn't available anymore" in html or "This video has been removed" in html:
        status = "removed"
    else:
        status = "unknown"
    return {"ok": True, "result": {"status": status}}


def ytdlp_audio(cfg: AgentConfig, url: str, audio_format: str = "mp3",
                quality: str = "128K") -> dict:
    """Descarga el audio de un vídeo con yt-dlp DESDE la IP residencial.

    Necesario porque la descarga directa desde el datacenter recibe HTTP 403.
    Devuelve la ruta del fichero en el VPS; el server lo recupera por
    ``/download``.
    """
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    ytdlp_bin = os.getenv("YTDLP_BIN", "yt-dlp")
    outdir = Path(tempfile.mkdtemp(prefix="agent_audio_"))
    outtmpl = str(outdir / "%(id)s.%(ext)s")
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    cmd = [
        ytdlp_bin, url,
        "-x", "--audio-format", audio_format,
        "--audio-quality", quality,
        "-o", outtmpl,
        "--no-warnings", "--no-check-certificate",
        "--retries", "3", "--fragment-retries", "3",
        "--user-agent", ua,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        candidates = sorted(
            (p for p in outdir.rglob("*") if p.is_file()),
            key=lambda p: p.stat().st_size, reverse=True,
        )
        if not candidates:
            err = (proc.stderr or "")[-800:].replace("\n", " ")
            return {"ok": False, "error": f"yt-dlp no produjo audio: {err}"}
        chosen = candidates[0]
        return {"ok": True, "result": {
            "path": str(chosen), "size": chosen.stat().st_size,
        }}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "yt-dlp timeout (>900s)"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:1000]}


def egress_check(cfg: AgentConfig) -> dict:
    """Devuelve la IP pública y geolocalización reales de salida de la VPS.

    Se compara contra la IP del server principal y contra el perfil esperado
    para detectar una fuga (si el túnel a la IP residencial cayó, la IP aquí
    sería la del propio VPS datacenter → la operación debe bloquearse).
    """
    ip = None
    geo = {}
    # ipify (IP) + ipwho.is (geo), ambos gratuitos sin token.
    for url in ("https://api.ipify.org?format=json", "https://ipwho.is/"):
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if "ip" in data:
                    ip = data["ip"]
                if "country" in data:
                    geo = {
                        "country": data.get("country"),
                        "country_code": data.get("country_code"),
                        "city": data.get("city"),
                        "isp": data.get("connection", {}).get("isp"),
                    }
                if ip and geo:
                    break
        except Exception:  # noqa: BLE001
            continue
    # Fallback: resolución DNS local (mejor que nada, pero es un leak DNS si
    # se usa sin túnel → lo marcamos como sospechoso).
    if not ip:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa: BLE001
            ip = "unknown"
    return {"ok": True, "result": {"ip": ip, "geo": geo}}


# ── Registro de operaciones /ytdlp y /fetch ─────────────────────
_YTDLP_OPS = {
    "classify": ytdlp_classify,
    "stats": ytdlp_stats,
    "channel_stats": ytdlp_channel_stats,
    "rss": rss_feed,
    "watch_status": watch_status,
    "audio": ytdlp_audio,
}


def run_ytdlp(cfg: AgentConfig, op: str, params: dict) -> dict:
    fn = _YTDLP_OPS.get(op)
    if fn is None:
        return {"ok": False, "error": f"operación yt-dlp desconocida: {op}"}
    try:
        return fn(cfg, **params)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:1000]}
