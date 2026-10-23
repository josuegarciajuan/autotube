"""Ranking de temas multi-señal «curiosidad oscura» (Fase 4).

Ordena candidatos **ya elegibles** combinando señales separadas y auditables:

* ``demand``    — demanda estimada (autocompletado / autocomplete fallback)
* ``niche``     — afinidad de nicho (proxy de ``niche_guard``)
* ``editorial`` — tensión narrativa (``editorial_policy.editorial_score``)
* ``visual``    — potencial visual (lo que se puede mostrar en miniatura)
* ``novelty``   — diferencia frente a los temas recientes del canal
* ``source``    — calidad/disponibilidad de la fuente

Invariantes:

* **Ordena, no filtra.** Nicho, content_safety, respaldo factual y demanda
  siguen siendo gates independientes (el llamador los aplica antes/después).
* **Determinista.** Mismas entradas → mismo orden (sin azar).
* **Fail-open.** Ante cualquier error o dato ausente se conserva el orden
  original; ninguna excepción debe romper la generación.
* **Sin hardcoding.** Nada de ramas por slug o channel_id.
* **Trazabilidad.** Cada candidato devuelve su desglose de señales para
  persistirlo en ``content_selection_provenance``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pipeline.editorial_policy import (
    build_policy,
    classify_tension,
    dominant_tension,
    editorial_label,
    editorial_score,
    pilot_decision,
)
from pipeline.niche_guard import niche_fit_score
from pipeline.title_tokens import normalize
from pipeline.topic_dedup import topic_tokens

STATE_DISABLED_KEY = "topic_ranking_disabled"
_DISABLED_VALUES = {"1", "true", "yes", "on"}

_NUMBER_RE = re.compile(r"\b\d{1,4}\b")
_PROPER_RE = re.compile(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,}")


@dataclass
class RankedTopic:
    """Candidato ordenado con su desglose auditable."""

    label: str
    score: float
    signals: dict = field(default_factory=dict)
    editorial_label: str = ""
    pilot_group: str = ""
    reason: str = ""
    source_item: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "score": self.score,
            "signals": dict(self.signals),
            "editorial_label": self.editorial_label,
            "pilot_group": self.pilot_group,
            "reason": self.reason,
        }


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _weights(cfg) -> dict[str, float]:
    from config import defaults

    raw = getattr(cfg, "TOPIC_RANKING_WEIGHTS", None)
    if not isinstance(raw, dict):
        raw = getattr(defaults, "TOPIC_RANKING_WEIGHTS", {})
    weights = {}
    for key, value in (raw or {}).items():
        try:
            weights[str(key)] = max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    # El canal gobierna la intensidad editorial con su propio knob.
    try:
        weights["editorial"] = max(0.0, float(getattr(
            cfg, "EDITORIAL_CURIOSITY_WEIGHT",
            getattr(defaults, "EDITORIAL_CURIOSITY_WEIGHT", 0.25),
        )))
    except (TypeError, ValueError):
        pass
    return weights


def visual_potential(text: str, cfg) -> float:
    """Proxy de «se puede mostrar»: pistas léxicas, cifras y nombres propios."""
    from config import defaults

    raw_text = str(text or "")
    if not raw_text.strip():
        return 0.0
    hints = getattr(cfg, "TOPIC_RANKING_VISUAL_HINTS", None)
    if hints is None:
        hints = getattr(defaults, "TOPIC_RANKING_VISUAL_HINTS", [])
    norm = normalize(raw_text)
    hits = sum(1 for hint in (hints or []) if normalize(str(hint)) in norm)
    numbers = len(_NUMBER_RE.findall(raw_text))
    proper = len(_PROPER_RE.findall(raw_text))
    length = min(len(raw_text), 240) / 240.0
    score = (
        0.30 * min(hits, 3) / 3.0
        + 0.20 * min(numbers, 3) / 3.0
        + 0.20 * min(proper, 3) / 3.0
        + 0.30 * length
    )
    return round(_clamp(score), 4)


def source_quality(item: dict) -> float:
    """Calidad/disponibilidad de fuente: URL + cuerpo suficiente."""
    text = str(item.get("text") or "")
    has_url = bool(str(item.get("url") or "").strip())
    if not text and not has_url:
        return 0.2  # candidato de demanda pura
    score = 0.3
    if has_url:
        score += 0.3
    if len(text) >= 120:
        score += 0.2
    if len(text) >= 400:
        score += 0.2
    return round(_clamp(score), 4)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def novelty_score(text: str, recent_texts: list[str]) -> tuple[float, float]:
    """``(novedad, similitud_máxima)`` frente a los temas recientes."""
    tokens = topic_tokens(text or "")
    if not tokens or not recent_texts:
        return 1.0, 0.0
    worst = 0.0
    for recent in recent_texts:
        rt = topic_tokens(recent or "")
        worst = max(worst, _jaccard(tokens, rt))
    return round(_clamp(1.0 - worst), 4), round(worst, 4)


def _recent_tensions(recent_texts: list[str], cfg, limit: int) -> list[str]:
    out: list[str] = []
    for text in (recent_texts or [])[: max(0, limit)]:
        tension = dominant_tension(text, cfg)
        if tension:
            out.append(tension)
    return out


def rank_candidates(
    items: list[dict],
    *,
    cfg,
    demand_scores: dict[str, float] | None = None,
    recent_texts: list[str] | None = None,
    day_key: str = "",
    channel_slug: str = "",
    route: str = "",
) -> list[RankedTopic]:
    """Ordena *items* (dicts de contenido) por score compuesto descendente.

    Nunca lanza: si algo falla devuelve el orden original con scores neutros.
    """
    items = list(items or [])
    if not items:
        return []

    try:
        weights = _weights(cfg)
        policy = build_policy(cfg)
        demand_scores = demand_scores or {}
        recent_texts = list(recent_texts or [])
        diversity_window = max(1, int(getattr(policy, "diversity_min", 2)))
        recent_tensions = _recent_tensions(recent_texts, cfg, diversity_window)
        repeat_penalty = 0.0
        try:
            from config import defaults
            repeat_penalty = float(getattr(
                cfg, "TOPIC_RANKING_REPEAT_PENALTY",
                getattr(defaults, "TOPIC_RANKING_REPEAT_PENALTY", 0.20),
            ))
        except (TypeError, ValueError):
            repeat_penalty = 0.20

        total_weight = sum(weights.values()) or 1.0
        ranked: list[RankedTopic] = []
        for index, item in enumerate(items):
            label = " ".join(str(
                item.get("title") or item.get("text") or ""
            ).split())[:240]
            demand = _clamp(float(
                demand_scores.get(
                    (item.get("title") or item.get("text") or "")[:120],
                    item.get("demand_score", item.get("score", 0.5)) or 0.0,
                )
            ))
            niche = _clamp(float(niche_fit_score(label, cfg)))
            ed_score = _clamp(editorial_score(label, cfg))
            visual = visual_potential(label, cfg)
            novelty, max_similarity = novelty_score(label, recent_texts)
            source = source_quality(item)

            signals = {
                "demand": round(demand, 4),
                "niche": round(niche, 4),
                "editorial": round(ed_score, 4),
                "visual": round(visual, 4),
                "novelty": round(novelty, 4),
                "source": round(source, 4),
                "similarity_max": max_similarity,
                "tension": dominant_tension(label, cfg),
            }
            composite = (
                weights.get("demand", 0.0) * demand
                + weights.get("niche", 0.0) * niche
                + weights.get("editorial", 0.0) * ed_score
                + weights.get("visual", 0.0) * visual
                + weights.get("novelty", 0.0) * novelty
                + weights.get("source", 0.0) * source
            ) / total_weight

            # Diversidad: no repetir el tipo de tensión dominante dentro de la
            # ventana configurada (penaliza, nunca veta).
            repeat = bool(
                signals["tension"] and signals["tension"] in recent_tensions
            )
            if repeat:
                composite -= repeat_penalty
            score = round(_clamp(composite), 4)

            reason_bits = [
                f"rank={score:.2f}",
                f"demanda={demand:.2f}",
                f"nicho={niche:.2f}",
                f"editorial={ed_score:.2f}"
                + (f"({signals['tension']})" if signals["tension"] else ""),
                f"visual={visual:.2f}",
                f"novedad={novelty:.2f}",
                f"fuente={source:.2f}",
            ]
            if repeat:
                reason_bits.append(f"repeticion=-{repeat_penalty:.2f}")
            if route:
                reason_bits.append(f"ruta={route}")

            ranked.append(RankedTopic(
                label=label,
                score=score,
                signals=signals,
                editorial_label=editorial_label(label, cfg),
                pilot_group=pilot_decision(channel_slug, day_key, cfg) if channel_slug else "",
                reason="; ".join(reason_bits)[:500],
                source_item=item,
            ))

        # sorted es estable: los empates conservan el orden original.
        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked
    except Exception:  # noqa: BLE001 — fail-open
        return [
            RankedTopic(
                label=" ".join(str(i.get("title") or i.get("text") or "").split())[:240],
                score=0.5,
                signals={},
                reason="ranking_unavailable",
                source_item=i,
            )
            for i in items
        ]


def pick_best(
    items: list[dict],
    *,
    cfg,
    demand_scores: dict[str, float] | None = None,
    recent_texts: list[str] | None = None,
    day_key: str = "",
    channel_slug: str = "",
    route: str = "",
) -> RankedTopic | None:
    """Primer elemento del ranking (``None`` si no hay candidatos)."""
    ranked = rank_candidates(
        items,
        cfg=cfg,
        demand_scores=demand_scores,
        recent_texts=recent_texts,
        day_key=day_key,
        channel_slug=channel_slug,
        route=route,
    )
    return ranked[0] if ranked else None


def is_disabled(db=None, cfg=None) -> bool:
    """Kill-switch (system_state > flag de canal). Fail-open a ``False``."""
    if cfg is not None and not bool(getattr(cfg, "TOPIC_RANKING_ENABLED", True)):
        return True
    if db is None:
        return False
    try:
        return str(db.get_system_state(STATE_DISABLED_KEY) or "").strip().lower() \
            in _DISABLED_VALUES
    except Exception:  # noqa: BLE001
        return False
