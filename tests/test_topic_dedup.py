"""Tests del registro anti-repetición de temáticas (consumed_topics, v58).

Cubre:
  1. Normalización y similitud por solapamiento de tokens (pipeline/topic_dedup).
  2. Persistencia: mark/is/filter sobre la tabla consumed_topics.
  3. Backfill histórico desde videos (long-form) y shorts nativos.

Run:  python3 -m pytest tests/test_topic_dedup.py -v
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from pipeline import topic_dedup as td
from database.db import init_db
from database.db_extended import ExtendedDatabase


# ═══════════════════════════════════════════════════════════════
# Módulo puro
# ═══════════════════════════════════════════════════════════════

class TestNormalization:
    def test_normalize_strips_accents_and_punctuation(self):
        assert td.normalize_topic("El Triángulo de las Bermudas!") == \
            "el triangulo de las bermudas"

    def test_normalize_empty(self):
        assert td.normalize_topic("") == ""
        assert td.normalize_topic(None) == ""

    def test_tokens_remove_stopwords_and_short(self):
        tokens = td.topic_tokens("El misterio de la desaparición en el Triángulo")
        assert "misterio" in tokens
        assert "desaparicion" in tokens
        assert "triangulo" in tokens
        # stopwords / palabras cortas fuera
        assert "el" not in tokens
        assert "de" not in tokens
        assert "la" not in tokens

    def test_tokens_empty_for_stopwords_only(self):
        assert td.topic_tokens("el de la y o a") == set()


class TestSimilarity:
    def test_identical_themes_duplicate(self):
        a = td.topic_tokens("desaparición en el Triángulo de las Bermudas")
        b = td.topic_tokens("el misterio del Triángulo de las Bermudas desaparición")
        dup, label = td.is_topic_duplicate(b, [("Bermudas", a)], threshold=0.5)
        assert dup is True
        assert label == "Bermudas"

    def test_unrelated_themes_not_duplicate(self):
        a = td.topic_tokens("civilización maya templo perdido")
        b = td.topic_tokens("anomalía médica enfermedad rara")
        dup, label = td.is_topic_duplicate(b, [("maya", a)], threshold=0.5)
        assert dup is False
        assert label is None

    def test_single_generic_shared_token_does_not_block(self):
        # Comparten solo "misterio" → min_shared=2 impide el bloqueo.
        a = td.topic_tokens("misterio desaparición bermudas")
        b = td.topic_tokens("misterio asesino victoriano")
        dup, _ = td.is_topic_duplicate(b, [("x", a)], threshold=0.3, min_shared=2)
        assert dup is False

    def test_similarity_metric(self):
        assert td.topic_similarity({"a", "b"}, {"a", "b"}) == 1.0
        assert td.topic_similarity({"a"}, {"a", "b", "c"}) == pytest.approx(1 / 3)
        assert td.topic_similarity(set(), {"a"}) == 0.0

    def test_tokens_json_roundtrip(self):
        toks = {"misterio", "bermudas"}
        raw = td.tokens_to_json(toks)
        assert td.tokens_from_json(raw) == toks
        assert td.tokens_from_json(None) == set()
        assert td.tokens_from_json("not json") == set()

    def test_settings_defaults_and_override(self):
        class _Cfg:
            TOPIC_DEDUP_ENABLED = True
            TOPIC_DEDUP_THRESHOLD = 0.7
            TOPIC_DEDUP_MIN_TOKENS = 3

        enabled, thr, mn = td.get_dedup_settings(_Cfg)
        assert enabled is True and thr == 0.7 and mn == 3
        # Sin config → defaults saneados
        e2, t2, m2 = td.get_dedup_settings(None)
        assert e2 is True and t2 == td.DEFAULT_THRESHOLD and m2 == td.DEFAULT_MIN_TOKENS


class TestFindBestDuplicate:
    def test_picks_highest_similarity(self):
        consumed = [
            ("poco", {"templ", "perdido"}),
            ("mucho", {"bermudas", "desaparicion", "triangulo"}),
        ]
        cand = {"bermudas", "desaparicion", "triangulo"}
        label, sim = td.find_best_duplicate(cand, consumed, threshold=0.5, min_shared=2)
        assert label == "mucho"
        assert sim == pytest.approx(1.0)


# ═══════════════════════════════════════════════════════════════
# Integración DB
# ═══════════════════════════════════════════════════════════════

_SCHEMA_V58 = Path(__file__).resolve().parent.parent / "database" / "schema_v58.sql"

_DDL = """
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL DEFAULT '{}',
    active BOOLEAN NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canal TEXT,
    titulo_final TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS shorts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    type TEXT NOT NULL DEFAULT 'native',
    title TEXT,
    topic TEXT,
    status TEXT NOT NULL DEFAULT 'published',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@pytest.fixture
