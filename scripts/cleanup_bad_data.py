#!/usr/bin/env python3
"""One-off cleanup: fix stale running slots, zombie generating videos, and prune cancelled noise.

Run once to bring the live DB counts back to reality.
Safe to run with a live generation worker active — only touches records
whose jobs have already failed/cancelled.
"""
import sqlite3
import logging
import sys
import os
from datetime import date, datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("cleanup")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "autotube.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def show_before():
    conn = get_conn()
    print("\n=== BEFORE CLEANUP ===")
    
    videos_status = conn.execute("SELECT status, COUNT(*) c FROM videos GROUP BY status ORDER BY c DESC").fetchall()
    print("  Videos by status:")
    for r in videos_status:
        print(f"    {r['status']:20} {r['c']}")
    
    today = date.today().isoformat()
    try:
        slots = conn.execute("SELECT status, COUNT(*) c FROM planned_slots WHERE date_key=? GROUP BY status", (today,)).fetchall()
        print(f"  Planned slots today ({today}):")
        for r in slots:
            print(f"    {r['status']:20} {r['c']}")
    except Exception as e:
        print(f"  Planned slots: err {e}")
    
    print()
    conn.close()

def run_cleanup():
    """Run the same cleanup paths that will run in production."""
    logger.info("Starting one-off cleanup...")
    
    # 1. Import the sync functions and run them
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    
    # 2. Cleanup orphaned jobs/videos (now includes immediate zombie reconciliation)
    logger.info("Running orphan cleanup...")
    result = db.cleanup_orphaned_jobs()
    logger.info("Orphan cleanup result: %s", result)
    
    # 3. Prune old cancelled/skipped slots (older than yesterday)
    logger.info("Pruning old slots...")
    prune_result = db.prune_old_slots()
    logger.info("Prune result: %s", prune_result)
    
    # 4. Run _sync_running_slots from both modules to fix stuck running slots
    logger.info("Running slot sync (schedule_engine)...")
    from api.services.schedule_engine import _sync_running_slots as sync1
    try:
        sync1(db)
    except Exception as e:
        logger.warning("schedule_engine sync: %s", e)
    
    logger.info("Running slot sync (planning_service)...")
    from api.services.planning_service import _sync_running_slots as sync2
    try:
        sync2(db)
    except Exception as e:
        logger.warning("planning_service sync: %s", e)
    
    # 5. Also cancel stale pending slots (>3h past schedule)
    from api.services.schedule_engine import _cancel_stale_slots
    try:
        _cancel_stale_slots(db)
    except Exception as e:
        logger.warning("cancel_stale: %s", e)
    
    logger.info("Cleanup complete.")

def show_after():
    conn = get_conn()
    print("\n=== AFTER CLEANUP ===")
    
    videos_status = conn.execute("SELECT status, COUNT(*) c FROM videos GROUP BY status ORDER BY c DESC").fetchall()
    print("  Videos by status:")
    for r in videos_status:
        print(f"    {r['status']:20} {r['c']}")
    
    today = date.today().isoformat()
    try:
        slots = conn.execute("SELECT status, COUNT(*) c FROM planned_slots WHERE date_key=? GROUP BY status", (today,)).fetchall()
        print(f"  Planned slots today ({today}):")
        for r in slots:
            print(f"    {r['status']:20} {r['c']}")
    except Exception as e:
        print(f"  Planned slots: err {e}")
    
    # Also show what's actually running
    running_slots = conn.execute(
        "SELECT id, channel_id, job_id, video_id, status FROM planned_slots WHERE date_key=? AND status='running'",
        (today,)
    ).fetchall()
    print(f"\n  Running slots today: {len(running_slots)}")
    for r in running_slots:
        print(f"    id={r['id']} channel={r['channel_id']} job={r['job_id']} video={r['video_id']}")
    
    # Show generating videos with live jobs
    generating = conn.execute("""
        SELECT v.id, v.progress, v.progress_phase, j.id as job_id, j.status as job_status, j.last_heartbeat_at
        FROM videos v
        LEFT JOIN generation_jobs j ON j.video_id = v.id AND j.status = 'running'
        WHERE v.status = 'generating'
        ORDER BY v.id DESC
    """).fetchall()
    print(f"\n  Generating videos: {len(generating)}")
    for r in generating:
        print(f"    video={r['id']} progress={r['progress']} phase={r['progress_phase']} job={r['job_id']} job_status={r['job_status']} heartbeat={r['last_heartbeat_at']}")
    
    print()
    conn.close()

if __name__ == "__main__":
    show_before()
    run_cleanup()
    show_after()
