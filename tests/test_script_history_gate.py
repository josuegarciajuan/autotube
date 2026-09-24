"""Tests de ``pipeline/script_history_gate.py`` (T1.4: anti-plantilla)."""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.script_history_gate import (  # noqa: E402
    _shingles,
    check_script_novelty,
    get_novelty_settings,
    jaccard,
)

BASE = " ".join(f"palabra{i}" for i in range(80))


class FakeDB:
    def __init__(self, scripts: list[str], canal: str = "canal3"):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE scripts (id INTEGER PRIMARY KEY, canal TEXT, guion TEXT)"
        )
        for i, g in enumerate(scripts):
            self.conn.execute(
                "INSERT INTO scripts (id, canal, guion) VALUES (?, ?, ?)",
                (i + 1, canal, g),
            )
        self.conn.commit()

    @contextlib.contextmanager
    def _connect(self):
        yield self.conn


def test_shingles_and_jaccard():
    a = _shingles("el gato negro duerme plácidamente sobre la alfombra roja")
    b = _shingles("el gato negro duerme plácidamente sobre la alfombra roja")
    assert a == b and jaccard(a, b) == 1.0
    assert jaccard(a, _shingles("otra cosa totalmente distinta aquí")) == 0.0


def test_novel_script_passes():
    db = FakeDB([BASE])
    res = check_script_novelty(
        " ".join(f"distinto{i}" for i in range(60)), "canal3", db,
    )
    assert res.novel is True
    assert res.similarity < 0.55


def test_near_duplicate_blocked():
    db = FakeDB([BASE])
    near_dup = BASE.replace("palabra5", "otra5")
    res = check_script_novelty(near_dup, "canal3", db, threshold=0.55)
    assert res.novel is False
    assert res.similar_to_id == 1
    assert res.similarity > 0.55


def test_short_script_fails_open():
    db = FakeDB([BASE])
    res = check_script_novelty("hola mundo", "canal3", db)
    assert res.novel is True


def test_own_script_is_excluded_from_history():
    """Regresión: el guion candidato ya está en `scripts` (insert antes de la
    pre-validación) y sin exclude_id se compara consigo mismo → sim=1.0 y se
    bloquea TODA generación. Con exclude_id debe ser novedoso."""
    candidate = " ".join(f"propio{i}" for i in range(60))
    # El guion candidato es la fila #1 de la tabla (ya persistido).
    db = FakeDB([candidate])

    self_match = check_script_novelty(candidate, "canal3", db)
    assert self_match.novel is False
    assert self_match.similar_to_id == 1
    assert self_match.similarity == 1.0

    excluded = check_script_novelty(candidate, "canal3", db, exclude_id=1)
    assert excluded.novel is True
    assert excluded.similarity == 0.0


def test_later_script_is_not_prior_history():
    """Un guion insertado DESPUÉS del candidato (id mayor) no cuenta como
    historial: compararlo bloqueaba guiones válidos por sim=1.00 (sequía canal3)."""
    candidate = " ".join(f"tema{i}" for i in range(60))
    near_dup = candidate.replace("tema5", "otro5")
    # Fila #1 = candidato (ya persistido). Fila #2 = guion posterior casi-idéntico.
    db = FakeDB([candidate, near_dup])
    res = check_script_novelty(candidate, "canal3", db, exclude_id=1)
    assert res.novel is True
    assert res.similarity == 0.0


def test_missing_table_fails_open():
    db = FakeDB([])
    db.conn.execute("DROP TABLE scripts")
    res = check_script_novelty(BASE, "canal3", db)
    assert res.novel is True


class _Cfg:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_novelty_settings():
    assert get_novelty_settings(None) == (True, 0.55)
    enabled, thr = get_novelty_settings(
        _Cfg(SCRIPT_HISTORY_GATE_ENABLED=False, SCRIPT_HISTORY_GATE_THRESHOLD=0.7)
    )
    assert enabled is False and thr == 0.7
    _, thr_bad = get_novelty_settings(_Cfg(SCRIPT_HISTORY_GATE_THRESHOLD="x"))
    assert 0.0 < thr_bad <= 1.0
