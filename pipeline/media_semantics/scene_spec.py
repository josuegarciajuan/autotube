"""P01-B — build one :class:`SceneSpec` per FINAL scene.

The final scene ranges produced by ``video_editor._compute_block_ranges``
(after merge/split) carry ``fragment_text``/``query_context`` for their exact
narration slice.  This module turns that slice plus the existing
``SceneVisualContext`` into a structured spec, without new LLM calls.  The
visual bible remains the primary source of filmable intent; deterministic
heuristics are only a fallback.
"""

from __future__ import annotations

from typing import Any, Callable

from .contracts import SceneSpec

_SHOT_BY_DEPICTION = {
    "literal": "medium shot, observable action",
    "documentary": "wide establishing shot, documentary b-roll",
    "contextual": "contextual detail shot, environmental b-roll",
    "symbolic": "atmospheric symbolic shot, no literal event",
}


def _scene_fragment(scene: dict[str, Any]) -> str:
    """Narration text that the FINAL scene actually covers."""
    for key in ("fragment_text", "query_context", "texto", "narration"):
        value = scene.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _origin(scene: dict[str, Any]) -> str:
    if scene.get("is_subscene"):
        return "subscene"
    if scene.get("merged_from") or scene.get("merged_count") or scene.get("is_merged"):
        return "merged"
    return "block"


