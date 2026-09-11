"""Tests for deterministic + hybrid title recovery."""

from api.services.packaging_policy import ValidationResult
import api.services.title_recovery as tr


CFG = type("Cfg", (), {"TITLE_MIN_CHARS": 28, "TITLE_MAX_CHARS": 65})


class FakeDB:
    def __init__(self, videos, scripts=None):
        self._videos = videos
        self._scripts = scripts or {}
        self.updated = []

    def get_videos(self, status=None, limit=None, **kwargs):
        return [dict(v) for v in self._videos if status is None or v.get("status") == status]

    def get_channel(self, channel_id):
        return {"id": channel_id, "slug": f"canal{channel_id}"}

    def get_script(self, script_id):
        return self._scripts.get(script_id)

    def update_video(self, video_id, **kwargs):
        self.updated.append((video_id, kwargs))
        return True


def _patch_config(monkeypatch):
    import config.config_bridge as bridge
    monkeypatch.setattr(bridge, "get_channel_config", lambda slug: CFG)


def test_repair_title_trims_long_title_within_bounds():
    long = ("El día que la muerte los pasó por alto: historias imposibles "
            "de sobrevivientes — ¿Suprimido?")
    out = tr.repair_title(long, CFG)
    assert out is not None
    assert 28 <= len(out) <= 65
    assert "Suprimido" not in out


def test_repair_title_strips_clickbait_suffix():
    out = tr.repair_title("Los casos que el FBI no puede resolver: desaparecidos sin explicación (Caso Rescatado)", CFG)
    assert out is not None
    assert "Caso Rescatado" not in out
    assert len(out) <= 65


def test_repair_title_rejects_too_short():
    assert tr.repair_title("Corto", CFG) is None


def test_retitle_dry_run_repairs_length_without_writing(monkeypatch):
    long = "¿Por qué no hay rastro de las 6 civilizaciones que nos precedieron?. ENIGMÁTICO."
    video = {"id": 1, "channel_id": 4, "status": "validation_failed",
             "titulo_final": long, "script_id": None,
             "thumbnail_path": "/x.jpg", "thumbnail_text": ""}
    db = FakeDB([video])
    _patch_config(monkeypatch)
    monkeypatch.setattr(
        tr, "_validate",
        lambda cfg, t, row: ValidationResult(28 <= len(t) <= 65, () if 28 <= len(t) <= 65 else ("length",)),
    )

    res = tr.retitle_validation_failed(db, dry_run=True, use_llm=False)
    assert res["retitled"] == 1
    assert db.updated == []
    assert res["details"][0]["action"] == "would_retitle"


def test_retitle_uses_llm_candidate(monkeypatch):
    video = {"id": 2, "channel_id": 3, "status": "validation_failed",
             "titulo_final": "Un sueño la llevó al lado oscuro: la transformación",
             "script_id": 10, "thumbnail_path": "/x.jpg", "thumbnail_text": ""}
    db = FakeDB([video], scripts={10: {"guion": "guion largo", "keywords_json": "[]"}})
    _patch_config(monkeypatch)
    # The original title fails; only the LLM candidate is accepted.
    monkeypatch.setattr(
        tr, "_validate",
        lambda cfg, t, row: ValidationResult(
            t == "El caso de Jung en 1961",
            () if t == "El caso de Jung en 1961" else ("specificity",),
        ),
    )
    monkeypatch.setattr(
        tr, "_candidate_from_llm",
        lambda cfg, guion, kw, row, attempts: ("El caso de Jung en 1961", ["El caso de Jung en 1961"]),
    )

    res = tr.retitle_validation_failed(db, dry_run=False, use_llm=True)
    assert res["retitled"] == 1
    assert db.updated and db.updated[0][1]["titulo_final"] == "El caso de Jung en 1961"
    assert db.updated[0][1]["status"] == "awaiting_upload"


def test_retitle_marks_no_script_specificity_as_manual(monkeypatch):
    video = {"id": 3, "channel_id": 3, "status": "validation_failed",
             "titulo_final": "Un sueño la llevó al lado oscuro: la transformación",
             "script_id": None, "thumbnail_path": "/x.jpg", "thumbnail_text": ""}
    db = FakeDB([video])
    _patch_config(monkeypatch)
    monkeypatch.setattr(tr, "_validate", lambda cfg, t, row: ValidationResult(False, ("specificity",)))

    res = tr.retitle_validation_failed(db, dry_run=False, use_llm=True)
    assert res["manual"] == 1
    assert res["retitled"] == 0
    assert db.updated == []
