import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from database.db import init_db
from database.db_extended import ExtendedDatabase, migrate_v2
from api.services import channel_policy


def _db(tmp_path):
    path = tmp_path / "delivery.db"
    init_db(str(path))
    migrate_v2(str(path))
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "INSERT INTO channels (id, name, slug, config_json, active) VALUES (1, 'One', 'one', ?, 1)",
            (json.dumps({"videos_per_day": 1, "planning_enabled": True}),),
        )
        conn.commit()
    return ExtendedDatabase(str(path))


def test_channel_state_change_preserves_manual_delivery_override(tmp_path):
    """La Configuración de Programación es la fuente de verdad: cambiar el
    estado de entrega (incl. transición automática strike→normal) NO borra el
    override del panel."""
    db = _db(tmp_path)
    channel_policy.set_channel_delivery_state("recovery", 1, db=db)
    channel_policy.set_channel_delivery_override(1, {"longs_per_day": 0}, db=db)

    assert channel_policy.resolve_channel_policy_values(1, db=db)["longform_publish_cap"] == 0

    channel_policy.set_channel_delivery_state("normal", 1, db=db)

    assert db.get_system_state("channel_delivery_override_1") in (None, "")
    assert channel_policy.get_channel_delivery_state(1, db=db) == "normal"
    # El override del panel sobrevive a la transición de estado.
    assert channel_policy.resolve_channel_policy_values(1, db=db)["longform_publish_cap"] == 0


def test_clearing_manual_override_returns_to_profile_cap(tmp_path):
    db = _db(tmp_path)
    channel_policy.set_channel_delivery_state("normal", 1, db=db)
    channel_policy.set_channel_delivery_override(1, {"longs_per_day": 0}, db=db)
    assert channel_policy.resolve_channel_policy_values(1, db=db)["longform_publish_cap"] == 0

    channel_policy.clear_publish_override(1, db=db)
    assert channel_policy.resolve_channel_policy_values(1, db=db)["longform_publish_cap"] == 2


def test_planning_config_exposes_public_generation_and_upload_capacity(tmp_path):
    db = _db(tmp_path)
    db.update_channel_planning_config(
        1, public_videos_per_day=1, longform_generation_per_day=4,
        upload_capacity_per_day=2,
    )

    config = db.get_channel_planning_config(1)
    assert config["public_videos_per_day"] == 1
    assert config["longform_generation_per_day"] == 4
    assert config["upload_capacity_per_day"] == 2


def test_delivery_profiles_are_database_values_and_public_target_is_not_aliased(tmp_path):
    db = _db(tmp_path)
    with db._connect() as conn:
        conn.execute("DROP INDEX uq_active_planned_public_target")
        rows = conn.execute("SELECT state, public_videos_per_day FROM delivery_profiles ORDER BY state").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("normal", 2), ("recovery", 1), ("strike", 1)]

    db.update_channel_planning_config(1, public_videos_per_day=2, longform_generation_per_day=5, upload_capacity_per_day=4)
    with db._connect() as conn:
        cfg = json.loads(conn.execute("SELECT config_json FROM channels WHERE id=1").fetchone()[0])
    assert cfg["public_videos_per_day"] == 2
    assert cfg["videos_per_day"] == 1


def test_delivery_change_creates_idempotent_replan_requests(tmp_path):
    db = _db(tmp_path)
    assert db.set_channel_delivery_state_atomic(1, "recovery") is True
    assert db.set_channel_delivery_state_atomic(1, "recovery") is False
    with db._connect() as conn:
        requests = conn.execute("SELECT COUNT(*) FROM scheduling_replan_requests WHERE channel_id=1").fetchone()[0]
    assert requests == 7


def test_duplicate_cleanup_reports_and_unique_active_upload_protection(tmp_path):
    db = _db(tmp_path)
    with db._connect() as conn:
        conn.execute("DROP INDEX uq_active_planned_public_target")
        conn.execute("INSERT INTO videos (channel_id, canal, status, video_path) VALUES (1, 'one', 'awaiting_upload', '/a')")
        conn.execute("INSERT INTO videos (channel_id, canal, status, video_path) VALUES (1, 'one', 'awaiting_upload', '/b')")
        conn.execute("INSERT INTO planned_slots (channel_id, date_key, scheduled_at, target_public_at, status) VALUES (1, '2030-01-01', '2030-01-01 10:00:00', '2030-01-01 12:00:00', 'pending')")
        conn.execute("INSERT INTO planned_slots (channel_id, date_key, scheduled_at, target_public_at, status) VALUES (1, '2030-01-01', '2030-01-01 10:00:00', '2030-01-01 12:00:00', 'pending')")
        conn.commit()
    diagnostics = db.cleanup_scheduling_duplicates()
    assert diagnostics["planned_slots_cancelled"] == 1
    with db._connect() as conn:
        conn.execute("INSERT INTO generation_jobs (channel_id, video_id, action, status) VALUES (1, 1, 'upload_only', 'running')")
        with __import__('pytest').raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO generation_jobs (channel_id, video_id, action, status) VALUES (1, 1, 'upload_only', 'running')")


