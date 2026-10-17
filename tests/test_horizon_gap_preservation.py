"""Fase 6: el warmup no puede romper los gaps de publicación del horizonte."""

import json
from datetime import datetime

import pytest

from database.db import init_db
from database.db_extended import ExtendedDatabase, migrate_v2


def _db(tmp_path):
    path = tmp_path / "horizon-gap.db"
    init_db(str(path))
    migrate_v2(str(path))
    db = ExtendedDatabase(str(path))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db._connect() as conn:
        conn.execute("DELETE FROM channels")
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json, active) "
            "VALUES (1, 'One', 'one', ?, 1)",
            (json.dumps({"videos_per_day": 2, "planning_enabled": True}),),
        )
        for rank, hour in ((1, 9), (2, 15), (3, 21)):
            conn.execute(
                """INSERT INTO optimal_publish_slots
                   (channel_id, content_type, slot_rank, target_hour, target_minute,
                    timezone, score, confidence, calculated_at)
                   VALUES (1, 'long', ?, ?, 0, 'Europe/Madrid', 0.5, 0.5, ?)""",
                (rank, hour, now),
            )
        conn.commit()
    return db


def test_horizon_moves_upload_not_publication(tmp_path, monkeypatch):
    import api.services.planning_service as ps

    db = _db(tmp_path)
    cfg = db.get_channel_planning_config(1)
    cfg.update({
        "publish_mode": "scheduled",
        "publish_warmup_min": 120,
        "publish_timezone": "Europe/Madrid",
        "longform_generation_per_day": 2,
        "slug": "one",
        "name": "One",
    })

    # Subida 30 min antes de publicar → incumple warmup 120+60. El fix debe
    # mover la SUBIDA 3h antes, no empujar la publicación (rompería el gap).
    monkeypatch.setattr(
        ps, "_pick_upload_minute",
        lambda *a, **k: (int(k.get("public_hour", 9)), 0, 9, 11),
    )
    monkeypatch.setattr(ps, "_publish_cap_per_day", lambda *a, **k: 2)
    monkeypatch.setattr(ps, "_publish_gap_minutes", lambda *a, **k: 6 * 60)

    slots = ps.compute_horizon_slots([cfg], horizon_days=2, db=db)
    assert slots

    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Madrid")
    public = sorted(
        datetime.fromisoformat(s["target_public_at"]).astimezone(tz)
        for s in slots
    )
    assert len(public) >= 2
    for prev, cur in zip(public, public[1:]):
        assert (cur - prev).total_seconds() / 60 >= 6 * 60

    # La publicación conserva una franja rankeada (9:00/15:00), no se desplaza.
    hours = {dt.hour for dt in public}
    assert hours <= {9, 15}