def dbx(tmp_path):
    path = tmp_path / "topics.db"
    init_db(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(_DDL)
        conn.executescript(_SCHEMA_V58.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json, active) VALUES (1, 'Canal 2', 'canal2', '{}', 1)"
        )
        conn.commit()
    return ExtendedDatabase(str(path))


class TestConsumedTopicsDB:
    def test_mark_and_is_consumed_roundtrip(self, dbx):
        assert dbx.mark_topic_consumed(1, "desaparición en el Triángulo de las Bermudas", "longform", 10) is True
        dup, label = dbx.is_topic_consumed(1, "el misterio del Triángulo de las Bermudas")
        assert dup is True
        assert "Bermudas" in label

    def test_unrelated_not_consumed(self, dbx):
        dbx.mark_topic_consumed(1, "civilización maya templo perdido", "longform", 1)
        dup, _ = dbx.is_topic_consumed(1, "anomalía médica enfermedad rara")
        assert dup is False

    def test_mark_is_idempotent(self, dbx):
        assert dbx.mark_topic_consumed(1, "tema único de prueba", "longform", 1) is True
        assert dbx.mark_topic_consumed(1, "tema único de prueba", "longform", 2) is False
        assert len(dbx.get_consumed_topics(1)) == 1

    def test_empty_label_not_registered(self, dbx):
        assert dbx.mark_topic_consumed(1, "   ", "longform", 1) is False
        assert dbx.mark_topic_consumed(1, "el de la y", "longform", 1) is False

    def test_cross_format_shared_registry(self, dbx):
        # Un tema consumido por un short bloquea un long-form equivalente.
        dbx.mark_topic_consumed(1, "naufragio del Endurance en la Antártida", "native_short", 5)
        dup, label = dbx.is_topic_consumed(1, "el naufragio del Endurance en la Antártida")
        assert dup is True

    def test_filter_consumed_topics(self, dbx):
        dbx.mark_topic_consumed(1, "desaparición en el Triángulo de las Bermudas", "longform", 1)
        result = dbx.filter_consumed_topics(1, [
            "misterio del Triángulo de las Bermudas",
            "anomalía médica enfermedad rara",
        ])
        assert result["kept"] == ["anomalía médica enfermedad rara"]
        assert len(result["rejected"]) == 1

    def test_kill_switch_disables_dedup(self, dbx):
        dbx.mark_topic_consumed(1, "desaparición en el Triángulo de las Bermudas", "longform", 1)
        dbx.set_system_state("topic_dedup_disabled", "true")
        dup, _ = dbx.is_topic_consumed(1, "el misterio del Triángulo de las Bermudas")
        assert dup is False

    def test_per_channel_isolation(self, dbx):
        dbx.mark_topic_consumed(1, "desaparición en el Triángulo de las Bermudas", "longform", 1)
        # Otro canal no debe ver el tema
        dup, _ = dbx.is_topic_consumed(2, "el misterio del Triángulo de las Bermudas")
        assert dup is False

    def test_backfill_and_idempotency(self, dbx):
        with dbx._connect() as conn:
            conn.execute(
                "INSERT INTO videos (canal, video_path, titulo_final) VALUES "
                "('canal2', '/tmp/v.mp4', 'La ciudad perdida de Atlantis')"
            )
            conn.execute(
                "INSERT INTO shorts (channel_id, type, title, topic) VALUES (1, 'native', 'T', 'El naufragio del Endurance')"
            )
            # Un clip NO debe sembrar el registro (deriva del long-form)
            conn.execute(
                "INSERT INTO shorts (channel_id, type, title, topic) VALUES (1, 'clip', 'C', 'Tema de clip')"
            )
            conn.commit()

        inserted = dbx.backfill_consumed_topics()
        assert inserted >= 2

        dup, _ = dbx.is_topic_consumed(1, "Atlantis, la ciudad perdida")
        assert dup is True
        dup2, _ = dbx.is_topic_consumed(1, "el naufragio del Endurance")
        assert dup2 is True

        # Segunda pasada: flag por canal evita re-barrido
        assert dbx.backfill_consumed_topics() == 0