def test_public_admission_caps_longform_and_shorts_but_not_private_backlog(tmp_path):
    db = _db(tmp_path)
    from api.services.publication_policy import assert_public_admission
    with db._connect() as conn:
        conn.execute("INSERT INTO videos (channel_id, canal, video_path, status, published_at) VALUES (1, 'one', '/a', 'published', datetime('now'))")
        conn.execute("INSERT INTO shorts (channel_id, type, status, actual_published_at) VALUES (1, 'native', 'published', datetime('now'))")
        conn.commit()
    with __import__('pytest').raises(ValueError):
        assert_public_admission(db, channel_id=1, content_type="long")
    with __import__('pytest').raises(ValueError):
        assert_public_admission(db, channel_id=1, content_type="short")


def test_active_slots_are_capped_by_public_target_date_not_generation_date(tmp_path):
    db = _db(tmp_path)
    channel_policy.set_channel_delivery_state("normal", 1, db=db)
    with db._connect() as conn:
        conn.execute("DROP INDEX uq_active_planned_public_target")
        for idx, status in enumerate(("pending", "pending", "pending", "running"), 1):
            conn.execute("""INSERT INTO planned_slots
                (channel_id, date_key, scheduled_at, target_public_at, source_mode, status)
                VALUES (1, ?, ?, '2030-01-01T20:00:00+00:00', ?, ?)""",
                (f"2029-12-{20 + idx:02d}", f"2029-12-{20 + idx:02d} 10:00:00",
                 "viral" if idx % 2 else "original", status))
        conn.commit()
    result = db.reconcile_active_public_slot_caps()
    assert result["cancelled"] == 2
    with db._connect() as conn:
        rows = conn.execute("SELECT status FROM planned_slots WHERE target_public_at='2030-01-01T20:00:00+00:00' ORDER BY id").fetchall()
    assert sum(row[0] in ("pending", "running") for row in rows) == 2


def test_safe_full_replan_cancels_excess_public_targets_without_deleting_history(tmp_path):
    db = _db(tmp_path)
    channel_policy.set_channel_delivery_state("normal", 1, db=db)
    with db._connect() as conn:
        conn.execute("DROP INDEX uq_active_planned_public_target")
        for idx in range(4):
            conn.execute("""INSERT INTO planned_slots
                (channel_id, date_key, scheduled_at, target_public_at, status)
                VALUES (1, '2029-12-20', ?, '2030-01-01T20:00:00+00:00', 'pending')""",
                (f"2029-12-20 0{idx}:00:00",))
        conn.commit()
    from api.services.planning_service import safe_full_replan_preflight, safe_full_replan_apply
    preflight = safe_full_replan_preflight(db=db, horizon_days=1)
    safe_full_replan_apply(preflight["confirmation_token"], db=db)
    with db._connect() as conn:
        active = conn.execute("""SELECT COUNT(*) FROM planned_slots
            WHERE channel_id=1 AND target_public_at='2030-01-01T20:00:00+00:00'
              AND status IN ('pending','running')""").fetchone()[0]
        historical = conn.execute("SELECT COUNT(*) FROM planned_slots WHERE status='cancelled'").fetchone()[0]
    assert active == 2
    assert historical >= 2


def test_claim_planned_slot_is_single_winner(tmp_path):
    db = _db(tmp_path)
    slot_id = db.create_planned_slot(1, "2030-01-01", "2030-01-01 10:00:00")

    first = db.claim_planned_slot(slot_id, "worker-a")
    second = db.claim_planned_slot(slot_id, "worker-b")

    assert first is True
    assert second is False
    slot = db.get_planned_slots(date_key="2030-01-01")[0]
    assert slot["status"] == "running"
    assert slot["claimed_by"] == "worker-a"


def test_claim_upload_video_is_single_winner(tmp_path):
    db = _db(tmp_path)
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO videos (channel_id, canal, status, video_path) VALUES (1, 'one', 'awaiting_upload', '/tmp/video.mp4')"
        )
        video_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

    assert db.claim_upload_video(video_id, "worker-a") is True
    assert db.claim_upload_video(video_id, "worker-b") is False
    video = db.get_video(video_id)
    assert video["status"] == "uploading"
    assert video["upload_claimed_by"] == "worker-a"


def test_account_upload_reservation_counts_against_daily_cap(tmp_path):
    db = _db(tmp_path)
    db.set_system_state("pacing_profile", "normal")

    # Normal profile: long pool = 6, short pool = 4 (independientes).
    for index in range(1, 7):
        assert db.reserve_account_upload_slot(
            "shared-account", f"long-{index}", f"worker-{index}", content_type="long"
        ) is True

    assert db.reserve_account_upload_slot(
        "shared-account", "long-7", "worker-7", content_type="long"
    ) is False


