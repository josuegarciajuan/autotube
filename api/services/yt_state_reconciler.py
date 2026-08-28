#!/usr/bin/env python3
"""Reconciler of the REAL publication state of shorts (0 YouTube Data API quota).

Root cause (ago 2026): shorts were marked status='published' + published_at=now
at UPLOAD time, even when uploaded PRIVATE with a future publishAt. The DB never
verified that the short actually went public, so the UI could show "published"
while YouTube still had it scheduled/private — or, worse, silently removed.

This module reconciles the external truth into derived columns on `shorts`:
  - publish_at          : scheduled publish time (set at upload).
  - yt_visibility       : 'public' | 'scheduled' | 'private' | 'age_restricted'
                          | 'removed' | 'unavailable' | 'unknown' | 'error'
  - yt_checked_at       : when the last external check happened.
  - yt_checked_source   : 'upload' | 'ytdlp' | 'rss' | 'data_api' | 'studio'

Classification is done with yt-dlp (innertube web client, 0 quota) plus the
public RSS feed as a cross-check for "public". No changes to the canonical
`status` column, so all existing queries/dashboards keep working.

Stuck detection: a short that is still 'private' after its publish_at has passed
(plus a grace window) raises a deduplicated alert so it is never silently lost.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("autotube.yt_state_reconciler")

# ── Tuning ────────────────────────────────────────────────────
RECONCILE_LOOKBACK_HOURS = 48        # only recent shorts
RECONCILE_MAX_PER_CHANNEL = 10       # cap checks per channel per run
RECONCILE_SHORT_COOLDOWN_SEC = 600   # don't re-check the same short more often
RECONCILE_FEED_CACHE_SEC = 300       # reuse one RSS fetch per channel for 5 min
STUCK_GRACE_MIN = 45                 # after publish_at, allow this lag before alerting

ALERT_TYPE_STUCK = "short_publish_stuck"

_FEED_CACHE: dict[int, tuple] = {}


def classify_video_visibility(yt_id: str) -> str:
    """Classify a video's real public state via yt-dlp (0 Data API quota).

    Returns one of: 'public', 'private', 'age_restricted', 'removed',
    'unavailable', 'unknown', 'error'.
    """
    import yt_dlp
    # yt-dlp imprime los fallos (private/age/removed) a stderr por defecto;
    # los silenciamos para no ensuciar los logs de producción.
    for _lg_name in ("yt_dlp", "youtube_dl"):
        logging.getLogger(_lg_name).setLevel(logging.CRITICAL)
    if not yt_id:
        return "unknown"
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True, "skip_download": True, "no_warnings": True,
        }) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={yt_id}", download=False,
            )
        if info:
            return "public"
        return "unknown"
    except Exception as exc:
        msg = str(exc).lower()
        if "sign in to confirm your age" in msg or "inappropriate for some users" in msg:
            return "age_restricted"
        if "private video" in msg or "granted access" in msg:
            return "private"
        if "video unavailable" in msg or "isn't available" in msg:
            return "removed"
        if "has been removed" in msg or "removed for violating" in msg:
            return "removed"
        if "unavailable" in msg:
            return "unavailable"
        return "error"


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _recent_shorts(db, lookback_hours: int = RECONCILE_LOOKBACK_HOURS) -> list[dict]:
    """Shorts recently uploaded that still need external verification.

    Includes BOTH status='published' (maybe optimistically marked) and
    status='scheduled' (waiting for a future publishAt). This lets the
    reconciler both confirm scheduled→published and downgrade published→scheduled
    when YouTube still has the short private/scheduled (backfill of shorts that
    were optimistically written as 'published' before v48).
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    with db._connect() as conn:
        rows = conn.execute(
            """SELECT id, channel_id, youtube_id, status, publish_at, published_at,
                      yt_visibility, yt_checked_at
               FROM shorts
               WHERE youtube_id IS NOT NULL AND youtube_id != ''
                 AND status IN ('published', 'scheduled')
                 AND (COALESCE(published_at, created_at, publish_at) >= ?)
               ORDER BY COALESCE(published_at, created_at) DESC
               LIMIT ?""",
            (since, lookback_hours * 12),
        ).fetchall()
    return [dict(r) for r in rows]


