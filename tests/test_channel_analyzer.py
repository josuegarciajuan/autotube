"""Tests de robustez del analizador de Insights AI.

Cubren parseo defensivo de JSON, normalización de recomendaciones y la
integración del streaming con validadores, sin DB real ni red.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import api.services.channel_analyzer as ca  # noqa: E402


# ── Fakes para streaming ────────────────────────────────────────────

class _Delta:
    def __init__(self, content=None, reasoning_content=None):
        self.content = content
        self.reasoning_content = reasoning_content


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Chunk:
    def __init__(self, content):
        self.choices = [_Choice(_Delta(content=content))]


class _Completions:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = 0

    def create(self, **kwargs):
        payload = self._payloads[self.calls]
        self.calls += 1
        return iter([_Chunk(payload)])


class FakeStreamingClient:
    """Cliente compatible con el bucle de _run_phase_streaming."""

    def __init__(self, payloads):
        self.completions = _Completions(payloads)
        self.chat = SimpleNamespace(completions=self.completions)


class StubDB:
    """Stub mínimo de ExtendedDatabase para los tests de integración."""

    def __init__(self, *args, **kwargs):
        self.completed = []
        self.failed = []

    def get_channel_insights(self, channel_id, limit=10):
        return []

    def get_channels(self):
        return []

    def get_channel(self, *args, **kwargs):
        return {}

    def update_insight_retry(self, *args, **kwargs):
        pass

    def update_insight_phase(self, *args, **kwargs):
        pass

    def update_insight_phase_detail(self, *args, **kwargs):
        pass

    def update_insight_heartbeat(self, *args, **kwargs):
        pass

    def complete_insight(self, *args, **kwargs):
        self.completed.append((args, kwargs))

    def fail_insight(self, *args, **kwargs):
        self.failed.append((args, kwargs))


# ── 1-5: _extract_json_from_content ────────────────────────────────

def test_extract_valid_json_with_trailing_text():
    out = ca._extract_json_from_content(
        'Aquí está el resultado: {"a": 1, "b": [2, 3]} espero que sirva'
    )
    assert out == {"a": 1, "b": [2, 3]}


def test_extract_markdown_fence():
    out = ca._extract_json_from_content(
        'preamble\n```json\n{"patterns": []}\n```\ntrailing'
    )
    assert out == {"patterns": []}


def test_extract_truncated_json_recovered():
    full = (
        '{"recommendations": ['
        '{"id": "a", "config_changes": {"A": 1}}, '
        '{"id": "b", "title": "texto que se corta a mitad'
    )
    out = ca._extract_json_from_content(full)
    assert isinstance(out, dict)
    recs = out.get("recommendations")
    assert isinstance(recs, list) and recs
    assert recs[0]["id"] == "a"
    assert recs[0]["config_changes"] == {"A": 1}


def test_extract_unrecoverable_raises():
    with pytest.raises((json.JSONDecodeError, ValueError)):
        ca._extract_json_from_content("esto no es json en absoluto {{{")


def test_extract_non_object_root_raises():
    with pytest.raises(ValueError, match="non-object JSON root"):
        ca._extract_json_from_content("[1, 2, 3]")


# ── 6-7: _normalize_recommendation ─────────────────────────────────

def test_normalize_recommendation_null_config_changes():
    rec = ca._normalize_recommendation({"config_changes": None})
    assert rec["config_changes"] == {}
    assert rec["data_cited"] == {}
    assert rec["requires_code"] is False


def test_normalize_recommendation_non_dict_returns_none():
    assert ca._normalize_recommendation("no-dict") is None


def test_normalize_recommendation_requires_code_prompt():
    rec = ca._normalize_recommendation(
        {"requires_code": True, "opencode_prompt": None}
    )
    assert rec["requires_code"] is True
    assert rec["opencode_prompt"] == ""


# ── 8-10: validadores de payload ───────────────────────────────────

def test_validate_unified_payload_normalizes_nulls():
    payload = {
        "recommendations": [
            {"id": "r1", "config_changes": None, "data_cited": None},
        ],
        "hypotheses": None,
        "key_metrics": "bad",
        "analysis_summary": 123,
    }
    out = ca._validate_unified_payload(payload)
    rec = out["recommendations"][0]
    assert rec["config_changes"] == {}
    assert rec["data_cited"] == {}
    assert out["hypotheses"] == []
    assert out["key_metrics"] == []
    assert out["analysis_summary"] == ""


def test_validate_unified_payload_missing_recommendations_raises():
    with pytest.raises(ValueError):
        ca._validate_unified_payload({"hypotheses": []})


def test_validate_unified_payload_empty_recommendations_ok():
    out = ca._validate_unified_payload({"recommendations": []})
    assert out["recommendations"] == []


def test_validate_exploration_payload_empty_raises():
    with pytest.raises(ValueError, match="no valid patterns"):
        ca._validate_exploration_payload({"patterns": []})
    with pytest.raises(ValueError, match="no valid patterns"):
        ca._validate_exploration_payload({"patterns": [1, "x"]})


def test_validate_exploration_payload_valid_ok():
    out = ca._validate_exploration_payload(
        {"patterns": [{"name": "p"}, "invalid"]}
    )
    assert out["patterns"] == [{"name": "p"}]


def test_validate_exploration_payload_not_dict_raises():
    with pytest.raises(ValueError):
        ca._validate_exploration_payload([])


# ── 11: _config_key_overlap ────────────────────────────────────────

def test_config_key_overlap_with_null_config_changes():
    assert ca._config_key_overlap(
        {"config_changes": None}, {"config_changes": {"A": 1}}
    ) == 0.0


# ── 12: streaming + retry con validador ────────────────────────────

def test_streaming_with_validator_accepts(monkeypatch):
    monkeypatch.setattr(ca, "ExtendedDatabase", lambda *a, **k: StubDB())
    client = FakeStreamingClient(['{"ok": true}'])

    out = ca._run_phase_streaming_with_retry(
        client, "fake-model",
        [{"role": "user", "content": "hola"}],
        max_tokens=100, temperature=0.0,
        insight_id=1, phase_label="test",
        validator=lambda payload: payload,
    )
    assert out["content"] == {"ok": True}


def test_streaming_retry_falls_back_without_thinking(monkeypatch):
    monkeypatch.setattr(ca, "ExtendedDatabase", lambda *a, **k: StubDB())

    def reject_first(payload):
        if "recommendations" not in payload:
            raise ValueError("missing recommendations")
        return payload

    # Primer intento: payload inválido; fallback: payload válido.
    client = FakeStreamingClient([
        '{"wrong": true}',
        '{"recommendations": []}',
    ])
    monkeypatch.setattr(ca, "create_llm_client", lambda **kwargs: client)

    out = ca._run_phase_streaming_with_retry(
        client, "fake-model",
        [{"role": "user", "content": "hola"}],
        max_tokens=100, temperature=0.0,
        insight_id=2, phase_label="test",
        validator=reject_first,
    )
    assert out["content"] == {"recommendations": []}
    assert client.completions.calls == 2


# ── 13: end-to-end ligero ──────────────────────────────────────────

def test_run_channel_analysis_sync_end_to_end(monkeypatch):
    stub = StubDB()
    monkeypatch.setattr(ca, "ExtendedDatabase", lambda *a, **k: stub)
    monkeypatch.setattr(ca, "_aggregate_channel_data", lambda db, cid, slug: {})
    monkeypatch.setattr(
        ca, "get_channel_config",
        lambda slug, force_reload=True: SimpleNamespace(),
    )
    monkeypatch.setattr(ca, "_serialize_config", lambda cfg: {})

    fake = FakeStreamingClient([
        json.dumps({"patterns": [{"name": "p"}]}),
        json.dumps({
            "recommendations": [
                {"id": "r1", "title": "t",
                 "config_changes": None, "data_cited": None},
            ],
            "hypotheses": [],
            "key_metrics": [],
            "analysis_summary": "resumen",
        }),
    ])
    monkeypatch.setattr(ca, "create_llm_client", lambda **kwargs: fake)

    ca.run_channel_analysis_sync(insight_id=1, channel_id=1, slug="canal2")

    assert len(stub.completed) == 1
    assert stub.failed == []
    # El JSON final debe haberse serializado con config_changes normalizado.
    insights_json = stub.completed[0][1]["insights_json"]
    saved = json.loads(insights_json)
    assert saved["recommendations"][0]["config_changes"] == {}
