"""Glass Box must not bypass render-gate failures or race active retries."""

import sqlite3

from api.services.glass_box import glass_box_recover_orphaned_videos


class FakeDB:
    def __init__(self, path):
        self.path = path

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _seed(tmp_path, *, failed_error=None, active=False):
    dbp = tmp_path / "gb.db"
    video = tmp_path / "render.mp4"
    with video.open("wb") as fh:
        fh.seek(51 * 1024 * 1024 - 1)
        fh.write(b"\0")

    with sqlite3.connect(dbp) as conn:
        conn.executescript("""
            CREATE TABLE channels (id INTEGER PRIMARY KEY, slug TEXT, name TEXT);
            CREATE TABLE videos (
                id INTEGER PRIMARY KEY, channel_id INTEGER, status TEXT,
                video_path TEXT, progress INTEGER, progress_phase TEXT,
                generation_finished_at TEXT, error_message TEXT,
                titulo_final TEXT, created_at TEXT, scheduled_upload_at TEXT
            );
            CREATE TABLE generation_jobs (
                id INTEGER PRIMARY KEY, video_id INTEGER, action TEXT,
                status TEXT, error_msg TEXT
            );
        """)
        conn.execute("INSERT INTO channels VALUES (4, 'canal3', 'Canal 3')")
        conn.execute(
            """INSERT INTO videos
               (id, channel_id, status, video_path, progress, progress_phase,
                generation_finished_at, error_message, titulo_final, created_at)
               VALUES (2723, 4, 'error', ?, 87, 'post_validate', NULL, '', 't', '2026-10-22')""",
            (str(video),),
        )
        if failed_error:
            conn.execute(
                "INSERT INTO generation_jobs (video_id, action, status, error_msg) "
                "VALUES (2723, 'generate_only', 'failed', ?)",
                (failed_error,),
            )
        if active:
            conn.execute(
                "INSERT INTO generation_jobs (video_id, action, status, error_msg) "
                "VALUES (2723, 'generate_only', 'queued', 'manual retry')"
            )
        conn.commit()
    return FakeDB(dbp), dbp


def test_glass_box_skips_render_gate_failure(tmp_path):
    db, _ = _seed(tmp_path, failed_error="render_gate: 6.6s de negro")
    out = glass_box_recover_orphaned_videos(db=db)
    assert out["recovered"] == 0
    assert out["skipped"] >= 1


def test_glass_box_skips_active_retry(tmp_path):
    db, _ = _seed(tmp_path, active=True)
    out = glass_box_recover_orphaned_videos(db=db)
    assert out["recovered"] == 0
    assert out["skipped"] >= 1


def test_glass_box_recovers_eligible_orphan(tmp_path):
    db, dbp = _seed(tmp_path)
    out = glass_box_recover_orphaned_videos(db=db)
    assert out["recovered"] == 1
    with sqlite3.connect(dbp) as conn:
        status = conn.execute("SELECT status FROM videos WHERE id=2723").fetchone()[0]
    assert status == "awaiting_upload"
