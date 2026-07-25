# Plan: Multi-Window Upload + Randomized Publish Scheduling

## Overview

Replace single upload window (9-11h) with two time windows (10-13h morning, 20-22h evening).
Uploads are distributed across windows via round-robin at random times.
Publishing uses the existing auto-calculated peak hours but with wider randomization (±90min spread = 3h window).

---

## File 1: Config Files (4 files)

### `config/canal2_config.py` (lines 673-680)

**BEFORE:**
```python
# ── 3-Phase Pipeline (v9) ─────────────────────────────────────────
GENERATION_LEAD_HOURS = 36    # Max hours ahead to generate
UPLOAD_WINDOW_START = 9       # Upload window: 9:00 AM
UPLOAD_WINDOW_END = 11        # Upload window: 11:00 AM
PUBLISH_TIMEZONE = "Europe/Madrid"
PUBLISH_TARGET_HOUR = 21           # 9 PM — peak para contenido de misterio
PUBLISH_JITTER_MIN = 20            # ±20 min de variación aleatoria
PUBLISH_WARMUP_MIN = 120           # Mínimo 2h en 'private' antes de publicar
```

**AFTER:**
```python
# ── 3-Phase Pipeline (v9) ─────────────────────────────────────────
GENERATION_LEAD_HOURS = 36    # Max hours ahead to generate
# Upload windows (franjas de subida): videos suben en estas franjas a horas random
UPLOAD_WINDOWS = [
    {"start": 10, "end": 13},   # Mañana: 10:00-13:00
    {"start": 20, "end": 22},   # Tarde: 20:00-22:00
]
PUBLISH_TIMEZONE = "Europe/Madrid"
PUBLISH_TARGET_HOUR = 21           # 9 PM — peak para contenido de misterio
PUBLISH_JITTER_MIN = 20            # ±20 min de variación aleatoria (legacy)
PUBLISH_WARMUP_MIN = 120           # Mínimo 2h en 'private' antes de publicar
PUBLISH_WINDOW_SPREAD_MIN = 90     # ±90min alrededor del peak = ventana de publicación de 3h
```

### `config/canal3_config.py` (lines 696-703)

**BEFORE:**
```python
# ── 3-Phase Pipeline (v9) ─────────────────────────────────────────
GENERATION_LEAD_HOURS = 36    # Max hours ahead to generate
UPLOAD_WINDOW_START = 9       # Upload window: 9:00 AM
UPLOAD_WINDOW_END = 11        # Upload window: 11:00 AM
PUBLISH_TIMEZONE = "Europe/Madrid"
PUBLISH_JITTER_MIN = 20
PUBLISH_WARMUP_MIN = 120
# PUBLISH_TARGET_HOUR not set — niche heuristic auto-detects (historia_documental → 20:00)
```

**AFTER:**
```python
# ── 3-Phase Pipeline (v9) ─────────────────────────────────────────
GENERATION_LEAD_HOURS = 36    # Max hours ahead to generate
# Upload windows (franjas de subida): videos suben en estas franjas a horas random
UPLOAD_WINDOWS = [
    {"start": 10, "end": 13},   # Mañana: 10:00-13:00
    {"start": 20, "end": 22},   # Tarde: 20:00-22:00
]
PUBLISH_TIMEZONE = "Europe/Madrid"
PUBLISH_JITTER_MIN = 20            # ±20 min de variación aleatoria (legacy)
PUBLISH_WARMUP_MIN = 120
PUBLISH_WINDOW_SPREAD_MIN = 90     # ±90min alrededor del peak = ventana de publicación de 3h
# PUBLISH_TARGET_HOUR not set — niche heuristic auto-detects (historia_documental → 20:00)
```

### `config/canal4_config.py` (lines 725-732)

