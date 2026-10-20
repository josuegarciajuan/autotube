"""Tests for standalone-shorts fixes: LLM array parsing + word-budget trim."""

import json

import pytest


# ── llm_json_array_call ──────────────────────────────────────────

class _FakeCompletions:
    def __init__(self, resp):
        self._resp = resp

    def create(self, **kwargs):
        return self._resp


class _FakeChat:
    def __init__(self, resp):
        self.completions = _FakeCompletions(resp)


class _FakeClient:
    def __init__(self, content):
        msg = type("Msg", (), {"content": content})()
        choice = type("Choice", (), {"message": msg})()
        self.chat = _FakeChat(type("Resp", (), {"choices": [choice]})())


def _parse(content):
    from config.llm_helpers import llm_json_array_call
    return llm_json_array_call(_FakeClient(content), model="x", messages=[])


def test_array_bare_json():
    """A bare JSON array response must come back as a list (regression)."""
    res = _parse('[{"title": "T1"}, {"title": "T2"}]')
    assert isinstance(res, list)
    assert [t["title"] for t in res] == ["T1", "T2"]


def test_array_markdown_fenced():
    res = _parse('```json\n[{"title": "A"}, {"title": "B"}]\n```')
    assert isinstance(res, list)
    assert [t["title"] for t in res] == ["A", "B"]


def test_single_object_wrapped():
    res = _parse('{"title": "Solo"}')
    assert isinstance(res, list)
    assert res[0]["title"] == "Solo"


def test_array_with_preamble():
    res = _parse('Aquí tienes:\n[{"title": "X"}, {"title": "Y"}] Fin')
    assert isinstance(res, list)
    assert len(res) == 2


# ── trim_blocks_to_word_budget ───────────────────────────────────

def _blocks():
    return [
        {"tipo": "hook", "texto": " ".join(["w"] * 12)},
        {"tipo": "desarrollo1", "texto": " ".join(["w"] * 40)},
        {"tipo": "desarrollo2", "texto": " ".join(["w"] * 40)},
        {"tipo": "desarrollo3", "texto": " ".join(["w"] * 40)},
        {"tipo": "climax", "texto": " ".join(["w"] * 20)},
        {"tipo": "cierre", "texto": " ".join(["w"] * 15)},
    ]


def _word_count(bloques):
    return sum(len(b["texto"].split()) for b in bloques)


def test_trim_under_budget_is_noop():
    from pipeline.shorts_tts import trim_blocks_to_word_budget
    bloques = [{"tipo": "hook", "texto": "a b c d"}]
    trim_blocks_to_word_budget(bloques, max_words=90)
    assert bloques[0]["texto"] == "a b c d"


def test_trim_to_budget_keeps_hook_and_cierre():
    from pipeline.shorts_tts import trim_blocks_to_word_budget
    bloques = _blocks()
    assert _word_count(bloques) == 167
    trim_blocks_to_word_budget(bloques, max_words=90)
    assert _word_count(bloques) <= 90
    # Hook and cierre must stay intact (words removed from later blocks first)
    assert len(bloques[0]["texto"].split()) == 12
    assert len(bloques[5]["texto"].split()) == 15


def test_trim_harder_budget():
    from pipeline.shorts_tts import trim_blocks_to_word_budget
    bloques = _blocks()
    trim_blocks_to_word_budget(bloques, max_words=72)
    assert _word_count(bloques) <= 72


# ── Normalización defensiva del script (listas del LLM) ──────────

def test_normalize_short_script_coerces_list_valued_fields():
    from pipeline.shorts_tts import normalize_short_script, validate_short_script
    script = {
        "tema": ["tema", "compuesto"],
        "titulo": "Título",
        "bloques": [
            {"tipo": "hook", "texto": ["Hola", "mundo"],
             "search_query_en": ["dark", "forest"]},
            {"tipo": "desarrollo1", "texto": "texto uno"},
            {"tipo": "desarrollo2", "texto": "texto dos"},
            {"tipo": "climax", "texto": "texto tres"},
            {"tipo": "cierre", "texto": "texto cuatro"},
        ],
    }
    normalize_short_script(script)
    assert script["tema"] == "tema compuesto"
    assert script["bloques"][0]["texto"] == "Hola mundo"
    assert script["bloques"][0]["search_query_en"] == "dark forest"
    assert isinstance(script["bloques"][0]["texto"], str)


def test_build_query_pool_accepts_list_search_query():
    from pipeline.shorts_media import _build_query_pool
    pool = _build_query_pool({
        "tipo": "hook",
        "texto": "una escena",
        "search_query_en": ["dark", "forest", "night"],
    })
    assert pool, "el pool no puede quedar vacío con una lista coercible"
    assert any("dark forest night" in q for q in pool)
