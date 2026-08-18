"""
Marathon mode: long-form ~1-hour video generation for a round-robin selected channel.

Triggered when the accumulated pipeline queue across all channels exceeds the threshold:
    backlog = awaiting_upload + uploaded_private (across ALL active channels)
    threshold = MARATHON_BACKLOG_PER_CHANNEL × número de canales activos

Cooldown: un canal que acaba de maratonear (MARATHON_COOLDOWN_HOURS, default 24h)
no vuelve a ser elegible hasta que pasa ese tiempo — la rotación round-robin
simplemente lo salta (select_marathon_channel).

Selects an eligible channel (MARATHON_ENABLED=True), creates a queued generation_jobs
record, and lets the normal _queue_consumer dispatch it when the worker is available.

The marathon is a long-form video just like any other: it respects the single-worker
serialization invariant. Once generated, it uploads directly (generate_and_upload).
It does NOT create planned_slots — it does not affect the normal planning schedule.
"""

from __future__ import annotations

import logging
import random
import json
from datetime import datetime

logger = logging.getLogger("autotube.marathon")


# ── Public API ─────────────────────────────────────────────────

def _marathon_cooldown_hours(cfg: dict) -> float:
    """Cooldown del canal (MARATHON_COOLDOWN_HOURS) con fallback al default."""
    try:
        from config.defaults import MARATHON_COOLDOWN_HOURS as _DEFAULT_COOLDOWN_H
    except ImportError:
        _DEFAULT_COOLDOWN_H = 24
    try:
        return float(cfg.get("MARATHON_COOLDOWN_HOURS", _DEFAULT_COOLDOWN_H) or _DEFAULT_COOLDOWN_H)
    except (TypeError, ValueError):
        return float(_DEFAULT_COOLDOWN_H)


def _channel_in_marathon_cooldown(db, channel_id: int, cfg: dict, now: datetime | None = None) -> bool:
    """True si el canal maratoneó hace menos de MARATHON_COOLDOWN_HOURS horas.

    El record de get_last_marathon guarda la fecha como "%Y-%m-%d %H:%M:%S"
    (sin tz, hora local). Los canales en cooldown NO se eligen: la rotación
    round-robin simplemente los salta.
    """
    try:
        last = db.get_last_marathon(channel_id)
        if not last or not last.get("date"):
            return False
        last_dt = datetime.strptime(str(last["date"]), "%Y-%m-%d %H:%M:%S")
        now = now or datetime.now()
        cooldown_h = _marathon_cooldown_hours(cfg)
        if (now - last_dt).total_seconds() < cooldown_h * 3600:
            return True
    except Exception:
        pass
    return False

def calculate_backlog(db) -> int:
    """Calculate total backlog: awaiting_upload + uploaded_private across all channels.

    These are videos that have been generated (scripts + TTS + media done) but
    haven't been published yet — they're sitting in the pipeline queue waiting
    for upload windows or warmup completion.

    past_due planned_slots are NOT included — they measure scheduling delay, not
    pipeline accumulation. A video that finishes generating on time but waits for
    its upload window shows as awaiting_upload, not past_due.
    """
    try:
        awaiting = db.count_all_awaiting_upload()
    except Exception:
        awaiting = 0

    try:
        warming = db.count_all_warming()
    except Exception:
        warming = 0

    return awaiting + warming


