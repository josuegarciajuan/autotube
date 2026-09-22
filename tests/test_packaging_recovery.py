"""Tests for the validation_failed packaging recovery sweep."""

from api.services.packaging_policy import ValidationResult
import api.services.packaging_recovery as recovery


class FakeDB:
    def __init__(self, videos):
        self._videos = videos
        self.updated = []
        self.state = {}

    def get_videos(self, status=None, limit=None, **kwargs):
        return [dict(v) for v in self._videos if status is None or v.get("status") == status]

    def get_channel(self, channel_id):
        return {"id": channel_id, "slug": f"canal{channel_id}"}

    def get_system_state(self, key):
        return self.state.get(key)

    def set_system_state(self, key, value):
        self.state[key] = value

    def update_video(self, video_id, **kwargs):
        self.updated.append((video_id, kwargs))
        return True


def _video(tmp_path, vid, valid):
    path = tmp_path / f"v{vid}.mp4"
    path.write_bytes(b"x" * 16)
    return {
        "id": vid,
        "channel_id": 2,
        "status": "validation_failed",
        "video_path": str(path),
        "titulo_final": f"titulo {vid}",
        "thumbnail_path": str(path),
        "thumbnail_text": "",
        "_valid": valid,
    }


def _patch(monkeypatch, videos):
    import api.services.upload_scheduler as scheduler
    import config.config_bridge as bridge

    def fake_validate(video, cfg):
        row = next(v for v in videos if v["titulo_final"] == video.get("titulo_final"))
        return ValidationResult(row["_valid"], () if row["_valid"] else ("specificity",))

    monkeypatch.setattr(scheduler, "validate_upload_packaging", fake_validate)
    monkeypatch.setattr(bridge, "get_channel_config", lambda slug: object())


def test_recovers_only_valid_videos(tmp_path, monkeypatch):
    good = _video(tmp_path, 1, valid=True)
    bad = _video(tmp_path, 2, valid=False)
    videos = [good, bad]
    _patch(monkeypatch, videos)

    db = FakeDB(videos)
    result = recovery.recover_packaging_held_videos(db=db)

    assert result["recovered"] == 1
    assert result["scanned"] == 2
    assert db.updated == [
        (1, {
            "status": "awaiting_upload",
            "progress": 5,
            "progress_phase": "upload",
            "scheduled_upload_at": None,
            "error_message": "Requeued by packaging recovery",
        })
    ]
    actions = {d["video_id"]: d["action"] for d in result["details"]}
    assert actions[1] == "requeued"
    assert actions[2] == "still_invalid"


def test_dry_run_does_not_write(tmp_path, monkeypatch):
    good = _video(tmp_path, 1, valid=True)
    _patch(monkeypatch, [good])

    db = FakeDB([good])
    result = recovery.recover_packaging_held_videos(db=db, dry_run=True)

    assert result["recovered"] == 1
    assert db.updated == []
    assert result["details"][0]["action"] == "would_requeue"


def test_recovers_length_by_trimming_title(tmp_path, monkeypatch):
    import api.services.upload_scheduler as scheduler
    import config.config_bridge as bridge

    def fake_validate(video, cfg):
        t = video.get("titulo_final") or ""
        ok = 28 <= len(t) <= 65
        return ValidationResult(ok, () if ok else ("length",))

    monkeypatch.setattr(scheduler, "validate_upload_packaging", fake_validate)
    monkeypatch.setattr(bridge, "get_channel_config", lambda slug: object())

    video = _video(tmp_path, 1, valid=False)
    video["titulo_final"] = (
        "El dia que la muerte los paso por alto historias imposibles de "
        "sobrevivientes suprimido"
    )
    db = FakeDB([video])
    result = recovery.recover_packaging_held_videos(db=db)

    assert result["recovered"] == 1
    vid, kwargs = db.updated[0]
    assert len(kwargs["titulo_final"]) <= 65
    assert kwargs["status"] == "awaiting_upload"


def test_recovers_injected_suffix_by_sanitizing_title(tmp_path, monkeypatch):
    import api.services.upload_scheduler as scheduler
    import config.config_bridge as bridge

    def fake_validate(video, cfg):
        t = video.get("titulo_final") or ""
        ok = "|" not in t and "[" not in t and "]" not in t and 28 <= len(t) <= 65
        return ValidationResult(ok, () if ok else ("injected_suffix",))

    monkeypatch.setattr(scheduler, "validate_upload_packaging", fake_validate)

    class Cfg:
        TITLE_MIN_CHARS = 28
        TITLE_MAX_CHARS = 65

    monkeypatch.setattr(bridge, "get_channel_config", lambda slug: Cfg())

    video = _video(tmp_path, 1, valid=False)
    video["titulo_final"] = "Cuando la conciencia humana convoca al | El Caso Suprimido"
    db = FakeDB([video])
    result = recovery.recover_packaging_held_videos(db=db)

    assert result["recovered"] == 1
    vid, kwargs = db.updated[0]
    assert "|" not in kwargs["titulo_final"]
    assert kwargs["status"] == "awaiting_upload"