**BEFORE:**
```python
# ── 3-Phase Pipeline (v9) ─────────────────────────────────────────
GENERATION_LEAD_HOURS = 36    # Max hours ahead to generate
UPLOAD_WINDOW_START = 9       # Upload window: 9:00 AM
UPLOAD_WINDOW_END = 11        # Upload window: 11:00 AM
PUBLISH_TIMEZONE = "Europe/Madrid"
PUBLISH_JITTER_MIN = 20
PUBLISH_WARMUP_MIN = 120
# PUBLISH_TARGET_HOUR not set — niche heuristic auto-detects (historia_documental → 20:00)
```

**AFTER:**
```python
# ── 3-Phase Pipeline (v9) ─────────────────────────────────────────
GENERATION_LEAD_HOURS = 36    # Max hours ahead to generate
# Upload windows (franjas de subida): videos suben en estas franjas a horas random
UPLOAD_WINDOWS = [
    {"start": 10, "end": 13},   # Mañana: 10:00-13:00
    {"start": 20, "end": 22},   # Tarde: 20:00-22:00
]
PUBLISH_TIMEZONE = "Europe/Madrid"
PUBLISH_JITTER_MIN = 20            # ±20 min de variación aleatoria (legacy)
PUBLISH_WARMUP_MIN = 120
PUBLISH_WINDOW_SPREAD_MIN = 90     # ±90min alrededor del peak = ventana de publicación de 3h
# PUBLISH_TARGET_HOUR not set — niche heuristic auto-detects (historia_documental → 20:00)
```

### `config/canal5_config.py` (lines 703-706)

**BEFORE:**
```python
# ── 3-Phase Pipeline (v9) ─────────────────────────────────────────
GENERATION_LEAD_HOURS = 36    # Max hours ahead to generate (1.5 days)
UPLOAD_WINDOW_START = 9       # Upload window: 9:00 AM
UPLOAD_WINDOW_END = 11       # Upload window: 11:00 AM
```

**AFTER:**
```python
# ── 3-Phase Pipeline (v9) ─────────────────────────────────────────
GENERATION_LEAD_HOURS = 36    # Max hours ahead to generate (1.5 days)
# Upload windows (franjas de subida): videos suben en estas franjas a horas random
UPLOAD_WINDOWS = [
    {"start": 10, "end": 13},   # Mañana: 10:00-13:00
    {"start": 20, "end": 22},   # Tarde: 20:00-22:00
]
PUBLISH_WINDOW_SPREAD_MIN = 90     # ±90min alrededor del peak = ventana de publicación de 3h
```

---

## File 2: Database Migration + db_extended.py

### DB Migration

Add to the idempotent migration in `database/db_extended.py` `migrate_v2()` function (or `migrate_v3()`):

```python
# v11: scheduled_upload_at for randomized upload dispatch
try:
    conn.execute("ALTER TABLE videos ADD COLUMN scheduled_upload_at TEXT")
    logger.info("[migration] Added scheduled_upload_at column to videos")
except sqlite3.OperationalError:
    pass  # Column already exists
```

### `database/db_extended.py` — `get_channel_planning_config()` (~line 3176)

**BEFORE:**
```python
result = {
    "upload_window_start": int(cfg.get("UPLOAD_WINDOW_START", 9)),
    "upload_window_end": int(cfg.get("UPLOAD_WINDOW_END", 11)),
    ...
}
```

**AFTER:**
```python
# Support new UPLOAD_WINDOWS list, backward compat with old UPLOAD_WINDOW_START/END
upload_windows = cfg.get("UPLOAD_WINDOWS")
if not upload_windows:
    # Backward compat: build single-window list from old keys
    ws = int(cfg.get("UPLOAD_WINDOW_START", 9))
    we = int(cfg.get("UPLOAD_WINDOW_END", 11))
    upload_windows = [{"start": ws, "end": we}]
result = {
    "upload_windows": upload_windows,
    "upload_window_start": upload_windows[0]["start"],  # compat
    "upload_window_end": upload_windows[0]["end"],       # compat
    ...
}
```

