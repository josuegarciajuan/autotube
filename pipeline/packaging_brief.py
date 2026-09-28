"""Single source of truth for the thumbnail text overlay (W3 packaging).

Before this module the thumbnail text was produced twice:

* ``metadata_generator`` produced a title-coherent ``thumbnail_text`` that was
  persisted to the DB and validated by the upload gate, but never painted;
* ``thumbnail_brainstorm`` produced ``text_gancho``/``text_complemento`` that
  was actually painted but never persisted or validated (the DB column was
  empty for every uploaded video, so the gate was a no-op).

An :class:`OverlaySpec` is built **once**, repaired deterministically so L1 is
anchored to the title, then painted, persisted and validated as the same
object. No producer may paint text that is not part of a spec.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from pipeline.title_tokens import normalize

# Accent-insensitive, case-insensitive token extractor (letters + digits).
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

_STOPWORDS = frozenset(normalize(w) for w in (
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "que", "y", "o", "a", "en", "con", "por", "para", "sin", "sobre",
    "entre", "desde", "hacia", "su", "sus", "al", "se", "lo", "ni",
    "pero", "porque", "cuando", "donde", "si", "no", "mas", "muy", "tan",
    "como", "the", "of", "to", "in", "and", "or", "es", "fue", "son",
))

_DEFAULT_CLICHES = (
    "oculto", "oculta", "real", "prohibido", "impactante", "increible",
    "increíble", "secreto", "secreta", "nadie", "exclusivo", "inedito",
    "inédito", "shock", "impensable", "caso real", "archivo", "expediente",
)

_DEFAULT_BUDGETS = {"l1": 14, "l2": 24, "badge": 14}


@dataclass
class OverlaySpec:
    """Canonical thumbnail overlay. Persisted, painted and validated as one."""

    l1: str = ""
    l2: str = ""
    badge: str = ""
    emphasis: str = ""
    subject_noun: str = ""
    variant_strategy: str = ""
    source: str = "llm"

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "OverlaySpec":
        data = data or {}
        return cls(
            l1=str(data.get("l1", "") or ""),
            l2=str(data.get("l2", "") or ""),
            badge=str(data.get("badge", "") or ""),
            emphasis=str(data.get("emphasis", "") or ""),
            subject_noun=str(data.get("subject_noun", "") or ""),
            variant_strategy=str(data.get("variant_strategy", "") or ""),
            source=str(data.get("source", "") or "llm"),
        )


def overlay_budgets(cfg) -> dict:
    """Resolved per-line character budgets, shared by writer/painter/validator."""
    raw = getattr(cfg, "THUMBNAIL_OVERLAY_BUDGETS", None) or {}
    out = {}
    for key, default in _DEFAULT_BUDGETS.items():
        try:
            out[key] = int(raw.get(key, default))
        except (TypeError, ValueError, AttributeError):
            out[key] = default
    return out


def overlay_cliches(cfg) -> frozenset[str]:
    raw = getattr(cfg, "THUMBNAIL_BADGE_CLICHES", None)
    if raw:
        return frozenset(normalize(str(t)) for t in raw)
    return frozenset(normalize(t) for t in _DEFAULT_CLICHES)


def _truncate_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    out = ""
    for word in text.split():
        cand = (out + " " + word).strip()
        if out and len(cand) > limit:
            break
        out = cand
    return out or text[:limit]


def _tokens(text: str) -> list[str]:
    return [normalize(t) for t in _TOKEN_RE.findall(text or "")]


def title_terms(title: str) -> list[str]:
    """Content words of *title* (normalized, stopwords removed), deduped."""
    out: list[str] = []
    for tok in _tokens(title):
        if len(tok) < 3 or tok in _STOPWORDS:
            continue
        if tok not in out:
            out.append(tok)
    return out


def _salient_title_term(title: str, terms: list[str]) -> str:
    """Pick the most informative title term (digit > proper noun > longest)."""
    if not terms:
        return ""
    words = _TOKEN_RE.findall(title or "")
    originals = {normalize(w): w for w in words}
    with_digits = [t for t in terms if any(c.isdigit() for c in t)]
    if with_digits:
        return originals.get(with_digits[0], with_digits[0])
    proper = [t for t in terms if originals.get(t, "").strip("¿?¡!.,;:()«»\"'")[:1].isupper()]
    if proper:
        return originals.get(proper[0], proper[0])
    return originals.get(max(terms, key=len), max(terms, key=len))


def split_overlay_text(text: str) -> tuple[str, str]:
    """Split a legacy ``"L1 | L2"`` string into its two lines."""
    raw = " ".join(str(text or "").split())
    if "|" in raw:
        parts = [p.strip() for p in raw.split("|") if p.strip()]
        if len(parts) >= 2:
            return parts[0], parts[1]
        if parts:
            return parts[0], ""
    return raw, ""


def _clean_badge(raw: str, cfg) -> str:
    badge = " ".join(str(raw or "").replace("|", " ").split()).strip().upper()
    badge = badge.strip("()（）[]")
    if not badge:
        return ""
    cliches = overlay_cliches(cfg)
    tokens = _tokens(badge)
    if tokens and all(t in cliches for t in tokens):
        return ""
    if normalize(badge) in cliches:
        return ""
    return _truncate_words(badge, overlay_budgets(cfg)["badge"])


def build_overlay_spec(
    title: str,
    cfg,
    *,
    script_text: str = "",
    raw_l1: str = "",
    raw_l2: str = "",
    raw_badge: str = "",
    emphasis: str = "",
    subject_noun: str = "",
    variant_strategy: str = "",
    source: str = "llm",
) -> OverlaySpec:
    """Reconcile raw overlay proposals into a coherent, in-budget spec.

    Deterministic repair:
    * truncate each line at a word boundary (never mid-word);
    * L1 must share a content token with the title, otherwise the most salient
      title term is substituted (prevents off-topic overlays);
    * L2 must not repeat the title or L1;
    * emphasis must belong to L1/L2;
    * badge is stripped of unsupported credibility clichés.
    """
    budgets = overlay_budgets(cfg)
    l1 = _truncate_words(" ".join(str(raw_l1 or "").replace("|", " ").split()), budgets["l1"])
    l2 = _truncate_words(" ".join(str(raw_l2 or "").replace("|", " ").split()), budgets["l2"])

    terms = title_terms(title)
    l1_tokens = set(_tokens(l1))
    if terms and not (l1_tokens & set(terms)):
        salient = _salient_title_term(title, terms)
        if salient:
            l1 = _truncate_words(salient.upper(), budgets["l1"])

    # L2 must add information: drop title/L1 tokens from it.
    banned_for_l2 = set(terms) | set(_tokens(l1))
    l2_words = [w for w in l2.split() if normalize(w) not in banned_for_l2]
    l2 = " ".join(l2_words).strip()

    if not emphasis:
        joined = _tokens(f"{l1} {l2}")
        in_title = [t for t in joined if t in terms]
        emphasis = max(in_title, key=len) if in_title else (max(joined, key=len) if joined else "")

    return OverlaySpec(
        l1=l1.upper(),
        l2=l2.upper(),
        badge=_clean_badge(raw_badge, cfg),
        emphasis=normalize(emphasis),
        subject_noun=" ".join(str(subject_noun or "").split())[:120],
        variant_strategy=str(variant_strategy or ""),
        source=source,
    )


def render_overlay(spec: OverlaySpec) -> str:
    """Render the spec to the persisted ``thumbnail_text`` format ``L1 | L2``."""
    parts = [p for p in (spec.l1.strip(), spec.l2.strip()) if p]
    return " | ".join(parts)


def spec_to_db_fields(spec: OverlaySpec) -> dict:
    return {
        "thumbnail_text": render_overlay(spec),
        "thumbnail_badge_text": spec.badge,
        "thumbnail_emphasis": spec.emphasis,
        "thumbnail_variant_strategy": spec.variant_strategy,
        "thumbnail_overlay_source": spec.source,
    }


def validate_overlay_spec(spec: OverlaySpec, title: str, cfg) -> tuple[list[str], list[str]]:
    """Return ``(reasons, warnings)`` for a spec against its title.

    ``reasons`` are blocking at the upload gate; ``warnings`` are advisory.
    """
    reasons: list[str] = []
    warnings: list[str] = []
    budgets = overlay_budgets(cfg)
    require_text = bool(getattr(cfg, "THUMBNAIL_REQUIRE_TEXT", True))

    if require_text and not (spec.l1.strip() or spec.l2.strip()):
        reasons.append("overlay_empty")
    if len(spec.l1) > budgets["l1"]:
        reasons.append("length")
    if len(spec.l2) > budgets["l2"]:
        reasons.append("length")

    terms = set(title_terms(title))
    # Advisory: generation-time build_overlay_spec already anchors L1 to a
    # title term. At the gate this is a warning, not a block, so a retitled
    # video is not stalled (the thumbnail is regenerated on the next cycle).
    if spec.l1 and terms and not (set(_tokens(spec.l1)) & terms):
        warnings.append("overlay_off_topic")

    norm_title = normalize(title)
    if spec.l2 and len(spec.l2) >= 8 and normalize(spec.l2) in norm_title:
        reasons.append("overlay_repeats_title")

    if spec.emphasis:
        if normalize(spec.emphasis) not in set(_tokens(f"{spec.l1} {spec.l2}")):
            warnings.append("emphasis_not_in_overlay")
    elif spec.l1:
        warnings.append("no_emphasis")

    cliches = overlay_cliches(cfg)
    badge_tokens = _tokens(spec.badge)
    if spec.badge:
        norm_badge = normalize(spec.badge)
        if norm_badge in cliches or (badge_tokens and all(t in cliches for t in badge_tokens)):
            reasons.append("credibility_stamp")

    return reasons, warnings