def test_does_not_guess_specificity(tmp_path, monkeypatch):
    import api.services.upload_scheduler as scheduler
    import config.config_bridge as bridge

    monkeypatch.setattr(
        scheduler, "validate_upload_packaging",
        lambda video, cfg: ValidationResult(False, ("specificity",)),
    )
    monkeypatch.setattr(bridge, "get_channel_config", lambda slug: object())

    db = FakeDB([_video(tmp_path, 1, valid=False)])
    result = recovery.recover_packaging_held_videos(db=db)

    assert result["recovered"] == 0
    assert db.updated == []
    assert result["details"][0]["action"] == "still_invalid"


def test_skips_missing_file(tmp_path, monkeypatch):
    video = _video(tmp_path, 1, valid=True)
    video["video_path"] = str(tmp_path / "gone.mp4")
    _patch(monkeypatch, [video])

    db = FakeDB([video])
    result = recovery.recover_packaging_held_videos(db=db)

    assert result["recovered"] == 0
    assert result["details"][0]["action"] == "missing_file"
    assert db.updated == []


def test_llm_retitles_evidence_only_failure_once(tmp_path, monkeypatch):
    """C1b: un fallo de 'specificity' con guion se reintenta con LLM UNA vez."""
    import api.services.upload_scheduler as scheduler
    import config.config_bridge as bridge
    import api.services.title_recovery as tr

    monkeypatch.setattr(
        scheduler, "validate_upload_packaging",
        lambda video, cfg: ValidationResult(False, ("specificity",)),
    )
    monkeypatch.setattr(bridge, "get_channel_config", lambda slug: object())

    calls = {"n": 0}

    def fake_retitle_one(db, row, cfg, **kw):
        calls["n"] += 1
        return {"action": "would_retitle" if kw.get("dry_run") else "retitled",
                "new_title": "El caso de Jung en 1961", "reasons": ["specificity"]}

    monkeypatch.setattr(tr, "retitle_one_video", fake_retitle_one)

    video = _video(tmp_path, 1, valid=False)
    video["script_id"] = 10
    db = FakeDB([video])

    result = recovery.recover_packaging_held_videos(db=db)
    assert result["recovered"] == 1
    assert result["details"][0]["action"] == "llm_retitled"
    assert calls["n"] == 1


def test_llm_retitle_guard_prevents_repeated_attempts(tmp_path, monkeypatch):
    """C1b: si el LLM no consigue un título válido, no se reintenta sin fin."""
    import api.services.upload_scheduler as scheduler
    import config.config_bridge as bridge
    import api.services.title_recovery as tr

    monkeypatch.setattr(
        scheduler, "validate_upload_packaging",
        lambda video, cfg: ValidationResult(False, ("specificity",)),
    )
    monkeypatch.setattr(bridge, "get_channel_config", lambda slug: object())

    calls = {"n": 0}

    def fake_retitle_one(db, row, cfg, **kw):
        calls["n"] += 1
        return {"action": "skipped", "new_title": None, "reasons": ["specificity"]}

    monkeypatch.setattr(tr, "retitle_one_video", fake_retitle_one)

    video = _video(tmp_path, 1, valid=False)
    video["script_id"] = 10
    db = FakeDB([video])

    result = recovery.recover_packaging_held_videos(db=db)
    assert result["recovered"] == 0
    assert result["details"][0]["action"] == "still_invalid"
    assert calls["n"] == 1

    # Segunda pasada: el guard por vídeo evita volver a gastar LLM.
    result2 = recovery.recover_packaging_held_videos(db=db)
    assert result2["recovered"] == 0
    assert calls["n"] == 1


def test_evidence_only_failure_without_script_stays_manual(tmp_path, monkeypatch):
    """C1b: sin guion no hay evidencia -> no se llama al LLM."""
    import api.services.upload_scheduler as scheduler
    import config.config_bridge as bridge
    import api.services.title_recovery as tr

    monkeypatch.setattr(
        scheduler, "validate_upload_packaging",
        lambda video, cfg: ValidationResult(False, ("specificity",)),
    )
    monkeypatch.setattr(bridge, "get_channel_config", lambda slug: object())

    called = {"n": 0}
    monkeypatch.setattr(
        tr, "retitle_one_video",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1)
        or {"action": "manual"},
    )

    db = FakeDB([_video(tmp_path, 1, valid=False)])
    result = recovery.recover_packaging_held_videos(db=db)

    assert result["recovered"] == 0
    assert result["details"][0]["action"] == "still_invalid"
    assert called["n"] == 0