### `database/db_extended.py` — `update_channel_planning_config()` (~line 3205)

Support `UPLOAD_WINDOWS` (list) and `PUBLISH_WINDOW_SPREAD_MIN` (int) alongside existing single-int fields.

### `database/db_extended.py` — `update_video()` helper

Ensure it supports the new `scheduled_upload_at` parameter:
```python
def update_video(self, video_id, scheduled_upload_at=None, **kwargs):
    # Add scheduled_upload_at to the SET clause if provided
    ...
```

---

## File 3: `api/services/upload_scheduler.py` — Major Rewrite

**FULL REPLACEMENT** of `dispatch_due_uploads()`:

```python
"""Upload Scheduler — Phase 2 of the 3-phase pipeline.

Dispatches upload jobs for videos that have been generated locally (F1)
and are awaiting upload (F2). Uploads happen within each channel's
configured upload windows (UPLOAD_WINDOWS list).

Upload windows are multi-window: morning (10-13h) and evening (20-22h).
Videos are distributed across windows via round-robin at random times
per day to avoid bot-like patterns.

Architecture:
  dispatch_due_uploads(db) → checks for awaiting_upload videos,
  computes random upload times within windows (round-robin),
  dispatches upload_only jobs when scheduled_upload_at arrives.
  Respects per-channel concurrency and global upload limit.

Called every 5 min by the checker loop in api/main.py.
"""

import json
import logging
import random
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("autotube.upload_scheduler")

MAX_CONCURRENT_UPLOADS = 1

# Round-robin state: {(channel_id, date_str): last_window_index}
_windows_rr: dict[tuple[int, str], int] = {}


def _parse_upload_windows(ch_cfg: dict) -> list[dict]:
    """Parse upload windows from channel config. Backward compat with old format."""
    windows = ch_cfg.get("UPLOAD_WINDOWS")
    if windows and isinstance(windows, list) and len(windows) > 0:
        # Validate structure
        valid = []
        for w in windows:
            if isinstance(w, dict) and "start" in w and "end" in w:
                valid.append({"start": int(w["start"]), "end": int(w["end"])})
        if valid:
            return valid
    # Backward compat: old UPLOAD_WINDOW_START / UPLOAD_WINDOW_END
    ws = int(ch_cfg.get("UPLOAD_WINDOW_START", 9))
    we = int(ch_cfg.get("UPLOAD_WINDOW_END", 11))
    return [{"start": ws, "end": we}]


def _is_in_any_window(now_hour: int, windows: list[dict]) -> bool:
    """Check if current hour falls within any upload window."""
    for w in windows:
        if w["start"] <= now_hour < w["end"]:
            return True
    return False


def _compute_random_upload_time(windows: list[dict], now: datetime,
                                 channel_id: int) -> datetime | None:
    """Pick next window via round-robin and compute random upload time within it.

    Returns the scheduled upload datetime, or None if no window is available today.
    """
    today_str = now.date().isoformat()
    rr_key = (channel_id, today_str)

    # Get next window index via round-robin
    last_idx = _windows_rr.get(rr_key, -1)
    next_idx = (last_idx + 1) % len(windows)
    _windows_rr[rr_key] = next_idx
    chosen = windows[next_idx]

    # Check if this window is still available today
    window_start_dt = now.replace(hour=chosen["start"], minute=0, second=0, microsecond=0)
    window_end_dt = now.replace(hour=chosen["end"], minute=0, second=0, microsecond=0)

    if now >= window_end_dt:
        # This window already passed today. Try the next window.
        for offset in range(1, len(windows)):
            alt_idx = (next_idx + offset) % len(windows)
            alt = windows[alt_idx]
            alt_start = now.replace(hour=alt["start"], minute=0, second=0, microsecond=0)
            alt_end = now.replace(hour=alt["end"], minute=0, second=0, microsecond=0)
            if now < alt_end:
                chosen = alt
                next_idx = alt_idx
                _windows_rr[rr_key] = next_idx
                window_start_dt = alt_start
                window_end_dt = alt_end
                break
        else:
            # No window available today, try tomorrow's first window
            tomorrow = now + timedelta(days=1)
            first_win = windows[0]
            window_start_dt = tomorrow.replace(hour=first_win["start"], minute=0, second=0, microsecond=0)
            window_end_dt = tomorrow.replace(hour=first_win["end"], minute=0, second=0, microsecond=0)
            _windows_rr[rr_key] = 0

    if now < window_start_dt:
        # Window hasn't started yet — use window_start as minimum
        earliest = window_start_dt
    else:
        earliest = now

    # Random minute within remaining window
    remaining_seconds = int((window_end_dt - earliest).total_seconds())
    if remaining_seconds <= 0:
        return None

    delay = random.randint(0, remaining_seconds)
    return earliest + timedelta(seconds=delay)


def dispatch_due_uploads(db=None) -> dict | None:
    """Check for awaiting_upload videos and dispatch upload jobs.

    Videos are dispatched at randomized times within their channel's
    upload windows, distributed via round-robin to avoid patterns.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    # 1. Count active upload jobs
    active_uploads = db.count_active_upload_jobs()
    if active_uploads >= MAX_CONCURRENT_UPLOADS:
        logger.info("Upload scheduler: %d upload(s) activos (max=%d) — no se despachan más",
                    active_uploads, MAX_CONCURRENT_UPLOADS)
        return None

    # 2. Find videos awaiting upload that are due (or need scheduling)
    now = datetime.now()

    with db._connect() as conn:
        rows = conn.execute(
            """SELECT v.id, v.channel_id, v.canal, v.video_path, v.thumbnail_path,
                      v.titulo_final, v.description, v.tags_json, v.target_public_at,
                      v.scheduled_upload_at, c.slug as channel_slug, c.config_json
               FROM videos v
               JOIN channels c ON v.channel_id = c.id
               WHERE v.status = 'awaiting_upload'
                 AND v.video_path IS NOT NULL
                 AND v.video_path != ''
                 AND (v.scheduled_upload_at IS NULL
                      OR v.scheduled_upload_at <= datetime('now'))
               ORDER BY v.scheduled_upload_at ASC, v.created_at ASC
               LIMIT 20"""
        ).fetchall()

    if not rows:
        return None

    # 3. Process candidates
    eligible = []
    for row in rows:
        ch_cfg = {}
        try:
            ch_cfg = json.loads(row["config_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            pass

        windows = _parse_upload_windows(ch_cfg)
        channel_id = row["channel_id"]

        # Check if scheduled_upload_at needs to be set (first time seeing this video)
        sched_at_str = row.get("scheduled_upload_at")
        if sched_at_str is None:
            sched_time = _compute_random_upload_time(windows, now, channel_id)
            if sched_time is None:
                continue  # No window available today
            # Store in DB
            try:
                db.update_video(row["id"], scheduled_upload_at=sched_time.isoformat())
            except Exception:
                pass
            if sched_time > now:
                logger.debug("Video %d: scheduled upload at %s, waiting...",
                           row["id"], sched_time.strftime("%H:%M"))
                continue
            # sched_time <= now — ready to dispatch

        # Past-due check (target_public_at already passed)
        past_due = False
        target_public = row["target_public_at"]
        if target_public:
            try:
                pub_dt = datetime.strptime(str(target_public), "%Y-%m-%d %H:%M:%S")
                if pub_dt < now:
                    past_due = True
            except (ValueError, TypeError):
                try:
                    pub_dt = datetime.fromisoformat(str(target_public).replace("Z", "+00:00"))
                    if pub_dt < now:
                        past_due = True
                except (ValueError, TypeError):
                    pass

        # Check if we're in a valid upload window (or past-due)
        current_hour = now.hour
        if past_due or _is_in_any_window(current_hour, windows):
            eligible.append({"row": dict(row), "past_due": past_due})

    if not eligible:
        return None

    # Sort: past-due first, then by scheduled_upload_at / created_at
    eligible.sort(key=lambda v: (not v["past_due"], v["row"].get("created_at", "")))

    # 4. Dispatch the first eligible video
    entry = eligible[0]
    video = entry["row"]
    video_id = video["id"]
    channel_id = video["channel_id"]
    slug = video.get("channel_slug", video.get("canal", "unknown"))

    # Per-channel guard
    active_for_channel = db.get_active_upload_job_for_channel(channel_id)
    if active_for_channel:
        logger.debug("Upload for %s deferred: channel already has active upload", slug)
        return None

    # Verify file exists
    vp = Path(video["video_path"]) if video.get("video_path") else None
    if not vp or not vp.exists():
        logger.warning("Video %d: file missing (%s) — marking as error", video_id, video.get("video_path"))
        db.update_video(video_id, status="error", progress_phase="upload")
        return None

    logger.info("Upload dispatching: video #%d (%s), file=%s | public scheduled: %s",
                video_id, slug, vp.name,
                (str(video.get("target_public_at") or "?")[:19] if video.get("target_public_at") else "IMMEDIATE"))

    # 5. Create upload job and dispatch
    import asyncio
    from api.services.generation_service import start_upload_job_from_scheduler

    job_id = db.create_job(channel_id, "upload_only", video_id)
    db.update_job(job_id, status="running")
    db.update_video(video_id, status="uploading", progress=5, progress_phase="upload",
                    scheduled_upload_at=None)  # Clear scheduled time after dispatch

    # Log to scheduled_publish log
    try:
        from api.services.scheduled_publish_logger import log_publish_event
        log_publish_event(
            event="upload_dispatched",
            slug=slug,
            video_id=video_id,
            target_public_at=str(video.get("target_public_at", "") or "IMMEDIATE")[:19],
            job_id=job_id,
        )
    except Exception:
        pass

    asyncio.create_task(
        start_upload_job_from_scheduler(
            job_id=job_id,
            video_id=video_id,
            channel_id=channel_id,
        )
    )

    return {
        "video_id": video_id,
        "job_id": job_id,
        "channel_slug": slug,
        "target_public_at": video.get("target_public_at"),
    }


def get_active_upload_count(db=None) -> int:
    """Count currently running upload_only jobs."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    return db.count_active_upload_jobs()
```

