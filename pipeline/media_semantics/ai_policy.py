"""P04-J/K — controlled AI-image fallback policy and prompt construction.

AI images are an explicit *fallback* after appropriate real sources, never the
universal first choice.  The policy is carried on the source route:

* ``AI_IMAGE_ALLOWED`` — may be used after real sources fail;
* ``AI_IMAGE_PREFERRED`` — conceptually symbolic scenes may go to AI before
  generic stock, but still after relevant stock imagery;
* ``AI_IMAGE_FORBIDDEN`` — never generate (e.g. a named historical figure with
  authentic archival material available).

The prompt is assembled from SceneSpec + visual bible so subject, action,
environment, era, shot, style continuity and ``must_avoid`` survive.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import SceneSpec
from .source_router import AIPolicy, AssetType, SourceRoute

_WORD_RE = re.compile(r"[a-z0-9áéíóúüñ]+", re.IGNORECASE)

_DEFAULT_STYLE = (
    "cinematic documentary photography, realistic, atmospheric lighting, "
    "consistent color grade, high detail, 16:9"
)


@dataclass
class AIGenerationRequest:
    prompt: str
    negative_prompt: str
    seed: int
    policy: str
    backend_preference: list[str] = field(default_factory=list)
    prompt_hash: str = ""
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "seed": self.seed,
            "policy": self.policy,
            "backend_preference": list(self.backend_preference),
            "prompt_hash": self.prompt_hash,
            "reasons": list(self.reasons),
        }


def ai_generation_allowed(route: SourceRoute, spec: SceneSpec) -> bool:
    if not route.generation_allowed:
        return False
    if route.ai_policy == AIPolicy.AI_IMAGE_FORBIDDEN.value:
        return False
    if AssetType.AI_IMAGE.value in route.forbidden_asset_types:
        return False
    return True


def should_attempt_ai(
    route: SourceRoute,
    spec: SceneSpec,
    *,
    no_good_real_candidate: bool,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for the AI fallback trigger."""
    if not no_good_real_candidate:
        return False, "real_candidate_available"
    if ai_generation_allowed(route, spec):
        return True, "NO_GOOD_CANDIDATE_after_appropriate_sources"
    return False, "ai_forbidden_by_policy"


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" ,.;")


def _avoid_terms(spec: SceneSpec) -> list[str]:
    terms: list[str] = []
    for item in spec.must_avoid:
        text = _clean(item).lower()
        if text and text not in terms:
            terms.append(text)
    # Universal documentary anti-patterns; never claim historical authenticity
    # for a generated image.
    return terms


def build_ai_prompt(
    spec: SceneSpec,
    *,
    visual_bible: dict[str, Any] | None = None,
    style_suffix: str = "",
    max_chars: int = 900,
) -> tuple[str, str]:
    """Build ``(positive, negative)`` prompts from the scene spec only."""
    bible = dict(visual_bible or {})
    extra = dict(spec.extra or {})
    style = _clean(
        style_suffix
        or extra.get("visual_style")
        or bible.get("visual_universe")
        or _DEFAULT_STYLE
    )
    parts: list[str] = []
    if spec.subject:
        parts.append(_clean(spec.subject))
    if spec.action:
        parts.append(_clean(spec.action))
    if spec.environment:
        parts.append(_clean(spec.environment))
    if spec.visual_concept and str(spec.visual_concept).lower() not in {
        p.lower() for p in parts
    }:
        parts.append(_clean(spec.visual_concept))
    if spec.era:
        parts.append(f"era: {_clean(spec.era)}")
    if spec.desired_shot:
        parts.append(_clean(spec.desired_shot))
    if spec.mood_style:
        parts.append(_clean(spec.mood_style))
    included = [str(x).strip() for x in spec.must_include if str(x).strip()]
    if included:
        parts.append("must show: " + ", ".join(included[:6]))
    if style:
        parts.append(style)
    prompt = ", ".join(p for p in parts if p)
    prompt = _clean(prompt)[:max_chars]

    negative = ", ".join(_avoid_terms(spec))
    negative += ", text, watermark, logo, deformed, extra limbs, low resolution"
    return prompt, negative.strip(" ,")


def build_ai_request(
    spec: SceneSpec,
    route: SourceRoute,
    *,
    visual_bible: dict[str, Any] | None = None,
    style_suffix: str = "",
    backends: list[str] | None = None,
) -> AIGenerationRequest:
    prompt, negative = build_ai_prompt(
        spec, visual_bible=visual_bible, style_suffix=style_suffix,
    )
    seed_text = f"{spec.scene_idx}:{spec.narration_fragment[:200]}:{prompt}"
    seed = int(hashlib.sha1(seed_text.encode("utf-8")).hexdigest()[:8], 16) % (2**31 - 1)
    prompt_hash = hashlib.sha1(prompt.encode("utf-8")).hexdigest()
    return AIGenerationRequest(
        prompt=prompt,
        negative_prompt=negative,
        seed=seed,
        policy=route.ai_policy,
        backend_preference=list(backends or ["pollinations", "local_sd", "stable_horde"]),
        prompt_hash=prompt_hash,
        reasons=[f"policy:{route.ai_policy}", f"category:{route.scene_type}"],
    )


def build_conceptual_query(spec: SceneSpec) -> str:
    """P04-L — a coherent *conceptual* search, explicitly non-literal.

    Used only after literal routing fails.  It deliberately drops specific
    proper nouns and keeps the scene's mood/environment, so the result is a
    plausible visual metaphor rather than a false literal match.
    """
    parts: list[str] = []
    for value in (spec.visual_concept, spec.environment, spec.mood_style):
        text = _clean(value)
        if text and text.lower() not in {p.lower() for p in parts}:
            parts.append(text)
    if not parts:
        words = [
            w.lower() for w in _WORD_RE.findall(spec.narration_fragment)
            if len(w) > 3
        ]
        parts = words[:4]
    if not parts:
        parts = ["atmospheric conceptual scene"]
    return _clean("conceptual visual metaphor, " + ", ".join(parts))[:120]


def ai_asset_metadata(request: AIGenerationRequest, backend: str, path: str) -> dict[str, Any]:
    return {
        "asset_type": AssetType.AI_IMAGE.value,
        "ai_generated": True,
        "generation_backend": backend,
        "generation_prompt_hash": request.prompt_hash,
        "license": "AI_GENERATED",
        "license_url": "",
        "attribution_required": False,
        "public_domain": False,
        "creator": f"AI ({backend})",
        "provenance": "AI_GENERATED",
        "path": path,
        "type": "image",
    }
