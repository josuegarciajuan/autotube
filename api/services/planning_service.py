"""Dynamic daily video planning engine.

Computes upload slots for all channels for a given day, distributing
them across Spain-optimized time windows with human-like variation
and anti-collision enforcement (only one generation at a time).

Architecture:
  compute_daily_slots(date_key, channel_configs) → list of slot dicts
  compute_and_store_slots(date_key) → persists slots to planned_slots table
  sync_midday() → reconcile config changes mid-day
  preview_week(overrides) → dry-run 7 days without persistence
"""

import hashlib
import json
import logging
import secrets
import threading
import functools
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional
import pytz

logger = logging.getLogger("autotube.planning")

# ── Dispatch lock (serializes all generation dispatches) ────────
from api.services.generation_service import _DISPATCH_LOCK

# ── Replan lock (v26): serializes writers of planned_slots ──────
# smart_replan, recovery_planner, sync_midday, _ensure_never_dry,
# compute_and_store_horizon, full_replan and _readjust_pending_slots all
# create/cancel/retime planned_slots. Without a shared lock they can
# overwrite each other's plan. RLock (reentrant) so nested calls from the
# same thread (e.g. smart_replan → compute_and_store_horizon) don't deadlock.
_REPLAN_LOCK = threading.RLock()

def _replan_locked(func):
    """Decorator: run the function under the shared _REPLAN_LOCK."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _REPLAN_LOCK:
            return func(*args, **kwargs)
    return wrapper

# ── Cooldown guard: prevent rapid successive replans ─────
_last_horizon_replan_ts: Optional[datetime] = None
_HORIZON_REPLAN_COOLDOWN_MIN = 5  # minimum minutes between replans

# ── Horizon replan gate (v40, persistido) ──────────────────
# El horizonte de 7 días se reconstruye como mucho una vez cada 24 h desde el
# ÚLTIMO replan (manual o automático). El timestamp vive en system_state para
# que un reinicio de la API no resete el gate. Las emergencias (pipeline seco,
# día vacío, cambio de config, _ensure_never_dry) siguen disparando de
# inmediato vía compute_and_store_horizon y resetan el timer.
HORIZON_REPLAN_INTERVAL_HOURS = 24
_LAST_REPLAN_KEY = "last_horizon_replan_ts"


def get_last_replan_ts(db=None) -> float:
    """Epoch (float) del último replan de horizonte (manual o automático).

    Fallback: now - 24h para que tras un upgrade el primer check periódico
    dispare (el gate de 24h se considera cumplido de arranque).
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    try:
        raw = db.get_system_state(_LAST_REPLAN_KEY)
        if raw:
            return float(raw)
    except Exception:
        pass
    return time.time() - HORIZON_REPLAN_INTERVAL_HOURS * 3600

# ── Post-full_replan quiet window ──────────────────────────
# After a manual full_replan(), block ALL automatic compute_and_store_horizon()
# calls for this many minutes — even force_replan=True cannot bypass.
# This prevents the user's manual replan from being overwritten by smart_replan,
# midnight check, never-dry, or startup replans.
# v40: se persiste en system_state("post_full_replan_block_until") (epoch string)
# para que un reinicio de la API no pierda la ventana silenciosa.
_POST_FULL_REPLAN_QUIET_MIN = 120
_post_full_replan_block_until: Optional[datetime] = None

# ── Dynamic VPD adjuster ───────────────────────────────────
class DynamicSlotAdjuster:
    """
    Auto-adjusts videos_per_day per channel based on pipeline health.
    
    Called by _readjust_pending_slots after slot reconciliation.
    Evaluates success rate, backlog, account quota sharing, and channel
    maturity to determine optimal daily slot allocation.
    """
    
    def __init__(self, db):
        self.db = db
        self._last_check: dict[str, float] = {}
    
    def should_check(self, slug: str, interval_h: int = 6) -> bool:
        import time
        now = time.time()
        last = self._last_check.get(slug, 0)
        if now - last > interval_h * 3600:
            self._last_check[slug] = now
            return True
        return False
    
    def evaluate_channel(self, slug: str) -> dict:
        from config.config_bridge import get_channel_config
        cfg = get_channel_config(slug)
        
        base = getattr(cfg, 'VIDEOS_PER_DAY_BASE', 2)
        vpd_min = getattr(cfg, 'VIDEOS_PER_DAY_MIN', 1)
        vpd_max = getattr(cfg, 'VIDEOS_PER_DAY_MAX', 4)
        
        # ── v26: never raise a channel the circuit breaker paused ──
        # If the channel was paused (videos_per_day=0 by the breaker or
        # manually), the dynamic adjuster must NOT raise it back. Raising
        # is a MANUAL decision once the root cause is fixed.
        current_vpd = self._get_current_vpd(slug)
        if current_vpd <= 0:
            return {'recommended': 0, 'reason': 'vpd_zero_paused'}
        
        if not getattr(cfg, 'DYNAMIC_VPD_ENABLED', True):
            return {'recommended': base, 'reason': 'dynamic_disabled'}
        
        # ── v26: circuit-breaker guard — a broken channel drops to 0 ──
        try:
            ch_row = self.db.get_channel_by_slug(slug)
            ch_id = ch_row["id"] if ch_row else None
            if ch_id is not None:
                failures = count_channel_consecutive_failures(self.db, ch_id)
                if failures >= CHANNEL_CONSECUTIVE_FAILURES:
                    return {
                        'recommended': 0,
                        'success_rate': round(self._compute_success_rate(
                            slug, getattr(cfg, 'DYNAMIC_VPD_SUCCESS_WINDOW', 10)), 2),
                        'reason': f'consecutive_failures={failures}',
                    }
        except Exception:
            pass
        
        success_rate = self._compute_success_rate(
            slug, getattr(cfg, 'DYNAMIC_VPD_SUCCESS_WINDOW', 10)
        )
        awaiting = self._count_awaiting(slug)
        backlog_boost = getattr(cfg, 'DYNAMIC_VPD_BACKLOG_BOOST', 4)
        # ── Quota-aware (ago 2026): el tope compartido se deriva del presupuesto
        # real del PROYECTO GCP (get_project_max_daily_uploads), no de una
        # heurística por google_account, y cubre longs + shorts nativos.
        from api.services.quota_tracker import get_project_max_daily_uploads
        project = self._get_project(slug)
        sibling_slugs = self._get_sibling_channels_by_project(project)
        account_total = self._sum_daily_uploads_for_slugs(sibling_slugs)
        account_max = get_project_max_daily_uploads(project)
        if account_max <= 0:
            # Fallback legacy: heurística por cuenta (solo si no se puede
            # resolver el proyecto o el presupuesto no está configurado).
            account = self._get_google_account(slug)
            sibling_slugs = self._get_sibling_channels(account)
            account_total = self._sum_vpd_for_slugs(sibling_slugs)
            account_max = getattr(cfg, 'DYNAMIC_VPD_SHARED_ACCOUNT_MAX', 6)
        published_count = self._count_published(slug)
        new_boost = 1 if published_count < getattr(cfg, 'DYNAMIC_VPD_NEW_CHANNEL_THRESHOLD', 30) else 0
        catchup = self._get_catchup_boost(slug, cfg)
        
        recommended = base
        if success_rate >= getattr(cfg, 'DYNAMIC_VPD_SUCCESS_THRESHOLD', 0.7) and awaiting < backlog_boost:
            recommended += 1
        elif success_rate < 0.5 or awaiting >= backlog_boost * 2:
            recommended -= 1
        recommended += new_boost
        recommended += catchup
        recommended = max(vpd_min, min(vpd_max, recommended))
        
        if len(sibling_slugs) > 1:
            others_total = account_total - self._get_current_vpd(slug)
            available = account_max - others_total
            recommended = min(recommended, max(vpd_min, available))
        
        return {
            'recommended': recommended,
            'success_rate': round(success_rate, 2),
            'awaiting_upload': awaiting,
            'published_count': published_count,
            'new_boost': new_boost,
            'catchup_boost': catchup,
            'account_siblings': len(sibling_slugs),
            'reason': self._build_reason(success_rate, awaiting, new_boost, catchup),
        }
    
    def apply(self, slug: str) -> int:
        result = self.evaluate_channel(slug)
        recommended = result['recommended']
        
        try:
            with self.db._connect() as conn:
                conn.execute(
                    "UPDATE channels SET config_json = json_set(config_json, '$.videos_per_day', ?) "
                    "WHERE slug = ?",
                    (recommended, slug),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[{slug}] Dynamic VPD apply failed: {e}")
            return -1
        
        logger.info(f"[{slug}] Dynamic VPD: vpd={recommended} — {result['reason']}")
        return recommended
    
    def apply_all(self) -> dict[str, int]:
        try:
            channels = self.db.fetch_all("SELECT slug FROM channels WHERE active = 1")
        except Exception:
            return {}
        results = {}
        for ch in channels:
            slug = ch['slug']
            if not self.should_check(slug):
                continue
            results[slug] = self.apply(slug)
        return results
    
    # ── private helpers ───────────────────────────────
    
    def _compute_success_rate(self, slug: str, window: int) -> float:
        try:
            jobs = self.db.fetch_all("""
                SELECT status FROM generation_jobs
                WHERE channel_slug = ? AND status IN ('completed', 'failed')
                ORDER BY created_at DESC LIMIT ?
            """, (slug, window))
        except Exception:
            return 1.0
        if not jobs:
            return 1.0
        completed = sum(1 for j in jobs if j['status'] == 'completed')
        return completed / len(jobs)
    
    def _count_awaiting(self, slug: str) -> int:
        try:
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM videos WHERE canal = ? AND status = 'awaiting_upload'",
                    (slug,),
                ).fetchone()
            return row['cnt'] if row else 0
        except Exception:
            return 0
    
    def _count_published(self, slug: str) -> int:
        try:
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM videos WHERE canal = ? AND status = 'published'",
                    (slug,),
                ).fetchone()
            return row['cnt'] if row else 0
        except Exception:
            return 0
    
    def _get_google_account(self, slug: str) -> str:
        try:
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT google_account FROM channels WHERE slug = ?", (slug,)
                ).fetchone()
            return row['google_account'] if row else ''
        except Exception:
            return ''
    
    def _get_sibling_channels(self, account: str) -> list:
        if not account:
            return []
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT slug FROM channels WHERE google_account = ? AND active = 1",
                    (account,),
                ).fetchall()
            return [r['slug'] for r in rows]
        except Exception:
            return []
    
    def _get_project(self, slug: str) -> str:
        """Proyecto GCP del canal (autoritativo vía client_secret)."""
        try:
            from api.services.quota_tracker import get_channel_project
            return get_channel_project(slug)
        except Exception:
            return "unknown"
    
    def _get_sibling_channels_by_project(self, project: str) -> list:
        """Canales activos del mismo proyecto GCP (comparten presupuesto real)."""
        if not project or project == "unknown":
            return []
        try:
            from api.services.quota_tracker import get_channel_project
            rows = self.db.get_channels(active_only=True) or []
            return [
                ch.get("slug") for ch in rows
                if ch.get("slug") and get_channel_project(ch.get("slug")) == project
            ]
        except Exception:
            return []
    
    def _sum_daily_uploads_for_slugs(self, slugs: list) -> int:
        """Suma de subidas/día de los canales: vpd (long) + shorts nativos."""
        total = 0
        for s in slugs:
            total += self._get_current_vpd(s)
            try:
                ch = self.db.get_channel_by_slug(s)
                ch_id = ch["id"] if ch else None
                if ch_id:
                    sc = self.db.get_shorts_planning_config(channel_id=ch_id)
                    if sc and sc[0].get("shorts_enabled", True):
                        total += int(sc[0].get("shorts_native_per_day", 3) or 3)
            except Exception:
                pass
        return total
    
    def _get_current_vpd(self, slug: str) -> int:
        try:
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT json_extract(config_json, '$.videos_per_day') as vpd "
                    "FROM channels WHERE slug = ?",
                    (slug,),
                ).fetchone()
            return int(row['vpd']) if row and row['vpd'] else 2
        except Exception:
            return 2
    
    def _sum_vpd_for_slugs(self, slugs: list) -> int:
        return sum(self._get_current_vpd(s) for s in slugs)
    
    def _get_catchup_boost(self, slug: str, cfg) -> int:
        try:
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM system_state WHERE key = 'quota_exhausted_at'"
                ).fetchone()
            if not row or not row['value']:
                return 0
            paused_dt = datetime.fromisoformat(row['value'])
            hours_paused = (datetime.now(timezone.utc) - paused_dt).total_seconds() / 3600
        except Exception:
            return 0
        
        catchup_duration = getattr(cfg, 'DYNAMIC_VPD_CATCHUP_DURATION_H', 48)
        if hours_paused > 6 and hours_paused < catchup_duration:
            return getattr(cfg, 'DYNAMIC_VPD_CATCHUP_BOOST', 2)
        return 0
    
    def _build_reason(self, success_rate: float, awaiting: int, new_boost: int, catchup: int) -> str:
        parts = [f"success_rate={success_rate:.0%}"]
        if awaiting > 0:
            parts.append(f"backlog={awaiting}")
        if new_boost:
            parts.append(f"new_channel_boost=+{new_boost}")
        if catchup:
            parts.append(f"catchup_boost=+{catchup}")
        return ", ".join(parts)


# ── Constants ────────────────────────────────────────────────

# Spain-optimal UPLOAD windows (CEST = UTC+2).  These are the target publish
# hours — the generation START is calculated backwards from these.
SPAIN_UPLOAD_WINDOWS = [
    # (start_hour, end_hour, weight, name)
    (10, 13, 1, "mañana"),      # Publish target: 10:00-12:59
    (14, 17, 2, "mediodía"),    # Publish target: 14:00-16:59 (lunch/post-lunch)
    (18, 22, 3, "noche"),       # Publish target: 18:00-21:59 (prime time)
]
SPAIN_FALLBACK_WINDOW = (8, 10, 0.5, "madrugada-overflow")

ESTIMATED_PIPELINE_MINUTES = 120  # fallback gen duration when per-channel data unavailable
MIN_GAP_MINUTES = 90               # minimum gap between generation START times (same-channel collision)
# v2 smart scheduling: realistic gap between completion of one job and start of next.
# Previously 5 min (unrealistic). Now 30 min buffer, chained as: spacing = ch_dur + GAP.
GLOBAL_GAP_MINUTES = 30            # buffer minutes between consecutive generation jobs
# ago 2026 (antiban): 3h → 6h — las ráfagas de publicación del mismo canal
# alimentaron el flag de spam de YouTube (strikes de ago-2026).
MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS = 6  # minimum hours between publish times for same channel (v10.1 collision fix; antiban 6h)
BUFFER_PCT = 0.15                  # safety buffer on per-channel avg creation time
DEFAULT_HORIZON_DAYS = 7           # days to plan ahead (today + 6)

# ── Channel circuit breaker (v26 Aug 2026) ────────────────────────
# After N consecutive PERMANENT long-form generation failures within a
# window, the channel is paused: an alert is created (documenting that
# videos_per_day must be raised again once the root cause is fixed),
# videos_per_day is lowered to 0, and its pending slots are cancelled.
# This prevents a broken channel from monopolizing the single long-form
# generation slot and starving the healthy channels (death-spiral fix).
CHANNEL_CONSECUTIVE_FAILURES = 3     # consecutive permanent failures to trip the breaker
CHANNEL_FAILURE_WINDOW_H = 6         # look-back window for counting failures
# Long-form generation actions only — shorts failures do NOT trip the breaker
CHANNEL_BREAKER_ACTIONS = ("generate_only", "generate_and_upload")


# ── Alternate pattern resolution ─────────────────────────────────

def _resolve_videos_per_day(ch: dict, date_str: str) -> int:
    """Resolve effective videos_per_day for a channel on a specific date.

    Supports two modes:
    1. Random daily boost (default): base videos_per_day + probabilistic +1.
       Uses MD5 hash of (date, channel_id) for deterministic randomness —
       same inputs always give the same result.
    2. Alternate pattern (legacy): if 'alternate_pattern' is set, it takes
       precedence over the random boost.
    """
    # Legacy alternate pattern takes precedence if explicitly set
    pattern = ch.get("alternate_pattern")
    if pattern and isinstance(pattern, list) and len(pattern) >= 2:
        day_ordinal = datetime.strptime(date_str, "%Y-%m-%d").toordinal()
        offset = ch.get("alternate_offset", 0)
        idx = (day_ordinal + offset) % len(pattern)
        return int(pattern[idx])

    base = int(ch.get("videos_per_day", 2) or 2)
    if base <= 0:
        return 0

    boost_weight = float(ch.get("videos_day_boost_weight", 0.7))
    ch_id = int(ch.get("channel_id", 0) or 0)

    # Deterministic hash-based random — better uniformity than random.Random(seed)
    seed_str = f"{date_str}|{ch_id}|videos"
    h = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
    roll = h / 0xFFFFFFFF

    if roll < boost_weight:
        return base + 1
    return base


# ── Quota-aware planning (ago 2026) ────────────────────────────
# Cada subida (long o short) consume UPLOAD_UNITS (1.600 ud) de videos.insert.
# El presupuesto automático de un proyecto GCP se deriva en runtime de
# get_project_automatic_budget_units(project) — NUNCA hardcodear subidas/día.
UPLOAD_UNITS = 1600


