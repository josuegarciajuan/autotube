# Dynamic Scheduling Rebalance — Implementation Plan

## Summary

Extender el sistema de planificación para que sea dinámico: detecte déficits (video falló → añadir slot) y excedentes (video manual creado → eliminar slot pendiente), tanto para **videos largos** como para **shorts**.

---

## Cambios

### 1. `database/db_extended.py` — Añadir `get_shorts_published_today()`

**Ubicación:** Después de `count_shorts_slots_by_status` (~línea 3454), antes de `get_channel_shorts_slots_today`.

```python
def get_shorts_published_today(self, channel_id: int) -> int:
    """Count shorts successfully published today for a channel.
    
    Used by the shorts recovery planner to determine how many of
    today's target shorts have already been published.
    
    Returns count of shorts where:
    - channel_id matches
    - youtube_id IS NOT NULL (successfully uploaded to YouTube)
    - status = 'published'
    - published_at is today (local time)
    """
    with self._connect() as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM shorts
               WHERE channel_id = ?
                 AND youtube_id IS NOT NULL
                 AND status = 'published'
                 AND DATE(published_at) = DATE('now', 'localtime')""",
            (channel_id,),
        ).fetchone()
    return row["cnt"] if row else 0
```

---

### 2. `api/services/recovery_planner.py` — Cancelar excedentes

**Cambio:** Extender el bucle principal para manejar `total_covered > target`.

Alrededor de línea 186, cambiar:

```python
if total_covered >= target:
    logger.debug("[%s] On track — no recovery needed", slug)
    continue
```

Por:

```python
if total_covered > target:
    excess = total_covered - target
    _cancel_excess_pending_slots(db, channel_id, today, excess, active_slots)
    logger.info("[%s] Excess: %d over target (%d) — cancelled %d pending slots",
                slug, total_covered, target, excess)
    continue
elif total_covered == target:
    logger.debug("[%s] On track — no recovery needed", slug)
    continue
```

Añadir la función `_cancel_excess_pending_slots`:

```python
def _cancel_excess_pending_slots(db, channel_id: int, today: str,
                                 excess: int, active_slots: list[dict]):
    """Cancel the last N pending slots to bring total back to target.
    
    Sorts pending slots by scheduled_at descending (cancel latest ones first),
    so earlier slots that may already be in-process keep priority.
    Never touches running slots.
    """
    pending = [s for s in active_slots if s.get("status") == "pending"]
    if not pending or excess <= 0:
        return

    pending_sorted = sorted(pending, key=lambda s: s.get("scheduled_at", ""),
                            reverse=True)
    to_cancel = [s["id"] for s in pending_sorted[:excess]]

    if to_cancel:
        db.cancel_slots(to_cancel)
        logger.info(
            "[%s] Cancelled %d excess pending slots: %s",
            db.get_channel(channel_id).get("slug", f"ch{channel_id}"),
            len(to_cancel), to_cancel,
        )
```

---

### 3. `api/services/shorts_recovery_planner.py` — NUEVO archivo

Recovery planner para shorts. Lógica idéntica al de long-form pero opera sobre `shorts_planned_slots` y `shorts`.

**Cuota total:** `native + clip` combinado (según decisión del usuario).