def _feed_public_ids(db, channel_id: int) -> dict:
    """Public IDs of a channel via its RSS feed (0 quota), cached in-process."""
    if channel_id in _FEED_CACHE and time.time() - _FEED_CACHE[channel_id][0] < RECONCILE_FEED_CACHE_SEC:
        return _FEED_CACHE[channel_id][1]
    ids = {}
    try:
        from pipeline.youtube_wall_scraper import fetch_channel_public_video_ids
        with db._connect() as conn:
            ch = conn.execute("SELECT yt_channel_id FROM channels WHERE id=?", (channel_id,)).fetchone()
        if ch and ch["yt_channel_id"]:
            ids = fetch_channel_public_video_ids(ch["yt_channel_id"]) or {}
    except Exception as exc:
        logger.debug("RSS feed fetch failed for channel #%s: %s", channel_id, exc)
    _FEED_CACHE[channel_id] = (time.time(), ids)
    return ids


def reconcile_recent_shorts(db=None) -> dict:
    """Reconcile the real publication state of recent shorts.

    Returns a summary dict: {checked, updated, public, private, age_restricted,
    removed, stuck, errors}.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    summary = {"checked": 0, "updated": 0, "public": 0, "private": 0,
               "age_restricted": 0, "removed": 0, "stuck": 0, "errors": 0}
    now = datetime.now(timezone.utc)
    shorts = _recent_shorts(db)

    by_channel: dict[int, list[dict]] = {}
    for s in shorts:
        by_channel.setdefault(int(s["channel_id"]), []).append(s)

    for channel_id, items in by_channel.items():
        feed = _feed_public_ids(db, channel_id)
        for s in items[:RECONCILE_MAX_PER_CHANNEL]:
            short_id = int(s["id"])
            yt_id = s["youtube_id"]
            checked_at = _parse_iso(s.get("yt_checked_at"))
            if checked_at and (now - checked_at).total_seconds() < RECONCILE_SHORT_COOLDOWN_SEC:
                continue  # recent check, respect cooldown

            vis = classify_video_visibility(yt_id)
            summary["checked"] += 1

            # RSS feed is authoritative for "public".
            if vis in ("public", "unknown", "error") and yt_id in feed:
                vis = "public"

            publish_at = _parse_iso(s.get("publish_at"))
            is_stuck = (
                vis in ("private", "scheduled")
                and publish_at is not None
                and (now - publish_at).total_seconds() > STUCK_GRACE_MIN * 60
            )

            # ── Canonical status (v48): 'published' SOLO si YouTube lo confirma
            # público. Los shorts subidos privados con publishAt futuro se escriben
            # como 'scheduled' y aquí se flipean a 'published' al confirmar; los que
            # fueron escritos optimistamente como 'published' (pre-v48) se degradan
            # a 'scheduled' si YT aún los tiene programados/privados.
            if vis == "public":
                new_status = "published"
                new_actual_published = now.isoformat()  # solo se aplica si NULL (COALESCE abajo)
            elif s.get("status") == "published":
                # BD decía published pero YT aún no: degradar a scheduled.
                new_status = "scheduled"
                new_actual_published = None
            else:
                new_status = s.get("status", "scheduled")
                new_actual_published = None

            # published_at nunca se toca aquí: SIEMPRE es la hora de subida (caps).
            # Solo si un short pre-v48 lo tenía NULL por error, lo rellenamos.
            new_published_at = s.get("published_at")
            if not new_published_at and vis == "public":
                new_published_at = now.isoformat()

            try:
                db._execute_write(
                    """UPDATE shorts SET status=?, yt_visibility=?, yt_checked_at=?,
                       yt_checked_source=?, published_at=COALESCE(?, published_at),
                       actual_published_at=COALESCE(actual_published_at, ?)
                       WHERE id=?""",
                    (new_status, vis, now.isoformat(),
                     'rss' if yt_id in feed else 'ytdlp',
                     new_published_at, new_actual_published, short_id),
                )
                summary["updated"] += 1
            except Exception as exc:
                logger.warning("Reconciler: failed to update short #%d: %s", short_id, exc)
                summary["errors"] += 1

            key = vis if vis in summary else ("errors" if vis == "error" else "unknown")
            summary[key] = summary.get(key, 0) + 1

            if is_stuck:
                summary["stuck"] += 1
                try:
                    from api.services.lifecycle_monitor import create_alert
                    with db._connect() as conn:
                        ch = conn.execute("SELECT slug FROM channels WHERE id=?", (channel_id,)).fetchone()
                    slug = ch["slug"] if ch else f"ch{channel_id}"
                    create_alert(
                        db,
                        entity_type="short",
                        entity_id=short_id,
                        channel_id=channel_id,
                        alert_type=ALERT_TYPE_STUCK,
                        severity="warning",
                        title=f"Short #{short_id} ({slug}): sigue privado tras su publicación programada",
                        message=(
                            f"El short {yt_id} se subió con publishAt {publish_at.isoformat()} "
                            f"pero sigue privado {STUCK_GRACE_MIN} min después de la hora programada. "
                            f"La publicación automática de YouTube no se aplicó. Revisa en Studio si "
                            f"fue retenido por políticas o si el publishAt no se fijó."
                        ),
                        metadata={"short_id": short_id, "yt_id": yt_id, "publish_at": publish_at.isoformat()},
                    )
                except Exception as exc:
                    logger.warning("Reconciler: stuck alert failed for short #%d: %s", short_id, exc)

    return summary


# ── Escaneo de YouTube Studio (on-demand, lee restricciones reales del canal) ──
# yt-dlp solo ve señales públicas (removed / age_restricted / private). Las
# sanciones A NIVEL DE CANAL (strikes, avisos de políticas, estado de
# monetización) solo se ven en YouTube Studio con sesión iniciada. Este helper
# abre Studio con el perfil del account del canal y guarda los hallazgos en
# system_state['studio_scan_<slug>'] para que la barra los muestre.

STUDIO_SCAN_KEY = "studio_scan_{slug}"
STUDIO_ALERT_PATTERNS = [
    "strike", "aviso por", "advertencia", "restricci", "monetizaci", "desmonetiza",
    "suspendida", "suspensión", "violaci", "reclamaci", "derechos de autor",
    "recurrente", "no cumple", "sintético", "sintetico", "engañosa",
    "restricción de edad", "visible para mayores",
]
STUDIO_NAV_NOISE = [
    "términos de uso", "política de privacidad", "políticas y seguridad",
    "política de cookies", "comunidad", "términos y condiciones", "normas",
    "términos del servicio",
]


def scan_studio_for_channel(db, channel_id: int, account: str = "", timeout_s: int = 90) -> dict:
    """Best-effort Studio scan for a channel. Stores results in system_state.

    Returns a summary. If the browser profile is locked/in use it skips
    gracefully (status='in_use') instead of interfering with running uploads.
    """
    result = {"status": "skipped", "reason": "no account"}
    try:
        if not account:
            with db._connect() as conn:
                ch = conn.execute(
                    "SELECT slug, google_account, yt_channel_id FROM channels WHERE id=?",
                    (channel_id,),
                ).fetchone()
            if not ch:
                return {"status": "skipped", "reason": "canal no encontrado"}
            slug = ch["slug"]
            account = ch["google_account"] or ""
            uc = ch["yt_channel_id"] or ""
        else:
            with db._connect() as conn:
                ch = conn.execute(
                    "SELECT slug, yt_channel_id FROM channels WHERE id=?", (channel_id,),
                ).fetchone()
            slug = ch["slug"] if ch else f"ch{channel_id}"
            uc = ch["yt_channel_id"] if ch else ""

        if not account:
            return {"status": "skipped", "reason": "sin google_account"}

        import os
        os.environ.setdefault("DISPLAY", ":99")

        # ── Respect profile lock: don't fight a running browser ──
        from pathlib import Path
        from pipeline.youtube_browser import TOKENS_DIR
        lock_file = Path(TOKENS_DIR) / f"{account}_browser_profile" / "SingletonLock"
        if lock_file.exists():
            return {"status": "in_use", "reason": f"perfil {account} en uso por otro proceso"}

        from pipeline.youtube_browser import get_browser, close_all_browsers
        browser = get_browser(account)
        browser._ensure_browser()
        page = browser._context.new_page()
        page.goto(f"https://studio.youtube.com/channel/{uc}", wait_until="domcontentloaded", timeout=timeout_s * 1000)
        page.wait_for_timeout(10000)
        body = page.evaluate("() => document.body ? document.body.innerText : ''")
        page.screenshot(path=f"/tmp/studio_scan_{slug}.png", full_page=True)
        close_all_browsers()

        lines = [l.strip() for l in body.splitlines() if l.strip() and len(l.strip()) > 3]
        findings = [
            l[:220] for l in lines
            if any(p in l.lower() for p in STUDIO_ALERT_PATTERNS)
            and not any(n in l.lower() for n in STUDIO_NAV_NOISE)
        ]
        # El dashboard muestra el canal seleccionado; confirmamos identidad.
        scanned_channel = slug
        for cand in ("Expediciones sin retorno", "Anomalias Medicas", "Sincronías",
                     "Civilizaciones Olvidadas", "Anomalías Médicas"):
            if cand in body:
                scanned_channel = cand

        result = {
            "status": "ok", "account": account, "channel": scanned_channel,
            "findings": findings, "scanned_at": datetime.now(timezone.utc).isoformat(),
        }
        db.set_system_state(STUDIO_SCAN_KEY.format(slug=slug), json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        logger.warning("Studio scan failed for channel #%s: %s", channel_id, exc)
        result = {"status": "error", "reason": str(exc)[:200]}
    return result
