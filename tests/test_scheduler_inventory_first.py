"""Inventario primero + cupo de generación con DB (oct 2026).

Cubre el lote de fixes de planificación/publicación:
  1. ``repack_channel_publish_times(prioritize_awaiting=True)`` coloca los
     vídeos más antiguos en los huecos más tempranos y no conserva targets
     posteriores al hueco calculado.
  2. ``adjusted_upload_at`` se calcula con el target FINAL (target >= subida +
     warmup), evitando el aborto del canal (#2724).
  3. ``apply_publish_repack`` excluye ítems inválidos por-ítem y aplica los
     válidos en vez de abortar el canal entero.
  4. ``compute_horizon_slots`` numera ``slot_position`` por (canal, día).
  5. ``sync_midday`` compara el cupo contra GENERACIÓN de hoy (no contra
     publicados), de modo que un vídeo de ayer publicado hoy no cancela.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from database.db import init_db
from database.db_extended import ExtendedDatabase, migrate_v2


# ── Helpers ──────────────────────────────────────────────────────

def _db(tmp_path, name="inventory.db"):
    path = tmp_path / name
    init_db(str(path))
    migrate_v2(str(path))
    db = ExtendedDatabase(str(path))
    with db._connect() as conn:
        conn.execute("DELETE FROM channels")
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json, active) "
            "VALUES (1, 'One', 'one', ?, 1)",
            (json.dumps({
                "PUBLISH_TIMEZONE": "Europe/Madrid",
                "PUBLISH_WARMUP_MIN": 120,
            }),),
        )
        conn.commit()
    return db


def _add_video(
    db, *,
    status="awaiting_upload",
    video_path="/v.mp4",
    created_at=None,
    uploaded_at=None,
    scheduled_upload_at=None,
    target_public_at=None,
    yt_video_id=None,
) -> int:
    with db._connect() as conn:
        cur = conn.execute(
            """INSERT INTO videos
               (channel_id, canal, video_path, status, publish_mode,
                uploaded_at, scheduled_upload_at, target_public_at,
                yt_video_id, created_at)
               VALUES (1, 'one', ?, ?, 'scheduled', ?, ?, ?, ?, ?)""",
            (video_path, status, uploaded_at, scheduled_upload_at,
             target_public_at, yt_video_id, created_at),
        )
        conn.commit()
        return int(cur.lastrowid)


def _utc_naive(dt: datetime) -> str:
    """SQLite UTC-naive text (el contrato de persistencia del runtime)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@pytest.fixture
def repack_policy(monkeypatch):
    """Política determinista: 1 long/día, gap 24h, warmup máx 48h, pico 09:00."""
    import api.services.channel_policy as cp
    values = {
        "longform_publish_cap": 1,
        "same_channel_publish_gap_h": 24,
        "max_warmup_hours": 48,
    }

    def _policy_value(cid, key, db=None, default=None, day=None):
        return values.get(key, default)

    monkeypatch.setattr(cp, "policy_value", _policy_value)
    monkeypatch.setattr(
        cp, "resolve_channel_policy_values",
        lambda cid, db=None, config=None: {
            **values, "publish_target_hour": None,
            "public_longform_per_day": 1,
        },
    )
    import pipeline.publish_scheduler as ps
    monkeypatch.setattr(ps, "_channel_peak_hours", lambda *a, **k: [9])
    return values


# ── 1 + 2. Orden inventario-primero y subida consistente ────────

def test_prioritize_awaiting_puts_oldest_first_and_earlier(tmp_path, repack_policy):
    from pipeline.publish_scheduler import repack_channel_publish_times
    from api.time_utils import parse_utc

    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    oldest_created = _utc_naive(now - timedelta(days=3))
    newer_created = _utc_naive(now - timedelta(days=1))
    # Targets futuros lejanos pero dentro del horizonte de seguridad (120h).
    oldest_old_target = _utc_naive(now + timedelta(days=4))
    newer_old_target = _utc_naive(now + timedelta(days=5))

    oldest_id = _add_video(
        db, created_at=oldest_created,
        scheduled_upload_at=_utc_naive(now + timedelta(hours=1)),
        target_public_at=oldest_old_target, video_path="/old.mp4",
    )
    newer_id = _add_video(
        db, created_at=newer_created,
        scheduled_upload_at=_utc_naive(now + timedelta(hours=2)),
        target_public_at=newer_old_target, video_path="/new.mp4",
    )

    plan = repack_channel_publish_times(
        db, 1, "one", gap_hours=24, prioritize_awaiting=True,
    )

    assert [p["video_id"] for p in plan] == [oldest_id, newer_id], (
        "el inventario más antiguo debe recibir el primer hueco"
    )
    first = parse_utc(plan[0]["new_target"])
    second = parse_utc(plan[1]["new_target"])
    assert first < second
    for item in plan:
        new_dt = parse_utc(item["new_target"])
        old_dt = parse_utc(item["old_target"])
        # Con prioritize_awaiting NUNCA se conserva un target POSTERIOR al hueco
        # calculado: ambos se adelantan estrictamente.
        assert new_dt < old_dt, item
        assert new_dt < now + timedelta(days=5), item

    # Sin priorización los targets lejanos se preservarían (control negativo).
    plan_legacy = repack_channel_publish_times(
        db, 1, "one", gap_hours=24, prioritize_awaiting=False,
    )
    assert [parse_utc(p["new_target"]) for p in plan_legacy] == [
        parse_utc(oldest_old_target), parse_utc(newer_old_target),
    ]


