"""
Per-channel shorts scheduling engine.
Computes publish slots/day/channel based on per-channel
shorts_planning_config (shorts_native_per_day + shorts_clip_per_day).

Supports variable native/clip counts per channel with:
- Native slots spread across fixed time windows
- Clip slots linked to long-form video completion
- ±20 min deterministic jitter per channel
- Fair daily rotation across channels
"""

import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("autotube.shorts_scheduler")

# ── Timezone constants ────────────────────────────────────────
CEST = ZoneInfo("Europe/Madrid")
UTC = timezone.utc


def _cest_to_utc(date_str: str, hour: int, minute: int) -> str:
    """Convert a naive CEST datetime (YYYY-MM-DD HH:MM:SS) to UTC string."""
    dt_cest = datetime.strptime(
        f"{date_str} {hour:02d}:{minute:02d}:00", "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=CEST)
    dt_utc = dt_cest.astimezone(UTC)
    return dt_utc.strftime("%Y-%m-%d %H:%M:%S")


# ── Per-type time windows (CEST, center of jitter range) ─────
# Native slots: spread across the day in optimal engagement windows
NATIVE_WINDOWS = [
    (9, 30),     # morning
    (12, 30),    # midday
    (15, 0),     # early afternoon
    (18, 30),    # evening
    (21, 0),     # prime time
    (23, 30),    # late night
]

# Clip slots: after long-form videos should have completed
CLIP_WINDOWS = [
    (17, 0),     # after first long video (~16:00)
    (20, 30),    # after second long video (~19:30)
    (23, 0),     # after third long video (~22:00)
]

JITTER_MINUTES = 20


def _day_seed(date_str: str, channel_slug: str, slot_idx: int) -> int:
    """Deterministic seed for a date+channel+slot combination."""
    h = hashlib.md5(f"{date_str}::{channel_slug}::{slot_idx}".encode()).hexdigest()
    return int(h[:8], 16)


def _jitter_minutes(date_str: str, channel_slug: str, slot_idx: int) -> int:
    """Return deterministic jitter in minutes (-JITTER_MINUTES .. +JITTER_MINUTES)."""
    seed = _day_seed(date_str, channel_slug, slot_idx)
    return (seed % (2 * JITTER_MINUTES + 1)) - JITTER_MINUTES


def _channel_rank(date_str: str, channel_slug: str, window_idx: int) -> float:
    """Compute a deterministic rank for ordering channels within a time window.

    Uses MD5 to get a fair rotation that changes daily.
    Lower rank → scheduled earlier within the window.
    """
    seed = _day_seed(date_str, channel_slug, window_idx)
    return (seed % 10000) / 10000.0  # 0.0 – 1.0


def _build_shorts_slots_for_channel(
    ch: dict,
    date_str: str,
    native_count: int,
    clip_count: int,
    global_start_pos: int,
) -> tuple[list[dict], int]:
    """Generate shorts slots for ONE channel for a given date.

    Args:
        ch: channel dict with at least id, slug, name
        date_str: YYYY-MM-DD
        native_count: how many native shorts today
        clip_count: how many clip shorts today
        global_start_pos: starting slot_position for this channel

    Returns (slots_list, next_global_pos).
    """
    slug = ch["slug"]
    slots = []
    pos = global_start_pos

    # ── Native slots ──────────────────────────────────────
    for i in range(native_count):
        # Pick window i (wrap if more native than windows)
        win_idx = i % len(NATIVE_WINDOWS)
        target_h, target_m = NATIVE_WINDOWS[win_idx]
        jitter = _jitter_minutes(date_str, slug, win_idx)
        total_min = target_h * 60 + target_m + jitter
        total_min = max(0, min(total_min, 24 * 60 - 1))
        h, m = total_min // 60, total_min % 60

        target_utc = _cest_to_utc(date_str, h, m)
        sched_total = max(0, total_min - 3)
        sh, sm = sched_total // 60, sched_total % 60
        sched_utc = _cest_to_utc(date_str, sh, sm)

        pos += 1
        slots.append({
            "channel_id": ch["id"],
            "date_key": date_str,
            "scheduled_at": sched_utc,
            "target_upload_at": target_utc,
            "short_type": "native",
            "long_slot_position": None,
            "slot_position": pos,
            "channel_name": ch.get("name", slug),
            "channel_slug": slug,
        })

    # ── Clip slots ────────────────────────────────────────
    for i in range(clip_count):
        win_idx = i % len(CLIP_WINDOWS)
        target_h, target_m = CLIP_WINDOWS[win_idx]
        jitter = _jitter_minutes(date_str, slug, win_idx + 100)  # offset seed
        total_min = target_h * 60 + target_m + jitter
        total_min = max(0, min(total_min, 24 * 60 - 1))
        h, m = total_min // 60, total_min % 60

        target_utc = _cest_to_utc(date_str, h, m)
        sched_total = max(0, total_min - 3)
        sh, sm = sched_total // 60, sched_total % 60
        sched_utc = _cest_to_utc(date_str, sh, sm)

        long_slot_pos = i + 1  # clip #i depends on long-form slot #(i+1)

        pos += 1
        slots.append({
            "channel_id": ch["id"],
            "date_key": date_str,
            "scheduled_at": sched_utc,
            "target_upload_at": target_utc,
            "short_type": "clip",
            "long_slot_position": long_slot_pos,
            "slot_position": pos,
            "channel_name": ch.get("name", slug),
            "channel_slug": slug,
        })

    return slots, pos


def compute_daily_shorts_slots(date_str: str, db=None) -> list[dict]:
    """Compute shorts slots per active channel for a given date (YYYY-MM-DD).

    Reads shorts_native_per_day and shorts_clip_per_day from
    shorts_planning_config per channel. Windows are defined in CEST.
    Timestamps are converted to UTC for storage.

    Returns list of dicts sorted by scheduled_at.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    # Get active channels
    channels = db.get_channels(active_only=True)
    channels = [ch for ch in channels if ch["slug"] != "test"]

    if len(channels) < 1:
        logger.warning("No active channels found — cannot compute shorts schedule")
        return []

    # Get per-channel shorts configs
    slugs_to_ch = {ch["slug"]: ch for ch in channels}
    shorts_configs = db.get_shorts_planning_config()
    config_by_chid = {sc["channel_id"]: sc for sc in shorts_configs}

    all_slots = []
    global_pos = 0

    for ch in channels:
        ch_id = ch["id"]
        sc = config_by_chid.get(ch_id, {})
        if not sc.get("shorts_enabled", True):
            continue

        native_count = sc.get("shorts_native_per_day", 3)
        clip_count = sc.get("shorts_clip_per_day", 2)

        if native_count == 0 and clip_count == 0:
            continue

        channel_slots, global_pos = _build_shorts_slots_for_channel(
            ch, date_str, native_count, clip_count, global_pos,
        )
        all_slots.extend(channel_slots)

    # Sort by scheduled_at
    all_slots.sort(key=lambda s: s["scheduled_at"])

    # Re-number slot positions after final sort
    for pos, s in enumerate(all_slots, 1):
        s["slot_position"] = pos

    return all_slots


def persist_daily_shorts_slots(date_str: str, slots: list[dict], db=None) -> int:
    """Store computed shorts slots in shorts_planned_slots table.
    Deletes existing pending slots for this date first.
    Returns count of stored slots.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    # Delete existing PENDING slots for this date (keep completed/running)
    with db._connect() as conn:
        conn.execute(
            "DELETE FROM shorts_planned_slots WHERE date_key = ? AND status = 'pending'",
            (date_str,),
        )
        conn.commit()

    if not slots:
        return 0

    count = db.create_shorts_planned_slots_batch(slots)
    logger.info("Persisted %d shorts slots for %s", count, date_str)
    return count


def generate_upcoming_shorts(days: int = 7, db=None) -> dict:
    """Generate and persist shorts slots for the next N days (including today).

    Returns summary dict: {date_str: "N slots" | "ERROR: ..."}.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    today = datetime.now(CEST).date()
    results = {}

    for day_offset in range(days):
        day_str = (today + timedelta(days=day_offset)).isoformat()
        try:
            slots = compute_daily_shorts_slots(day_str, db)
            count = persist_daily_shorts_slots(day_str, slots, db)
            results[day_str] = f"{count} slots"
        except Exception as e:
            logger.error("Failed to generate shorts schedule for %s: %s", day_str, e)
            results[day_str] = f"ERROR: {e}"

    total = sum(
        int(v.split()[0]) for v in results.values() if v and v[0].isdigit()
    )
    logger.info(
        "Generated shorts slots for %d days: %d total",
        days, total,
    )
    return results


def ensure_today_shorts_scheduled(db=None) -> bool:
    """Check if today has shorts planned slots. If not, generate them.
    Returns True if slots exist (existing or newly generated).
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    today = datetime.now(CEST).date().isoformat()
    existing = db.get_shorts_planned_slots(date_key=today)

    active_slots = [s for s in existing if s["status"] != "cancelled"]
    if len(active_slots) > 0:
        logger.debug("Today's shorts schedule OK: %d active slots", len(active_slots))
        return True

    logger.info("Regenerating today's shorts schedule")
    slots = compute_daily_shorts_slots(today, db)
    count = persist_daily_shorts_slots(today, slots, db)
    return count > 0


# ── Smart shorts slot dispatcher ───────────────────────────────

def dispatch_next_due_shorts_slot(db=None) -> dict | None:
    """Check for due shorts planned slots and dispatch ONE.

    Called every 5 min by the API checker loop.
    Only dispatches if no job is currently running.

    - For native slots: dispatch immediately
    - For clip slots: check if source long video exists (today, completed)
      If not, skip. If yes, set source_video_id and dispatch.

    Returns:
        dict with dispatched slot info, or None if nothing to do.
    """
    import sqlite3
    from config.settings import DATABASE_PATH

    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    # 1. Sync running slots: mark completed/failed based on short status
    _sync_running_shorts_slots(db)

    # 2. Cancel stale pending slots (>6h past scheduled_at)
    _cancel_stale_shorts_slots(db)

    # 3. Guard: only one SHORT at a time (videos can run concurrently)
    active = db.get_active_shorts_job()
    if active:
        logger.debug("Shorts dispatch skipped: short job #%d is %s", active["id"], active["status"])
        return None

    # 4. Memory gate (shorts use minimal RAM — 1 GB is enough)
    if not _memory_ok(min_free_gb=1.0):
        logger.warning("Low memory — delaying shorts slot dispatch")
        return None

    # 5. Get next pending short slot that is due
    next_slot = db.get_next_pending_shorts_slot()
    if not next_slot:
        logger.debug("No pending shorts slots due")
        return None

    slot_id = next_slot["id"]
    channel_id = next_slot["channel_id"]
    slug = next_slot.get("channel_slug", "")
    short_type = next_slot.get("short_type", "native")
    scheduled = next_slot.get("scheduled_at", "?")

    logger.info(
        "Dispatching shorts slot #%d: %s type=%s (scheduled %s)",
        slot_id, slug, short_type, scheduled,
    )

    # 6. For clip slots: check source video dependency
    source_video_id = None
    if short_type == "clip":
        long_pos = next_slot.get("long_slot_position")
        source_video_id = _resolve_clip_source(channel_id, long_pos)
        if source_video_id is None:
            # No source video available — cancel this slot so it doesn't
            # block the queue. Next cycle picks the next due slot.
            db.update_shorts_slot_status(
                slot_id, "cancelled",
                error_message="No completed source long video available",
            )
            logger.info(
                "Shorts slot #%d cancelled: clip type but no completed source "
                "long video (channel=%s, long_slot=%s)",
                slot_id, slug, long_pos,
            )
            return None

    # 7. Mark slot as running with source_video_id
    db.update_shorts_slot_status(slot_id, "running", source_video_id=source_video_id)

    # 8. Create job record for tracking
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    job_action = "generate_native_short" if short_type == "native" else "generate_clip_short"
    job_id = db.create_job(channel_id, job_action)

    # Mark job as running immediately
    db.update_job(job_id, status="running")

    # Link job to slot
    db.update_shorts_slot_status(slot_id, "running", job_id=job_id,
                                  source_video_id=source_video_id)
    conn.close()

    # 9. Dispatch the actual generation (fire and forget)
    import asyncio
    asyncio.create_task(
        _dispatch_short_async(
            slot_id=slot_id,
            job_id=job_id,
            channel_id=channel_id,
            channel_slug=slug,
            short_type=short_type,
            source_video_id=source_video_id,
        )
    )

    return {
        "slot_id": slot_id,
        "job_id": job_id,
        "channel_slug": slug,
        "short_type": short_type,
    }


def _resolve_clip_source(channel_id: int, long_slot_position) -> int | None:
    """Find the completed long video for a clip short.
    
    long_slot_position 1 = first completed video today
    long_slot_position 2 = second completed video today
    
    Returns video_id or None if not available.
    """
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()

    videos = db.get_completed_videos_today(channel_id)
    if not videos:
        return None

    pos = long_slot_position or 1
    # Map to 0-indexed: position 1 -> index 0, position 2 -> index 1
    idx = pos - 1
    if idx < len(videos):
        return videos[idx]["id"]

    return None


async def _dispatch_short_async(slot_id: int, job_id: int, channel_id: int,
                                 channel_slug: str, short_type: str,
                                 source_video_id: int = None):
    """Async wrapper that dispatches the actual short generation and updates DB."""
    import sqlite3
    from config.settings import DATABASE_PATH

    try:
        if short_type == "native":
            short_id = _dispatch_native_short(channel_id, channel_slug)
        else:
            short_id = _dispatch_clip_short(channel_id, channel_slug, source_video_id)

        if short_id:
            # Mark slot as completed
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            conn.execute(
                "UPDATE shorts_planned_slots SET status = 'completed', short_id = ? WHERE id = ?",
                (short_id, slot_id),
            )
            conn.execute(
                "UPDATE generation_jobs SET status = 'completed' WHERE id = ?",
                (job_id,),
            )
            conn.commit()
            conn.close()
            logger.info("Shorts slot #%d completed: short_id=%d", slot_id, short_id)
        else:
            # Mark slot as cancelled
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            conn.execute(
                "UPDATE shorts_planned_slots SET status = 'cancelled' WHERE id = ?",
                (slot_id,),
            )
            conn.execute(
                "UPDATE generation_jobs SET status = 'failed', error_msg = 'No short_id returned' WHERE id = ?",
                (job_id,),
            )
            conn.commit()
            conn.close()
            logger.warning("Shorts slot #%d returned no short_id", slot_id)
    except Exception as e:
        logger.error("Shorts dispatch error for slot #%d: %s", slot_id, e)
        try:
            conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
            conn.execute(
                "UPDATE shorts_planned_slots SET status = 'cancelled' WHERE id = ?",
                (slot_id,),
            )
            conn.execute(
                "UPDATE generation_jobs SET status = 'failed', error_msg = ? WHERE id = ?",
                (str(e)[:500], job_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass


# ── Native short generation ────────────────────────────────────

def _dispatch_native_short(channel_id: int, channel_slug: str) -> int | None:
    """Generate and publish a native Short.

    Uses the existing native short generation pipeline (LLM script → TTS → render → upload).

    Returns short_id or None on failure.
    """
    import json
    import re
    import subprocess
    import time
    import sqlite3
    from pathlib import Path
    from config.settings import DATABASE_PATH, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, OUTPUT_DIR
    from config.config_bridge import get_channel_config

    ch_config = get_channel_config(channel_slug)
    hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])
    niche = getattr(ch_config, "CANAL_NARRATIVE_STYLE", "documental")
    display_name = getattr(ch_config, "CANAL_DISPLAY_NAME", channel_slug)
    tagline = getattr(ch_config, "CANAL_TAGLINE", "")

    # 1. Script via LLM
    from openai import OpenAI
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": (
            f"Genera un Short viral en español de ~45-50 segundos (~65-80 palabras totales, minimo 50). "
            f"Canal: {display_name} — {niche}. Tagline: {tagline}. "
            f"Usa 5 bloques (hook, desarrollo1, desarrollo2, climax, cierre). "
            f"IMPORTANTE: desarrollo1, desarrollo2 y climax deben tener 2-3 frases cada uno. "
            f"Hook y cierre: 1-2 frases. Minimo 10 palabras por bloque. "
            f"El total debe superar 50 palabras. "
            f"Devuelve SOLO JSON: "
            f'{{"titulo": "...", "hook_text": "frase de gancho 8-12 palabras", '
            f'"bloques": [{{"tipo": "hook", "texto": "1-2 frases"}}, '
            f'{{"tipo": "desarrollo1", "texto": "2-3 frases con contexto y detalle"}}, '
            f'{{"tipo": "desarrollo2", "texto": "2-3 frases con dato impactante especifico"}}, '
            f'{{"tipo": "climax", "texto": "2-3 frases con la consecuencia o revelacion"}}, '
            f'{{"tipo": "cierre", "texto": "1-2 frases cierre + suscribete"}}]}}. '
            f"NADA MAS fuera del JSON."
        )}],
        temperature=0.9, max_tokens=1200,
    )
    content = response.choices[0].message.content
    content = re.sub(r"^```(?:json)?\s*\n", "", content)
    content = re.sub(r"\n```\s*$", "", content).strip()
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        logger.error("No JSON found in LLM response for %s: %s", channel_slug, content[:200])
        return None
    script = json.loads(match.group(0))

    # 1b. Validate script completeness
    from pipeline.shorts_tts import validate_short_script
    errors = validate_short_script(script)
    if errors:
        logger.error("Short script validation failed for %s: %s", channel_slug, errors)
        return None

    title = (script.get("titulo") or script.get("title") or "Short")[:100]
    hook_text = (script.get("hook_text") or "")[:100]
    bloques = script.get("bloques", [])

    # 2. Segmented TTS
    output_dir = OUTPUT_DIR / "videos" / "shorts"
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    audio_path = output_dir / f"sched_audio_{channel_slug}_{ts}.mp3"
    srt_path = output_dir / f"sched_audio_{channel_slug}_{ts}.srt"

    from pipeline.shorts_tts import synthesize_shorts_blocks
    try:
        tts_result = synthesize_shorts_blocks(
            bloques=bloques,
            ch_config=ch_config,
            output_audio_path=audio_path,
            output_srt_path=srt_path,
        )
        audio_duration = tts_result["duration_sec"] + 1.5
    except RuntimeError as e:
        logger.error("Short TTS failed for %s: %s", channel_slug, e)
        return None

    # 3. Fetch portrait images
    portrait_queries = [b.get("texto", "")[:80] for b in bloques]
    portrait_queries = [q for q in portrait_queries if q.strip()]
    if not portrait_queries:
        portrait_queries = [hook_text[:80]]

    from pipeline.shorts_media import fetch_portrait_images, render_slideshow_with_images
    image_paths = []
    try:
        image_paths = fetch_portrait_images(portrait_queries, ch_config, count=4)
    except Exception as e:
        logger.warning("Portrait image fetch failed (will use solid bg): %s", e)

    # 4. Render
    video_path = output_dir / f"sched_short_{channel_slug}_{ts}.mp4"

    color_palette = getattr(ch_config, "COLOR_PALETTE", {})
    def _to_hex(c):
        if isinstance(c, (tuple, list)) and len(c) == 3:
            return f"{int(c[0]):02x}{int(c[1]):02x}{int(c[2]):02x}"
        return str(c).lstrip("#").replace("#", "")
    bg_color = _to_hex(color_palette.get("text_shadow", (10, 10, 26)))

    try:
        render_slideshow_with_images(
            image_paths=image_paths,
            audio_path=audio_path,
            hook_text=hook_text,
            output_path=video_path,
            audio_duration=audio_duration,
            bg_color_hex=bg_color,
            srt_path=srt_path if srt_path.exists() else None,
        )
    except Exception as e:
        logger.warning("Slideshow render failed, falling back to solid bg: %s", e)
        from pipeline.shorts_media import _build_solid_bg_filter
        filter_str = _build_solid_bg_filter(
            bg_color,
            srt_path=srt_path if srt_path.exists() else None,
        )
        render_cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=0x{bg_color}:s=1080x1920:d={audio_duration}:r=30",
            "-i", str(audio_path), "-filter_complex", filter_str,
            "-map", "[v]", "-map", "1:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart",
            str(video_path),
        ]
        result = subprocess.run(render_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.error("FFmpeg render failed for %s: %s", channel_slug, result.stderr[-300:])
            return None

    if not video_path.exists():
        logger.error("Render produced no output file for %s", channel_slug)
        return None

    # 5. Upload
    from pipeline.youtube_uploader import YouTubeUploader
    uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
    if not uploader.authenticate():
        logger.error("YouTube auth failed for %s", channel_slug)
        return None

    # Cross-promotion
    from pipeline.shorts_cross_promote import (
        get_best_longform_link, build_short_description, run_post_publish_promotion,
        should_cross_promote,
    )
    longform_url = None
    if should_cross_promote(ch_config):
        longform_url = get_best_longform_link(channel_id)

    channel_url = getattr(ch_config, "YOUTUBE_CHANNEL_URL", "")
    description = build_short_description(
        hook_text=hook_text,
        hashtags=hashtags,
        longform_url=longform_url,
        channel_url=channel_url,
    )
    result = uploader.upload(
        video_path=video_path,
        title=title[:100],
        description=description[:5000],
        tags=hashtags[:60],
        category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
        privacy="public",
    )

    yt_id = result.get("video_id")
    if not yt_id:
        logger.error("Upload failed for %s: no video ID", channel_slug)
        return None

    # 6. Register in DB
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    cursor = conn.execute(
        """INSERT INTO shorts
           (channel_id, type, title, hook_title, hook_text,
            status, file_path, youtube_id, youtube_url, published_at)
           VALUES (?, 'native', ?, ?, ?, 'published', ?, ?, ?, datetime('now','localtime'))""",
        (channel_id, title, title[:60], hook_text,
         str(video_path), yt_id, result.get("url", "")),
    )
    short_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Post-publish cross-promotion
    run_post_publish_promotion(
        channel_slug=channel_slug,
        short_yt_id=yt_id,
        channel_id=channel_id,
        source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
        channel_config=ch_config,
    )

    logger.info("Scheduled native Short published: %s → %s", title[:40], result.get("url", ""))
    return short_id


# ── Clip short generation ──────────────────────────────────────

def _dispatch_clip_short(channel_id: int, channel_slug: str,
                          source_video_id: int) -> int | None:
    """Extract a clip from a long video, render, and publish.

    Uses the ShortsExtractor pipeline pattern from api/routers/shorts.py
    (extract-and-publish endpoint).

    Returns short_id or None on failure.
    """
    import sqlite3
    import json as _json
    import subprocess
    import tempfile
    import time
    from pathlib import Path
    from config.settings import DATABASE_PATH, OUTPUT_DIR
    from config.config_bridge import get_channel_config

    logger.info("Clip extraction: channel=%s source_video=%d", channel_slug, source_video_id)

    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")

    video = conn.execute(
        """SELECT v.*, c.slug as channel_slug
           FROM videos v
           JOIN channels c ON v.channel_id = c.id
           WHERE v.id = ?""",
        (source_video_id,),
    ).fetchone()

    if not video:
        conn.close()
        logger.error("Source video #%d not found for clip short", source_video_id)
        return None

    video = dict(video)

    # ── Phase 1: Script + blocks ──
    script_text = ""
    bloques = []
    if video.get("script_id"):
        script_row = conn.execute(
            "SELECT guion, bloques_json FROM scripts WHERE id = ?",
            (video["script_id"],),
        ).fetchone()
        if script_row:
            script_text = script_row["guion"] or ""
            try:
                bloques = _json.loads(script_row["bloques_json"] or "[]") if script_row["bloques_json"] else []
            except Exception:
                bloques = []

    if not script_text and bloques:
        script_text = " ".join(b.get("texto", "") for b in bloques if b.get("texto"))

    if not script_text:
        bloques_raw = video.get("title_options") or "{}"
        try:
            fallback = _json.loads(bloques_raw) if isinstance(bloques_raw, str) else {}
        except Exception:
            fallback = {}
        script_text = str(fallback.get("script", "")) or script_text

    if not script_text:
        conn.close()
        logger.error("Source video #%d has no script text", source_video_id)
        return None

    # ── Phase 2: LLM extracts best clip timecodes ──
    # Build approximate word-level timestamps from blocks
    from pipeline.shorts_extractor import ShortsExtractor
    total_duration = video.get("duracion_seg") or 0
    n_blocks = len(bloques) if bloques else 1
    timestamps = []
    for idx, block in enumerate(bloques if bloques else [{"texto": script_text}]):
        texto = block.get("texto", "")
        if not texto:
            continue
        words = texto.split()
        block_start = (idx / n_blocks) * total_duration
        block_end = ((idx + 1) / n_blocks) * total_duration
        word_dur = (block_end - block_start) / max(len(words), 1)
        for wi, word in enumerate(words):
            ts_start = round(block_start + wi * word_dur, 1)
            ts_end = round(ts_start + word_dur, 1)
            timestamps.append({"word": word, "start": ts_start, "end": ts_end})

    # Extract word-level TTS timestamps for subtitle rendering
    tts_word_ts = []
    try:
        td_raw = video.get("timing_data") or "{}"
        td = _json.loads(td_raw) if isinstance(td_raw, str) else td_raw
        tts_word_ts = td.get("phases", {}).get("tts_timestamps", [])
        if not isinstance(tts_word_ts, list):
            tts_word_ts = []
    except Exception:
        pass

    extractor = ShortsExtractor()
    clips = extractor.extract(script_text=script_text, timestamps=timestamps,
                              max_clips=3, min_clips=1)

    conn.close()

    if not clips:
        logger.error("No suitable clip found in video #%d", source_video_id)
        return None

    best_clip = clips[0]

    # ── Phase 3: Find or download source video ──
    source_path, clip_offset = _resolve_source_video(video, best_clip["start_time"],
                                                      best_clip["end_time"])
    if source_path is None:
        logger.error("Cannot access source video file for #%d", source_video_id)
        return None

    if clip_offset > 0:
        original_start = best_clip["start_time"]
        original_end = best_clip["end_time"]
        clip_duration = original_end - original_start
        best_clip["start_time"] = clip_offset
        best_clip["end_time"] = clip_offset + clip_duration

    # ── Phase 4: Render → Upload → Promote ──
    _downloaded_temp = source_path
    from pipeline.shorts_renderer import ShortsRenderer
    renderer = ShortsRenderer()
    output_path = None

    try:
        render_word_ts = tts_word_ts if clip_offset == 0 else None
        output_path = renderer.render(
            source_path, best_clip, word_timestamps=render_word_ts,
        )
        if not output_path or not output_path.exists():
            logger.error("Render produced no output for clip from video #%d", source_video_id)
            return None

        ch_config = get_channel_config(channel_slug)
        hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])

        from pipeline.youtube_uploader import YouTubeUploader
        uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
        if not uploader.authenticate():
            logger.error("YouTube auth failed for %s", channel_slug)
            return None

        title = best_clip.get("hook_title", "Short")[:100]
        hook_text = best_clip.get("hook_text", "")

        from pipeline.shorts_cross_promote import (
            get_best_longform_link, build_short_description, run_post_publish_promotion,
            should_cross_promote,
        )
        longform_url = None
        if should_cross_promote(ch_config):
            longform_url = get_best_longform_link(channel_id, source_video_id=source_video_id)

        channel_url_value = getattr(ch_config, "YOUTUBE_CHANNEL_URL", "")
        description = build_short_description(
            hook_text=hook_text,
            hashtags=hashtags,
            longform_url=longform_url,
            channel_url=channel_url_value,
        )
        result = uploader.upload(
            video_path=output_path,
            title=title,
            description=description[:5000],
            tags=hashtags[:60],
            category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"),
            privacy="public",
        )

        yt_id = result.get("video_id")
        if not yt_id:
            logger.error("Upload failed for clip: no video ID returned")
            return None

        # Register in DB
        conn2 = sqlite3.connect(str(DATABASE_PATH), timeout=30)
        cursor = conn2.execute(
            """INSERT INTO shorts
               (channel_id, source_video_id, type, title, hook_title, hook_text,
                start_time, end_time, status, file_path, youtube_id, youtube_url, published_at)
               VALUES (?, ?, 'clip', ?, ?, ?, ?, ?, 'published', ?, ?, ?, datetime('now','localtime'))""",
            (channel_id, source_video_id, title, title[:60], hook_text,
             best_clip.get("start_time"), best_clip.get("end_time"),
             str(output_path), yt_id, result.get("url", "")),
        )
        short_id = cursor.lastrowid
        conn2.commit()
        conn2.close()

        run_post_publish_promotion(
            channel_slug=channel_slug,
            short_yt_id=yt_id,
            channel_id=channel_id,
            source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
        )

        logger.info("Scheduled clip Short published: %s → %s", title[:40], result.get("url", ""))
        return short_id

    finally:
        if _downloaded_temp and str(_downloaded_temp).startswith("/tmp/"):
            try:
                _downloaded_temp.unlink(missing_ok=True)
            except Exception:
                pass