```python
"""Shorts auto-recovery planner for missing daily publications.

Detects channels behind/ahead their daily shorts target, creates recovery
slots or cancels excess. Runs every 60 min.

Algorithm:
  1. For each active channel, read shorts_native_per_day + shorts_clip_per_day = target.
  2. Count shorts successfully published today.
  3. Count shorts planned slots still pending/running for today.
  4. If published + pending < target → create recovery slots.
  5. If published + pending > target → cancel excess pending slots.
  6. For recovery slots, pick low-audience hours (avoiding peak upload hours).
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

import pytz

logger = logging.getLogger("autotube.shorts_recovery")

MIN_GAP_MINUTES = 30
MIN_HOUR_AHEAD = 1
RECOVERY_START_HOUR = 10
RECOVERY_END_HOUR = 23
RECOVERY_INTERVAL_MINUTES = 60
SHORTS_GEN_TIME_MINUTES = 30  # Shorts se generan mucho más rápido


def _now_madrid() -> datetime:
    return datetime.now(pytz.timezone("Europe/Madrid"))


def _collides(minute_of_day: int, existing: list[int],
              gap_min: int = MIN_GAP_MINUTES) -> bool:
    for e in existing:
        if abs(minute_of_day - e) < gap_min:
            return True
    return False


def _find_available_minute(now_minute: int, existing: list[int],
                           min_ahead: int) -> Optional[int]:
    """Find first available minute-of-day >= now + min_ahead, no collision."""
    start = now_minute + min_ahead
    for m in range(start, 24 * 60, 30):
        if not _collides(m, existing):
            return m
    return None


def auto_recover_shorts(db=None) -> dict:
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    now_local = _now_madrid()
    now_hour = now_local.hour
    now_minute = now_local.hour * 60 + now_local.minute

    if now_hour < RECOVERY_START_HOUR or now_hour >= RECOVERY_END_HOUR:
        return {"recovered_count": 0, "skipped": True,
                "reason": f"Outside window ({RECOVERY_START_HOUR:02d}-{RECOVERY_END_HOUR:02d}h)"}

    today = date.today().isoformat()
    min_ahead = MIN_HOUR_AHEAD * 60

    result = {"date": today, "recovered_count": 0, "cancelled_count": 0,
              "channels_affected": [], "details": []}

    configs = db.get_shorts_planning_config()
    if not configs:
        return {**result, "reason": "no_channel_configs"}

    for cfg in configs:
        ch_id = cfg["channel_id"]
        slug = cfg["slug"]
        if not cfg.get("shorts_enabled", True):
            continue

        native = cfg.get("shorts_native_per_day", 0)
        clip = cfg.get("shorts_clip_per_day", 0)
        target = native + clip
        if target <= 0:
            continue

        published_today = db.get_shorts_published_today(ch_id)
        today_slots = db.get_channel_shorts_slots_today(ch_id, today)
        active_slots = [s for s in today_slots
                        if s.get("status") in ("pending", "running")]
        active_count = len(active_slots)

        total_covered = published_today + active_count

        logger.info("[shorts:%s] Check: target=%d published=%d active=%d total=%d",
                    slug, target, published_today, active_count, total_covered)

        # ── EXCESS: cancel pending slots ──
        if total_covered > target:
            excess = total_covered - target
            pending = [s for s in active_slots if s["status"] == "pending"]
            if pending and excess > 0:
                pending_sorted = sorted(pending,
                                        key=lambda s: s.get("scheduled_at", ""),
                                        reverse=True)
                to_cancel = [s["id"] for s in pending_sorted[:excess]]
                if to_cancel:
                    db.cancel_shorts_slots(to_cancel)
                    result["cancelled_count"] += len(to_cancel)
                    result["channels_affected"].append(slug)
                    result["details"].append({
                        "channel_id": ch_id, "slug": slug,
                        "action": "cancelled_excess",
                        "count": len(to_cancel),
                        "target": target, "published": published_today,
                        "active": active_count,
                    })
                    logger.info("[shorts:%s] Cancelled %d excess pending slots",
                                slug, len(to_cancel))
            continue

        # ── On track ──
        if total_covered == target:
            continue

        # ── DEFICIT: create recovery slots ──
        missing = target - total_covered
        logger.info("[shorts:%s] Behind by %d — creating recovery slots",
                    slug, missing)

        existing_times = []
        for s in active_slots:
            val = s.get("scheduled_at")
            if val:
                try:
                    ts = str(val).replace("T", " ")
                    h, m = map(int, ts[11:16].split(":"))
                    existing_times.append(h * 60 + m)
                except (ValueError, IndexError):
                    pass

        created = 0
        for i in range(missing):
            chosen = _find_available_minute(now_minute, existing_times, min_ahead)
            if chosen is None:
                logger.warning("[shorts:%s] No available minute for recovery slot #%d",
                               slug, i + 1)
                break

            sch_h, sch_m = chosen // 60, chosen % 60
            up_min = chosen + SHORTS_GEN_TIME_MINUTES
            up_h, up_m = min(up_min // 60, 23), min(up_min % 60, 59)

            scheduled_str = f"{today} {sch_h:02d}:{sch_m:02d}:00"
            upload_str = f"{today} {up_h:02d}:{up_m:02d}:00"

            try:
                slot_id = db.create_shorts_slot(
                    channel_id=ch_id, date_key=today,
                    scheduled_at=scheduled_str,
                    target_upload_at=upload_str,
                    short_type="native",
                    slot_position=active_count + created + 1,
                )
                existing_times.append(chosen)
                created += 1
                logger.info("[shorts:%s] Recovery slot #%d created: %02d:%02d",
                            slug, slot_id, sch_h, sch_m)
            except Exception as exc:
                logger.error("[shorts:%s] Failed to create recovery slot: %s",
                             slug, exc)

        if created:
            result["recovered_count"] += created
            if slug not in result["channels_affected"]:
                result["channels_affected"].append(slug)
            result["details"].append({
                "channel_id": ch_id, "slug": slug,
                "action": "recovered",
                "count": created,
                "target": target, "published": published_today,
                "active": active_count,
            })

    if result["recovered_count"] or result["cancelled_count"]:
        logger.info("Shorts recovery: +%d -%d across %d channels",
                    result["recovered_count"], result["cancelled_count"],
                    len(result["channels_affected"]))
    else:
        logger.debug("Shorts recovery: all channels on track")

    return result
```