def _count_running_upload_slots_for_project(db, project_id: str, date_str: str) -> int:
    """Slots 'running' (long+short) de hoy para un proyecto (aún sin reservar).

    Representan subidas comprometidas en curso; restarlas de la capacidad evita
    sobre-planificar cuando ya hay trabajo de hoy en marcha.
    """
    from api.services.quota_tracker import get_channel_project
    ch_ids: list[int] = []
    try:
        for ch in (db.get_channels(active_only=False) or []):
            s = ch.get("slug")
            if s and get_channel_project(s) == project_id and ch.get("id"):
                ch_ids.append(int(ch["id"]))
    except Exception:
        return 0
    if not ch_ids:
        return 0
    count = 0
    try:
        ph = ",".join("?" for _ in ch_ids)
        with db._connect() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS total FROM planned_slots
                    WHERE status = 'running' AND date_key = ?
                      AND channel_id IN ({ph})""",
                (date_str, *ch_ids),
            ).fetchone()
            count += int(row["total"] or 0) if row else 0
            row2 = conn.execute(
                f"""SELECT COUNT(*) AS total FROM shorts_planned_slots
                    WHERE status = 'running' AND date_key = ?
                      AND channel_id IN ({ph})""",
                (date_str, *ch_ids),
            ).fetchone()
            count += int(row2["total"] or 0) if row2 else 0
    except Exception:
        pass
    return count


def compute_daily_upload_allocation(db, date_str: str) -> dict:
    """Cupo diario de subidas por canal y proyecto (quota-aware).

    Política (long-form primero):
      1. Longs de todos los canales del proyecto (round-robin).
      2. Clips — consecuencia garantizada de los longs (clips_per_long × longs).
         Se RESERVA presupuesto (no crea slots) para que los clips ad-hoc de
         runtime no agoten el cupo del proyecto por sorpresa.
      3. Shorts nativos (round-robin) con el cupo sobrante.

    Para hoy, la capacidad se reduce con las subidas ya reservadas/consumidas
    (yt_quota_reservations + yt_quota_log vía get_project_remaining_upload_slots)
    y con los slots 'running' de hoy (subirán sin haber reservado aún).

    Args:
        db: ExtendedDatabase.
        date_str: día de publicación (YYYY-MM-DD, hora local Europe/Madrid).

    Returns:
        {project_id: {"max_uploads": int,
                      "channels": {slug: {"long": int, "native": int, "clips_est": int}}}}
    """
    from api.services.quota_tracker import (
        get_channel_project,
        get_project_max_daily_uploads,
        get_project_remaining_upload_slots,
        quota_day_pacific,
    )

    today_local = date.today().isoformat()
    today_pt = quota_day_pacific()

    try:
        channels = db.get_channels(active_only=True) or []
    except Exception:
        return {}

    # Config de shorts por canal (misma fuente que compute_daily_shorts_slots)
    shorts_cfg = {}
    try:
        for sc in (db.get_shorts_planning_config() or []):
            shorts_cfg[sc["channel_id"]] = sc
    except Exception:
        pass

    by_project: dict[str, list[dict]] = {}
    for ch in channels:
        slug = ch.get("slug")
        if not slug or slug == "test" or not ch.get("id"):
            continue
        ch_id = int(ch["id"])
        # Canal pausado por el breaker → no consume cupo
        try:
            if _channel_paused_for_planning(db, ch_id):
                continue
        except Exception:
            pass
        proj = get_channel_project(slug)
        if proj == "unknown":
            continue
        cfg = {}
        try:
            cfg = db.get_channel_planning_config(ch_id)
        except Exception:
            pass
        desired_long = 0
        if cfg.get("planning_enabled", True):
            try:
                desired_long = _resolve_videos_per_day(cfg, date_str)
            except Exception:
                desired_long = 0
        sc = shorts_cfg.get(ch_id, {})
        shorts_on = sc.get("shorts_enabled", True)
        desired_native = int(sc.get("shorts_native_per_day", 3) or 3) if shorts_on else 0
        clips_per_long = int(sc.get("shorts_clips_per_long", 3) or 3)
        by_project.setdefault(proj, []).append({
            "slug": slug,
            "channel_id": ch_id,
            "long": max(int(desired_long), 0),
            "native": max(int(desired_native), 0),
            "clips_per_long": max(int(clips_per_long), 0),
        })

    result: dict[str, dict] = {}
    for proj, chans in by_project.items():
        # ── Capacidad del día ──
        if date_str == today_local:
            capacity = get_project_remaining_upload_slots(proj, date=today_pt, db=db)
            try:
                capacity = max(capacity - _count_running_upload_slots_for_project(
                    db, proj, date_str), 0)
            except Exception:
                pass
        else:
            capacity = get_project_max_daily_uploads(proj)

        chans_sorted = sorted(chans, key=lambda c: c["channel_id"])
        for c in chans_sorted:
            c["long_cap"] = 0
            c["native_cap"] = 0
            c["clips_est"] = 0

        remaining = max(int(capacity), 0)

        # ── 1. Longs (round-robin) ──
        while remaining > 0:
            progressed = False
            for c in chans_sorted:
                if c["long_cap"] < c["long"]:
                    c["long_cap"] += 1
                    remaining -= 1
                    progressed = True
                    if remaining <= 0:
                        break
            if not progressed:
                break

        # ── 2. Clips: reservar presupuesto (no crea slots) ──
        for c in chans_sorted:
            c["clips_est"] = c["clips_per_long"] * c["long_cap"]
        clip_total = sum(c["clips_est"] for c in chans_sorted)
        remaining -= min(clip_total, remaining)

        # ── 3. Shorts nativos (round-robin) con el sobrante ──
        while remaining > 0:
            progressed = False
            for c in chans_sorted:
                if c["native_cap"] < c["native"]:
                    c["native_cap"] += 1
                    remaining -= 1
                    progressed = True
                    if remaining <= 0:
                        break
            if not progressed:
                break

        result[proj] = {
            "max_uploads": max(int(capacity), 0),
            "channels": {
                c["slug"]: {
                    "long": c["long_cap"],
                    "native": c["native_cap"],
                    "clips_est": c["clips_est"],
                }
                for c in chans_sorted
            },
        }
    return result


def _resolve_long_cap(alloc: dict, slug: str, default_n: int) -> int:
    """Cap de longs para un canal según la asignación quota-aware."""
    try:
        for proj, data in alloc.items():
            if slug in data.get("channels", {}):
                return min(default_n, int(data["channels"][slug].get("long", default_n)))
    except Exception:
        pass
    return default_n


def _resolve_native_cap(alloc: dict, slug: str, default_n: int) -> int:
    """Cap de shorts nativos para un canal según la asignación quota-aware."""
    try:
        for proj, data in alloc.items():
            if slug in data.get("channels", {}):
                return min(default_n, int(data["channels"][slug].get("native", default_n)))
    except Exception:
        pass
    return default_n


# ── Source mode alternation ─────────────────────────────────

def _build_source_mode_sequence(total: int, ch: dict, date_str: str) -> list[str]:
    """Build alternating source_mode sequence for one channel's daily slots.

    Viral count is computed deterministically: viral_per_day (base minimum) +
    probabilistic +1 based on viral_day_boost_weight, capped at total.

    Always starts with 'viral' when viral_count > 0 so viral slots get
    earlier scheduled_at and are dispatched first. Then alternates to
    distribute them evenly throughout the day.

    Examples:
        total=3, viral_count=1 → ['viral', 'original', 'original']
        total=3, viral_count=2 → ['viral', 'original', 'viral']
        total=4, viral_count=2 → ['viral', 'original', 'viral', 'original']
        total=2, viral_count=0 → ['original', 'original']
        total=2, viral_count=2 → ['viral', 'viral']
    """
    # ── Deterministic viral count with boost ──
    viral_min = int(ch.get("viral_per_day", 1) or 1)
    viral_boost = float(ch.get("viral_day_boost_weight", 0.2))
    ch_id = int(ch.get("channel_id", 0) or 0)

    # Deterministic hash-based random with offset suffix to avoid same seed as videos
    seed_str = f"{date_str}|{ch_id}|viral"
    h = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
    roll = h / 0xFFFFFFFF

    viral_count = min(viral_min, total)
    if total > viral_min and roll < viral_boost:
        viral_count = min(viral_min + 1, total)
    viral_count = max(0, min(viral_count, total))

    if viral_count <= 0:
        return ["original"] * total
    if viral_count >= total:
        return ["viral"] * total

    original_count = total - viral_count
    # Always start with viral first so they get earlier scheduled_at
    # and are dispatched before original slots. The alternation still
    # ensures even distribution throughout the day.
    first, second = "viral", "original"
    first_avail, second_avail = viral_count, original_count

    result = []
    for i in range(total):
        if i % 2 == 0:
            # Even slots → use the primary (more abundant) type
            if first_avail > 0:
                result.append(first)
                first_avail -= 1
            else:
                result.append(second)
                second_avail -= 1
        else:
            # Odd slots → use the secondary type
            if second_avail > 0:
                result.append(second)
                second_avail -= 1
            else:
                result.append(first)
                first_avail -= 1
    return result


# ── Seed helpers ──────────────────────────────────────────────

def _day_seed(date_str: str) -> int:
    """Deterministic seed for a date. Same date always → same seed."""
    h = hashlib.md5(date_str.encode()).hexdigest()
    return int(h[:8], 16)


def _channel_seed(day_seed: int, channel_id: int) -> int:
    """Combine day seed with channel id for per-channel variation."""
    return (day_seed ^ (channel_id * 0x9E3779B9)) & 0xFFFFFFFF


# ── Timezone helpers ─────────────────────────────────────────

def _naive_local_to_utc(naive_str: str, tz_str: str) -> str:
    """Convert a naive local datetime string to ISO8601 UTC.

    Args:
        naive_str: e.g. '2026-07-24 21:07:00' (local time, no TZ).
        tz_str: e.g. 'Europe/Madrid'.

    Returns:
        ISO8601 UTC string, e.g. '2026-07-24T19:07:00+00:00'.
    """
    try:
        tz = pytz.timezone(tz_str)
        naive_dt = datetime.strptime(naive_str, "%Y-%m-%d %H:%M:%S")
        localized = tz.localize(naive_dt)
        utc_dt = localized.astimezone(timezone.utc)
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    except (pytz.UnknownTimeZoneError, ValueError, TypeError):
        # Fallback: return as-is (will be treated as naive by downstream parsers)
        logger.debug("_naive_local_to_utc failed for '%s' (tz=%s) — returning as-is", naive_str, tz_str)
        return naive_str


# ── Core algorithm ────────────────────────────────────────────

def _pick_time_in_window(
    window: tuple,        # (start_h, end_h, weight, name)
    channel_seed: int,
    avoid_hh00_hh30: bool = True,
) -> tuple:
    """Pick a random minute within a time window using deterministic seed.
    
    Returns (hour, minute) in CEST — this is the TARGET UPLOAD time.
    """
    start_h, end_h, _, _ = window
    range_minutes = (end_h - start_h) * 60
    
    # Base: deterministically pick a minute offset within the range
    offset = channel_seed % range_minutes
    
    # Jitter: asymmetric — more room before, tight after (-25..+5)
    jitter = ((channel_seed >> 16) % 31) - 25  # -25..+5
    
    total_offset = offset + jitter
    total_offset = max(0, min(range_minutes - 1, total_offset))
    
    total_minutes = start_h * 60 + total_offset
    hour = total_minutes // 60
    minute = total_minutes % 60
    
    # Avoid :00 and :30 — push to :07 / :37 or :22 / :52
    if avoid_hh00_hh30:
        if minute == 0:
            minute = 7 + (channel_seed % 15)   # 7-21
        elif minute == 30:
            minute = 30 + 7 + (channel_seed % 15)  # 37-51
            if minute >= 60:
                minute -= 20
    
    return hour, minute


def _optimal_slot_windows(db, channel_id: int) -> list[tuple]:
    """Build (start_h, end_h, weight, name) windows from data-driven optimal slots.

    Returns [] if no fresh optimal slots exist (caller falls back to heuristic).
    """
    if db is None:
        return []
    try:
        slots = db.get_optimal_slots(channel_id, "long")
    except Exception as exc:
        logger.debug("get_optimal_slots failed for ch%d: %s", channel_id, exc)
        return []
    if not slots:
        return []
    windows = []
    for i, s in enumerate(slots):
        h = int(s.get("target_hour", 0) or 0)
        rank = int(s.get("slot_rank", i + 1) or (i + 1))
        weight = 3 if rank == 1 else 2
        windows.append((max(0, h - 1), min(23, h + 1), weight, f"optimal_rank{rank}"))
    return windows


def _distribute_slots(
    videos_per_day: int,
    day_seed: int,
    channel_id: int,
    is_scheduled: bool = False,
    scheduled_cfg: dict = None,
    db=None,
) -> list[tuple]:
    """Distribute N video slots across time windows for one channel.

    For scheduled channels, prefers data-driven optimal_publish_slots (real
    audience peak hours); falls back to niche-specific peak windows (publish_scheduler).
    For immediate channels, uses generic SPAIN_UPLOAD_WINDOWS.

    Returns list of (hour, minute) tuples — TARGET UPLOAD times (public for scheduled).

    Logic:
      1/dia → best window
      2/dia → best window + second best (spread)
      3/dia → three best windows
      4+/day → distribute across all 3 windows evenly, overflow to fallback
    """
    windows = list(SPAIN_UPLOAD_WINDOWS)
    fallback = SPAIN_FALLBACK_WINDOW
    ch_seed = _channel_seed(day_seed, channel_id)

    # ── Scheduled mode: prefer data-driven optimal slots over niche heuristic ──
    if is_scheduled and scheduled_cfg:
        optimal_wins = _optimal_slot_windows(db, channel_id)
        if optimal_wins:
            windows = optimal_wins
            fallback = windows[-1] if len(windows) > 2 else windows[0]
        else:
            try:
                from pipeline.publish_scheduler import get_peak_windows
                peak_wins = get_peak_windows(scheduled_cfg, n=max(videos_per_day, 3))
                if peak_wins:
                    windows = peak_wins
                    fallback = windows[-1] if len(windows) > 2 else windows[0]
            except Exception as exc:
                logger.debug("Failed to load peak windows, falling back to generic: %s", exc)
    
    if videos_per_day <= 0:
        return []
    
    # For scheduled channels: the primary peak (highest-weight window) MUST
    # always be one of the assigned slots. Only secondary peaks rotate by day.
    if is_scheduled and len(windows) > 1:
        primary = windows[0]
        secondary_wins = windows[1:]
        day_window_offset = day_seed % len(secondary_wins)
        rotated = [primary] + (secondary_wins[day_window_offset:] + secondary_wins[:day_window_offset])
    else:
        day_window_offset = day_seed % len(windows)
        rotated = windows[day_window_offset:] + windows[:day_window_offset]
    
    slots = []
    
    if videos_per_day == 1:
        # Single slot: use the first rotated window
        h, m = _pick_time_in_window(rotated[0], ch_seed)
        slots.append((h, m))
    
    elif videos_per_day == 2:
        # Two slots: best window + second best (spread out)
        h1, m1 = _pick_time_in_window(rotated[0], ch_seed)
        h2, m2 = _pick_time_in_window(rotated[1], ch_seed ^ 0xABCD)
        slots = [(h1, m1), (h2, m2)]
    
    elif videos_per_day == 3:
        # All three windows, one per window
        for i, w in enumerate(rotated[:3]):
            h, m = _pick_time_in_window(w, ch_seed ^ (i * 0x1111))
            slots.append((h, m))
    
    else:
        # 4+: distribute across windows, overflow goes to fallback
        per_window, remainder = divmod(videos_per_day, len(rotated[:3]))
        for i, w in enumerate(rotated[:3]):
            count = per_window + (1 if i < remainder else 0)
            for j in range(count):
                sub_seed = ch_seed ^ (i * 0x1111 + j * 0x2222)
                h, m = _pick_time_in_window(w, sub_seed)
                slots.append((h, m))
        
        # If remainder didn't fit, add fallback slots
        planned = len(slots)
        remaining = videos_per_day - planned
        for j in range(remaining):
            sub_seed = ch_seed ^ (0xFFFF + j * 0x3333)
            h, m = _pick_time_in_window(fallback, sub_seed)
            slots.append((h, m))
    
    # Sort slots by time within this channel
    slots.sort(key=lambda x: x[0] * 60 + x[1])
    
    # ── v10.1: Enforce minimum spread between same-channel publish times ──
    # After sorting, ensure no two adjacent slots are closer than 
    # MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS. Push later slots forward if needed.
    if len(slots) >= 2:
        min_gap_minutes = MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS * 60
        spread_slots = [slots[0]]
        for i in range(1, len(slots)):
            prev_minutes = spread_slots[-1][0] * 60 + spread_slots[-1][1]
            curr_minutes = slots[i][0] * 60 + slots[i][1]
            if curr_minutes - prev_minutes < min_gap_minutes:
                # Push this slot forward to maintain minimum gap
                new_minutes = prev_minutes + min_gap_minutes
                new_h = min(new_minutes // 60, 23)
                new_m = new_minutes % 60
                if new_h >= 23 and new_m > 50:
                    # Overflow: wrap to next day (caller handles this)
                    new_h = min(new_h, 23)
                    new_m = min(new_m, 59)
                spread_slots.append((new_h, new_m))
                logger.debug("_distribute_slots: pushed slot %d from %02d:%02d → %02d:%02d "
                             "(gap was %d min < %d min)",
                             i + 1,
                             slots[i][0], slots[i][1],
                             new_h, new_m,
                             curr_minutes - prev_minutes,
                             min_gap_minutes)
            else:
                spread_slots.append(slots[i])
        slots = spread_slots
    
    return slots


def compute_daily_slots(
    date_str: str,
    channel_configs: list[dict],
    db=None,
) -> list[dict]:
    """Core planning algorithm: compute upload slots for all channels.

    The times picked from the optimal windows are TARGET UPLOAD times
    (when the video should appear on YouTube).  Generation START time
    is calculated backwards: scheduled_at = target_upload_at − pipeline_minutes.

    Collision resolution operates on scheduled_at (only one gen at a time).
    After resolving, target_upload_at is recalculated forward.

    Quota-aware (ago 2026): si `db` se provee, el nº de slots por canal se
    limita al cupo del proyecto GCP (compute_daily_upload_allocation) y los
    slots scheduled se agrupan en batches de subida por cuenta.

    Args:
        date_str: ISO date string (YYYY-MM-DD).
        channel_configs: list of dicts with {channel_id, slug, videos_per_day,
                          planning_enabled, name}.
        db: ExtendedDatabase (opcional). Sin db no se aplica cap de cuota.
    """
    day_seed = _day_seed(date_str)
    all_slots = []

    # ── Quota-aware: cupo por proyecto para la fecha ──
    alloc = {}
    if db is not None:
        try:
            alloc = compute_daily_upload_allocation(db, date_str)
        except Exception as exc:
            logger.debug("compute_daily_slots: allocation skipped (%s)", exc)
    
    for ch in channel_configs:
        if not ch.get("planning_enabled", True):
            continue
        n = _resolve_videos_per_day(ch, date_str)
        if db is not None:
            n = _resolve_long_cap(alloc, ch.get("slug", ""), n)
        if n <= 0:
            continue

        is_scheduled = ch.get("publish_mode") == "scheduled"
        warmup_min = ch.get("publish_warmup_min", 120) if is_scheduled else 0
        jitter_min = ch.get("publish_jitter_min", 20) if is_scheduled else 0
        
        # ── Build source_mode sequence for this channel's daily slots ──
        mode_sequence = _build_source_mode_sequence(n, ch, date_str) if n > 0 else []
        
        # _distribute_slots returns (h, m) = TARGET_UPLOAD (public for scheduled)
        raw_slots = _distribute_slots(n, day_seed, ch["channel_id"],
                                       is_scheduled=is_scheduled,
                                       scheduled_cfg=ch if is_scheduled else None,
                                       db=db)
        
        for pos, (target_h, target_m) in enumerate(raw_slots, 1):
            # target_upload_at: for scheduled = public peak time; for immediate = upload time
            target_str = f"{date_str} {target_h:02d}:{target_m:02d}:00"
            
            # ── Generation START: work backwards from target ──
            if is_scheduled:
                # Back out: pipeline + warmup (no extra buffer — collision resolution handles gaps)
                reverse_min = ESTIMATED_PIPELINE_MINUTES + warmup_min
            else:
                reverse_min = ESTIMATED_PIPELINE_MINUTES
            
            total_min = target_h * 60 + target_m - reverse_min
            if total_min < 0:
                total_min = 0  # clamp to midnight
            sched_h = total_min // 60
            sched_m = total_min % 60
            sched_str = f"{date_str} {sched_h:02d}:{sched_m:02d}:00"
            
            # Assign source_mode from the pre-built alternating sequence (0-indexed)
            slot_mode = mode_sequence[pos - 1] if (pos - 1) < len(mode_sequence) else "original"
            
            all_slots.append({
                "channel_id": ch["channel_id"],
                "date_key": date_str,
                "scheduled_at": sched_str,
                "target_upload_at": target_str,
                "target_public_at": target_str if is_scheduled else None,
                "slot_position": pos,
                "channel_name": ch.get("name", ""),
                "channel_slug": ch.get("slug", ""),
                "source_mode": slot_mode,
                "publish_mode": ch.get("publish_mode", "immediate"),
                "publish_timezone": ch.get("publish_timezone", "Europe/Madrid"),
                "publish_warmup_min": warmup_min,
                "upload_window_start": ch.get("upload_window_start", 9),
                "upload_window_end": ch.get("upload_window_end", 11),
            })
    
    # Sort all slots chronologically by generation START time
    all_slots.sort(key=lambda s: s["scheduled_at"])
    
    # ── Collision resolution on scheduled_at ──────────────────
    resolved = []
    for slot in all_slots:
        if resolved:
            last = resolved[-1]
            last_h, last_m = map(int, last["scheduled_at"][11:16].split(":"))
            this_h, this_m = map(int, slot["scheduled_at"][11:16].split(":"))
            
            last_total = last_h * 60 + last_m
            this_total = this_h * 60 + this_m
            
            diff = this_total - last_total
            if diff < MIN_GAP_MINUTES:
                # Push this slot's generation START forward
                new_total = last_total + MIN_GAP_MINUTES
                nh = new_total // 60
                nm = new_total % 60
                nh = min(nh, 23)
                nm = min(nm, 59)
                slot["scheduled_at"] = f"{date_str} {nh:02d}:{nm:02d}:00"
        
        resolved.append(slot)
    
    # Recalculate target_upload_at from scheduled_at after collision resolution
    # ── v10.1: Also enforce same-channel publish spread for scheduled channels ──
    # Group slots by channel to check intra-channel target_upload_at collisions
    channel_slots = {}
    for s in resolved:
        ch_id = s["channel_id"]
        if ch_id not in channel_slots:
            channel_slots[ch_id] = []
        channel_slots[ch_id].append(s)
    
    for ch_id, ch_slots in channel_slots.items():
        # Sort by target_upload_at for this channel
        ch_slots.sort(key=lambda s: s["target_upload_at"])
        min_gap = MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS * 60
        
        for i in range(1, len(ch_slots)):
            prev = ch_slots[i - 1]
            curr = ch_slots[i]
            
            prev_h, prev_m = map(int, prev["target_upload_at"][11:16].split(":"))
            curr_h, curr_m = map(int, curr["target_upload_at"][11:16].split(":"))
            gap = (curr_h * 60 + curr_m) - (prev_h * 60 + prev_m)
            
            if gap < min_gap and curr.get("publish_mode") == "scheduled":
                new_total = prev_h * 60 + prev_m + min_gap
                nh = min(new_total // 60, 23)
                nm = new_total % 60
                push_delta = new_total - (curr_h * 60 + curr_m)
                curr["target_upload_at"] = f"{curr['date_key']} {nh:02d}:{nm:02d}:00"
                # ── v (Aug 2026): keep target_public_at >= target_upload_at ──
                # Pushing the upload forward without touching the publish time
                # could leave target_upload_at > target_public_at (publish before
                # upload). Push target_public_at by the same delta so ordering holds.
                tpa = curr.get("target_public_at")
                if tpa:
                    try:
                        from datetime import datetime as _dtp, timedelta as _tdp
                        _tpa_dt = _dtp.strptime(str(tpa)[:19], "%Y-%m-%d %H:%M:%S")
                        _tpa_dt = _tpa_dt + _tdp(minutes=push_delta)
                        curr["target_public_at"] = _tpa_dt.strftime("%Y-%m-%d %H:%M:%S")
                    except (ValueError, TypeError):
                        # target_public_at may already be ISO/UTC — leave as-is
                        pass
                logger.info(
                    "compute_daily_slots: [%s] pushed slot #%d target from %02d:%02d → %02d:%02d "
                    "(gap was %d min < %d min)",
                    curr.get("channel_slug", "?"), curr["slot_position"],
                    curr_h, curr_m, nh, nm, gap, min_gap,
                )
    
    for s in resolved:
        sched_h, sched_m = map(int, s["scheduled_at"][11:16].split(":"))
        up_total = sched_h * 60 + sched_m + ESTIMATED_PIPELINE_MINUTES
        uh = min(up_total // 60, 23)
        um = min(up_total % 60, 59)
        # For scheduled channels, target_upload_at stays as original peak time
        # (don't recalculate — the generation start was already backed out from peak)
        if s.get("publish_mode") != "scheduled":
            s["target_upload_at"] = f"{date_str} {uh:02d}:{um:02d}:00"
    
    # Update slot positions after collision resolution
    for pos, s in enumerate(resolved, 1):
        s["slot_position"] = pos
    
    # ── Quota-aware: agrupar subidas long-form por cuenta (batches) ──
    # Antes de convertir target_public_at a UTC (la comparación con las horas
    # de batch se hace en hora local naive).
    if db is not None:
        try:
            _apply_upload_batching(resolved, db)
        except Exception as exc:
            logger.debug("compute_daily_slots: upload batching skipped (%s)", exc)
    
    # ── Convert target_public_at from naive local → ISO8601 UTC ──
    for s in resolved:
        if s.get("target_public_at") and s.get("publish_mode") == "scheduled":
            tz_str = s.get("publish_timezone", "Europe/Madrid")
            s["target_public_at"] = _naive_local_to_utc(s["target_public_at"], tz_str)
        elif not s.get("publish_mode") == "scheduled":
            s["target_public_at"] = None
    
    return resolved


# ── Upload batching por cuenta (6a, ago 2026) ─────────────────
# Los videos long-form (fase F2) de los canales que comparten cuenta Google se
# suben agrupados en UPLOAD_BATCH_TIMES (mañana/tarde) para dar sensación de
# subida manual. Solo afecta a slots `publish_mode="scheduled"` (los shorts y
# los inmediatos no se agrupan). Respeta upload + warmup <= publicación; si un
# video no cabe en ningún batch del día, se sube en el último batch del día
# anterior (se conserva la fecha de subida en target_upload_at).

def _slot_batch_times(slot: dict, db) -> list[tuple[int, int]]:
    """Batches configurados para el canal del slot (override por config_json)."""
    from config.settings import UPLOAD_BATCH_TIMES as _DEFAULT_BATCHES
    try:
        ch = db.get_channel(slot.get("channel_id"))
        if ch:
            import json as _json
            cfg = _json.loads(ch.get("config_json") or "{}")
            raw = cfg.get("UPLOAD_BATCH_TIMES")
            if raw:
                from config.settings import _parse_batch_times
                parsed = _parse_batch_times(",".join(str(x) for x in raw)
                                            if isinstance(raw, (list, tuple))
                                            else str(raw))
                if parsed:
                    return parsed
    except Exception:
        pass
    return list(_DEFAULT_BATCHES)


def _slot_latest_allowed_upload(slot: dict) -> str:
    """Último instante permitido para subir: publish − warmup (naive local)."""
    try:
        from datetime import datetime as _dt, timedelta as _td
        tpa = slot.get("target_public_at") or slot.get("target_upload_at")
        if not tpa:
            return None
        s = str(tpa).replace("T", " ")[:19]
        dt = _dt.strptime(s, "%Y-%m-%d %H:%M:%S")
        warmup = int(slot.get("publish_warmup_min", 120) or 120)
        return (dt - _td(minutes=warmup)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _apply_upload_batching(slots: list[dict], db) -> None:
    """Reasigna target_upload_at de slots long-form scheduled a batches por cuenta.

    Modifica `slots` in-place. Determinista: mismo (cuenta, día, slots) → mismo
    resultado. Los slots se ordenan por target_public_at y cada uno se asigna al
    ÚLTIMO batch cuya hora cumpla upload + warmup <= publish; si ninguno cabe,
    se usa el último batch del día anterior. Después se equilibran los batches
    (si todos cayeron en el mismo, se reparte la mitad al batch más temprano,
    que siempre es seguro porque subir antes solo aumenta el warmup).
    """
    from config.settings import _parse_batch_times

    # Agrupar por (google_account, date_key)
    groups: dict[tuple, list[dict]] = {}
    for s in slots:
        if s.get("publish_mode") != "scheduled":
            continue
        acc = ""
        try:
            ch = db.get_channel(s.get("channel_id"))
            if ch:
                acc = ch.get("google_account") or ""
        except Exception:
            pass
        if not acc:
            from api.services.quota_tracker import get_channel_project
            acc = get_channel_project(s.get("channel_slug", "")) or "unknown"
        key = (acc, s.get("date_key"))
        groups.setdefault(key, []).append(s)

    for (acc, day_key), group in groups.items():
        batches = _slot_batch_times(group[0], db)
        if not batches:
            continue
        # Ordenar por publicación para asignar batches de forma estable
        group.sort(key=lambda s: str(s.get("target_public_at") or s.get("target_upload_at") or ""))

        assigned: list[tuple[dict, str]] = []  # (slot, "YYYY-MM-DD HH:MM:SS" local)
        for s in group:
            latest = _slot_latest_allowed_upload(s)
            chosen = None
            if latest is not None:
                # Último batch (hora) del mismo día que cumpla la restricción
                for (bh, bm) in reversed(batches):
                    cand = f"{day_key} {bh:02d}:{bm:02d}:00"
                    if cand <= latest:
                        chosen = cand
                        break
            if chosen is None:
                # Ningún batch de hoy cabe → último batch del día anterior
                from datetime import date as _d, timedelta as _td2
                prev = (_d.fromisoformat(day_key) - _td2(days=1)).isoformat()
                bh, bm = batches[-1]
                chosen = f"{prev} {bh:02d}:{bm:02d}:00"
            assigned.append((s, chosen))

        # Equilibrar: si todos los slots cayeron en el mismo batch y hay >= 2,
        # mover la mitad al batch más temprano (subir antes siempre es seguro).
        from collections import Counter
        batch_minutes = Counter(a[1][11:16] for a in assigned)
        if len(batch_minutes) == 1 and len(assigned) >= 2 and len(batches) >= 2:
            early_b = batches[0]
            early_min = early_b[0] * 60 + early_b[1]
            target = f"{day_key} {early_b[0]:02d}:{early_b[1]:02d}:00"
            half = len(assigned) // 2
            for i in range(half):
                s, _chosen = assigned[i]
                # solo si el batch temprano cumple la restricción del slot
                latest = _slot_latest_allowed_upload(s)
                if latest is not None and target > latest:
                    continue
                assigned[i] = (s, target)

        # Aplicar: hora de batch + jitter humano determinista (2-5 min)
        for i, (s, batch_ts) in enumerate(assigned):
            jitter = 2 + (int(hashlib.md5(
                f"{day_key}|{acc}|{i}|batch".encode()).hexdigest()[:8], 16) % 4)
            from datetime import datetime as _dtb, timedelta as _tdb
            try:
                bdt = _dtb.strptime(batch_ts, "%Y-%m-%d %H:%M:%S") + _tdb(minutes=jitter)
                s["target_upload_at"] = bdt.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                s["target_upload_at"] = batch_ts
            logger.info(
                "[%s] %s: subida batch %s → %s (jitter +%d min)",
                s.get("channel_slug", "?"), day_key,
                batch_ts[11:16], s["target_upload_at"][11:16], jitter,
            )


# ── 3-Phase Horizon Planning (v9) ──────────────────────────────

def _pick_upload_minute(day_seed: int, channel_id: int, slot_pos: int,
                          window_start: int = None, window_end: int = None,
                          videos_per_day: int = 1,
                          windows: list = None,
                          public_hour: int = None,
                          warmup_min: int = 120) -> tuple:
    """Pick a deterministic minute within the upload window(s) for one slot.

    v11+: Accepts `windows` list of dicts [{"start":10,"end":13},...] with
    round-robin distribution across windows. Falls back to single window_start/end
    for backward compatibility.

    v12+: Accepts `public_hour` to filter upload windows that complete BEFORE
    the publication time (accounting for warmup). This prevents the ordering bug
    where target_upload_at > target_public_at.

    Spreads multiple videos from the same channel across the window
    (min 15 min apart). Returns (hour, minute, window_start, window_end).
    """
    # ── v11: multi-window round-robin ──
    if windows and isinstance(windows, list) and len(windows) > 0:
        # Validate windows
        valid_windows = []
        for w in windows:
            if isinstance(w, dict) and "start" in w and "end" in w:
                valid_windows.append(w)

        if valid_windows and public_hour is not None:
            # ── v12: Filter windows to only those BEFORE publication ──
            # Upload must complete at least warmup_min before the publication peak
            latest_upload_hour = public_hour - (warmup_min / 60.0)
            before_pub_windows = [
                w for w in valid_windows
                if w["end"] <= latest_upload_hour
            ]
            if before_pub_windows:
                valid_windows = before_pub_windows
                logger.debug(
                    "_pick_upload_minute: filtered %d→%d windows before pub=%02d:00 "
                    "(warmup=%dmin, latest_upload=%02d:00)",
                    len(windows), len(valid_windows), public_hour,
                    warmup_min, int(latest_upload_hour),
                )
            else:
                logger.debug(
                    "_pick_upload_minute: no upload window fits before pub=%02d:00 "
                    "(warmup=%dmin) — using all windows, public_at will be adjusted",
                    public_hour, warmup_min,
                )

        if valid_windows:
            rr_idx = (day_seed + slot_pos + channel_id) % len(valid_windows)
            chosen = valid_windows[rr_idx]
            window_start = chosen["start"]
            window_end = chosen["end"]
        # else: fall through to window_start/window_end
    elif window_start is None or window_end is None:
        window_start, window_end = 9, 11  # ultimate fallback

    ch_seed = _channel_seed(day_seed, channel_id) ^ (slot_pos * 0xCAFE)
    window_minutes = (window_end - window_start) * 60

    if videos_per_day <= 1:
        # Single video: random within window
        offset = ch_seed % max(window_minutes - 1, 1)
    else:
        # Multiple videos: divide the window into N segments, then jitter
        segment = window_minutes // videos_per_day
        base = slot_pos * segment - segment // 2
        jitter = (ch_seed % max(segment - 15, 1))
        offset = max(0, min(window_minutes - 1, base + jitter))

    total_min = window_start * 60 + offset
    return (total_min // 60, total_min % 60, window_start, window_end)


def compute_horizon_slots(
    channel_configs: list[dict],
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    db=None,
) -> list[dict]:
    """Core 3-phase planning: generate slots across N-day horizon with cross-day gen chaining.
    
    Algorithm:
      1. For each day D in [today .. today+horizon_days):
         - Compute target_public_at (peak publish) per channel using existing windows
         - Compute target_upload_at (upload window) — deterministic minute within 
           the channel's UPLOAD_WINDOW_START..END on day D
      2. Sort ALL slots by target_upload_at (chronologically).
      3. Chain generation starts BACKWARDS:
         - Each generation must FINISH before its target_upload_at
         - Each generation must not START more than lead_hours before target_public_at
         - Generations are chained with GLOBAL_GAP_MINUTES gap
         - Uses per-channel avg creation time (from timing_data) + BUFFER_PCT
    
    Returns:
        Sorted list of slot dicts with: channel_id, date_key, scheduled_at,
        target_upload_at, target_public_at, slot_position, channel_name, channel_slug,
        source_mode, publish_mode.
    """
    from datetime import date as _date, datetime as _dt, timedelta as _td
    
    today = _date.today()
    now = _dt.now()
    all_raw_slots = []
    
    # ── 1. Collect all raw slots across the horizon ──────────────
    for day_offset in range(horizon_days):
        d = today + _td(days=day_offset)
        date_str = d.isoformat()

        # ── Quota-aware (ago 2026): cupo por proyecto para la fecha ──
        alloc = {}
        if db is not None:
            try:
                alloc = compute_daily_upload_allocation(db, date_str)
            except Exception as exc:
                logger.debug("compute_horizon_slots: allocation skipped (%s)", exc)

        for ch in channel_configs:
            if not ch.get("planning_enabled", True):
                continue
            n = _resolve_videos_per_day(ch, date_str)
            if db is not None:
                n = _resolve_long_cap(alloc, ch.get("slug", ""), n)
            if n <= 0:
                continue
            
            is_scheduled = ch.get("publish_mode") == "scheduled"
            ch_id = ch["channel_id"]
            
            # ── A. Compute target_public_at (peak publish time) ──
            raw_peaks = _distribute_slots(
                n, _day_seed(date_str), ch_id,
                is_scheduled=is_scheduled,
                scheduled_cfg=ch if is_scheduled else None,
            )
            
            # ── B. Build source_mode sequence ──
            mode_sequence = _build_source_mode_sequence(n, ch, date_str) if n > 0 else ["original"] * n
            
            for pos, (peak_h, peak_m) in enumerate(raw_peaks, 1):
                target_public_at = f"{date_str} {peak_h:02d}:{peak_m:02d}:00"
                
                # ── C. Compute target_upload_at (within upload window on pub day) ──
                upload_windows = ch.get("upload_windows")
                win_start = ch.get("upload_window_start", 9)
                win_end = ch.get("upload_window_end", 11)
                ws_start = win_start
                ws_end = win_end
                if not is_scheduled:
                    # Immediate mode: upload = right after gen (use target_public_at as deadline)
                    up_h, up_m = peak_h, peak_m
                else:
                    warmup_minutes = ch.get("publish_warmup_min", 120)
                    up_h, up_m, ws_start, ws_end = _pick_upload_minute(
                        _day_seed(date_str), ch_id, pos,
                        window_start=win_start, window_end=win_end,
                        videos_per_day=n, windows=upload_windows,
                        public_hour=peak_h if is_scheduled else None,
                        warmup_min=warmup_minutes,
                    )
                target_upload_at = f"{date_str} {up_h:02d}:{up_m:02d}:00"

                # ── v12: Enforce target_upload_at < target_public_at ──
                # Upload MUST happen before publication (with warmup buffer).
                # If the upload window falls after the peak publish time,
                # push the publication time forward to after upload+warmup.
                if is_scheduled:
                    warmup_minutes = ch.get("publish_warmup_min", 120)
                    # Add 60min safety buffer so the gap is clearly visible even
                    # after timezone conversions and UI rounding
                    effective_warmup = warmup_minutes + 60
                    upload_dt_chk = _dt.strptime(target_upload_at, "%Y-%m-%d %H:%M:%S")
                    public_dt_chk = _dt.strptime(target_public_at, "%Y-%m-%d %H:%M:%S")
                    min_public_dt = upload_dt_chk + _td(minutes=effective_warmup)
                    if public_dt_chk < min_public_dt:
                        new_public = min_public_dt
                        old_public_at = target_public_at
                        target_public_at = new_public.strftime("%Y-%m-%d %H:%M:%S")
                        logger.warning(
                            "compute_horizon_slots: [%s] target_public_at pushed: "
                            "%s → %s (upload at %s + %dmin warmup+%dmin buffer)",
                            ch.get("slug", "?"),
                            old_public_at[11:16], target_public_at[11:16],
                            target_upload_at[11:16], warmup_minutes, 60,
                        )
                
                all_raw_slots.append({
                    "channel_id": ch_id,
                    "date_key": date_str,           # Publication day
                    "target_public_at": target_public_at,
                    "target_upload_at": target_upload_at,
                    "slot_position": pos,
                    "channel_name": ch.get("name", ""),
                    "channel_slug": ch.get("slug", ""),
                    "source_mode": mode_sequence[pos - 1] if (pos - 1) < len(mode_sequence) else "original",
                    "publish_mode": ch.get("publish_mode", "immediate"),
                    "publish_timezone": ch.get("publish_timezone", "Europe/Madrid"),
                    "publish_warmup_min": int(ch.get("publish_warmup_min", 120) or 120),
                    "lead_hours": ch.get("generation_lead_hours", 36),
                    "avg_duration_min": ch.get("avg_duration_min", ESTIMATED_PIPELINE_MINUTES),
                    "upload_window_start": ws_start,
                    "upload_window_end": ws_end,
                })
    
    if not all_raw_slots:
        return []
    
    # ── 1b. Quota-aware: agrupar subidas long-form por cuenta (batches) ──
    # Antes de ordenar/encadenar scheduled_at (el chaining depende de la hora
    # de subida). Las comparaciones de batch se hacen en hora local naive.
    if db is not None:
        try:
            _apply_upload_batching(all_raw_slots, db)
        except Exception as exc:
            logger.debug("compute_horizon_slots: upload batching skipped (%s)", exc)
    
    # ── 2. Sort all slots by target_upload_at ───────────────────
    all_raw_slots.sort(key=lambda s: s["target_upload_at"])
    
    # ── 3. Chain generation starts (walk BACKWARDS) ─────────────
    # Walk from the latest slot to the earliest, chaining generation starts
    # so each gen finishes before the NEXT slot's upload window.
    
    next_scheduled_at = None  # The next (later) slot's scheduled_at
    
    for i in range(len(all_raw_slots) - 1, -1, -1):
        s = all_raw_slots[i]
        ch_dur = s["avg_duration_min"]
        lead_h = s["lead_hours"]
        is_scheduled = s["publish_mode"] == "scheduled"
        
        # ── Adaptive lead time: increase buffer when behind schedule ──
        # If past-due slots exist for this channel, boost lead_hours so
        # future slots are planned with much more margin, building a
        # pre-generated buffer that prevents future delays.
        if is_scheduled:
            try:
                from database.db_extended import ExtendedDatabase
                _adap_db = ExtendedDatabase()
                past_due = _adap_db.count_past_due_slots()
                if past_due > 0:
                    # Boost: base lead + extra per past-due slot (max 72h)
                    lead_h = min(lead_h + past_due * 2, 72)
            except Exception:
                pass
        
        # Parse target times
        upload_dt = _dt.strptime(s["target_upload_at"], "%Y-%m-%d %H:%M:%S")
        public_dt = _dt.strptime(s["target_public_at"], "%Y-%m-%d %H:%M:%S")
        
        # ── Latest allowed start: must finish before upload window ──
        latest_start = upload_dt - _td(minutes=ch_dur)
        
        # ── Earliest allowed start: no more than lead_hours before public ──
        earliest_start = public_dt - _td(hours=lead_h)
        
        if not is_scheduled:
            # Immediate mode: same logic as before (back from public)
            latest_start = public_dt - _td(minutes=ESTIMATED_PIPELINE_MINUTES)
            earliest_start = latest_start  # No lead time for immediate
        
        # ── Chain constraint: must finish before next upload ──
        if next_scheduled_at is not None:
            chained_latest = next_scheduled_at - _td(minutes=GLOBAL_GAP_MINUTES) - _td(minutes=ch_dur)
            latest_start = min(latest_start, chained_latest)
        
        # ── Pick the start time ──
        if latest_start < earliest_start:
            # Overcapacity: can't fit in lead window. Respect chain constraint
            # anyway to maintain realistic gaps between consecutive generations.
            # Violates lead_hours but prevents impossible 5-minute gaps.
            if next_scheduled_at is not None:
                scheduled_dt = next_scheduled_at - _td(minutes=GLOBAL_GAP_MINUTES) - _td(minutes=ch_dur)
            else:
                scheduled_dt = latest_start  # no chain constraint to respect
            logger.warning(
                "Overcapacity: %s slot #%d (pub=%s) — "
                "latest_start=%s < earliest_start=%s. Chaining with gap=%dmin.",
                s["channel_slug"], s["slot_position"],
                s["target_public_at"],
                latest_start.strftime("%m-%d %H:%M"),
                earliest_start.strftime("%m-%d %H:%M"),
                GLOBAL_GAP_MINUTES,
            )
        else:
            # Start as EARLY as possible within the window (clear the queue quickly)
            scheduled_dt = earliest_start
        
        # ── Floor: never schedule in the past ──
        # Without this, slots generated with lead_hours > time-to-public
        # get scheduled_at in the past. Only affects horizon-slot generation;
        # full_replan() has its own floor in its rebuild phase.
        if scheduled_dt < now + _td(minutes=1):
            scheduled_dt = now + _td(minutes=2)
        
        s["scheduled_at"] = scheduled_dt.strftime("%Y-%m-%d %H:%M:%S")
        next_scheduled_at = scheduled_dt
    
    # ── 3b. v26: re-chain floor-clamped (catch-up) slots sequentially ──
    # The floor above clamps EVERY overdue slot to now+2min, collapsing them
    # onto the same instant (observed: all of today's slots at 09:32:07).
    # Re-chain them back-to-back in forward order (earliest upload first) so
    # the dispatcher works through the backlog sequentially instead of seeing
    # a pile of identical scheduled_at values.
    floor_ts = (now + _td(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    catchup = [s for s in all_raw_slots if s["scheduled_at"] == floor_ts]
    if len(catchup) > 1:
        catchup.sort(key=lambda s: s["target_upload_at"])
        cursor = now + _td(minutes=2)
        for s in catchup:
            ch_dur = s["avg_duration_min"]
            s["scheduled_at"] = cursor.strftime("%Y-%m-%d %H:%M:%S")
            cursor = cursor + _td(minutes=ch_dur + GLOBAL_GAP_MINUTES)
        logger.info(
            "Catch-up re-chain: %d overdue slot(s) chained sequentially from %s "
            "through %s",
            len(catchup),
            (now + _td(minutes=2)).strftime("%m-%d %H:%M"),
            cursor.strftime("%m-%d %H:%M"),
        )
    
    # ── 3c. Enforce a monotonic generation chain across ALL slots ──
    # After the catch-up re-chain (and the backward walk), guarantee no slot
    # starts before the previous one finished: scheduled_i >= prev_finish + GAP.
    prev_end = now + _td(minutes=2)
    for s in sorted(all_raw_slots, key=lambda x: x["target_upload_at"]):
        sched_dt = _dt.strptime(s["scheduled_at"], "%Y-%m-%d %H:%M:%S")
        if sched_dt < prev_end:
            s["scheduled_at"] = prev_end.strftime("%Y-%m-%d %H:%M:%S")
        sched_dt = _dt.strptime(s["scheduled_at"], "%Y-%m-%d %H:%M:%S")
        prev_end = sched_dt + _td(minutes=s["avg_duration_min"] + GLOBAL_GAP_MINUTES)
    
    # ── 4. Sort final output by scheduled_at ────────────────────
    all_raw_slots.sort(key=lambda s: s["scheduled_at"])
    
    for pos, s in enumerate(all_raw_slots, 1):
        s["slot_position"] = pos
    
    # ── 5. Convert target_public_at from naive local → ISO8601 UTC ──
    for s in all_raw_slots:
        if s.get("target_public_at") and s.get("publish_mode") == "scheduled":
            tz_str = _get_slot_timezone(s)
            s["target_public_at"] = _naive_local_to_utc(s["target_public_at"], tz_str)
        elif not s.get("publish_mode") == "scheduled":
            s["target_public_at"] = None
    
    return all_raw_slots


def _get_slot_timezone(slot: dict) -> str:
    """Extract timezone string from a slot dict. Falls back to Europe/Madrid."""
    return slot.get("publish_timezone", "Europe/Madrid")


# ── Persistence layer ─────────────────────────────────────────

def _resolve_existing_collisions(
    slots: list[dict],
    date_str: str,
    db,
) -> None:
    """Check newly-computed slots against existing DB entries for the same
    channel on the same date, and push conflicting target_public_at forward.

    v10.3 (Aug 2026): Prevents replans from creating duplicate publish times
    when non-pending slots or already-created videos already occupy a time window.

    Modifies slots in-place if collisions are found.
    """
    import sqlite3
    from datetime import timezone as _tz, datetime as _dt, timedelta as _td

    min_gap = _td(hours=MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS)

    # Group slots by channel for efficient DB queries
    by_channel = {}
    for s in slots:
        ch_id = s.get("channel_id")
        if not ch_id or not s.get("target_public_at") or s.get("publish_mode") != "scheduled":
            continue
        by_channel.setdefault(ch_id, []).append(s)

    if not by_channel:
        return

    conn = None
    try:
        if hasattr(db, '_connect'):
            conn = db._connect()

        for ch_id, ch_slots in by_channel.items():
            # Query existing non-pending planned_slots for this channel+date
            existing_targets = []
            if conn:
                # Check 1: non-pending planned_slots
                rows = conn.execute("""
                    SELECT ps.target_public_at, ps.status
                    FROM planned_slots ps
                    WHERE ps.channel_id = ?
                      AND ps.date_key = ?
                      AND ps.status IN ('running', 'completed')
                      AND ps.target_public_at IS NOT NULL
                    ORDER BY ps.target_public_at
                """, (ch_id, date_str)).fetchall()
                for row in rows:
                    ts = row["target_public_at"]
                    if isinstance(ts, str):
                        try:
                            dt = _dt.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T"))
                        except (ValueError, TypeError):
                            continue
                    else:
                        dt = ts
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_tz.utc)
                    existing_targets.append((dt, f"slot({row['status']})"))

                # Check 2: videos with target_public_at on the same date
                rows2 = conn.execute("""
                    SELECT v.target_public_at, v.titulo_final, v.status
                    FROM videos v
                    WHERE v.channel_id = ?
                      AND v.target_public_at IS NOT NULL
                      AND date(v.target_public_at) = ?
                    ORDER BY v.target_public_at
                """, (ch_id, date_str)).fetchall()
                for row in rows2:
                    ts = row["target_public_at"]
                    if isinstance(ts, str):
                        try:
                            dt = _dt.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T"))
                        except (ValueError, TypeError):
                            continue
                    else:
                        dt = ts
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_tz.utc)
                    existing_targets.append((dt, f"video({row['status']})"))

            if not existing_targets:
                continue

            # Sort existing targets by time
            existing_targets.sort(key=lambda x: x[0])

            # For each new slot, check against existing targets
            for s in ch_slots:
                tpa_str = s.get("target_public_at")
                if not tpa_str:
                    continue
                try:
                    if isinstance(tpa_str, str):
                        tpa = _dt.fromisoformat(tpa_str.replace("Z", "+00:00").replace(" ", "T"))
                    else:
                        tpa = tpa_str
                    if tpa.tzinfo is None:
                        tpa = tpa.replace(tzinfo=_tz.utc)
                except (ValueError, TypeError):
                    continue

                # Find the closest existing target after ours
                adjusted = tpa
                for ex_dt, ex_label in existing_targets:
                    gap = abs((adjusted - ex_dt).total_seconds()) / 3600.0
                    if gap < MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS:
                        # Collision! Push our slot forward to after the existing one
                        pushed = ex_dt + min_gap
                        logger.info(
                            "_resolve_existing_collisions: [ch=%d] slot target %s conflicts "
                            "with existing %s (%s, gap=%.1fh). Pushing to %s.",
                            ch_id,
                            tpa_str[:19] if isinstance(tpa_str, str) else str(tpa)[:19],
                            ex_label,
                            str(ex_dt)[:19],
                            gap,
                            str(pushed)[:19],
                        )
                        adjusted = pushed

                if adjusted != tpa:
                    s["target_public_at"] = adjusted.isoformat()
                    # Also adjust target_upload_at (back out warmup + pipeline)
                    # to maintain ordering: target_upload_at < target_public_at
                    warmup_min = s.get("warmup_min", 120)
                    reverse_min = ESTIMATED_PIPELINE_MINUTES + warmup_min
                    upload_dt = adjusted - _td(minutes=reverse_min)
                    s["target_upload_at"] = upload_dt.isoformat()
    except Exception as e:
        logger.debug("_resolve_existing_collisions: skipped (non-fatal): %s", e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@_replan_locked
def compute_and_store_slots(
    date_str: Optional[str] = None,
    db=None,
) -> dict:
    """Compute slots for a single date and store them in planned_slots.
    
    Legacy single-day planning — for backward compatibility.
    Prefer compute_and_store_horizon() for scheduled channels.
    
    Args:
        date_str: date to plan (defaults to today).
        db: ExtendedDatabase instance (auto-created if None).
    
    Returns:
        dict with {date, total_slots, slots_by_channel}.
    """
    if date_str is None:
        date_str = date.today().isoformat()
    
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    
    # Get all active channels and their planning config
    channels = db.get_channels(active_only=True)
    channel_configs = []
    for ch in channels:
        # v26: skip channels paused by the circuit breaker (vpd=0 / alert active)
        if _channel_paused_for_planning(db, ch["id"]):
            continue
        cfg = db.get_channel_planning_config(ch["id"])
        if cfg.get("planning_enabled", True) and cfg.get("videos_per_day", 0) > 0:
            channel_configs.append(cfg)
    
    if not channel_configs:
        logger.info("compute_and_store_slots(%s): no active planning channels", date_str)
        return {"date": date_str, "total_slots": 0, "slots_by_channel": {}}
    
    # Delete existing PENDING slots for this date (to allow full replan)
    with db._connect() as conn:
        conn.execute(
            "DELETE FROM planned_slots WHERE date_key = ? AND status = 'pending'",
            (date_str,),
        )
        conn.commit()
    
    # Compute slots
    slots = compute_daily_slots(date_str, channel_configs, db=db)

    # ── v24 (Aug 2026): Topic-level dedup ──
    # Prevent creating multiple slots for the same topic by checking
    # recent (7-day) planned_slots and published videos for content overlap.
    # Uses source_url + primary keyword hash for fast comparison.
    slots = _deduplicate_slots_by_topic(slots, db)
    
    # ── v10.3: Resolve collisions with already-existing slots/videos ──
    # Check against non-pending planned_slots AND existing videos with 
    # target_public_at on the same date. This prevents replans from 
    # creating duplicate publish times.
    _resolve_existing_collisions(slots, date_str, db)
    
    # Store them
    stored = db.create_planned_slots_batch(slots)
    
    slots_by_channel = {}
    for s in slots:
        ch_id = s["channel_id"]
        if ch_id not in slots_by_channel:
            slots_by_channel[ch_id] = []
        slots_by_channel[ch_id].append(s.get("target_upload_at", "")[11:16])  # show upload times in log
    
    logger.info(
        "Planned %s: %d slots across %d channels → %s",
        date_str, stored, len(slots_by_channel),
        {ch_id: times for ch_id, times in slots_by_channel.items()},
    )

    # ── v40: este "Replan día" manual cuenta como último replan de horizonte:
    # resetea el gate de 24h (DB + global en memoria para el cooldown de 5 min).
    global _last_horizon_replan_ts
    _last_horizon_replan_ts = datetime.now()
    try:
        db.set_system_state(_LAST_REPLAN_KEY, str(time.time()))
    except Exception:
        pass

    return {
        "date": date_str,
        "total_slots": stored,
        "slots_by_channel": slots_by_channel,
    }


def _deduplicate_slots_by_topic(slots: list[dict], db) -> list[dict]:
    """Remove slots whose topic already appears in recent slots or published videos.

    v24 (Aug 2026): Prevents planning from creating multiple slots for the
    same content topic — a common source of duplicate video uploads when
    replans or horizon extensions generate slots from the same sources.

    Detection uses a lightweight content hash combining:
      - source_url (URL of the original content source)
      - primary keyword / search query
      - channel_id

    A slot is skipped if a matching hash exists in:
      - planned_slots from the last 7 days (any status)
      - videos with yt_video_id from the last 7 days

    Returns filtered slot list.
    """
    import hashlib

    def _slot_hash(slot: dict) -> str:
        """Create a stable content hash for a slot."""
        source = slot.get("source_url", "")
        keyword = slot.get("primary_keyword", slot.get("search_query", ""))
        channel = str(slot.get("channel_id", ""))
        raw = f"{source}|{keyword}|{channel}"
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]

    # Collect existing topic hashes from recent slots and videos
    existing_hashes: set[str] = set()
    try:
        with db._connect() as conn:
            # Recent planned_slots (any status, last 7 days)
            recent_slots = conn.execute(
                """SELECT source_url, channel_id
                   FROM planned_slots
                   WHERE scheduled_at >= datetime('now', '-7 days')
                     AND source_url IS NOT NULL AND source_url != ''"""
            ).fetchall()
            for rs in recent_slots:
                raw = f"{rs['source_url'] or ''}||{rs['channel_id']}"
                h = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
                existing_hashes.add(h)

            # Recent published videos (last 7 days, with yt_video_id)
            recent_vids = conn.execute(
                """SELECT source_url, channel_id
                   FROM videos
                   WHERE yt_video_id IS NOT NULL AND yt_video_id != ''
                     AND created_at >= datetime('now', '-7 days')
                     AND source_url IS NOT NULL AND source_url != ''"""
            ).fetchall()
            for rv in recent_vids:
                raw = f"{rv['source_url'] or ''}||{rv['channel_id']}"
                h = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
                existing_hashes.add(h)
    except Exception as e:
        logger.debug("Topic dedup: could not load existing hashes: %s", e)
        return slots  # Fail open — don't block planning

    filtered: list[dict] = []
    skipped = 0
    for slot in slots:
        h = _slot_hash(slot)
        if h in existing_hashes:
            skipped += 1
            logger.debug(
                "Topic dedup: skipping slot for ch=%s src=%s (hash=%s — already exists)",
                slot.get("channel_id", "?"),
                (slot.get("source_url") or "?")[:50],
                h,
            )
            continue
        filtered.append(slot)
        existing_hashes.add(h)  # Prevent duplicates within the same batch

    if skipped:
        logger.info(
            "Topic dedup: skipped %d/%d slots that would duplicate recent content",
            skipped, skipped + len(filtered),
        )
    return filtered


def _augment_channel_configs(db, channel_configs: list[dict]) -> list[dict]:
    """Add per-channel avg_duration_min from historical timing data."""
    from api.services.schedule_engine import get_avg_creation_minutes
    
    for cfg in channel_configs:
        ch_id = cfg["channel_id"]
        try:
            avg = get_avg_creation_minutes(ch_id)
            cfg["avg_duration_min"] = avg * (1 + BUFFER_PCT)  # buffered
        except Exception:
            cfg["avg_duration_min"] = ESTIMATED_PIPELINE_MINUTES
    return channel_configs


@_replan_locked
def compute_and_store_horizon(
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    db=None,
    force_replan: bool = False,
) -> dict:
    """Plan and store all slots across the N-day horizon using 3-phase cross-day chaining.
    
    Replaces the old per-day planning loop. Drops and recreates ALL pending
    slots across the horizon to ensure a fresh, coherent plan.
    
    Args:
        horizon_days: days to plan (including today).
        db: ExtendedDatabase instance.
        force_replan: if True, also drop running slots (use with care).
    
    Returns:
        dict with {total_slots, days_planned, slots_by_channel}.
    """
    from datetime import date as _date

    now = datetime.now()

    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    # ── Post-full_replan quiet-window guard (NOT bypassable by force_replan) ──
    # v40: la ventana silenciosa se persiste en system_state para que un
    # reinicio de la API no la pierda.
    global _post_full_replan_block_until
    if _post_full_replan_block_until is None:
        try:
            _raw_q = db.get_system_state("post_full_replan_block_until")
            if _raw_q:
                _parsed_q = float(_raw_q)
                if _parsed_q > now.timestamp():  # solo hidratar si sigue vigente
                    _post_full_replan_block_until = datetime.fromtimestamp(_parsed_q)
        except Exception:
            pass

    if _post_full_replan_block_until is not None:
        if now < _post_full_replan_block_until:
            remaining = int((_post_full_replan_block_until - now).total_seconds() / 60)
            logger.info(
                "compute_and_store_horizon: blocked — post-full_replan quiet window, "
                "%d min remaining",
                remaining,
            )
            return {
                "total_slots": 0,
                "days_planned": 0,
                "slots_by_channel": {},
                "skipped": True,
                "reason": f"post-full_replan quiet ({remaining} min remaining)",
            }
        else:
            _post_full_replan_block_until = None  # clear expired
            try:
                db.set_system_state("post_full_replan_block_until", "")
            except Exception:
                pass

    # ── Cooldown guard ──────────────────────────────────
    # v40: hidratar el cooldown desde DB si arrancamos con el global vacío,
    # para que el cooldown de 5 min sobreviva a reinicios de la API.
    global _last_horizon_replan_ts
    if _last_horizon_replan_ts is None:
        try:
            _raw_ts = db.get_system_state(_LAST_REPLAN_KEY)
            if _raw_ts:
                _parsed_ts = float(_raw_ts)
                if _parsed_ts <= now.timestamp():
                    _last_horizon_replan_ts = datetime.fromtimestamp(_parsed_ts)
        except Exception:
            pass
    if _last_horizon_replan_ts is not None and not force_replan:
        elapsed = (now - _last_horizon_replan_ts).total_seconds() / 60
        if elapsed < _HORIZON_REPLAN_COOLDOWN_MIN:
            logger.info(
                "compute_and_store_horizon: skipped — last replan %.0f min ago (cooldown=%d min)",
                elapsed, _HORIZON_REPLAN_COOLDOWN_MIN,
            )
            return {
                "total_slots": 0,
                "days_planned": 0,
                "slots_by_channel": {},
                "skipped": True,
                "reason": f"cooldown ({_HORIZON_REPLAN_COOLDOWN_MIN} min)",
            }
    _last_horizon_replan_ts = now
    # Persistir el replan SOLO cuando realmente ejecuta (después de los guards
    # de quiet-window y cooldown) — es el timestamp que gobierna el gate de 24h.
    try:
        db.set_system_state(_LAST_REPLAN_KEY, str(now.timestamp()))
    except Exception:
        pass
    # ────────────────────────────────────────────────────

    # Get all active channels and their planning config
    channels = db.get_channels(active_only=True)
    channel_configs = []
    for ch in channels:
        # v26: skip channels paused by the circuit breaker (vpd=0 / alert active)
        if _channel_paused_for_planning(db, ch["id"]):
            continue
        cfg = db.get_channel_planning_config(ch["id"])
        if cfg.get("planning_enabled", True) and cfg.get("videos_per_day", 0) > 0:
            channel_configs.append(cfg)
    
    if not channel_configs:
        logger.info("compute_and_store_horizon: no active planning channels")
        return {"total_slots": 0, "days_planned": 0, "slots_by_channel": {}}
    
    # Augment with real durations
    channel_configs = _augment_channel_configs(db, channel_configs)
    
    today = _date.today()
    
    # Delete existing PENDING slots across the horizon
    with db._connect() as conn:
        start_date = today.isoformat()
        end_date = (today + timedelta(days=horizon_days)).isoformat()
        deleted = conn.execute(
            "DELETE FROM planned_slots WHERE date_key >= ? AND date_key < ? AND status = 'pending'",
            (start_date, end_date),
        ).rowcount
        if force_replan:
            # Also cancel running slots that are stale (no job)
            conn.execute(
                "UPDATE planned_slots SET status = 'cancelled' "
                "WHERE date_key >= ? AND date_key < ? AND status = 'running' "
                "AND job_id IS NULL",
                (start_date, end_date),
            )
        conn.commit()
        if deleted:
            logger.info("Horizon replan: cleared %d pending slots", deleted)
    
    # Compute horizon slots
    slots = compute_horizon_slots(channel_configs, horizon_days=horizon_days, db=db)
    
    if not slots:
        logger.info("compute_and_store_horizon: no slots to plan")
        return {"total_slots": 0, "days_planned": 0, "slots_by_channel": {}}
    
    # Store them
    stored = db.create_planned_slots_batch(slots)
    
    # Collect stats
    slots_by_channel = {}
    days_used = set()
    for s in slots:
        ch_id = s["channel_id"]
        if ch_id not in slots_by_channel:
            slots_by_channel[ch_id] = []
        slots_by_channel[ch_id].append({
            "date": s["date_key"],
            "gen": s["scheduled_at"][11:16],
            "upload": s["target_upload_at"][11:16],
            "public": s["target_public_at"][11:16],
        })
        days_used.add(s["date_key"])
    
    # Log summary
    for ch_id, items in slots_by_channel.items():
        slug = items[0] if items else "?"
        logger.info(
            "  %s: %d slots, gen starts: %s",
            next(s["channel_slug"] for s in slots if s["channel_id"] == ch_id) if slots else "?",
            len(items),
            ", ".join(f"{i['date']}@{i['gen']}" for i in items),
        )
    
    logger.info(
        "Horizon planned: %d days, %d slots across %d channels",
        len(days_used), stored, len(slots_by_channel),
    )
    
    # ── Overcapacity check ──
    total_gen_min = sum(s["avg_duration_min"] + GLOBAL_GAP_MINUTES for s in slots)
    available_hours = horizon_days * 24
    if total_gen_min > available_hours * 60 * 0.9:  # >90% capacity
        logger.warning(
            "⚠️  OVERCAPACITY RISK: %.0f h of generation needed for %d days "
            "(%.1f%% of available time). Consider reducing videos_per_day or increasing lead_hours.",
            total_gen_min / 60, horizon_days,
            (total_gen_min / (available_hours * 60)) * 100,
        )
    
    return {
        "total_slots": stored,
        "days_planned": len(days_used),
        "slots_by_channel": {ch_id: len(items) for ch_id, items in slots_by_channel.items()},
    }


@_replan_locked
def sync_midday(db=None) -> dict:
    """Mid-day reconciliation: adapt planning if channel configs changed.
    
    Called every 5 min by the checker loop.  Checks:
    1. If today has no planned_slots yet → compute all
    2. If a channel's videos_per_day increased → add new pending slots
       in remaining windows
    3. If decreased → cancel excess pending slots
    4. If planning_enabled toggled off → cancel all pending
    
    Returns the updated state.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    
    today = date.today().isoformat()
    existing = db.get_planned_slots(date_key=today)
    
    # Case 1: No slots at all for today → full plan
    if not existing:
        return compute_and_store_slots(today, db)
    
    # Get current channel configs
    channels = db.get_channels(active_only=True)
    result = {"date": today, "added": 0, "cancelled": 0, "replanned_channels": []}

    # ── Quota-aware (ago 2026): cupo por proyecto para hoy ──
    alloc = {}
    try:
        alloc = compute_daily_upload_allocation(db, today)
    except Exception:
        pass
    
    for ch in channels:
        cfg = db.get_channel_planning_config(ch["id"])
        ch_id = ch["id"]
        target = _resolve_videos_per_day(cfg, today) if cfg.get("planning_enabled", True) else 0
        # Cap de cuota: nunca planificar más longs de los que el proyecto admite
        target = _resolve_long_cap(alloc, ch.get("slug", ""), target)
        
        # Current slots for this channel today
        ch_slots = [s for s in existing if s["channel_id"] == ch_id]
        
        # Count real publications (includes manual videos) + active slots only.
        # NOT counting "completed" slots — they may be error videos (pre-fix)
        # or already accounted for via published_today (avoiding double count).
        published_today = db.get_videos_published_today(ch_id)
        running = sum(1 for s in ch_slots if s["status"] == "running")
        pending = [s for s in ch_slots if s["status"] == "pending"]
        
        current_planned = published_today + running + len(pending)
        target_planned = target
        
        if current_planned == target_planned:
            continue  # No change needed
        
        if current_planned > target_planned:
            # Too many: cancel the last N pending slots.
            # Sort so 'original' slots are cancelled before 'viral' slots,
            # preserving viral quota whenever possible.
            excess = current_planned - target_planned
            pending.sort(key=lambda s: 0 if s.get("source_mode") == "viral" else 1)
            to_cancel = pending[:excess] if excess <= len(pending) else pending
            if to_cancel:
                ids = [s["id"] for s in to_cancel]
                cancelled = db.cancel_slots(ids)
                result["cancelled"] += cancelled
                result["replanned_channels"].append({
                    "channel_id": ch_id,
                    "slug": ch["slug"],
                    "action": "cancelled",
                    "count": cancelled,
                })
        
        elif current_planned < target_planned:
            # Not enough: add new slots in remaining windows
            needed = target_planned - current_planned
            if needed <= 0:
                continue
            
            # Find the last scheduled_at for all non-cancelled slots today
            all_sorted = sorted(existing, key=lambda s: s["scheduled_at"])
            last_sched = None
            for s in reversed(all_sorted):
                if s["status"] != "cancelled":
                    last_sched = s["scheduled_at"]
                    break
            
            # Compute full day for this channel alone (already includes collisions internally)
            day_slots = compute_daily_slots(today, [{
                "channel_id": ch_id,
                "slug": ch["slug"],
                "name": ch["name"],
                "videos_per_day": target,
                "planning_enabled": True,
                "publish_mode": cfg.get("publish_mode", "immediate"),
                "publish_target_hour": cfg.get("publish_target_hour"),
                "publish_jitter_min": cfg.get("publish_jitter_min", 20),
                "publish_warmup_min": cfg.get("publish_warmup_min", 120),
                "publish_timezone": cfg.get("publish_timezone", "Europe/Madrid"),
                "seo_primary_keyword": cfg.get("seo_primary_keyword", ""),
                "seo_secondary_keywords": cfg.get("seo_secondary_keywords", []),
                "viral_per_day": cfg.get("viral_per_day", 0),
            }], db=db)
            
            # Filter: only take slots NOT already covered by existing non-cancelled slots
            new_slots = []
            existing_positions = {
                es["slot_position"]
                for es in ch_slots
                if es["status"] != "cancelled"
            }
            for s in day_slots:
                if s["slot_position"] not in existing_positions:
                    new_slots.append(s)
            new_slots = new_slots[:needed]
            
            # Ensure new slots don't collide with today's existing non-cancelled slots
            for ns in new_slots:
                if last_sched:
                    lh, lm = map(int, last_sched[11:16].split(":"))
                    sh, sm = map(int, ns["scheduled_at"][11:16].split(":"))
                    diff = (sh * 60 + sm) - (lh * 60 + lm)
                    if diff < MIN_GAP_MINUTES:
                        # Push generation start forward
                        new_gen = lh * 60 + lm + MIN_GAP_MINUTES
                        nh = min(new_gen // 60, 23)
                        nm = min(new_gen % 60, 59)
                        ns["scheduled_at"] = f"{today} {nh:02d}:{nm:02d}:00"
                        up_t = nh * 60 + nm + ESTIMATED_PIPELINE_MINUTES
                        uh = min(up_t // 60, 23)
                        um = min(up_t % 60, 59)
                        ns["target_upload_at"] = f"{today} {uh:02d}:{um:02d}:00"
                last_sched = ns["scheduled_at"]
            
            db.create_planned_slots_batch(new_slots)
            result["added"] += len(new_slots)
            result["replanned_channels"].append({
                "channel_id": ch_id,
                "slug": ch["slug"],
                "action": "added",
                "count": len(new_slots),
            })
    
    if result["added"] > 0 or result["cancelled"] > 0:
        logger.info(
            "Mid-day sync(%s): +%d added, -%d cancelled → %s",
            today, result["added"], result["cancelled"],
            [r["slug"] for r in result["replanned_channels"]],
        )
    
    return result


# ── Dry-run preview ───────────────────────────────────────────

def preview_week(overrides: dict = None, db=None) -> dict:
    """Simulate 7 days of planning WITHOUT persisting.
    
    Args:
        overrides: dict mapping slug → {videos_per_day, planning_enabled} to
                   simulate config changes.
    
    Returns:
        dict with {days: [{date, slots: [{time, channel_name, channel_slug}]}]}
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    
    channels = db.get_channels(active_only=True)
    channel_configs = []
    
    for ch in channels:
        cfg = db.get_channel_planning_config(ch["id"])
        
        # Apply overrides if any
        slug = ch["slug"]
        if overrides and slug in overrides:
            ov = overrides[slug]
            if "videos_per_day" in ov:
                cfg["videos_per_day"] = ov["videos_per_day"]
            if "planning_enabled" in ov:
                cfg["planning_enabled"] = ov["planning_enabled"]
        
        if cfg.get("planning_enabled") and cfg.get("videos_per_day", 0) > 0:
            channel_configs.append(cfg)
    
    today = date.today()
    days = []
    
    for offset in range(7):
        d = today + timedelta(days=offset)
        date_str = d.isoformat()
        slots = compute_daily_slots(date_str, channel_configs, db=db)
        
        days.append({
            "date": date_str,
            "weekday": d.strftime("%a"),
            "slots": [
                {
                    "gen_start": s["scheduled_at"][11:16],
                    "upload": s["target_upload_at"][11:16] if s.get("target_upload_at") else None,
                    "channel_name": s["channel_name"],
                    "channel_slug": s["channel_slug"],
                    "channel_id": s["channel_id"],
                }
                for s in slots
            ],
        })
    
    return {"days": days, "overrides_applied": bool(overrides)}


# ── Smart replanning (v2) ─────────────────────────────────────

def _cancel_excess_pending_by_quota(db) -> int:
    """Cancelar slots pendientes de HOY que excedan el cupo del proyecto.

    Cuando el cupo se reduce (subidas ya reservadas/consumidas durante el día),
    los slots pendientes sobrantes nunca podrían subirse sin saltar el breaker.
    Política de cancelación: shorts nativos primero (prioridad baja), luego
    longs de los canales con más slots pendientes. Nunca toca running/completed.

    Returns: nº de slots cancelados.
    """
    today = date.today().isoformat()
    try:
        alloc = compute_daily_upload_allocation(db, today)
    except Exception:
        return 0
    if not alloc:
        return 0

    try:
        channels = db.get_channels(active_only=True) or []
    except Exception:
        return 0
    slug_to_ch = {ch.get("slug"): ch for ch in channels if ch.get("slug")}

    cancelled = 0
    for proj, data in alloc.items():
        capacity = int(data.get("max_uploads", 0))
        ch_ids = []
        for slug in data.get("channels", {}).keys():
            ch = slug_to_ch.get(slug)
            if ch and ch.get("id"):
                ch_ids.append(int(ch["id"]))
        if not ch_ids:
            continue
        ph = ",".join("?" for _ in ch_ids)
        try:
            with db._connect() as conn:
                long_pending = conn.execute(
                    f"""SELECT id FROM planned_slots
                        WHERE status = 'pending' AND date_key = ?
                          AND channel_id IN ({ph})
                        ORDER BY scheduled_at ASC""",
                    (today, *ch_ids),
                ).fetchall()
                short_pending = conn.execute(
                    f"""SELECT id FROM shorts_planned_slots
                        WHERE status = 'pending' AND date_key = ?
                          AND channel_id IN ({ph})
                        ORDER BY scheduled_at ASC""",
                    (today, *ch_ids),
                ).fetchall()
        except Exception:
            continue
        total_pending = len(long_pending) + len(short_pending)
        excess = total_pending - capacity
        if excess <= 0:
            continue

        shorts_to_cancel = [r["id"] for r in short_pending[:excess]]
        longs_to_cancel = []
        if len(shorts_to_cancel) < excess:
            longs_to_cancel = [
                r["id"] for r in long_pending[: excess - len(shorts_to_cancel)]
            ]

        if shorts_to_cancel:
            try:
                with db._connect() as conn:
                    ph2 = ",".join("?" for _ in shorts_to_cancel)
                    conn.execute(
                        f"""UPDATE shorts_planned_slots SET status = 'cancelled'
                            WHERE id IN ({ph2})""",
                        shorts_to_cancel,
                    )
                    conn.commit()
                cancelled += len(shorts_to_cancel)
            except Exception:
                pass
        if longs_to_cancel:
            try:
                with db._connect() as conn:
                    ph3 = ",".join("?" for _ in longs_to_cancel)
                    conn.execute(
                        f"""UPDATE planned_slots SET status = 'cancelled'
                            WHERE id IN ({ph3})""",
                        longs_to_cancel,
                    )
                    conn.commit()
                cancelled += len(longs_to_cancel)
            except Exception:
                pass
    return cancelled


# ── Smart replanning (v2) ─────────────────────────────────────

# Never drop below this many pending slots across all channels.
# If below threshold, auto-trigger a full horizon replan.
MIN_PENDING_SLOTS = 3

# Days ahead to check for empty coverage (no slots at all for that day)
EMPTY_DAY_CHECK_DAYS = 3


@_replan_locked
def smart_replan(db=None) -> dict:
    """Periodic intelligent replan: cancel stale slots, detect config changes,
    fill empty days, warn on overcapacity.

    Contrato (#5, v40):
      - Checks ligeros cada 30 min (10:00-23:00): cancelar stale, detectar
        cambio de config, fill días vacíos, alerta overcapacity.
      - El rebuild completo del horizonte vive en el gate periódico de 24h
        (HORIZON_REPLAN_INTERVAL_HOURS) o en los triggers de emergencia
        (pipeline seco < MIN_PENDING_SLOTS, día vacío, cambio de config,
        _ensure_never_dry).
      - El exceso de cuota se poda una vez por día en medianoche PT
        (ver _cancel_excess_pending_by_quota), no aquí.

    Returns dict with: {cancelled_count, horiz_replan, channels_adjusted, overcapacity_warn}
    """
    import json as _json
    from datetime import date as _date, datetime as _dt, timedelta as _td
    
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    
    today = _date.today().isoformat()
    now = _dt.now()
    cancelled_total = 0
    channels_adjusted = []
    overcapacity_warn = False
    horizon_replanned = False
    
    # ── 0. Count total pending slots ──
    with db._connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM planned_slots WHERE status='pending'"
        ).fetchone()
        total_pending = row["cnt"] if row else 0
    
    # ── 0a. Auto-replan if pipeline is running dry ──
    if total_pending < MIN_PENDING_SLOTS:
        logger.warning(
            "Pipeline running DRY: %d pending slots (min=%d). "
            "Triggering full horizon replan.",
            total_pending, MIN_PENDING_SLOTS,
        )
        result = compute_and_store_horizon(horizon_days=7, db=db, force_replan=True)
        horizon_replanned = True
        logger.info(
            "Auto-replan complete: %d slots across %d days",
            result.get("total_slots", 0), result.get("days_planned", 0),
        )
        # Refresh pending count
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM planned_slots WHERE status='pending'"
            ).fetchone()
            total_pending = row["cnt"] if row else 0
    
    # ── 0b. Detect empty days in the horizon ──
    need_replan = False
    for day_offset in range(EMPTY_DAY_CHECK_DAYS):
        check_day = (_date.today() + _td(days=day_offset)).isoformat()
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM planned_slots WHERE date_key=? AND status='pending'",
                (check_day,),
            ).fetchone()
            day_count = row["cnt"] if row else 0
        if day_count == 0:
            logger.warning(
                "Empty day detected: %s has 0 pending slots. Triggering horizon replan.",
                check_day,
            )
            need_replan = True
            break
    
    if need_replan and not horizon_replanned:
        result = compute_and_store_horizon(horizon_days=7, db=db, force_replan=True)
        horizon_replanned = True
        logger.info(
            "Empty-day replan complete: %d slots across %d days",
            result.get("total_slots", 0), result.get("days_planned", 0),
        )
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM planned_slots WHERE status='pending'"
            ).fetchone()
            total_pending = row["cnt"] if row else 0
    
    # ── 1. Load all active channels ──
    channels = db.get_channels(active_only=True)
    if not channels:
        return {"cancelled_count": 0, "horiz_replan": horizon_replanned,
                "channels_adjusted": [], "overcapacity_warn": False}
    
    for ch in channels:
        ch_id = ch.id if hasattr(ch, 'id') else ch.get("id", 0)
        slug = ch.slug if hasattr(ch, 'slug') else ch.get("slug", "?")
        cfg_raw = ch.config_json if hasattr(ch, 'config_json') else ch.get("config_json", "{}")
        try:
            cfg = _json.loads(cfg_raw) if isinstance(cfg_raw, str) else (cfg_raw or {})
        except (_json.JSONDecodeError, TypeError):
            cfg = {}
        
        vpd = _resolve_videos_per_day({**cfg, "channel_id": ch_id}, today)
        planning_enabled = cfg.get("planning_enabled", True)
        
        # ── v26: skip channels paused by the circuit breaker ──
        if _channel_paused_for_planning(db, ch_id):
            with db._connect() as conn:
                cnt = conn.execute(
                    "UPDATE planned_slots SET status='cancelled' WHERE channel_id=? AND status='pending'",
                    (ch_id,),
                ).rowcount
                conn.commit()
            if cnt > 0:
                logger.info(
                    "Smart replan: cancelled %d slots for %s (circuit breaker — channel paused)",
                    cnt, slug,
                )
                cancelled_total += cnt
                channels_adjusted.append(slug)
            continue
        
        if not planning_enabled:
            with db._connect() as conn:
                cnt = conn.execute(
                    "UPDATE planned_slots SET status='cancelled' WHERE channel_id=? AND status='pending'",
                    (ch_id,),
                ).rowcount
                conn.commit()
            if cnt > 0:
                logger.info("Smart replan: cancelled %d slots for %s (planning disabled)", cnt, slug)
                cancelled_total += cnt
                channels_adjusted.append(slug)
            continue
        
        # ── 2a. Count videos done today ──
        generated_today = db.count_videos_generated_today(ch_id)
        
        # ── 2b. Cancel today's slots if quota met ──
        with db._connect() as conn:
            today_cnt = conn.execute(
                "SELECT COUNT(*) as cnt FROM planned_slots WHERE channel_id=? AND date_key=? AND status='pending'",
                (ch_id, today),
            ).fetchone()
            today_pending = today_cnt["cnt"] if today_cnt else 0
        
        if generated_today >= vpd and today_pending > 0:
            with db._connect() as conn:
                cnt = conn.execute(
                    "UPDATE planned_slots SET status='cancelled' WHERE channel_id=? AND date_key=? AND status='pending'",
                    (ch_id, today),
                ).rowcount
                conn.commit()
            logger.info(
                "Smart replan: cancelled %d today-slots for %s (quota met: %d/%d)",
                cnt, slug, generated_today, vpd,
            )
            cancelled_total += cnt
            channels_adjusted.append(slug)
        
        # ── 2c. Detect config change vs tomorrow's slots ──
        if not horizon_replanned:
            tomorrow_str = (_date.today() + _td(days=1)).isoformat()
            with db._connect() as conn:
                tomorrow_cnt = conn.execute(
                    "SELECT COUNT(*) as cnt FROM planned_slots WHERE channel_id=? AND date_key=? AND status='pending'",
                    (ch_id, tomorrow_str),
                ).fetchone()
                tcnt = tomorrow_cnt["cnt"] if tomorrow_cnt else 0
            if tcnt > 0 and tcnt != _resolve_videos_per_day({**cfg, "channel_id": ch_id}, tomorrow_str):
                logger.warning(
                    "Config mismatch for %s: tomorrow has %d slots but resolved_vpd=%d. "
                    "Triggering horizon replan.",
                    slug, tcnt, _resolve_videos_per_day({**cfg, "channel_id": ch_id}, tomorrow_str),
                )
                if not horizon_replanned:
                    result = compute_and_store_horizon(horizon_days=7, db=db, force_replan=True)
                    horizon_replanned = True
                    logger.info(
                        "Config-change replan complete: %d slots",
                        result.get("total_slots", 0),
                    )
        
        # ── 2d. Cancel stale slots — only for today/past dates.
        #     Future-date slots with old scheduled_at are pre-generated
        #     buffer slots and should NOT be cancelled.
        with db._connect() as conn:
            stale_cnt = conn.execute(
                """UPDATE planned_slots SET status='cancelled'
                   WHERE channel_id=? AND status='pending'
                     AND date_key <= date('now', 'localtime')
                     AND scheduled_at <= datetime('now', 'localtime', '-2 hours')""",
                (ch_id,),
            ).rowcount
            conn.commit()
        if stale_cnt > 0:
            logger.info("Smart replan: cancelled %d stale slots for %s (today/past only)", stale_cnt, slug)
            cancelled_total += stale_cnt
    
    # ── 3. Overcapacity check ──
    daily_capacity = 8  # with pipelining
    max_realistic = daily_capacity * 2  # 2 days worth
    
    # Refresh count if replan happened
    if not horizon_replanned:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM planned_slots WHERE status='pending'"
            ).fetchone()
            total_pending = row["cnt"] if row else 0
    
    if total_pending > max_realistic:
        logger.warning(
            "Smart replan: overcapacity — %d pending slots vs ~%d realistic (2 days).",
            total_pending, max_realistic,
        )
        overcapacity_warn = True
    
    return {
        "cancelled_count": cancelled_total,
        "horiz_replan": horizon_replanned,
        "channels_adjusted": list(set(channels_adjusted)),
        "overcapacity_warn": overcapacity_warn,
        "pending_total": total_pending,
    }


# ── Scheduler integration ─────────────────────────────────────

def _score_priority_slot(slot: dict, db, today_str: str) -> int:
    """Score a slot for priority dispatch. Higher = dispatch first.
    
    Factors:
      - date_key proximity: today=100, tomorrow=70, +2=40, +3+=10
      - deadline urgency: past-due but still viable → +50
      - channel fairness: no video generated today → +30
      - buffer pressure: exceeding max_awaiting_upload → -30
      - priority_weight: per-channel multiplier (from config_json, default 1.0)
    """
    from datetime import date, datetime
    
    score = 0
    slot_date = slot["date_key"]
    ch_id = slot["channel_id"]
    
    # ── Load channel config overrides ──
    cfg_json = slot.get("config_json", "{}")
    if isinstance(cfg_json, str):
        import json as _j
        try:
            cfg_json = _j.loads(cfg_json)
        except Exception:
            cfg_json = {}
    elif cfg_json is None:
        cfg_json = {}
    
    max_awaiting = int(cfg_json.get("max_awaiting_upload", 3) or 3)
    priority_weight = float(cfg_json.get("priority_weight", 1.0) or 1.0)
    
    # ── 1. Date proximity ──
    days_ahead = (datetime.strptime(slot_date, "%Y-%m-%d").date() - date.today()).days
    if days_ahead == 0:
        score += 100
    elif days_ahead == 1:
        score += 70
    elif days_ahead == 2:
        score += 40
    else:
        score += max(5, 30 - days_ahead * 5)
    
    # ── 2. Deadline urgency ──
    upload_at = slot.get("target_upload_at")
    if upload_at:
        try:
            upload_dt = datetime.strptime(str(upload_at)[:19], "%Y-%m-%d %H:%M:%S")
            if upload_dt < datetime.now():
                score += 50  # catch-up bonus: this is late but still viable
        except (ValueError, TypeError):
            pass
    
    # ── 3. Channel fairness: channel with nothing generated today gets priority ──
    try:
        generated_today = db.count_videos_generated_today(ch_id)
        if generated_today == 0:
            vpd = _resolve_videos_per_day({**cfg_json, "channel_id": ch_id}, today_str)
            if vpd > 0:
                score += 30  # channel needs content today
    except Exception:
        pass
    
    # ── 4. Buffer pressure: too many awaiting → lower priority ──
    try:
        awaiting = db.count_awaiting_upload(ch_id)
        if awaiting >= max_awaiting:
            score -= 30
    except Exception:
        pass
    
    # ── 5. Apply channel priority weight ──
    if priority_weight != 1.0:
        score = int(score * priority_weight)
    
    return score


def _get_active_channel_ids(db) -> list[int]:
    """Get ordered list of ACTIVE channel IDs for round-robin dispatch.

    v26 fix: previously included inactive/test channels (e.g. the "test"
    channel), skewing the round-robin rotation.
    """
    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM channels WHERE active = 1 ORDER BY id"
            ).fetchall()
        return [r["id"] for r in rows]
    except Exception:
        return []


def _pick_round_robin(candidates: list[dict], db) -> dict | None:
    """Select the next slot using round-robin across channels.

    Candidates are already sorted by priority score (highest first).
    This function picks the highest-scored candidate from the next
    channel in the round-robin sequence, ensuring all channels get
    equal dispatch turns.

    Returns the selected slot dict, or None if no channel has ready
    candidates.
    """
    if not candidates:
        return None

    channel_ids = _get_active_channel_ids(db)
    if not channel_ids:
        return candidates[0]

    # Determine last dispatched channel.
    # v26 fix: read from system_state (persistent) instead of inferring from
    # the last running/completed slot — cancelled slots (the common failure
    # outcome) were excluded from that query, which broke the rotation and
    # let a failing channel keep winning dispatch turns.
    last_channel = None
    try:
        raw = db.get_system_state("last_dispatched_channel")
        if raw:
            last_channel = int(raw)
    except Exception:
        pass
    if last_channel is None:
        # Fallback for existing deployments: infer from the most recent slot.
        try:
            with db._connect() as conn:
                row = conn.execute(
                    """SELECT channel_id FROM planned_slots
                       WHERE status IN ('running', 'completed')
                       ORDER BY scheduled_at DESC
                       LIMIT 1"""
                ).fetchone()
            if row:
                last_channel = row["channel_id"]
        except Exception:
            pass

    # Find starting index in round-robin
    if last_channel and last_channel in channel_ids:
        start_idx = (channel_ids.index(last_channel) + 1) % len(channel_ids)
    else:
        start_idx = 0

    # Cycle through channels to find one with candidates
    for offset in range(len(channel_ids)):
        target_ch = channel_ids[(start_idx + offset) % len(channel_ids)]
        ch_candidates = [c for c in candidates if c["channel_id"] == target_ch]
        if ch_candidates:
            if offset > 0:
                logger.info(
                    "Round-robin: skipped %d channel(s) without candidates, "
                    "dispatching channel %d",
                    offset, target_ch,
                )
            return ch_candidates[0]

    return None  # no channel has ready candidates


def process_planned_slots(db=None, loop=None) -> dict | None:
    """Check for due planned slots and dispatch generation if possible.
    
    Called every 5 min by the API checker loop.
    
    v9 3-phase model:
      - Scheduled channels: dispatch with generate_only (F1), upload later (F2)
      - Pull-forward: dispatch early if worker is idle and target_public_at 
        is within the channel's lead window.

    Args:
        db: ExtendedDatabase instance (created if None).
        loop: asyncio event loop for scheduling the async worker. Required
              when called from a thread pool (e.g. via asyncio.to_thread).
              If None, falls back to asyncio.create_task (must be called
              from an active event loop thread).
    
    Returns:
        dict with dispatched slot info, or None if nothing to do.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    
    # 0. Mark completed/failed: any running slot whose job is done
    _sync_running_slots(db)
    
    # 0b. Detect manual jobs that completed recently (no planned_slot row)
    #     and trigger reajuste so pending slots align to the real timeline.
    if _detect_manual_completions(db):
        _readjust_pending_slots(db)
    
    # 1. Reconcile mid-day changes
    sync_midday(db)
    
    # 1b. Cancel stale pending slots (upload window already passed)
    _cancel_stale_slots(db)
    
    # 2. Find candidate slots and pick the best one by priority score
    from datetime import date as _date
    today_str = _date.today().isoformat()
    candidates = db.get_priority_slot_candidates(max_future_hours=36, limit=20)
    if not candidates:
        return None
    
    # Score each candidate and sort by priority (highest first)
    for c in candidates:
        c["_priority_score"] = _score_priority_slot(c, db, today_str)
    candidates.sort(key=lambda c: c["_priority_score"], reverse=True)
    
    # Pick the next slot using round-robin across channels
    next_slot = _pick_round_robin(candidates, db)
    if next_slot is None:
        logger.info("Round-robin dispatch: no channel has ready candidates")
        return None
    best_score = next_slot.get("_priority_score", 0)
    slug_preview = next_slot.get("channel_slug", "?")
    logger.info(
        "Round-robin dispatch: %d candidates → selected %s (score=%d, pub=%s)",
        len(candidates), slug_preview, best_score,
        (next_slot.get("target_public_at") or "?")[:16],
    )
    
    # 2b. Is there already an active job for this channel?
    active = db.get_active_job_for_channel(next_slot["channel_id"])
    if active:
        logger.debug("Planned slot skipped: channel %d already has active job #%d",
                     next_slot["channel_id"], active["id"])
        return None
    
    # 2c. Phase-pipelining guard: allow up to 1 render + 1 prep concurrently.
    #     - If no render is active → dispatch any job (it will claim render slot).
    #     - If 1 render is active → allow dispatching 1 PREP worker (2 total).
    #     - If 2 jobs already active → defer dispatch.
    active_count = db.count_active_longform_jobs()
    render_count = db.count_render_phase_jobs()
    MAX_TOTAL_JOBS = 1
    MAX_RENDER_JOBS = 1
    
    if active_count >= MAX_TOTAL_JOBS:
        logger.info(
            "Planned slot deferred: %d/%d active jobs (render=%d) — at capacity",
            active_count, MAX_TOTAL_JOBS, render_count,
        )
        return None
    
    if render_count >= MAX_RENDER_JOBS:
        # A render is active. Check if this channel allows pipelining.
        ch_cfg = {}
        try:
            cfg_raw = next_slot.get("config_json", "{}")
            if isinstance(cfg_raw, str):
                import json as _j2
                ch_cfg = _j2.loads(cfg_raw) if cfg_raw else {}
            elif cfg_raw:
                ch_cfg = cfg_raw
        except Exception:
            pass
        if ch_cfg.get("pipeline_enabled", True) is False:
            logger.info(
                "Planned slot deferred: render active + pipeline disabled for %s",
                slug_preview,
            )
            return None
        
        # A render is active. Only allow dispatch if there's room for a prep worker.
        if active_count >= MAX_TOTAL_JOBS:
            logger.info(
                "Planned slot deferred: render active + %d total jobs = at capacity",
                active_count,
            )
            return None
        logger.info(
            "Phase pipelining: render active (%d), dispatching prep worker "
            "(total active: %d → %d)",
            render_count, active_count, active_count + 1,
        )
    
    # 3b. Memory guard: different threshold for prep vs render dispatch.
    #     Prep phases (scrape→media) need ~1.5 GB. Render needs ~2.5 GB
    #     (xfade_batch_size=25 → concat peak ~1.5 GB).
    if render_count > 0:
        # Dispatching a prep worker while a render is active
        if not _memory_ok(min_free_gb=1.5):
            logger.warning("Low memory (prep) — delaying planned slot dispatch")
            return None
    else:
        if not _memory_ok(min_free_gb=2.5):
            logger.warning("Low memory (render) — delaying planned slot dispatch")
            return None
    
    # ── Enter dispatch critical section ──────────────────────────
    # Acquire global lock before atomically creating the job.
    # The lock + guards together prevent TOCTOU races with other
    # dispatch entry points (manual click, due schedules, priority).
    with _DISPATCH_LOCK:
        # Re-check guard under lock (belts-and-suspenders for TOCTOU)
        active_under = db.count_active_longform_jobs()
        render_under = db.count_render_phase_jobs()
        if active_under >= MAX_TOTAL_JOBS:
            logger.info("Planned slot deferred (under lock): %d/%d jobs active",
                        active_under, MAX_TOTAL_JOBS)
            return None
        if render_under >= MAX_RENDER_JOBS and active_under >= MAX_TOTAL_JOBS:
            logger.info("Planned slot deferred (under lock): render active + at capacity")
            return None
        
        slot_id = next_slot["id"]
        channel_id = next_slot["channel_id"]
        slug = next_slot.get("channel_slug", "")
        source_mode = next_slot.get("source_mode", "original")
        
        logger.info(
            "Dispatching slot #%d: %s (scheduled=%s, pub=%s)",
            slot_id, slug,
            (next_slot.get("scheduled_at") or "?")[11:16],
            (next_slot.get("target_public_at") or "?")[11:16] if next_slot.get("target_public_at") else "?",
        )
        
        # 4. Mark slot as running
        db.update_slot_status(slot_id, "running")
        
        # 5. Create the video record
        from database.db_extended import ExtendedDatabase
        
        # Get channel config to check publish mode
        ch_cfg = db.get_channel_planning_config(channel_id)
        publish_mode = ch_cfg.get("publish_mode", "immediate") if ch_cfg else "immediate"
        is_scheduled = publish_mode == "scheduled"
        
        # ── Use the correct target_public_at (peak publish time) ──
        target_public_at = next_slot.get("target_public_at")
        
        # ── v10.3: Collision guard at dispatch time ──
        # Before creating the video, verify the target_public_at doesn't collide
        # with already-existing videos or planned slots from the same channel.
        # This catches cases where the planning system assigned the same time
        # to multiple slots (e.g., due to replan/race conditions).
        if target_public_at and is_scheduled:
            try:
                from pipeline.publish_scheduler import _avoid_channel_collision
                from datetime import timezone as _tz_guard, datetime as _dt_guard
                
                # Parse the ISO8601 UTC string to datetime
                tpa_str = str(target_public_at) if target_public_at else ""
                if isinstance(target_public_at, str):
                    proposed = _dt_guard.fromisoformat(
                        tpa_str.replace("Z", "+00:00").replace(" ", "T")
                    )
                else:
                    proposed = target_public_at
                if proposed.tzinfo is None:
                    proposed = proposed.replace(tzinfo=_tz_guard.utc)
                
                adjusted = _avoid_channel_collision(
                    channel_id, proposed, db=db, slug=slug,
                )
                if adjusted != proposed:
                    logger.info(
                        "[%s] Dispatch collision guard: pushed target_public_at from %s → %s",
                        slug,
                        proposed.isoformat(),
                        adjusted.isoformat(),
                    )
                    target_public_at = adjusted.isoformat()
                    # Update the planned_slot's target_public_at too
                    try:
                        with db._connect() as _gc:
                            _gc.execute(
                                "UPDATE planned_slots SET target_public_at = ? WHERE id = ?",
                                (target_public_at, slot_id),
                            )
                            _gc.commit()
                    except Exception:
                        pass
            except Exception as guard_exc:
                logger.debug("[%s] Dispatch collision guard skipped: %s", slug, guard_exc)
        
        with db._connect() as conn:
            # ── Quota-aware (ago 2026): propagar la hora de subida planificada
            # (batch por cuenta) → scheduled_upload_at para que upload_scheduler
            # suba en el batch y no recalcule un momento aleatorio.
            planned_upload_at = next_slot.get("target_upload_at")
            if is_scheduled and planned_upload_at:
                planned_upload_at = str(planned_upload_at).replace("T", " ")[:19]
            cursor = conn.execute(
                "INSERT INTO videos (canal, channel_id, video_path, status, progress, "
                "publish_mode, target_public_at, scheduled_upload_at, created_at) "
                "VALUES (?, ?, '', 'generating', 0, ?, ?, ?, CURRENT_TIMESTAMP)",
                (slug, channel_id, publish_mode, target_public_at, planned_upload_at),
            )
            conn.commit()
            video_id = cursor.lastrowid
        
        # 6. Determine action — scheduled channels use generate_only (F1 only)
        if is_scheduled:
            action = "generate_only"
            # Don't pass upload=True — the worker will skip upload and keep mp4
        else:
            action = "generate_and_upload"
        
        # 7. Create job and mark it running IMMEDIATELY
        job_id = db.create_job(channel_id, action, video_id)
        db.update_job(job_id, status="running")
        
        # 8. Link job to slot
        db.update_slot_status(slot_id, "running", job_id=job_id, video_id=video_id)
    # ── End dispatch critical section ────────────────────────────
    
    # 9. Fire and forget the generation
    # ── Schedule async worker safely ──
    # asyncio.create_task fails with RuntimeError when called from a
    # thread-pool thread (no running event loop). Use
    # run_coroutine_threadsafe when a loop is provided by the caller
    # (main.py passes it via asyncio.get_running_loop).
    import asyncio as _asyncio_plan
    from api.services.generation_service import (
        start_generation_job,
        start_generation_job_subprocess,
        USE_SUBPROCESS_WORKER,
    )
    
    if USE_SUBPROCESS_WORKER:
        _gen_coro = start_generation_job_subprocess(
            job_id=job_id,
            channel_id=channel_id,
            video_id=video_id,
            action=action,
            source_mode=source_mode,
        )
    else:
        _gen_coro = start_generation_job(
            job_id=job_id,
            channel_id=channel_id,
            video_id=video_id,
            action=action,
            source_mode=source_mode,
        )
    
    if loop is not None:
        _asyncio_plan.run_coroutine_threadsafe(_gen_coro, loop)
    else:
        _asyncio_plan.create_task(_gen_coro)

    # ── v26: persist the last dispatched channel for fair round-robin ──
    try:
        db.set_system_state("last_dispatched_channel", str(channel_id))
    except Exception:
        pass

    return {
        "slot_id": slot_id,
        "job_id": job_id,
        "video_id": video_id,
        "channel_slug": slug,
        "action": action,
    }


def _ensure_never_dry(db) -> bool:
    """Fallback: if no slot could be dispatched, check if the pipeline is empty
    and auto-replan the horizon to keep generation flowing.
    
    Returns True if a replan was triggered.
    """
    # Check if there's at least one pending slot in the next 72h
    candidates = db.get_priority_slot_candidates(max_future_hours=72, limit=1)
    if candidates:
        return False  # slots exist, just not dispatchable right now
    
    # ── v26: do not force a replan if every active channel is paused by the
    # circuit breaker — the pipeline is intentionally idle, not "dry".
    try:
        channels = db.get_channels(active_only=True)
        if channels and all(
            _channel_paused_for_planning(db, ch["id"]) for ch in channels
        ):
            logger.info(
                "Never-dry: all %d active channel(s) paused by circuit breaker — "
                "skipping emergency replan",
                len(channels),
            )
            return False
    except Exception:
        pass
    
    # No slots in the next 3 days — pipeline is empty!
    logger.warning(
        "Pipeline EMPTY: no dispatchable slots in next 72h. "
        "Triggering emergency horizon replan."
    )
    try:
        result = compute_and_store_horizon(horizon_days=7, db=db, force_replan=True)
        logger.info(
            "Emergency replan complete: %d slots across %d days",
            result.get("total_slots", 0), result.get("days_planned", 0),
        )
        return True
    except Exception as exc:
        logger.error("Emergency replan failed: %s", exc)
        return False


# ── Channel failure detection (circuit breaker) ─────────────────

def _is_transient_failure(error_msg: str) -> bool:
    """Return True if the job error matches a known transient pattern.

    Transient failures (RAM, timeout, broken pipe, …) are retried with
    backoff and must NOT count towards the consecutive-failure breaker.
    """
    _TRANSIENT = [
        "timeout", "memory guard", "broken pipe", "brokenpipe",
        "orphaned: process lost", "memory", "abortado: memoria",
        "ram too low", "ram insuficiente",
    ]
    return any(p in error_msg for p in _TRANSIENT)


def count_channel_consecutive_failures(db, channel_id: int) -> int:
    """Count consecutive PERMANENT long-form generation failures for a channel.

    Walks the most recent failed long-form jobs (newest first) and stops at
    the first transient failure or non-failed job — so only an unbroken run
    of permanent failures counts.

    NOTE: ``generation_jobs.created_at`` is stored in UTC (SQLite
    CURRENT_TIMESTAMP), so the window is computed in UTC to match.

    Args:
        db: ExtendedDatabase instance.
        channel_id: Channel id.

    Returns:
        int: number of consecutive permanent failures (0 if none).
    """
    from datetime import datetime, timedelta, timezone

    window_start = (datetime.now(timezone.utc) - timedelta(hours=CHANNEL_FAILURE_WINDOW_H)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT error_msg FROM generation_jobs
                   WHERE channel_id = ? AND status = 'failed'
                     AND action IN ('generate_only', 'generate_and_upload')
                     AND created_at >= ?
                   ORDER BY id DESC LIMIT 20""",
                (channel_id, window_start),
            ).fetchall()
    except Exception as exc:
        logger.warning("count_channel_consecutive_failures: query failed: %s", exc)
        return 0

    count = 0
    for row in rows:
        err = (row["error_msg"] or "").lower()
        if _is_transient_failure(err):
            break  # transient failure resets the streak
        count += 1
    return count


def _channel_breaker_active(db, channel_id: int) -> bool:
    """Return True if the channel is currently paused by the circuit breaker."""
    try:
        with db._connect() as conn:
            row = conn.execute(
                """SELECT id FROM pipeline_alerts
                   WHERE entity_type = 'channel' AND entity_id = ?
                     AND alert_type = 'consecutive_failures' AND resolved = 0
                   LIMIT 1""",
                (channel_id,),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _apply_channel_failure_breaker(db, channel_id: int, slug: str) -> bool:
    """Pause a channel after consecutive permanent generation failures.

    Actions (all idempotent):
      1. Create a critical alert documenting the failure and that
         ``videos_per_day`` must be raised again once the root cause is fixed.
      2. Lower ``videos_per_day`` to 0 (persisted in ``channels.config_json``;
         the startup config sync preserves DB-only planning keys, so this
         survives restarts).
      3. Cancel all pending planned_slots for the channel.

    The channel is excluded from automatic replans while ``videos_per_day``
    is 0 / the alert is unresolved. Reactivation is MANUAL: fix the root
    cause and raise ``videos_per_day`` in the channel planning panel.

    Returns True if the breaker was tripped (or was already active).
    """
    # Idempotency: if already paused with an unresolved alert, nothing to do.
    if _channel_breaker_active(db, channel_id):
        logger.debug("Circuit breaker already active for channel_id=%d (%s)", channel_id, slug)
        return True

    failures = count_channel_consecutive_failures(db, channel_id)
    if failures < CHANNEL_CONSECUTIVE_FAILURES:
        return False

    logger.warning(
        "⚠️  CIRCUIT BREAKER: %s has %d consecutive permanent generation failures "
        "(threshold %d) — pausing channel",
        slug, failures, CHANNEL_CONSECUTIVE_FAILURES,
    )

    # 1. Alert (well documented — instructs the operator how to re-enable).
    try:
        from api.services.lifecycle_monitor import create_alert
        create_alert(
            db,
            entity_type="channel",
            entity_id=channel_id,
            channel_id=channel_id,
            alert_type="consecutive_failures",
            severity="critical",
            title=(
                f"Canal {slug}: {failures} fallos consecutivos de generación — "
                f"videos_per_day bajado a 0"
            ),
            message=(
                f"El canal {slug} ha acumulado {failures} fallos PERMANENTES consecutivos "
                f"de generación long-form (umbral: {CHANNEL_CONSECUTIVE_FAILURES} en "
                f"{CHANNEL_FAILURE_WINDOW_H}h). Se ha puesto videos_per_day=0 y se han "
                f"cancelado sus slots pendientes para no bloquear el pipeline de los demás canales.\n\n"
                f"⚠️ IMPORTANTE: cuando el fallo de creación de vídeos esté SOLVENTADO, "
                f"entra en Planificación de {slug} y vuelve a subir videos_per_day "
                f"(recuerda: 0 = canal pausado)."
            ),
            metadata={
                "failures": failures,
                "threshold": CHANNEL_CONSECUTIVE_FAILURES,
                "window_h": CHANNEL_FAILURE_WINDOW_H,
                "action": "videos_per_day=0 + slots cancelados",
            },
        )
    except Exception as exc:
        logger.warning("Circuit breaker: alert creation failed for %s: %s", slug, exc)

    # 2. videos_per_day = 0 (persisted in config_json, survives restarts).
    try:
        db.update_channel_planning_config(channel_id, videos_per_day=0)
        logger.warning("Circuit breaker: %s videos_per_day → 0", slug)
    except Exception as exc:
        logger.warning("Circuit breaker: failed to set videos_per_day=0 for %s: %s", slug, exc)

    # 3. Cancel pending slots (today + future).
    try:
        with db._connect() as conn:
            cnt = conn.execute(
                "UPDATE planned_slots SET status = 'cancelled' "
                "WHERE channel_id = ? AND status = 'pending'",
                (channel_id,),
            ).rowcount
            conn.commit()
        if cnt:
            logger.warning("Circuit breaker: %s — %d pending slot(s) cancelled", slug, cnt)
    except Exception as exc:
        logger.warning("Circuit breaker: slot cancellation failed for %s: %s", slug, exc)

    # 4. Drop the last-dispatched marker if it points at this channel so the
    #    round-robin does not keep returning to a paused channel.
    try:
        if str(db.get_system_state("last_dispatched_channel")) == str(channel_id):
            db.set_system_state("last_dispatched_channel", "")
    except Exception:
        pass

    return True


def _channel_paused_for_planning(db, channel_id: int) -> bool:
    """Return True if the channel must be excluded from automatic planning.

    A channel is paused when the circuit breaker is active OR its effective
    videos_per_day is 0. Guards smart_replan, recovery planner, never-dry
    and horizon planning from creating slots for broken channels.
    """
    if _channel_breaker_active(db, channel_id):
        return True
    try:
        cfg = db.get_channel_planning_config(channel_id)
        return int(cfg.get("videos_per_day", 1) or 0) <= 0
    except Exception:
        return False


def _sync_running_slots(db):
    """Check ALL running slots: if their job is done, mark the slot accordingly.
    
    Scans every slot with status='running' across ALL dates, not just today.
    This prevents orphaned running slots from accumulating when jobs are
    dispatched from future date_keys and subsequently fail.
    
    For transient failures (RAM, timeout, etc.), applies exponential backoff
    via dispatch cooldown instead of immediately cancelling the slot.
    Permanent failures (API errors, code bugs) are cancelled as before.
    
    Also triggers _readjust_pending_slots() when a job completes, to
    realign the remaining slots and avoid cascading time drift.
    """
    running_slots = db.get_planned_slots(status="running")
    any_completed = False
    permanent_failed_slots = []  # v26: for the channel circuit breaker
    
    # Transient error patterns (same as generation_service._auto_retry_if_transient)
    TRANSIENT_PATTERNS = [
        "timeout", "memory guard", "broken pipe", "brokenpipe",
        "orphaned: process lost", "memory", "abortado: memoria",
        "ram too low", "ram insuficiente",
    ]
    
    for s in running_slots:
        job_id = s.get("job_id")
        if not job_id:
            # No job linked at all → stale, mark cancelled (never consumed)
            db.update_slot_status(s["id"], "cancelled")
            logger.info("Slot #%d cancelled (no job linked — stale)", s["id"])
            continue
        job = db.get_job(job_id)
        if not job:
            # Job row missing → stale, mark cancelled (never consumed)
            db.update_slot_status(s["id"], "cancelled")
            logger.info("Slot #%d cancelled (job #%d not found)", s["id"], job_id)
            continue
        if job["status"] in ("completed", "success"):
            db.update_slot_status(s["id"], "completed")
            logger.info("Slot #%d marked completed (job #%d done)", s["id"], job_id)
            any_completed = True
        elif job["status"] in ("failed", "cancelled"):
            error_msg = (job.get("error_msg") or "").lower()
            is_transient = any(p in error_msg for p in TRANSIENT_PATTERNS)
            if is_transient:
                # Apply backoff instead of cancelling — v12
                result = db.record_slot_dispatch_failure(job_id)
                logger.info(
                    "Slot #%d (%s) transient failure — backoff=%s (error: %.100s)",
                    s["id"], job_id, result, error_msg,
                )
            else:
                # Permanent failure → cancel slot
                db.update_slot_status(s["id"], "cancelled")
                logger.info("Slot #%d cancelled (job #%d %s)", s["id"], job_id, job["status"])
                # v26: track for the channel circuit breaker (after the loop)
                permanent_failed_slots.append(s)
            # NOT any_completed — failed slots should NOT trigger readjustment
    
    if any_completed:
        _readjust_pending_slots(db)

    # ── v26: channel circuit breaker — pause channels with an unbroken run
    # of permanent failures so they stop monopolizing the generation slot.
    for s in permanent_failed_slots:
        try:
            _apply_channel_failure_breaker(
                db, s.get("channel_id"), s.get("channel_slug", "?"),
            )
        except Exception as exc:
            logger.debug("Circuit breaker check failed: %s", exc)

    # ── v26: also let the Dynamic VPD adjuster react to permanent failures
    # (not only completions), so a channel with 0% success drops its quota.
    if permanent_failed_slots:
        try:
            _dynamic_vpd_adjust(db, permanent_failed_slots)
        except Exception as exc:
            logger.debug("Dynamic VPD adjust after failures failed: %s", exc)


@_replan_locked
def _readjust_pending_slots(db):
    """Realign remaining pending slots after a job finishes — cross-day aware.
    
    When a generation job completes, pulls forward the next pending slot
    regardless of its date_key. All pending slots across the horizon are
    re-chained with per-channel durations and GLOBAL_GAP_MINUTES.
    
    target_upload_at and target_public_at are preserved — only scheduled_at
    (generation start) is adjusted for the real timeline.
    """
    GAP = GLOBAL_GAP_MINUTES
    
    # Get ALL pending slots across the horizon (not just today)
    now = datetime.now()
    today = date.today().isoformat()
    horizon_end = (date.today() + timedelta(days=DEFAULT_HORIZON_DAYS)).isoformat()
    
    all_pending = db.get_planned_slots_week(today, horizon_end)
    pending = [s for s in all_pending if s["status"] == "pending"]
    if not pending:
        return
    
    # Sort by current scheduled_at
    pending_sorted = sorted(pending, key=lambda s: s.get("scheduled_at", "9999"))
    
    # Anchor: now + gap (real time)
    anchor = now + timedelta(minutes=GAP)
    
    logger.info(
        "Readjusting %d pending slots cross-day from anchor=%s",
        len(pending_sorted), anchor.strftime("%m-%d %H:%M"),
    )
    
    next_start = anchor
    for slot in pending_sorted:
        # Get channel's avg duration
        ch_dur = ESTIMATED_PIPELINE_MINUTES  # fallback
        try:
            from api.services.schedule_engine import get_avg_creation_minutes
            ch_dur = get_avg_creation_minutes(slot["channel_id"]) * (1 + BUFFER_PCT)
        except Exception:
            pass
        
        old_sched = slot.get("scheduled_at", "?")[11:16] if slot.get("scheduled_at") else "?"
        
        new_sched = next_start.strftime("%Y-%m-%d %H:%M:%S")
        new_sched_short = next_start.strftime("%H:%M")
        
        if old_sched != new_sched_short:
            logger.info(
                "  Slot #%d (%s): gen %s → %s (pub=%s, upload=%s)",
                slot["id"], slot.get("channel_slug", "?"),
                old_sched, new_sched_short,
                (slot.get("target_public_at") or "?")[11:16] if slot.get("target_public_at") else "?",
                (slot.get("target_upload_at") or "?")[11:16] if slot.get("target_upload_at") else "?",
            )
        
        with db._connect() as conn:
            conn.execute(
                "UPDATE planned_slots SET scheduled_at = ? WHERE id = ?",
                (new_sched, slot["id"]),
            )
            conn.commit()
        
        next_start = next_start + timedelta(minutes=ch_dur + GAP)
    
    # ── Dynamic VPD auto-adjustment ────────────────────────
    _dynamic_vpd_adjust(db, pending_sorted)


def _dynamic_vpd_adjust(db, pending_sorted):
    """Evaluate and apply Dynamic VPD for channels with recently adjusted slots.

    ⚠️  Intencionalmente desactivado (#6): DYNAMIC_VPD_ENABLED = False en
    defaults.py — feedback negativo previo (bugs → baja success_rate → Dynamic
    VPD baja videos_per_day). Si se reactiva, revisar topes
    (VIDEOS_PER_DAY_MIN/MAX, DYNAMIC_VPD_SHARED_ACCOUNT_MAX) y validación de
    impacto.
    """
    try:
        from config.config_bridge import get_channel_config
    except Exception:
        return
    
    unique_slugs = set()
    for slot in pending_sorted:
        slug = slot.get('channel_slug') or slot.get('canal')
        if slug:
            unique_slugs.add(slug)
    
    adjuster = DynamicSlotAdjuster(db)
    for slug in unique_slugs:
        cfg = get_channel_config(slug)
        interval = getattr(cfg, 'DYNAMIC_VPD_CHECK_INTERVAL_H', 6)
        if not adjuster.should_check(slug, interval_h=interval):
            continue
        adjuster.apply(slug)


def _cancel_stale_slots(db):
    """Cancel pending slots whose upload window has already passed.

    In the 3-phase model, a slot is "stale" if its target_upload_at is in the past
    (the upload window for that day has come and gone). These slots can never be
    generated+uploaded in time.

    Also cancels slots whose scheduled_at is >2h in the past (server crash/restart
    scenarios where the dispatcher never picked them up).

    Viral protection: stale viral slots are preserved if the channel has NOT yet
    generated any viral video today — this ensures the viral quota is met even when
    scheduling delays push viral slots past their window.

    Skips cancellation when an active generation is in progress — slots
    blocked by the global concurrency guard are held intentionally.
    """
    # Don't cancel slots held back by an active generation
    if db.count_active_jobs() > 0:
        return

    # Subquery: channels that already generated a viral video today.
    # Viral slots for these channels can be cancelled (quota met).
    # Viral slots for other channels are preserved (quota not met yet).
    channels_with_viral_today = """
        SELECT DISTINCT v.channel_id
        FROM videos v
        JOIN scripts sc ON v.script_id = sc.id
        JOIN raw_content rc ON sc.raw_content_id = rc.id
        WHERE rc.source_mode = 'viral'
          AND DATE(v.uploaded_at) = DATE('now', 'localtime')
          AND v.yt_video_id IS NOT NULL
    """

    cancelled = 0
    with db._connect() as conn:
        # Cancel slots whose upload window has passed.
        # Viral slots are only cancelled if the channel already has a
        # viral video today; otherwise they are preserved.
        c1 = conn.execute(
            f"""UPDATE planned_slots SET status = 'cancelled'
               WHERE status = 'pending'
                 AND (
                     source_mode != 'viral'
                     OR channel_id IN ({channels_with_viral_today})
                 )
                 AND target_upload_at IS NOT NULL
                 AND target_upload_at <= datetime('now', 'localtime', '-30 minutes')"""
        ).rowcount

        # Also cancel very old pending slots (>2h past scheduled_at).
        # Only for today/past dates — future-date slots with old scheduled_at
        # are pre-generated buffer slots and should NOT be cancelled.
        c2 = conn.execute(
            f"""UPDATE planned_slots SET status = 'cancelled'
               WHERE status = 'pending'
                 AND date_key <= date('now', 'localtime')
                 AND (
                     source_mode != 'viral'
                     OR channel_id IN ({channels_with_viral_today})
                 )
                 AND scheduled_at <= datetime('now', 'localtime', '-2 hours')"""
        ).rowcount

        conn.commit()
        cancelled = c1 + c2

    if cancelled:
        logger.info("Cancelled %d stale pending slots (upload window passed: %d, very old: %d)",
                     cancelled, c1, c2)


def _detect_manual_completions(db) -> bool:
    """Detect recently completed jobs that have NO planned_slots row.
    
    These are manual generations (triggered from the panel).  When one
    finishes, we should reajust today's pending planned slots so they
    don't fall behind the real timeline.
    
    Returns True if any manual completions were found in the last 15 min.
    """
    with db._connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM generation_jobs j
               WHERE j.status IN ('completed', 'failed')
                 AND j.finished_at >= datetime('now', 'localtime', '-15 minutes')
                 AND NOT EXISTS (
                     SELECT 1 FROM planned_slots ps WHERE ps.job_id = j.id
                 )"""
        ).fetchone()
        count = row["cnt"] if row else 0
    
    if count > 0:
        logger.info(
            "Detected %d manual job completion(s) — triggering reajuste", count
        )
        return True
    return False


def _memory_ok(min_free_gb: float = 4.0) -> bool:
    """Check if the server has enough free RAM to launch a generation job.
    
    Returns False if available memory is below the threshold, to prevent
    OOM killer from taking down the process.
    
    Uses /proc/meminfo::MemAvailable (container-aware) instead of
    os.sysconf() which reports host memory in containerized environments.
    
    Threshold raised to 4.0 GB (from 2.0) to account for MoviePy rendering
    peaks that can exhaust ~25 MB/frame in raw RGB during write_videofile.
    """
    try:
        # MemAvailable is container-aware (cgroup limits) on Linux 3.14+
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    avail_kb = int(line.split()[1])
                    avail_gb = avail_kb / (1024 * 1024)
                    if avail_gb < min_free_gb:
                        logger.warning(
                            "Memory guard: only %.1f GB free (need %.1f GB) — skipping dispatch",
                            avail_gb, min_free_gb,
                        )
                        return False
                    return True
        # Fallback: no MemAvailable field (old kernel) — let it proceed
        logger.warning("Memory guard: /proc/meminfo has no MemAvailable — skipping guard")
        return True
    except Exception:
        return True  # can't determine — let it proceed


# ── Shorts Planning (DEPRECATED — use api.services.shorts_scheduler instead) ──
# These functions have been moved to:
#   - api/services/shorts_scheduler.py (compute, persist, dispatch)
#   - database/db_extended.py (get_shorts_planning_config, update_shorts_planning_config)
#
# Kept for backward compatibility during transition. Will be removed in v3.

def get_shorts_planning_config(db=None) -> list[dict]:
    """[DEPRECATED] Get shorts planning config for all active channels.
    
    Use db.get_shorts_planning_config() instead.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    return db.get_shorts_planning_config()


def update_shorts_planning_config(channel_id: int, data: dict, db=None) -> dict:
    """[DEPRECATED] Update shorts planning config for one channel.
    
    Use db.update_shorts_planning_config(channel_id, data) instead.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    ok = db.update_shorts_planning_config(channel_id, data)
    return {"ok": ok, "message": "Shorts planning config updated" if ok else "No valid fields"}


def ensure_shorts_planned(db=None):
    """[DEPRECATED] Ensure today has shorts_schedule entries.
    
    Use shorts_scheduler.generate_upcoming_shorts() instead.
    """
    from api.services.shorts_scheduler import generate_upcoming_shorts
    generate_upcoming_shorts(days=7, db=db)


def process_shorts_slots(db=None):
    """[DEPRECATED] Process due shorts_schedule slots.
    
    Use shorts_scheduler.dispatch_next_due_shorts_slot() instead.
    Called by the background checker loop in api/main.py.
    """
    from api.services.shorts_scheduler import dispatch_next_due_shorts_slot
    dispatch_next_due_shorts_slot(db=db)


def _generate_and_publish_native_short(channel_id: int, channel_slug: str, db=None):
    """Generate and publish one native Short for a channel (internal, used by scheduler).

    This function is kept as the canonical native short generator and is imported
    by shorts_scheduler._dispatch_native_short. The shorts_scheduler has its own
    copy for independence, but external callers can use this too.

    Args:
        channel_id: Channel ID in the database.
        channel_slug: Channel slug (e.g. 'canal2').
        db: Optional ExtendedDatabase instance.

    Returns:
        short_id if successful, None on failure.
    """
    import json, random, re, subprocess, time, sqlite3
    from pathlib import Path
    from config.settings import DATABASE_PATH, LLM_MODEL, OUTPUT_DIR
    from config.llm_client import create_llm_client
    from config.config_bridge import get_channel_config

    ch_config = get_channel_config(channel_slug)
    hashtags = getattr(ch_config, "SHORTS_HASHTAGS", ["#Shorts"])

    # Fetch recent topics to avoid repetition
    from database.db_extended import ExtendedDatabase
    dbx = ExtendedDatabase(str(DATABASE_PATH))
    recent_topics = dbx.get_recent_short_topics(channel_id, limit=15)
    topic_warning = ""
    if recent_topics:
        topic_list = "\n".join(f'  - "{t}"' for t in recent_topics)
        topic_warning = (
            f"\n\n⚠️ IMPORTANTE: NO repitas NINGUNO de estos temas ya publicados "
            f"recientemente en este canal:\n{topic_list}\n\n"
            f"Elige un tema COMPLETAMENTE DIFERENTE y fresco. "
            f"Incluye en el JSON un campo \"tema\" con una frase corta (max 80 chars) "
            f"que identifique claramente de qué trata este Short.\n"
        )
    else:
        topic_warning = (
            f"\n\nIncluye en el JSON un campo \"tema\" con una frase corta (max 80 chars) "
            f"que identifique claramente de qué trata este Short.\n"
        )

    # 1. Script via LLM
    from config.llm_helpers import llm_json_call
    client = create_llm_client(enable_thinking=False)
    niche = getattr(ch_config, "CANAL_NARRATIVE_STYLE", "documental")
    display_name = getattr(ch_config, "CANAL_DISPLAY_NAME", channel_slug)
    tagline = getattr(ch_config, "CANAL_TAGLINE", "")

    try:
        script = llm_json_call(
            client,
            max_retries=3,
            retry_delay=2.0,
            model=LLM_MODEL,
            messages=[{"role": "user", "content": f"Genera un Short viral en español de ~35-45 segundos (~45-55 palabras totales, minimo 45). Canal: {display_name} — {niche}. Tagline: {tagline}.{topic_warning}Usa 5 bloques (hook, desarrollo1, desarrollo2, climax, cierre). IMPORTANTE: desarrollo1, desarrollo2 y climax deben tener 2-3 frases cada uno. Hook y cierre: 1-2 frases. Minimo 10 palabras por bloque. El total debe superar 50 palabras. cierre debe ser una conclusion natural, SIN pedir suscripcion (se añadira un bloque separado si es necesario). PARA CADA BLOQUE genera 'search_query_en': 5-8 keywords EN INGLÉS para buscar imágenes de stock que coincidan con lo narrado. Sé muy concreto: incluye tema + detalles visuales (iluminación, tipo de plano, atmósfera). NO uses español. Además genera 'theme_keywords_en': 5-8 keywords EN INGLÉS del tema visual GLOBAL del short. Devuelve SOLO JSON: {{\"tema\": \"frase corta que identifica el tema (max 80 chars)\", \"titulo\": \"...\", \"hook_text\": \"frase de gancho 8-12 palabras\", \"theme_keywords_en\": [\"global\", \"theme\"], \"bloques\": [{{\"tipo\": \"hook\", \"texto\": \"1-2 frases\", \"search_query_en\": \"english keywords\"}}, {{\"tipo\": \"desarrollo1\", \"texto\": \"2-3 frases con contexto y detalle\", \"search_query_en\": \"english keywords\"}}, {{\"tipo\": \"desarrollo2\", \"texto\": \"2-3 frases con dato impactante especifico\", \"search_query_en\": \"english keywords\"}}, {{\"tipo\": \"climax\", \"texto\": \"2-3 frases con la consecuencia o revelacion\", \"search_query_en\": \"english keywords\"}}, {{\"tipo\": \"cierre\", \"texto\": \"1-2 frases cierre natural\", \"search_query_en\": \"english keywords\"}}]}}. NADA MAS fuera del JSON."}],
            temperature=0.9, max_tokens=1200,
        )
    except Exception as e:
        logger.error("Short script generation failed after retries for %s: %s", channel_slug, e)
        return

    # 1b. Validate script completeness
    from pipeline.shorts_tts import validate_short_script
    errors = validate_short_script(script)
    if errors:
        logger.error("Short script validation failed for %s: %s", channel_slug, errors)
        return

    title = (script.get("titulo") or script.get("title") or "Short")[:100]
    hook_text = (script.get("hook_text") or "")[:100]
    bloques = script.get("bloques", [])
    topic = (script.get("tema") or "")[:200]  # store topic for dedup

    # 1c. Subscribe CTA (~40% of native shorts) — programmatic append
    has_subscribe_cta = False
    cta_variants = getattr(ch_config, "SHORTS_SUBSCRIBE_CTA_VARIANTS", [])
    if cta_variants and random.random() < 0.4:
        cta_text = random.choice(cta_variants)
        # ── Word budget guard: skip CTA if script is already long ──
        current_words = sum(len(b.get("texto", "").split()) for b in bloques)
        cta_words = len(cta_text.split())
        if current_words + cta_words > 100:  # 100 words ≈ 42-45s, safe under 55s
            logger.info(
                "[%s] Skipping subscribe CTA — script already %d words (+%d CTA would overflow)",
                channel_slug, current_words, cta_words,
            )
        else:
            bloques.append({
                "tipo": "subscribe_cta",
                "texto": cta_text,
                "search_query_en": "subscribe button youtube channel notification bell",
            })
            has_subscribe_cta = True
            logger.info("[%s] Added subscribe CTA to native short: '%s'", channel_slug, cta_text)

    # 2. Segmented TTS (block-by-block, no mid-phrase truncation)
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
        return

    # 3. Fetch portrait images — build theme-aware English queries
    from pipeline.shorts_media import fetch_portrait_images, render_slideshow_with_images, _build_portrait_query
    theme_kw = script.get("theme_keywords_en", [])
    style_mod = getattr(ch_config, "IMAGE_STYLE_MODIFIERS", "")

    portrait_queries = []
    for b in bloques:
        search_en = b.get("search_query_en", "")
        if search_en and search_en.strip():
            portrait_queries.append(_build_portrait_query(search_en, theme_kw, style_mod))
        else:
            texto = b.get("texto", "")
            if texto.strip():
                portrait_queries.append(texto[:80])

    if not portrait_queries:
        portrait_queries = [hook_text[:80]]
    image_paths = fetch_portrait_images(portrait_queries, ch_config, count=4)

    # 4. Render (slideshow of images + text + audio)
    video_path = output_dir / f"sched_short_{channel_slug}_{ts}.mp4"

    # Background color as fallback
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
        # Solid-bg fallback with subtitles
        from pipeline.shorts_media import _build_solid_bg_filter
        filter_str = _build_solid_bg_filter(
            bg_color,
            srt_path=srt_path if srt_path.exists() else None,
        )
        render_cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x{bg_color}:s=1080x1920:d={audio_duration}:r=30",
            "-i", str(audio_path), "-filter_complex", filter_str,
            "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart", str(video_path)]
        subprocess.run(render_cmd, capture_output=True, timeout=120)

    # 5. Upload
    from pipeline.youtube_uploader import YouTubeUploader
    uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
    if not uploader.authenticate():
        return

    # ── Cross-promotion: link to long-form video ──────────
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
    result = uploader.upload(video_path=video_path, title=title[:100], description=description[:5000], tags=hashtags[:60], category_id=getattr(ch_config, "YT_CATEGORY_ID", "24"), privacy="public")

    yt_id = result.get("video_id")
    if yt_id:
        conn = sqlite3.connect(str(DATABASE_PATH))
        cursor = conn.execute("INSERT INTO shorts (channel_id, type, title, hook_title, hook_text, topic, status, file_path, youtube_id, youtube_url, published_at, has_subscribe_cta, longform_linked, longform_linked_at) VALUES (?, 'native', ?, ?, ?, ?, 'published', ?, ?, ?, datetime('now','localtime'), ?, 1, datetime('now','localtime'))", (channel_id, title, title[:60], hook_text, topic, str(video_path), yt_id, result.get("url", ""), int(has_subscribe_cta)))
        short_id = cursor.lastrowid
        conn.commit()
        conn.close()

        # ── Post-publish cross-promotion ──────────────
        run_post_publish_promotion(
            channel_slug=channel_slug,
            short_yt_id=yt_id,
            channel_id=channel_id,
            source_yt_id=longform_url.split("v=")[-1] if longform_url else None,
            channel_config=ch_config,
        )

        logger.info("Scheduled native Short published: %s → %s", title[:40], result.get("url", ""))
        return short_id

    return None


# ═══════════════════════════════════════════════════════════════
#  Full Replan — "Reprogramar Ahora" button
# ═══════════════════════════════════════════════════════════════

_FULL_REPLAN_COOLDOWN_MIN = 2  # minimum minutes between full replans
_last_full_replan_ts: Optional[datetime] = None


_SAFE_REPLAN_TOKEN_TTL_MINUTES = 10


def _safe_replan_snapshot(db) -> str:
    """Hash the mutable inputs that make a preflight safe to apply."""
    with db._connect() as conn:
        slots = [dict(row) for row in conn.execute("""
            SELECT id, channel_id, date_key, scheduled_at, target_upload_at,
                   target_public_at, upload_window_start, upload_window_end,
                   slot_position, source_mode, status, job_id
            FROM planned_slots
            WHERE status = 'pending'
            ORDER BY id
        """).fetchall()]
        channels = [dict(row) for row in conn.execute("""
            SELECT id, config_json FROM channels WHERE active = 1 ORDER BY id
        """).fetchall()]
        active_jobs = [dict(row) for row in conn.execute("""
            SELECT id, channel_id, video_id, status
            FROM generation_jobs
            WHERE status IN ('queued', 'running')
            ORDER BY id
        """).fetchall()]
        active_videos = [dict(row) for row in conn.execute("""
            SELECT id, channel_id, status
            FROM videos
            WHERE status IN ('generating', 'running', 'awaiting_upload', 'uploading', 'uploaded_private')
            ORDER BY id
        """).fetchall()]
        shorts_slots = [dict(row) for row in conn.execute("""
            SELECT id, channel_id, date_key, scheduled_at, target_upload_at,
                   short_type, long_slot_position, source_video_id, status,
                   job_id, short_id, slot_position
            FROM shorts_planned_slots
            WHERE status IN ('pending', 'running')
            ORDER BY id
        """).fetchall()]
    payload = json.dumps({
        "slots": slots,
        "channels": channels,
        "active_jobs": active_jobs,
        "active_videos": active_videos,
        # Pending Shorts are candidates for in-place retiming; running Shorts
        # are immutable work in progress. Both must make a preflight stale.
        "shorts_slots": shorts_slots,
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_replan_proposed_slots(db, horizon_days: int) -> list[dict]:
    """Build the long-form proposal without writing any planning state."""
    if horizon_days < 1 or horizon_days > 31:
        raise ValueError("horizon_days must be between 1 and 31")

    configs = []
    for channel in db.get_channels(active_only=True):
        config = db.get_channel_planning_config(channel["id"])
        if config.get("planning_enabled", True) and config.get("videos_per_day", 0) > 0:
            configs.append(config)

    proposed = []
    today = date.today()
    for offset in range(horizon_days):
        proposed.extend(compute_daily_slots(
            (today + timedelta(days=offset)).isoformat(), configs, db=db))
    return proposed


class _ShortsProposalDatabase:
    """Read-only DB view that hides existing Shorts from scheduler idempotency.

    The Shorts scheduler suppresses a computed target when a pending slot already
    exists.  A safe replan needs that target in order to retime the existing row
    rather than creating a duplicate, so only this lookup is overridden.
    """

    def __init__(self, db):
        self._db = db

    def get_shorts_planned_slots(self, *args, **kwargs):
        return []

    def __getattr__(self, name):
        return getattr(self._db, name)


def _safe_replan_proposed_shorts(db, horizon_days: int) -> list[dict]:
    """Build a Shorts proposal without persisting or deleting existing slots."""
    from api.services.shorts_scheduler import compute_daily_shorts_slots

    proposal_db = _ShortsProposalDatabase(db)
    today = date.today()
    proposed = []
    for offset in range(horizon_days):
        proposed.extend(compute_daily_shorts_slots(
            (today + timedelta(days=offset)).isoformat(), proposal_db,
        ))
    return proposed


def _safe_replan_review(existing: list[dict], proposed: list[dict], kind: str = "long_form") -> tuple[list[dict], dict]:
    """Reconcile generated capacity with all pending slots, without dropping backlog."""
    existing_by_channel = {}
    proposed_by_channel = {}
    for slot in existing:
        existing_by_channel.setdefault(slot["channel_id"], []).append(slot)
    for slot in proposed:
        proposed_by_channel.setdefault(slot["channel_id"], []).append(slot)

    review = []
    counts = {"retained": 0, "rescheduled": 0, "new": 0}
    for channel_id in sorted(set(existing_by_channel) | set(proposed_by_channel)):
        backlog = sorted(existing_by_channel.get(channel_id, []), key=lambda s: (s["scheduled_at"], s["id"]))
        generated = sorted(proposed_by_channel.get(channel_id, []), key=lambda s: s["scheduled_at"])
        for index, old in enumerate(backlog):
            if index >= len(generated):
                review.append({
                    "kind": kind,
                    "action": "retained",
                    "reason": "backlog_exceeds_horizon",
                    "slot_id": old["id"],
                    "channel_id": channel_id,
                    "before": old,
                    "after": old,
                })
                counts["retained"] += 1
                continue

            replacement = generated[index]
            changed = any(old.get(field) != replacement.get(field) for field in (
                "date_key", "scheduled_at", "target_upload_at", "target_public_at",
                "upload_window_start", "upload_window_end", "slot_position", "source_mode",
            ))
            action = "rescheduled" if changed else "retained"
            review.append({
                "kind": kind,
                "action": action,
                "reason": "backlog_prioritized",
                "slot_id": old["id"],
                "channel_id": channel_id,
                "before": old,
                "after": replacement,
            })
            counts[action] += 1
        for replacement in generated[len(backlog):]:
            review.append({
                "kind": kind,
                "action": "new",
                "reason": "generated_capacity",
                "slot_id": None,
                "channel_id": channel_id,
                "before": None,
                "after": replacement,
            })
            counts["new"] += 1
    return review, counts


def safe_full_replan_preflight(db=None, horizon_days: int = 7) -> dict:
    """Create a non-mutating, expiring confirmation for long-form and Shorts."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    snapshot_hash = _safe_replan_snapshot(db)
    proposed_slots = _safe_replan_proposed_slots(db, horizon_days)
    proposed_shorts = _safe_replan_proposed_shorts(db, horizon_days)
    with db._connect() as conn:
        existing = [dict(row) for row in conn.execute("""
            SELECT * FROM planned_slots WHERE status = 'pending' ORDER BY scheduled_at, id
        """).fetchall()]
        existing_shorts = [dict(row) for row in conn.execute("""
            SELECT * FROM shorts_planned_slots WHERE status = 'pending' ORDER BY scheduled_at, id
        """).fetchall()]
    long_review, long_counts = _safe_replan_review(existing, proposed_slots)
    shorts_review, shorts_counts = _safe_replan_review(
        existing_shorts, proposed_shorts, kind="short",
    )
    review = long_review + shorts_review
    # Keep flat long-form counters for compatibility while making per-kind
    # counts explicit for clients of this expanded contract.
    counts = {
        **long_counts,
        "long_form": long_counts,
        "shorts": shorts_counts,
        "total": {
            action: long_counts[action] + shorts_counts[action]
            for action in ("retained", "rescheduled", "new")
        },
    }
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=_SAFE_REPLAN_TOKEN_TTL_MINUTES)).isoformat()

    with db._connect() as conn:
        conn.execute("""
            INSERT INTO safe_replan_confirmations
                (token_hash, snapshot_hash, plan_json, expires_at)
            VALUES (?, ?, ?, ?)
        """, (token_hash, snapshot_hash, json.dumps({"review": review, "counts": counts}), expires_at))
        conn.commit()

    return {
        "ok": True,
        "confirmation_token": token,
        "expires_at": expires_at,
        "snapshot_hash": snapshot_hash,
        "proposed_slots": proposed_slots,
        "proposed_shorts": proposed_shorts,
        "review": review,
        "counts": counts,
        "covers": "long-form + Shorts",
        "summary": {
            "message": "Safe replan covers long-form + Shorts.",
            "long_form": {"proposed": len(proposed_slots), **long_counts},
            "shorts": {"proposed": len(proposed_shorts), **shorts_counts},
            "total_proposed": len(proposed_slots) + len(proposed_shorts),
            "horizon_days": horizon_days,
        },
    }


def safe_full_replan_apply(confirmation_token: str, db=None) -> dict:
    """Apply a preflight in place; it never deletes slots or cancels jobs."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    if not confirmation_token:
        raise ValueError("confirmation token is required")

    token_hash = hashlib.sha256(confirmation_token.encode("utf-8")).hexdigest()
    with db._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        confirmation = conn.execute("""
            SELECT * FROM safe_replan_confirmations WHERE token_hash = ?
        """, (token_hash,)).fetchone()
        if not confirmation:
            conn.rollback()
            raise ValueError("confirmation token is invalid")
        if confirmation["used_at"] is not None:
            conn.rollback()
            raise ValueError("confirmation token was already used")
        if datetime.fromisoformat(confirmation["expires_at"]) <= datetime.now(timezone.utc):
            conn.execute("UPDATE safe_replan_confirmations SET used_at = CURRENT_TIMESTAMP WHERE id = ?", (confirmation["id"],))
            conn.commit()
            raise ValueError("confirmation token has expired")
        if _safe_replan_snapshot(db) != confirmation["snapshot_hash"]:
            conn.execute("UPDATE safe_replan_confirmations SET used_at = CURRENT_TIMESTAMP WHERE id = ?", (confirmation["id"],))
            conn.commit()
            raise ValueError("replan confirmation is stale")

        plan = json.loads(confirmation["plan_json"])
        review = plan["review"]
        actual_long_counts = {"retained": 0, "rescheduled": 0, "new": 0}
        actual_shorts_counts = {"retained": 0, "rescheduled": 0, "new": 0}
        # Apply every existing Short first. This guarantees a pending Shorts
        # backlog is retained/re-timed before the transaction inserts a new one.
        ordered_review = [item for item in review if item["action"] != "new"] + [
            item for item in review if item["action"] == "new"
        ]
        for item in ordered_review:
            action = item["action"]
            kind = item.get("kind", "long_form")
            actual_counts = actual_shorts_counts if kind == "short" else actual_long_counts
            if action == "retained":
                actual_counts["retained"] += 1
            elif action == "rescheduled":
                new = item["after"]
                # Deliberately excludes id, status, job_id, and video_id.
                if kind == "short":
                    # Preserve type, source/long pairing, job, short, and status.
                    cursor = conn.execute("""
                        UPDATE shorts_planned_slots
                        SET date_key = ?, scheduled_at = ?, target_upload_at = ?, slot_position = ?
                        WHERE id = ? AND status = 'pending'
                    """, (
                        new["date_key"], new["scheduled_at"], new.get("target_upload_at"),
                        new.get("slot_position", 0), item["slot_id"],
                    ))
                else:
                    cursor = conn.execute("""
                        UPDATE planned_slots
                        SET date_key = ?, scheduled_at = ?, target_upload_at = ?, target_public_at = ?,
                            upload_window_start = ?, upload_window_end = ?,
                            slot_position = ?, source_mode = ?
                        WHERE id = ? AND status = 'pending'
                    """, (
                        new["date_key"], new["scheduled_at"], new.get("target_upload_at"), new.get("target_public_at"),
                        new.get("upload_window_start", 9), new.get("upload_window_end", 11),
                        new.get("slot_position", 0), new.get("source_mode", "original"), item["slot_id"],
                    ))
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise ValueError("replan confirmation is stale")
                actual_counts["rescheduled"] += 1
            elif action == "new":
                new = item["after"]
                if kind == "short":
                    conn.execute("""
                        INSERT INTO shorts_planned_slots
                            (channel_id, date_key, scheduled_at, target_upload_at, short_type,
                             long_slot_position, source_video_id, slot_position, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    """, (
                        new["channel_id"], new["date_key"], new["scheduled_at"],
                        new.get("target_upload_at"), new.get("short_type", "native"),
                        new.get("long_slot_position"), new.get("source_video_id"),
                        new.get("slot_position", 0),
                    ))
                else:
                    conn.execute("""
                        INSERT INTO planned_slots
                            (channel_id, date_key, scheduled_at, target_upload_at, target_public_at,
                             upload_window_start, upload_window_end, slot_position, source_mode, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    """, (
                        new["channel_id"], new["date_key"], new["scheduled_at"], new.get("target_upload_at"),
                        new.get("target_public_at"), new.get("upload_window_start", 9),
                        new.get("upload_window_end", 11), new.get("slot_position", 0),
                        new.get("source_mode", "original"),
                    ))
                actual_counts["new"] += 1

        conn.execute("UPDATE safe_replan_confirmations SET used_at = CURRENT_TIMESTAMP WHERE id = ?", (confirmation["id"],))
        conn.commit()

    # ── v40: este replan manual aplicado cuenta como último replan de
    # horizonte (resetea el gate de 24h) y activa la ventana silenciosa
    # post-full_replan. Persistido en system_state para sobrevivir reinicios.
    global _last_horizon_replan_ts, _post_full_replan_block_until
    _last_horizon_replan_ts = datetime.now()
    _post_full_replan_block_until = datetime.now() + timedelta(minutes=_POST_FULL_REPLAN_QUIET_MIN)
    try:
        db.set_system_state(_LAST_REPLAN_KEY, str(time.time()))
        db.set_system_state(
            "post_full_replan_block_until",
            str(_post_full_replan_block_until.timestamp()),
        )
    except Exception:
        pass
    logger.info(
        "safe_full_replan_apply: quiet window activated — auto-replans blocked for %d min",
        _POST_FULL_REPLAN_QUIET_MIN,
    )

    return {
        "ok": True,
        "counts": {
            **actual_long_counts,
            "long_form": actual_long_counts,
            "shorts": actual_shorts_counts,
            "total": {
                action: actual_long_counts[action] + actual_shorts_counts[action]
                for action in ("retained", "rescheduled", "new")
            },
        },
        "review": review,
        "covers": "long-form + Shorts",
        # Compatibility aliases for the initial safe-replan response.
        "updated": actual_long_counts["rescheduled"],
        "created": actual_long_counts["new"],
        "preserved": actual_long_counts["retained"],
    }


@_replan_locked
def full_replan(db=None) -> dict:
    """Force a complete planning reset: delete ALL pending planned slots
    (videos + shorts) and recreate from scratch across the 7-day horizon.

    Key differences from compute_and_store_horizon:
        1. Also nukes shorts pending slots
        2. Cancels ALL queued generation_jobs (orphaned)
        3. For today: computes RESIDUAL quota (full quota - already committed)
           so slots match reality -- no double-booking
        4. Returns detailed summary for frontend notification

    Returns:
        dict with {ok, videos, shorts, jobs_cancelled, catchup_slots, next_slot, summary}.
    """
    from datetime import date as _date, datetime as _dt, timedelta as _td

    # ── Cooldown guard ──────────────────────────────────
    global _last_full_replan_ts
    now = _dt.now()
    if _last_full_replan_ts is not None:
        elapsed = (now - _last_full_replan_ts).total_seconds() / 60
        if elapsed < _FULL_REPLAN_COOLDOWN_MIN:
            logger.info(
                "full_replan: skipped -- last replan %.0f min ago (cooldown=%d min)",
                elapsed, _FULL_REPLAN_COOLDOWN_MIN,
            )
            return {
                "ok": False,
                "skipped": True,
                "reason": f"Cooldown activo -- espera {_FULL_REPLAN_COOLDOWN_MIN - int(elapsed)} min",
            }
    _last_full_replan_ts = now

    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    today = _date.today()
    horizon_days = 7

    # ═══════════════════════════════════════════════════════════
    #  FASE 1 -- Auditar estado actual (que hay en vuelo)
    # ═══════════════════════════════════════════════════════════
    channels = db.get_channels(active_only=True)
    channel_states = {}

    for ch in channels:
        ch_id = ch["id"]
        slug = ch["slug"]
        if slug == "test":
            continue

        cfg = db.get_channel_planning_config(ch_id)
        if not cfg.get("planning_enabled", True):
            continue

        # Videos committed (in-flight, cannot be touched)
        with db._connect() as conn:
            running_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM videos WHERE channel_id=? AND status='running'",
                (ch_id,),
            ).fetchone()
            running = running_row["cnt"] if running_row else 0

        awaiting = db.count_awaiting_upload(ch_id)
        published_today = db.get_videos_published_today(ch_id)

        # Warming (uploaded_private) = already on YT as private
        with db._connect() as conn:
            warming_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM videos "
                "WHERE channel_id=? AND status='uploaded_private' AND published_at IS NULL",
                (ch_id,),
            ).fetchone()
            warming = warming_row["cnt"] if warming_row else 0

        videos_committed = running + awaiting + published_today + warming

        # Shorts committed
        with db._connect() as conn:
            shorts_running = conn.execute(
                "SELECT COUNT(*) as cnt FROM shorts WHERE channel_id=? AND status IN ('rendering','uploading')",
                (ch_id,),
            ).fetchone()["cnt"]

        shorts_published_today = db.get_shorts_published_today(ch_id)

        with db._connect() as conn:
            shorts_slots_running = conn.execute(
                "SELECT COUNT(*) as cnt FROM shorts_planned_slots WHERE channel_id=? AND status='running'",
                (ch_id,),
            ).fetchone()["cnt"]

        shorts_committed = shorts_running + shorts_published_today + shorts_slots_running

        channel_states[ch_id] = {
            "slug": slug,
            "name": ch["name"],
            "videos_committed": videos_committed,
            "shorts_committed": shorts_committed,
            "videos_per_day": _resolve_videos_per_day(cfg, today.isoformat()),
        }

    # ═══════════════════════════════════════════════════════════
    #  FASE 2 -- Borrar TODOS los slots pendientes
    # ═══════════════════════════════════════════════════════════
    with db._connect() as conn:
        deleted_videos = conn.execute(
            "DELETE FROM planned_slots WHERE status = 'pending'"
        ).rowcount
        deleted_shorts = conn.execute(
            "DELETE FROM shorts_planned_slots WHERE status IN ('pending', 'cancelled')"
        ).rowcount
        orphaned = conn.execute(
            "UPDATE generation_jobs SET status = 'cancelled', "
            "error_msg = 'Full replan: slot was deleted', "
            "finished_at = datetime('now') "
            "WHERE status = 'queued'"
        ).rowcount
        conn.commit()

    logger.info(
        "Full replan: cleared %d video slots, %d shorts slots, cancelled %d orphaned jobs",
        deleted_videos, deleted_shorts, orphaned,
    )

    # ═══════════════════════════════════════════════════════════
    #  FASE 3 -- Continuous pipeline: chain from NOW using per-channel durations
    # ═══════════════════════════════════════════════════════════
    # Old approach: distributed slots across fixed windows (10-13, 14-17, 18-22)
    #   → created dead zones (e.g. 02:45→08:49 gap) and overlapped when gen took >2h
    # New approach: chain slots back-to-back starting NOW, using real per-channel
    #   pipeline durations from historical timing_data. Slots interleave channels
    #   via round-robin so no channel starves.

    # 3a. Get per-channel pipeline duration (from history or heuristic)
    from api.services.schedule_engine import get_avg_creation_minutes, estimate_remaining_minutes

    channel_pipeline = {}  # channel_id → pipeline_minutes
    channel_upload_windows = {}  # channel_id → [(start, end), ...]
    for ch in channels:
        ch_id = ch["id"]
        slug = ch["slug"]
        if slug == "test":
            continue
        cfg = db.get_channel_planning_config(ch_id)
        if not cfg.get("planning_enabled", True):
            continue

        # Pipeline duration from real historical data
        raw_avg = None
        try:
            raw_avg = get_avg_creation_minutes(ch_id, n=5)
            pipeline_min = raw_avg * (1 + BUFFER_PCT)  # +15% safety buffer
        except Exception:
            pipeline_min = ESTIMATED_PIPELINE_MINUTES

        # Floor: minimum 90 min (never schedule tighter than this)
        pipeline_min = max(pipeline_min, 90)

        channel_pipeline[ch_id] = pipeline_min

        # Parse upload windows for scheduled channels
        upload_windows_raw = cfg.get("upload_windows")
        if upload_windows_raw and isinstance(upload_windows_raw, list):
            wins = []
            for w in upload_windows_raw:
                if isinstance(w, dict) and "start" in w and "end" in w:
                    wins.append((int(w["start"]), int(w["end"])))
            if wins:
                channel_upload_windows[ch_id] = wins
                continue
        # Backward-compat fallback
        ws = int(cfg.get("upload_window_start", 9))
        we = int(cfg.get("upload_window_end", 11))
        channel_upload_windows[ch_id] = [(ws, we)]

        logger.info(
            "Full replan pipeline: %s avg=%.0f min (historical=%.0f min + %.0f%% buffer)",
            slug, pipeline_min, raw_avg if raw_avg else 0, BUFFER_PCT * 100,
        )

    # 3b. Build flat round-robin slot list across all 7 days — TRUE per-slot interleaving
    # Each round adds 1 slot per active channel. Channels that hit their daily quota skip.
    # This ensures fair distribution: C4, C3, C2, C5, C4, C3, C2, C5, ... not C4×6, C3×2, ...

    ch_order = [ch["id"] for ch in channels if ch["id"] in channel_pipeline]
    # Deterministic starting offset per day
    day_seed_global = _day_seed(today.isoformat())
    rr_start = day_seed_global % len(ch_order) if ch_order else 0

    # Pre-compute daily quotas for all channels across all days
    daily_quotas = {}  # (channel_id, date_key) → total_vpd
    for day_offset in range(horizon_days):
        day_str = (today + _td(days=day_offset)).isoformat()
        for ch_id in ch_order:
            cfg = db.get_channel_planning_config(ch_id)
            if day_offset == 0:
                state = channel_states.get(ch_id, {"videos_committed": 0, "videos_per_day": 2})
                vpd = max(0, state["videos_per_day"] - state["videos_committed"])
            else:
                vpd = _resolve_videos_per_day(
                    {"channel_id": ch_id, "videos_per_day": cfg.get("videos_per_day", 2),
                     "videos_day_boost_weight": cfg.get("videos_day_boost_weight", 0.7)},
                    day_str,
                )
            daily_quotas[(ch_id, day_str)] = vpd

    # Build interleaved slot queue: one round = one slot per channel that still has quota
    slot_queue = []
    remaining = dict(daily_quotas)  # (ch_id, day_str) → slots left
    max_remaining = sum(remaining.values())
    round_idx = 0

    while max_remaining > 0:
        # Round-robin: start at offset position, cycle through all channels
        for offset in range(len(ch_order)):
            ch_id = ch_order[(rr_start + offset) % len(ch_order)]

            # Find the earliest day for this channel that still has remaining slots
            for day_offset in range(horizon_days):
                day_str = (today + _td(days=day_offset)).isoformat()
                key = (ch_id, day_str)
                if remaining.get(key, 0) > 0:
                    remaining[key] -= 1
                    vpd_used = daily_quotas[key] - remaining[key]  # 1-indexed position

                    slug = next((c["slug"] for c in channels if c["id"] == ch_id), "?")
                    cfg = db.get_channel_planning_config(ch_id)
                    mode_seq = _build_source_mode_sequence(
                        daily_quotas[key], cfg, day_str,
                    ) if daily_quotas[key] > 0 else ["original"] * max(1, daily_quotas[key])
                    source_mode = mode_seq[vpd_used - 1] if (vpd_used - 1) < len(mode_seq) else "original"

                    slot_queue.append({
                        "channel_id": ch_id,
                        "channel_slug": slug,
                        "date_key": day_str,
                        "source_mode": source_mode,
                        "pipeline_minutes": channel_pipeline.get(ch_id, ESTIMATED_PIPELINE_MINUTES),
                        "slot_number_within_day": vpd_used,
                        "publish_mode": cfg.get("publish_mode", "immediate"),
                        "publish_timezone": cfg.get("publish_timezone", "Europe/Madrid"),
                        "publish_warmup_min": cfg.get("publish_warmup_min", 120),
                        "upload_windows": channel_upload_windows.get(ch_id, [(9, 11)]),
                    })
                    break  # one slot per channel per round

            if max(remaining.values()) == 0:
                break

        max_remaining = max(remaining.values())
        round_idx += 1

    # 3c. Chain slots from NOW (pipeline continua)
    # ── Offset start for active generations on ANY channel ──
    # If a channel is already generating a video, push the pipeline cursor
    # past its estimated completion so the first planned slot doesn't start
    # before the current job has finished.
    max_active_remaining_min = 0.0
    for ch in channels:
        ch_id = ch["id"]
        if ch.get("slug") == "test":
            continue
        try:
            remaining = estimate_remaining_minutes(ch_id)
            if remaining > 0:
                logger.info(
                    "Full replan: channel %s has active generation, "
                    "estimated %.1f min remaining",
                    ch.get("slug"), remaining,
                )
                max_active_remaining_min = max(max_active_remaining_min, remaining)
        except Exception as exc:
            logger.warning(
                "Full replan: error estimating remaining for channel %s: %s",
                ch.get("slug"), exc,
            )

    if max_active_remaining_min > 0:
        pipeline_cursor = now + _td(minutes=max_active_remaining_min + GLOBAL_GAP_MINUTES)
        logger.info(
            "Full replan: pipeline delayed by %.1f min (active gen remaining %.1f + %d min gap)",
            max_active_remaining_min + GLOBAL_GAP_MINUTES,
            max_active_remaining_min, GLOBAL_GAP_MINUTES,
        )
    else:
        pipeline_cursor = now  # current time in the pipeline chain
    last_channel_target = {}  # channel_id → last target_upload_at (for same-channel gap)
    resolved_videos = []
    channel_day_count = {}  # (channel_id, date_key) → count (for slot_position within day)

    for idx, slot in enumerate(slot_queue):
        ch_id = slot["channel_id"]
        slug = slot["channel_slug"]
        date_key = slot["date_key"]
        pipeline_min = slot["pipeline_minutes"]
        is_scheduled = slot["publish_mode"] == "scheduled"
        warmup_min = slot["publish_warmup_min"]
        windows = slot["upload_windows"]

        # ── Scheduled_at: starts right after previous slot finishes ──
        scheduled_at = pipeline_cursor

        # ── Target_upload_at: when the video should be uploading ──
        upload_target = scheduled_at + _td(minutes=pipeline_min)

        # ── For scheduled channels: align upload to next available window ──
        actual_target_public_at = None
        if is_scheduled and windows:
            # Check if upload_target falls within any upload window for this channel
            upload_hour = upload_target.hour
            upload_min = upload_target.minute
            in_window = any(
                w[0] <= upload_hour < w[1] or (upload_hour == w[1] - 1 and upload_min < 30)
                for w in windows
            )
            if not in_window:
                # Upload is outside permitted windows → push to next available window
                found = False
                for attempt_day in range(4):  # try up to 4 days ahead
                    check_date = upload_target.date() + _td(days=attempt_day)
                    for w in windows:
                        # Build a candidate at the start of this window
                        jitter = hash(f"{ch_id}|{idx}|{check_date}") % 60
                        candidate_h = w[0]
                        candidate_m = jitter
                        candidate = _dt(check_date.year, check_date.month, check_date.day,
                                        candidate_h, candidate_m, 0)
                        if candidate > upload_target:
                            upload_target = candidate
                            found = True
                            break
                    if found:
                        break
                if not found:
                    # Extreme fallback: keep original, video sits in upload queue
                    pass

            # ── target_public_at = upload + warmup (for scheduled channels) ──
            actual_target_public_at = upload_target + _td(minutes=warmup_min)

        # ── Same-channel publish gap: 3h minimum ──
        if is_scheduled and ch_id in last_channel_target:
            last_target = last_channel_target[ch_id]
            min_gap = _td(hours=MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS)
            if upload_target - last_target < min_gap:
                upload_target = last_target + min_gap
                # Recalculate scheduled_at backwards
                scheduled_at = upload_target - _td(minutes=pipeline_min + warmup_min)

        # ── Floor: never schedule in the past ──
        if scheduled_at < now + _td(minutes=1):
            scheduled_at = now + _td(minutes=2)
            upload_target = scheduled_at + _td(minutes=pipeline_min)

        # ── Update pipeline cursor for next slot ──
        # Generation finishes at scheduled_at + pipeline_minutes.
        # The NEXT slot can start after that (with gap), regardless of when
        # the upload actually happens. For scheduled channels, the video
        # sits in the upload queue — the pipeline doesn't block on it.
        gen_finish = scheduled_at + _td(minutes=pipeline_min)
        pipeline_cursor = gen_finish + _td(minutes=GLOBAL_GAP_MINUTES)

        # Track last target per channel
        last_channel_target[ch_id] = upload_target

        # Per-day slot position
        key = (ch_id, date_key)
        channel_day_count[key] = channel_day_count.get(key, 0) + 1

        # Determine if it's a catchup slot (scheduled before now + was overridden)
        is_catchup = abs((scheduled_at - (now + _td(minutes=2))).total_seconds()) < 5

        # ── Build slot dict ──
        date_key_str = scheduled_at.strftime("%Y-%m-%d")  # actual gen date (may differ from planned if spans midnight)

        # target_public_at for scheduled channels
        target_public_at = None
        if is_scheduled:
            target_public_at = upload_target + _td(minutes=warmup_min)
            # Convert to UTC ISO for DB storage
            target_public_at_str = _naive_local_to_utc(
                target_public_at.strftime("%Y-%m-%d %H:%M:%S"),
                slot["publish_timezone"],
            )
        else:
            target_public_at_str = None

        resolved_videos.append({
            "channel_id": ch_id,
            "date_key": date_key_str,
            "scheduled_at": scheduled_at.strftime("%Y-%m-%d %H:%M:%S"),
            "target_upload_at": upload_target.strftime("%Y-%m-%d %H:%M:%S"),
            "target_public_at": target_public_at_str,
            "slot_position": channel_day_count[key],
            "channel_name": slot.get("channel_name", slug),
            "channel_slug": slug,
            "source_mode": slot["source_mode"],
            "catchup": is_catchup,
            "publish_mode": slot["publish_mode"],
            "publish_timezone": slot["publish_timezone"],
            "upload_window_start": windows[0][0] if windows else 9,
            "upload_window_end": windows[0][1] if windows else 11,
        })

    # ── Log pipeline summary ──
    if resolved_videos:
        first = resolved_videos[0]
        last = resolved_videos[-1]
        total_gen_hours = sum(s["pipeline_minutes"] for s in slot_queue) / 60 if slot_queue else 0
        logger.info(
            "Full replan pipeline: %d slots, first=%s @%s, last=%s @%s, "
            "total gen time=%.1fh, pipeline fills until %s",
            len(resolved_videos),
            first.get("channel_slug", "?"), first["scheduled_at"][11:16],
            last.get("channel_slug", "?"), last["scheduled_at"][11:16],
            total_gen_hours,
            pipeline_cursor.strftime("%Y-%m-%d %H:%M"),
        )

    # Persist video slots
    stored_videos = 0
    if resolved_videos:
        stored_videos = db.create_planned_slots_batch(resolved_videos)

    # ═══════════════════════════════════════════════════════════
    #  FASE 4 -- Planificar shorts (horizonte completo)
    # ═══════════════════════════════════════════════════════════
    stored_shorts = 0
    try:
        from api.services.shorts_scheduler import generate_upcoming_shorts
        shorts_result = generate_upcoming_shorts(days=horizon_days, db=db)
        stored_shorts = sum(
            int(v.split()[0]) for v in shorts_result.values()
            if v and isinstance(v, str) and v[0].isdigit()
        )
    except Exception as e:
        logger.error("Full replan: shorts generation failed: %s", e)
        shorts_result = {"error": str(e)}

    # 4a. Cancel excess shorts for today (beyond residual)
    for ch in channels:
        ch_id = ch["id"]
        slug = ch["slug"]
        if slug == "test":
            continue

        state = channel_states.get(ch_id, {"shorts_committed": 0})
        shorts_committed = state["shorts_committed"]

        cfg = db.get_channel_planning_config(ch_id)
        shorts_native_per_day = cfg.get("shorts_native_per_day", 3)

        # Count today's pending shorts per type
        with db._connect() as conn:
            native_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM shorts_planned_slots "
                "WHERE channel_id=? AND date_key=? AND status='pending' AND short_type='native'",
                (ch_id, today.isoformat()),
            ).fetchone()["cnt"]

        excess_native = max(0, native_count + shorts_committed - shorts_native_per_day)
        if excess_native > 0:
            with db._connect() as conn:
                conn.execute(
                    "DELETE FROM shorts_planned_slots WHERE id IN ("
                    "SELECT id FROM shorts_planned_slots "
                    "WHERE channel_id=? AND date_key=? AND status='pending' AND short_type='native' "
                    "ORDER BY scheduled_at DESC LIMIT ?)",
                    (ch_id, today.isoformat(), excess_native),
                )
                conn.commit()
            logger.info(
                "Full replan: cancelled %d excess native shorts for %s (quota %d)",
                excess_native, slug, shorts_native_per_day,
            )

    # ═══════════════════════════════════════════════════════════
    #  FASE 5 -- Build summary
    # ═══════════════════════════════════════════════════════════
    catchup_slots = sum(1 for s in resolved_videos if s.get("catchup"))
    next_slot = None
    if resolved_videos:
        next_s = resolved_videos[0]
        next_slot = {
            "time": next_s["scheduled_at"][11:16],
            "channel": next_s.get("channel_slug", "?"),
            "kind": "video",
            "catchup": next_s.get("catchup", False),
        }

    videos_by_channel = {}
    for s in resolved_videos:
        slug = s.get("channel_slug", "?")
        if slug not in videos_by_channel:
            videos_by_channel[slug] = 0
        videos_by_channel[slug] += 1

    # ── Set post-full_replan quiet window to prevent automatic overwrites ──
    # v40: persistido en system_state para que un reinicio de la API no pierda
    # la ventana silenciosa. Además, el replan manual resetea el gate de 24h.
    global _post_full_replan_block_until, _last_horizon_replan_ts
    _post_full_replan_block_until = _dt.now() + _td(minutes=_POST_FULL_REPLAN_QUIET_MIN)
    _last_horizon_replan_ts = _dt.now()
    try:
        db.set_system_state("post_full_replan_block_until", str(_post_full_replan_block_until.timestamp()))
        db.set_system_state(_LAST_REPLAN_KEY, str(time.time()))
    except Exception:
        pass
    logger.info(
        "full_replan: quiet window activated — auto-replans blocked for %d min",
        _POST_FULL_REPLAN_QUIET_MIN,
    )

    return {
        "ok": True,
        "videos": {
            "deleted": deleted_videos,
            "created": stored_videos,
            "by_channel": videos_by_channel,
        },
        "shorts": {
            "deleted": deleted_shorts,
            "created": stored_shorts,
        },
        "jobs_cancelled": orphaned,
        "catchup_slots": catchup_slots,
        "next_slot": next_slot,
        "summary": (
            f"Borrados {deleted_videos} videos + {deleted_shorts} shorts pendientes. "
            f"Creados {stored_videos} videos + {stored_shorts} shorts. "
            f"Jobs huefanos cancelados: {orphaned}. "
            f"Slots catchup (atrasados): {catchup_slots}."
        ),
    }


def _resolve_cross_day_collisions(slots: list[dict]) -> None:
    """Post-processing: resolve target_upload_at collisions after global sort.

    Walk the globally sorted list and push overlapping same-channel
    target_upload_at forward by MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS.
    Also recalculate target_upload_at from scheduled_at for non-scheduled channels.
    """
    from datetime import datetime as _dt, timedelta as _td

    # Group by channel and sort
    by_channel = {}
    for s in slots:
        ch_id = s["channel_id"]
        if ch_id not in by_channel:
            by_channel[ch_id] = []
        by_channel[ch_id].append(s)

    min_gap = MIN_SAME_CHANNEL_PUBLISH_GAP_HOURS * 60
    for ch_slots in by_channel.values():
        ch_slots.sort(key=lambda s: s["target_upload_at"])
        for i in range(1, len(ch_slots)):
            prev = ch_slots[i - 1]
            curr = ch_slots[i]
            prev_h, prev_m = map(int, prev["target_upload_at"][11:16].split(":"))
            curr_h, curr_m = map(int, curr["target_upload_at"][11:16].split(":"))
            gap = (curr_h * 60 + curr_m) - (prev_h * 60 + prev_m)
            if gap < min_gap:
                new_total = prev_h * 60 + prev_m + min_gap
                nh = min(new_total // 60, 23)
                nm = new_total % 60
                push_delta = new_total - (curr_h * 60 + curr_m)
                curr["target_upload_at"] = (
                    f"{curr['date_key']} {nh:02d}:{nm:02d}:00"
                )
                # ── v (Aug 2026): keep target_public_at >= target_upload_at ──
                # Only applies to scheduled slots where target_public_at is a
                # naive local string (ISO/UTC strings are left untouched).
                if curr.get("publish_mode") == "scheduled":
                    tpa = curr.get("target_public_at")
                    if tpa and " " in str(tpa):
                        try:
                            _tpa_dt = _dt.strptime(str(tpa)[:19], "%Y-%m-%d %H:%M:%S")
                            _tpa_dt = _tpa_dt + _td(minutes=push_delta)
                            curr["target_public_at"] = _tpa_dt.strftime("%Y-%m-%d %H:%M:%S")
                        except (ValueError, TypeError):
                            pass

    # Recalculate target_upload_at for non-scheduled
    for s in slots:
        if s.get("publish_mode") != "scheduled":
            sched_h, sched_m = map(int, s["scheduled_at"][11:16].split(":"))
            up_total = sched_h * 60 + sched_m + ESTIMATED_PIPELINE_MINUTES
            uh = min(up_total // 60, 23)
            um = up_total % 60
            s["target_upload_at"] = f"{s['date_key']} {uh:02d}:{um:02d}:00"