def test_account_upload_reservation_short_pool_is_independent(tmp_path):
    """Longs y shorts tienen pools SEPARADOS: shorts pueden agotar su pool aun
    con longs reservando el suyo, y viceversa."""
    db = _db(tmp_path)
    db.set_system_state("pacing_profile", "normal")

    # Llénense los 6 slots long del pool.
    for index in range(1, 7):
        assert db.reserve_account_upload_slot(
            "shared-account", f"long-{index}", "worker", content_type="long"
        ) is True
    # El pool long está lleno; un long más se deniega.
    assert db.reserve_account_upload_slot(
        "shared-account", "long-7", "worker", content_type="long"
    ) is False

    # Pero los shorts tienen su PROPIO pool de 4: los 4 entran aunque longs estén llenos.
    for index in range(1, 5):
        assert db.reserve_account_upload_slot(
            "shared-account", f"short-{index}", "worker", content_type="short"
        ) is True
    assert db.reserve_account_upload_slot(
        "shared-account", "short-5", "worker", content_type="short"
    ) is False


def test_account_upload_reservation_is_idempotent_per_content(tmp_path):
    db = _db(tmp_path)

    assert db.reserve_account_upload_slot("shared-account", "video-1", "worker-a", content_type="long") is True
    assert db.reserve_account_upload_slot("shared-account", "video-1", "worker-b", content_type="long") is False


def test_account_upload_reservation_can_be_released(tmp_path):
    db = _db(tmp_path)

    assert db.reserve_account_upload_slot("shared-account", "video-1", "worker-a", content_type="long") is True
    assert db.release_account_upload_slot("shared-account", "video-1") is True
    assert db.reserve_account_upload_slot("shared-account", "video-2", "worker-b", content_type="long") is True


def test_account_upload_reservation_has_one_winner_for_last_slot(tmp_path):
    db = _db(tmp_path)
    db.set_system_state("pacing_profile", "normal")
    for index in range(1, 6):
        assert db.reserve_account_upload_slot(
            "shared-account", f"video-{index}", f"worker-{index}", content_type="long"
        ) is True

    def reserve(content_key):
        worker_db = ExtendedDatabase(str(db.db_path))
        return worker_db.reserve_account_upload_slot(
            "shared-account", content_key, content_key, content_type="long"
        )

    # Pool long normal = 6; ya hay 5 reservadas; de dos candidatos solo uno entra.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ("video-6", "video-7")))

    assert sorted(results) == [False, True]


def test_account_cap_shorts_not_blocked_by_pending_longs(tmp_path):
    """Fix (ago 2026): los shorts NO deben ahogarse por long-forms pendientes.

    Antes, un long-form ``awaiting_upload`` de hoy reservaba cupo y dejaba 0
    slots a shorts en cuentas con >= cap longs pendientes (p. ej. 8 longs
    pendientes con cap 8 → shorts jamás subían). Con pools separados, los longs
    pendientes NO afectan al pool de shorts.
    """
    db = _db(tmp_path)
    db.set_system_state("pacing_profile", "normal")
    with sqlite3.connect(str(db.db_path)) as conn:
        conn.execute("UPDATE channels SET google_account='acct' WHERE id=1")
        # Muchos long-forms pendientes de subir hoy (>= cap long) que antes
        # dejaban a shorts sin cupo.
        for i in range(1, 9):
            conn.execute(
                """INSERT INTO videos (id, channel_id, canal, status, video_path)
                   VALUES (?, 1, 'one', 'awaiting_upload', '/tmp/pending.mp4')""",
                (100 + i,),
            )
        conn.commit()

    # Los shorts SÍ entran. El tope de la cuenta = SUMA de los topes
    # configurados de sus canales (1 canal × 3 shorts/día en perfil normal);
    # no compite con los longs (pool independiente).
    for index in range(1, 4):
        assert db.reserve_account_upload_slot(
            "acct", f"short-{index}", "worker", content_type="short"
        ) is True
    # Un short más se deniega: agotó SU pool de 3 (independiente).
    assert db.reserve_account_upload_slot(
        "acct", "short-4", "worker", content_type="short"
    ) is False


def test_account_cap_no_pending_long_allows_all_shorts(tmp_path):
    """Sin competencia, los shorts agotan el tope diario de su canal (3 normal)."""
    db = _db(tmp_path)
    db.set_system_state("pacing_profile", "normal")
    with sqlite3.connect(str(db.db_path)) as conn:
        conn.execute("UPDATE channels SET google_account='acct' WHERE id=1")
        conn.commit()

    for index in range(1, 4):
        assert db.reserve_account_upload_slot(
            "acct", f"short-{index}", "worker", content_type="short"
        ) is True
    assert db.reserve_account_upload_slot(
        "acct", "short-4", "worker", content_type="short"
    ) is False