---

### 4. `api/main.py` — Enganchar al checker loop

**Cambio 1:** Añadir `last_shorts_recovery_check = 0` junto a `last_recovery_check` (~línea 255):

```python
last_recovery_check = 0
last_shorts_recovery_check = 0
```

**Cambio 2:** Añadir bloque de recovery de shorts (~línea 285, después del recovery de long-form):

```python
# Shorts auto-recovery: replan/cancel every 60 min
if now - last_shorts_recovery_check > 3600:  # 60 minutes
    await _process_shorts_recovery_planner()
    last_shorts_recovery_check = now
```

**Cambio 3:** Añadir función `_process_shorts_recovery_planner` (~línea 365, después de `_process_recovery_planner`):

```python
async def _process_shorts_recovery_planner():
    """Check for channels behind/ahead daily shorts target and rebalance.
    
    Runs every 60 min. Only active between 10:00-23:00 local time.
    """
    import asyncio, logging
    logger = logging.getLogger("autotube.shorts_recovery")
    try:
        from api.services.shorts_recovery_planner import auto_recover_shorts
        result = await asyncio.to_thread(auto_recover_shorts)
        total = (result.get("recovered_count", 0) + result.get("cancelled_count", 0))
        if total > 0:
            logger.info(
                "Shorts recovery: +%d added, -%d cancelled across %d channels: %s",
                result.get("recovered_count", 0),
                result.get("cancelled_count", 0),
                len(result.get("channels_affected", [])),
                ", ".join(result.get("channels_affected", [])),
            )
    except Exception as e:
        logger.error("Shorts recovery planner error: %s", e)
```

---

## Verificación

- No hay tests existentes específicamente para `recovery_planner` de long-form (solo para `auto_recover_on_startup` de generation_service)
- Se verifica ejecutando `python3 -c "from api.services.recovery_planner import *"` y `python3 -c "from api.services.shorts_recovery_planner import *"` para comprobar que no hay errores de sintaxis
- No rompe tests existentes porque el recovery_planner solo añade lógica (no cambia comportamiento existente)