---

## File 4: `pipeline/publish_scheduler.py`

### Change in `calculate_target_public_time()` (lines 374-377)

**BEFORE:**
```python
    # ── 3. Aplicar jitter aleatorio ──
    jitter = random.randint(-jitter_min, jitter_min)
    effective_hour = seed_hour + (jitter / 60.0)  # hora decimal
```

**AFTER:**
```python
    # ── 3. Aplicar spread aleatorio dentro de la ventana de publicación ──
    # PUBLISH_WINDOW_SPREAD_MIN define ±minutos alrededor del peak
    # (ej. 90 min = ventana de 3h). Si no existe, usa PUBLISH_JITTER_MIN (legacy).
    spread = jitter_min  # jitter_min ahora representa el spread (backward compat)
    jitter = random.randint(-spread, spread)
    effective_hour = seed_hour + (jitter / 60.0)  # hora decimal
```

Note: The function already receives `jitter_min` as parameter. The orchestrator will pass the spread value. The variable rename is cosmetic.

### Also update `get_channel_peak_info()` to include spread:

Add to the return dict at ~line 178:
```python
    return {
        "peak_hour": peak,
        "secondary_peaks": secondary,
        "jitter_min": jitter,
        "spread_min": jitter,  # alias for PUBLISH_WINDOW_SPREAD_MIN
        "timezone": tz_str,
        "warmup_min": warmup,
        "source": source,
        "niche": niche,
    }
```