def test_adjusted_upload_at_consistent_with_final_target(tmp_path, repack_policy):
    from pipeline.publish_scheduler import repack_channel_publish_times
    from api.time_utils import parse_utc

    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    # Configuración que dispara la preservación de targets tempranos y luego
    # un target distinto: el upload debe recalcularse con el slot FINAL.
    for i in range(2):
        _add_video(
            db, created_at=_utc_naive(now - timedelta(days=2 - i)),
            scheduled_upload_at=_utc_naive(now + timedelta(hours=1 + i)),
            target_public_at=_utc_naive(now + timedelta(days=3 + i)),
            video_path=f"/v{i}.mp4",
        )

    plan = repack_channel_publish_times(
        db, 1, "one", gap_hours=24, prioritize_awaiting=True,
    )
    assert plan
    warmup = 120  # PUBLISH_WARMUP_MIN de la config del canal
    for item in plan:
        if item["status"] not in ("awaiting_upload", "ready"):
            continue
        upload_raw = item["adjusted_upload_at"] or item["scheduled_upload_at"]
        upload_dt = parse_utc(upload_raw)
        target_dt = parse_utc(item["new_target"])
        assert upload_dt is not None
        assert target_dt >= upload_dt + timedelta(minutes=warmup), item


# ── 3. Un ítem inválido no aborta el canal ──────────────────────

def test_apply_repack_skips_invalid_item_and_applies_valid(tmp_path, monkeypatch):
    import api.services.publish_repack as pr
    import api.services.channel_policy as cp

    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    invalid_id = _add_video(
        db, video_path="/invalid.mp4", target_public_at=_utc_naive(now + timedelta(days=2)),
    )
    valid_id = _add_video(
        db, video_path="/valid.mp4", target_public_at=_utc_naive(now + timedelta(days=11)),
    )

    valid_new = _iso(now + timedelta(days=10))
    valid_old = _utc_naive(now + timedelta(days=11))
    invalid_new = _iso(now + timedelta(hours=1))
    invalid_old = _utc_naive(now + timedelta(days=2))

    plan = [
        {
            "video_id": invalid_id, "status": "awaiting_upload",
            "yt_video_id": "", "old_target": invalid_old,
            "new_target": invalid_new, "requires_yt_update": False,
            "adjusted_upload_at": _utc_naive(now + timedelta(hours=2)),
            "uploaded_at": None,
            "scheduled_upload_at": _utc_naive(now + timedelta(hours=2)),
            "is_marathon": False, "preserved": False,
        },
        {
            "video_id": valid_id, "status": "awaiting_upload",
            "yt_video_id": "", "old_target": valid_old,
            "new_target": valid_new, "requires_yt_update": False,
            "adjusted_upload_at": _utc_naive(now + timedelta(days=10) - timedelta(hours=3)),
            "uploaded_at": None,
            "scheduled_upload_at": _utc_naive(now + timedelta(days=10) - timedelta(hours=3)),
            "is_marathon": False, "preserved": False,
        },
    ]
    monkeypatch.setattr(
        "pipeline.publish_scheduler.repack_channel_publish_times",
        lambda *a, **k: [dict(item) for item in plan],
    )
    monkeypatch.setattr(
        pr, "_policy",
        lambda db, cid: {
            "same_channel_publish_gap_h": 24,
            "longform_publish_cap": 1,
            "max_warmup_hours": 48,
        },
    )
    monkeypatch.setattr(
        cp, "policy_value",
        lambda cid, key, db=None, default=None, day=None: default,
    )
    # Ningún ítem requiere YouTube (awaiting), pero si se llamara sería un bug.
    monkeypatch.setattr(
        "pipeline.youtube_uploader.YouTubeUploader",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debe tocar YouTube")),
    )

    result = pr.apply_publish_repack(
        db, 1, "one", dry_run=False, quota_gate=False,
    )

    assert result["skipped_invalid"] == [invalid_id]
    assert result["rescheduled"] == 1, result
    assert result["plan_valid"] is True
    with db._connect() as conn:
        valid_target = conn.execute(
            "SELECT target_public_at FROM videos WHERE id=?", (valid_id,)
        ).fetchone()[0]
        invalid_target = conn.execute(
            "SELECT target_public_at FROM videos WHERE id=?", (invalid_id,)
        ).fetchone()[0]
    assert valid_target == valid_new
    assert invalid_target == invalid_old, "el ítem inválido no debe tocarse"


