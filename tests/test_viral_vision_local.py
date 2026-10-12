"""Tests for the configurable / local-API vision analyzer in viral_cloner.

Covers:
  - Real MIME detection (magic bytes) and rejection of unsupported formats.
  - Size guard before sending (``VISION_MAX_IMAGE_MB``).
  - JSON parsing / schema validation -> degrade to ``None`` (never raise).
  - Happy path with a mocked OpenAI-compatible client, using the configured
    local endpoint (single vision candidate, no DeepSeek fallback).

Run:  python3 -m pytest tests/test_viral_vision_local.py -q
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config.settings as settings
import config.llm_client as llm_client
from pipeline import viral_cloner


PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPEG_MAGIC = b"\xff\xd8\xff" + b"0" * 64
WEBP_MAGIC = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"0" * 64


def _write(tmp_path, data: bytes, name: str = "thumb.bin") -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


@pytest.fixture(autouse=True)
def _vision_env(monkeypatch):
    """Point the analyzer at the local IA API, like production would."""
    monkeypatch.setattr(settings, "VISION_API_KEY", "test-project-key")
    monkeypatch.setattr(settings, "VISION_MODEL", "local-vision")
    monkeypatch.setattr(settings, "VISION_BASE_URL", "http://127.0.0.1:8140/v1")
    monkeypatch.setattr(settings, "VISION_MAX_IMAGE_MB", 8.0)
    monkeypatch.setattr(settings, "VISION_JSON_MODE", True)


def _mock_response(payload):
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class _CallRecorder:
    """Records calls to the LLM client and returns/raises a fixed outcome."""

    def __init__(self, response=None, exc=None):
        self.calls = []
        self.response = response
        self.exc = exc

    def __call__(self, candidates, messages, **kwargs):
        self.calls.append({"candidates": candidates, "messages": messages, "kwargs": kwargs})
        if self.exc is not None:
            raise self.exc
        return self.response


_VALID_PAYLOAD = {
    "description": "a dark cave lit by a torch",
    "objects": ["cave", "torch", "rocks"],
    "colors": ["black", "orange"],
    "composition": "centered subject, text on the left",
    "text_overlay": "SHOCKING FIND",
    "emotion": "fear and curiosity",
    "style": "documentary",
}


# ── MIME detection ───────────────────────────────────────────────


def test_detect_mime_supported_formats():
    assert viral_cloner._detect_image_mime(JPEG_MAGIC) == "image/jpeg"
    assert viral_cloner._detect_image_mime(PNG_MAGIC) == "image/png"
    assert viral_cloner._detect_image_mime(WEBP_MAGIC) == "image/webp"


def test_detect_mime_rejects_gif():
    assert viral_cloner._detect_image_mime(b"GIF89a" + b"0" * 32) is None


# ── Happy path ───────────────────────────────────────────────────


def test_happy_path_returns_dict_and_uses_local_endpoint(tmp_path, monkeypatch):
    rec = _CallRecorder(response=_mock_response(_VALID_PAYLOAD))
    monkeypatch.setattr(llm_client, "chat_completion_with_failover", rec)

    result = viral_cloner._analyze_thumbnail_vision(
        _write(tmp_path, PNG_MAGIC, "t.png"), SimpleNamespace()
    )

    assert result == _VALID_PAYLOAD
    assert len(rec.calls) == 1
    call = rec.calls[0]
    # Exactly one vision candidate, no DeepSeek fallback.
    assert len(call["candidates"]) == 1
    assert call["candidates"][0]["base_url"] == "http://127.0.0.1:8140/v1"
    assert call["candidates"][0]["model"] == "local-vision"
    # Real MIME is honoured (png, not a hardcoded jpeg).
    url = call["messages"][1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert call["kwargs"]["response_format"] == {"type": "json_object"}


def test_markdown_fenced_json_is_parsed(tmp_path, monkeypatch):
    fenced = "```json\n" + json.dumps(_VALID_PAYLOAD) + "\n```"
    rec = _CallRecorder(response=_mock_response(fenced))
    monkeypatch.setattr(llm_client, "chat_completion_with_failover", rec)

    result = viral_cloner._analyze_thumbnail_vision(
        _write(tmp_path, JPEG_MAGIC), SimpleNamespace()
    )
    assert result == _VALID_PAYLOAD


def test_json_mode_disabled_omits_response_format(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "VISION_JSON_MODE", False)
    rec = _CallRecorder(response=_mock_response(_VALID_PAYLOAD))
    monkeypatch.setattr(llm_client, "chat_completion_with_failover", rec)

    viral_cloner._analyze_thumbnail_vision(_write(tmp_path, JPEG_MAGIC), SimpleNamespace())
    assert "response_format" not in rec.calls[0]["kwargs"]


# ── Degradation to None ──────────────────────────────────────────


def test_invalid_json_returns_none(tmp_path, monkeypatch):
    rec = _CallRecorder(response=_mock_response("this is definitely not json {{{"))
    monkeypatch.setattr(llm_client, "chat_completion_with_failover", rec)

    assert viral_cloner._analyze_thumbnail_vision(
        _write(tmp_path, JPEG_MAGIC), SimpleNamespace()
    ) is None


def test_json_root_not_object_returns_none(tmp_path, monkeypatch):
    rec = _CallRecorder(response=_mock_response("[1, 2, 3]"))
    monkeypatch.setattr(llm_client, "chat_completion_with_failover", rec)

    assert viral_cloner._analyze_thumbnail_vision(
        _write(tmp_path, JPEG_MAGIC), SimpleNamespace()
    ) is None


def test_schema_invalid_returns_none(tmp_path, monkeypatch):
    # 'objects' must be a list; a string would crash the caller's slicing.
    rec = _CallRecorder(response=_mock_response({"objects": "not-a-list"}))
    monkeypatch.setattr(llm_client, "chat_completion_with_failover", rec)

    assert viral_cloner._analyze_thumbnail_vision(
        _write(tmp_path, JPEG_MAGIC), SimpleNamespace()
    ) is None


def test_unsupported_mime_returns_none_without_calling_client(tmp_path, monkeypatch):
    rec = _CallRecorder(response=_mock_response(_VALID_PAYLOAD))
    monkeypatch.setattr(llm_client, "chat_completion_with_failover", rec)

    result = viral_cloner._analyze_thumbnail_vision(
        _write(tmp_path, b"GIF89a" + b"0" * 128), SimpleNamespace()
    )

    assert result is None
    assert rec.calls == []


def test_image_too_large_returns_none_without_calling_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "VISION_MAX_IMAGE_MB", 0.0)
    rec = _CallRecorder(response=_mock_response(_VALID_PAYLOAD))
    monkeypatch.setattr(llm_client, "chat_completion_with_failover", rec)

    result = viral_cloner._analyze_thumbnail_vision(
        _write(tmp_path, PNG_MAGIC + b"0" * 256), SimpleNamespace()
    )

    assert result is None
    assert rec.calls == []


def test_provider_error_returns_none(tmp_path, monkeypatch):
    rec = _CallRecorder(exc=RuntimeError("upstream unavailable"))
    monkeypatch.setattr(llm_client, "chat_completion_with_failover", rec)

    assert viral_cloner._analyze_thumbnail_vision(
        _write(tmp_path, JPEG_MAGIC), SimpleNamespace()
    ) is None


def test_missing_api_key_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "VISION_API_KEY", "")
    rec = _CallRecorder(response=_mock_response(_VALID_PAYLOAD))
    monkeypatch.setattr(llm_client, "chat_completion_with_failover", rec)

    assert viral_cloner._analyze_thumbnail_vision(
        _write(tmp_path, JPEG_MAGIC), SimpleNamespace()
    ) is None
    assert rec.calls == []


def test_missing_file_returns_none(tmp_path):
    assert viral_cloner._analyze_thumbnail_vision(
        str(tmp_path / "nope.jpg"), SimpleNamespace()
    ) is None
