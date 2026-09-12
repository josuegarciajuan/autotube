"""Art-direction policy for thumbnails: subject-first, per-video diversity.

This module is the *policy* layer that sits between the LLM brainstorm
(``pipeline.thumbnail_brainstorm``) and the composition engine
(``pipeline.thumbnail_maker``). It does not call any LLM itself — it turns the
LLM's raw proposals into a deterministic, validated plan:

- subject type + face role (a face is the protagonist ONLY when the video is
  about a person);
- image-provider plan (AI vs stock) driven by the subject;
- layout chosen from the channel pool while avoiding the last N layouts used
  by the same channel;
- the topic-first creative directive injected into the LLM prompts.

Keeping the policy pure makes it cheap to unit-test and prevents the "always a
shocked face on a dark background" degenerate pattern.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# ── Subject vocabulary ──────────────────────────────────────────

SUBJECT_TYPES = ("person", "place", "object_artifact", "concept", "medical", "event")

FACE_ROLES = ("protagonist", "secondary", "none")

# provider_plan[subject_type] = (main, face, inset)
#   main  : "ai" (Pollo) | "stock" (Unsplash/Pexels/Pixabay)
#   face  : "stock" | "none"   (human faces are NEVER AI-generated)
#   inset : "stock" | "scene_image" | "none"
PROVIDER_TABLE: dict[str, dict[str, str]] = {
    "person": {"main": "stock", "face": "stock", "inset": "none"},
    "place": {"main": "ai", "face": "none", "inset": "stock"},
    "object_artifact": {"main": "stock", "face": "none", "inset": "scene_image"},
    "concept": {"main": "ai", "face": "none", "inset": "stock"},
    "medical": {"main": "ai", "face": "stock", "inset": "stock"},
    "event": {"main": "stock", "face": "none", "inset": "stock"},
}

# ── Topic-first creative directive (replaces the MrBeast face directive) ──

TOPIC_FIRST_DIRECTIVE = (
    "REGLA DE ORO: la IMAGEN PRINCIPAL debe REPRESENTAR EL TEMA CONCRETO DEL "
    "TÍTULO (persona, lugar, objeto o concepto específico), no una cara genérica "
    "ni un fondo de stock sin relación. Un espectador debe poder adivinar el tema "
    "del vídeo solo con mirar la imagen.\n"
    "EL ROSTRO HUMANO SOLO ES PROTAGONISTA cuando el vídeo trata de una persona "
    "concreta (un caso, un protagonista, un explorador). En ese caso debe ser un "
    "rostro real de banco de imágenes (NUNCA generado por IA, sin menores), con "
    "emoción intensa pero creíble — nada de muecas caricaturescas. En el resto de "
    "casos la cara es, como mucho, un elemento secundario y pequeño en una esquina; "
    "el sujeto del tema sigue siendo el foco.\n"
    "EVITA: primer plano de una cara sorprendida como único contenido, fondos "
    "abstractos que no tengan que ver con el título, repetir la composición de "
    "miniaturas anteriores del canal.\n"
    "COLOR: el color debe nacer del contenido (luz, atmósfera, materiales del "
    "tema), no de una paleta fija de canal. No fuerces dorados/azules porque sí."
)


def _hash_int(value: str, modulo: int) -> int:
    if modulo <= 0:
        return 0
    digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()
    return int(digest, 16) % modulo


@dataclass
class ArtDirection:
    """Validated per-video art direction."""

    subject_type: str = "concept"
    primary_subject: str = ""
    face_role: str = "none"
    provider_main: str = "ai"
    provider_face: str = "none"
    provider_inset: str = "scene_image"
    layout: str = "topic_hero"
    color_intent: str = ""
    emphasis_word: str = ""
    recent_context: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "subject_type": self.subject_type,
            "primary_subject": self.primary_subject,
            "face_role": self.face_role,
            "provider_main": self.provider_main,
            "provider_face": self.provider_face,
            "provider_inset": self.provider_inset,
            "layout": self.layout,
            "color_intent": self.color_intent,
            "emphasis_word": self.emphasis_word,
        }


def normalize_subject_type(raw: str, title: str = "", script: str = "") -> str:
    """Map a free-text LLM label to one of ``SUBJECT_TYPES``; heuristic fallback."""
    text = f"{raw} {title} {script[:400]}".casefold()
    # 1. Exact type token from the LLM (e.g. raw="place").
    raw_lower = str(raw or "").strip().casefold()
    if raw_lower in SUBJECT_TYPES:
        return raw_lower
    # 2. Person-centric framing wins first ("el caso de X", "la historia de X"):
    # a named-protagonist case should let a real face be the protagonist even
    # when the topic touches medicine.
    if any(marker in text for marker in (
        "el caso de", "la historia de", "la vida de", "el hombre que",
        "la mujer que", "el doctor ", "la doctora ", "el paciente",
        "el explorador", "la exploradora", "el protagonista",
    )):
        return "person"
    # 3. Spanish heuristics, ordered by specificity. Place/medical/event win
    # over a bare generic mention of "hombres"/"personas" so an expedition or
    # a shipwreck is not misclassified as a person-centric video.
    if any(marker in text for marker in (
        "enfermedad", "síndrome", "sindrome", "diagnóstico", "diagnostico",
        "anomalía", "anomalia", "cerebro", "adn", "patología", "patologia",
    )):
        return "medical"
    if any(marker in text for marker in (
        "expedición", "expedicion", "civilización", "civilizacion", "ruinas",
        "templo", "desierto", "montaña", "montana", "isla", "ciudad perdida",
        "naufragio", "océano", "oceano", "artico", "ártico", "antártida",
        "antartida",
    )):
        return "place"
    if any(marker in text for marker in (
        "batalla", "guerra", "accidente", "catástrofe", "catastrofe",
        "masacre", "tragedia",
    )):
        return "event"
    if any(marker in text for marker in (
        "documento", "artefacto", "manuscrito", "reliquia", "archivo", "objeto",
        "cráneo", "craneo", "mapa antiguo",
    )):
        return "object_artifact"
    if any(marker in text for marker in (
        "médic", "medic", "doctor", "paciente", "protagonista", "explorador",
        "el hombre que", "la mujer que", "niño", "niña", "piloto", "soldado",
        "rey ", "reina ", "persona que",
    )):
        return "person"
    return "concept"


def resolve_face_role(subject_type: str, allow_faces: bool = True) -> str:
    """A face is protagonist only for person-centric videos (and if allowed)."""
    if not allow_faces:
        return "none"
    if subject_type == "person":
        return "protagonist"
    if subject_type == "medical":
        return "secondary"
    return "none"


def resolve_provider_plan(subject_type: str, face_role: str,
                          explicit_main: str = "", explicit_face: str = "") -> dict:
    """Return a validated provider plan for the given subject/face role."""
    base = PROVIDER_TABLE.get(subject_type, PROVIDER_TABLE["concept"])
    main = explicit_main if explicit_main in ("ai", "stock") else base["main"]
    if face_role == "none":
        face = "none"
    else:
        face = explicit_face if explicit_face in ("stock",) else base["face"]
        if face != "stock":
            face = "stock"  # human faces always come from a real stock bank
    inset = base["inset"]
    return {"main": main, "face": face, "inset": inset}


def resolve_layout(
    layout_pool: list[str] | None,
    recent_layouts: list[str] | None,
    preferred: str = "",
    seed: str = "",
    depth: int = 3,
) -> str:
    """Pick a layout from the pool, avoiding the most recent ones.

    ``preferred`` (the LLM's proposal) is honoured only when it is not blocked
    by the recent history. When every pool member is blocked (small pool), the
    least-recently-used one wins instead of raising.
    """
    pool = [layout for layout in (layout_pool or []) if layout]
    if not pool:
        pool = ["topic_hero"]
    depth = max(0, int(depth))
    recent = [layout for layout in (recent_layouts or []) if layout]
    blocked = set(recent[:depth])

    candidates = [layout for layout in pool if layout not in blocked] or pool
    if preferred and preferred in candidates:
        return preferred

    # Deterministic pseudo-random choice, but prefer the least recently used.
    usage = {layout: recent.count(layout) for layout in candidates}
    best_usage = min(usage.values())
    least_used = [layout for layout in candidates if usage[layout] == best_usage]
    return least_used[_hash_int(seed or "seed", len(least_used))]


def build_recent_context(rows: list[dict] | None) -> dict:
    """Summarise recent thumbnails (newest first) for prompt injection.

    ``rows`` come from ``ExtendedDatabase.get_recent_thumbnail_context`` and are
    expected to contain ``thumbnail_layout``, ``thumbnail_color_key`` and
    ``thumbnail_subject``.
    """
    rows = rows or []
    layouts = [str(r.get("thumbnail_layout") or "") for r in rows]
    colors = [str(r.get("thumbnail_color_key") or "") for r in rows]
    subjects = [str(r.get("thumbnail_subject") or "") for r in rows]
    return {
        "layouts": [layout for layout in layouts if layout],
        "colors": [color for color in colors if color],
        "subjects": [subject for subject in subjects if subject],
    }


def recent_context_prompt(recent_context: dict | None) -> str:
    """Human-readable instruction telling the LLM what NOT to repeat."""
    ctx = recent_context or {}
    layouts = ctx.get("layouts") or []
    subjects = ctx.get("subjects") or []
    if not layouts and not subjects:
        return ""
    parts = ["EVITA REPETIR miniaturas anteriores del canal:"]
    if layouts:
        parts.append(f"- layouts ya usados (no repitas): {', '.join(layouts[:5])}.")
    if subjects:
        parts.append(f"- sujetos ya usados (busca un ángulo visual distinto): {', '.join(subjects[:5])}.")
    return " ".join(parts)


class ThumbnailArtDirector:
    """Turns raw brainstorm output into a validated ``ArtDirection``."""

    def plan(
        self,
        *,
        title: str,
        script_text: str = "",
        style: dict | None = None,
        raw_subject_type: str = "",
        primary_subject: str = "",
        raw_main: str = "",
        raw_face: str = "",
        color_intent: str = "",
        emphasis_word: str = "",
        layout_pool: list[str] | None = None,
        recent_context: dict | None = None,
        allow_faces: bool = True,
        preferred_layout: str = "",
        video_id: int = 0,
    ) -> ArtDirection:
        subject_type = normalize_subject_type(raw_subject_type, title=title,
                                               script=script_text)
        face_role = resolve_face_role(subject_type, allow_faces=allow_faces)
        providers = resolve_provider_plan(subject_type, face_role,
                                          explicit_main=raw_main,
                                          explicit_face=raw_face)
        recent = recent_context or {}
        layout = resolve_layout(
            layout_pool=layout_pool,
            recent_layouts=recent.get("layouts"),
            preferred=preferred_layout,
            seed=f"{title}|{video_id}",
            depth=int((style or {}).get("_layout_history_depth", 3)),
        )
        return ArtDirection(
            subject_type=subject_type,
            primary_subject=(primary_subject or title)[:160],
            face_role=face_role,
            provider_main=providers["main"],
            provider_face=providers["face"],
            provider_inset=providers["inset"],
            layout=layout,
            color_intent=color_intent[:80],
            emphasis_word=emphasis_word[:20],
            recent_context=recent,
        )