def select_marathon_channel(db) -> tuple[str, int, dict] | None:
    """Select the next channel for a marathon video.

    Uses a deterministic rotation that gets shuffled on each new iteration.
    Channels are eligible if MARATHON_ENABLED=True. v40: los canales en
    cooldown (maratoneados hace < MARATHON_COOLDOWN_HOURS) se saltan — la
    rotación round-robin los omite sin romper el orden.

    Returns:
        (slug, channel_id, config_json) or None if no channel eligible.
    """
    channels = db.get_channels(active_only=True)
    if not channels:
        return None

    # Filter: only channels with MARATHON_ENABLED=True
    eligible = []

    for ch in channels:
        ch_id = ch.id if hasattr(ch, 'id') else ch.get("id", 0)
        slug = ch.slug if hasattr(ch, 'slug') else ch.get("slug", "?")

        cfg_raw = ch.config_json if hasattr(ch, 'config_json') else ch.get("config_json", "{}")
        try:
            cfg = json.loads(cfg_raw) if isinstance(cfg_raw, str) else (cfg_raw or {})
        except (json.JSONDecodeError, TypeError):
            cfg = {}

        if not cfg.get("MARATHON_ENABLED", False):
            logger.debug("Marathon: %s disabled (MARATHON_ENABLED=False)", slug)
            continue

        eligible.append((slug, ch_id, cfg))

    # ── v40: cooldown filter — skip channels that marathoned recently ──
    all_eligible = eligible
    eligible = [
        item for item in all_eligible
        if not _channel_in_marathon_cooldown(db, item[1], item[2])
    ]
    skipped_cooldown = len(all_eligible) - len(eligible)
    if skipped_cooldown:
        logger.debug(
            "Marathon: %d channel(s) skipped (cooldown), %d eligible",
            skipped_cooldown, len(eligible),
        )

    if not eligible:
        logger.debug("Marathon: no eligible channels (%d active, %d in cooldown)",
                     len(channels), skipped_cooldown)
        return None

    # ── Rotation: shuffle order on first run, persist for restarts ──
    rotation = db.get_marathon_rotation_order()

    if not rotation or len(rotation) == 0:
        random.shuffle(eligible)
        rotation = [slug for slug, _, _ in eligible]
        db.set_marathon_rotation_order(rotation)
        logger.info("Marathon: new rotation order: %s", " → ".join(rotation))

    # Pop the first eligible (non-cooldown) slug from the persisted rotation,
    # preserving round-robin: cooldown channels are simply skipped.
    selected = None
    for i, rotation_slug in enumerate(rotation):
        match = None
        for slug, ch_id, cfg in eligible:
            if slug == rotation_slug:
                match = (slug, ch_id, cfg)
                break
        if match is not None:
            selected = match
            rotation.pop(i)
            break

    if selected is None:
        logger.warning(
            "Marathon: no rotation slug is currently eligible, rebuilding rotation"
        )
        random.shuffle(eligible)
        rotation = [slug for slug, _, _ in eligible]
        db.set_marathon_rotation_order(rotation)
        if rotation:
            selected_slug = rotation.pop(0)
            for slug, ch_id, cfg in eligible:
                if slug == selected_slug:
                    selected = (slug, ch_id, cfg)
                    break

    # Push selected to end of rotation and save
    if selected:
        rotation.append(selected[0])
        db.set_marathon_rotation_order(rotation)

    return selected


