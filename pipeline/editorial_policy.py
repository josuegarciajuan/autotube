"""Modelo editorial «curiosidad oscura» (Fase 3).

Clasifica la tensión narrativa de un tema, decide si es elegible para el
piloto y asigna piloto/control de forma **determinista** por (canal, día).

Invariantes de este módulo:

* **Config-driven:** todos los knobs vienen de ``config.defaults`` y pueden
  sobrescribirse por canal en ``config/<slug>_config.py``. Aquí no hay
  ninguna rama por slug ni por channel_id.
* **Determinista:** misma entrada → misma salida. La asignación piloto/control
  se deriva de un hash de (canal, día), así que un reintento del mismo día no
  cambia de grupo (la medición de la Fase 6 queda estable).
* **Fail-open:** ante config ausente o corrupta se usan los defaults y el
  score es neutral; nunca lanza ni bloquea la generación.
* **No sustituye gates:** nicho, demanda, respaldo factual y content_safety
  siguen siendo requisitos aparte. Este modelo solo ORDENA candidatos
  elegibles y etiqueta el experimento.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from pipeline.title_tokens import normalize

STATE_DISABLED_KEY = "editorial_curiosity_disabled"
_DISABLED_VALUES = {"1", "true", "yes", "on"}

# Alias de las categorías excluidas: se comparan normalizados sobre el tema.
_EXCLUDED_ALIASES: dict[str, tuple[str, ...]] = {
    "menores": ("menor", "nino", "nina", "adolescente", "infancia"),
    "autolesion": ("autolesion", "se corta", "se hirio a si mismo"),
    "suicidio": ("suicidio", "se quito la vida", "acabo con su vida"),
    "violencia_grafica": ("decapitad", "desmembrad", "torturad", "sangrient", "masacre"),
    "desinformacion_sanitaria": ("cura milagrosa", "cura el cancer", "remedio milagroso"),
}


@dataclass(frozen=True)
class EditorialPolicy:
    """Política editorial resuelta para un canal."""

    enabled: bool = True
    pilot_share: float = 0.5
    weight: float = 0.25
    tension_types: tuple[str, ...] = ()
    tension_weights: dict[str, float] = field(default_factory=dict)
    tension_lexicon: dict[str, tuple[str, ...]] = field(default_factory=dict)
    excluded_categories: tuple[str, ...] = ()
    require_factual_support: bool = True
    diversity_min: int = 2


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_lexicon(raw) -> dict[str, tuple[str, ...]]:
    """Normaliza el léxico (claves y términos) y descarta entradas vacías."""
    out: dict[str, tuple[str, ...]] = {}
    if not isinstance(raw, dict):
        return out
    for key, terms in raw.items():
        k = str(key or "").strip()
        if not k:
            continue
        if isinstance(terms, str):
            terms = [terms]
        cleaned = tuple(
            normalize(str(t)).strip()
            for t in (terms or [])
            if str(t or "").strip()
        )
        if cleaned:
            out[k] = cleaned
    return out


def build_policy(cfg) -> EditorialPolicy:
    """Resuelve la política editorial desde la config del canal (fail-open)."""
    from config import defaults

    raw_types = getattr(cfg, "EDITORIAL_TENSION_TYPES", None)
    if raw_types is None:
        raw_types = getattr(defaults, "EDITORIAL_TENSION_TYPES", [])
    tension_types = tuple(str(t) for t in (raw_types or []) if str(t or "").strip())

    raw_weights = getattr(cfg, "EDITORIAL_TENSION_WEIGHTS", None)
    if not isinstance(raw_weights, dict):
        raw_weights = getattr(defaults, "EDITORIAL_TENSION_WEIGHTS", {})
    weights = {
        str(k): max(0.0, _as_float(v, 0.0))
        for k, v in (raw_weights or {}).items()
        if str(k or "").strip()
    }

    raw_lexicon = getattr(cfg, "EDITORIAL_TENSION_LEXICON", None)
    if not isinstance(raw_lexicon, dict):
        raw_lexicon = getattr(defaults, "EDITORIAL_TENSION_LEXICON", {})
    lexicon = _normalize_lexicon(raw_lexicon)

    raw_excluded = getattr(cfg, "EDITORIAL_EXCLUDED_CATEGORIES", None)
    if raw_excluded is None:
        raw_excluded = getattr(defaults, "EDITORIAL_EXCLUDED_CATEGORIES", [])
    excluded = tuple(
        normalize(str(c)).strip()
        for c in (raw_excluded or [])
        if str(c or "").strip()
    )

    return EditorialPolicy(
        enabled=bool(getattr(
            cfg, "EDITORIAL_CURIOSITY_ENABLED",
            getattr(defaults, "EDITORIAL_CURIOSITY_ENABLED", True),
        )),
        pilot_share=min(1.0, max(0.0, _as_float(
            getattr(cfg, "EDITORIAL_CURIOSITY_PILOT_SHARE",
                    getattr(defaults, "EDITORIAL_CURIOSITY_PILOT_SHARE", 0.5)),
            0.5,
        ))),
        weight=min(1.0, max(0.0, _as_float(
            getattr(cfg, "EDITORIAL_CURIOSITY_WEIGHT",
                    getattr(defaults, "EDITORIAL_CURIOSITY_WEIGHT", 0.25)),
            0.25,
        ))),
        tension_types=tension_types,
        tension_weights=weights,
        tension_lexicon=lexicon,
        excluded_categories=excluded,
        require_factual_support=bool(getattr(
            cfg, "EDITORIAL_REQUIRE_FACTUAL_SUPPORT",
            getattr(defaults, "EDITORIAL_REQUIRE_FACTUAL_SUPPORT", True),
        )),
        diversity_min=max(0, _as_int(
            getattr(cfg, "EDITORIAL_RECENT_DIVERSITY_MIN",
                    getattr(defaults, "EDITORIAL_RECENT_DIVERSITY_MIN", 2)),
            2,
        )),
    )


def is_enabled(db, cfg) -> bool:
    """Kill-switch global (system_state) por encima del flag de canal."""
    if not build_policy(cfg).enabled:
        return False
    try:
        return str(db.get_system_state(STATE_DISABLED_KEY) or "").strip().lower() \
            not in _DISABLED_VALUES
    except Exception:  # noqa: BLE001 — fail-open
        return True


def classify_tension(text: str, cfg) -> dict[str, float]:
    """Puntuación (0..weight) por tipo de tensión detectado en *text*.

    Un acierto léxico da señal parcial; dos o más, señal plena del tipo.
    Solo se devuelven los tipos presentes (dict vacío = sin tensión).
    """
    policy = build_policy(cfg)
    norm = normalize(text or "")
    if not norm:
        return {}
    scores: dict[str, float] = {}
    for ttype in policy.tension_types:
        weight = policy.tension_weights.get(ttype, 0.0)
        if weight <= 0:
            continue
        terms = policy.tension_lexicon.get(ttype, ())
        hits = sum(1 for term in terms if term and term in norm)
        if hits:
            scores[ttype] = round(min(1.0, hits / 2.0) * weight, 4)
    return scores


def dominant_tension(text: str, cfg) -> str:
    """Tipo de tensión dominante ("" si el tema no tiene señal editorial)."""
    scores = classify_tension(text, cfg)
    if not scores:
        return ""
    return max(scores.items(), key=lambda kv: kv[1])[0]


def editorial_score(text: str, cfg) -> float:
    """Señal editorial normalizada en [0,1] (0 = sin tensión reconocible).

    Es el valor que la Fase 4 combina con demanda/nicho. No es un gate.
    """
    policy = build_policy(cfg)
    scores = classify_tension(text, cfg)
    if not scores:
        return 0.0
    ceiling = max(policy.tension_weights.values() or [1.0]) or 1.0
    return round(min(1.0, max(scores.values()) / ceiling), 4)


def is_sensitive(text: str, cfg) -> bool:
    """True si el tema cae en una categoría excluida del piloto."""
    policy = build_policy(cfg)
    norm = normalize(text or "")
    if not norm:
        return False
    for category in policy.excluded_categories:
        aliases = _EXCLUDED_ALIASES.get(category, (category,))
        if any(normalize(a) in norm for a in aliases):
            return True
    return False


def is_eligible(
    text: str,
    cfg,
    *,
    has_factual_support: bool | None = None,
) -> bool:
    """¿Puede este tema entrar al piloto editorial?

    Requisitos: política activa, tensión reconocible, no sensible y —si el
    llamador aporta la señal— respaldo factual. ``has_factual_support=None``
    significa «desconocido»: el modelo no veta por falta de dato (la Fase 4
    debe pasarlo cuando lo tenga).
    """
    policy = build_policy(cfg)
    if not policy.enabled:
        return False
    if is_sensitive(text, cfg):
        return False
    if not classify_tension(text, cfg):
        return False
    if policy.require_factual_support and has_factual_support is False:
        return False
    return True


def pilot_bucket(channel_slug: str, day_key: str) -> int:
    """Entero estable 0..9999 derivado de (canal, día). Sin estado."""
    raw = f"{channel_slug}|{day_key}|editorial-pilot"
    return int(hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8], 16) % 10000


def pilot_decision(channel_slug: str, day_key: str, cfg) -> str:
    """``pilot`` | ``control`` | ``disabled`` para (canal, día).

    Determinista: el mismo par devuelve siempre el mismo grupo.
    """
    policy = build_policy(cfg)
    if not policy.enabled:
        return "disabled"
    share = policy.pilot_share
    if share <= 0.0:
        return "control"
    if share >= 1.0:
        return "pilot"
    return "pilot" if pilot_bucket(channel_slug, day_key) < int(share * 10000) else "control"


def editorial_label(text: str, cfg) -> str:
    """Etiqueta estable para persistir/atribuir el tema (Fase 6).

    Formato: ``editorial:<tipo>`` o ``editorial:none``. Es independiente del
    grupo piloto/control (eso se decide por canal+día).
    """
    tension = dominant_tension(text, cfg)
    return f"editorial:{tension}" if tension else "editorial:none"