---

## File 5: `orchestrator.py` (lines 1258-1261)

**BEFORE:**
```python
                primary_kw = getattr(self.config, "SEO_PRIMARY_KEYWORD", "")
                secondary_kws = getattr(self.config, "SEO_SECONDARY_KEYWORDS", [])
                tz = getattr(self.config, "PUBLISH_TIMEZONE", "Europe/Madrid")
                target_h = getattr(self.config, "PUBLISH_TARGET_HOUR", None)
                jitter = getattr(self.config, "PUBLISH_JITTER_MIN", 20)
                warmup = getattr(self.config, "PUBLISH_WARMUP_MIN", 120)
```

**AFTER:**
```python
                primary_kw = getattr(self.config, "SEO_PRIMARY_KEYWORD", "")
                secondary_kws = getattr(self.config, "SEO_SECONDARY_KEYWORDS", [])
                tz = getattr(self.config, "PUBLISH_TIMEZONE", "Europe/Madrid")
                target_h = getattr(self.config, "PUBLISH_TARGET_HOUR", None)
                # PUBLISH_WINDOW_SPREAD_MIN (new) > PUBLISH_JITTER_MIN (legacy)
                spread = getattr(self.config, "PUBLISH_WINDOW_SPREAD_MIN", None)
                if spread is None:
                    spread = getattr(self.config, "PUBLISH_JITTER_MIN", 20)
                warmup = getattr(self.config, "PUBLISH_WARMUP_MIN", 120)
```