def _resolve_source_video(video: dict, clip_start: float, clip_end: float):
    """Find or download the source video for clip extraction.
    Returns (Path, offset_seconds) or (None, None)."""
    import subprocess
    import tempfile
    from pathlib import Path

    # 1. Try local file
    if video.get("video_path"):
        for p in [Path(video["video_path"]),
                  Path("/root/autotube") / str(video["video_path"])]:
            if p.exists():
                return p, 0.0

    # 2. Download clip segment from YouTube
    yt_id = video.get("yt_video_id")
    if not yt_id:
        return None, None

    yt_url = video.get("yt_url") or f"https://www.youtube.com/watch?v={yt_id}"
    padding = 3.0
    section_start = max(0, clip_start - padding)
    section_end = clip_end + padding
    section_spec = f"*{section_start:.1f}-{section_end:.1f}"

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["yt-dlp",
             "--download-sections", section_spec,
             "-f", "best[height<=720]/best",
             "--no-playlist",
             "--socket-timeout", "30",
             "--force-overwrites",
             "--no-warnings",
             "-o", tmp_path,
             yt_url],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0 or not Path(tmp_path).exists():
            Path(tmp_path).unlink(missing_ok=True)
            return None, None
        return Path(tmp_path), padding
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        return None, None


# ── Internal helpers ───────────────────────────────────────────

