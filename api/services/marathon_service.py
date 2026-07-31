"""
Marathon mode: long-form ~1-hour video generation for a randomly selected channel.

Triggered when the backlog of pending slots across all channels exceeds a threshold.
Selects one eligible channel (not in cooldown), creates a marathon planned_slot,
and lets the normal dispatch mechanism process it.

The marathon naturally drains the queue since it ties up the single long-form worker
for 4-6 hours, preventing any other long-form generation.
"""

from __future__ import annotations

import logging
import random
import json
from datetime import datetime, timedelta

logger = logging.getLogger("autotube.marathon")


# ── Public API ─────────────────────────────────────────────────

def calculate_backlog(db) -> int:
    """Calculate total backlog: past-due planned_slots + awaiting_upload videos.
    
    Returns the number of "stuck" videos across all channels.
    """
    try:
        past_due = db.count_past_due_slots()
    except Exception:
        past_due = 0
    
    try:
        awaiting = db.count_all_awaiting_upload()
    except Exception:
        awaiting = 0
    
    return past_due + awaiting


def select_marathon_channel(db, min_backlog: int = 8) -> tuple[str, int, dict] | None:
    """Select the next channel for a marathon video.
    
    Uses a deterministic rotation that gets shuffled on each new iteration.
    Channels in cooldown (success or failure) are skipped.
    
    Args:
        db: ExtendedDatabase instance.
        min_backlog: Minimum backlog to trigger a marathon.
    
    Returns:
        (slug, channel_id, config_json) or None if no channel eligible.
    """
    # Load all active channels
    channels = db.get_channels(active_only=True)
    if not channels:
        return None
    
    # Filter: only channels with MARATHON_ENABLED=True and not in cooldown
    eligible = []
    now = datetime.now()
    
    for ch in channels:
        ch_id = ch.id if hasattr(ch, 'id') else ch.get("id", 0)
        slug = ch.slug if hasattr(ch, 'slug') else ch.get("slug", "?")
        
        # Read channel config from DB
        cfg_raw = ch.config_json if hasattr(ch, 'config_json') else ch.get("config_json", "{}")
        try:
            cfg = json.loads(cfg_raw) if isinstance(cfg_raw, str) else (cfg_raw or {})
        except (json.JSONDecodeError, TypeError):
            cfg = {}
        
        if not cfg.get("MARATHON_ENABLED", False):
            logger.debug("Marathon: %s disabled (MARATHON_ENABLED=False)", slug)
            continue
        
        # Check cooldown
        last_marathon = db.get_last_marathon(ch_id)
        if last_marathon:
            days_since = (now - last_marathon["date"]).days if last_marathon.get("date") else 999
            status = last_marathon.get("status", "completed")
            cooldown_success = cfg.get("MARATHON_COOLDOWN_SUCCESS_DAYS", 7)
            cooldown_failure = cfg.get("MARATHON_COOLDOWN_FAILURE_DAYS", 1)
            
            if status == "completed" and days_since < cooldown_success:
                logger.debug("Marathon: %s in cooldown (success, %d/%d days)",
                             slug, days_since, cooldown_success)
                continue
            
            if status == "failed" and days_since < cooldown_failure:
                logger.debug("Marathon: %s in failure cooldown (%d/%d days)",
                             slug, days_since, cooldown_failure)
                continue
        
        eligible.append((slug, ch_id, cfg))
    
    if not eligible:
        logger.debug("Marathon: no eligible channels (%d active)", len(channels))
        return None
    
    # ── Rotation: shuffle order on each iteration, persist for restarts ──
    rotation = db.get_marathon_rotation_order()
    
    if not rotation or len(rotation) == 0:
        # No rotation saved → create fresh shuffle
        random.shuffle(eligible)
        rotation = [slug for slug, _, _ in eligible]
        db.set_marathon_rotation_order(rotation)
        logger.info("Marathon: new rotation order: %s", " → ".join(rotation))
    
    # Pop first from rotation
    selected_slug = rotation.pop(0)
    
    # Find the matching channel data
    selected = None
    for slug, ch_id, cfg in eligible:
        if slug == selected_slug:
            selected = (slug, ch_id, cfg)
            break
    
    if selected is None:
        # Selected slug is no longer eligible → reshuffle
        logger.warning("Marathon: selected slug %s no longer eligible, rebuilding rotation", selected_slug)
        random.shuffle(eligible)
        rotation = [slug for slug, _, _ in eligible]
        db.set_marathon_rotation_order(rotation)
        if rotation:
            selected_slug = rotation[0]
            for slug, ch_id, cfg in eligible:
                if slug == selected_slug:
                    selected = (slug, ch_id, cfg)
                    rotation.pop(0)
                    break
    
    # Push selected to end of rotation and save
    if selected:
        rotation.append(selected[0])
        db.set_marathon_rotation_order(rotation)
    
    return selected