And update the call to `calculate_target_public_time()`:
```python
                publish_schedule_info = calculate_target_public_time(
                    slug=self.canal,
                    primary_keyword=primary_kw,
                    secondary_keywords=secondary_kws,
                    timezone_str=tz,
                    target_hour=target_h,
                    jitter_min=spread,    # was: jitter_min=jitter
                    warmup_min=warmup,
                    db=self.db,
                    channel_id=channel_id,
                )
```

---

## File 6: `api/services/planning_service.py`

### `_pick_upload_minute()` (lines 441-463)

**BEFORE:** Takes single `(window_start, window_end)` as ints.

**AFTER:** Takes `windows` as list of dicts:

```python
def _pick_upload_minute(windows: list[dict], day_ordinal: int = 0,
                         slot_index: int = 0, seed_value: int = 0) -> datetime.time:
    """Pick a random minute within one of the upload windows using round-robin.
    
    Args:
        windows: [{"start": 10, "end": 13}, {"start": 20, "end": 22}]
        day_ordinal: day number for rotation
        slot_index: index of this slot within the day
        seed_value: ensures deterministic jitter per slot
    
    Returns:
        datetime.time for the scheduled upload.
    """
    import random as _random_pum
    
    if not windows:
        return datetime.time(9, 0)  # fallback
    
    # Round-robin across windows
    rr_index = (day_ordinal + slot_index) % len(windows)
    chosen = windows[rr_index % len(windows)]
    
    ws = chosen["start"]
    we = chosen["end"]
    
    window_minutes = (we - ws) * 60
    rng = _random_pum.Random(seed_value + slot_index * 997 + day_ordinal * 7919)
    minute_offset = rng.randint(15, max(15, window_minutes - 15))
    
    total_minutes = ws * 60 + minute_offset
    hour = total_minutes // 60
    minute = total_minutes % 60
    return datetime.time(hour, minute)
```

### `compute_horizon_slots()` (~line 525-534)

Change how upload windows are read:
```python
# BEFORE:
upload_win_start = ch.get("upload_window_start", 9)
upload_win_end = ch.get("upload_window_end", 11)
# ...
target_upload_at = _pick_upload_minute(upload_win_start, upload_win_end, ...)

# AFTER:
upload_windows = ch.get("upload_windows", [{"start": 9, "end": 11}])
# ...
target_upload_at = _pick_upload_minute(upload_windows, day_ordinal=..., slot_index=...)
```

---

## File 7: `api/services/recovery_planner.py`

