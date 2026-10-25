"""Regression tests for Pollo.ai account selection and status handling.

Covers two production failures:
  * a 401 (expired session) was written as ``credits_exhausted``;
  * when every account was flagged, the selector returned only the first one
    (``fallback=None``), so the second account was never attempted.
"""

import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import pipeline.ai_image_generator as aig


WORKER_PATH = Path(__file__).resolve().parents[1] / "tools" / "pollo_image_worker.py"


def _load_worker():
    spec = importlib.util.spec_from_file_location("pollo_image_worker_under_test", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def worker():
    return _load_worker()


# ── worker: error classification ──────────────────────────────

def test_classify_session_error_is_not_credits(worker):
    exc = worker.PolloSessionError("Sesion caducada (401).")
    assert worker.classify_account_error(exc) == worker.STATUS_SESSION


def test_classify_credits_error(worker):
    exc = worker.PolloCreditsError("Cuenta sin creditos (403 user video limit)")
    assert worker.classify_account_error(exc) == worker.STATUS_CREDITS


def test_classify_does_not_match_bare_401_substring(worker):
    # A prompt or unrelated message containing "401" must not be treated as auth.
    assert worker.classify_account_error(RuntimeError("scene 401 rendered fine")) is None


# ── worker: status file semantics ────────────────────────────

def test_session_expired_does_not_mark_credits_exhausted(worker, tmp_path):
    status_file = tmp_path / "pollo_accounts_status.json"
    status_file.write_text(json.dumps({"tracatrack": {"credits_exhausted": True}}))

    worker._write_account_status(status_file, "tracatrack", worker.STATUS_SESSION, "401")

    data = json.loads(status_file.read_text())
    entry = data["tracatrack"]
    assert entry["session_expired"] is True
    assert "credits_exhausted" not in entry
    assert "session_expired_at" in entry
    assert "exhausted_at" not in entry


def test_credits_status_is_backward_compatible(worker, tmp_path):
    status_file = tmp_path / "pollo_accounts_status.json"
    status_file.write_text(json.dumps({"burrianacasa2026": {"session_expired": True}}))

    worker._write_account_status(
        status_file, "burrianacasa2026", worker.STATUS_CREDITS, "limit"
    )

    entry = json.loads(status_file.read_text())["burrianacasa2026"]
    assert entry["credits_exhausted"] is True
    assert "exhausted_at" in entry          # CRM reads this key
    assert "session_expired" not in entry


def test_status_write_preserves_other_accounts_and_malformed_file(worker, tmp_path):
    status_file = tmp_path / "pollo_accounts_status.json"
    status_file.write_text(json.dumps({"other": {"credits_exhausted": True}}))
    worker._write_account_status(status_file, "acc1", worker.STATUS_SESSION)

    data = json.loads(status_file.read_text())
    assert data["other"] == {"credits_exhausted": True}

    status_file.write_text("{not json")
    worker._write_account_status(status_file, "acc1", worker.STATUS_SESSION)
    assert status_file.read_text() == "{not json"     # never clobbered


def test_postprocess_keeps_png_container(worker, tmp_path):
    from PIL import Image

    path = tmp_path / "image.png"
    Image.new("RGB", (16, 16), (10, 20, 30)).save(path, "PNG")

    worker.apply_realism_postprocess(path)

    assert Image.open(path).format == "PNG"


# ── selector: always a distinct fallback ─────────────────────

def _patch_accounts(monkeypatch, accounts, status=None, legacy_cookie=""):
    monkeypatch.setattr(aig, "_read_pollo_accounts", lambda: accounts)
    monkeypatch.setattr(aig, "_read_pollo_status", lambda: status or {})
    monkeypatch.setattr(aig, "_read_cookie", lambda: legacy_cookie)


def _acc(label, cookie=None):
    return {"label": label, "cookie": cookie or f"cookie-{label}", "expires": "2099-01-01"}


def test_selector_returns_distinct_primary_and_fallback(monkeypatch):
    _patch_accounts(monkeypatch, [_acc("a"), _acc("b")])
    primary, plabel, fallback, flabel = aig._select_pollo_account_pair()
    assert plabel in {"a", "b"}
    assert flabel in {"a", "b"}
    assert plabel != flabel
    assert primary and fallback


def test_selector_still_uses_both_when_all_accounts_are_flagged(monkeypatch):
    accounts = [_acc("a"), _acc("b")]
    status = {
        "a": {"credits_exhausted": True},
        "b": {"session_expired": True},
    }
    _patch_accounts(monkeypatch, accounts, status=status)

    _, plabel, _, flabel = aig._select_pollo_account_pair()
    assert plabel != flabel
    assert {plabel, flabel} == {"a", "b"}


def test_selector_prefers_healthy_account_as_primary(monkeypatch):
    accounts = [_acc("expired"), _acc("healthy")]
    status = {"expired": {"session_expired": True}}
    _patch_accounts(monkeypatch, accounts, status=status)

    for _ in range(10):
        _, plabel, _, flabel = aig._select_pollo_account_pair()
        assert plabel == "healthy"
        assert flabel == "expired"


def test_selector_single_account_has_no_fallback(monkeypatch):
    _patch_accounts(monkeypatch, [_acc("only")])
    _, plabel, fallback, _ = aig._select_pollo_account_pair()
    assert plabel == "only"
    assert fallback is None


def test_selector_legacy_single_cookie(monkeypatch):
    _patch_accounts(monkeypatch, [], legacy_cookie="legacy-cookie")
    primary, label, fallback, _ = aig._select_pollo_account_pair()
    assert (primary, label, fallback) == ("legacy-cookie", "legacy", None)


# ── AIImageGenerator: cookie never in argv + 0600 file ───────

def test_generate_passes_cookies_via_private_file(monkeypatch, tmp_path):
    _patch_accounts(monkeypatch, [_acc("a", "SECRET-A"), _acc("b", "SECRET-B")])
    gen = aig.AIImageGenerator()
    out = tmp_path / "thumb.jpg"
    out.write_bytes(b"x")

    captured = {}

    def fake_run(cmd, json_out):
        captured["cmd"] = cmd
        cookie_file = cmd[cmd.index("--cookie-file") + 1]
        captured["cookie_file"] = cookie_file
        captured["mode"] = stat.S_IMODE(os.stat(cookie_file).st_mode)
        captured["payload"] = json.loads(Path(cookie_file).read_text())
        return {"ok": True, "image_path": str(out), "cookie_used": "a"}

    monkeypatch.setattr(gen, "_run_worker", fake_run)
    gen.generate("prompt", out)

    assert "--cookie" not in captured["cmd"]
    assert "SECRET-A" not in " ".join(captured["cmd"])
    assert "SECRET-B" not in " ".join(captured["cmd"])
    assert captured["mode"] == 0o600
    assert captured["payload"]["cookie"].startswith("SECRET-")
    assert not Path(captured["cookie_file"]).exists()   # cleaned up


# ── SceneImageGenerator: cache actually hits ─────────────────

def test_scene_cache_hits_jpg_without_regenerating(monkeypatch, tmp_path):
    gen = aig.SceneImageGenerator(session_cookie="x", cache_dir=str(tmp_path), model="m1")

    def _fake_generate(prompt, cache_key):
        path = tmp_path / f"{cache_key}.jpg"
        path.write_bytes(b"img")
        return path

    monkeypatch.setattr(gen, "_call_pollo_api", _fake_generate)
    first = gen.generate_scene_image("dark forest")
    assert first.exists()

    def _boom(*_args, **_kwargs):
        raise AssertionError("cache miss: generation should not run twice")

    monkeypatch.setattr(gen, "_call_pollo_api", _boom)
    second = gen.generate_scene_image("dark forest")
    assert second == first