def check_and_dispatch_marathon(db) -> dict | None:
    """Main entry point: check backlog and enqueue a marathon if conditions are met.

    Condition: (awaiting_upload + uploaded_private) across ALL channels
               >= MARATHON_BACKLOG_PER_CHANNEL × número de canales activos.

    Called by the schedule checker loop every ~60 minutes.
    Does NOT create planned_slots or fire subprocesses directly.
    Creates a queued generation_jobs record → the normal _queue_consumer
    picks it up when the single long-form worker is free.

    Returns:
        dict with dispatch info if a marathon was enqueued, or None.
    """
    from config.defaults import MARATHON_BACKLOG_PER_CHANNEL
    from config.settings import YT_REMEDIATION_MODE

    if YT_REMEDIATION_MODE:
        logger.info("Marathon: remediation mode active — generation is held until backlog preflight")
        return None

    # 1. Check backlog (awaiting + warming only — pipeline accumulation signal)
    awaiting = 0
    warming = 0
    try:
        awaiting = db.count_all_awaiting_upload()
    except Exception:
        pass
    try:
        warming = db.count_all_warming()
    except Exception:
        pass
    backlog = awaiting + warming

    # 2. Dynamic threshold: per-channel value × active channels
    active_channels = db.get_channels(active_only=True)
    active_count = len(active_channels) if active_channels else 1
    min_backlog = MARATHON_BACKLOG_PER_CHANNEL * active_count

    if backlog < min_backlog:
        logger.debug(
            "Marathon: awaiting=%d + warming=%d = %d < %d (per_ch=%d × ch=%d), skipping",
            awaiting, warming, backlog, min_backlog,
            MARATHON_BACKLOG_PER_CHANNEL, active_count,
        )
        return None

    logger.info(
        "Marathon: awaiting=%d + warming=%d = %d >= %d (per_ch=%d × ch=%d) — evaluating candidates",
        awaiting, warming, backlog, min_backlog,
        MARATHON_BACKLOG_PER_CHANNEL, active_count,
    )

    # 2. Guard: don't enqueue if there's already a queued or running marathon job
    #    (prevents duplicate marathon jobs piling up)
    if _has_pending_marathon(db):
        logger.debug("Marathon: already has a pending marathon job — skipping")
        return None

    # 3. Select channel
    selected = select_marathon_channel(db)
    if selected is None:
        return None

    slug, channel_id, cfg = selected

    # 3b. v40: cooldown double-check (defensivo — select_marathon_channel ya
    #     filtra, pero el record puede haberse creado entre medias).
    if _channel_in_marathon_cooldown(db, channel_id, cfg):
        logger.debug("Marathon: deferred — channel %s in cooldown", slug)
        return None

    # 4. Build marathon config from channel config
    marathon_cfg = {
        "duration_target": cfg.get("MARATHON_VIDEO_DURATION_TARGET", 60),
        "num_sections": cfg.get("MARATHON_NUM_SECTIONS", 12),
        "narrative_format": cfg.get("MARATHON_NARRATIVE_FORMAT", "top_cases"),
        "title_formulas": cfg.get("MARATHON_TITLE_FORMULAS", []),
        "hook_types": cfg.get("MARATHON_HOOK_TYPES", []),
        "validate_title": cfg.get("MARATHON_VALIDATE_TITLE", True),
        "min_virality_score": cfg.get("MARATHON_MIN_VIRALITY_SCORE", 7),
        "outline_chapters": cfg.get("MARATHON_OUTLINE_CHAPTERS", 15),
        "media_video_pct": cfg.get("MARATHON_MEDIA_VIDEO_PCT", 20),
        "script_words_min": cfg.get("MARATHON_SCRIPT_WORDS_MIN", 8000),
        "script_words_max": cfg.get("MARATHON_SCRIPT_WORDS_MAX", 12000),
        "script_blocks_min": cfg.get("MARATHON_SCRIPT_BLOCKS_MIN", 50),
        "script_blocks_max": cfg.get("MARATHON_SCRIPT_BLOCKS_MAX", 90),
        "llm_max_batches": cfg.get("MARATHON_LLM_MAX_BATCHES", 150),
        "llm_max_empty_strikes": cfg.get("MARATHON_LLM_MAX_EMPTY_STRIKES", 20),
    }

    # 5. Guard: don't create if channel already has an active job (running or queued)
    active = db.get_active_job_for_channel(channel_id)
    if active:
        logger.info("Marathon: deferred — channel %s has active job #%d", slug, active["id"])
        return None

    # 6. Create video record (no planned_slot — marathon is outside the planning system)
    # Use PUBLISH_MODE from channel config (defaults to scheduled as in MARATHON_PUBLISH_MODE)
    marathon_publish_mode = cfg.get("PUBLISH_MODE", cfg.get("MARATHON_PUBLISH_MODE", "scheduled"))
    with db._connect() as conn:
        cursor = conn.execute(
            """INSERT INTO videos 
               (canal, channel_id, video_path, status, progress, publish_mode,
                is_marathon, marathon_config, created_at)
               VALUES (?, ?, '', 'generating', 0, ?, 1, ?, CURRENT_TIMESTAMP)""",
            (slug, channel_id, marathon_publish_mode, json.dumps(marathon_cfg)),
        )
        conn.commit()
        video_id = cursor.lastrowid

    # 7. Create queued job — _queue_consumer dispatches when worker is free
    #    If the channel's GCP project quota is exhausted, use generate_only so
    #    the marathon is generated and stored locally, then uploaded when that
    #    project recovers (per-project breaker — Fase cuota ago 2026).
    _quota_exhausted = False
    try:
        _quota_exhausted = db.is_quota_exhausted_for_channel(slug)
    except Exception:
        pass
    action = "generate_only" if _quota_exhausted else "generate_and_upload"
    job_id = db.create_job(channel_id, action, video_id)
    if _quota_exhausted:
        logger.info("[MARATHON] Quota exhausted — dispatching as generate_only (no upload)")
    db.update_job(job_id, status="queued")

    # 8. Record marathon dispatch
    db.record_marathon(channel_id, "running")

    logger.info(
        "[MARATHON] Enqueued! channel=%s duration=%dmin sections=%d format=%s job=%d video=%d backlog=%d",
        slug, marathon_cfg["duration_target"], marathon_cfg["num_sections"],
        marathon_cfg["narrative_format"], job_id, video_id, backlog,
    )

    return {
        "dispatched": True,
        "channel": slug,
        "channel_id": channel_id,
        "job_id": job_id,
        "video_id": video_id,
        "marathon_config": marathon_cfg,
        "backlog": backlog,
    }


# ── Internal helpers ───────────────────────────────────────────

def _has_pending_marathon(db) -> bool:
    """Check if there's already a queued, running, or recently-failed marathon job.

    Recently-failed marathons (within 4 hours) are treated as "pending" to prevent
    the dispatcher from spawning duplicate marathons after an API restart kills
    all workers. Without this guard, all marathons become 'failed' on restart and
    the dispatcher sees zero pending → creates more → restart kills more → loop.
    """
    try:
        with db._connect() as conn:
            # Active jobs (queued/running)
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM generation_jobs gj "
                "JOIN videos v ON gj.video_id = v.id "
                "WHERE v.is_marathon = 1 "
                "AND gj.status IN ('queued', 'running')"
            ).fetchone()
            if row and row["cnt"] > 0:
                return True

            # Recently-failed marathon jobs (within 4 hours)
            # Prevents re-spawning after API restart turns all running → failed
            row2 = conn.execute(
                "SELECT COUNT(*) as cnt FROM generation_jobs gj "
                "JOIN videos v ON gj.video_id = v.id "
                "WHERE v.is_marathon = 1 "
                "AND gj.status = 'failed' "
                "AND gj.created_at > datetime('now', 'localtime', '-4 hours')"
            ).fetchone()
            return row2["cnt"] > 0 if row2 else False
    except Exception:
        return False
