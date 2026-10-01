"""Tests de F3 — trazabilidad de la selección editorial."""
from database.db_extended import ExtendedDatabase, migrate_v2


def _db(tmp_path):
    path = tmp_path / "f3.db"
    db = ExtendedDatabase(path)
    migrate_v2(str(path))
    with db._connect() as conn:
        conn.execute("INSERT INTO channels(id, slug, name, active) "
                     "VALUES (3,'canal2','Sincronías',1)")
        conn.commit()
    return db


def test_provenance_roundtrip(tmp_path):
    db = _db(tmp_path)
    ok = db.record_selection_provenance(
        channel_id=3, entity_type="script", entity_id=42, route="viral",
        topic="La Atlántida", demand_query="atlantida documental",
        source="autocomplete", demand_score=0.8, niche_fit=0.9,
        reason="ruta=viral; demanda=sí; nicho=0.90",
    )
    assert ok is True
    rows = db.get_recent_selection_provenance(3)
    assert len(rows) == 1
    assert rows[0]["route"] == "viral"
    assert rows[0]["demand_query"] == "atlantida documental"
    assert rows[0]["niche_fit"] == 0.9


def test_mark_topic_demand_used(tmp_path):
    db = _db(tmp_path)
    db.save_topic_demand_candidates(3, [
        {"query": "atlantida documental", "source": "autocomplete", "demand_score": 0.9},
        {"query": "civilizaciones perdidas", "source": "autocomplete", "demand_score": 0.7},
    ])
    assert db.mark_topic_demand_used(3, "atlantida documental") == 1
    cands = db.get_recent_topic_demand_candidates(3, limit=10)
    used = {c["query"]: c["status"] for c in cands}
    assert used["atlantida documental"] == "used"
    assert used["civilizaciones perdidas"] != "used"
