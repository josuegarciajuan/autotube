"""Tests for reliable Insight AI application (Phase 1).

Covers canonical key normalization, type validation, rollback on ineffective
writes, per-channel serialization and supersede bookkeeping.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import api.routers.insights as insights_router
import config.config_bridge as bridge


class FakeDB:
    def __init__(self, slug="canal2", config=None, insight=None):
        self.slug = slug
        self.channels = {
            3: {
                "id": 3,
                "slug": slug,
                "name": "Test",
                "config_json": json.dumps(config or {}),
            }
        }
        self.insight = insight or {"id": 43, "channel_id": 3, "insights_json": "{}"}
        self.updates: list[dict] = []
        self.applied_marks: list[int] = []

    def get_channel(self, channel_id):
        ch = self.channels.get(channel_id)
        return dict(ch) if ch else None

    def get_insight(self, insight_id):
        return dict(self.insight)

    def update_channel(self, channel_id, name=None, slug=None, config=None, **kw):
        if config is not None:
            self.channels[channel_id]["config_json"] = json.dumps(config, ensure_ascii=False)
            self.updates.append(dict(config))
        return True

    def mark_insight_applied(self, insight_id, applied_by="system"):
        self.applied_marks.append(insight_id)

    def update_insight_recommendation(self, insight_id, rec_id, updates):
        data = json.loads(self.insight["insights_json"])
        for rec in data.get("recommendations", []):
            if rec.get("id") == rec_id:
                rec.update(updates)
                break
        self.insight["insights_json"] = json.dumps(data, ensure_ascii=False)
        return True


def _insight(recs):
    return {"id": 43, "channel_id": 3, "insights_json": json.dumps({"recommendations": recs})}


def _rec(rec_id, changes, applied=False, **kw):
    base = {"id": rec_id, "title": rec_id, "config_changes": changes, "applied": applied}
    base.update(kw)
    return base


@pytest.fixture()
def patch_db(monkeypatch):
    def _install(db):
        monkeypatch.setattr(insights_router, "get_db", lambda: db)

        def fake_effective(slug, force_reload=False):
            cfg = json.loads(db.channels[3]["config_json"])
            return SimpleNamespace(**cfg)

        monkeypatch.setattr(bridge, "get_channel_config", fake_effective)
        monkeypatch.setattr(insights_router, "_APPLY_LOCKS", {})
        return db

    return _install


def test_lowercase_key_is_normalized_before_write(patch_db):
    db = patch_db(FakeDB(config={}, insight=_insight([
        _rec("r1", {"title_formulas": ["A {name}", "B {name}"]}),
    ])))
    out = insights_router.apply_insight(3, 43, "r1", -1)
    saved = json.loads(db.channels[3]["config_json"])
    assert "TITLE_FORMULAS" in saved
    assert "title_formulas" not in saved
    assert out["applied_status"] == "effective"
    assert out["effective_changes"]["TITLE_FORMULAS"] == saved["TITLE_FORMULAS"]
    assert db.applied_marks == [43]


def test_type_mismatch_is_rejected_without_write(patch_db):
    db = patch_db(FakeDB(
        config={"TITLE_FORMULAS": ["old"]},
        insight=_insight([_rec("r1", {"TITLE_FORMULAS": "not-a-list"})]),
    ))
    with pytest.raises(HTTPException) as exc:
        insights_router.apply_insight(3, 43, "r1", -1)
    assert exc.value.status_code == 422
    assert db.updates == []
    assert db.applied_marks == []


def test_ineffective_write_rolls_back(patch_db, monkeypatch):
    db = FakeDB(
        config={"TITLE_FORMULAS": ["old"]},
        insight=_insight([_rec("r1", {"TITLE_FORMULAS": ["new"]})]),
    )
    patch_db(db)

    # Simulate a pipeline that ignores the written key.
    def broken_effective(slug, force_reload=False):
        return SimpleNamespace(TITLE_FORMULAS=["old"])

    monkeypatch.setattr(bridge, "get_channel_config", broken_effective)

    with pytest.raises(HTTPException) as exc:
        insights_router.apply_insight(3, 43, "r1", -1)
    assert exc.value.status_code == 500
    assert json.loads(db.channels[3]["config_json"])["TITLE_FORMULAS"] == ["old"]
    assert db.applied_marks == []


def test_second_apply_supersedes_first(patch_db):
    db = patch_db(FakeDB(
        config={"TITLE_FORMULAS": ["old"]},
        insight=_insight([
            _rec("r1", {"TITLE_FORMULAS": ["one"]}, applied=True),
            _rec("r2", {"TITLE_FORMULAS": ["two"]}),
        ]),
    ))
    out = insights_router.apply_insight(3, 43, "r2", -1)
    data = json.loads(db.insight["insights_json"])
    by_id = {r["id"]: r for r in data["recommendations"]}
    assert by_id["r1"]["applied_status"] == "superseded"
    assert by_id["r1"]["superseded_by"] == "r2"
    assert by_id["r2"]["applied_status"] == "effective"
    assert out["applied_changes"]["TITLE_FORMULAS"] == ["two"]
    assert any("Superseded" in w for w in out["warnings"])


def test_legacy_duration_range_is_translated_to_objective(patch_db):
    db = patch_db(FakeDB(
        config={"VIDEO_AVERAGE_DURATION_MIN": 10, "VIDEO_DURATION_DISCREPANCY_MIN": 2},
        insight=_insight([_rec("r1", {
            "PROD_VIDEO_DURATION_MIN": 15,
            "PROD_VIDEO_DURATION_MAX": 19,
        })]),
    ))
    out = insights_router.apply_insight(3, 43, "r1", -1)
    saved = json.loads(db.channels[3]["config_json"])
    assert saved["VIDEO_AVERAGE_DURATION_MIN"] == 17.0
    assert saved["VIDEO_DURATION_DISCREPANCY_MIN"] == 2.0
    assert out["applied_status"] == "effective"


def test_unknown_key_is_preserved_with_warning(patch_db):
    db = patch_db(FakeDB(config={}, insight=_insight([_rec("r1", {"FOO_BAR": 1})])))
    out = insights_router.apply_insight(3, 43, "r1", -1)
    assert json.loads(db.channels[3]["config_json"])["FOO_BAR"] == 1
    assert out["applied_status"] == "partial"
    assert any("Unknown config keys" in w for w in out["warnings"])


def test_case_variant_is_removed_when_canonical_is_written(patch_db):
    db = patch_db(FakeDB(
        config={"title_formulas": ["shadow-old"]},
        insight=_insight([_rec("r1", {"TITLE_FORMULAS": ["canonical-new"]})]),
    ))
    insights_router.apply_insight(3, 43, "r1", -1)
    saved = json.loads(db.channels[3]["config_json"])
    assert saved["TITLE_FORMULAS"] == ["canonical-new"]
    assert "title_formulas" not in saved


def test_contradictory_duration_patch_is_rejected(patch_db):
    db = patch_db(FakeDB(
        config={"VIDEO_AVERAGE_DURATION_MIN": 16, "VIDEO_DURATION_DISCREPANCY_MIN": 4},
        insight=_insight([_rec("r1", {
            "VIDEO_AVERAGE_DURATION_MIN": 16,
            "PROD_VIDEO_DURATION_MIN": 15,
            "PROD_VIDEO_DURATION_MAX": 18,
        })]),
    ))
    with pytest.raises(HTTPException) as exc:
        insights_router.apply_insight(3, 43, "r1", -1)
    assert exc.value.status_code == 422
    assert "Incoherent duration patch" in exc.value.detail
    assert db.updates == []


def test_inert_key_is_rejected_instead_of_false_success(patch_db):
    db = patch_db(FakeDB(
        config={"CONTENT_PILLARS": [{"name": "El Caso", "ratio": 55}]},
        insight=_insight([_rec("r1", {"CONTENT_PILLARS": [{"name": "El Caso", "ratio": 65}]})]),
    ))
    with pytest.raises(HTTPException) as exc:
        insights_router.apply_insight(3, 43, "r1", -1)
    assert exc.value.status_code == 422
    assert "does not consume yet" in exc.value.detail
    assert db.updates == []
    assert db.applied_marks == []
