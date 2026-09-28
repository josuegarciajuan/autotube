"""Zero-quota search-demand planning for long-form titles (W2 packaging).

The channel already fetched real search signals in two disconnected places
(``seo_researcher`` Google Trends + autocomplete, ``topic_demand`` suggestion
overlap) but they never reached the final title. This module unifies them into
one :class:`KeywordPlan` that the ``TitleEngine`` receives:

* **demand** — suggestion hit-rate across expanded seeds (0 quota);
* **niche_fit** — overlap with the channel's own keyword vocabulary;
* **competition** — optional ``yt-dlp`` result proxy (0 quota, opt-in).

Everything fails open: a network failure yields a neutral score and the title
engine keeps working with its deterministic rubric.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field

from pipeline.title_tokens import normalize
from pipeline.topic_demand import NEUTRAL_SCORE, fetch_suggestions, score_demand

logger = logging.getLogger("autotube.title_keyword_planner")

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

_STOPWORDS = frozenset(normalize(w) for w in (
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "que", "y", "o", "a", "en", "con", "por", "para", "sin", "sobre",
    "entre", "desde", "hacia", "su", "sus", "al", "se", "lo", "ni",
    "pero", "porque", "cuando", "donde", "si", "no", "mas", "muy", "tan",
    "como", "es", "fue", "son", "the", "of", "to", "in", "and", "or",
))

_DEFAULT_PREFIXES = ("cómo", "por qué", "qué pasó con", "la historia de")
_DEFAULT_SUFFIXES = ("explicado", "caso", "documental")


@dataclass
class KeywordSignal:
    demand: float = NEUTRAL_SCORE
    competition: float = NEUTRAL_SCORE
    niche_fit: float = NEUTRAL_SCORE
    score: float = NEUTRAL_SCORE


@dataclass
class KeywordPlan:
    primary: str = ""
    secondary: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    source: str = "none"

    def as_dict(self) -> dict:
        return {
            "primary": self.primary,
            "secondary": list(self.secondary),
            "entities": list(self.entities),
            "signals": self.signals,
            "source": self.source,
        }


def _tokens(text: str) -> list[str]:
    return [normalize(t) for t in _TOKEN_RE.findall(text or "")]


def _significant(text: str) -> list[str]:
    out = []
    for tok in _tokens(text):
        if len(tok) < 3 or tok in _STOPWORDS:
            continue
        if tok not in out:
            out.append(tok)
    return out


class TitleKeywordPlanner:
    """Build a :class:`KeywordPlan` for a script (0 quota, fail-open)."""

    def __init__(self, cfg, slug: str = ""):
        self.cfg = cfg
        self.slug = slug or str(getattr(cfg, "CANAL_NAME", "") or "")
        self.max_seeds = int(getattr(cfg, "TITLE_KEYWORD_MAX_SEEDS", 8) or 8)
        self.competition_enabled = bool(
            getattr(cfg, "TITLE_KEYWORD_COMPETITION_ENABLED", False)
        )
        self.prefixes = list(
            getattr(cfg, "TITLE_KEYWORD_INTENT_PREFIXES", None) or _DEFAULT_PREFIXES
        )
        self.suffixes = list(
            getattr(cfg, "TITLE_KEYWORD_SUFFIXES", None) or _DEFAULT_SUFFIXES
        )
        anchors = []
        for attr in ("CHANNEL_KEYWORDS", "SEO_SECONDARY_KEYWORDS"):
            anchors.extend(list(getattr(cfg, attr, []) or []))
        anchors.append(str(getattr(cfg, "SEO_PRIMARY_KEYWORD", "") or ""))
        self._anchors = {t for a in anchors for t in _significant(str(a))}

    # ── Public API ────────────────────────────────────────────────

    def plan(self, script: dict, source_content: dict = None) -> KeywordPlan:
        seeds = self._seeds(script, source_content)
        if not seeds:
            return KeywordPlan(source="none")
        try:
            return self._rank(seeds)
        except Exception as exc:  # noqa: BLE001 — never block generation
            logger.debug("keyword planning failed: %s", exc)
            return KeywordPlan(primary=seeds[0], secondary=seeds[1:4], source="seed")

    # ── Seed generation ───────────────────────────────────────────

    def _seeds(self, script: dict, source_content: dict) -> list[str]:
        script = script or {}
        base_terms: list[str] = []
        kw_raw = script.get("keywords") or script.get("keywords_json") or []
        if isinstance(kw_raw, str):
            import json
            try:
                kw_raw = json.loads(kw_raw)
            except (ValueError, TypeError):
                kw_raw = [kw_raw]
        for kw in (kw_raw if isinstance(kw_raw, list) else [])[:3]:
            base_terms.extend(_significant(str(kw)))
        base_terms.extend(_significant(str(getattr(self.cfg, "SEO_PRIMARY_KEYWORD", "") or "")))
        if source_content:
            base_terms.extend(_significant(str(source_content.get("title", "") or "")))
        core = " ".join(dict.fromkeys(base_terms))[:70]
        if not core:
            return []

        seeds: list[str] = [core]
        for suffix in self.suffixes[:3]:
            seeds.append(f"{core} {suffix}"[:80])
        for prefix in self.prefixes[:3]:
            seeds.append(f"{prefix} {core}"[:80])
        # Deduplicate, keep order, bound the number of network calls.
        out: list[str] = []
        for seed in seeds:
            seed = " ".join(seed.split())
            if seed and seed not in out:
                out.append(seed)
        return out[: self.max_seeds]

    # ── Ranking ───────────────────────────────────────────────────

    def _rank(self, seeds: list[str]) -> KeywordPlan:
        hits: dict[str, int] = {}
        for seed in seeds:
            for suggestion in fetch_suggestions(seed) or []:
                phrase = " ".join(str(suggestion).split())
                if phrase:
                    hits[phrase] = hits.get(phrase, 0) + 1

        scored: list[tuple[str, KeywordSignal]] = []
        for phrase, count in hits.items():
            if count > 0:
                demand = min(1.0, count / 5.0)
            else:
                d = score_demand(phrase)
                demand = NEUTRAL_SCORE if d is None else d
            competition = self._competition(phrase) if self.competition_enabled else NEUTRAL_SCORE
            niche = self._niche_fit(phrase)
            score = 0.45 * demand + 0.30 * niche + 0.25 * (1.0 - competition)
            scored.append((phrase, KeywordSignal(demand, competition, niche, round(score, 4))))

        # Always include the raw seeds so we never return an empty plan.
        for seed in seeds[:3]:
            if seed not in hits:
                niche = self._niche_fit(seed)
                scored.append((seed, KeywordSignal(NEUTRAL_SCORE, NEUTRAL_SCORE, niche,
                                                   0.45 * NEUTRAL_SCORE + 0.30 * niche + 0.25 * NEUTRAL_SCORE)))

        if not scored:
            return KeywordPlan(source="none")
        scored.sort(key=lambda item: (-item[1].score, len(item[0])))
        top = scored[:8]
        entities = self._entities(top[0][0], seeds)
        return KeywordPlan(
            primary=top[0][0],
            secondary=[p for p, _ in top[1:6]],
            entities=entities,
            signals={p: asdict(sig) for p, sig in top},
            source="autocomplete",
        )

    def _niche_fit(self, phrase: str) -> float:
        if not self._anchors:
            return NEUTRAL_SCORE
        tokens = set(_significant(phrase))
        if not tokens:
            return NEUTRAL_SCORE
        return min(1.0, len(tokens & self._anchors) / max(1, min(len(tokens), 4)))

    def _entities(self, primary: str, seeds: list[str]) -> list[str]:
        out: list[str] = []
        for source in [primary] + seeds:
            for word in re.findall(r"\b[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑ-]{2,}\b|\b\d{3,4}\b", source or ""):
                if word not in out:
                    out.append(word)
        return out[:6]

    def _competition(self, phrase: str) -> float:
        """0 (open) … 1 (crowded) proxy from yt-dlp search results."""
        try:
            import yt_dlp  # type: ignore
        except Exception:
            return NEUTRAL_SCORE
        try:
            opts = {
                "quiet": True,
                "skip_download": True,
                "extract_flat": True,
                "no_warnings": True,
                "socket_timeout": 6,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch5:{phrase}", download=False)
            entries = (info or {}).get("entries") or []
            if not entries:
                return 0.0
            return min(1.0, len(entries) / 10.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("yt-dlp competition lookup failed for %r: %s", phrase[:50], exc)
            return NEUTRAL_SCORE
