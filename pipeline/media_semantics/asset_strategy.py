"""P04-C — per-scene asset strategy derived from the source route.

The strategy is the contract between routing and retrieval:

* which asset types are allowed / preferred / forbidden;
* whether an AI image is a fallback, a preference, or unavailable;
* how a conceptual fallback is labelled;
* which renderer media type each candidate maps to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import Candidate, SceneSpec
from .source_router import (
    AIPolicy,
    AssetType,
    MEDIA_TYPE_BY_ASSET,
    SourceRoute,
)


@dataclass
class AssetStrategy:
    allowed_asset_types: list[str] = field(default_factory=list)
    preferred_asset_types: list[str] = field(default_factory=list)
    forbidden_asset_types: list[str] = field(default_factory=list)
    fallback_order: list[str] = field(default_factory=list)
    ai_policy: str = AIPolicy.AI_IMAGE_ALLOWED.value
    generation_allowed: bool = True
    historical_priority: bool = False
    literal_expected: bool = True
    conceptual_allowed: bool = True
    generic_allowed: bool = True
    reasons: list[str] = field(default_factory=list)

    def allows(self, asset_type: str) -> bool:
        asset_type = str(asset_type or "")
        if asset_type in self.forbidden_asset_types:
            return False
        return not self.allowed_asset_types or asset_type in self.allowed_asset_types

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_asset_types": list(self.allowed_asset_types),
            "preferred_asset_types": list(self.preferred_asset_types),
            "forbidden_asset_types": list(self.forbidden_asset_types),
            "fallback_order": list(self.fallback_order),
            "ai_policy": self.ai_policy,
            "generation_allowed": bool(self.generation_allowed),
            "historical_priority": bool(self.historical_priority),
            "literal_expected": bool(self.literal_expected),
            "conceptual_allowed": bool(self.conceptual_allowed),
            "generic_allowed": bool(self.generic_allowed),
            "reasons": list(self.reasons),
        }


def build_asset_strategy(spec: SceneSpec, route: SourceRoute) -> AssetStrategy:
    """Materialise the route into an explicit, auditable strategy."""
    forbidden = list(route.forbidden_asset_types)
    allowed = [a for a in route.allowed_asset_types if a not in forbidden]
    preferred = [a for a in route.preferred_asset_types if a not in forbidden]
    if not allowed:
        allowed = [AssetType.GENERIC_FALLBACK.value]
    literal_expected = str(spec.depiction_mode or "").lower() not in (
        "symbolic", "metaphoric", "abstract",
    )
    reasons = list(route.reasons)
    if not literal_expected:
        reasons.append("symbolic_depiction")
    return AssetStrategy(
        allowed_asset_types=allowed,
        preferred_asset_types=preferred,
        forbidden_asset_types=forbidden,
        fallback_order=[a for a in route.fallback_order if a not in forbidden],
        ai_policy=route.ai_policy,
        generation_allowed=bool(route.generation_allowed)
        and AssetType.AI_IMAGE.value not in forbidden,
        historical_priority=bool(route.historical_priority),
        literal_expected=literal_expected,
        conceptual_allowed=True,
        generic_allowed=True,
        reasons=reasons,
    )


def media_type_for_asset_type(asset_type: str, candidate: Candidate | None = None) -> str:
    if asset_type in MEDIA_TYPE_BY_ASSET:
        return MEDIA_TYPE_BY_ASSET[asset_type]
    if candidate is not None and candidate.media_type in ("image", "video"):
        return candidate.media_type
    return "image"


def infer_asset_type(
    candidate: Candidate,
    route: SourceRoute,
    *,
    fallback_type: str = "",
) -> str:
    """Best-effort mapping of a provider candidate to a strategy asset type."""
    explicit = str(candidate.asset_type or "").strip().upper()
    if explicit and explicit in {a.value for a in AssetType}:
        return explicit
    if candidate.ai_generated:
        return AssetType.AI_IMAGE.value
    if fallback_type == "CONCEPTUAL":
        return AssetType.CONCEPTUAL_STOCK.value
    provider = str(candidate.provider or "").lower()
    media_type = str(candidate.media_type or "").lower()
    if provider in ("nasa_image_library",):
        return AssetType.SCIENTIFIC_IMAGE.value
    if provider in ("wikimedia_commons",):
        return (
            AssetType.ARCHIVAL_VIDEO.value
            if media_type == "video" else AssetType.ARCHIVAL_IMAGE.value
        )
    if media_type == "video" or route.scene_type in ("PEOPLE", "CITY"):
        if media_type == "video":
            return AssetType.STOCK_VIDEO.value
    return AssetType.STOCK_IMAGE.value


def conceptual_fallback_asset_type(route: SourceRoute) -> str:
    """P04-L: explicit conceptual label, never disguised as a literal match."""
    return AssetType.CONCEPTUAL_STOCK.value
