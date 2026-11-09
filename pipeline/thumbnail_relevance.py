"""Subject ↔ title relevance checks for thumbnails.

The pipeline can produce a beautiful thumbnail of the wrong place: the
Cachemira video shipped with a map of Australia. The art director classifies
the subject as ``place`` but never verifies *which* place, so this module adds
a deterministic guard:

* extract places mentioned in the title and in the proposed subject/concept;
* flag a mismatch when both sets are non-empty and disjoint;
* rewrite the image prompt with the correct location and an explicit
  "no maps/flags of other countries" constraint.

No network calls, no model downloads: a compact Spanish/English place lexicon
plus token matching. The LLM still writes the concept; this only prevents the
most destructive failure mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pipeline.title_tokens import normalize

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _norm(text: str) -> str:
    return normalize(text or "")


# Canonical place → accepted surface forms (normalized). Covers the countries,
# regions and landmarks that appear in the four channels. Extend as needed; the
# check fails open when a place is unknown.
_PLACES: dict[str, tuple[str, ...]] = {
    "australia": ("australia", "australiano", "australiana", "outback"),
    "cachemira": ("cachemira", "kashmir"),
    "india": ("india", "indio", "india"),
    "pakistan": ("paquistan", "pakistan"),
    "peru": ("peru", "peruano", "peruana", "andino", "andes"),
    "egipto": ("egipto", "egipcio", "egipcia", "egiptologo"),
    "grecia": ("grecia", "griego", "griega", "minoico", "minoica", "thera", "santorini"),
    "mexico": ("mexico", "mexicano", "mexicana", "azteca", "maya"),
    "brasil": ("brasil", "brasileno", "amazonia", "amazonas"),
    "japon": ("japon", "japones", "japonesa"),
    "china": ("china", "chino", "china"),
    "rusia": ("rusia", "ruso", "rusa", "siberia"),
    "alaska": ("alaska", "alaskano"),
    "canada": ("canada", "canadiense"),
    "estados unidos": ("estados unidos", "eeuu", "ee uu", "yosemite", "california",
                       "las vegas", "alcatraz"),
    "marruecos": ("marruecos", "marroqui"),
    "turquia": ("turquia", "turco", "turca", "anatolia"),
    "italia": ("italia", "italiano", "romano", "romana"),
    "islandia": ("islandia", "islandes"),
    "atlantida": ("atlantida", "atlante", "atlantes"),
    "antartida": ("antartida", "antartico", "antartica"),
    "k2": ("k2", "chogori"),
    "everest": ("everest", "himalaya"),
    "nepal": ("nepal", "nepali"),
}

# Words that make a map/flag/landmark of another region a hard failure.
_GEO_ARTIFACTS = (
    "mapa", "map", "bandera", "flag", "frontera", "border",
    "atlas", "cartografia", "cartografía",
)


_PLACE_DISPLAY = {
    "atlantida": "Atlántida", "peru": "Perú", "mexico": "México",
    "japon": "Japón", "egipto": "Egipto", "grecia": "Grecia",
    "cachemira": "Cachemira", "pakistan": "Pakistán", "turquia": "Turquía",
    "islandia": "Islandia", "antartida": "Antártida", "brasil": "Brasil",
    "estados unidos": "Estados Unidos",
}


def display_place(place: str) -> str:
    return _PLACE_DISPLAY.get(place, place.title())


@dataclass
class RelevanceResult:
    title_places: list[str] = field(default_factory=list)
    subject_places: list[str] = field(default_factory=list)
    mismatched_places: list[str] = field(default_factory=list)
    missing_place: bool = False

    @property
    def ok(self) -> bool:
        return not self.mismatched_places


def _tokens(text: str) -> list[str]:
    return [_norm(t) for t in _TOKEN_RE.findall(text or "")]


def extract_places(text: str) -> list[str]:
    """Return canonical places mentioned in *text* (deduped, order kept)."""
    raw = f" {_norm(text)} "
    tokens = set(_tokens(text))
    found: list[str] = []
    for canonical, aliases in _PLACES.items():
        for alias in aliases:
            alias_norm = _norm(alias)
            if not alias_norm:
                continue
            if " " in alias_norm:
                hit = f" {alias_norm} " in raw
            else:
                hit = alias_norm in tokens
            if hit:
                if canonical not in found:
                    found.append(canonical)
                break
    return found


def check_relevance(title: str, subject: str, concept: str = "") -> RelevanceResult:
    """Compare places in the title with places in the proposed visual."""
    title_places = extract_places(title)
    subject_text = f"{subject or ''} {concept or ''}"
    subject_places = extract_places(subject_text)
    mismatched = [
        p for p in subject_places if title_places and p not in title_places
    ]
    missing = bool(title_places) and not any(
        p in subject_places for p in title_places
    )
    return RelevanceResult(
        title_places=title_places,
        subject_places=subject_places,
        mismatched_places=mismatched,
        missing_place=missing,
    )


def enforce_subject_relevance(concept: str, title: str,
                              primary_subject: str = "") -> str:
    """Append the correct location + a no-wrong-map constraint to *concept*.

    If the proposed text mentions a different place than the title, the
    constraint names both so the image model cannot drift to the wrong region.
    """
    result = check_relevance(title, primary_subject, concept)
    text = " ".join(str(concept or "").split())
    if not result.title_places:
        return text

    place = display_place(result.title_places[0])
    suffix = (
        f" The scene is set in {place}. Show authentic {place} geography, "
        f"architecture and people. Do not depict maps, flags, borders or "
        f"landmarks of any other country."
    )
    if result.mismatched_places:
        wrong = ", ".join(result.mismatched_places)
        suffix += f" Explicitly avoid {wrong}: it is not the location of this story."
    if result.missing_place:
        suffix += f" The subject must be clearly identifiable as {place}."
    if _norm(suffix) in _norm(text):
        return text
    return (text + suffix).strip()


def has_geo_artifact(text: str) -> bool:
    """True when the visual concept explicitly asks for a map/flag/border."""
    tokens = set(_tokens(text))
    return any(_norm(word) in tokens for word in _GEO_ARTIFACTS)
