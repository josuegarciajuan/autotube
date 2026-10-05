"""Per-scene visual context shared by every media decision.

One object carries everything a scene needs to stay coherent with the whole
video: its own narration fragment, its narrative phase, the global visual
direction (visual bible), the era/theme anchor and the block context.  The
same context feeds:
  - the stock query pool (so clips respect the video's visual world),
  - the deterministic candidate ranking (full-video brief, not a bare fragment),
  - the optional LLM reranking (era + concept + forbidden elements),
  - the AI image prompt.

This makes "lo que ves = lo que oyes" hold across ALL asset types, not just
AI images.

Fase 3 (calidad-coherencia) adds a *filmable intent* per scene
(``subject``/``action``/``object``/``setting``), explicit ``must_show`` /
``must_avoid`` lists and a ``depiction_mode`` so the media search knows whether
a scene wants a literal depiction, a documentary shot or a symbolic image.
Everything is best-effort and fail-open: a scene with no bible and no text
still yields a valid context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


def _cget(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ═══════════════════════════════════════════════════════════════════
# Deterministic scene-intent derivation (Fase 3)
# ═══════════════════════════════════════════════════════════════════
#
# Small, dependency-free heuristics (NO LLM) used as a fallback when the
# visual bible is missing/incomplete.  They never raise: any failure degrades
# to ``documentary`` / empty strings so the pipeline is never blocked.

_WORD_RE = re.compile(r"[a-záéíóúüñ]+")

# Verbs that imply an observable action a camera can register.
_ACTION_VERBS: frozenset[str] = frozenset({
    # Spanish
    "cruza", "cruzan", "camina", "caminan", "corre", "corren", "huye",
    "huyen", "lucha", "luchan", "descubre", "descubren", "encuentra",
    "encuentran", "construye", "construyen", "excava", "excavan", "navega",
    "navegan", "escribe", "escriben", "lee", "leen", "roba", "roban",
    "mata", "matan", "ataca", "atacan", "defiende", "defienden", "escapa",
    "escapan", "entra", "entran", "sale", "salen", "sube", "suben", "baja",
    "bajan", "abre", "abren", "cierra", "cierran", "rompe", "rompen",
    "quema", "queman", "busca", "buscan", "observa", "observan", "señala",
    "señalan", "toca", "tocan", "habla", "hablan", "grita", "gritan",
    "muere", "mueren", "nace", "nacen", "carga", "cargan", "dispara",
    "disparan", "firma", "firman", "pinta", "pintan", "trabaja", "trabajan",
    # English
    "walks", "walk", "runs", "run", "flees", "flee", "fights", "fight",
    "discovers", "discover", "finds", "find", "builds", "build", "digs",
    "dig", "sails", "sail", "writes", "write", "reads", "read", "steals",
    "steal", "attacks", "attack", "defends", "defend", "escapes", "escape",
    "enters", "enter", "leaves", "leave", "climbs", "climb", "opens",
    "open", "closes", "close", "burns", "burn", "searches", "search",
    "observes", "observe", "dies", "die", "carries", "carry", "shoots",
    "shoot", "signs", "sign", "paints", "paint", "works", "work",
    # English gerunds (stock queries usually use them)
    "crossing", "crosses", "cross", "walking", "running", "fleeing",
    "fighting", "discovering", "finding", "building", "digging", "sailing",
    "writing", "reading", "stealing", "attacking", "defending", "escaping",
    "entering", "leaving", "climbing", "opening", "closing", "burning",
    "searching", "observing", "carrying", "shooting", "signing", "painting",
    "working", "rowing", "riding", "praying", "trading", "marching",
})

# Purely abstract vocabulary → symbolic depiction when no concrete/action cue.
_ABSTRACT_TERMS: frozenset[str] = frozenset({
    # Spanish
    "idea", "ideas", "concepto", "conceptos", "pensamiento", "pensamientos",
    "memoria", "memorias", "tiempo", "destino", "miedo", "amor",
    "conciencia", "alma", "espíritu", "esperanza", "duda", "verdad",
    "mentira", "sueño", "sueños", "muerte", "vida", "eternidad", "silencio",
    "libertad", "poder", "fe", "olvido", "abstracto", "abstracta",
    "metáfora", "símbolo", "simboliza", "representa", "creencias",
    # English
    "concept", "concepts", "thought", "thoughts", "memory", "memories",
    "time", "fate", "fear", "love", "consciousness", "soul", "spirit",
    "hope", "doubt", "truth", "lie", "dream", "dreams", "death", "life",
    "eternity", "silence", "freedom", "power", "faith", "oblivion",
    "symbol", "metaphor", "abstract", "belief", "beliefs",
})

# Concrete subjects/objects → literal depiction when no abstraction dominates.
_CONCRETE_TERMS: frozenset[str] = frozenset({
    # Spanish
    "explorador", "exploradora", "barco", "castillo", "templo", "ciudad",
    "desierto", "montaña", "mar", "río", "caballo", "soldado", "rey",
    "reina", "mercado", "pergamino", "mapa", "espada", "antorcha", "ruinas",
    "columnas", "instrumento", "máquina", "laboratorio", "documento",
    "archivo", "excavación", "tumba", "nave", "avión", "coche", "tren",
    "pirámide", "momia", "estatua", "moneda", "anillo", "reliquia",
    # English
    "explorer", "ship", "boat", "castle", "temple", "city", "desert",
    "mountain", "sea", "river", "horse", "soldier", "king", "queen",
    "market", "scroll", "map", "sword", "torch", "ruins", "columns",
    "instrument", "machine", "laboratory", "document", "archive",
    "excavation", "tomb", "plane", "car", "train", "pyramid", "mummy",
    "statue", "coin", "ring", "relic", "artifact", "artefacto",
})

_DEPICTION_MODES = ("literal", "documentary", "contextual", "symbolic")


def _as_scene_text(scene: Any) -> str:
    """Best-effort extraction of the narration text from a scene-like object."""
    if isinstance(scene, str):
        return scene
    if isinstance(scene, dict):
        for key in ("fragment_text", "texto", "narration", "text", "search_query_en"):
            val = scene.get(key)
            if val:
                return str(val)
    return ""


def _as_scene_query_en(scene: Any) -> str:
    """English stock query for the scene, if the caller provided one."""
    if isinstance(scene, dict):
        val = scene.get("search_query_en")
        if val:
            return str(val)
    return ""


def _clip(text: str, max_len: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0].rstrip(" ,.")


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def _find_action_phrase(text: str) -> str:
    """Return the first ``verb + up to 2 following words`` action phrase, or ""."""
    if not text:
        return ""
    low = text.lower()
    for match in _WORD_RE.finditer(low):
        if match.group(0) in _ACTION_VERBS:
            words = low[match.start():].split()
            return " ".join(words[:3]).strip(" ,.")
    return ""


def derive_depiction_mode(scene: Any) -> str:
    """Deterministic depiction mode for a scene (fail-open → ``documentary``).

    Rules:
      - ``literal``     when the fragment contains an action verb or a concrete
                        subject/object and is not purely abstract.
      - ``symbolic``    when the fragment is purely abstract (no action, no
                        concrete cue).
      - ``documentary`` in every other case (including empty text).
    """
    try:
        text = _as_scene_text(scene).strip()
        if not text:
            return "documentary"
        toks = _tokens(text)
        has_action = bool(toks & _ACTION_VERBS)
        has_abstract = bool(toks & _ABSTRACT_TERMS)
        has_concrete = bool(toks & _CONCRETE_TERMS)
        if has_action or (has_concrete and not has_abstract):
            return "literal"
        if has_abstract and not has_concrete and not has_action:
            return "symbolic"
        return "documentary"
    except Exception:
        return "documentary"


def derive_scene_intent(scene: Any, setting: str = "") -> dict:
    """Derive a minimal, best-effort visual intent from a scene/fragment.

    Returns a dict with the Fase 3 intent fields.  Never raises; unknown
    fields stay empty rather than being guessed wrong.  When an English
    ``search_query_en`` is available it is preferred for ``subject`` so stock
    queries stay English and cache-safe.
    """
    intent: dict[str, Any] = {
        "subject": "",
        "action": "",
        "object": "",
        "setting": "",
        "must_show": [],
        "must_avoid": [],
        "depiction_mode": "documentary",
    }
    try:
        text = _as_scene_text(scene).strip()
        query_en = _as_scene_query_en(scene).strip()
        mode = derive_depiction_mode(scene)
        intent["depiction_mode"] = mode

        source = query_en or text
        if source:
            intent["subject"] = _clip(source, 80)
        if query_en:
            action = _find_action_phrase(query_en)
        else:
            action = _find_action_phrase(text)
        if action:
            intent["action"] = _clip(action, 60)
        if setting:
            intent["setting"] = _clip(str(setting), 60)

        shown = [
            p for p in (intent["action"], intent["subject"]) if p
        ]
        intent["must_show"] = list(dict.fromkeys(shown))[:2]
    except Exception:
        pass
    return intent


@dataclass
class SceneVisualContext:
    fragment: str = ""
    block_text: str = ""
    phase_id: str = "default"
    phase_label: str = ""
    script_title: str = ""
    prev_snippet: str = ""
    next_snippet: str = ""
    media_tipo: str = "image"
    visual_concept: str = ""
    bridge_from_prev: str = ""
    central_entity: str = ""
    recurring_elements: list[str] = field(default_factory=list)
    visual_universe: str = ""
    era: str = ""
    forbidden_elements: list[str] = field(default_factory=list)

    # ── Fase 3: filmable per-scene intent ──────────────────────
    subject: str = ""
    action: str = ""
    object: str = ""
    setting: str = ""
    must_show: list[str] = field(default_factory=list)
    must_avoid: list[str] = field(default_factory=list)
    depiction_mode: str = ""

    # ── Compact English query variant for stock providers ────
    def _intent_phrase(self) -> str:
        """action + subject + object + setting, de-duplicated and ordered."""
        pieces = (
            self.action, self.subject, self.object, self.setting,
        )
        return " ".join(
            dict.fromkeys(p.strip() for p in pieces if p and p.strip())
        )

    def to_query_variant(self, max_len: int = 100) -> str:
        """A short English query grounding the scene in the global visual world.

        Priority (Fase 3): the filmable intent (action → subject → object →
        setting) → the scene's visual concept → a recurring motif/entity → the
        era.  Always English, always <= ``max_len``, safe for stock APIs.
        """
        parts: list[str] = []
        intent = self._intent_phrase()
        if intent:
            parts.append(intent)
        elif self.visual_concept:
            parts.append(self.visual_concept)
        elif self.central_entity:
            parts.append(self.central_entity)
        if self.recurring_elements:
            parts.append(self.recurring_elements[0])
        if self.era and self.era not in ("atemporal", "presente"):
            parts.append(self.era)
        query = " ".join(dict.fromkeys(p.strip(" ,.") for p in parts if p))
        if len(query) <= max_len:
            return query
        return query[:max_len].rsplit(" ", 1)[0].rstrip(" ,.")

    # ── Human-readable brief for the LLM reranker ─────────────
    def to_rerank_brief(self) -> str:
        """A compact narrative+visual brief the reranker can reason over."""
        lines: list[str] = []
        if self.script_title:
            lines.append(f"Tema del video: {self.script_title}")
        if self.phase_label:
            lines.append(f"Fase narrativa: {self.phase_label} ({self.phase_id})")
        if self.fragment:
            lines.append(f"Narración (escena): \"{self.fragment[:300]}\"")
        if self.visual_concept:
            lines.append(f"Dirección visual: {self.visual_concept}")
        if self.bridge_from_prev:
            lines.append(f"Puente con escena anterior: {self.bridge_from_prev}")
        if self.central_entity:
            lines.append(f"Entidad central: {self.central_entity}")
        # Fase 3: filmable intent.
        if self.subject:
            lines.append(f"Sujeto: {self.subject}")
        if self.action:
            lines.append(f"Acción: {self.action}")
        if self.object:
            lines.append(f"Objeto: {self.object}")
        if self.setting:
            lines.append(f"Espacio: {self.setting}")
        if self.depiction_mode:
            lines.append(f"Modo de representación: {self.depiction_mode}")
        if self.must_show:
            lines.append(f"Debe mostrar: {', '.join(self.must_show[:6])}")
        if self.era and self.era not in ("atemporal", "presente"):
            lines.append(f"Época: {self.era}")
        if self.forbidden_elements:
            lines.append(f"Evitar mostrar: {', '.join(self.forbidden_elements[:6])}")
        if self.must_avoid:
            lines.append(f"Evitar (must_avoid): {', '.join(self.must_avoid[:6])}")
        return "\n".join(lines)


def build_scene_context(
    scene: dict,
    scene_idx: int = 0,
    theme_ctx: Any = None,
    visual_bible: dict | None = None,
    structure: list[dict] | None = None,
    script_title: str = "",
    prev_snippet: str = "",
    next_snippet: str = "",
    temporal_overrides_enabled: bool = True,
) -> SceneVisualContext:
    """Assemble the context for one scene from every available source."""
    ctx = SceneVisualContext(
        fragment=scene.get("fragment_text") or scene.get("texto", "") or "",
        block_text=scene.get("texto", "") or "",
        script_title=script_title or scene.get("video_title", ""),
        media_tipo=str(scene.get("media_tipo", "imagen")),
        prev_snippet=prev_snippet or str(scene.get("prev_snippet", "") or ""),
        next_snippet=next_snippet or str(scene.get("next_snippet", "") or ""),
    )

    pid = scene.get("phase_id")
    if structure:
        for p in structure:
            if p.get("id") == pid:
                ctx.phase_id = pid or ctx.phase_id
                ctx.phase_label = p.get("step", "")
                break
    ctx.phase_id = pid or ctx.phase_id

    # Theme / era anchoring.  Prefer the per-scene era from temporal_segments
    # (Fase 3) so a deliberate time jump re-anchors the scene correctly.
    effective_era = ""
    if theme_ctx is not None:
        effective_era = (
            _cget(theme_ctx, "era_decade", "") or _cget(theme_ctx, "era", "") or ""
        )
        if temporal_overrides_enabled:
            try:
                from pipeline.theme_extractor import era_for_scene

                seg_era = era_for_scene(theme_ctx, scene_idx)
                if seg_era:
                    effective_era = seg_era
            except Exception:
                pass
        ctx.era = effective_era
        ctx.forbidden_elements = list(
            _cget(theme_ctx, "forbidden_elements", []) or []
        )

    # Visual bible → global + per-scene direction.
    if visual_bible:
        ctx.visual_universe = _cget(visual_bible, "visual_universe", "") or ""
        entity = _cget(visual_bible, "central_entity", {}) or {}
        if isinstance(entity, dict) and entity.get("type") not in (None, "none"):
            if scene_idx in (entity.get("appears_in_scenes") or []):
                ctx.central_entity = (
                    entity.get("variation_by_scene", {}).get(str(scene_idx))
                    or entity.get("master_description", "")
                )
        ctx.recurring_elements = list(
            _cget(visual_bible, "recurring_elements", []) or []
        )[:3]
        scene_map = _cget(visual_bible, "scene_visual_map", []) or []
        if scene_idx < len(scene_map):
            vscene = scene_map[scene_idx] or {}
            ctx.visual_concept = _cget(vscene, "visual_concept", "") or ""
            ctx.bridge_from_prev = _cget(vscene, "bridge_from_prev", "") or ""
            # Fase 3 per-scene intent (empty fields fall back below).
            ctx.subject = _cget(vscene, "subject", "") or ""
            ctx.action = _cget(vscene, "action", "") or ""
            ctx.object = _cget(vscene, "object", "") or ""
            ctx.setting = _cget(vscene, "setting", "") or ""
            ctx.must_show = list(_cget(vscene, "must_show", []) or [])
            ctx.must_avoid = list(_cget(vscene, "must_avoid", []) or [])
            ctx.depiction_mode = _cget(vscene, "depiction_mode", "") or ""

    # Depiction mode fallback (always available, deterministic).
    if not ctx.depiction_mode:
        ctx.depiction_mode = derive_depiction_mode(scene)

    # ── must_avoid: explicit lists + theme forbidden + historical
    #    anachronisms present in the narration.  Deliberately-modern
    #    temporal_segments are exempt so "material actual en documentales
    #    históricos" is not vetoed (Fase 3).
    avoid: list[str] = list(ctx.must_avoid)
    for forbidden in ctx.forbidden_elements:
        if forbidden and forbidden not in avoid:
            avoid.append(forbidden)
    try:
        from pipeline.era_terms import (
            anachronism_hits,
            era_anchor,
            is_anachronism_exempt,
        )

        exempt = (
            is_anachronism_exempt(scene, theme_ctx, scene_idx=scene_idx)
            if temporal_overrides_enabled
            else False
        )
        historical = bool(era_anchor("", effective_era)) if effective_era else False
        if historical and not exempt:
            for hit in anachronism_hits(ctx.fragment):
                if hit not in avoid:
                    avoid.append(hit)
    except Exception:
        pass
    ctx.must_avoid = avoid

    return ctx
