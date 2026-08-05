"""Facebook video publisher — Graph API v18+.

Monetization opportunities:
- In-Stream Ads: 55% revenue share on videos >3min (10k followers + 600k min/year)
- Reels Bonuses: invitation-based, pays for performance
- Stars: viewer tips during live/on-demand
- Subscriptions: recurring monthly revenue

Upload flow:
1. Resolve page access token from channel_social_accounts
2. Init resumable upload session (POST /{page-id}/videos)
3. Stream video in 256KB chunks with progress reporting
4. Verify upload completion + processing status
5. For Reels: use /{page-id}/video_reels endpoint

Credentials stored in channel_social_accounts:
    platform = 'facebook'
    username = Facebook page name
    encrypted_password = page access token (long-lived, obtained via /me/accounts)
    cookies_json = user session (for token refresh if needed)

Reference: https://developers.facebook.com/docs/video-api/guides/publishing/
"""

import logging
import os
import time
from typing import Optional

import requests

from api.services.publishers.base import (
    AbstractVideoPublisher,
    VideoMetadata,
    UploadResult,
    register_publisher,
)

logger = logging.getLogger(__name__)

FACEBOOK_GRAPH_URL = "https://graph.facebook.com/v18.0"
FACEBOOK_VIDEO_URL = "https://graph-video.facebook.com/v18.0"
CHUNK_SIZE = 256 * 1024  # 256 KB
UPLOAD_TIMEOUT = 3600     # 60 minutes for large files


