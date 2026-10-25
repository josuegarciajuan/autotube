"""P04-O/P/Q/R — video-level coherence, shot diversity and style signals.

A scene decision is not independent: repeating the same clip, the same still
image, the same provider or four close-ups in a row degrades the video even if
each scene is locally relevant.  This module keeps a bounded window of recent
selections and returns *soft penalties* that the sourcing pipeline applies to
the relevance score.

The only hard veto remains a real duplicate (handled by ``dedup.py``).  Shot
diversity and style coherence are ranking signals; relevance always wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import Candidate, SceneSpec

_WORD_RE = re.compile(r"[a-z0-9áéíóúüñ]+", re.IGNORECASE)

SHOT_CLASSES = ("wide", "medium", "close", "detail", "establishing")
_SHOT_ALIASES = {
    "wide": "wide", "establishing": "establishing", "aerial": "wide",
    "medium": "medium", "mid": "medium", "portrait": "close", "close": "close",
    "closeup": "close", "detail": "detail", "macro": "detail",
    "insert": "detail", "pov": "medium",
}


def _tokens(text: Any) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(str(text or ""))}


def shot_class(value: Any) -> str:
    words = _tokens(value)
    # Explicit priority: the most specific shot label wins over a generic
    # "shot" token.  Sets have no stable order, so never iterate them here.
    for word in ("close", "closeup", "macro", "detail", "insert", "establishing",
                 "wide", "aerial", "medium", "mid", "portrait", "pov"):
        if word in words:
            return _SHOT_ALIASES[word]
    text = str(value or "").lower()
    for key, cls in _SHOT_ALIASES.items():
        if key in text:
            return cls
    return ""


@dataclass
class CoherenceConfig:
    window: int = 8
    provider_repeat_penalty: float = 0.035
    asset_type_repeat_penalty: float = 0.02
    consecutive_image_penalty: float = 0.03
    consecutive_image_cap: float = 0.09
    subject_repeat_penalty: float = 0.04
    same_clip_penalty: float = 0.10
    style_mismatch_penalty: float = 0.05
    era_mismatch_penalty: float = 0.08
    portrait_penalty: float = 0.02
    shot_repeat_penalty: float = 0.015
    shot_diversity_bonus: float = 0.02
    penalty_cap: float = 0.28


@dataclass
class CoherenceSignals:
    penalties: dict[str, float] = field(default_factory=dict)
    bonuses: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    recent_providers: list[str] = field(default_factory=list)
    recent_asset_types: list[str] = field(default_factory=list)
    consecutive_images: int = 0
    shot_class: str = ""
    provider_share: float = 0.0

    @property
    def total_penalty(self) -> float:
        return round(float(sum(self.penalties.values())), 6)

    @property
    def total_bonus(self) -> float:
        return round(float(sum(self.bonuses.values())), 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "penalties": {k: round(v, 6) for k, v in self.penalties.items()},
            "bonuses": {k: round(v, 6) for k, v in self.bonuses.items()},
            "total_penalty": self.total_penalty,
            "total_bonus": self.total_bonus,
            "reasons": list(self.reasons),
            "recent_providers": list(self.recent_providers),
            "recent_asset_types": list(self.recent_asset_types),
            "consecutive_images": int(self.consecutive_images),
            "shot_class": self.shot_class,
            "provider_share": round(float(self.provider_share), 4),
        }


@dataclass
class _Recent:
    key: str
    provider: str
    asset_type: str
    shot: str
    subject_tokens: set[str]
    era: str
    depiction: str
    style_tokens: set[str]
    media_type: str
    width: int | None
    height: int | None


class SelectionCoherence:
    """Bounded recent-selection context for one video construction."""

    VERSION = "p04-coherence-1"

    def __init__(self, config: CoherenceConfig | None = None) -> None:
        self.config = config or CoherenceConfig()
        self._recent: list[_Recent] = []

    def reset(self) -> None:
        self._recent.clear()

    def recent(self) -> list[dict[str, Any]]:
        return [
            {
                "key": r.key, "provider": r.provider, "asset_type": r.asset_type,
                "shot": r.shot, "media_type": r.media_type,
            }
            for r in self._recent
        ]

    def __len__(self) -> int:
        return len(self._recent)

    def evaluate(
        self,
        candidate: Candidate,
        *,
        spec: SceneSpec,
        asset_type: str,
        media_type: str = "",
        is_conceptual: bool = False,
    ) -> CoherenceSignals:
        window = self._recent[-max(1, self.config.window):]
        signals = CoherenceSignals(
            recent_providers=[r.provider for r in window],
            recent_asset_types=[r.asset_type for r in window],
        )
        if not window:
            signals.shot_class = shot_class(spec.desired_shot)
            return signals

        self._provider(candidate, window, signals)
        self._asset_type(asset_type, media_type, window, signals)
        self._subject(candidate, spec, window, signals)
        self._shot(spec, window, signals)
        self._style(candidate, spec, asset_type, window, signals)
        self._technical(candidate, signals)

        cap = self.config.penalty_cap
        total = sum(signals.penalties.values())
        if total > cap:
            factor = cap / total
            signals.penalties = {k: v * factor for k, v in signals.penalties.items()}
            signals.reasons.append("coherence_penalty_capped")
        return signals

    def record(
        self,
        candidate: Candidate,
        *,
        spec: SceneSpec,
        asset_type: str,
        media_type: str = "",
    ) -> None:
        keys = _tokens(" ".join([candidate.title, candidate.description, " ".join(candidate.tags or [])]))
        self._recent.append(
            _Recent(
                key=candidate.canonical_key,
                provider=str(candidate.provider or ""),
                asset_type=str(asset_type or ""),
                shot=shot_class(spec.desired_shot),
                subject_tokens=_tokens(spec.subject) | _tokens(candidate.title),
                era=str(spec.era or "").lower(),
                depiction=str(spec.depiction_mode or "").lower(),
                style_tokens=keys,
                media_type=str(media_type or candidate.media_type or ""),
                width=candidate.width,
                height=candidate.height,
            )
        )
        if len(self._recent) > max(self.config.window * 4, 40):
            del self._recent[: len(self._recent) - max(self.config.window * 4, 40)]

    # ── individual signals ─────────────────────────────────────
    def _provider(self, candidate, window, signals) -> None:
        provider = str(candidate.provider or "")
        if not provider:
            return
        same = sum(1 for r in window if r.provider == provider)
        share = same / max(len(window), 1)
        signals.provider_share = share
        if share >= 0.5:
            signals.penalties["provider_repetition"] = (
                self.config.provider_repeat_penalty * (1.0 + (share - 0.5))
            )
            signals.reasons.append(f"provider_repeated:{provider}:{same}/{len(window)}")

    def _asset_type(self, asset_type, media_type, window, signals) -> None:
        consecutive = 0
        for recent in reversed(window):
            if recent.media_type == "image" and media_type == "image":
                consecutive += 1
            else:
                break
        signals.consecutive_images = consecutive
        if media_type == "image" and consecutive >= 2:
            penalty = min(
                self.config.consecutive_image_cap,
                self.config.consecutive_image_penalty * (consecutive - 1),
            )
            signals.penalties["consecutive_stills"] = penalty
            signals.reasons.append(f"consecutive_stills:{consecutive}")
        if asset_type:
            same_type = sum(1 for r in window if r.asset_type == asset_type)
            if same_type >= max(3, len(window) - 1):
                signals.penalties["asset_type_repetition"] = self.config.asset_type_repeat_penalty
                signals.reasons.append(f"asset_type_repeated:{asset_type}")

    def _subject(self, candidate, spec, window, signals) -> None:
        subject = _tokens(spec.subject)
        if subject:
            for recent in reversed(window):
                overlap = subject & recent.subject_tokens
                if recent.key == candidate.canonical_key:
                    signals.penalties["same_clip"] = self.config.same_clip_penalty
                    signals.reasons.append("same_asset_recent")
                    break
                if overlap:
                    ratio = len(overlap) / max(len(subject), 1)
                    if ratio >= 0.6:
                        signals.penalties["subject_repetition"] = (
                            self.config.subject_repeat_penalty * ratio
                        )
                        signals.reasons.append("same_subject_recent")
                        break

    def _shot(self, spec, window, signals) -> None:
        desired = shot_class(spec.desired_shot)
        signals.shot_class = desired
        last = window[-1].shot
        if desired and last:
            if desired == last:
                signals.penalties["shot_repetition"] = self.config.shot_repeat_penalty
                signals.reasons.append(f"same_shot_class:{desired}")
            else:
                signals.bonuses["shot_diversity"] = self.config.shot_diversity_bonus
                signals.reasons.append(f"shot_diversity:{last}->{desired}")

    def _style(self, candidate, spec, asset_type, window, signals) -> None:
        depiction = str(spec.depiction_mode or "").lower()
        if depiction == "documentary" and asset_type in ("AI_IMAGE", "CONCEPTUAL_STOCK"):
            signals.penalties["generated_in_documentary"] = self.config.style_mismatch_penalty
            signals.reasons.append("generated_asset_in_documentary_scene")
        # Era mismatch against the current scene is already handled by the
        # text ranker; here we only penalise a *style jump* between the
        # candidate and the immediately previous selection.
        recent = window[-1]
        era = str(spec.era or "").lower()
        if era and recent.era and era != recent.era:
            signals.penalties["era_jump"] = self.config.era_mismatch_penalty
            signals.reasons.append(f"era_jump:{recent.era}->{era}")
        style_tokens = _tokens(" ".join([
            candidate.title, candidate.description, " ".join(candidate.tags or []),
        ]))
        illustration = {"illustration", "drawing", "painting", "cartoon", "anime"}
        documentary = {"documentary", "photo", "photograph", "realistic", "footage"}
        if (style_tokens & illustration) and (recent.style_tokens & documentary):
            signals.penalties["style_illustration_jump"] = self.config.style_mismatch_penalty
            signals.reasons.append("illustration_after_documentary")

    def _technical(self, candidate, signals) -> None:
        try:
            width = int(candidate.width or 0)
            height = int(candidate.height or 0)
        except (TypeError, ValueError):
            return
        if width and height and height > width:
            signals.penalties["portrait_crop"] = self.config.portrait_penalty
            signals.reasons.append("portrait_aspect_ratio")
