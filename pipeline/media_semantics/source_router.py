"""P04-B/C — explicit source routing for one :class:`SceneSpec`.

The router answers a different question than the P01 ranker: *what kind of
material can represent this scene at all, and where should we look for it?*
Stock video is not the universal answer.  A historical scene may be better
served by an authentic archival photograph, a space scene by NASA, and an
abstract concept by a generated image.

The classification is deliberately deterministic (keyword/era heuristics) so
it works offline and in tests.  An optional LLM hook may refine the route, but
it is never required and any failure falls back to the deterministic route.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .contracts import SceneSpec


class SceneCategory(str, Enum):
    PEOPLE = "PEOPLE"
    NATURE = "NATURE"
    ANIMALS = "ANIMALS"
    CITY = "CITY"
    TECHNOLOGY = "TECHNOLOGY"
    HISTORY = "HISTORY"
    SPACE = "SPACE"
    SCIENCE = "SCIENCE"
    MEDICAL = "MEDICAL"
    GEOGRAPHY = "GEOGRAPHY"
    ABSTRACT = "ABSTRACT"
    GENERIC = "GENERIC"
    OTHER = "OTHER"


class AssetType(str, Enum):
    STOCK_VIDEO = "STOCK_VIDEO"
    STOCK_IMAGE = "STOCK_IMAGE"
    ARCHIVAL_IMAGE = "ARCHIVAL_IMAGE"
    ARCHIVAL_VIDEO = "ARCHIVAL_VIDEO"
    SCIENTIFIC_IMAGE = "SCIENTIFIC_IMAGE"
    AI_IMAGE = "AI_IMAGE"
    CONCEPTUAL_STOCK = "CONCEPTUAL_STOCK"
    GENERIC_FALLBACK = "GENERIC_FALLBACK"


class AIPolicy(str, Enum):
    AI_IMAGE_ALLOWED = "AI_IMAGE_ALLOWED"
    AI_IMAGE_PREFERRED = "AI_IMAGE_PREFERRED"
    AI_IMAGE_FORBIDDEN = "AI_IMAGE_FORBIDDEN"


# Media types understood by the renderer (image is first-class via Ken Burns).
MEDIA_TYPE_BY_ASSET = {
    AssetType.STOCK_VIDEO.value: "video",
    AssetType.ARCHIVAL_VIDEO.value: "video",
    AssetType.STOCK_IMAGE.value: "image",
    AssetType.ARCHIVAL_IMAGE.value: "image",
    AssetType.SCIENTIFIC_IMAGE.value: "image",
    AssetType.AI_IMAGE.value: "image",
    AssetType.CONCEPTUAL_STOCK.value: "image",
    AssetType.GENERIC_FALLBACK.value: "image",
}

STOCK_VIDEO_PROVIDERS = ("pexels", "pixabay", "mixkit", "coverr", "youtube_cc")
STOCK_IMAGE_PROVIDERS = ("pixabay_photo", "unsplash", "pexels_photo")
ARCHIVE_PROVIDERS = ("wikimedia_commons",)
NASA_PROVIDERS = ("nasa_image_library",)
AI_PROVIDERS = ("pollinations", "local_sd", "stable_horde")


_WORD_RE = re.compile(r"[a-z0-9áéíóúüñ]+", re.IGNORECASE)


def _words(text: Any) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(str(text or ""))}


def _contains_any(words: set[str], terms: set[str]) -> set[str]:
    return words & terms


# Kept small and explicit.  Ambiguous words ("light", "dark") are avoided on
# purpose: they describe mood, not a source category.
_CATEGORY_TERMS: dict[SceneCategory, set[str]] = {
    SceneCategory.SPACE: {
        "space", "nasa", "planet", "planets", "mars", "moon", "lunar", "galaxy",
        "galaxies", "nebula", "cosmos", "cosmic", "astronaut", "astronomy",
        "telescope", "orbit", "rocket", "spaceship", "spacecraft", "satellite",
        "asteroid", "comet", "solar", "eclipse", "espacio", "planeta", "luna",
        "galaxia", "cohete", "astronauta", "telescopio", "órbita", "orbita",
    },
    SceneCategory.MEDICAL: {
        "medical", "medicine", "hospital", "surgery", "surgeon", "doctor",
        "patient", "disease", "virus", "bacteria", "cell", "cells", "brain",
        "heart", "blood", "anatomy", "clinical", "diagnosis", "tumor", "cancer",
        "médico", "medico", "medicina", "hospital", "cirugía", "cirugia",
        "paciente", "enfermedad", "virus", "célula", "celula", "cerebro",
        "corazón", "corazon", "sangre", "anatomía", "anatomia",
    },
    SceneCategory.HISTORY: {
        "history", "historical", "ancient", "antiquity", "medieval", "century",
        "empire", "roman", "greek", "egypt", "egyptian", "pharaoh", "mummy",
        "archaeology", "archaeologist", "excavation", "tomb", "ruins", "artifact",
        "artefact", "napoleon", "caesar", "king", "queen", "dynasty", "war",
        "battle", "soldier", "1940", "1914", "1922", "1800", "1700",
        "historia", "histórico", "historico", "antiguo", "antigua", "medieval",
        "siglo", "imperio", "romano", "griego", "egipto", "egipcio", "faraón",
        "faraon", "momia", "arqueología", "arqueologia", "arqueólogo",
        "arqueologo", "excavación", "excavacion", "tumba", "ruinas", "artefacto",
        "guerra", "batalla", "soldado",
    },
    SceneCategory.SCIENCE: {
        "science", "scientific", "physics", "chemistry", "biology", "molecule",
        "atom", "dna", "gene", "genetic", "laboratory", "microscope", "experiment",
        "quantum", "particle", "neuron", "geology", "volcano", "earthquake",
        "ciencia", "científico", "cientifico", "física", "fisica", "química",
        "quimica", "biología", "biologia", "molécula", "molecula", "átomo",
        "atomo", "adn", "gen", "laboratorio", "microscopio", "experimento",
    },
    SceneCategory.TECHNOLOGY: {
        "technology", "computer", "software", "algorithm", "robot", "robotic",
        "ai", "artificial", "intelligence", "internet", "network", "server",
        "data", "digital", "machine", "engine", "engineering", "cyber",
        "tecnología", "tecnologia", "computadora", "ordenador", "software",
        "algoritmo", "robot", "inteligencia", "internet", "red", "servidor",
        "datos", "digital", "máquina", "maquina", "ingeniería", "ingenieria",
    },
    SceneCategory.ANIMALS: {
        "animal", "animals", "bird", "birds", "fish", "shark", "whale", "wolf",
        "wolves", "lion", "tiger", "bear", "horse", "dog", "cat", "snake",
        "insect", "spider", "elephant", "dinosaur", "animales", "pájaro", "pajaro",
        "ave", "aves", "pez", "ballena", "lobo", "león", "leon", "tigre", "oso",
        "caballo", "perro", "gato", "serpiente", "insecto", "araña", "arana",
        "elefante", "dinosaurio",
    },
    SceneCategory.NATURE: {
        "nature", "forest", "jungle", "mountain", "mountains", "river", "ocean",
        "sea", "desert", "snow", "rain", "storm", "volcano", "cave", "island",
        "tree", "trees", "flower", "landscape", "wildlife", "naturaleza", "bosque",
        "selva", "montaña", "montana", "río", "rio", "océano", "oceano", "mar",
        "desierto", "nieve", "lluvia", "tormenta", "cueva", "isla", "árbol",
        "arbol", "flor", "paisaje",
    },
    SceneCategory.CITY: {
        "city", "street", "urban", "building", "buildings", "skyscraper",
        "traffic", "subway", "metro", "market", "crowd", "neighborhood",
        "ciudad", "calle", "urbano", "edificio", "rascacielos", "tráfico",
        "trafico", "mercado", "multitud", "barrio",
    },
    SceneCategory.PEOPLE: {
        "man", "woman", "person", "people", "family", "child", "children",
        "crowd", "worker", "doctor", "scientist", "artist", "portrait",
        "hombre", "mujer", "persona", "personas", "familia", "niño", "nino",
        "niños", "gente", "trabajador", "retrato",
    },
    SceneCategory.GEOGRAPHY: {
        "map", "maps", "earth", "continent", "country", "border", "coast",
        "geography", "region", "territory", "climate", "mapa", "mapas", "tierra",
        "continente", "país", "pais", "frontera", "costa", "geografía",
        "geografia", "región", "region", "territorio", "clima",
    },
    SceneCategory.ABSTRACT: {
        "concept", "abstract", "symbolic", "metaphor", "idea", "memory", "dream",
        "fear", "hope", "grief", "love", "time", "consciousness", "soul",
        "spirit", "energy", "light", "darkness", "infinity", "eternity",
        "concepto", "abstracto", "simbólico", "simbolico", "metáfora", "metafora",
        "idea", "memoria", "sueño", "sueño", "miedo", "esperanza", "duelo",
        "amor", "tiempo", "conciencia", "alma", "espíritu", "espiritu", "energía",
        "energia", "luz", "oscuridad", "infinito", "eternidad",
    },
}

_ERA_MARKERS = {
    "ancient", "antiquity", "medieval", "century", "siglo", "antiguo", "antigua",
    "prehistoric", "prehistórico", "prehistorico", "roman", "romano", "greek",
    "griego", "egyptian", "egipcio", "victorian", "victoriano", "1920s",
    "1930s", "1940s", "1950s", "1960s", "1970s", "1980s", "19th", "18th",
    "17th", "16th", "15th", "14th", "13th",
}


@dataclass
class SourceRoute:
    """Structured sourcing decision for one scene."""

    scene_type: str = SceneCategory.GENERIC.value
    preferred_asset_types: list[str] = field(default_factory=list)
    preferred_providers: list[str] = field(default_factory=list)
    fallback_order: list[str] = field(default_factory=list)
    historical_priority: bool = False
    generation_allowed: bool = True
    ai_policy: str = AIPolicy.AI_IMAGE_ALLOWED.value
    confidence: float = 0.55
    reasons: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    mode: str = "deterministic"
    allowed_asset_types: list[str] = field(default_factory=list)
    forbidden_asset_types: list[str] = field(default_factory=list)
    providers_considered: list[str] = field(default_factory=list)
    providers_skipped: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_type": self.scene_type,
            "preferred_asset_types": list(self.preferred_asset_types),
            "preferred_providers": list(self.preferred_providers),
            "fallback_order": list(self.fallback_order),
            "historical_priority": bool(self.historical_priority),
            "generation_allowed": bool(self.generation_allowed),
            "ai_policy": self.ai_policy,
            "confidence": round(float(self.confidence), 4),
            "reasons": list(self.reasons),
            "matched_terms": list(self.matched_terms[:12]),
            "mode": self.mode,
            "allowed_asset_types": list(self.allowed_asset_types),
            "forbidden_asset_types": list(self.forbidden_asset_types),
            "providers_considered": list(self.providers_considered),
            "providers_skipped": dict(self.providers_skipped),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceRoute":
        data = dict(data or {})
        return cls(
            scene_type=str(data.get("scene_type") or SceneCategory.GENERIC.value),
            preferred_asset_types=list(data.get("preferred_asset_types") or []),
            preferred_providers=list(data.get("preferred_providers") or []),
            fallback_order=list(data.get("fallback_order") or []),
            historical_priority=bool(data.get("historical_priority")),
            generation_allowed=bool(data.get("generation_allowed", True)),
            ai_policy=str(data.get("ai_policy") or AIPolicy.AI_IMAGE_ALLOWED.value),
            confidence=float(data.get("confidence") or 0.55),
            reasons=list(data.get("reasons") or []),
            matched_terms=list(data.get("matched_terms") or []),
            mode=str(data.get("mode") or "deterministic"),
            allowed_asset_types=list(data.get("allowed_asset_types") or []),
            forbidden_asset_types=list(data.get("forbidden_asset_types") or []),
            providers_considered=list(data.get("providers_considered") or []),
            providers_skipped=dict(data.get("providers_skipped") or {}),
        )


# Default ladders by category.  They intentionally differ: a single universal
# ladder would push HISTORY/SPACE toward modern stock video.
_LADDERS: dict[SceneCategory, list[str]] = {
    SceneCategory.HISTORY: [
        AssetType.ARCHIVAL_IMAGE.value, AssetType.ARCHIVAL_VIDEO.value,
        AssetType.STOCK_IMAGE.value, AssetType.STOCK_VIDEO.value,
        AssetType.AI_IMAGE.value, AssetType.CONCEPTUAL_STOCK.value,
        AssetType.GENERIC_FALLBACK.value,
    ],
    SceneCategory.SPACE: [
        AssetType.SCIENTIFIC_IMAGE.value, AssetType.ARCHIVAL_IMAGE.value,
        AssetType.STOCK_VIDEO.value, AssetType.STOCK_IMAGE.value,
        AssetType.AI_IMAGE.value, AssetType.CONCEPTUAL_STOCK.value,
        AssetType.GENERIC_FALLBACK.value,
    ],
    SceneCategory.SCIENCE: [
        AssetType.SCIENTIFIC_IMAGE.value, AssetType.ARCHIVAL_IMAGE.value,
        AssetType.STOCK_VIDEO.value, AssetType.STOCK_IMAGE.value,
        AssetType.AI_IMAGE.value, AssetType.CONCEPTUAL_STOCK.value,
        AssetType.GENERIC_FALLBACK.value,
    ],
    SceneCategory.MEDICAL: [
        AssetType.SCIENTIFIC_IMAGE.value, AssetType.STOCK_IMAGE.value,
        AssetType.STOCK_VIDEO.value, AssetType.AI_IMAGE.value,
        AssetType.CONCEPTUAL_STOCK.value, AssetType.GENERIC_FALLBACK.value,
    ],
    SceneCategory.GEOGRAPHY: [
        AssetType.STOCK_VIDEO.value, AssetType.ARCHIVAL_IMAGE.value,
        AssetType.STOCK_IMAGE.value, AssetType.SCIENTIFIC_IMAGE.value,
        AssetType.AI_IMAGE.value, AssetType.CONCEPTUAL_STOCK.value,
        AssetType.GENERIC_FALLBACK.value,
    ],
    SceneCategory.ABSTRACT: [
        AssetType.STOCK_IMAGE.value, AssetType.AI_IMAGE.value,
        AssetType.CONCEPTUAL_STOCK.value, AssetType.STOCK_VIDEO.value,
        AssetType.GENERIC_FALLBACK.value,
    ],
    SceneCategory.PEOPLE: [
        AssetType.STOCK_VIDEO.value, AssetType.STOCK_IMAGE.value,
        AssetType.ARCHIVAL_IMAGE.value, AssetType.AI_IMAGE.value,
        AssetType.CONCEPTUAL_STOCK.value, AssetType.GENERIC_FALLBACK.value,
    ],
    SceneCategory.CITY: [
        AssetType.STOCK_VIDEO.value, AssetType.STOCK_IMAGE.value,
        AssetType.ARCHIVAL_IMAGE.value, AssetType.AI_IMAGE.value,
        AssetType.CONCEPTUAL_STOCK.value, AssetType.GENERIC_FALLBACK.value,
    ],
    SceneCategory.NATURE: [
        AssetType.STOCK_VIDEO.value, AssetType.STOCK_IMAGE.value,
        AssetType.AI_IMAGE.value, AssetType.CONCEPTUAL_STOCK.value,
        AssetType.GENERIC_FALLBACK.value,
    ],
    SceneCategory.ANIMALS: [
        AssetType.STOCK_VIDEO.value, AssetType.STOCK_IMAGE.value,
        AssetType.ARCHIVAL_IMAGE.value, AssetType.AI_IMAGE.value,
        AssetType.CONCEPTUAL_STOCK.value, AssetType.GENERIC_FALLBACK.value,
    ],
    SceneCategory.TECHNOLOGY: [
        AssetType.STOCK_VIDEO.value, AssetType.STOCK_IMAGE.value,
        AssetType.SCIENTIFIC_IMAGE.value, AssetType.AI_IMAGE.value,
        AssetType.CONCEPTUAL_STOCK.value, AssetType.GENERIC_FALLBACK.value,
    ],
    SceneCategory.GENERIC: [
        AssetType.STOCK_VIDEO.value, AssetType.STOCK_IMAGE.value,
        AssetType.ARCHIVAL_IMAGE.value, AssetType.AI_IMAGE.value,
        AssetType.CONCEPTUAL_STOCK.value, AssetType.GENERIC_FALLBACK.value,
    ],
    SceneCategory.OTHER: [
        AssetType.STOCK_VIDEO.value, AssetType.STOCK_IMAGE.value,
        AssetType.AI_IMAGE.value, AssetType.CONCEPTUAL_STOCK.value,
        AssetType.GENERIC_FALLBACK.value,
    ],
}

_PROVIDERS_BY_ASSET: dict[str, tuple[str, ...]] = {
    AssetType.STOCK_VIDEO.value: STOCK_VIDEO_PROVIDERS,
    AssetType.STOCK_IMAGE.value: STOCK_IMAGE_PROVIDERS,
    AssetType.ARCHIVAL_IMAGE.value: ARCHIVE_PROVIDERS,
    AssetType.ARCHIVAL_VIDEO.value: ARCHIVE_PROVIDERS,
    AssetType.SCIENTIFIC_IMAGE.value: NASA_PROVIDERS + ARCHIVE_PROVIDERS,
    AssetType.AI_IMAGE.value: AI_PROVIDERS,
    AssetType.CONCEPTUAL_STOCK.value: STOCK_IMAGE_PROVIDERS,
    AssetType.GENERIC_FALLBACK.value: STOCK_IMAGE_PROVIDERS,
}


def _spec_text(spec: SceneSpec) -> str:
    return " ".join(
        str(x or "") for x in (
            spec.subject, spec.action, spec.environment, spec.visual_concept,
            spec.narration_fragment, spec.era, " ".join(spec.must_include),
        )
    )


def _detect_era(spec: SceneSpec) -> bool:
    words = _words(_spec_text(spec))
    if _contains_any(words, _ERA_MARKERS):
        return True
    # Numeric years in the pre-2000 range signal a historical era.
    for token in re.findall(r"\b1[0-9]{3}\b|\b20[0-1][0-9]\b", _spec_text(spec)):
        try:
            year = int(token)
        except ValueError:
            continue
        if year < 2000:
            return True
    era = str(spec.era or "").strip().lower()
    return bool(era and era not in ("", "atemporal", "presente", "present", "timeless", "modern"))


class SourceRouter:
    """Deterministic scene→source route with an optional LLM refinement hook."""

    VERSION = "p04-source-router-1"

    def __init__(
        self,
        *,
        llm_router: Callable[[SceneSpec], dict[str, Any] | None] | None = None,
        generation_allowed: bool = True,
    ) -> None:
        self.llm_router = llm_router
        self.generation_allowed = bool(generation_allowed)

    def classify(self, spec: SceneSpec) -> tuple[SceneCategory, list[str]]:
        text = _spec_text(spec)
        words = _words(text)
        if not words:
            return SceneCategory.GENERIC, []
        scores: list[tuple[int, int, SceneCategory, list[str]]] = []
        for category, terms in _CATEGORY_TERMS.items():
            hits = sorted(_contains_any(words, terms))
            if hits:
                # Longer/more specific terms are worth slightly more than a
                # single common noun; ties resolve by category enum order.
                weight = sum(len(h.split()) + 1 for h in hits)
                scores.append((len(hits), weight, category, hits))
        if not scores:
            return SceneCategory.GENERIC, []
        scores.sort(key=lambda item: (-item[0], -item[1], item[2].value))
        best = scores[0]
        historical = _detect_era(spec)
        if historical and best[2] not in (SceneCategory.SPACE, SceneCategory.ABSTRACT):
            # Era context is a strong signal for archival material.
            return SceneCategory.HISTORY, sorted(set(best[3]) | {"era"})
        return best[2], best[3]

    def route(self, spec: SceneSpec) -> SourceRoute:
        category, matched = self.classify(spec)
        reasons = [f"deterministic_category:{category.value}"]
        if matched:
            reasons.append("terms:" + ",".join(matched[:6]))
        historical = _detect_era(spec) or category is SceneCategory.HISTORY
        if historical:
            reasons.append("era_or_history_signal")

        ladder = list(_LADDERS.get(category, _LADDERS[SceneCategory.GENERIC]))

        # A2 policy: historical documentary with authentic material available
        # prefers real archives; abstract concepts prefer AI after a stock try.
        ai_policy = AIPolicy.AI_IMAGE_ALLOWED.value
        if category is SceneCategory.ABSTRACT:
            ai_policy = AIPolicy.AI_IMAGE_PREFERRED.value
        elif historical and self._has_named_public_figure(spec):
            ai_policy = AIPolicy.AI_IMAGE_FORBIDDEN.value
            reasons.append("named_historical_figure_requires_authentic_material")

        if not self.generation_allowed or ai_policy == AIPolicy.AI_IMAGE_FORBIDDEN.value:
            ladder = [a for a in ladder if a != AssetType.AI_IMAGE.value]
            if not self.generation_allowed:
                reasons.append("ai_generation_disabled")

        preferred_providers: list[str] = []
        providers_skipped: dict[str, str] = {}
        considered: list[str] = []
        for asset_type in ladder:
            for provider in _PROVIDERS_BY_ASSET.get(asset_type, ()):
                considered.append(provider)
                if provider not in preferred_providers:
                    preferred_providers.append(provider)

        # Provider budgets: NASA only for space/science; archive only where it
        # can plausibly help.  Skipped providers remain explicit in provenance.
        for provider in AI_PROVIDERS:
            if provider not in preferred_providers:
                providers_skipped[provider] = "ai_not_allowed_by_route"
        for provider in NASA_PROVIDERS:
            if provider not in preferred_providers:
                providers_skipped[provider] = "not_space_or_science"
        for provider in ARCHIVE_PROVIDERS:
            if provider not in preferred_providers:
                providers_skipped[provider] = "not_archival_route"

        # Preferred asset types are the first strategy of each media kind,
        # preserving the ladder order but without redundant duplicates.
        preferred_assets = list(dict.fromkeys(ladder))
        allowed_assets = list(dict.fromkeys(ladder + [AssetType.GENERIC_FALLBACK.value]))
        forbidden_assets: list[str] = []
        if ai_policy == AIPolicy.AI_IMAGE_FORBIDDEN.value:
            forbidden_assets.append(AssetType.AI_IMAGE.value)

        confidence = 0.55
        if matched:
            confidence = min(0.85, 0.55 + 0.04 * len(matched))
        if historical and category is SceneCategory.HISTORY:
            confidence = min(0.9, confidence + 0.05)

        route = SourceRoute(
            scene_type=category.value,
            preferred_asset_types=preferred_assets,
            preferred_providers=preferred_providers,
            fallback_order=list(ladder),
            historical_priority=historical,
            generation_allowed=self.generation_allowed and AssetType.AI_IMAGE.value not in forbidden_assets,
            ai_policy=ai_policy,
            confidence=confidence,
            reasons=reasons,
            matched_terms=matched,
            mode="deterministic",
            allowed_asset_types=allowed_assets,
            forbidden_asset_types=forbidden_assets,
            providers_considered=list(dict.fromkeys(considered)),
            providers_skipped=providers_skipped,
        )

        if self.llm_router is not None:
            refined = self._try_llm_route(spec, route)
            if refined is not None:
                return refined
        return route

    @staticmethod
    def _has_named_public_figure(spec: SceneSpec) -> bool:
        """Conservative heuristic: an explicit historical person named in the
        scene.  We do not attempt NER; only obvious named figures are honoured."""
        text = _spec_text(spec).lower()
        figures = (
            "napoleon", "napoleón", "caesar", "césar", "cleopatra", "cleopatra",
            "alexander the great", "alejandro magno", "einstein", "newton",
            "darwin", "tesla", "lincoln", "kennedy", "churchill", "hitler",
            "mozart", "beethoven", "columbus", "colón", "colon",
        )
        return any(f in text for f in figures)

    def _try_llm_route(self, spec: SceneSpec, base: SourceRoute) -> SourceRoute | None:
        try:
            payload = self.llm_router(spec)  # type: ignore[misc]
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        scene_type = str(payload.get("scene_type") or "").upper()
        if scene_type not in {c.value for c in SceneCategory}:
            return None
        # Never let an LLM widen the hard policy (e.g. enable AI for a named
        # historical figure).  It may refine category and preferred providers.
        merged = SourceRoute.from_dict(base.to_dict())
        merged.scene_type = scene_type
        merged.mode = "llm_refined"
        preferred = payload.get("preferred_providers")
        if isinstance(preferred, list):
            allowed = set(base.preferred_providers)
            merged.preferred_providers = [p for p in preferred if p in allowed] or list(
                base.preferred_providers
            )
        merged.reasons = list(base.reasons) + ["llm_refinement"]
        merged.confidence = max(base.confidence, min(0.95, float(payload.get("confidence") or 0.0)))
        return merged