# ── 4. slot_position por (canal, día) ───────────────────────────

def test_compute_horizon_slots_numbers_per_channel_day(monkeypatch):
    import api.services.planning_service as ps

    monkeypatch.setattr(ps, "_resolve_generation_per_day", lambda *a, **k: 2)
    monkeypatch.setattr(ps, "_publish_cap_per_day", lambda *a, **k: 2)
    monkeypatch.setattr(ps, "_global_upload_spacing_minutes", lambda db: 45)

    channels = [
        {
            "channel_id": 1, "slug": "one", "name": "One",
            "planning_enabled": True, "publish_mode": "immediate",
            "longform_generation_per_day": 2, "viral_per_day": 0,
            "viral_day_boost_weight": 0.0, "videos_day_boost_weight": 0.0,
            "publish_timezone": "Europe/Madrid",
        },
        {
            "channel_id": 2, "slug": "two", "name": "Two",
            "planning_enabled": True, "publish_mode": "immediate",
            "longform_generation_per_day": 2, "viral_per_day": 0,
            "viral_day_boost_weight": 0.0, "videos_day_boost_weight": 0.0,
            "publish_timezone": "Europe/Madrid",
        },
    ]

    slots = ps.compute_horizon_slots(channels, horizon_days=2, db=None)
    assert slots, "el horizonte debe producir slots"

    groups: dict = {}
    for slot in slots:
        key = (slot["channel_id"], slot["date_key"])
        groups.setdefault(key, []).append(slot["slot_position"])

    # 2 canales × 2 días, 2 slots/día → cada grupo numera 1..2, no global.
    for key, positions in groups.items():
        assert sorted(positions) == list(range(1, len(positions) + 1)), (key, positions)
    assert any(max(p) > 1 for p in groups.values())
    assert all(max(p) <= 2 for p in groups.values())


# ── 5. sync_midday: cupo de generación, no de publicados ────────

def test_sync_midday_does_not_cancel_for_video_published_today(tmp_path, monkeypatch):
    import api.services.planning_service as ps

    db = _db(tmp_path)
    today = datetime.now().date().isoformat()
    now = datetime.now(timezone.utc)

    # Config: 2 long-forms de GENERACIÓN/día, sin boost.
    with db._connect() as conn:
        conn.execute(
            "UPDATE channels SET config_json=? WHERE id=1",
            (json.dumps({
                "planning_enabled": True,
                "longform_generation_per_day": 2,
                "videos_day_boost_weight": 0.0,
                "PUBLISH_TIMEZONE": "Europe/Madrid",
            }),),
        )
        # 2 slots pendientes hoy.
        for pos in (1, 2):
            conn.execute(
                """INSERT INTO planned_slots
                   (channel_id, date_key, scheduled_at, target_upload_at,
                    status, slot_position)
                   VALUES (1, ?, ?, ?, 'pending', ?)""",
                (today, _utc_naive(now + timedelta(hours=pos)),
                 _utc_naive(now + timedelta(hours=pos + 2)), pos),
            )
        # Un vídeo PUBLICADO hoy pero CREADO ayer (no cuenta como generado hoy).
        conn.execute(
            """INSERT INTO videos
               (channel_id, canal, video_path, status, publish_mode,
                uploaded_at, yt_video_id, created_at)
               VALUES (1, 'one', '/published.mp4', 'published', 'scheduled',
                       ?, 'yt-pub', ?)""",
            (_utc_naive(now), _utc_naive(now - timedelta(days=1))),
        )
        conn.commit()

    # El colchón de awaiting no debe alterar el objetivo (2 awaiting ya existen).
    for i in range(2):
        _add_video(
            db, created_at=_utc_naive(now - timedelta(days=1)),
            scheduled_upload_at=_utc_naive(now + timedelta(hours=5 + i)),
            video_path=f"/awaiting{i}.mp4",
        )

    # Aislar de la cuota real / política de pacing del entorno.
    monkeypatch.setattr(ps, "compute_daily_upload_allocation", lambda db, day: {})
    monkeypatch.setattr(ps, "_channel_paused_for_planning", lambda db, cid: False)

    result = ps.sync_midday(db=db)

    assert result["cancelled"] == 0, result
    remaining = [
        s for s in db.get_planned_slots(date_key=today, channel_id=1)
        if s["status"] == "pending"
    ]
    assert len(remaining) == 2
