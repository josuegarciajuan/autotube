"""Seeding de temas por demanda de búsqueda (Fase 2 del experimento).

Invierte el flujo de selección de tema: en vez de scrapear primero y ordenar
después por demanda, siembra CONSULTAS desde el autocompletado público de
YouTube (0 cuota, sin OAuth) a partir de las keywords SEO del canal, las
puntúa y las persiste como candidatos auditables en
``topic_demand_candidates`` (migración v65).

- 0 cuota: usa Google Suggest (``pipeline.topic_demand``).
- Fail-open: sin red devuelve ``[]`` y el pipeline conserva su comportamiento.
- Kill-switch: ``TOPIC_SEEDING_ENABLED=False`` o
  ``system_state["topic_seeding_disabled"]="true"``.

Contrato: ``specs/fase-2-search-first.md``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("autotube.topic_seeding")

STATE_DISABLED_KEY = "topic_seeding_disabled"
_DISABLED_VALUES = {"1", "true", "yes", "on"}


def is_disabled(db, cfg) -> bool:
    """Kill-switch (config del canal > system_state). Fail-open a ``False``."""
    if cfg is not None and not getattr(cfg, "TOPIC_SEEDING_ENABLED", True):
        return True
    try:
        return str(db.get_system_state(STATE_DISABLED_KEY) or "").strip().lower() in _DISABLED_VALUES
    except Exception:  # noqa: BLE001
        return False


def seeds_from_config(cfg) -> list[str]:
    """Consultas semilla del canal (explícitas > SEO > nicho), sin duplicados."""
    seeds: list[str] = []
    for q in (getattr(cfg, "TOPIC_SEED_QUERIES", []) or []):
        if str(q).strip():
            seeds.append(str(q).strip())
    primary = str(getattr(cfg, "SEO_PRIMARY_KEYWORD", "") or "").strip()
    if primary:
        seeds.insert(0, primary)
    for k in (getattr(cfg, "SEO_SECONDARY_KEYWORDS", []) or [])[:5]:
        if str(k).strip():
            seeds.append(str(k).strip())
    if not seeds:
        for k in (getattr(cfg, "NICHE_KEYWORDS_ENG", []) or [])[:5]:
            if str(k).strip():
                seeds.append(str(k).strip())
    # F4: series de contenido → coherencia de audiencia y ocupación sostenida.
    try:
        from pipeline.topic_series import series_seed_queries
        for q in series_seed_queries(cfg):
            if str(q).strip():
                seeds.append(str(q).strip())
    except Exception:  # noqa: BLE001
        pass
    seen: set[str] = set()
    out: list[str] = []
    for s in seeds:
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def collect_candidates(cfg, extra_queries=None) -> list[dict]:
    """Candidatos ``{query, source, demand_score}`` ordenados por demanda.

    Las sugerencias reales de autocompletado implican demanda de búsqueda, así
    que puntúan alto (decayendo con la posición). El propio seed solo entra si
    tiene demanda medible (``score_demand``). Fail-open: sin red devuelve lo que
    se haya podido recolectar (posiblemente vacío).
    """
    try:
        from pipeline.topic_demand import fetch_suggestions, score_demand
    except Exception as exc:  # noqa: BLE001
        logger.debug("topic_seeding: topic_demand import failed: %s", exc)
        return []

    seeds = seeds_from_config(cfg)
    for q in (extra_queries or []):
        if str(q).strip():
            seeds.append(str(q).strip())

    max_q = int(getattr(cfg, "TOPIC_SEED_MAX_QUERIES", 12) or 12)
    min_score = float(getattr(cfg, "TOPIC_SEEDING_MIN_SCORE", 0.0) or 0.0)

    candidates: list[dict] = []
    seen: set[str] = set()
    for seed in seeds:
        suggestions = fetch_suggestions(seed)
        pool = list(suggestions or [])
        seed_score = score_demand(seed, suggestions)
        if seed_score is not None and seed_score > 0:
            pool.append(seed)
        for i, q in enumerate(pool):
            q = " ".join(str(q).split())
            if not q:
                continue
            key = q.lower()
            if key in seen:
                continue
            seen.add(key)
            if q.lower() == seed.lower():
                score = float(seed_score or 0.0)
            else:
                score = max(0.6, 1.0 - i * 0.05)
            if score < min_score:
                continue
            candidates.append({
                "query": q,
                "source": "autocomplete",
                "demand_score": round(score, 3),
            })

    candidates.sort(key=lambda c: c["demand_score"], reverse=True)
    return candidates[:max_q]


def seed_channel(db, channel_id, cfg, extra_queries=None,
                 persist: bool = True) -> list[dict]:
    """Recolecta y persiste candidatos de demanda para un canal.

    Devuelve la lista de candidatos (o ``[]`` en fail-open / kill-switch).
    """
    if not channel_id:
        return []
    if is_disabled(db, cfg):
        return []
    try:
        candidates = collect_candidates(cfg, extra_queries=extra_queries)
    except Exception as exc:  # noqa: BLE001
        logger.warning("topic_seeding: collect failed (ch=%s): %s", channel_id, exc)
        return []
    if candidates and persist:
        try:
            db.save_topic_demand_candidates(channel_id, candidates)
        except Exception as exc:  # noqa: BLE001
            logger.warning("topic_seeding: persist failed (ch=%s): %s", channel_id, exc)
    return candidates
