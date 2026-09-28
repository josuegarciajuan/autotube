"""Tests for the W7 offline packaging audit (in-memory DB, no network)."""

import os
import sqlite3
import sys
from types import SimpleNamespace

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import scripts.packaging_audit as audit  # noqa: E402


def _cfg():
    return SimpleNamespace(
        NICHE_GUARD_ENABLED=False,
        TITLE_NICHE_FIT_MIN=0.3,
        NICHE_ANCHORS=[],
    )


def _con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE videos (
            id INTEGER, channel_id INTEGER, titulo_final TEXT,
            thumbnail_text TEXT, thumbnail_overlay_source TEXT,
            thumbnail_variant_strategy TEXT, thumbnail_layout TEXT, status TEXT)"""
    )
    con.execute(
        "INSERT INTO videos VALUES (1, 7, 'El síndrome que nadie diagnosticó en 1990',"
        " 'FRANKLIN | 129', 'llm', 'subject_hero', 'topic_hero', 'published')"
    )
    con.execute(
        "INSERT INTO videos VALUES (2, 7, 'La historia que nadie contó sobre',"
        " '', '', '', '', 'awaiting_upload')"
    )
    return con


def test_audit_channel_counts(monkeypatch):
    monkeypatch.setattr(audit, "get_channel_config", lambda slug: _cfg())
    res = audit.audit_channel(_con(), {"id": 7, "slug": "canal5", "name": "Anomalias"}, 10)
    assert res["sampled"] == 2
    assert res["incomplete_titles"] == 1
    assert res["overlay_persisted"] == 1
    assert res["overlay_source_set"] == 1
    assert 0.0 <= res["overlay_persisted_ratio"] <= 1.0


def test_audit_handles_empty_channel(monkeypatch):
    monkeypatch.setattr(audit, "get_channel_config", lambda slug: _cfg())
    res = audit.audit_channel(_con(), {"id": 99, "slug": "canal9", "name": "Vacio"}, 10)
    assert res["sampled"] == 0
    assert res["incomplete_ratio"] == 0.0
