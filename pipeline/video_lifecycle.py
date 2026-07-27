"""Video Lifecycle Manager — Post-upload promotion orchestration.

After a video is published to YouTube, this module schedules and executes a
timeline of promotion actions:

   T+1min:   Add video to configured playlists
   T+5min:   Post engaging first comment
   T+12h:    Reply to viewer comments (round 1)
   T+24h:    Reply to viewer comments (round 2)
   T+48h:    Analyze CTR and performance
   T+72h:    Re-optimize metadata if CTR is low
   T+30min:  TikTok clip (if account configured)
   T+60min:  Twitter/X thread
   T+120min: Instagram Reel
   T+180min: Facebook post
   T+240min: Reddit post

All actions are idempotent and non-critical — failures don't affect the main
pipeline. The scheduler in api/main.py processes pending actions every 15 min.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from api.utils import db_now

from config.settings import (
    LIFECYCLE_DEFAULT_TIMELINE,
    LIFECYCLE_ENABLED,
    COMMENT_REPLY_MAX_PER_VIDEO,
    METADATA_OPTIMIZE_ENABLED,
    METADATA_OPTIMIZE_CTR_THRESHOLD,
    FIRST_COMMENT_ENABLED,
)

logger = logging.getLogger(__name__)


def _sync_broadcast_progress(job_id: int, progress: int, phase: str,
                              message: str, video_id: int = None,
                              status: str = "running"):
    """Sync progress update (DB write) + best-effort async WebSocket broadcast.

    Designed to be called from synchronous lifecycle handlers. Updates both
    the generation_jobs and videos tables, then schedules a WebSocket broadcast
    on the event loop if available. Falls back to frontend polling if the loop
    is unavailable.
    """
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    db.update_job(job_id, progress=progress, phase=phase, status=status)
    if video_id:
        db.update_video(video_id, progress=progress, progress_phase=phase)
    # Best-effort WebSocket broadcast via running event loop
    try:
        from api.services.generation_service import _broadcast_progress as _async_broadcast
        loop = asyncio.get_running_loop()
        loop.create_task(_async_broadcast(
            job_id, progress, phase, message, status, video_id,
        ))
    except (RuntimeError, ImportError):
        pass  # no running loop or module unavailable — frontend polling handles it


class VideoLifecycleManager:
    """Orchestrate post-upload promotion actions for a channel."""

    def __init__(self, channel_slug: str):
        self.slug = channel_slug
        self._db: Optional[object] = None

    @property
    def db(self):
        if self._db is None:
            from database.db_extended import ExtendedDatabase
            self._db = ExtendedDatabase()
        return self._db

    # ════════════════════════════════════════════════════════════
    # Scheduling: called immediately after upload
    # ════════════════════════════════════════════════════════════

    def on_video_published(self, db_video_id: int, yt_video_id: str,
                            channel_id: int, script_text: str = None,
                            timeline: list[dict] = None) -> int:
        """Schedule the full lifecycle timeline for a newly published video.

        Called from orchestrator.phase_upload() or generation_service after
        a successful YouTube upload.

        Args:
            db_video_id: Local DB video ID (videos.id)
            yt_video_id: YouTube video ID
            channel_id: Local DB channel ID (channels.id)
            script_text: Video script text (for LLM context)
            timeline: Optional custom timeline (defaults to LIFECYCLE_DEFAULT_TIMELINE)

        Returns number of actions scheduled.
        """
        if not LIFECYCLE_ENABLED:
            logger.debug("[%s] Lifecycle disabled — skipping for video %d", self.slug, db_video_id)
            return 0

        if timeline is None:
            # Check if channel has a custom timeline
            try:
                from config.config_bridge import get_channel_config
                config = get_channel_config(self.slug)
                custom_timeline = getattr(config, "LIFECYCLE_TIMELINE", None)
                if custom_timeline:
                    timeline = custom_timeline
                    logger.debug("[%s] Using custom lifecycle timeline", self.slug)
            except Exception:
                pass

        if timeline is None:
            timeline = LIFECYCLE_DEFAULT_TIMELINE

        now = datetime.now(timezone.utc)
        scheduled_count = 0

        for action_def in timeline:
            action_type = action_def["action"]
            
            # Skip first_comment scheduling if disabled (v12)
            if action_type == "first_comment" and not FIRST_COMMENT_ENABLED:
                continue
            
            offset_minutes = action_def.get("offset_minutes", 0)
            offset_hours = action_def.get("offset_hours", 0)
            total_offset = timedelta(minutes=offset_minutes, hours=offset_hours)
            scheduled_for = (now + total_offset).isoformat()

            # Build action-specific config
            config_json = None
            if action_type == "first_comment" and script_text:
                import json
                config_json = json.dumps({"script_snippet": script_text[:2000]})
            elif action_type == "metadata_reoptimize" and script_text:
                import json
                config_json = json.dumps({"script_snippet": script_text[:2500]})

            try:
                self.db.create_lifecycle_action(
                    video_id=db_video_id,
                    action_type=action_type,
                    channel_id=channel_id,
                    yt_video_id=yt_video_id,
                    scheduled_for=scheduled_for,
                    config_json=config_json,
                )
                scheduled_count += 1
            except Exception as exc:
                logger.error("[%s] Failed to schedule %s for video %d: %s",
                             self.slug, action_type, db_video_id, exc)

        logger.info("[%s] Lifecycle: scheduled %d actions for video %d (yt: %s)",
                     self.slug, scheduled_count, db_video_id, yt_video_id)
        return scheduled_count

    def on_video_uploaded_scheduled(self, db_video_id: int, yt_video_id: str,
                                     channel_id: int, script_text: str = None,
                                     target_public_at: str = None,
                                     warmup_until: str = None) -> int:
        """Schedule the scheduled-publishing lifecycle for a video uploaded as private.

        Unlike on_video_published (immediate mode), the actions are timed relative
        to the target PUBLIC time, not the upload time. The key action is 'go_public'
        which sets privacy=public at the peak target hour.
        """
        if not LIFECYCLE_ENABLED:
            logger.debug("[%s] Lifecycle disabled — skipping for video %d", self.slug, db_video_id)
            return 0

        now_iso = datetime.now(timezone.utc).isoformat()
        scheduled_count = 0

        # ── v10.1: Collision guard — avoid same-channel same-hour go_public ──
        SAME_CHANNEL_GAP_HOURS = 3
        if target_public_at:
            try:
                from datetime import datetime as _dt2, timedelta as _td2
                proposed_dt = _dt2.fromisoformat(
                    target_public_at.replace("Z", "+00:00").replace(" ", "T")
                )
                if proposed_dt.tzinfo is None:
                    proposed_dt = proposed_dt.replace(tzinfo=timezone.utc)

                # Check for existing pending go_public actions for this channel
                # using a direct query since get_due_lifecycle_actions doesn't filter
                with self.db._connect() as conn:
                    existing_rows = conn.execute(
                        """SELECT vla.scheduled_for
                           FROM video_lifecycle_actions vla
                           WHERE vla.channel_id = ?
                             AND vla.action_type = 'go_public'
                             AND vla.status = 'pending'
                           ORDER BY vla.scheduled_for DESC
                           LIMIT 10""",
                        (channel_id,),
                    ).fetchall()

                if existing_rows:
                    # Find the latest go_public time for this channel
                    latest_go_public = None
                    for (sf,) in existing_rows:
                        if sf:
                            try:
                                sf_dt = _dt2.fromisoformat(
                                    sf.replace("Z", "+00:00")
                                )
                                if sf_dt.tzinfo is None:
                                    sf_dt = sf_dt.replace(tzinfo=timezone.utc)
                                if latest_go_public is None or sf_dt > latest_go_public:
                                    latest_go_public = sf_dt
                            except (ValueError, TypeError):
                                pass

                    if latest_go_public is not None:
                        gap = proposed_dt - latest_go_public
                        min_gap = _td2(hours=SAME_CHANNEL_GAP_HOURS)
                        if abs(gap) < min_gap:
                            # Collision! Push forward
                            new_proposed = latest_go_public + min_gap
                            # Also check against now + warmup
                            warmup_dt = datetime.now(timezone.utc) + timedelta(minutes=120)
                            new_proposed = max(new_proposed, warmup_dt)
                            logger.warning(
                                "[%s] COLLISION GUARD: proposed go_public %s is within %dh of "
                                "existing go_public at %s. Pushing to %s.",
                                self.slug,
                                proposed_dt.strftime("%m-%d %H:%M"),
                                SAME_CHANNEL_GAP_HOURS,
                                latest_go_public.strftime("%m-%d %H:%M"),
                                new_proposed.strftime("%m-%d %H:%M"),
                            )
                            target_public_at = new_proposed.isoformat()
            except Exception as e:
                logger.debug("[%s] Collision guard skipped: %s", self.slug, e)

        # ── 1. go_public: set video to public at target time ──
        if target_public_at:
            self.db.create_lifecycle_action(
                video_id=db_video_id,
                action_type="go_public",
                channel_id=channel_id,
                yt_video_id=yt_video_id,
                scheduled_for=target_public_at,
                config_json=None,
            )
            scheduled_count += 1
            logger.info("[%s] Scheduled go_public for video %d at %s",
                        self.slug, db_video_id, target_public_at)

            # ── 2. Playlist add: 1 min after go_public (i.e., after public) ──
            from datetime import datetime as _dt, timedelta as _td
            try:
                target_dt = _dt.fromisoformat(target_public_at)
            except (ValueError, TypeError):
                target_dt = _dt.now() + _td(hours=2)  # fallback — uses Europe/Madrid local time
            playlist_at = (target_dt + _td(minutes=1)).isoformat()
            self.db.create_lifecycle_action(
                video_id=db_video_id,
                action_type="playlist_add",
                channel_id=channel_id,
                yt_video_id=yt_video_id,
                scheduled_for=playlist_at,
            )
            scheduled_count += 1

            # ── 3. First comment: 5 min after public ──
            if FIRST_COMMENT_ENABLED:
                comment_at = (target_dt + _td(minutes=5)).isoformat()
                config_json = None
                if script_text:
                    import json
                    config_json = json.dumps({"script_snippet": script_text[:2000]})
                self.db.create_lifecycle_action(
                    video_id=db_video_id,
                    action_type="first_comment",
                    channel_id=channel_id,
                    yt_video_id=yt_video_id,
                    scheduled_for=comment_at,
                    config_json=config_json,
                )
                scheduled_count += 1

            # ── 4-5. Comment replies at 12h and 24h after public ──
            reply1_at = (target_dt + _td(hours=12)).isoformat()
            self.db.create_lifecycle_action(
                video_id=db_video_id,
                action_type="comment_reply_1",
                channel_id=channel_id,
                yt_video_id=yt_video_id,
                scheduled_for=reply1_at,
            )
            scheduled_count += 1

            reply2_at = (target_dt + _td(hours=24)).isoformat()
            self.db.create_lifecycle_action(
                video_id=db_video_id,
                action_type="comment_reply_2",
                channel_id=channel_id,
                yt_video_id=yt_video_id,
                scheduled_for=reply2_at,
            )
            scheduled_count += 1

            # ── 6. CTR check at 48h after public ──
            ctr_at = (target_dt + _td(hours=48)).isoformat()
            self.db.create_lifecycle_action(
                video_id=db_video_id,
                action_type="ctr_check",
                channel_id=channel_id,
                yt_video_id=yt_video_id,
                scheduled_for=ctr_at,
            )
            scheduled_count += 1

            # ── 7. Metadata reoptimize at 72h after public ──
            meta_at = (target_dt + _td(hours=72)).isoformat()
            meta_config = None
            if script_text:
                import json
                meta_config = json.dumps({"script_snippet": script_text[:2500]})
            self.db.create_lifecycle_action(
                video_id=db_video_id,
                action_type="metadata_reoptimize",
                channel_id=channel_id,
                yt_video_id=yt_video_id,
                scheduled_for=meta_at,
                config_json=meta_config,
            )
            scheduled_count += 1

            # ── 8-12: Social media promotion actions ──
            # Read per-platform timing from channel config
            social_timing = self._get_social_timing()

            social_actions = [
                ("social_clip_tiktok", "tiktok"),
                ("social_thread_twitter", "twitter"),
                ("social_reel_instagram", "instagram"),
                ("social_post_facebook", "facebook"),
                ("social_post_reddit", "reddit"),
            ]

            social_config = None
            if script_text:
                import json
                social_config = json.dumps({
                    "script_text": script_text,
                    "db_video_id": db_video_id,
                })

            for action_type, platform_key in social_actions:
                delay_min = social_timing.get(platform_key, 0)
                if delay_min <= 0:
                    # Platform not configured or disabled — skip
                    continue

                # Check if channel has an enabled account for this platform
                if not self._has_social_account(platform_key):
                    logger.debug("[%s] No enabled %s account — skipping %s",
                                 self.slug, platform_key, action_type)
                    continue

                social_at = (target_dt + _td(minutes=delay_min)).isoformat()
                self.db.create_lifecycle_action(
                    video_id=db_video_id,
                    action_type=action_type,
                    channel_id=channel_id,
                    yt_video_id=yt_video_id,
                    scheduled_for=social_at,
                    config_json=social_config,
                )
                scheduled_count += 1
                logger.debug("[%s] Scheduled %s at T+%dmin (%s)",
                             self.slug, action_type, delay_min, social_at)

        else:
            # No target time provided — fallback to wrap-up now
            logger.warning("[%s] No target_public_at — scheduling go_public with warmup only", self.slug)
            self.db.create_lifecycle_action(
                video_id=db_video_id,
                action_type="go_public",
                channel_id=channel_id,
                yt_video_id=yt_video_id,
                scheduled_for=warmup_until or now_iso,
            )
            scheduled_count += 1

        logger.info("[%s] Lifecycle (scheduled): %d actions for video %d (target: %s)",
                     self.slug, scheduled_count, db_video_id, target_public_at or "N/A")
        return scheduled_count

    # ════════════════════════════════════════════════════════════
    # Execution: called by scheduler
    # ════════════════════════════════════════════════════════════

    def process_due_actions(self) -> dict:
        """Find and execute all due lifecycle actions for this channel.

        Called periodically by the scheduler (every 15 min).

        Returns {processed: N, succeeded: N, failed: N}.
        """
        if not LIFECYCLE_ENABLED:
            return {"processed": 0, "succeeded": 0, "failed": 0}

        # Get channel DB record for this slug
        ch = self.db.get_channel_by_slug(self.slug)
        if not ch:
            logger.warning("[%s] Channel not found in DB", self.slug)
            return {"processed": 0, "succeeded": 0, "failed": 0}

        channel_id = ch["id"]

        # Find due actions (global query, filtered by channel_id below)
        all_due = self.db.get_due_lifecycle_actions()
        due_actions = [a for a in all_due if a["channel_id"] == channel_id]

        if not due_actions:
            return {"processed": 0, "succeeded": 0, "failed": 0}

        processed, succeeded, failed = 0, 0, 0
        by_type = {}  # action_type → {succeeded, failed}
        now_iso = datetime.now(timezone.utc).isoformat()

        logger.info("[%s] 📋 Procesando %d acciones lifecycle pendientes", self.slug, len(due_actions))

        for action in due_actions:
            action_id = action["id"]
            action_type = action["action_type"]
            yt_video_id = action.get("yt_video_id")

            if not yt_video_id:
                self.db.update_lifecycle_action_status(
                    action_id, "failed",
                    executed_at=now_iso,
                    error_message="No yt_video_id available",
                )
                failed += 1
                continue

            try:
                success = self._dispatch(action)
                if success:
                    self.db.update_lifecycle_action_status(
                        action_id, "executed", executed_at=now_iso,
                    )
                    succeeded += 1
                    by_type[action_type] = by_type.get(action_type, {"succeeded": 0, "failed": 0})
                    by_type[action_type]["succeeded"] += 1
                else:
                    # Handle retry
                    retry_count = action.get("retry_count", 0)
                    max_retries = action.get("max_retries", 2)
                    if retry_count < max_retries:
                        # Retry in 1 hour
                        retry_time = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
                        self.db.update_lifecycle_action_status(
                            action_id, "pending",
                            scheduled_for=retry_time,
                            retry_count=retry_count + 1,
                        )
                        logger.debug("[%s] Action %d retry %d/%d", self.slug, action_id, retry_count + 1, max_retries)
                    else:
                        self.db.update_lifecycle_action_status(
                            action_id, "failed",
                            executed_at=now_iso,
                            error_message="Max retries exceeded",
                        )
                        failed += 1
                        by_type[action_type] = by_type.get(action_type, {"succeeded": 0, "failed": 0})
                        by_type[action_type]["failed"] += 1
            except Exception as exc:
                logger.error("[%s] Action %d (%s) failed: %s", self.slug, action_id, action_type, exc)
                by_type[action_type] = by_type.get(action_type, {"succeeded": 0, "failed": 0})
                by_type[action_type]["failed"] += 1
                self.db.update_lifecycle_action_status(
                    action_id, "failed",
                    executed_at=now_iso,
                    error_message=str(exc)[:500],
                )
                failed += 1

            processed += 1

        # ── Build summary string ──
        summary_parts = []
        for atype, counts in sorted(by_type.items()):
            s = counts["succeeded"]
            f = counts["failed"]
            if s or f:
                parts = []
                if s:
                    parts.append(f"✓{s}")
                if f:
                    parts.append(f"✗{f}")
                summary_parts.append(f"{atype}={'+'.join(parts)}")
        summary_str = ", ".join(summary_parts) if summary_parts else "ninguna"

        if processed > 0:
            logger.info(
                "[%s] 📋 Lifecycle: %d procesadas (%d ok, %d fallos) — %s",
                self.slug, processed, succeeded, failed, summary_str,
            )
        else:
            logger.debug("[%s] 📋 Lifecycle: 0 acciones pendientes", self.slug)

        return {"processed": processed, "succeeded": succeeded, "failed": failed}

    def _dispatch(self, action: dict) -> bool:
        """Route a lifecycle action to the appropriate handler.

        Returns True if the action was executed successfully.
        """
        action_type = action["action_type"]
        yt_video_id = action["yt_video_id"]
        db_video_id = action["video_id"]

        if action_type == "playlist_add":
            return self._handle_playlist_add(yt_video_id, db_video_id, action)
        elif action_type == "go_public":
            return self._handle_go_public(yt_video_id, db_video_id, action)
        elif action_type == "first_comment":
            from config.settings import FIRST_COMMENT_ENABLED
            if not FIRST_COMMENT_ENABLED:
                import json
                self.db.update_lifecycle_action_status(
                    action["id"], "skipped",
                    result_json=json.dumps({"skipped": True, "reason": "FIRST_COMMENT_ENABLED=false"}),
                )
                return True
            return self._handle_first_comment(yt_video_id, db_video_id, action)
        elif action_type in ("comment_reply_1", "comment_reply_2"):
            return self._handle_comment_reply(yt_video_id, db_video_id, action)
        elif action_type == "ctr_check":
            return self._handle_ctr_check(yt_video_id, db_video_id, action)
        elif action_type == "metadata_reoptimize":
            return self._handle_metadata_reoptimize(yt_video_id, db_video_id, action)
        elif action_type.startswith("social_"):
            return self._handle_social_action(yt_video_id, db_video_id, action)
        else:
            logger.warning("[%s] Unknown action_type: %s", self.slug, action_type)
            return False

    # ── Action handlers ───────────────────────────────────────────

    def _handle_playlist_add(self, yt_video_id: str, db_video_id: int,
                              _action: dict) -> bool:
        """Add video to all configured playlists (legacy — deprecated).
        
        Since the playlist redesign, videos are assigned to a single target
        playlist during pipeline execution (before scraping). This handler
        is kept for backward compatibility with any pending lifecycle actions
        from older videos, but no longer uses auto-classify.
        """
        from pipeline.youtube_playlists import YouTubePlaylistManager

        mgr = YouTubePlaylistManager(self.slug)
        if not mgr.authenticate():
            logger.error("[%s] Cannot auth for playlist add", self.slug)
            return False

        # Sync playlists (create any missing)
        sync_result = mgr.sync_playlists_from_config()

        # Cache created/existing playlist IDs in DB
        channel = self.db.get_channel_by_slug(self.slug)
        channel_id = channel["id"] if channel else None

        if channel_id:
            for pl in sync_result.get("created", []):
                self.db.upsert_youtube_playlist(
                    channel_id, pl["slug"], pl["yt_playlist_id"], pl["name"],
                )
            for pl in sync_result.get("existing", []):
                self.db.upsert_youtube_playlist(
                    channel_id, pl["slug"], pl["yt_playlist_id"], pl["name"],
                )

        # Check if video already has a target playlist assigned by pipeline
        video = self.db.get_video(db_video_id)
        tgt_slug = (video.get("target_playlist_slug") or "") if video else ""
        
        if tgt_slug:
            # Video already assigned to a playlist — use add_video_to_playlist_by_slug
            result = mgr.add_video_to_playlist_by_slug(
                yt_video_id, tgt_slug, channel_id=channel_id
            )
            added = [tgt_slug] if result.get("yt_playlist_item_id") else []
            already_in = [tgt_slug] if result.get("was_already_present") else []
        else:
            # Legacy: add to all playlists
            add_result = mgr.add_video_to_all_playlists(yt_video_id)
            added = add_result.get("added_to", [])
            already_in = add_result.get("already_in", [])

        # Record in DB
        if channel_id:
            for slug_key in added:
                cached = self.db.get_playlist_by_slug(channel_id, slug_key)
                if cached:
                    self.db.add_video_to_playlist_db(db_video_id, cached["id"])

            for slug_key in already_in:
                cached = self.db.get_playlist_by_slug(channel_id, slug_key)
                if cached:
                    self.db.add_video_to_playlist_db(db_video_id, cached["id"])

        # Store result
        import json
        result_data = {"added_to": added, "already_in": already_in}
        self.db.update_lifecycle_action_status(
            _action["id"], "executed",
            result_json=json.dumps(result_data, ensure_ascii=False),
        )
        return True

    def _handle_go_public(self, yt_video_id: str, db_video_id: int,
                           _action: dict) -> bool:
        """Set a private/unlisted video to public at the scheduled peak time.
        
        Creates a short-lived 'publish' job so the frontend progress bar shows
        real-time feedback during the go-public phase.
        """
        from pipeline.youtube_uploader import YouTubeUploader

        # Get channel slug for this video
        ch = self.db.get_video(db_video_id)
        slug = ch.get("canal") if ch else self.slug
        title = ch.get("titulo_final", "?") if ch else "?"
        uploaded_at = ch.get("created_at", "?") if ch else "?"
        target_public = ch.get("target_public_at", "?") if ch else "?"
        
        # Get channel_id for job creation
        ch_record = self.db.get_channel_by_slug(slug)
        channel_id = ch_record["id"] if ch_record else None

        # ── Create publish job for progress bar feedback ──
        publish_job_id = None
        if channel_id:
            try:
                publish_job_id = self.db.create_job(channel_id, "publish", db_video_id)
                _sync_broadcast_progress(publish_job_id, 10, "publish",
                                          f"Preparando publicación: {title[:50]}...",
                                          video_id=db_video_id)
            except Exception as exc:
                logger.warning("[%s] Could not create publish job: %s", self.slug, exc)

        # ── Resolve local time for display ──
        local_time_str = ""
        try:
            from config.config_bridge import get_channel_config
            config = get_channel_config(slug)
            tz_str = getattr(config, "PUBLISH_TIMEZONE", "Europe/Madrid")
            import pytz
            from datetime import datetime as _dt, timezone as _tz
            tz = pytz.timezone(tz_str)
            now_local = _dt.now(tz).strftime("%Y-%m-%d %H:%M:%S")
            local_time_str = f" ({now_local} {tz_str})"
        except Exception:
            pass

        uploader = YouTubeUploader(slug)
        if not uploader.authenticate():
            logger.error("[%s] ❌ PUBLICAR: auth fallida para video %s — NO se pudo hacer público",
                         self.slug, yt_video_id)
            if publish_job_id:
                _sync_broadcast_progress(publish_job_id, 0, "publish",
                                          "Error: autenticación fallida",
                                          video_id=db_video_id, status="failed")
            return False

        try:
            from datetime import datetime, timezone
            
            if publish_job_id:
                _sync_broadcast_progress(publish_job_id, 50, "publish",
                                          "Haciendo público en YouTube...",
                                          video_id=db_video_id)
            
            now = datetime.now()
            # ── Past-due catch-up log ──
            if target_public and target_public != "?":
                try:
                    from datetime import timezone as _tz
                    target_dt = datetime.fromisoformat(str(target_public).replace("Z", "+00:00"))
                    if target_dt < now.replace(tzinfo=_tz.utc):
                        logger.info(
                            "[%s] ⚠️ PAST-DUE go_public: video %s was scheduled for %s "
                            "(%.1fh ago) — publishing NOW as catch-up",
                            self.slug, yt_video_id,
                            target_dt.strftime("%m-%d %H:%M"),
                            (now.replace(tzinfo=_tz.utc) - target_dt).total_seconds() / 3600,
                        )
                except Exception:
                    pass
            result = uploader.set_privacy(yt_video_id, "public")
            if result.get("updated") or result.get("privacy") == "public":
                # Update video status in DB
                self.db.update_video(
                    db_video_id,
                    status="published",
                    privacy_status="public",
                    published_at=db_now(),
                )
                # ── Log detallado del evento de publicación ──
                logger.info(
                    "[%s] ✅ PUBLICADO: '%s' (yt=%s, id=%d) | "
                    "subido=%s | target=%s | real=%s%s",
                    self.slug, title, yt_video_id, db_video_id,
                    uploaded_at, target_public, now.isoformat(), local_time_str,
                )
                # ── Complete publish job ──
                if publish_job_id:
                    _sync_broadcast_progress(publish_job_id, 100, "publish",
                                              f"Video publicado: {title[:50]}",
                                              video_id=db_video_id, status="completed")
                # ── Also log to the dedicated scheduled_publish log ──
                try:
                    from api.services.scheduled_publish_logger import log_publish_event
                    log_publish_event(
                        event="go_public",
                        slug=slug,
                        video_title=title,
                        yt_video_id=yt_video_id,
                        db_video_id=db_video_id,
                        uploaded_at=str(uploaded_at),
                        target_public_at=str(target_public),
                        actual_public_at=now.isoformat(),
                        local_time=local_time_str,
                    )
                except Exception:
                    pass
                return True
            else:
                logger.error(
                    "[%s] ❌ PUBLICAR fallido: video %s ('%s') — respuesta: %s",
                    self.slug, yt_video_id, title, result,
                )
                if publish_job_id:
                    _sync_broadcast_progress(publish_job_id, 0, "publish",
                                              "Error: YouTube no confirmó la publicación",
                                              video_id=db_video_id, status="failed")
                return False
        except Exception as e:
            logger.error(
                "[%s] ❌ PUBLICAR excepción: video %s ('%s') — %s",
                self.slug, yt_video_id, title, e,
            )
            if publish_job_id:
                _sync_broadcast_progress(publish_job_id, 0, "publish",
                                          f"Error: {str(e)[:100]}",
                                          video_id=db_video_id, status="failed")
            return False

    def _handle_first_comment(self, yt_video_id: str, db_video_id: int,
                               _action: dict) -> bool:
        """Post an engaging first comment on the video."""
        from config.settings import FIRST_COMMENT_ENABLED
        if not FIRST_COMMENT_ENABLED:
            import json
            self.db.update_lifecycle_action_status(
                _action["id"], "skipped",
                result_json=json.dumps({"skipped": True, "reason": "FIRST_COMMENT_ENABLED=false"}),
            )
            return True

        from pipeline.youtube_comments import YouTubeCommentManager

        mgr = YouTubeCommentManager(self.slug)
        if not mgr.authenticate():
            return False

        # Extract script snippet from config if available
        script_text = None
        if _action.get("config_json"):
            try:
                import json
                cfg = json.loads(_action["config_json"])
                script_text = cfg.get("script_snippet")
            except Exception:
                pass

        result = mgr.post_first_comment(yt_video_id, script_text, db_video_id)

        if result.get("skipped"):
            import json
            self.db.update_lifecycle_action_status(
                _action["id"], "skipped",
                result_json=json.dumps(result, ensure_ascii=False),
            )
            return True  # Skipped is not a failure

        if result.get("yt_comment_id"):
            # Log to comment_log
            self.db.log_comment(
                db_video_id, yt_video_id, result["yt_comment_id"],
                comment_type="first", comment_text=result.get("text"),
            )
            import json
            self.db.update_lifecycle_action_status(
                _action["id"], "executed",
                result_json=json.dumps(result, ensure_ascii=False),
            )
            return True

        return False

    def _handle_comment_reply(self, yt_video_id: str, db_video_id: int,
                               _action: dict) -> bool:
        """Reply to viewer comments."""
        from config.settings import COMMENT_REPLY_ENABLED
        if not COMMENT_REPLY_ENABLED:
            self.db.update_lifecycle_action_status(_action["id"], "skipped",
                                                    error_message="comment_reply disabled")
            return True

        from pipeline.youtube_comments import YouTubeCommentManager

        mgr = YouTubeCommentManager(self.slug)
        if not mgr.authenticate():
            return False

        result = mgr.reply_to_comments(yt_video_id, COMMENT_REPLY_MAX_PER_VIDEO, db_video_id)

        # Log each reply
        # Note: we don't have individual comment IDs from the batch reply method
        # For now, just record the action result

        import json
        self.db.update_lifecycle_action_status(
            _action["id"], "executed",
            result_json=json.dumps(result, ensure_ascii=False),
        )
        return True

    def _handle_ctr_check(self, yt_video_id: str, db_video_id: int,
                           _action: dict) -> bool:
        """Analyze video performance (CTR, views, engagement).

        This is an informational-only action. No automatic changes are made
        based on CTR (thumbnail A/B is deferred to a future module).
        The action records analytics data for the user to review.
        """
        try:
            from pipeline.youtube_stats import YouTubeStatsFetcher

            fetcher = YouTubeStatsFetcher(self.slug)
            if not fetcher.authenticate():
                logger.warning("[%s] Cannot auth for CTR check", self.slug)
                return False

            stats = fetcher.get_video_stats(yt_video_id)
            analytics = fetcher.get_video_analytics(yt_video_id, days=2)

            import json
            result_data = {
                "viewCount": stats.get("viewCount", 0),
                "likeCount": stats.get("likeCount", 0),
                "commentCount": stats.get("commentCount", 0),
                "estimatedMinutesWatched": analytics.get("estimatedMinutesWatched", 0),
                "averageViewDuration": analytics.get("averageViewDuration", 0),
            }

            self.db.update_lifecycle_action_status(
                _action["id"], "executed",
                result_json=json.dumps(result_data, ensure_ascii=False),
            )
            logger.info("[%s] CTR check for %s: views=%s likes=%s comments=%s",
                         self.slug, yt_video_id,
                         result_data["viewCount"],
                         result_data["likeCount"],
                         result_data["commentCount"])
            return True

        except Exception as exc:
            logger.warning("[%s] CTR check failed for %s: %s", self.slug, yt_video_id, exc)
            return False

    def _handle_metadata_reoptimize(self, yt_video_id: str, db_video_id: int,
                                     _action: dict) -> bool:
        """Re-optimize video metadata if CTR is low.

        Only runs if METADATA_OPTIMIZE_ENABLED is True.
        """
        if not METADATA_OPTIMIZE_ENABLED:
            self.db.update_lifecycle_action_status(_action["id"], "skipped",
                                                    error_message="metadata_optimize disabled")
            return True

        # Get script text from config_json
        script_text = None
        if _action.get("config_json"):
            try:
                import json
                cfg = json.loads(_action["config_json"])
                script_text = cfg.get("script_snippet")
            except Exception:
                pass

        if not script_text:
            # Try to get script from DB
            video = self.db.get_video(db_video_id)
            if video:
                # We only stored script snippets, not the full script
                # Fallback: skip optimization without script context
                logger.warning("[%s] No script text for metadata reoptimization of video %d",
                               self.slug, db_video_id)
                self.db.update_lifecycle_action_status(_action["id"], "skipped",
                                                        error_message="no script text available")
                return True

        # Get current video stats
        try:
            from pipeline.youtube_stats import YouTubeStatsFetcher
            fetcher = YouTubeStatsFetcher(self.slug)
            if fetcher.authenticate():
                stats = fetcher.get_video_stats(yt_video_id)
                analytics = fetcher.get_video_analytics(yt_video_id, days=3)
            else:
                stats = {}
                analytics = {}
        except Exception:
            stats, analytics = {}, {}

        # Get current title/description from the video record
        video = self.db.get_video(db_video_id)
        current_title = video.get("titulo_final", "") if video else ""
        current_description = video.get("description", "") if video else ""

        if not current_title:
            # Fall back to YouTube API
            if stats.get("title"):
                current_title = stats["title"]

        # Run optimization
        from pipeline.metadata_optimizer import MetadataOptimizer
        optimizer = MetadataOptimizer(self.slug)
        if not optimizer.authenticate():
            return False

        analytics_data = {
            "viewCount": stats.get("viewCount", 0),
            "likeCount": stats.get("likeCount", 0),
            "commentCount": stats.get("commentCount", 0),
        }

        result = optimizer.run_full_optimization(
            yt_video_id, script_text,
            current_title, current_description,
            analytics_data,
        )

        import json
        if result and "error" not in result:
            self.db.update_lifecycle_action_status(
                _action["id"], "executed",
                result_json=json.dumps(result, ensure_ascii=False),
            )
            # Update the video record with the new title
            if result.get("new_title"):
                try:
                    self.db.update_video(db_video_id,
                                          titulo_final=result["new_title"])
                except Exception:
                    pass
            return True
        elif result:
            self.db.update_lifecycle_action_status(
                _action["id"], "failed",
                result_json=json.dumps(result, ensure_ascii=False),
                error_message=result.get("error", "Unknown error"),
            )
            return False
        else:
            self.db.update_lifecycle_action_status(
                _action["id"], "failed",
                error_message="reoptimization returned no result",
            )
            return False

    # ── Social media helpers ─────────────────────────────────────

    def _get_social_timing(self) -> dict:
        """Read per-platform social timing delays from channel config."""
        try:
            from config.config_bridge import get_channel_config
            config = get_channel_config(self.slug)
            social_timing = getattr(config, "SOCIAL_TIMING", None)
            if social_timing and isinstance(social_timing, dict):
                return social_timing
        except Exception:
            pass

        # Check config_json in DB
        try:
            ch = self.db.get_channel_by_slug(self.slug)
            if ch and ch.get("config_json"):
                import json
                cfg = ch["config_json"]
                if isinstance(cfg, str):
                    cfg = json.loads(cfg)
                st = cfg.get("SOCIAL_TIMING", {})
                if st:
                    return st
        except Exception:
            pass

        # Default delays
        return {
            "tiktok": 30,
            "twitter": 60,
            "instagram": 120,
            "facebook": 180,
            "reddit": 240,
        }

    def _has_social_account(self, platform: str) -> bool:
        """Check if the channel has an enabled social account for this platform."""
        try:
            ch = self.db.get_channel_by_slug(self.slug)
            if not ch:
                return False
            accounts = self.db.get_enabled_social_accounts(ch["id"])
            return any(a["platform"] == platform and a["enabled"] for a in accounts)
        except Exception:
            return False

    def _handle_social_action(self, yt_video_id: str, db_video_id: int,
                              action: dict) -> bool:
        """Handle all social media posting actions.

        Routes to the correct platform publisher based on action_type.
        """
        action_type = action["action_type"]
        platform_map = {
            "social_clip_tiktok": "tiktok",
            "social_thread_twitter": "twitter",
            "social_reel_instagram": "instagram",
            "social_post_facebook": "facebook",
            "social_post_reddit": "reddit",
        }
        platform = platform_map.get(action_type)
        if not platform:
            logger.warning("[%s] Unknown social action type: %s", self.slug, action_type)
            return False

        try:
            import asyncio
            result = asyncio.run(self._publish_social_post(
                action, platform, yt_video_id, db_video_id,
            ))
            return result
        except RuntimeError as exc:
            # If already in an event loop, use create_task
            if "This event loop is already running" in str(exc):
                import nest_asyncio
                try:
                    nest_asyncio.apply()
                    result = asyncio.run(self._publish_social_post(
                        action, platform, yt_video_id, db_video_id,
                    ))
                    return result
                except ImportError:
                    logger.error("[%s] nest_asyncio not available for social publish", self.slug)
                    return False
            logger.error("[%s] Event loop error for %s: %s", self.slug, platform, exc)
            return False
        except Exception as exc:
            logger.error("[%s] Social publish failed for %s: %s", self.slug, platform, exc)
            self.db.update_lifecycle_action_status(
                action["id"], "failed", error_message=str(exc)[:500],
            )
            return False

    async def _publish_social_post(self, action: dict, platform: str,
                                    yt_video_id: str, db_video_id: int) -> bool:
        """Publish content to a social media platform."""
        # ── 1. Get account credentials ──
        ch = self.db.get_channel_by_slug(self.slug)
        if not ch:
            logger.error("[%s] Channel not found for social publish", self.slug)
            return False

        acct = self.db.get_social_account(ch["id"], platform)
        if not acct or not acct.get("enabled"):
            logger.info("[%s] No enabled %s account — skipping", self.slug, platform)
            self.db.update_lifecycle_action_status(action["id"], "skipped",
                                                    error_message=f"no enabled {platform} account")
            return True  # Not a failure — just skipped

        # ── 2. Decrypt password ──
        from pipeline.social_encryption import get_encryption
        enc = get_encryption()
        password = enc.decrypt(acct["encrypted_password"])
        if not password:
            logger.error("[%s] Failed to decrypt %s password", self.slug, platform)
            return False

        # ── 3. Get video metadata ──
        video = self.db.get_video(db_video_id)
        video_title = video.get("titulo_final", "") if video else ""
        yt_url = f"https://youtu.be/{yt_video_id}" if yt_video_id else ""

        # Get script text for caption generation
        script_text = ""
        if action.get("config_json"):
            import json
            try:
                cfg = json.loads(action["config_json"])
                script_text = cfg.get("script_text", "")
            except Exception:
                pass

        # ── 4. Generate caption ──
        from pipeline.social_caption_generator import SocialCaptionGenerator
        cap_gen = SocialCaptionGenerator()
        channel_niche = ""
        try:
            from config.config_bridge import get_channel_config
            config = get_channel_config(self.slug)
            channel_niche = getattr(config, "SEO_PRIMARY_KEYWORD", "")
        except Exception:
            pass

        caption = cap_gen.generate(
            platform=platform,
            script_text=script_text,
            video_title=video_title,
            yt_url=yt_url,
            channel_niche=channel_niche,
        )

        # ── 5. Create social post log ──
        log_id = self.db.create_social_post_log(
            video_id=db_video_id,
            channel_id=ch["id"],
            platform=platform,
            account_id=acct["id"],
            lifecycle_action_id=action["id"],
            caption_text=caption.text[:2000],
            status="publishing",
        )

        # ── 6. Generate clip if needed (TikTok, Instagram) ──
        clip_path = ""
        if platform in ("tiktok", "instagram") and caption.media_ready:
            try:
                from pipeline.social_clip_extractor import SocialClipExtractor
                extractor = SocialClipExtractor()

                # Find the video file path
                video_path = video.get("file_path") if video else None
                if not video_path or not os.path.exists(video_path):
                    # Try to find in output dir
                    from config.settings import OUTPUT_DIR
                    pattern = OUTPUT_DIR / "videos" / self.slug / f"*{db_video_id}*"
                    import glob
                    candidates = glob.glob(str(pattern))
                    if candidates:
                        video_path = candidates[0]

                if video_path and os.path.exists(video_path):
                    clip_output = OUTPUT_DIR / "social_clips" / self.slug
                    os.makedirs(clip_output, exist_ok=True)
                    clip_path = str(clip_output / f"social_{platform}_{db_video_id}.mp4")

                    # Use ffmpeg to extract a 60s clip from the middle
                    import subprocess
                    probe = subprocess.run(
                        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                         "-of", "csv=p=0", video_path],
                        capture_output=True, text=True, timeout=10,
                    )
                    total_duration = float(probe.stdout.strip() or 120)
                    start = max(0, (total_duration / 2) - 30)
                    duration = min(60, total_duration - start)

                    extractor.extract_clip(
                        video_path, start, duration, clip_path,
                        subtitle_text=caption.text[:200],
                    )
                    logger.info("[%s] Generated social clip: %s", self.slug, clip_path)
                else:
                    logger.warning("[%s] No video file found for social clip", self.slug)
            except Exception as exc:
                logger.warning("[%s] Clip generation failed (non-fatal): %s", self.slug, exc)

        # ── 7. Publish via browser ──
        from pipeline.social_browser import BrowserSessionManager
        from pipeline.social_publishers.base import SocialContent, get_publisher

        publish_content = SocialContent(
            platform=platform,
            text=caption.text,
            media_path=clip_path,
            yt_url=yt_url,
            thread_parts=caption.thread_parts,
            hashtags=caption.hashtags,
        )

        try:
            async with BrowserSessionManager() as bsm:
                page = await bsm.new_page()

                # Load existing cookies
                if acct.get("cookies_json"):
                    await bsm.load_cookies(page, acct["cookies_json"])

                publisher = get_publisher(platform)
                post_url = await publisher.publish(page, publish_content)

                if post_url:
                    # Save updated cookies
                    new_cookies = await bsm.save_cookies(page)
                    if new_cookies:
                        self.db.update_social_cookies(acct["id"], new_cookies)

                    # Update social post log
                    self.db.update_social_post_result(
                        log_id, "published", post_url=post_url,
                    )

                    # Update lifecycle action
                    import json
                    self.db.update_lifecycle_action_status(
                        action["id"], "executed",
                        result_json=json.dumps({"post_url": post_url, "platform": platform}),
                    )
                    logger.info("[%s] Published to %s: %s", self.slug, platform, post_url)
                    return True
                else:
                    self.db.update_social_post_result(
                        log_id, "failed", error_message="publish returned no URL",
                    )
                    self.db.update_social_error(acct["id"], "publish returned no URL")
                    return False

        except Exception as exc:
            logger.error("[%s] Browser publish failed for %s: %s", self.slug, platform, exc)
            self.db.update_social_post_result(
                log_id, "failed", error_message=str(exc)[:2000],
            )
            self.db.update_social_error(acct["id"], str(exc)[:1000])
            return False

    # ════════════════════════════════════════════════════════════
    # Manual triggers (called from API endpoints)
    # ════════════════════════════════════════════════════════════

    def trigger_action_manual(self, video_id: int, action_type: str,
                               yt_video_id: str = None,
                               channel_id: int = None) -> dict:
        """Execute a lifecycle action immediately (manual trigger from UI).

        Args:
            video_id: Local DB video ID
            action_type: One of the valid action types
            yt_video_id: YouTube video ID (auto-resolved if None)
            channel_id: Channel DB ID (auto-resolved if None)

        Returns {success: True/False, result: ..., error: ...}
        """
        if not channel_id:
            ch = self.db.get_channel_by_slug(self.slug)
            if ch:
                channel_id = ch["id"]
            else:
                return {"success": False, "error": "Channel not found"}

        if not yt_video_id:
            video = self.db.get_video(video_id)
            if video:
                yt_video_id = video.get("yt_video_id")
            if not yt_video_id:
                return {"success": False, "error": "Video not uploaded to YouTube yet"}

        # Create a temporary action record
        now_iso = datetime.now(timezone.utc).isoformat()
        temp_action = {
            "id": -1,  # Not stored in DB
            "action_type": action_type,
            "yt_video_id": yt_video_id,
            "video_id": video_id,
            "channel_id": channel_id,
            "config_json": None,
        }

        try:
            success = self._dispatch(temp_action)
            return {"success": success}
        except Exception as exc:
            return {"success": False, "error": str(exc)}