class FacebookVideoPublisher(AbstractVideoPublisher):
    """Upload full videos to Facebook Page via Graph API.

    Supports:
    - Long-form video (In-Stream Ads eligible, >= 3 min)
    - Reels (short vertical video)
    - Scheduled publishing
    - Custom thumbnail
    """

    platform = "facebook"

    def __init__(self):
        super().__init__()
        self._page_token: str | None = None
        self._page_id: str | None = None
        self._channel_id: int | None = None

    # ── Auth ────────────────────────────────────────────────

    def _authenticate(self, channel_id: int) -> bool:
        """Load page access token from channel_social_accounts."""
        if self._page_token and self._channel_id == channel_id:
            return True

        creds = self._get_credentials(channel_id)
        if not creds:
            logger.warning("[Facebook] No enabled account for channel_id=%s", channel_id)
            return False

        decrypted = self._decrypt_password(creds.get("encrypted_password", ""))
        if not decrypted:
            logger.warning("[Facebook] Empty page token for channel_id=%s", channel_id)
            return False

        self._page_token = decrypted
        self._channel_id = channel_id

        # Resolve page_id from token
        page_info = self._get_page_info()
        if not page_info:
            logger.warning("[Facebook] Could not resolve page_id from token")
            return False

        self._page_id = page_info.get("id", "")
        logger.info(
            "[Facebook] Authenticated: page=%s (id=%s)",
            page_info.get("name", "unknown"), self._page_id,
        )
        return True

    def _get_page_info(self) -> dict | None:
        """GET /me/accounts → find the page associated with this token.

        If the stored token is already a page token, GET /me directly
        to verify and get the page name + id.
        """
        try:
            resp = requests.get(
                f"{FACEBOOK_GRAPH_URL}/me",
                params={"access_token": self._page_token, "fields": "id,name"},
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning("[Facebook] Token validation failed: HTTP %d", resp.status_code)
        except Exception as exc:
            logger.warning("[Facebook] Token validation error: %s", exc)
        return None

    def _validate_token(self) -> bool:
        """Check if the current page token is still valid."""
        if not self._page_token:
            return False
        try:
            resp = requests.get(
                f"{FACEBOOK_GRAPH_URL}/me",
                params={"access_token": self._page_token},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    # ── Upload ──────────────────────────────────────────────

    async def upload(self, metadata: VideoMetadata,
                     progress_cb=None) -> UploadResult:
        """Upload a video to Facebook Page via resumable upload.

        Uses Facebook's Resumable Upload Protocol:
        https://developers.facebook.com/docs/graph-api/video-uploads
        """
        if not self._authenticate(metadata.channel_id if hasattr(metadata, 'channel_id') else self._channel_id or 0):
            return UploadResult(
                success=False, platform="facebook",
                error="Not authenticated — no valid page token",
            )

        video_path = metadata.video_path
        if not os.path.exists(video_path):
            return UploadResult(
                success=False, platform="facebook",
                error=f"Video file not found: {video_path}",
            )

        file_size = os.path.getsize(video_path)
        logger.info("[Facebook] Starting upload: %s (%d MB) → page %s",
                     os.path.basename(video_path), file_size // (1024 * 1024),
                     self._page_id)

        try:
            # Step 1: Init upload session
            session_id, upload_url = self._init_upload_session(file_size)
            if not session_id:
                return UploadResult(
                    success=False, platform="facebook",
                    error="Failed to initialize upload session",
                )

            # Step 2: Upload binary chunks
            self._upload_chunks(upload_url, video_path, file_size, progress_cb)

            # Step 3: Finalize with metadata
            fb_video_id = self._finalize_upload(session_id, metadata)
            if not fb_video_id:
                return UploadResult(
                    success=False, platform="facebook",
                    error="Upload transfer succeeded but finalization failed",
                )

            # Step 4: Attach thumbnail if provided
            if metadata.thumbnail_path and os.path.exists(metadata.thumbnail_path):
                self._attach_thumbnail(fb_video_id, metadata.thumbnail_path)

            video_url = f"https://www.facebook.com/{self._page_id}/videos/{fb_video_id}"
            return UploadResult(
                success=True, platform="facebook",
                platform_video_id=fb_video_id,
                platform_video_url=video_url,
                status="published",
            )

        except requests.exceptions.Timeout:
            return UploadResult(
                success=False, platform="facebook",
                error="Upload timed out",
            )
        except requests.exceptions.ConnectionError as exc:
            return UploadResult(
                success=False, platform="facebook",
                error=f"Connection error: {exc}",
            )
        except Exception as exc:
            logger.exception("[Facebook] Upload failed")
            return UploadResult(
                success=False, platform="facebook",
                error=f"Upload failed: {exc}",
            )

    def _init_upload_session(self, file_size: int) -> tuple[str | None, str | None]:
        """Init a Facebook resumable upload session.

        POST /{page-id}/videos?upload_phase=start&file_size=...
        Returns (session_id, upload_url).
        """
        params = {
            "access_token": self._page_token,
            "upload_phase": "start",
            "file_size": file_size,
        }
        for attempt in range(self._MAX_RETRIES):
            try:
                resp = requests.post(
                    f"{FACEBOOK_GRAPH_URL}/{self._page_id}/videos",
                    params=params,
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    session_id = data.get("upload_session_id") or data.get("video", {}).get("id")
                    if not session_id:
                        # Initial create may return video_id directly for small files
                        vid = data.get("id") or (data.get("video", {}) if isinstance(data.get("video"), dict) else {}).get("id")
                        if vid:
                            # Small file — upload complete in one go
                            logger.info("[Facebook] Small file upload — got video_id directly")
                            return vid, None
                        logger.warning("[Facebook] No session_id in response: %s", data)
                        return None, None
                    return session_id, data.get("upload_url") or f"{FACEBOOK_VIDEO_URL}/{self._page_id}/videos"
                logger.warning(
                    "[Facebook] Init session failed (attempt %d): HTTP %d — %s",
                    attempt + 1, resp.status_code, resp.text[:200],
                )
                if resp.status_code in (401, 403):
                    break
            except Exception as exc:
                logger.warning("[Facebook] Init error (attempt %d): %s", attempt + 1, exc)
            if attempt < self._MAX_RETRIES - 1:
                time.sleep(self._RETRY_BASE_DELAY * (2 ** attempt))
        return None, None

    def _upload_chunks(self, upload_url: str | None, video_path: str,
                       file_size: int, progress_cb=None) -> None:
        """Upload video data in chunks.

        If upload_url is None, the session_id IS the video_id (small file),
        and we upload via POST with upload_phase=transfer.
        """
        if upload_url is None:
            # Small file — already created, skip chunk upload
            logger.info("[Facebook] Small file — skipping chunk upload phase")
            return

        headers = {"Content-Type": "application/octet-stream"}
        uploaded = 0
        last_pct = -1

        with open(video_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                offset = uploaded
                uploaded += len(chunk)
                was_last = uploaded >= file_size

                params = {
                    "access_token": self._page_token,
                    "upload_phase": "transfer",
                    "start_offset": offset,
                }
                for attempt in range(self._MAX_RETRIES):
                    try:
                        resp = requests.post(
                            upload_url,
                            params=params,
                            data=chunk,
                            headers={**headers, "Content-Length": str(len(chunk))},
                            timeout=600,
                        )
                        if resp.status_code == 200:
                            break
                        logger.warning(
                            "[Facebook] Chunk offset=%d failed (attempt %d): HTTP %d",
                            offset, attempt + 1, resp.status_code,
                        )
                        if attempt < self._MAX_RETRIES - 1:
                            time.sleep(self._RETRY_BASE_DELAY * (2 ** attempt))
                            f.seek(offset)  # reset position
                    except Exception as exc:
                        logger.warning("[Facebook] Chunk error (attempt %d): %s", attempt + 1, exc)
                        if attempt < self._MAX_RETRIES - 1:
                            time.sleep(self._RETRY_BASE_DELAY * (2 ** attempt))
                            f.seek(offset)

                pct = int(uploaded * 100 / file_size)
                if pct != last_pct:
                    last_pct = pct
                    if progress_cb:
                        try:
                            progress_cb(pct)
                        except Exception:
                            pass

        logger.info("[Facebook] Binary upload complete: %d bytes", file_size)

    def _finalize_upload(self, session_id: str, metadata: VideoMetadata) -> str | None:
        """Finalize the upload with metadata.

        POST /{page-id}/videos?upload_phase=finish&upload_session_id=...
        """
        description = self._build_description(metadata)
        params = {
            "access_token": self._page_token,
            "upload_phase": "finish",
            "upload_session_id": session_id,
            "title": metadata.title,
            "description": description,
        }
        # Add optional fields
        if metadata.tags:
            params["tags"] = ",".join(metadata.tags[:30])
        if metadata.privacy:
            params["published"] = "true" if metadata.privacy == "public" else "false"
        if metadata.schedule_at:
            params["scheduled_publish_time"] = int(
                time.mktime(time.strptime(metadata.schedule_at, "%Y-%m-%dT%H:%M:%S"))
            )
            params["published"] = "false"

        for attempt in range(self._MAX_RETRIES):
            try:
                resp = requests.post(
                    f"{FACEBOOK_GRAPH_URL}/{self._page_id}/videos",
                    params=params,
                    timeout=120,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    vid = data.get("id") or data.get("video_id")
                    if vid:
                        logger.info("[Facebook] Upload finalized: video_id=%s", vid)
                        return vid
                    logger.warning("[Facebook] No video_id in finalize response: %s", data)
                logger.warning(
                    "[Facebook] Finalize failed (attempt %d): HTTP %d — %s",
                    attempt + 1, resp.status_code, resp.text[:200],
                )
            except Exception as exc:
                logger.warning("[Facebook] Finalize error (attempt %d): %s", attempt + 1, exc)
            if attempt < self._MAX_RETRIES - 1:
                time.sleep(self._RETRY_BASE_DELAY * (2 ** attempt))
        return None

    def _attach_thumbnail(self, fb_video_id: str, thumbnail_path: str) -> bool:
        """Upload a custom thumbnail for the video."""
        try:
            with open(thumbnail_path, "rb") as f:
                resp = requests.post(
                    f"{FACEBOOK_GRAPH_URL}/{fb_video_id}/thumbnails",
                    params={"access_token": self._page_token},
                    files={"source": (os.path.basename(thumbnail_path), f, "image/jpeg")},
                    timeout=60,
                )
            if resp.status_code == 200:
                logger.info("[Facebook] Thumbnail attached to %s", fb_video_id)
                return True
            logger.warning("[Facebook] Thumbnail upload failed: HTTP %d", resp.status_code)
        except Exception as exc:
            logger.warning("[Facebook] Thumbnail error: %s", exc)
        return False

    # ── Reels ────────────────────────────────────────────────

    async def upload_reel(self, metadata: VideoMetadata,
                          progress_cb=None) -> UploadResult:
        """Upload a short vertical video as a Facebook Reel.

        POST /{page-id}/video_reels
        """
        if not self._authenticate(self._channel_id or 0):
            return UploadResult(
                success=False, platform="facebook",
                error="Not authenticated",
            )

        video_path = metadata.video_path
        if not os.path.exists(video_path):
            return UploadResult(
                success=False, platform="facebook",
                error=f"Video file not found: {video_path}",
            )

        description = metadata.description or metadata.title
        if metadata.cross_reference_yt and metadata.yt_video_url:
            description += f"\n\nVideo completo en YouTube: {metadata.yt_video_url}"

        try:
            with open(video_path, "rb") as f:
                resp = requests.post(
                    f"{FACEBOOK_GRAPH_URL}/{self._page_id}/video_reels",
                    params={
                        "access_token": self._page_token,
                        "description": description[:2200],
                    },
                    files={"source": (os.path.basename(video_path), f, "video/mp4")},
                    timeout=UPLOAD_TIMEOUT,
                )
            if resp.status_code == 200:
                data = resp.json()
                reel_id = data.get("id") or data.get("video_id")
                return UploadResult(
                    success=True, platform="facebook",
                    platform_video_id=reel_id,
                    platform_video_url=f"https://www.facebook.com/reel/{reel_id}",
                    status="published",
                )
            return UploadResult(
                success=False, platform="facebook",
                error=f"Reel upload failed: HTTP {resp.status_code} — {resp.text[:200]}",
            )
        except Exception as exc:
            return UploadResult(
                success=False, platform="facebook",
                error=f"Reel upload error: {exc}",
            )

    # ── Helpers ──────────────────────────────────────────────

    def _build_description(self, metadata: VideoMetadata) -> str:
        """Build description with optional YouTube cross-reference."""
        desc = metadata.description or ""
        if metadata.cross_reference_yt and metadata.yt_video_url:
            desc += (
                f"\n\n——\n"
                f"📺 Mira también en YouTube: {metadata.yt_video_url}\n"
                f"Síguenos para más contenido como este."
            )
        return desc[:63206]  # Facebook description limit

    async def get_status(self, platform_video_id: str) -> dict:
        """Get Facebook video processing status."""
        if not self._page_token:
            return {"status": "unknown"}
        try:
            resp = requests.get(
                f"{FACEBOOK_GRAPH_URL}/{platform_video_id}",
                params={
                    "access_token": self._page_token,
                    "fields": "status,permalink_url,created_time,length",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {"status": "unknown"}

    async def update_metadata(self, platform_video_id: str,
                              title: str = None, description: str = None,
                              tags: list[str] = None) -> bool:
        """Update video metadata via Graph API."""
        if not self._page_token:
            return False
        params = {"access_token": self._page_token}
        if title:
            params["title"] = title
        if description:
            params["description"] = description[:63206]
        if tags:
            params["tags"] = ",".join(tags[:30])
        if len(params) <= 1:
            return False
        try:
            resp = requests.post(
                f"{FACEBOOK_GRAPH_URL}/{platform_video_id}",
                params=params, timeout=30,
            )
            return resp.status_code == 200
        except Exception:
            return False


# ── Auto-register ─────────────────────────────────────────────

register_publisher("facebook", FacebookVideoPublisher())
