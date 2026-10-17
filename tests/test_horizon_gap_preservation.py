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


def test_horizon_avoids_existing_video_targets(tmp_path, monkeypatch):
    """Un vídeo pendiente ya comprometido ocupa su franja/cap del día."""
    import api.services.planning_service as ps
    from zoneinfo import ZoneInfo
    from datetime import timedelta

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

    tz = ZoneInfo("Europe/Madrid")
    target_day = datetime.now(tz).date() + timedelta(days=1)
    target_local = datetime(
        target_day.year, target_day.month, target_day.day, 9, 0, tzinfo=tz,
    )
    target_utc = target_local.astimezone(ZoneInfo("UTC")).isoformat()
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO videos
               (channel_id, canal, video_path, status, publish_mode, target_public_at)
               VALUES (1, 'one', '/tmp/pending.mp4', 'awaiting_upload', 'scheduled', ?)""",
            (target_utc,),
        )
        conn.commit()

    monkeypatch.setattr(
        ps, "_pick_upload_minute",
        lambda *a, **k: (max(0, int(k.get("public_hour", 9)) - 3), 0, 6, 9),
    )
    monkeypatch.setattr(ps, "_publish_cap_per_day", lambda *a, **k: 2)
    monkeypatch.setattr(ps, "_publish_gap_minutes", lambda *a, **k: 6 * 60)

    slots = ps.compute_horizon_slots([cfg], horizon_days=3, db=db)
    assert slots

    public = sorted(
        datetime.fromisoformat(s["target_public_at"]).astimezone(tz)
        for s in slots
    )
    # Ningún slot planificado puede caer a menos de 6h del vídeo pendiente.
    for dt in public:
        assert abs((dt - target_local).total_seconds()) >= 6 * 3600, dt
    # Y el día del vídeo no puede superar el cap de 2.
    day_count = sum(1 for dt in public if dt.date() == target_day)
    assert day_count <= 1, day_count  # vídeo + 1 slot como máximo
