"""Scoring de demanda de temas (T1.3 del experimento de recuperación de alcance).

Problema: los temas se elegían por disponibilidad del scraper, no por demanda
real del público. Resultado: vídeos sobre temas que nadie busca → 0 impresiones
(el long-form recibía 3-43 vistas).

Solución: puntuar cada tema candidato con el autocompletado público de YouTube
(0 cuota, sin OAuth) y **ordenar** los candidatos por demanda antes de guionar.
Es un ranking, no un bloqueo: si la red falla, devuelve neutral y se conserva el
orden original (fail-open).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request

from pipeline.topic_dedup import topic_tokens

logger = logging.getLogger("autotube.topic_demand")

SUGGEST_URL = (
    "https://suggestqueries.google.com/complete/search"
    "?client=firefox&ds=yt&hl=es&gl=ES&q={q}"
)
NEUTRAL_SCORE = 0.5
_CACHE_TTL_S = 3600
_cache: dict[str, tuple[float, list[str]]] = {}


def _parse_suggestions(raw: str) -> list[str]:
    """Parsea la respuesta del autocompletado (JSON o JSONP de Google)."""
    if not raw:
        return []
    text = raw.strip()
    # Variante JSONP: window.google.ac.h([...]) o similar.
    if text.startswith("window.") or text.startswith(")"):
        start = text.find("(")
        if start != -1 and text.endswith(")"):
            text = text[start + 1:-1]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(data, list) or len(data) < 2:
        return []
    out: list[str] = []
    for item in data[1] or []:
        if isinstance(item, list) and item:
            out.append(str(item[0]))
        elif isinstance(item, str):
            out.append(item)
    return out


def fetch_suggestions(query: str, timeout: float = 4.0) -> list[str]:
    """Sugerencias públicas de YouTube para ``query`` (0 cuota). ``[]`` si falla."""
    query = (query or "").strip()
    if not query:
        return []
    now = time.time()
    cached = _cache.get(query)
    if cached and now - cached[0] < _CACHE_TTL_S:
        return cached[1]
    try:
        url = SUGGEST_URL.format(q=urllib.parse.quote(query))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        suggestions = _parse_suggestions(raw)
        _cache[query] = (now, suggestions)
        return suggestions
    except Exception as exc:  # noqa: BLE001
        logger.debug("autocomplete unavailable for %r: %s", query[:60], exc)
        return []


def score_demand(topic: str, suggestions: list[str] | None = None) -> float | None:
    """Demanda estimada del tema en [0,1]. ``None`` = no evaluable (fallo de red).

    Heurística: nº de sugerencias que comparten ≥2 tokens significativos con el
    tema, normalizado. Es un proxy del interés de búsqueda, suficiente para
    ORDENAR candidatos (no para bloquear).
    """
    topic = (topic or "").strip()
    if not topic:
        return 0.0
    if suggestions is None:
        suggestions = fetch_suggestions(topic)
        if not suggestions:
            # Sin sugerencias: puede ser 0 demanda real o fallo de red →
            # distinguimos por si la llamada devolvió algo en absoluto.
            return None if _cache.get(topic) is None else 0.0
    tt = topic_tokens(topic)
    if not tt:
        return NEUTRAL_SCORE
    hits = 0
    for s in suggestions:
        st = topic_tokens(s)
        if len(tt & st) >= 2:
            hits += 1
    bonus = 0.15 if any(topic.lower() in s.lower() for s in suggestions) else 0.0
    return min(1.0, hits / 5.0 + bonus)


def rank_labels(labels: list[str], fetch_all: bool = True) -> list[tuple[str, float]]:
    """Ordena etiquetas por demanda (descendente). Neutrales al final, orden estable.

    Fail-open: si el autocompletado no responde, todos reciben ``NEUTRAL_SCORE``
    y se conserva el orden original.
    """
    scored: list[tuple[str, float]] = []
    for label in labels:
        try:
            suggestions = fetch_suggestions(label) if fetch_all else None
            # fetch_suggestions solo cachea en éxito: ausencia en caché = fallo
            # de red → neutral (no penalizamos el tema por un problema técnico).
            failed = fetch_all and label not in _cache
            score = None if failed else score_demand(label, suggestions)
        except Exception as exc:  # noqa: BLE001
            logger.debug("demand scoring failed for %r: %s", (label or "")[:50], exc)
            score = None
        scored.append((label, NEUTRAL_SCORE if score is None else score))
    # sorted es estable: los empates conservan el orden original.
    return sorted(scored, key=lambda x: x[1], reverse=True)
