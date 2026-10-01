"""Tests de F8 — CTA con razón concreta y conversión atribuible."""
import contextlib
import sqlite3

from pipeline.shorts_cross_promote import (
    attributable_conversion, build_short_description,
)


class _DB:
    def __init__(self, path):
        self.path = str(path)

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def test_description_includes_concrete_subscribe_reason():
    desc = build_short_description(
        hook_text="Un caso increíble",
        hashtags=["misterio"],
        longform_url="https://www.youtube.com/watch?v=abcdefghijk",
        channel_url="https://www.youtube.com/@canal",
        subscribe_reason="cada caso documentado, sin relleno",
    )
    assert "youtu.be/abcdefghijk" in desc
    assert "Suscríbete para más: cada caso documentado" in desc


def test_description_without_reason_keeps_generic_cta():
    desc = build_short_description(channel_url="https://www.youtube.com/@x")
    assert "🔔 Suscríbete:" in desc


def test_attributable_conversion_unknown_without_stats(tmp_path):
    path = tmp_path / "shorts.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE short_stats(id INTEGER PRIMARY KEY, short_id INTEGER, "
                 "subscribers_gained INTEGER, views INTEGER)")
    conn.commit()
    conn.close()
    assert attributable_conversion(_DB(path), 99)["status"] == "unknown"


def test_attributable_conversion_known_with_stats(tmp_path):
    path = tmp_path / "shorts2.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE short_stats(id INTEGER PRIMARY KEY, short_id INTEGER, "
                 "subscribers_gained INTEGER, views INTEGER)")
    conn.execute("INSERT INTO short_stats(short_id, subscribers_gained, views) VALUES (7, 2, 500)")
    conn.commit()
    conn.close()
    res = attributable_conversion(_DB(path), 7)
    assert res["status"] == "known"
    assert res["subs_gained"] == 2
    assert res["conversion_pct"] == 0.4