def _sync_running_shorts_slots(db):
    """Check running shorts slots across ALL recent dates: mark completed if their
    short exists, mark failed if their generation job died (e.g. server restart).

    Previously only scanned today's slots, which left server-restart orphans
    stuck in 'running' state indefinitely for previous days.
    """
    today = datetime.now(CEST).date()
    all_running = []
    for offset in range(-7, 1):  # scan last 7 days including today
        date_key = (today + timedelta(days=offset)).isoformat()
        slots = db.get_shorts_planned_slots(date_key=date_key, status="running")
        if slots:
            all_running.extend(slots)

    if not all_running:
        return

    for s in all_running:
        slot_id = s["id"]
        short_id = s.get("short_id")
        job_id = s.get("job_id")

        # Case 1: short exists and is published → mark completed
        if short_id:
            short = db.get_short(short_id)
            if short and short.get("status") == "published":
                db.update_shorts_slot_status(slot_id, "completed")
                logger.info("Shorts slot #%d marked completed", slot_id)
                continue

        # Case 2: linked job failed (server restart, error, etc.) → mark failed
        if job_id:
            job = db.get_job(job_id)
            if job is None:
                # Job record deleted — slot is orphaned
                db.update_shorts_slot_status(
                    slot_id, "failed",
                    error_message="Orphaned: job record missing",
                )
                logger.info("Shorts slot #%d marked failed (job #%d missing)", slot_id, job_id)
            elif job.get("status") == "failed":
                db.update_shorts_slot_status(
                    slot_id, "failed",
                    error_message=f"Job #{job_id} failed: {(job.get('error_msg') or '')[:200]}",
                )
                logger.info("Shorts slot #%d marked failed (job #%d failed)", slot_id, job_id)


