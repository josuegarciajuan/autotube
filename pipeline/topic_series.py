"""Series de contenido por canal (F4).

Una serie es un conjunto de entregas complementarias dentro de un subnicho del
canal. Ayuda a dos cosas:

* **Coherencia de audiencia**: el espectador sabe qué esperar, así que la señal
  de YouTube no se diluye con temas ajenos.
* **Descubrimiento**: cada entrega comparte semillas de búsqueda, de modo que la
  serie ocupa consultas reales de forma sostenida en vez de un pico aislado.

Definición en la config del canal (``CONTENT_SERIES``), nunca en código::

    CONTENT_SERIES = [
        {
            "name": "Expediciones polares",
            "seed_queries": ["expediciones polares", "naufragio en la antartida"],
            "episodes": ["La expedición perdida de ...", ...],
        },
    ]

Fail-open: sin series configuradas devuelve listas vacías.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("autotube.topic_series")


def _iter_series(cfg):
    for s in (getattr(cfg, "CONTENT_SERIES", []) or []):
        if isinstance(s, dict) and s.get("name"):
            yield s
        elif isinstance(s, str) and s.strip():
            yield {"name": s.strip(), "seed_queries": [s.strip()]}


def series_seed_queries(cfg, limit: int = 12) -> list[str]:
    """Consultas semilla derivadas de las series (sin duplicados)."""
    out: list[str] = []
    seen: set[str] = set()
    for s in _iter_series(cfg):
        for q in (s.get("seed_queries") or []) + [s.get("name")]:
            text = " ".join(str(q or "").split())
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= int(limit):
                return out
    return out


def series_episode_hints(cfg, limit: int = 12) -> list[dict]:
    """Entregas planificadas ``{series, episode}`` (contexto para el LLM)."""
    out: list[dict] = []
    for s in _iter_series(cfg):
        for ep in (s.get("episodes") or []):
            text = " ".join(str(ep or "").split())
            if text:
                out.append({"series": s.get("name"), "episode": text})
            if len(out) >= int(limit):
                return out
    return out


def series_names(cfg) -> list[str]:
    return [s.get("name") for s in _iter_series(cfg)]