### `auto_recover_missing_publications()` — add window awareness

When creating recovery slots, ensure `target_upload_at` falls within at least one of the channel's `UPLOAD_WINDOWS`. If the computed upload time falls outside all windows, snap it to the nearest available window.

```python
# In the recovery slot creation logic (~line 200):
upload_windows = ch_cfg.get("upload_windows") or ch_cfg.get("UPLOAD_WINDOWS")
if not upload_windows:
    ws = ch_cfg.get("upload_window_start", ch_cfg.get("UPLOAD_WINDOW_START", 9))
    we = ch_cfg.get("upload_window_end", ch_cfg.get("UPLOAD_WINDOW_END", 11))
    upload_windows = [{"start": ws, "end": we}]

# Check if target_upload_at falls within any window
upload_hour = target_upload_at.hour
in_window = any(w["start"] <= upload_hour < w["end"] for w in upload_windows)
if not in_window:
    # Snap to first available window
    target_upload_at = target_upload_at.replace(
        hour=upload_windows[0]["start"], minute=0
    )
```

---

## File 8: `api/routers/planning.py`

### `PlanningConfigUpdate` model — add new fields:

```python
class PlanningConfigUpdate(BaseModel):
    videos_per_day: Optional[int] = None
    planning_enabled: Optional[bool] = None
    viral_per_day: Optional[int] = None
    # NEW:
    upload_windows: Optional[list[dict]] = None      # [{"start":10,"end":13},{"start":20,"end":22}]
    publish_window_spread_min: Optional[int] = None   # ±minutes around peak (default 90)
```

### `get_channel_planning_config` response — include new fields:

```python
result = {
    ...
    "upload_windows": upload_windows,
    "publish_window_spread_min": int(cfg.get("PUBLISH_WINDOW_SPREAD_MIN", 90)),
}
```

### `update_channel_planning_config` endpoint — handle new fields:

```python
if update.upload_windows is not None:
    safe["UPLOAD_WINDOWS"] = update.upload_windows
if update.publish_window_spread_min is not None:
    safe["PUBLISH_WINDOW_SPREAD_MIN"] = update.publish_window_spread_min
```

---

## File 9: `config/config_bridge.py`

**No changes needed.** The bridge already handles `list` and `dict` types through its serialization check (`isinstance(value, (dict, list, str, int, float, bool, tuple, type(None)))`). `UPLOAD_WINDOWS` (list of dicts) and `PUBLISH_WINDOW_SPREAD_MIN` (int) are both compatible.

---

## File 10: `database/db_extended.py` — `update_video()` support

Ensure the `update_video` method accepts and handles `scheduled_upload_at`:

```python
def update_video(self, video_id: int, **kwargs) -> bool:
    # ... existing logic ...
    if "scheduled_upload_at" in kwargs:
        updates.append("scheduled_upload_at = ?")
        params.append(kwargs["scheduled_upload_at"])
    # ... rest of method ...
```

---

## Summary of Changes

| File | Lines Changed | Type |
|------|--------------|------|
| `config/canal2_config.py` | 3 lines | Replace config keys |
| `config/canal3_config.py` | 3 lines | Replace config keys |
| `config/canal4_config.py` | 3 lines | Replace config keys |
| `config/canal5_config.py` | 2 lines | Replace config keys |
| `database/db_extended.py` | ~15 lines | Migration + config helpers |
| `api/services/upload_scheduler.py` | ~120 lines | Full rewrite of dispatch |
| `pipeline/publish_scheduler.py` | ~5 lines | Spread instead of jitter |
| `orchestrator.py` | ~5 lines | Pass spread config |
| `api/services/planning_service.py` | ~15 lines | Multi-window upload |
| `api/services/recovery_planner.py` | ~10 lines | Window awareness |
| `api/routers/planning.py` | ~15 lines | API schema + endpoints |

**Total: ~10 files, ~200 lines changed (mostly upload_scheduler.py rewrite)**