def _block_idx(scene: dict[str, Any]) -> int | None:
    value = scene.get("block_idx", scene.get("asset_idx"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_scene_spec(
    scene: dict[str, Any],
    scene_ctx: Any = None,
    *,
    scene_idx: int | None = None,
    parent_scene_idx: int | None = None,
) -> SceneSpec:
    """Derive a :class:`SceneSpec` from a final scene range + visual context.

    ``scene_ctx`` is an existing ``SceneVisualContext`` (duck-typed); when it
    is missing every field still falls back to deterministic scene data.
    """
    if scene_idx is None:
        try:
            scene_idx = int(scene.get("scene_idx", 0))
        except (TypeError, ValueError):
            scene_idx = 0
    if parent_scene_idx is None:
        try:
            parent_scene_idx = int(scene["parent_scene_idx"])
        except (KeyError, TypeError, ValueError):
            parent_scene_idx = None

    fragment = _scene_fragment(scene)
    subject = str(getattr(scene_ctx, "subject", "") or "").strip()
    action = str(getattr(scene_ctx, "action", "") or "").strip()
    environment = str(getattr(scene_ctx, "setting", "") or "").strip()
    visual_concept = str(getattr(scene_ctx, "visual_concept", "") or "").strip()

    # Deterministic fallbacks: never invent facts.  For a sub-scene with an
    # inherited parent query we derive the observable action from the FINAL
    # narration fragment, so two sub-scenes of one block cannot share the
    # exact same intent/query (P01-B/P01-C).
    is_subscene = bool(scene.get("is_subscene"))
    if not action and fragment:
        try:
            from pipeline.scene_context import derive_scene_intent

            local = derive_scene_intent({"fragment_text": fragment})
            action = str(local.get("action") or "").strip()
            if is_subscene and not subject:
                subject = str(local.get("subject") or "").strip()
        except Exception:
            pass
    if not subject:
        subject = str(scene.get("search_query_en", "") or "").strip()
    if not environment:
        environment = str(getattr(scene_ctx, "visual_universe", "") or "").strip()
    if not visual_concept:
        visual_concept = str(getattr(scene_ctx, "central_entity", "") or "").strip()

    era = str(getattr(scene_ctx, "era", "") or scene.get("era", "") or "").strip()
    depiction = str(
        getattr(scene_ctx, "depiction_mode", "") or "documentary"
    ).strip()
    desired_shot = str(
        scene.get("desired_shot", scene.get("shot", "")) or ""
    ).strip()
    if not desired_shot:
        desired_shot = _SHOT_BY_DEPICTION.get(depiction, _SHOT_BY_DEPICTION["documentary"])

    must_include: list[str] = []
    for item in list(getattr(scene_ctx, "must_show", []) or []) + list(
        scene.get("must_show", []) or []
    ):
        text = str(item or "").strip()
        if text and text not in must_include:
            must_include.append(text)

    must_avoid: list[str] = []
    for item in list(getattr(scene_ctx, "must_avoid", []) or []) + list(
        scene.get("must_avoid", []) or []
    ):
        text = str(item or "").strip()
        if text and text not in must_avoid:
            must_avoid.append(text)

    bible_sourced = bool(
        getattr(scene_ctx, "subject", "") or getattr(scene_ctx, "action", "")
    )
    source = "visual_bible" if bible_sourced else "deterministic"
    confidence = 0.9 if bible_sourced else 0.6

    try:
        duration = float(scene.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0

    return SceneSpec(
        scene_idx=int(scene_idx),
        block_idx=_block_idx(scene),
        parent_scene_idx=parent_scene_idx,
        media_request_id=str(scene.get("media_request_id", "") or ""),
        origin=_origin(scene),
        narration_fragment=fragment,
        subject=subject,
        action=action,
        environment=environment,
        era=era,
        desired_shot=desired_shot,
        visual_concept=visual_concept,
        mood_style=str(scene.get("mood", scene.get("mood_style", "")) or ""),
        must_include=must_include,
        must_avoid=must_avoid,
        previous_context=str(getattr(scene_ctx, "prev_snippet", "") or ""),
        next_context=str(getattr(scene_ctx, "next_snippet", "") or ""),
        duration=duration,
        media_type=str(scene.get("media_tipo", "image") or "image"),
        depiction_mode=depiction,
        source=source,
        confidence=confidence,
        extra={
            "phase_id": str(getattr(scene_ctx, "phase_id", "") or scene.get("phase_id", "")),
            "phase_label": str(getattr(scene_ctx, "phase_label", "") or ""),
            "script_title": str(getattr(scene_ctx, "script_title", "") or ""),
            "block_text": str(getattr(scene_ctx, "block_text", "") or ""),
            "search_query_en": str(scene.get("search_query_en", "") or ""),
            "query_context": str(scene.get("query_context", "") or ""),
            "media_request_id": str(scene.get("media_request_id", "") or ""),
            "parent_tipo": str(scene.get("parent_tipo", "") or ""),
        },
    )


def attach_scene_specs(
    scenes: list[dict[str, Any]],
    context_builder: Callable[[dict[str, Any], int], Any],
) -> list[SceneSpec]:
    """Attach a spec to every final scene in place; return the spec list.

    ``context_builder(scene, scene_idx)`` must return a ``SceneVisualContext``
    (or a compatible object).  Fail-open: if a scene cannot be enriched, a
    minimal deterministic spec is still produced.
    """
    specs: list[SceneSpec] = []
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            specs.append(SceneSpec(scene_idx=index))
            continue
        try:
            ctx = context_builder(scene, index)
        except Exception:
            ctx = None
        spec = build_scene_spec(scene, ctx, scene_idx=index)
        if spec.origin == "subscene" and spec.parent_scene_idx is None:
            # The parent block is identified by asset_idx (the original block).
            spec.parent_scene_idx = spec.block_idx
        # Store the JSON-safe form: the same scene ranges are persisted in the
        # media checkpoint (json.dumps), so a dataclass instance would break
        # resume.  `_scene_spec_for` accepts both dicts and SceneSpec objects.
        scene["scene_spec"] = spec.to_dict()
        scene["scene_idx"] = index
        scene["final_scene_idx"] = index
        scene.setdefault("block_idx", spec.block_idx)
        scene.setdefault("parent_scene_idx", spec.parent_scene_idx)
        scene.setdefault("media_request_id", spec.media_request_id)
        scene.setdefault("narration_fragment", spec.narration_fragment)
        specs.append(spec)
    return specs
