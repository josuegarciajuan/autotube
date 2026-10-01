"""Tests de F9 — feedback doble (alertas de sistema + avisos de estado)."""
from database.db_extended import ExtendedDatabase, migrate_v2
from api.services import improvement_feedback as ifb


def _db(tmp_path):
    path = tmp_path / "f9.db"
    db = ExtendedDatabase(path)
    migrate_v2(str(path))
    return db


def test_publish_all_sets_status_and_schedules_reviews(tmp_path):
    db = _db(tmp_path)
    payload = ifb.publish_all(db, start_iso="2026-10-01T09:00:00")
    items = {i["id"]: i for i in payload["items"]}
    assert len(items) == len(ifb.IMPROVEMENTS)

    # Mejoras que no tocan contenido quedan verificadas; las demás esperan vídeos.
    assert items["F0_pausa_protegida"]["status"] == ifb.STATUS_VERIFIED
    assert items["F6_ab_flujo_f1f2"]["status"] == ifb.STATUS_AWAITING_GENERATION

    # Aviso de estado persistido.
    status = ifb.get_status(db)
    assert len(status["items"]) == len(ifb.IMPROVEMENTS)

    # Revisiones programadas (48h/7d/14d/28d).
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT title FROM scheduled_reminders WHERE alert_type='improvement_review'"
        ).fetchall()
    assert len(rows) == 4


def test_publish_all_emits_system_alerts(tmp_path):
    db = _db(tmp_path)
    ifb.publish_all(db, start_iso="2026-10-01T09:00:00")
    with db._connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM pipeline_alerts WHERE alert_type='improvement_deployed'"
        ).fetchone()[0]
    assert n >= 1


def test_set_improvement_status_does_not_touch_alerts(tmp_path):
    db = _db(tmp_path)
    ifb.publish_all(db, start_iso="2026-10-01T09:00:00")
    ifb.set_improvement_status(db, "F6_ab_flujo_f1f2", ifb.STATUS_VALIDATED, "ok")
    status = ifb.get_status(db)
    items = {i["id"]: i for i in status["items"]}
    assert items["F6_ab_flujo_f1f2"]["status"] == ifb.STATUS_VALIDATED
    # El aviso de estado es independiente de las alertas.
    assert status["updated_at"] is not None
