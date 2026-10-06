"""Descarga distribuida de un CLIP de vídeo de YouTube (nodos residenciales).

Los shorts de tipo "clip" extraen una sección de un long-form ya publicado. Si
el mp4 local no existe (p. ej. se borró tras subir), la casa descarga la sección
con yt-dlp… y recibe **403** desde la IP del datacenter. Los nodos etiquetados
``ytdlp`` (IP residencial) sí descargan.

Delega en la definición ``autotube-ytdlp-clip``; activación con
``AUTOTUBE_DIST_YTDLP=1``. **Fail-open**: ``None`` ⇒ el llamador usa su ruta local.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFINITION = "autotube-ytdlp-clip"
DEFAULT_TIMEOUT_SEC = 1800


def is_enabled() -> bool:
    """True si la delegación a la flota está activada."""
    return os.getenv("AUTOTUBE_DIST_YTDLP", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def download_clip_dist(
    video_url: str,
    section: str,
    out_path: "str | Path",
    *,
    video_id: str = "",
    timeout_sec: Optional[float] = None,
) -> Optional[Path]:
    """Descarga la sección *section* (p. ej. ``"*12.0-25.0"``) en un nodo residencial.

    Returns the destination path on success, or ``None`` (fail-open).
    """
    if not is_enabled() or not video_url or not section:
        return None

    try:
        from pipeline_dist import dsl_client
    except Exception as exc:  # noqa: BLE001
        logger.debug("ytdlp-clip-dist: dsl_client no disponible (%s)", exc)
        return None

    key = str(video_id or "clip")
    timeout = float(timeout_sec or DEFAULT_TIMEOUT_SEC)
    try:
        eid = dsl_client.submit(
            DEFINITION,
            {"clips": [{"key": key, "url": str(video_url),
                        "videoId": key, "section": str(section)}]},
            label=f"ytdlp-clip:{key}",
            max_inflight=1,
            deadline_sec=int(timeout),
        )
        summary = dsl_client.wait(eid, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("ytdlp-clip-dist: ejecución fallida (%s) — ruta local", exc)
        return None

    if not isinstance(summary, dict) or summary.get("status") != "done":
        logger.warning(
            "ytdlp-clip-dist: estado %s (%s) — ruta local",
            (summary or {}).get("status"), (summary or {}).get("error"),
        )
        return None

    entries = ((summary.get("result") or {}).get("clips")) or []
    if not entries:
        return None
    entry = entries[0] or {}
    out_dir, filename = entry.get("outDir"), entry.get("filename")
    if not out_dir or not filename:
        return None
    src = Path(str(out_dir)) / str(filename)
    try:
        if not src.exists() or src.stat().st_size <= 0:
            logger.warning("ytdlp-clip-dist: artefacto inexistente o vacío (%s)", src)
            return None
        dst = Path(out_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logger.info(
            "ytdlp-clip-dist: clip descargado en la flota (%.1f MB)",
            dst.stat().st_size / (1024 * 1024),
        )
        return dst
    except OSError as exc:
        logger.warning("ytdlp-clip-dist: no se pudo copiar el clip (%s)", exc)
        return None
