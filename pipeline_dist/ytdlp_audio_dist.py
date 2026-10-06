"""Descarga distribuida de audio de YouTube en nodos con IP residencial.

La casa (datacenter) recibe **HTTP 403 / "Sign in"** de YouTube al descargar
media con yt-dlp. Los nodos de la flota etiquetados ``ytdlp`` (PC personales con
IP residencial, o VPS con egress residencial) sí descargan sin problema.

Esta capa delega la descarga+extracción de audio a la definición distribuida
``autotube-ytdlp-audio`` (SuperServer) y trae el mp3 al hogar para transcribir.

Activación: ``AUTOTUBE_DIST_YTDLP=1`` (por defecto OFF). **Fail-open**: ante
cualquier fallo (flag off, motor no disponible, sin nodo elegible, timeout) se
devuelve ``None`` y el llamador cae a la ruta local/egress de siempre.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFINITION = "autotube-ytdlp-audio"
DEFAULT_TIMEOUT_SEC = 1800


def is_enabled() -> bool:
    """True si la delegación a la flota está activada."""
    return os.getenv("AUTOTUBE_DIST_YTDLP", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def download_audio_dist(
    video_url: str,
    video_id: str,
    out_path: "str | Path",
    *,
    timeout_sec: Optional[float] = None,
) -> Optional[Path]:
    """Descarga el audio de *video_url* en un nodo residencial de la flota.

    Returns the destination path on success, or ``None`` (fail-open) so the
    caller can use its local/egress fallback.
    """
    if not is_enabled() or not video_url or not video_id:
        return None

    try:
        from pipeline_dist import dsl_client
    except Exception as exc:  # noqa: BLE001 — sin puente => ruta local
        logger.debug("ytdlp-dist: dsl_client no disponible (%s)", exc)
        return None

    timeout = float(timeout_sec or DEFAULT_TIMEOUT_SEC)
    try:
        eid = dsl_client.submit(
            DEFINITION,
            {"urls": [{"key": str(video_id), "url": str(video_url), "videoId": str(video_id)}]},
            label=f"ytdlp-audio:{video_id}",
            max_inflight=1,
            deadline_sec=int(timeout),
        )
        summary = dsl_client.wait(eid, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("ytdlp-dist: ejecución fallida (%s) — se usará la ruta local", exc)
        return None

    if not isinstance(summary, dict) or summary.get("status") != "done":
        logger.warning(
            "ytdlp-dist: estado %s (%s) — se usará la ruta local",
            (summary or {}).get("status"), (summary or {}).get("error"),
        )
        return None

    entries = ((summary.get("result") or {}).get("audios")) or []
    if not entries:
        logger.warning("ytdlp-dist: sin audios en el resultado — se usará la ruta local")
        return None

    entry = entries[0] or {}
    out_dir, filename = entry.get("outDir"), entry.get("filename")
    if not out_dir or not filename:
        return None
    src = Path(str(out_dir)) / str(filename)
    try:
        if not src.exists() or src.stat().st_size <= 0:
            logger.warning("ytdlp-dist: artefacto inexistente o vacío (%s)", src)
            return None
        dst = Path(out_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logger.info(
            "ytdlp-dist: audio %s descargado en la flota (%.1f MB)",
            video_id, dst.stat().st_size / (1024 * 1024),
        )
        return dst
    except OSError as exc:
        logger.warning("ytdlp-dist: no se pudo copiar el audio (%s)", exc)
        return None
