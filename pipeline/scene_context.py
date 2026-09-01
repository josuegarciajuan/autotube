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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _cget(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


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

    # ── Compact English query variant for stock providers ────
    def to_query_variant(self, max_len: int = 100) -> str:
        """A short English query grounding the scene in the global visual world.

        Priority: the scene's visual concept → a compact recurring motif/entity
        → the era. Always English, always <= ``max_len``, safe for stock APIs.
        """
        parts: list[str] = []
        if self.visual_concept:
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
        if self.era and self.era not in ("atemporal", "presente"):
            lines.append(f"Época: {self.era}")
        if self.forbidden_elements:
            lines.append(f"Evitar mostrar: {', '.join(self.forbidden_elements[:6])}")
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
) -> SceneVisualContext:
    """Assemble the context for one scene from every available source."""
    ctx = SceneVisualContext(
        fragment=scene.get("fragment_text") or scene.get("texto", "") or "",
        block_text=scene.get("texto", "") or "",
        script_title=script_title or scene.get("video_title", ""),
        media_tipo=str(scene.get("media_tipo", "imagen")),
        prev_snippet=prev_snippet,
        next_snippet=next_snippet,
    )

    pid = scene.get("phase_id")
    if structure:
        for p in structure:
            if p.get("id") == pid:
                ctx.phase_id = pid or ctx.phase_id
                ctx.phase_label = p.get("step", "")
                break
    ctx.phase_id = pid or ctx.phase_id

    # Theme / era anchoring.
    if theme_ctx is not None:
        ctx.era = _cget(theme_ctx, "era_decade", "") or _cget(theme_ctx, "era", "")
        ctx.forbidden_elements = list(_cget(theme_ctx, "forbidden_elements", []) or [])

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

    return ctx
