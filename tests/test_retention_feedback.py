"""Tests del bucle de retención (Fase 3 del experimento de recuperación).

Cubren: beat mapping (scene_ranges y fallback), agregación por fase, directiva,
fail-open, kill-switch, staleness de caché, fetch de la curva y la inyección en
los prompts. Todo con DB/API falsas — sin red ni cuota.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api.services import retention_feedback as rf


# ─────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────

class _Conn:
    """Wrapper que delega en la conexión compartida y no la cierra."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args):
        return self._conn.execute(*args)

    def close(self):
        pass


class FakeDB:
    def __init__(self, conn):
        self._conn = conn
        self.state: dict[str, str] = {}

    def _connect(self):
        return _Conn(self._conn)

    def get_system_state(self, key):
        return self.state.get(key)

    def set_system_state(self, key, value):
        self.state[key] = value

    def get_script(self, sid):
        row = self._conn.execute(
            "SELECT * FROM scripts WHERE id = ?", (sid,)
        ).fetchone()
        return dict(row) if row else None

    def get_video_retention_curve(self, video_id, report_type="audience_retention"):
        rows = self._conn.execute(
            """SELECT dimension, metric_value FROM video_analytics_detailed
               WHERE video_id = ? AND report_type = ?
               ORDER BY CAST(dimension AS REAL) ASC""",
            (video_id, report_type),
        ).fetchall()
        return [
            {"elapsed": float(r["dimension"]), "watch_ratio": float(r["metric_value"])}
            for r in rows
        ]

    def get_channels(self, active_only=False):
        return [{"id": 1, "slug": "canalx"}]


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE scripts(id INTEGER PRIMARY KEY, bloques_json TEXT);
        CREATE TABLE videos(id INTEGER PRIMARY KEY, channel_id INTEGER,
            script_id INTEGER, duracion_seg REAL, published_at TEXT,
            uploaded_at TEXT, checkpoint_data TEXT);
        CREATE TABLE video_analytics_detailed(id INTEGER PRIMARY KEY,
            video_id INTEGER, yt_video_id TEXT, report_type TEXT,
            dimension TEXT, metric_value REAL, fetched_at TEXT);
        """
    )
    return conn


SIMPLE_TIMELINE = [
    {"phase_id": "gancho", "start": 0, "end": 60},
    {"phase_id": "desarrollo", "start": 60, "end": 400},
    {"phase_id": "cierre", "start": 400, "end": 600},
]


def _seed_videos(conn, n=4, wr_gancho=0.55, wr_desarrollo=0.20, wr_cierre=0.35,
                 with_ranges=True, with_blocks=False, duration=600):
    for v in range(1, n + 1):
        cp = json.dumps({"media": {"scene_ranges": SIMPLE_TIMELINE}}) if with_ranges else None
        sid = None
        if with_blocks:
            bloques = [
                {"phase_id": "gancho", "texto": "a " * 10},
                {"phase_id": "desarrollo", "texto": "b " * 40},
                {"phase_id": "cierre", "texto": "c " * 20},
            ]
            conn.execute("INSERT INTO scripts(id, bloques_json) VALUES (?, ?)",
                         (v, json.dumps(bloques)))
            sid = v
        conn.execute(
            "INSERT INTO videos VALUES (?,?,?,?,?,?,?)",
            (v, 1, sid, duration, f"2026-09-{v:02d}T10:00:00", None, cp),
        )
        rows = []
        for i in range(0, 100, 5):
            ratio = i / 100.0
            t = ratio * duration
            wr = wr_gancho if t < 60 else (wr_desarrollo if t < 400 else wr_cierre)
            rows.append((v, f"y{v}", "audience_retention", str(round(ratio, 5)), wr))
        conn.executemany(
            """INSERT INTO video_analytics_detailed
               (video_id, yt_video_id, report_type, dimension, metric_value)
               VALUES (?,?,?,?,?)""",
            rows,
        )
    conn.commit()


def _cfg(**overrides):
    base = dict(
        CANAL_NAME="canalx",
        RETENTION_FEEDBACK_ENABLED=True,
        RETENTION_CURVE_MAX_VIDEOS=20,
        RETENTION_LOOKBACK_DAYS=90,
        RETENTION_CURVE_MIN_VIDEOS=3,
        RETENTION_FOCUS_MAX_PHASES=2,
        RETENTION_TARGET_PCT=40.0,
        RETENTION_FEEDBACK_MAX_AGE_HOURS=48,
        SCRIPT_STRUCTURE=[
            {"id": "gancho", "step": "EL GANCHO"},
            {"id": "desarrollo", "step": "EL DESARROLLO"},
            {"id": "cierre", "step": "EL CIERRE"},
        ],
        CANAL_TONE="tono", CANAL_NARRATIVE_STYLE="documental de asombro",
        CANAL_STYLE_DESCRIPTION="", TARGET_AUDIENCE="LATAM",
        CANAL_OUTRO_TAGLINE="fin", SCRIPT_HOOK_RULE="gancho rule",
        VIRALITY_TRIGGERS=[], SCRIPT_EMOTIONAL_ARC={},
        VIDEO_AVERAGE_DURATION_MIN=12, TEST_MODE=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ─────────────────────────────────────────────────────────────────────
# Beat timeline
# ─────────────────────────────────────────────────────────────────────

def test_timeline_from_scene_ranges():
    conn = _make_conn()
    db = FakeDB(conn)
    video = {"duracion_seg": 600, "checkpoint_data": json.dumps(
        {"media": {"scene_ranges": SIMPLE_TIMELINE}})}
    tl = rf.build_beat_timeline(db, video)
    assert [r["phase_id"] for r in tl] == ["gancho", "desarrollo", "cierre"]


def test_timeline_fallback_from_blocks():
    conn = _make_conn()
    conn.execute("INSERT INTO scripts(id, bloques_json) VALUES (1, ?)", (
        json.dumps([
            {"phase_id": "gancho", "texto": "a " * 10},
            {"phase_id": "desarrollo", "texto": "b " * 40},
            {"phase_id": "cierre", "texto": "c " * 20},
        ]),
    ))
    conn.commit()
    db = FakeDB(conn)
    tl = rf.build_beat_timeline(db, {"duracion_seg": 700, "script_id": 1,
                                     "checkpoint_data": None})
    assert [r["phase_id"] for r in tl] == ["gancho", "desarrollo", "cierre"]
    assert tl[-1]["end"] == pytest.approx(700, rel=1e-6)


def test_timeline_none_without_duration():
    db = FakeDB(_make_conn())
    assert rf.build_beat_timeline(db, {"duracion_seg": 0}) is None


# ─────────────────────────────────────────────────────────────────────
# Señal
# ─────────────────────────────────────────────────────────────────────

def test_signal_picks_weakest_phase_and_caches():
    conn = _make_conn()
    _seed_videos(conn)
    db = FakeDB(conn)
    sig = rf.compute_channel_signal(db, {"id": 1, "slug": "canalx"}, cfg=_cfg())
    assert sig is not None
    assert sig["weak_phases"][0]["phase_id"] == "desarrollo"
    assert sig["weak_phases"][0]["label"] == "EL DESARROLLO"
    assert "EL DESARROLLO" in sig["directive"]
    assert sig["retention_pct"] == pytest.approx(28.0, abs=1.0)
    assert "retention_feedback_canalx" in db.state
    cached = rf.load_cached_signal(db, "canalx")
    assert cached["weak_phases"][0]["phase_id"] == "desarrollo"


def test_phase_directives_use_channel_anchors():
    """Fase 3: la directiva usa ancla, descripción y pacing de la fase débil."""
    conn = _make_conn()
    _seed_videos(conn)
    db = FakeDB(conn)
    cfg = _cfg(SCRIPT_STRUCTURE=[
        {"id": "gancho", "step": "EL GANCHO", "time_pct": "0-10%"},
        {"id": "desarrollo", "step": "EL DESARROLLO", "time_pct": "30-55%",
         "scene_pacing": {"image_target_sec": 6.0, "video_target_sec": 4.0},
         "description": "El suceso paso a paso.",
         "retention_anchor": "CLIFFHANGER al 50%: recapitulemos."},
        {"id": "cierre", "step": "EL CIERRE", "time_pct": "85-100%"},
    ])
    sig = rf.compute_channel_signal(db, {"id": 1, "slug": "canalx"}, cfg=cfg)
    assert sig is not None
    pds = {p["phase_id"]: p for p in sig["phase_directives"]}
    assert "desarrollo" in pds
    instr = pds["desarrollo"]["instruction"]
    assert "CLIFFHANGER al 50%" in instr
    assert "imagen ≤6.0s" in instr
    assert "El suceso paso a paso." in instr
    # El directivo global incluye el bloque estructurado por fase.
    assert "como arreglarlas" in sig["directive"]
    assert "EL DESARROLLO" in sig["directive"]


def test_signal_above_target_has_no_weak_directive():
    conn = _make_conn()
    _seed_videos(conn, wr_gancho=0.95, wr_desarrollo=0.85, wr_cierre=0.9)
    db = FakeDB(conn)
    sig = rf.compute_channel_signal(db, {"id": 1, "slug": "canalx"}, cfg=_cfg())
    assert sig["retention_pct"] > 40.0
    assert "OBJETIVO" not in sig["directive"]  # positive branch


def test_weak_phases_exclude_default_when_real_phases_exist():
    """La pseudo-fase 'default' (vídeos viejos sin phase_id) no se cita."""
    conn = _make_conn()
    timeline = [
        {"phase_id": "gancho", "start": 0, "end": 60},
        {"phase_id": "default", "start": 60, "end": 300},
        {"phase_id": "cierre", "start": 300, "end": 600},
    ]
    for v in range(1, 5):
        conn.execute(
            "INSERT INTO videos VALUES (?,?,?,?,?,?,?)",
            (v, 1, None, 600, f"2026-09-{v:02d}T10:00:00", None,
             json.dumps({"media": {"scene_ranges": timeline}})),
        )
        rows = []
        for i in range(0, 100, 5):
            ratio = i / 100.0
            t = ratio * 600
            wr = 0.50 if t < 60 else (0.05 if t < 300 else 0.30)
            rows.append((v, f"y{v}", "audience_retention", str(round(ratio, 5)), wr))
        conn.executemany(
            """INSERT INTO video_analytics_detailed
               (video_id, yt_video_id, report_type, dimension, metric_value)
               VALUES (?,?,?,?,?)""",
            rows,
        )
    conn.commit()
    db = FakeDB(conn)
    sig = rf.compute_channel_signal(db, {"id": 1, "slug": "canalx"}, cfg=_cfg())
    assert sig is not None
    weak_ids = [w["phase_id"] for w in sig["weak_phases"]]
    assert "default" not in weak_ids
    assert weak_ids and weak_ids[0] == "cierre"  # 0.30 vs gancho 0.50


def test_signal_insufficient_data_returns_none():
    conn = _make_conn()
    _seed_videos(conn, n=2)  # < RETENTION_CURVE_MIN_VIDEOS=3
    db = FakeDB(conn)
    assert rf.compute_channel_signal(db, {"id": 1, "slug": "canalx"}, cfg=_cfg()) is None


def test_signal_disabled_by_config():
    conn = _make_conn()
    _seed_videos(conn)
    db = FakeDB(conn)
    cfg = _cfg(RETENTION_FEEDBACK_ENABLED=False)
    assert rf.compute_channel_signal(db, {"id": 1, "slug": "canalx"}, cfg=cfg) is None


def test_signal_disabled_by_system_state():
    conn = _make_conn()
    _seed_videos(conn)
    db = FakeDB(conn)
    db.state[rf.DISABLED_KEY] = "true"
    assert rf.compute_channel_signal(db, {"id": 1, "slug": "canalx"}, cfg=_cfg()) is None


def test_directive_uses_fresh_cache_and_ignores_stale():
    conn = _make_conn()
    _seed_videos(conn)
    db = FakeDB(conn)
    # fresh cache
    fresh = {"generated_at": datetime.now(timezone.utc).isoformat(),
             "directive": "FRESCA"}
    db.state["retention_feedback_canalx"] = json.dumps(fresh)
    assert rf.get_directive_for_generation(
        db, "canalx", channel_id=1, cfg=_cfg()) == "FRESCA"

    # stale cache -> recompute (data is present, so a directive is produced)
    old = {"generated_at": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
           "directive": "VIEJA"}
    db.state["retention_feedback_canalx"] = json.dumps(old)
    out = rf.get_directive_for_generation(db, "canalx", channel_id=1, cfg=_cfg())
    assert out != "VIEJA"
    assert "RETENCION" in out.upper()


# ─────────────────────────────────────────────────────────────────────
# Fetch de la curva
# ─────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, rows):
        self._rows = rows

    def execute(self):
        return {"rows": self._rows}


class _FakeReports:
    def __init__(self, rows, fail_rrp):
        self.rows = rows
        self.fail_rrp = fail_rrp

    def query(self, **kwargs):
        if self.fail_rrp and "relativeRetentionPerformance" in kwargs.get("metrics", ""):
            raise RuntimeError("rrp unavailable")
        return _FakeResp(self.rows)


class _FakeAnalytics:
    def __init__(self, rows, fail_rrp=False):
        self.rows = rows
        self.fail_rrp = fail_rrp

    def reports(self):
        return _FakeReports(self.rows, self.fail_rrp)


def test_curve_fetch_sorts_and_parses_rrp():
    from pipeline.youtube_stats import YouTubeStatsFetcher
    f = YouTubeStatsFetcher.__new__(YouTubeStatsFetcher)
    f._analytics_service = _FakeAnalytics([
        [0.5, 0.6, 0.1], [0.0, 1.0, 0.2], [0.25, 0.8, -0.05],
    ])
    out = f.get_video_audience_retention("abc")
    assert [b["dimension"] for b in out] == ["0.0", "0.25", "0.5"]
    assert out[1]["rrp"] == -0.05


def test_curve_fetch_retries_without_rrp():
    from pipeline.youtube_stats import YouTubeStatsFetcher
    f = YouTubeStatsFetcher.__new__(YouTubeStatsFetcher)
    f._analytics_service = _FakeAnalytics([[0.0, 1.0], [0.5, 0.6]], fail_rrp=True)
    out = f.get_video_audience_retention("abc")
    assert len(out) == 2 and "rrp" not in out[0]


def test_curve_fetch_no_service():
    from pipeline.youtube_stats import YouTubeStatsFetcher
    f = YouTubeStatsFetcher.__new__(YouTubeStatsFetcher)
    f._analytics_service = None
    assert f.get_video_audience_retention("abc") == []


# ─────────────────────────────────────────────────────────────────────
# Inyección en prompts
# ─────────────────────────────────────────────────────────────────────

def test_prompts_include_directive_only_when_provided():
    from prompts.base_prompts import (
        build_system_prompt, build_outline_prompt, build_content_only_prompt,
    )
    cfg = _cfg()
    directive = "FASE DE PRUEBA: refuerza el desarrollo."
    cases = [
        (build_system_prompt, {}),
        (build_outline_prompt, {}),
        (build_content_only_prompt, {"source_text": "fuente"}),
    ]
    for fn, kwargs in cases:
        without = fn(cfg, **kwargs)
        with_d = fn(cfg, retention_directive=directive, **kwargs)
        assert "DIRECTIVA DE RETENCION" not in without
        assert "DIRECTIVA DE RETENCION" in with_d
        assert directive in with_d


def test_playbook_is_injected_from_config():
    from prompts.base_prompts import build_system_prompt
    cfg = _cfg()
    prompt = build_system_prompt(cfg)
    assert "REGLA DE GANCHO DEL CANAL" in prompt
    assert "ESTRUCTURA NARRATIVA OBLIGATORIA" in prompt
    assert "EL DESARROLLO" in prompt


def test_script_generator_passes_directive():
    """ScriptGenerator.retention_directive llega a los prompts de outline."""
    from pipeline.script_generator import ScriptGenerator
    import inspect
    src = inspect.getsource(ScriptGenerator._generate_outline)
    assert "retention_directive=self.retention_directive" in src
