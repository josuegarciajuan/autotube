"""Optimal Publish Slots Calculator (v12).

Calculates optimal time slots per channel per content type
once per day, using:

  1. YouTube Analytics API — audience country split (ES vs LATAM)
  2. Local DB — historical performance by REAL publish hour
     (``actual_published_at`` / ``published_at``, en la zona del canal)
  3. Niche heuristics — fallback when no real data available

La dimensión horaria de YouTube Analytics no está disponible para informes de
canal (``dimensions=hour`` no es compatible), así que ``api_activity`` queda
desactivado y se refleja explícitamente en ``data_sources``.

Scoring formula:
  score[h] = 0.5 * norm_activity[h] + 0.3 * norm_historical[h] + 0.2 * norm_watchtime[h]
  
3 long-form, 4 short-form non-adjacent peaks (min 3h spacing) are selected.
After calculation, triggers replanning of pending slots for changed channels.
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo

from database.db_extended import ExtendedDatabase
from pipeline.youtube_stats import YouTubeStatsFetcher
from api.services.publish_allocator import (
    Franja,
    assign_franja_slots,
)


def _global_spacing_minutes(db) -> int:
    try:
        from api.services.pacing_profile import get_pacing_value
        return max(0, int(get_pacing_value("global_upload_spacing_min", 45, db) or 0))
    except Exception:
        return 45


logger = logging.getLogger("autotube.optimal_slots")

# ── Scoring weights ──────────────────────────────────────────────
WEIGHTS_LONG = {"activity": 0.5, "historical": 0.3, "watchtime": 0.2}
WEIGHTS_SHORT = {"activity": 0.40, "historical": 0.40, "watchtime": 0.2}

# ── Peak counts per content type (v12: 3 long, 3 shorts) ────────
NUM_PEAKS_LONG = 3
NUM_PEAKS_SHORT = 3

# ── Peak detection ────────────────────────────────────────────────
MIN_PEAK_SPACING_HOURS = 3
EXCLUSION_ZONE = 1.5  # ±1.5h around each selected peak excluded

# ── Replan threshold ──────────────────────────────────────────────
SLOT_CHANGE_THRESHOLD_HOURS = 1  # Trigger replan only if slot shifts >1h

# ── Historical data ───────────────────────────────────────────────
HISTORICAL_LOOKBACK_DAYS = 90
HISTORICAL_RECENCY_WEIGHT_DAYS = 30  # recent data weighted higher

# ── LATAM audience threshold ──────────────────────────────────────
LATAM_SIGNIFICANT_THRESHOLD = 0.15  # 15% minimum LATAM audience to consider

# ── Niche heuristic fallback (from publish_scheduler.py) ──────────
NICHO_PEAK_HOURS: dict[str, dict] = {
    "misterio_paranormal": {
        "primary": 21, "secondary": [0, 14, 17],
        "description": "Contenido de misterio — prime time nocturno"
    },
    "historia_documental": {
        "primary": 20, "secondary": [11, 14, 17],
        "description": "Documentales históricos — tarde/noche"
    },
    "noticias_actualidad": {
        "primary": 12, "secondary": [7, 19, 22],
        "description": "Noticias — mediodía y mañana"
    },
    "educacion_ciencia": {
        "primary": 18, "secondary": [10, 14, 21],
        "description": "Educación/ciencia — media tarde"
    },
    "entretenimiento_general": {
        "primary": 20, "secondary": [12, 15, 22],
        "description": "Entretenimiento — prime time general (fallback)"
    },
}

# Keywords that map to niches (from publish_scheduler.py)
NICHO_KEYWORDS: dict[str, list[str]] = {
    "misterio_paranormal": [
        "misterio", "paranormal", "fantasma", "ovni", "conspiración",
        "milagro", "casualidad", "sincronía", "sincronia", "sincronicidad",
        "inexplicable", "sobrenatural", "esotérico",
    ],
    "historia_documental": [
        "historia", "documental", "civilización", "civilizacion", "antiguo",
        "arqueología", "arqueologia", "imperio", "expedición", "expedicion",
        "biografía", "biografia", "edad media", "guerra mundial",
    ],
    "noticias_actualidad": [
        "noticia", "actualidad", "última hora", "breaking", "tendencia",
        "política", "politica", "economía", "economia",
    ],
    "educacion_ciencia": [
        "ciencia", "educación", "educacion", "medicina", "médico", "medico",
        "enfermedad", "cuerpo humano", "anatomía", "anatomia", "biología",
        "biologia", "física", "fisica", "química", "quimica", "tecnología",
        "tecnologia", "tutorial", "aprender", "anomalía", "anomalia",
    ],
}

# LATAM timezone offsets from UTC (for CET/CEST mapping)
# When we have CET data, LATAM prime time (19-22 local) maps to:
#   Mexico (UTC-6/-5) → 01-04 CET / 02-05 CEST
#   Colombia (UTC-5)  → 00-03 CET / 01-04 CEST
#   Argentina (UTC-3) → 22-01 CET / 23-02 CEST
LATAM_PRIME_HOURS_CEST = [23, 0, 1, 2, 3]  # Rough LATAM evening in CEST


class OptimalSlotsCalculator:
    """Calculates and persists optimal publish slots per channel."""

    def __init__(self, db: ExtendedDatabase | None = None):
        self._db = db or ExtendedDatabase()

    # ── Public API ──────────────────────────────────────────────

    def run_daily_for_all_channels(self) -> dict:
        """Run calculation for all active channels. Returns summary + replanning results."""
        channels = self._db.get_channels(active_only=True)
        result = {
            "channels_processed": 0,
            "slots_calculated": 0,
            "channels_replanned": 0,
            "long_replanned": 0,
            "shorts_replanned": 0,
            "details": {},
        }

        for ch in channels:
            ch_id = ch["id"]
            slug = ch["slug"]
            try:
                ch_result = self.calculate_for_channel(ch_id, slug)
                result["channels_processed"] += 1
                result["slots_calculated"] += ch_result.get("slots_stored", 0)
                result["details"][slug] = ch_result

                # Replan if slots changed
                if ch_result.get("long_changed") or ch_result.get("shorts_changed"):
                    replan = self._replan_channel(ch_id, slug, ch_result)
                    if replan.get("long_replanned"):
                        result["long_replanned"] += replan["long_replanned"]
                    if replan.get("shorts_replanned"):
                        result["shorts_replanned"] += replan["shorts_replanned"]
                    if replan.get("long_replanned") or replan.get("shorts_replanned"):
                        result["channels_replanned"] += 1

            except Exception as exc:
                logger.error("Optimal slots calculation failed for %s: %s", slug, exc)
                result["details"][slug] = {"error": str(exc)}

        # ── v40: este replan no pasa por compute_and_store_horizon, así que
        # resetea el gate de 24h manualmente (persistido en system_state) — el
        # retiming de target_public_at cuenta como replan de horizonte.
        if result.get("channels_replanned", 0) > 0:
            try:
                self._db.set_system_state("last_horizon_replan_ts", str(time.time()))
            except Exception:
                pass

        return result

    def calculate_for_channel(self, channel_id: int, slug: str) -> dict:
        """Calculate optimal slots for a single channel.

        Returns:
            Dict with slots_stored, long_slots, short_slots, long_changed, shorts_changed.
        """
        # Fetch channel config for timezone and keywords
        channel = self._db.get_channel(channel_id)
        if not channel:
            return {"error": "Channel not found", "slots_stored": 0}

        timezone = self._get_channel_timezone(slug, channel)
        keywords = self._get_channel_keywords(slug, channel)

        # Read per-channel overrides from config
        long_num_peaks = self._get_config_override(slug, "OPTIMAL_PEAKS_LONG", NUM_PEAKS_LONG)
        forced_primary_hour = self._get_config_override(slug, "PUBLISH_TARGET_HOUR", None)

        # 1. Fetch data sources
        fetcher = YouTubeStatsFetcher(slug)
        api_ok = fetcher.authenticate()

        # Source A: YT Analytics hourly activity (disabled — 'hour' dimension
        # not supported by YouTube Analytics API v2 for channel reports.
        # Falls back to niche heuristics + historical DB data.)
        hourly_data = {}

        # Source B: Country split (ES vs LATAM)
        country_split = {}
        if api_ok:
            country_split = fetcher.get_audience_country_split(days=90)

        # Source C: DB historical publish-hour performance (hora REAL de
        # publicación, convertida a la zona horaria del canal)
        historical_long = self._get_historical_by_publish_hour(
            channel_id, "long", timezone,
        )
        historical_short = self._get_historical_by_publish_hour(
            channel_id, "short", timezone,
        )

        # Source D: Niche heuristic (fallback seed)
        niche = self._detect_niche(keywords)

        # 2. Compute scores for long-form
        long_scores, long_sources = self._compute_hourly_scores(
            hourly_data, historical_long, niche, WEIGHTS_LONG
        )

        # If PUBLISH_TARGET_HOUR is forced in config, boost that hour to guarantee rank 1
        if forced_primary_hour is not None and 0 <= forced_primary_hour < 24:
            max_score = max(long_scores) if max(long_scores) > 0 else 1.0
            long_scores[forced_primary_hour] = max_score + 0.5
            long_sources["forced_primary"] = True

        # 3. Compute scores for shorts (use per-content-type data if available)
        short_hourly = self._extract_shorts_hourly(hourly_data)
        short_scores, short_sources = self._compute_hourly_scores(
            short_hourly or hourly_data, historical_short, niche, WEIGHTS_SHORT
        )

        # Dejar explícitas las fuentes reales: la dimensión horaria de Analytics
        # no está disponible para informes de canal, así que el histórico se
        # calcula con la hora real de publicación y la heurística es el respaldo.
        for _sources in (long_sources, short_sources):
            _sources["historical_basis"] = "published_at_local"
            _sources["historical_timezone"] = timezone
            _sources["analytics_hour_available"] = False
            _sources["audience_country_available"] = bool(country_split)

        # 4. Find top peaks for each (v12: per-channel long peaks, global short peaks)
        long_slots = self._find_top_peaks(long_scores, timezone, country_split, "long",
                                           num_peaks=long_num_peaks)
        short_slots = self._find_top_peaks(short_scores, timezone, country_split, "short",
                                            num_peaks=NUM_PEAKS_SHORT)

        # 5. Check if slots changed from previous
        old_long = self._db.get_optimal_slots(channel_id, "long")
        old_short = self._db.get_optimal_slots(channel_id, "short")
        long_changed = self._slots_changed(old_long, long_slots)
        shorts_changed = self._slots_changed(old_short, short_slots)

        # 6. Store new slots
        slots_stored = 0
        metrics_snapshot = json.dumps({
            f"h{h}": {
                "score": round(long_scores[h], 4),
                "activity": round(self._get_hourly_value(hourly_data, h, "views"), 1),
                "historical": round(historical_long.get(h, 0), 1),
                "watchtime": round(self._get_hourly_value(hourly_data, h, "watch_minutes"), 1),
            } for h in range(24)
        }, ensure_ascii=False)

        for slot in long_slots:
            self._db.upsert_optimal_slot(
                channel_id=channel_id, content_type="long",
                slot_rank=slot["rank"], target_hour=slot["hour"],
                target_minute=slot.get("minute", 0), timezone=timezone,
                score=slot["score"], confidence=slot.get("confidence", 0.3),
                audience_focus=slot.get("audience_focus", "blend"),
                metrics_snapshot=metrics_snapshot,
                data_sources=json.dumps(long_sources),
                audience_split=json.dumps(country_split),
            )
            slots_stored += 1

        for slot in short_slots:
            self._db.upsert_optimal_slot(
                channel_id=channel_id, content_type="short",
                slot_rank=slot["rank"], target_hour=slot["hour"],
                target_minute=slot.get("minute", 0), timezone=timezone,
                score=slot["score"], confidence=slot.get("confidence", 0.3),
                audience_focus=slot.get("audience_focus", "blend"),
                metrics_snapshot=json.dumps({
                    f"h{h}": {"score": round(short_scores[h], 4)}
                    for h in range(24)
                }, ensure_ascii=False),
                data_sources=json.dumps(short_sources),
                audience_split=json.dumps(country_split),
            )
            slots_stored += 1

        # Clean up orphaned long-form slots from previous configs with higher num_peaks
        # (e.g. when reducing from 3 to 2 peaks per channel)
        if long_num_peaks > 0:
            with self._db._connect() as conn:
                conn.execute(
                    """DELETE FROM optimal_publish_slots
                       WHERE channel_id = ? AND content_type = ?
                         AND slot_rank > ?""",
                    (channel_id, "long", long_num_peaks),
                )
                conn.commit()

        return {
            "slots_stored": slots_stored,
            "long_slots": long_slots,
            "short_slots": short_slots,
            "long_changed": long_changed,
            "shorts_changed": shorts_changed,
            "timezone": timezone,
            "niche": niche,
            "sources_long": long_sources,
            "sources_short": short_sources,
            "country_split": country_split,
        }

    # ── Internal: data fetching ─────────────────────────────────

    def _get_channel_timezone(self, slug: str, channel: dict) -> str:
        """Extract timezone from channel config."""
        try:
            from config.config_bridge import get_channel_config
            config = get_channel_config(slug)
            tz = getattr(config, "PUBLISH_TIMEZONE", None)
            if tz:
                return tz
        except Exception:
            pass
        # Default: Spain timezone (CET/CEST)
        return "Europe/Madrid"

    def _get_config_override(self, slug: str, attr: str, default: any = None) -> any:
        """Read an optional integer override from channel config.

        Returns the config attribute value if set and non-None, otherwise default.
        """
        try:
            from config.config_bridge import get_channel_config
            config = get_channel_config(slug)
            value = getattr(config, attr, None)
            if value is not None:
                return value
        except Exception:
            pass
        return default

    def _get_channel_keywords(self, slug: str, channel: dict) -> list[str]:
        """Extract SEO keywords from channel config."""
        try:
            from config.config_bridge import get_channel_config
            config = get_channel_config(slug)
            primary = getattr(config, "SEO_PRIMARY_KEYWORD", None)
            secondary = getattr(config, "SEO_SECONDARY_KEYWORDS", None)
            keywords = []
            if primary:
                keywords.append(str(primary).lower())
            if secondary:
                if isinstance(secondary, list):
                    keywords.extend([str(k).lower() for k in secondary])
                else:
                    keywords.append(str(secondary).lower())
            if keywords:
                return keywords
        except Exception:
            pass
        return []

    def _detect_niche(self, keywords: list[str]) -> dict | None:
        """Match keywords to niche heuristic. Returns niche dict or None."""
        if not keywords:
            return None

        best_niche = None
        best_score = 0
        for niche_name, niche_kws in NICHO_KEYWORDS.items():
            score = 0
            for kw in keywords:
                for nkw in niche_kws:
                    if nkw.lower() in kw.lower() or kw.lower() in nkw.lower():
                        score += 1
                        break
            if score > best_score:
                best_score = score
                best_niche = niche_name

        if best_niche and best_score > 0:
            return NICHO_PEAK_HOURS.get(best_niche)
        # Fallback to general entertainment
        return NICHO_PEAK_HOURS.get("entretenimiento_general")

    def _get_historical_by_publish_hour(
        self,
        channel_id: int,
        content_type: str,
        timezone: str = "Europe/Madrid",
    ) -> dict[int, float]:
        """Get average views by REAL publish hour (canal timezone).

        Agrupa por ``actual_published_at`` → ``published_at`` (longs) o
        ``actual_published_at`` → ``publish_at`` → ``published_at`` (shorts), no
        por ``created_at``: la hora de generación no dice nada del rendimiento
        de publicación. Las timestamps naive se asumen UTC (convención de la
        DB); después se convierten a la zona horaria del canal.

        Returns dict {hour: avg_views}.
        """
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(timezone or "Europe/Madrid")
        except Exception:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("UTC")

        lookback = f"-{HISTORICAL_LOOKBACK_DAYS} days"
        try:
            with self._db._connect() as conn:
                if content_type == "long":
                    # actual_published_at (verdad externa) manda; published_at
                    # es la hora de publicación registrada por el uploader.
                    rows = conn.execute(
                        """SELECT COALESCE(v.actual_published_at, v.published_at) AS pub_at,
                                  vsh.views AS views
                           FROM videos v
                           JOIN video_stats_history vsh ON vsh.video_id = v.id
                           WHERE v.channel_id = ?
                             AND v.yt_video_id IS NOT NULL
                             AND v.yt_video_id != ''
                             AND COALESCE(v.actual_published_at, v.published_at) IS NOT NULL
                             AND datetime(COALESCE(v.actual_published_at, v.published_at))
                                 >= datetime('now', ?)
                             AND vsh.views > 0
                           ORDER BY vsh.fetched_at DESC""",
                        (channel_id, lookback),
                    ).fetchall()
                else:
                    # published_at de un short es la hora de SUBIDA; publish_at
                    # es el publishAt programado y actual_published_at la
                    # confirmación de que ya es público.
                    rows = conn.execute(
                        """SELECT COALESCE(s.actual_published_at, s.publish_at,
                                           s.published_at) AS pub_at,
                                  ss.views AS views
                           FROM shorts s
                           JOIN short_stats ss ON ss.short_id = s.id
                           WHERE s.channel_id = ?
                             AND s.youtube_id IS NOT NULL
                             AND s.youtube_id != ''
                             AND COALESCE(s.actual_published_at, s.publish_at,
                                          s.published_at) IS NOT NULL
                             AND datetime(COALESCE(s.actual_published_at, s.publish_at,
                                                   s.published_at))
                                 >= datetime('now', ?)
                             AND ss.views > 0
                           ORDER BY ss.fetched_at DESC""",
                        (channel_id, lookback),
                    ).fetchall()
        except Exception as exc:
            logger.debug("Historical publish-hour query failed for ch%d: %s", channel_id, exc)
            return {}

        if not rows:
            return {}

        hourly_views: dict[int, list[dict]] = {}
        now_utc = datetime.now(tz=dt_timezone.utc)

        for row in rows:
            pub_raw = row["pub_at"]
            views = row["views"]
            if not pub_raw or views is None:
                continue
            try:
                if isinstance(pub_raw, str):
                    published = datetime.fromisoformat(
                        pub_raw.replace("Z", "+00:00").replace(" ", "T")
                    )
                else:
                    published = pub_raw
                if published.tzinfo is None:
                    published = published.replace(tzinfo=dt_timezone.utc)
                published_local = published.astimezone(tz)
                publish_hour = published_local.hour
                # Recency weight: 1.0 for last 30 days, decaying to 0.3 for 90 days
                days_ago = max(0, (now_utc - published).days)
                weight = max(0.3, 1.0 - 0.7 * (days_ago / HISTORICAL_LOOKBACK_DAYS))
                if publish_hour not in hourly_views:
                    hourly_views[publish_hour] = []
                hourly_views[publish_hour].append({"views": views, "weight": weight})
            except Exception:
                continue

        # Compute weighted average per hour
        result: dict[int, float] = {}
        for h, entries in hourly_views.items():
            total_weighted = sum(e["views"] * e["weight"] for e in entries)
            total_weight = sum(e["weight"] for e in entries)
            result[h] = total_weighted / max(total_weight, 1)

        return result

    def _extract_shorts_hourly(self, hourly_data: dict) -> dict | None:
        """Extract shorts-specific hourly data if creatorContentType was available."""
        by_type = hourly_data.get("by_content_type")
        if not by_type:
            return None

        shorts_data = by_type.get("SHORTS", {})
        if not shorts_data:
            return None

        # Convert to same format as aggregated hourly
        hourly_list = []
        for h in range(24):
            sd = shorts_data.get(h, {})
            hourly_list.append({
                "hour": h,
                "views": sd.get("views", 0),
                "watch_minutes": sd.get("watch_minutes", 0.0),
            })
        return {"hourly": hourly_list, "sources": ["api_hour_creatorContentType"]}

    # ── Internal: scoring ───────────────────────────────────────

    def _compute_hourly_scores(self, hourly_data: dict, historical: dict[int, float],
                                niche: dict | None, weights: dict) -> tuple[list[float], dict]:
        """Compute 24-hour score vector from available data sources.

        Returns (scores[24], sources dict).
        """
        scores = [0.0] * 24
        sources: dict[str, bool] = {"api_activity": False, "db_historical": False,
                                      "niche_fallback": False}

        # 1. Normalize YT Analytics activity
        hourly_raw = hourly_data.get("hourly", []) if hourly_data else []
        activity_values = []
        for h in range(24):
            if h < len(hourly_raw):
                activity_values.append(float(hourly_raw[h].get("views", 0)))
            else:
                activity_values.append(0.0)

        max_activity = max(activity_values) if max(activity_values) > 0 else 1.0
        has_activity = max_activity > 1.0 and len(hourly_raw) > 0

        # 2. Normalize historical data
        hist_values = [historical.get(h, 0.0) for h in range(24)]
        max_hist = max(hist_values) if max(hist_values) > 0 else 1.0
        has_historical = max_hist > 1.0 and sum(hist_values) > 0

        # 3. Normalize watch time
        watch_values = []
        for h in range(24):
            if h < len(hourly_raw):
                watch_values.append(float(hourly_raw[h].get("watch_minutes", 0)))
            else:
                watch_values.append(0.0)
        max_watch = max(watch_values) if max(watch_values) > 0 else 1.0
        has_watchtime = max_watch > 1.0

        # 4. Compute weighted score
        if has_activity or has_historical or has_watchtime:
            # Has real data — use weighted formula
            for h in range(24):
                scores[h] = (
                    weights["activity"] * (activity_values[h] / max_activity) +
                    weights["historical"] * (hist_values[h] / max_hist) +
                    weights["watchtime"] * (watch_values[h] / max_watch)
                )
            sources["api_activity"] = has_activity
            sources["db_historical"] = has_historical
        elif niche:
            # No real data — seed from niche heuristic
            sources["niche_fallback"] = True
            primary = niche.get("primary", 20)
            secondary = niche.get("secondary", [12, 15, 22])
            for h in range(24):
                if h == primary:
                    scores[h] = 1.0
                elif h in secondary:
                    scores[h] = 0.5
                else:
                    # Gaussian spread around primary
                    dist = min(abs(h - primary), 24 - abs(h - primary))
                    scores[h] = max(0.0, 1.0 - dist * 0.1)
                    # Boost around secondary peaks
                    for sec in secondary:
                        sec_dist = min(abs(h - sec), 24 - abs(h - sec))
                        scores[h] = max(scores[h], 0.5 - sec_dist * 0.1)
        else:
            # Absolute fallback — generic prime time 20:00
            sources["niche_fallback"] = True
            for h in range(24):
                dist = min(abs(h - 20), 24 - abs(h - 20))
                scores[h] = max(0.0, 1.0 - dist * 0.12)

        return scores, sources

    def _find_top_peaks(self, scores: list[float], timezone: str,
                         country_split: dict, content_type: str,
                         num_peaks: int = 3) -> list[dict]:
        """Find top N non-adjacent peaks from 24-hour score vector.

        Excludes ±1.5h zone around each selected peak. If LATAM audience is significant
        (>15%), biases towards finding at least one LATAM-friendly hour.

        Args:
            num_peaks: how many peaks to select (3 for long, 4 for shorts in v12)

        Returns list of N dicts with {rank, hour, minute, score, confidence, audience_focus}.
        """
        latam_pct = country_split.get("latam_pct", 0) if country_split else 0
        has_latam = latam_pct >= LATAM_SIGNIFICANT_THRESHOLD

        # Copy scores and apply LATAM bias if applicable
        adjusted = list(scores)
        if has_latam:
            for h in LATAM_PRIME_HOURS_CEST:
                adjusted[h] *= 1.15  # 15% boost for LATAM prime hours

        slots: list[dict] = []
        available = list(range(24))
        excluded: set[int] = set()

        for rank in range(1, num_peaks + 1):
            # Find best available hour
            if not available:
                # Fallback: pick hour farthest from existing slots
                if slots:
                    available = [h for h in range(24) if h not in excluded]
                if not available:
                    available = [h for h in range(24)]
            best_h = max(available, key=lambda h: adjusted[h])
            best_score = adjusted[best_h]

            if best_score <= 0.001 and slots:
                # Very flat — use remaining niche hours if no clear peak
                best_h = min(available, key=lambda h: abs(h - (slots[0]["hour"] + 8) % 24))
                best_score = adjusted[best_h]

            # Determine audience focus
            focus = "blend"
            if has_latam and best_h in LATAM_PRIME_HOURS_CEST:
                focus = "latam"
            elif has_latam and best_h in [19, 20, 21, 22]:
                focus = "spain"

            # Confidence based on data sources used
            max_score = max(scores) if max(scores) > 0 else 1.0
            confidence = min(1.0, best_score / max_score)

            slots.append({
                "rank": rank,
                "hour": best_h,
                "minute": random.choice([7, 13, 22, 37, 43, 52]),  # human-like minute
                "score": round(best_score, 4),
                "confidence": round(confidence, 2),
                "audience_focus": focus,
            })

            # Mark exclusion zone
            for offset in range(-int(EXCLUSION_ZONE * 2), int(EXCLUSION_ZONE * 2) + 1):
                excluded.add((best_h + offset) % 24)

            available = [h for h in range(24) if h not in excluded]

        # Fill any missing slots with best remaining
        while len(slots) < num_peaks:
            if available:
                h = available[0]
            else:
                h = (slots[0]["hour"] + 8 + len(slots) * 4) % 24 if slots else 20
            slots.append({
                "rank": len(slots) + 1,
                "hour": h,
                "minute": random.choice([7, 13, 22, 37, 43, 52]),
                "score": round(adjusted[h], 4),
                "confidence": 0.1,
                "audience_focus": "blend",
            })
            excluded.add(h)
            available = [x for x in available if x not in excluded]

        return sorted(slots, key=lambda s: s["rank"])

    # ── Internal: helpers ───────────────────────────────────────

    def _get_hourly_value(self, hourly_data: dict, hour: int, key: str) -> float:
        """Extract a value from hourly_data for a specific hour."""
        hourly = hourly_data.get("hourly", []) if hourly_data else []
        if hour < len(hourly):
            return float(hourly[hour].get(key, 0))
        return 0.0

    def _slots_changed(self, old_slots: list[dict], new_slots: list[dict]) -> bool:
        """Check if slots changed significantly (>threshold hours)."""
        if not old_slots and new_slots:
            return True
        if len(old_slots) != len(new_slots):
            return True
        old_hours = sorted([s.get("target_hour", s.get("hour", 0)) for s in old_slots])
        new_hours = sorted([s["hour"] for s in new_slots])
        for oh, nh in zip(old_hours, new_hours):
            diff = min(abs(oh - nh), 24 - abs(oh - nh))
            if diff > SLOT_CHANGE_THRESHOLD_HOURS:
                return True
        return False

    # ── Replanning ──────────────────────────────────────────────

    def _replan_channel(self, channel_id: int, slug: str, ch_result: dict) -> dict:
        """Replan pending slots for a channel whose optimal slots changed.

        Long-form: updates target_public_at for pending planned_slots.
        Shorts: regenerates shorts_planned_slots for remaining horizon days.
        """
        result = {"long_replanned": 0, "shorts_replanned": 0}

        try:
            if ch_result.get("long_changed"):
                result["long_replanned"] = self._replan_long_form(channel_id)
        except Exception as exc:
            logger.error("Long-form replan failed for %s: %s", slug, exc)

        try:
            if ch_result.get("shorts_changed"):
                result["shorts_replanned"] = self._replan_shorts(channel_id)
        except Exception as exc:
            logger.error("Shorts replan failed for %s: %s", slug, exc)

        return result

    def _replan_long_form(self, channel_id: int) -> int:
        """Reparte los slots long-form pendientes con el asignador común.

        Aplica el ranking de franjas, el cap diario y el gap del canal, y mueve
        al día siguiente lo que no cabe (antes se recortaba a 23:xx o se
        desplazaba +1 min). No toca slots ``running``/``completed``.

        Escribe ``target_public_at`` en ISO-8601 UTC, que es el formato que usa
        ``planned_slots`` en producción.
        """
        long_slots = self._db.get_optimal_slots(channel_id, "long")
        if not long_slots:
            logger.debug("No long slots for replan ch%d", channel_id)
            return 0

        franjas = [
            Franja(
                int(s.get("target_hour", 0) or 0),
                int(s.get("target_minute", 0) or 0),
                int(s.get("slot_rank", i + 1) or (i + 1)),
            )
            for i, s in enumerate(long_slots)
        ]

        tz_name = long_slots[0].get("timezone") or "Europe/Madrid"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Europe/Madrid")

        # Política efectiva del canal (cap + gap). Si no se puede resolver,
        # se usan los valores conservadores de seguridad.
        try:
            from api.services.channel_policy import resolve_channel_policy_values
            policy = resolve_channel_policy_values(channel_id, db=self._db)
            daily_cap = int(policy.get("longform_publish_cap", 1) or 1)
            min_gap = int(policy.get("same_channel_publish_gap_h", 24) or 24) * 60
        except Exception:
            daily_cap = 1
            min_gap = 24 * 60
        if daily_cap <= 0:
            logger.info("Long-form replan ch%d: cap=0 — no se reprograma", channel_id)
            return 0

        # Cap variable por día (rotación): un escalar aplicaría el bonus de hoy
        # a todo el backlog y podría recortar/duplicar días futuros.
        _cap_cache: dict = {}

        def _cap_for_day(_day) -> int:
            key = _day.isoformat() if hasattr(_day, "isoformat") else str(_day)[:10]
            if key not in _cap_cache:
                try:
                    from api.services.channel_policy import policy_value
                    _cap_cache[key] = max(0, int(policy_value(
                        channel_id, "longform_publish_cap", db=self._db,
                        default=daily_cap, day=key,
                    ) or 0))
                except Exception:
                    _cap_cache[key] = daily_cap
            return int(_cap_cache[key])

        try:
            with self._db._connect() as conn:
                pending = conn.execute(
                    """SELECT * FROM planned_slots
                       WHERE channel_id = ? AND status = 'pending'
                       ORDER BY date_key ASC, scheduled_at ASC""",
                    (channel_id,),
                ).fetchall()
                # Targets ya ocupados por generaciones en curso o vídeos.
                occupied: set[str] = set()
                try:
                    for row in conn.execute(
                        """SELECT target_public_at FROM planned_slots
                           WHERE channel_id = ? AND status = 'running'
                             AND target_public_at IS NOT NULL""",
                        (channel_id,),
                    ).fetchall():
                        if row["target_public_at"]:
                            occupied.add(str(row["target_public_at"]))
                except Exception:
                    pass
                try:
                    for row in conn.execute(
                        """SELECT target_public_at FROM videos
                           WHERE channel_id = ? AND target_public_at IS NOT NULL
                             AND status NOT IN ('error', 'cancelled')""",
                        (channel_id,),
                    ).fetchall():
                        if row["target_public_at"]:
                            occupied.add(str(row["target_public_at"]))
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("Pending slots query failed for ch%d: %s", channel_id, exc)
            return 0

        if not pending:
            return 0

        today = datetime.now(tz).date()
        pending_days = []
        for row in pending:
            try:
                pending_days.append(date.fromisoformat(str(row["date_key"])[:10]))
            except Exception:
                continue
        start_day = max(today, min(pending_days)) if pending_days else today
        earliest = (
            datetime.now(tz)
            if start_day == today
            else datetime.combine(start_day, datetime.min.time(), tzinfo=tz)
        )

        # Repartir TODO el backlog pendiente con la regla franja-first:
        # tantos elementos por día como permita el cap; si hay más que
        # franjas, comparten franja con offset. Sin carry por gap.
        busy_dt: list[datetime] = []
        day_usage: dict = {}
        for raw in occupied:
            try:
                value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if value.tzinfo is None:
                    value = value.replace(tzinfo=dt_timezone.utc)
                local = value.astimezone(tz)
            except Exception:
                continue
            busy_dt.append(local)
            day_usage[local.date()] = day_usage.get(local.date(), 0) + 1

        day_counts: list[tuple[date, int]] = []
        remaining = len(pending)
        cursor_day = start_day
        guard = 0
        while remaining > 0 and guard < max(60, len(pending) + 30):
            cap = max(0, _cap_for_day(cursor_day))
            if cap <= 0:
                cursor_day += timedelta(days=1)
                guard += 1
                continue
            take = min(remaining, cap)
            day_counts.append((cursor_day, take))
            remaining -= take
            cursor_day += timedelta(days=1)
            guard += 1

        allocated = assign_franja_slots(
            franjas,
            day_counts,
            tz=tz,
            earliest=earliest,
            daily_cap=_cap_for_day,
            busy=busy_dt,
            day_usage=day_usage,
            soft_busy=True,
            soft_gap=True,
            busy_spacing_minutes=_global_spacing_minutes(self._db),
        )

        # Calcular todos los destinos ANTES de tocar la tabla. Actualizar en
        # caliente chocaba con el target antiguo de otra fila `pending` aún no
        # procesada (UNIQUE index uq_active_planned_public_target) y abortaba
        # el replan completo (bug oct 2026, canal5).
        assignments: list[tuple[int, str, datetime, int]] = []
        for row, slot in zip(pending, allocated):
            iso = slot.when.astimezone(dt_timezone.utc).isoformat()
            assignments.append((
                int(row["id"]), iso, slot.when, int(slot.duplicate_index),
            ))

        updated = 0
        with self._db._connect() as conn:
            # Fase 1: liberar los targets de TODAS las filas pending con valores
            # únicos temporales. Así ninguna actualización intermedia colisiona
            # con el target antiguo de otra fila que todavía no se ha movido.
            for row in pending:
                placeholder = (
                    datetime(1900, 1, 1, tzinfo=dt_timezone.utc)
                    + timedelta(minutes=int(row["id"]))
                ).isoformat()
                conn.execute(
                    """UPDATE planned_slots
                       SET target_public_at = ?, franja_dup_index = 0
                       WHERE id = ? AND status = 'pending'""",
                    (placeholder, row["id"]),
                )

            # Fase 2: escribir los targets definitivos.
            for slot_id, iso, _target_dt, dup_index in assignments:
                conn.execute(
                    """UPDATE planned_slots
                       SET target_public_at = ?, franja_dup_index = ?
                       WHERE id = ? AND status = 'pending'""",
                    (iso, dup_index, slot_id),
                )
                updated += 1

            # Si el asignador no cubrió alguna fila (caso excepcional), se
            # restaura su target original para no dejarla sin programar.
            assigned_ids = {slot_id for slot_id, _iso, _dt, _dup in assignments}
            for row in pending:
                if int(row["id"]) not in assigned_ids and row["target_public_at"]:
                    conn.execute(
                        """UPDATE planned_slots SET target_public_at = ?
                           WHERE id = ? AND status = 'pending'""",
                        (row["target_public_at"], row["id"]),
                    )
            conn.commit()

        logger.info("Long-form replan ch%d: %d pending slots updated", channel_id, updated)
        return updated

    def _replan_shorts(self, channel_id: int) -> int:
        """Regenerate shorts_planned_slots for the next 7 days using new optimal slots.

        Deletes pending slots for each future day and recomputes them.
        Running/completed slots are NOT touched.
        """
        try:
            from api.services.shorts_scheduler import (
                compute_daily_shorts_slots, persist_daily_shorts_slots,
            )
        except ImportError as exc:
            logger.warning("Cannot import shorts_scheduler for replan: %s", exc)
            return 0

        from datetime import date as _date
        today = _date.today()

        total_replanned = 0
        for offset in range(7):
            day = today + timedelta(days=offset)
            date_key = day.isoformat()

            try:
                # Delete only pending slots for this channel+date
                with self._db._connect() as conn:
                    conn.execute(
                        """DELETE FROM shorts_planned_slots
                           WHERE channel_id = ? AND date_key = ?
                             AND status = 'pending'""",
                        (channel_id, date_key),
                    )
                    conn.commit()
            except Exception as exc:
                logger.debug("Delete pending shorts failed for ch%d %s: %s",
                            channel_id, date_key, exc)
                continue

            # Recompute slots for this day
            try:
                new_slots = compute_daily_shorts_slots(date_key, self._db)
                if new_slots:
                    persist_daily_shorts_slots(date_key, new_slots, self._db)
                    # Count only this channel's new slots
                    ch_slots = [s for s in new_slots if s.get("channel_id") == channel_id]
                    total_replanned += len(ch_slots)
            except Exception as exc:
                logger.debug("Recompute shorts failed for ch%d %s: %s",
                            channel_id, date_key, exc)

        logger.info("Shorts replan ch%d: %d slots regenerated across 7-day horizon",
                     channel_id, total_replanned)
        return total_replanned


# ── Convenience function for scheduler ────────────────────────────

def calculate_and_replan_all(db: ExtendedDatabase | None = None) -> dict:
    """Run optimal slots calculation + replanning for all active channels.

    Called once per day by the background scheduler loop.
    """
    calc = OptimalSlotsCalculator(db)
    return calc.run_daily_for_all_channels()