def check_and_dispatch_marathon(db, min_backlog: int = 8) -> dict | None:
    """Main entry point: check backlog and dispatch a marathon if conditions are met.
    
    Called by the schedule checker loop every ~30 minutes.
    Uses the same dispatch pattern as planning_service.py to create a job.
    
    Args:
        db: ExtendedDatabase instance.
        min_backlog: Minimum past-due + awaiting_upload to trigger marathon.
    
    Returns:
        dict with dispatch info if a marathon was dispatched, or None.
    """
    # 1. Check backlog
    backlog = calculate_backlog(db)
    if backlog < min_backlog:
        logger.debug("Marathon: backlog=%d < min=%d, skipping", backlog, min_backlog)
        return None
    
    logger.info("Marathon: backlog=%d >= min=%d — evaluating candidates", backlog, min_backlog)
    
    # 2. Select channel
    selected = select_marathon_channel(db, min_backlog)
    if selected is None:
        return None
    
    slug, channel_id, cfg = selected
    
    # 3. Build marathon config from channel config
    marathon_cfg = {
        "duration_target": cfg.get("MARATHON_VIDEO_DURATION_TARGET", 60),
        "num_sections": cfg.get("MARATHON_NUM_SECTIONS", 12),
        "narrative_format": cfg.get("MARATHON_NARRATIVE_FORMAT", "top_cases"),
        "title_format": cfg.get("MARATHON_TITLE_FORMAT", ""),
        "outline_chapters": cfg.get("MARATHON_OUTLINE_CHAPTERS", 15),
        "media_video_pct": cfg.get("MARATHON_MEDIA_VIDEO_PCT", 20),
        "script_words_min": cfg.get("MARATHON_SCRIPT_WORDS_MIN", 8000),
        "script_words_max": cfg.get("MARATHON_SCRIPT_WORDS_MAX", 12000),
        "script_blocks_min": cfg.get("MARATHON_SCRIPT_BLOCKS_MIN", 50),
        "script_blocks_max": cfg.get("MARATHON_SCRIPT_BLOCKS_MAX", 90),
        "llm_max_batches": cfg.get("MARATHON_LLM_MAX_BATCHES", 150),
        "llm_max_empty_strikes": cfg.get("MARATHON_LLM_MAX_EMPTY_STRIKES", 20),
    }
    
    # 4. Import dispatch lock and create the job atomically
    from api.services.generation_service import _DISPATCH_LOCK
    
    with _DISPATCH_LOCK:
        # Re-check guards under lock (TOCTOU prevention)
        active_count = db.count_active_longform_jobs()
        if active_count >= 1:
            logger.info("Marathon: deferred — %d active long-form job(s)", active_count)
            return None
        
        # Check channel-level guard
        active = db.get_active_job_for_channel(channel_id)
        if active:
            logger.info("Marathon: deferred — channel %s has active job #%d", slug, active["id"])
            return None
        
        # Create planned_slot for marathon (needed for tracking)
        now = datetime.now()
        date_key = now.strftime("%Y-%m-%d")
        
        # Target publish: next day prime time (20:00)
        target_publish = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if target_publish <= now:
            target_publish += timedelta(days=1)
        
        target_upload = target_publish - timedelta(hours=4)  # upload 4h before
        
        with db._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO planned_slots 
                   (channel_id, date_key, scheduled_at, target_upload_at, target_public_at,
                    status, slot_position, source_mode)
                   VALUES (?, ?, datetime('now'), ?, ?, 'running', 99, 'marathon')""",
                (channel_id, date_key,
                 target_upload.strftime("%Y-%m-%d %H:%M:%S"),
                 target_publish.strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
            slot_id = cursor.lastrowid
        
        # Create video record
        with db._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO videos 
                   (canal, channel_id, video_path, status, progress, is_marathon,
                    marathon_config, publish_mode, target_public_at, created_at)
                   VALUES (?, ?, '', 'generating', 0, 1, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (slug, channel_id, json.dumps(marathon_cfg),
                 cfg.get("MARATHON_PUBLISH_MODE", "scheduled"),
                 target_publish.strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
            video_id = cursor.lastrowid
        
        # Create job
        action = "generate_only" if cfg.get("MARATHON_PUBLISH_MODE") == "scheduled" else "generate_and_upload"
        job_id = db.create_job(channel_id, action, video_id)
        db.update_job(job_id, status="running")
        
        # Link job to slot
        db.update_slot_status(slot_id, "running", job_id=job_id, video_id=video_id)
    
    # 5. Record marathon dispatch in system_state
    db.record_marathon(channel_id, "running")
    
    # 6. Fire-and-forget subprocess
    import asyncio as _asyncio_mar
    from api.services.generation_service import (
        start_generation_job_subprocess,
        USE_SUBPROCESS_WORKER,
    )
    
    if USE_SUBPROCESS_WORKER:
        try:
            loop = _asyncio_mar.get_event_loop()
        except RuntimeError:
            loop = _asyncio_mar.new_event_loop()
            _asyncio_mar.set_event_loop(loop)
        
        loop.create_task(start_generation_job_subprocess(
            job_id=job_id,
            channel_id=channel_id,
            video_id=video_id,
            action=action,
            source_mode="marathon",
        ))
    
    logger.info(
        "[MARATHON] Dispatched! channel=%s duration=%dmin sections=%d format=%s job=%d video=%d",
        slug, marathon_cfg["duration_target"], marathon_cfg["num_sections"],
        marathon_cfg["narrative_format"], job_id, video_id,
    )
    
    return {
        "dispatched": True,
        "channel": slug,
        "channel_id": channel_id,
        "slot_id": slot_id,
        "job_id": job_id,
        "video_id": video_id,
        "marathon_config": marathon_cfg,
        "backlog": backlog,
    }
