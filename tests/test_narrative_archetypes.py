"""Tests de T1.5: arquetipos narrativos variables (anti-plantilla)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.narrative_archetypes import (  # noqa: E402
    DEFAULT_ARCHETYPES,
    archetype_prompt_block,
    get_archetypes,
    pick_archetype,
)
from prompts.base_prompts import build_outline_prompt  # noqa: E402


class _Cfg:
    CANAL_TONE = "Documental"
    CANAL_NARRATIVE_STYLE = "documental medico"
    TARGET_AUDIENCE = "publico adulto"
    NARRATIVE_ARCHETYPES = []


def test_default_archetypes_are_distinct():
    keys = [a["key"] for a in DEFAULT_ARCHETYPES]
    assert len(keys) == len(set(keys)) >= 4
    assert all(a.get("guidance") for a in DEFAULT_ARCHETYPES)


def test_pick_is_deterministic_with_seed():
    a1 = pick_archetype(_Cfg(), seed=7)
    a2 = pick_archetype(_Cfg(), seed=7)
    assert a1 == a2


def test_pick_varies_across_seeds():
    picks = {pick_archetype(_Cfg(), seed=s).key for s in range(12)}
    assert len(picks) >= 3, "debe haber variedad real de estructura entre vídeos"


def test_config_override():
    cfg = _Cfg()
    cfg.NARRATIVE_ARCHETYPES = [
        {"key": "unico", "name": "Único", "guidance": "Solo uno."}
    ]
    arches = get_archetypes(cfg)
    assert [a.key for a in arches] == ["unico"]
    assert pick_archetype(cfg, seed=1).key == "unico"


def test_prompt_block_contains_guidance():
    arch = pick_archetype(_Cfg(), seed=3)
    block = archetype_prompt_block(arch)
    assert arch.name in block
    assert arch.key in block
    assert arch.guidance.split(".")[0][:20] in block


def test_outline_prompt_includes_archetype():
    cfg = _Cfg()
    prompt = build_outline_prompt(cfg, duration_min=12, variant_seed=42)
    arch = pick_archetype(cfg, seed=42)
    assert arch.name in prompt
    assert "ARQUETIPO NARRATIVO" in prompt
