"""Tests para los fixes de native shorts (ago 2026).

Cubre:
- `_titles_too_similar` con stopwords (menos falsos positivos sin debilitar
  la detección de duplicados reales).
- `get_queued_generated_shorts` (cola de shorts generados visible).
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from api.services.shorts_scheduler import (
    _titles_too_similar,
    TITLE_SIMILARITY_STOPWORDS,
)
from database.db_extended import ExtendedDatabase


# ── _titles_too_similar ──────────────────────────────────────────

class TestTitlesTooSimilar:
    def test_identical_title_still_rejected(self):
        """El MISMO título repetido se sigue rechazando (raw overlap 1.0)."""
        t = "EL HOMBRE QUE SE CONVIRTIÓ EN PIEDRA"
        assert _titles_too_similar(t, t) is True

    def test_real_content_duplicate_rejected(self):
        """Duplicado real de contenido: meaningful overlap alto → True."""
        a = "LA CASA ABANDONADA DEL BOSQUE"
        b = "LA CASA ABANDONADA DEL BOSQUE ENCANTADA"
        assert _titles_too_similar(a, b) is True

    def test_formula_pattern_not_rejected(self):
        """Mismo patrón de gancho con tema distinto ya NO choca (solo stopwords)."""
        a = "EL MISTERIO DE LA CASA DE LA COLINA"
        b = "EL MISTERIO DE LA MONTAÑA DE FUEGO"
        assert _titles_too_similar(a, b) is False

    def test_completely_different_not_rejected(self):
        assert _titles_too_similar(
            "EL TREN QUE LLEGA A MEDIANOCHE", "LA MONEDA QUE NUNCA MIENTE",
        ) is False

    def test_stopwords_are_filtered(self):
        assert "el" in TITLE_SIMILARITY_STOPWORDS
        assert "que" in TITLE_SIMILARITY_STOPWORDS

    def test_empty_title_safe(self):
        assert _titles_too_similar("", "algo") is False
        assert _titles_too_similar(None, None) is False


# ── get_queued_generated_shorts ──────────────────────────────────

@pytest.fixture
def queued_db():
    """Temp DB with channels + shorts tables (mínimo para la consulta)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY, name TEXT, slug TEXT,
            google_account TEXT, yt_studio_url TEXT
        );
        CREATE TABLE shorts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            type TEXT NOT NULL DEFAULT 'native',
            title TEXT, hook_title TEXT, hook_text TEXT, topic TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            file_path TEXT, youtube_id TEXT, youtube_url TEXT,
            published_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            error_message TEXT
        );
    """)
    conn.execute("INSERT INTO channels (id, name, slug) VALUES (1, 'Canal Uno', 'canal1')")
    conn.execute("INSERT INTO channels (id, name, slug) VALUES (2, 'Canal Dos', 'canal2')")
    conn.commit()
    conn.close()

    db = ExtendedDatabase(db_path)
    yield db, db_path
    try:
        Path(db_path).unlink(missing_ok=True)
    except Exception:
        pass


def test_get_queued_generated_shorts_only_generated(queued_db):
    db, db_path = queued_db
    conn = sqlite3.connect(db_path)
    # 2 generados en cola + 1 publicado + 1 generado SIN youtube_id pero SIN archivo
    conn.executemany(
        """INSERT INTO shorts (channel_id, type, title, status, file_path, youtube_id)
           VALUES (?, 'native', ?, ?, ?, ?)""",
        [
            (1, "Generado A", "generated", "/tmp/a.mp4", None),
            (1, "Generado B", "generated", "/tmp/b.mp4", None),
            (1, "Publicado", "published", "/tmp/c.mp4", "yt_abc"),
            (2, "Generado C", "generated", None, None),
        ],
    )
    conn.commit()
    conn.close()

    queued = db.get_queued_generated_shorts()
    # Solo los 'generated' con archivo en disco y sin youtube_id (A y B)
    titles = {q["title"] for q in queued}
    assert titles == {"Generado A", "Generado B"}
    for q in queued:
        assert q["channel_slug"] in ("canal1", "canal2")
        assert "file_path" in q
