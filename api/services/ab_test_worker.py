"""AB Test Worker — Sequential title/thumbnail optimization.

Protocol:
  Día 0: Upload with title_v1 + 1 thumbnail (variant 1)
  Día 2 (+48h): If CTR < threshold, rotate ONLY title or thumbnail (never both)
  Día 4 (+48h more): Compare CTR_v1 vs CTR_v2, keep winner

Quota-aware: prioritizes local DB reads (video_stats_history), falls back to
YouTube Analytics API only when data is stale (>24h old or absent).

Invariants:
  - NUNCA cambiar título y thumbnail en la misma ventana de 48h
  - Solo 1 video en test por canal a la vez
  - Shorts → skip (no aplica A/B)
  - <100 impresiones tras 7 días → completed por datos insuficientes
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ABTestWorker:
    """Lightweight worker that processes A/B tests on a schedule.

    Called every hour from the main scheduler loop. Each `run_cycle()`
    scans for videos needing first or second checks and processes them
    one channel at a time (at most 1 active test per channel).
    """

    def __init__(self, db):
        """Initialize the A/B test worker.

        Args:
            db: ExtendedDatabase instance (shared with scheduler).
        """
        self.db = db

        # ── Thresholds (from settings, with fallbacks) ──────────
        from config.settings import (
            AB_TEST_CTR_THRESHOLD,
            AB_TEST_FIRST_CHECK_HOURS,
            AB_TEST_SECOND_CHECK_HOURS,
            AB_TEST_MIN_IMPRESSIONS,
            AB_TEST_MAX_STALE_DAYS,
            AB_TEST_MIN_IMPRESSIONS_POST_CHANGE,
        )
        self.ctr_threshold = AB_TEST_CTR_THRESHOLD          # % CTR mínimo
        self.first_check_hours = AB_TEST_FIRST_CHECK_HOURS   # horas hasta 1ª revisión
        self.second_check_hours = AB_TEST_SECOND_CHECK_HOURS  # horas hasta 2ª revisión
        self.min_impressions = AB_TEST_MIN_IMPRESSIONS        # mínimo para decidir
        self.max_stale_days = AB_TEST_MAX_STALE_DAYS          # días máximos esperando datos
        self.min_impressions_post = AB_TEST_MIN_IMPRESSIONS_POST_CHANGE  # mínimo post-cambio

    # ═══════════════════════════════════════════════════════════════
    # Main cycle
    # ═══════════════════════════════════════════════════════════════

    def run_cycle(self):
        """Called every hour by the scheduler loop.

        1. Scan for videos in 'pending' phase that are ready for first check.
        2. Scan for videos in 'title_rotated' or 'thumbnail_rotated' phase
           that are ready for second check.
        3. Process each, one channel at a time.
        """
        now = datetime.now(timezone.utc)
        
        # ── First checks: pending videos older than first_check_hours ──
        first_candidates = self._find_videos_for_first_check(now)
        for row in first_candidates:
            try:
                self._process_first_check(row, now)
            except Exception as exc:
                logger.error("First check failed for video %s: %s", row.get("video_id"), exc)
        
        # ── Second checks: rotated videos older than second_check_hours ──
        second_candidates = self._find_videos_for_second_check(now)
        for row in second_candidates:
            try:
                self._process_second_check(row, now)
            except Exception as exc:
                logger.error("Second check failed for video %s: %s", row.get("video_id"), exc)
        
        # ── Expired: pending videos beyond max_stale_days with no data ──
        self._handle_expired(now)

    # ═══════════════════════════════════════════════════════════════
    # Database queries
    # ═══════════════════════════════════════════════════════════════

    def _find_videos_for_first_check(self, now: datetime) -> list[dict]:
        """Find videos in 'pending' phase that are old enough for first check.

        Also enforces: only 1 video per channel in test at a time.
        """
        conn = self.db._get_conn() if hasattr(self.db, '_get_conn') else None
        if conn is None:
            # Fallback: use raw sqlite3
            import sqlite3
            from config.settings import DATABASE_PATH
            conn = sqlite3.connect(str(DATABASE_PATH))
            conn.row_factory = sqlite3.Row

        cutoff = (now - timedelta(hours=self.first_check_hours)).strftime("%Y-%m-%d %H:%M:%S")

        # Find all pending videos ready for first check
        rows = conn.execute("""
            SELECT vab.*, v.*, ch.slug as channel_slug
            FROM video_ab_tests vab
            JOIN videos v ON vab.video_id = v.id
            JOIN channels ch ON vab.channel_id = ch.id
            WHERE vab.phase = 'pending'
              AND v.created_at <= ?
              AND v.privacy_status NOT LIKE '%short%'
              AND v.yt_video_id IS NOT NULL
              AND v.yt_video_id != ''
            ORDER BY v.created_at ASC
        """, (cutoff,)).fetchall()

        # ── Gate: only 1 active test per channel ───────────────
        active_channels = conn.execute("""
            SELECT DISTINCT channel_id
            FROM video_ab_tests
            WHERE phase IN ('first_check', 'title_rotated', 'thumbnail_rotated', 'second_check')
        """).fetchall()
        active_ids = {r["channel_id"] for r in active_channels}

        candidates = []
        for row in rows:
            row_dict = dict(row)
            if row_dict["channel_id"] not in active_ids:
                candidates.append(row_dict)
                active_ids.add(row_dict["channel_id"])  # Reserve this channel
            else:
                logger.debug(
                    "Skipping video %s: channel %s already has active test",
                    row_dict["video_id"], row_dict["channel_id"],
                )

        return candidates

    def _find_videos_for_second_check(self, now: datetime) -> list[dict]:
        """Find videos in 'title_rotated' or 'thumbnail_rotated' phase
        that are old enough for second check.
        """
        conn = self._get_db_conn()
        cutoff = (now - timedelta(hours=self.second_check_hours)).strftime("%Y-%m-%d %H:%M:%S")

        rows = conn.execute("""
            SELECT vab.*, v.*, ch.slug as channel_slug
            FROM video_ab_tests vab
            JOIN videos v ON vab.video_id = v.id
            JOIN channels ch ON vab.channel_id = ch.id
            WHERE vab.phase IN ('title_rotated', 'thumbnail_rotated')
              AND (
                (vab.phase = 'title_rotated' AND vab.title_rotated_at <= ?)
                OR
                (vab.phase = 'thumbnail_rotated' AND vab.thumbnail_rotated_at <= ?)
              )
        """, (cutoff, cutoff)).fetchall()

        return [dict(r) for r in rows]

    def _handle_expired(self, now: datetime):
        """Mark as completed videos that have been pending for > max_stale_days
        with insufficient impressions.
        """
        conn = self._get_db_conn()
        stale_cutoff = (now - timedelta(days=self.max_stale_days)).strftime("%Y-%m-%d %H:%M:%S")

        conn.execute("""
            UPDATE video_ab_tests
            SET phase = 'insufficient_data',
                completed_at = datetime('now'),
                updated_at = datetime('now')
            WHERE phase = 'pending'
              AND created_at <= ?
        """, (stale_cutoff,))
        conn.commit()

    # ═══════════════════════════════════════════════════════════════
    # Phase processing
    # ═══════════════════════════════════════════════════════════════

    def _process_first_check(self, row: dict, now: datetime):
        """Evaluate a video's CTR after the first check window.

        Logic:
          1. Fetch CTR + impressions (local DB first, API fallback).
          2. If impressions < min_impressions → stay pending (retry later).
          3. If CTR >= threshold → skipped (already good).
          4. If CTR < threshold:
             - If thumbnail_active=1 → rotate to variant 2 → phase='thumbnail_rotated'
             - Else → generate title_v2 → change on YT → phase='title_rotated'
        """
        video_id = row["video_id"]
        yt_video_id = row["yt_video_id"]
        channel_slug = row.get("channel_slug", "")
        channel_id = row["channel_id"]

        logger.info(
            "[AB] First check: video %s (yt=%s) channel=%s",
            video_id, yt_video_id, channel_slug,
        )

        # ── Fetch CTR data ────────────────────────────────────
        ctr_data = self._fetch_ctr(row)
        ctr = ctr_data.get("ctr", 0.0)
        impressions = ctr_data.get("impressions", 0)
        retention = ctr_data.get("avg_duration", 0.0)

        logger.info(
            "[AB] CTR data: %.2f%% | %d impressions | retention %.1fs",
            ctr, impressions, retention,
        )

        # ── Insufficient data ──────────────────────────────────
        if impressions < self.min_impressions:
            video_age_hours = self._video_age_hours(row, now)
            if video_age_hours > self.max_stale_days * 24:
                # Too old, give up
                self._update_phase(video_id, "insufficient_data", now)
                logger.info("[AB] Video %s: insufficient data after %dh — marking complete", video_id, video_age_hours)
            else:
                # Stay pending for now
                logger.info("[AB] Video %s: only %d impressions (< %d) — waiting", video_id, impressions, self.min_impressions)
            return

        # ── CTR already good ──────────────────────────────────
        if ctr >= self.ctr_threshold:
            self._update_phase(video_id, "skipped", now)
            self._record_learning(row, ctr, None, "skipped")
            logger.info("[AB] Video %s: CTR %.2f%% >= %.2f%% — skipping optimization", video_id, ctr, self.ctr_threshold)
            return

        # ── CTR low — decide what to change ───────────────────
        active_variant = row.get("thumbnail_variant_active", 1)
        variant_paths_json = row.get("thumbnail_variant_paths", "[]")
        try:
            variant_paths = json.loads(variant_paths_json) if variant_paths_json else []
        except (json.JSONDecodeError, TypeError):
            variant_paths = []

        conn = self._get_db_conn()

        # ── Record baseline metrics ────────────────────────────
        conn.execute("""
            UPDATE video_ab_tests
            SET ctr_v1 = ?, impressions_v1 = ?, retention_v1 = ?,
                first_checked_at = datetime('now'),
                updated_at = datetime('now')
            WHERE video_id = ?
        """, (ctr, impressions, retention, video_id))
        conn.commit()

        # ── Decision: thumbnail first, title second ───────────
        if active_variant == 1 and len(variant_paths) >= 2:
            # Rotate to thumbnail variant 2
            new_variant = 2
            self._swap_thumbnail_on_youtube(yt_video_id, channel_slug, variant_paths, new_variant, video_id, channel_id)

            conn.execute("""
                UPDATE video_ab_tests
                SET phase = 'thumbnail_rotated',
                    thumbnail_variant_active = ?,
                    thumbnail_rotated_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE video_id = ?
            """, (new_variant, video_id))
            conn.commit()
            logger.info(
                "[AB] Video %s: CTR low (%.2f%%) — rotated thumbnail to variant %d",
                video_id, ctr, new_variant,
            )
        else:
            # Rotate title
            title_v1 = row.get("title_v1", "")
            self._rotate_title(row, title_v1, ctr)

    def _process_second_check(self, row: dict, now: datetime):
        """Compare CTR before and after the change to determine the winner.

        Logic:
          - Fetch current CTR/impressions (post-change).
          - If post-change impressions < min_impressions_post → wait.
          - If ctr_v2 > ctr_v1 * 1.1 → keep change (winner = v2).
          - If ctr_v1 > ctr_v2 * 1.1 → restore original.
          - If difference < 10% → keep original (conservative).
        """
        video_id = row["video_id"]
        yt_video_id = row["yt_video_id"]
        channel_slug = row.get("channel_slug", "")
        phase = row["phase"]
        ctr_v1 = row.get("ctr_v1", 0)
        impressions_v1 = row.get("impressions_v1", 0)

        logger.info(
            "[AB] Second check: video %s (yt=%s) phase=%s v1: %.2f%% / %d imp",
            video_id, yt_video_id, phase, ctr_v1, impressions_v1,
        )

        # ── Fetch new CTR data ─────────────────────────────────
        ctr_data = self._fetch_ctr(row, post_change=True)
        ctr_v2 = ctr_data.get("ctr", 0.0)
        impressions_v2 = ctr_data.get("impressions", 0)
        retention_v2 = ctr_data.get("avg_duration", 0.0)

        logger.info(
            "[AB] Post-change data: %.2f%% | %d impressions | retention %.1fs",
            ctr_v2, impressions_v2, retention_v2,
        )

        # ── Insufficient post-change data ──────────────────────
        if impressions_v2 < self.min_impressions_post:
            change_age = self._change_age_hours(row, now)
            if change_age > self.second_check_hours * 2:
                # Too long waiting, use whatever we have
                logger.info("[AB] Video %s: post-change impressions still low after %dh — deciding with available data", video_id, change_age)
            else:
                logger.info("[AB] Video %s: only %d post-change impressions (< %d) — waiting", video_id, impressions_v2, self.min_impressions_post)
                return

        # ── Compare ────────────────────────────────────────────
        if ctr_v1 is None:
            ctr_v1 = 0
        if ctr_v2 is None:
            ctr_v2 = 0

        conn = self._get_db_conn()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        if ctr_v1 == 0 or ctr_v2 == 0:
            # Can't compare — keep original
            winner_title = row.get("title_v1", "")
            self._complete_test(video_id, winner_title, phase, ctr_v2,
                                impressions_v2, retention_v2, "original",
                                "insufficient_data")
            logger.info("[AB] Video %s: can't compare CTR (0 data) — keeping original", video_id)
        elif ctr_v2 > ctr_v1 * 1.1:
            # Improvement > 10% — keep the change
            winner_title = row.get("title_v2", row.get("title_v1", ""))
            self._complete_test(video_id, winner_title, phase, ctr_v2,
                                impressions_v2, retention_v2, "v2",
                                "winner_v2")
            logger.info("[AB] Video %s: CTR improved %.2f%% → %.2f%% — KEEPING change", video_id, ctr_v1, ctr_v2)
            self._record_learning(row, ctr_v1, ctr_v2, winner_title)
        elif ctr_v1 > ctr_v2 * 1.1:
            # Regression > 10% — restore original
            winner_title = row.get("title_v1", "")
            self._restore_original(row, phase)
            self._complete_test(video_id, winner_title, phase, ctr_v2,
                                impressions_v2, retention_v2, "v1",
                                "winner_v1")
            logger.info("[AB] Video %s: CTR dropped %.2f%% → %.2f%% — RESTORING original", video_id, ctr_v1, ctr_v2)
        else:
            # Difference < 10% — keep original (conservative)
            winner_title = row.get("title_v1", "")
            if phase == "title_rotated":
                self._restore_title(row)
            self._complete_test(video_id, winner_title, phase, ctr_v2,
                                impressions_v2, retention_v2, "v1",
                                "tie_kept_original")
            logger.info("[AB] Video %s: CTR change < 10%% (%.2f%% → %.2f%%) — keeping original", video_id, ctr_v1, ctr_v2)

    # ═══════════════════════════════════════════════════════════════
    # CTR fetching (quota-aware)
    # ═══════════════════════════════════════════════════════════════

    def _fetch_ctr(self, row: dict, post_change: bool = False) -> dict:
        """Fetch CTR data for a video. Quota-aware: local DB first, API fallback.

        Returns dict with:
          ctr: float (percentage, e.g. 2.5 = 2.5%)
          impressions: int
          avg_duration: float (seconds)
        """
        video_id = row["video_id"]
        yt_video_id = row["yt_video_id"]
        channel_slug = row.get("channel_slug", "")

        # ── Try local DB (video_stats_history) first ──────────
        try:
            conn = self._get_db_conn()
            stats_rows = conn.execute("""
                SELECT ctr, impressions, estimated_minutes_watched, average_view_duration
                FROM video_stats_history
                WHERE yt_video_id = ?
                ORDER BY fetched_at DESC
                LIMIT 5
            """, (yt_video_id,)).fetchall()

            if stats_rows:
                # Aggregate stats
                total_impressions = 0
                total_views = 0
                avg_ctr = 0.0
                avg_duration = 0.0
                count = 0

                for sr in stats_rows:
                    sr_dict = dict(sr)
                    imp = sr_dict.get("impressions", 0) or 0
                    c = sr_dict.get("ctr", 0) or 0
                    dur = sr_dict.get("average_view_duration", 0) or 0
                    if imp > 0:
                        total_impressions += imp
                        avg_ctr += c
                        avg_duration += dur
                        count += 1

                if count > 0 and total_impressions > 0:
                    return {
                        "ctr": round(avg_ctr / count, 2),
                        "impressions": total_impressions,
                        "avg_duration": round(avg_duration / count, 1),
                    }
        except Exception as exc:
            logger.debug("Local CTR fetch failed for %s: %s", video_id, exc)

        # ── Fallback: YouTube Analytics API ────────────────────
        if channel_slug:
            try:
                from pipeline.youtube_stats import YouTubeStatsFetcher
                fetcher = YouTubeStatsFetcher(channel_slug)
                if fetcher.authenticate():
                    # Use post-change window if needed
                    if post_change:
                        change_time = row.get("title_rotated_at") or row.get("thumbnail_rotated_at") or ""
                        if change_time:
                            from datetime import datetime as _dt
                            try:
                                start_dt = _dt.strptime(change_time[:10], "%Y-%m-%d")
                                start_date = start_dt.strftime("%Y-%m-%d")
                            except ValueError:
                                start_date = (_dt.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                        else:
                            start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                    else:
                        start_date = (datetime.now() - timedelta(days=self.first_check_hours // 24 + 1)).strftime("%Y-%m-%d")

                    end_date = datetime.now().strftime("%Y-%m-%d")
                    analytics = fetcher.get_video_ctr_analytics(
                        yt_video_id,
                        start_date=start_date,
                        end_date=end_date,
                    )
                    if analytics and analytics.get("impressions", 0) > 0:
                        ctr_pct = round(analytics.get("impressionsClickThroughRate", 0) * 100, 2)
                        return {
                            "ctr": ctr_pct,
                            "impressions": analytics.get("impressions", 0),
                            "avg_duration": round(analytics.get("averageViewDuration", 0), 1),
                        }
            except Exception as exc:
                logger.debug("API CTR fetch failed for %s: %s", video_id, exc)

        # ── Alert if both local and API failed ──
        video_id = row.get("video_id")
        channel_id = row.get("channel_id")
        self._raise_ab_alert(
            video_id, channel_id,
            'ctr_fetch_failed',
            f'No se pudo obtener CTR para video {video_id}',
            f'Ni la DB local ni la YouTube Analytics API devolvieron datos de CTR para {yt_video_id}. '
            f'El A/B test seguirá en fase pending hasta recibir datos.'
        )
        return {"ctr": 0.0, "impressions": 0, "avg_duration": 0.0}

    # ═══════════════════════════════════════════════════════════════
    # YouTube API interactions
    # ═══════════════════════════════════════════════════════════════

    def _rotate_title(self, row: dict, title_v1: str, ctr_v1: float):
        """Generate title_v2 and update it on YouTube via videos().update()."""
        video_id = row["video_id"]
        yt_video_id = row["yt_video_id"]
        channel_slug = row.get("channel_slug", "")
        channel_id = row["channel_id"]

        # ── Get script text from DB ────────────────────────────
        conn = self._get_db_conn()
        video = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not video:
            logger.error("[AB] Video %s not found in DB", video_id)
            return

        video_dict = dict(video)
        script_id = video_dict.get("script_id")

        # Get script content
        script_text = ""
        keywords = []
        if script_id:
            script_row = conn.execute("SELECT * FROM scripts WHERE id = ?", (script_id,)).fetchone()
            if script_row:
                script_dict = dict(script_row)
                script_text = script_dict.get("guion", "") or ""
                try:
                    kw_raw = script_dict.get("keywords_json", "[]") or "[]"
                    keywords = json.loads(kw_raw) if isinstance(kw_raw, str) else (kw_raw or [])
                except (json.JSONDecodeError, TypeError):
                    keywords = []

        if not script_text:
            logger.warning("[AB] No script text for video %s — skipping title rotation", video_id)
            return

        # ── Generate alternative title ─────────────────────────
        try:
            from config.config_bridge import get_channel_config
            from pipeline.metadata_generator import MetadataGenerator

            cfg = get_channel_config(channel_slug)
            generator = MetadataGenerator(cfg)
            title_v2 = generator.generate_alternative_title(
                title_v1=title_v1,
                script_text=script_text,
                keywords=keywords,
                ctr_v1=ctr_v1,
            )
        except Exception as exc:
            logger.error("[AB] Title generation failed for video %s: %s", video_id, exc)
            self._raise_ab_alert(
                video_id, channel_id,
                'title_generation_failed',
                f'Fallo al generar título alternativo para video {video_id}',
                f'Error del LLM al generar title_v2: {str(exc)[:300]}'
            )
            return

        if not title_v2 or title_v2 == title_v1:
            logger.warning("[AB] No valid alternative title for video %s — skipping", video_id)
            return

        # ── Swap title on YouTube ──────────────────────────────
        try:
            from pipeline.youtube_uploader import _YouTubeUploaderBackend
            backend = _YouTubeUploaderBackend(channel_slug)
            if backend.authenticate():
                backend._service.videos().update(
                    part="snippet",
                    body={
                        "id": yt_video_id,
                        "snippet": {"title": title_v2[:100]},
                    },
                ).execute()
                logger.info("[AB] Title changed on YT for %s: '%s' → '%s'", yt_video_id, title_v1[:40], title_v2[:40])
            else:
                logger.error("[AB] YT auth failed for channel %s — cannot rotate title", channel_slug)
                self._raise_ab_alert(
                    video_id, channel_id,
                    'title_rotation_failed',
                    f'Fallo de autenticación YT al rotar título de {yt_video_id}',
                    f'No se pudo autenticar con YouTube para el canal {channel_slug}. Verificar token.'
                )
                return
        except Exception as exc:
            logger.error("[AB] YT title update failed for %s: %s", yt_video_id, exc)
            self._raise_ab_alert(
                video_id, channel_id,
                'title_rotation_failed',
                f'Fallo al cambiar título en YouTube para {yt_video_id}',
                f'Error de YT API videos().update(): {str(exc)[:300]}'
            )
            return

        # ── Update DB ──────────────────────────────────────────
        conn.execute("""
            UPDATE video_ab_tests
            SET phase = 'title_rotated',
                title_v2 = ?,
                title_rotated_at = datetime('now'),
                updated_at = datetime('now')
            WHERE video_id = ?
        """, (title_v2, video_id))
        conn.commit()
        logger.info("[AB] Video %s: phase → title_rotated", video_id)

    def _swap_thumbnail_on_youtube(self, yt_video_id: str, channel_slug: str,
                                     variant_paths: list, new_variant: int,
                                     video_id: int = None, channel_id: int = None):
        """Swap the YouTube custom thumbnail to a different variant.

        Uses youtube.thumbnails().set() — costs 50 quota units.
        """
        if not variant_paths or new_variant > len(variant_paths):
            logger.warning("[AB] Cannot swap thumbnail: variant %d not available (have %d)", new_variant, len(variant_paths))
            return

        new_path = variant_paths[new_variant - 1]  # 0-indexed
        if not new_path or not Path(new_path).exists():
            logger.warning("[AB] Thumbnail variant %d path not found: %s", new_variant, new_path)
            return

        try:
            from pipeline.youtube_uploader import _YouTubeUploaderBackend
            backend = _YouTubeUploaderBackend(channel_slug)
            if backend.authenticate():
                backend._service.thumbnails().set(
                    videoId=yt_video_id,
                    media_body=str(new_path),
                ).execute()
                logger.info("[AB] Thumbnail swapped on YT for %s: variant %d", yt_video_id, new_variant)
            else:
                logger.error("[AB] YT auth failed for channel %s — cannot swap thumbnail", channel_slug)
                if video_id and channel_id:
                    self._raise_ab_alert(
                        video_id, channel_id,
                        'thumbnail_swap_failed',
                        f'Fallo de autenticación YT al rotar miniatura de {yt_video_id}',
                        f'No se pudo autenticar con YouTube para el canal {channel_slug}. Verificar token.'
                    )
        except Exception as exc:
            logger.error("[AB] YT thumbnail swap failed for %s: %s", yt_video_id, exc)
            if video_id and channel_id:
                self._raise_ab_alert(
                    video_id, channel_id,
                    'thumbnail_swap_failed',
                    f'Fallo al cambiar miniatura en YouTube para {yt_video_id}',
                    f'Error de YT API thumbnails().set(): {str(exc)[:300]}'
                )

    def _restore_original(self, row: dict, phase: str):
        """Restore the original title or thumbnail after a regression."""
        yt_video_id = row["yt_video_id"]
        channel_slug = row.get("channel_slug", "")
        title_v1 = row.get("title_v1", "")

        if phase == "title_rotated" and title_v1:
            self._restore_title(row)

    def _restore_title(self, row: dict):
        """Restore title_v1 on YouTube."""
        yt_video_id = row["yt_video_id"]
        channel_slug = row.get("channel_slug", "")
        title_v1 = row.get("title_v1", "")

        try:
            from pipeline.youtube_uploader import _YouTubeUploaderBackend
            backend = _YouTubeUploaderBackend(channel_slug)
            if backend.authenticate() and title_v1:
                backend._service.videos().update(
                    part="snippet",
                    body={
                        "id": yt_video_id,
                        "snippet": {"title": title_v1[:100]},
                    },
                ).execute()
                logger.info("[AB] Title restored on YT for %s: '%s'", yt_video_id, title_v1[:40])
        except Exception as exc:
            logger.error("[AB] YT title restore failed for %s: %s", yt_video_id, exc)

    # ═══════════════════════════════════════════════════════════════
    # Completion & learning
    # ═══════════════════════════════════════════════════════════════

    def _complete_test(self, video_id: int, winner_title: str, phase: str,
                       ctr_v2: float, impressions_v2: int, retention_v2: float,
                       winner_source: str, result: str):
        """Mark the test as completed with results."""
        conn = self._get_db_conn()
        conn.execute("""
            UPDATE video_ab_tests
            SET phase = 'completed',
                ctr_v2 = ?, impressions_v2 = ?, retention_v2 = ?,
                winner_title = ?,
                second_checked_at = datetime('now'),
                completed_at = datetime('now'),
                updated_at = datetime('now')
            WHERE video_id = ?
        """, (ctr_v2, impressions_v2, retention_v2, winner_title, video_id))
        conn.commit()
        logger.info("[AB] Video %s: test COMPLETED — winner: %s (source: %s)", video_id, winner_title[:40], winner_source)

    def _record_learning(self, row: dict, ctr_v1: float, ctr_v2: float,
                         winning_formula: str):
        """Record which formula type won for future title generation.

        Updates title_formula_performance table with the result.
        """
        channel_id = row["channel_id"]
        title_v1 = row.get("title_v1", "") or ""
        title_v2 = row.get("title_v2", "") or ""

        # Determine formula type of the winner
        winning_title = title_v2 if winning_formula and winning_formula != "skipped" else title_v1
        formula_type = self._classify_title_formula(winning_title)

        if not formula_type:
            return

        conn = self._get_db_conn()

        # Upsert: update existing row or insert new
        existing = conn.execute("""
            SELECT id, total_tests, total_wins, avg_ctr_improvement
            FROM title_formula_performance
            WHERE channel_id = ? AND formula_type = ?
        """, (channel_id, formula_type)).fetchone()

        if existing:
            existing_dict = dict(existing)
            new_tests = existing_dict["total_tests"] + 1
            new_wins = existing_dict["total_wins"]

            if ctr_v2 is not None and ctr_v1 is not None and winning_formula != "skipped":
                ctr_delta = ctr_v2 - ctr_v1
                if ctr_delta > 0:
                    new_wins += 1
                    old_avg = existing_dict.get("avg_ctr_improvement", 0) or 0
                    total = existing_dict["total_tests"] or 0
                    new_avg = (old_avg * total + ctr_delta) / (total + 1) if total > 0 else ctr_delta
                else:
                    new_avg = existing_dict.get("avg_ctr_improvement", 0) or 0
            else:
                new_avg = existing_dict.get("avg_ctr_improvement", 0) or 0

            conn.execute("""
                UPDATE title_formula_performance
                SET total_tests = ?, total_wins = ?, avg_ctr_improvement = ?,
                    updated_at = datetime('now')
                WHERE id = ?
            """, (new_tests, new_wins, round(new_avg, 3), existing_dict["id"]))
        else:
            ctr_delta = (ctr_v2 - ctr_v1) if (ctr_v2 is not None and ctr_v1 is not None and winning_formula != "skipped") else 0.0
            is_win = 1 if ctr_delta > 0 else 0
            conn.execute("""
                INSERT INTO title_formula_performance
                (channel_id, formula_type, total_tests, total_wins, avg_ctr_improvement)
                VALUES (?, ?, 1, ?, ?)
            """, (channel_id, formula_type, is_win, round(max(ctr_delta, 0), 3)))

        conn.commit()
        logger.info("[AB] Learning recorded: formula=%s channel=%s tests=%d", formula_type, channel_id, existing_dict.get("total_tests", 0) + 1 if existing else 1)

    # ═══════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════

    def _get_db_conn(self):
        """Get a database connection."""
        if hasattr(self.db, '_get_conn'):
            return self.db._get_conn()
        import sqlite3
        from config.settings import DATABASE_PATH
        conn = sqlite3.connect(str(DATABASE_PATH))
        conn.row_factory = sqlite3.Row
        return conn

    def _update_phase(self, video_id: int, phase: str, now: datetime):
        """Update phase and relevant timestamp."""
        conn = self._get_db_conn()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        if phase in ("skipped", "insufficient_data"):
            conn.execute("""
                UPDATE video_ab_tests
                SET phase = ?, completed_at = ?, updated_at = ?
                WHERE video_id = ?
            """, (phase, now_str, now_str, video_id))
        else:
            conn.execute("""
                UPDATE video_ab_tests
                SET phase = ?, updated_at = ?
                WHERE video_id = ?
            """, (phase, now_str, video_id))
        conn.commit()

    def _video_age_hours(self, row: dict, now: datetime) -> float:
        """Calculate video age in hours."""
        created = row.get("created_at", "")
        if not created:
            return 0
        try:
            if "T" in created:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                created_dt = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            return (now - created_dt).total_seconds() / 3600
        except (ValueError, TypeError):
            return 0

    def _change_age_hours(self, row: dict, now: datetime) -> float:
        """Calculate hours since last change (title or thumbnail rotation)."""
        changed_at = row.get("title_rotated_at") or row.get("thumbnail_rotated_at") or ""
        if not changed_at:
            return 0
        try:
            changed_dt = datetime.strptime(changed_at, "%Y-%m-%d %H:%M:%S")
            if changed_dt.tzinfo is None:
                changed_dt = changed_dt.replace(tzinfo=timezone.utc)
            return (now - changed_dt).total_seconds() / 3600
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _classify_title_formula(title: str) -> str:
        """Classify a title into formula type for learning."""
        if not title:
            return ""
        t = title.strip().lower()
        if "?" in t:
            return "question"
        if any(w in t for w in ["revelado", "filtrado", "censurado", "prohibido", "secreto", "exclusiva"]):
            return "revelation"
        if any(w in t for w in ["impactante", "shock", "aterrador", "horror", "pesadilla"]):
            return "shock"
        if any(w in t for w in ["urgente", "última hora", "ahora", "no verás", "antes de"]):
            return "urgency"
        if any(c.isdigit() for c in t[:10]) and any(w in t for w in ["cosas", "casos", "datos", "secretos", "razones"]):
            return "list"
        if t.startswith("cómo") or t.startswith("como"):
            return "how_to"
        if any(w in t for w in ["nadie", "nunca", "jamás", "imposible"]):
            return "curiosity_gap"
        return "statement"

    def _raise_ab_alert(self, video_id, channel_id, alert_type, title, message):
        """Create a pipeline alert for the monitor dashboard."""
        try:
            from api.services.lifecycle_monitor import create_alert
            create_alert(
                self.db,
                entity_type='video',
                entity_id=video_id,
                channel_id=channel_id,
                alert_type=f'ab_test_{alert_type}',
                severity='warning',
                title=title,
                message=message,
            )
        except Exception as exc:
            logger.debug("Failed to create AB test alert: %s", exc)


# ── Module-level helper for manual trigger ─────────────────────

def run_ab_test_cycle(db=None):
    """Convenience function to run one cycle from the API or CLI."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    worker = ABTestWorker(db)
    worker.run_cycle()
    return {"status": "ok", "message": "AB test cycle completed"}