def _cancel_stale_shorts_slots(db):
    """Cancel pending shorts slots that are >8h past their scheduled_at (UTC).
    
    Scans ALL dates (not just today) to catch stuck slots from previous days.
    """
    # Fetch pending slots across all recent dates (past 7 days)
    today = datetime.now(CEST).date()
    all_pending = []
    for offset in range(-7, 1):  # from 7 days ago to today
        date_key = (today + timedelta(days=offset)).isoformat()
        pending = db.get_shorts_planned_slots(date_key=date_key, status="pending")
        if pending:
            all_pending.extend(pending)

    if not all_pending:
        return

    now_utc = datetime.now(UTC)
    cancelled = 0
    for s in all_pending:
        try:
            sched = datetime.strptime(s["scheduled_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
        if (now_utc - sched).total_seconds() > 8 * 3600:
            db.update_shorts_slot_status(s["id"], "cancelled")
            cancelled += 1

    if cancelled:
        logger.info("Cancelled %d stale pending shorts slots (>8h past scheduled)", cancelled)


def _memory_ok(min_free_gb: float = 4.0) -> bool:
    """Check if enough RAM is available for dispatch.
    
    Shorts use minimal RAM (1.0 GB), long-form videos need 4.0 GB.
    """
    try:
        from pipeline.ram_governor import available_mb
        avail_mb = available_mb()
        if avail_mb < 0:
            return True  # Can't determine — let it proceed
        min_free_mb = min_free_gb * 1024
        return avail_mb >= min_free_mb
    except ImportError:
        return True
