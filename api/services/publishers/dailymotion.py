"""Dailymotion video publisher — API v2.

Dailymotion (https://www.dailymotion.com) allows programmatic upload of full
long-form videos via API v2 (free, OAuth2 client_credentials).

Upload flow (documented in https://developers.dailymotion.com/docs/upload-videos):
1. POST /v2/files/upload_sessions            → {upload_url, progress_url}
2. POST upload_url (multipart, field "file")  → {url: <file_url>, ...}
3. POST /v2/profiles/{profile_id}/videos      → {video_id, ...}
   body: {title, category, visibility, is_for_kids, source: {file_url}}

Credentials stored in channel_social_accounts:
    platform = 'dailymotion'
    username = Dailymotion username
    encrypted_password = JSON: {"client_id": "...", "client_secret": "..."}
"""

import json
import logging
import os
import time

import requests

from api.services.publishers.base import (
    AbstractVideoPublisher,
    VideoMetadata,
    UploadResult,
    register_publisher,
)

logger = logging.getLogger(__name__)

DAILY_API_BASE = "https://api.dailymotion.com/v2"
DAILY_OAUTH_URL = "https://oauth2.dailymotion.com/v2/token"
DAILY_SCOPE = "bundle.publisher"
CHUNK_TIMEOUT = 3600  # 60 minutes for large files


class DailymotionPublisher(AbstractVideoPublisher):
    """Upload full horizontal videos to Dailymotion via API v2."""

    platform = "dailymotion"

    def __init__(self):
        super().__init__()
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._profile_id: str | None = None
        self._channel_id: int | None = None

    # ── Auth ────────────────────────────────────────────────

    def _authenticate(self, channel_id: int) -> bool:
        """Load client credentials, fetch access token + resolve profile_id."""
        if self._access_token and self._channel_id == channel_id:
            if time.time() < self._token_expires_at - 60:
                return True
            # Token expired — refresh
            self._access_token = None

        creds = self._get_credentials(channel_id)
        if not creds:
            logger.warning("[Dailymotion] No enabled account for channel_id=%s", channel_id)
            return False

        decrypted = self._decrypt_password(creds.get("encrypted_password", ""))
        if not decrypted:
            logger.warning("[Dailymotion] Empty credentials for channel_id=%s", channel_id)
            return False
        try:
            cred = json.loads(decrypted)
            cid = cred.get("client_id", "")
            ckey = cred.get("client_secret", "")
        except (ValueError, AttributeError):
            cid, ckey = "", ""

        if not cid or not ckey:
            logger.warning("[Dailymotion] client_id/client_secret missing for channel_id=%s", channel_id)
            return False

        tok = self._fetch_access_token(cid, ckey)
        if not tok:
            return False
        self._access_token = tok
        self._channel_id = channel_id

        profile_id = self._resolve_profile_id()
        if not profile_id:
            logger.warning("[Dailymotion] Could not resolve profile_id for channel_id=%s", channel_id)
            return False
        self._profile_id = profile_id
        return True

    def _fetch_access_token(self, cid: str, ckey: str) -> str | None:
        """POST oauth2 token endpoint → bearer token (30 min expiry)."""
        for attempt in range(self._MAX_RETRIES):
            try:
                resp = requests.post(
                    DAILY_OAUTH_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": cid,
                        "client_secret": ckey,
                        "scope": DAILY_SCOPE,
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._token_expires_at = time.time() + int(data.get("expires_in", 1800))
                    return data.get("access_token", "")
                logger.warning(
                    "[Dailymotion] Token request failed (attempt %d): HTTP %d — %s",
                    attempt + 1, resp.status_code, resp.text[:200],
                )
                if resp.status_code in (400, 401, 403):
                    break  # bad credentials — no retry
            except Exception as exc:
                logger.warning("[Dailymotion] Token error (attempt %d): %s", attempt + 1, exc)
            if attempt < self._MAX_RETRIES - 1:
                time.sleep(self._RETRY_BASE_DELAY * (2 ** attempt))
        return None

    def _resolve_profile_id(self) -> str | None:
        """GET /v2/me → first manageable profile id."""
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            resp = requests.get(
                f"{DAILY_API_BASE}/me",
                params={"fields": "id,username,profiles"},
                headers=headers,
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                profiles = data.get("profiles") or []
                if isinstance(profiles, list) and profiles:
                    first = profiles[0]
                    if isinstance(first, dict):
                        return first.get("profile_id") or first.get("id")
                    return str(first)
                return data.get("id")
            logger.warning("[Dailymotion] /me failed: HTTP %d — %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("[Dailymotion] /me error: %s", exc)
        return None

    # ── Upload ──────────────────────────────────────────────

    async def upload(self, metadata: VideoMetadata,
                     progress_cb=None) -> UploadResult:
        """Upload a full video to Dailymotion."""
        channel_id = getattr(metadata, "channel_id", None) or self._channel_id or 0
        if not self._authenticate(channel_id):
            return UploadResult(
                success=False, platform="dailymotion",
                error="Not authenticated — no valid client credentials",
            )

        video_path = metadata.video_path
        if not os.path.exists(video_path):
            return UploadResult(
                success=False, platform="dailymotion",
                error=f"Video file not found: {video_path}",
            )

        file_size = os.path.getsize(video_path)
        logger.info("[Dailymotion] Starting upload: %s (%d MB) → profile %s",
                     os.path.basename(video_path), file_size // (1024 * 1024),
                     self._profile_id)

        try:
            # Step 1: create upload session
            upload_url = self._create_upload_session()
            if not upload_url:
                return UploadResult(
                    success=False, platform="dailymotion",
                    error="Failed to create upload session",
                )

            # Step 2: upload binary (multipart)
            file_url = self._upload_binary(upload_url, video_path, progress_cb)
            if not file_url:
                return UploadResult(
                    success=False, platform="dailymotion",
                    error="Binary upload to Dailymotion failed",
                )

            # Step 3: create + publish video record
            video_id = self._create_video(metadata, file_url)
            if not video_id:
                return UploadResult(
                    success=False, platform="dailymotion",
                    error="Video file uploaded but record creation failed",
                )

            video_url = f"https://www.dailymotion.com/video/{video_id}"
            return UploadResult(
                success=True, platform="dailymotion",
                platform_video_id=video_id,
                platform_video_url=video_url,
                status="processing",
            )

        except requests.exceptions.Timeout:
            return UploadResult(
                success=False, platform="dailymotion",
                error="Upload timed out",
            )
        except requests.exceptions.ConnectionError as exc:
            return UploadResult(
                success=False, platform="dailymotion",
                error=f"Connection error: {exc}",
            )
        except Exception as exc:
            logger.exception("[Dailymotion] Upload failed")
            return UploadResult(
                success=False, platform="dailymotion",
                error=f"Upload failed: {exc}",
            )

    def _create_upload_session(self) -> str | None:
        """POST /v2/files/upload_sessions → upload_url."""
        headers = {"Authorization": f"Bearer {self._access_token}"}
        for attempt in range(self._MAX_RETRIES):
            try:
                resp = requests.post(
                    f"{DAILY_API_BASE}/files/upload_sessions",
                    headers=headers, timeout=30,
                )
                if resp.status_code in (200, 201):
                    return resp.json().get("upload_url", "")
                logger.warning(
                    "[Dailymotion] Create session failed (attempt %d): HTTP %d — %s",
                    attempt + 1, resp.status_code, resp.text[:200],
                )
                if resp.status_code in (401, 403):
                    break
            except Exception as exc:
                logger.warning("[Dailymotion] Session error (attempt %d): %s", attempt + 1, exc)
            if attempt < self._MAX_RETRIES - 1:
                time.sleep(self._RETRY_BASE_DELAY * (2 ** attempt))
        return None

    def _upload_binary(self, upload_url: str, video_path: str,
                       progress_cb=None) -> str | None:
        """POST the file to upload_url (multipart field 'file')."""
        try:
            with open(video_path, "rb") as f:
                resp = requests.post(
                    upload_url,
                    files={"file": (os.path.basename(video_path), f, "video/mp4")},
                    timeout=CHUNK_TIMEOUT,
                )
            if resp.status_code in (200, 201):
                data = resp.json()
                if progress_cb:
                    try:
                        progress_cb(100)
                    except Exception:
                        pass
                return data.get("url", "")
            logger.warning("[Dailymotion] Binary upload failed: HTTP %d — %s",
                           resp.status_code, resp.text[:300])
        except Exception as exc:
            logger.warning("[Dailymotion] Binary upload error: %s", exc)
        return None

    def _create_video(self, metadata: VideoMetadata, file_url: str) -> str | None:
        """POST /v2/profiles/{profile_id}/videos with mandatory fields."""
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "title": metadata.title[:255],
            "description": self._build_description(metadata)[:3000],
            "category": "entertainment",
            "visibility": "public" if metadata.privacy != "private" else "private",
            "is_for_kids": False,
            "is_ai_altered": True,
            "source": {"file_url": file_url},
        }
        if metadata.tags:
            payload["tags"] = metadata.tags[:30]
        if getattr(metadata, "language", "es") == "es":
            payload["language"] = "es"

        for attempt in range(self._MAX_RETRIES):
            try:
                resp = requests.post(
                    f"{DAILY_API_BASE}/profiles/{self._profile_id}/videos",
                    json=payload, headers=headers, timeout=60,
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    vid = data.get("video_id") or data.get("id")
                    if vid:
                        logger.info("[Dailymotion] Video created: %s", vid)
                        return vid
                    logger.warning("[Dailymotion] No video_id in response: %s", data)
                logger.warning(
                    "[Dailymotion] Create video failed (attempt %d): HTTP %d — %s",
                    attempt + 1, resp.status_code, resp.text[:300],
                )
                if resp.status_code in (401, 403):
                    break
            except Exception as exc:
                logger.warning("[Dailymotion] Create video error (attempt %d): %s", attempt + 1, exc)
            if attempt < self._MAX_RETRIES - 1:
                time.sleep(self._RETRY_BASE_DELAY * (2 ** attempt))
        return None

    # ── Helpers ──────────────────────────────────────────────

    def _build_description(self, metadata: VideoMetadata) -> str:
        desc = metadata.description or ""
        if metadata.cross_reference_yt and metadata.yt_video_url:
            desc += (
                f"\n\n——\n"
                f"📺 Mira también en YouTube: {metadata.yt_video_url}\n"
                f"Síguenos para más contenido como este."
            )
        return desc[:3000]

    async def validate(self, channel_id: int) -> dict:
        """Valida client_id/client_secret: autentica + GET /v2/me (200 = válido)."""
        if not self._authenticate(channel_id):
            return {"ok": False, "message": "Credenciales inválidas o incompletas"}
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            resp = requests.get(
                f"{DAILY_API_BASE}/me",
                headers=headers, timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {"ok": True, "message": f"API válida — usuario {data.get('username', '')}"}
            return {"ok": False, "message": f"GET /me HTTP {resp.status_code}"}
        except Exception as exc:
            return {"ok": False, "message": f"Error al validar: {exc}"}

    async def get_status(self, platform_video_id: str) -> dict:
        """GET /v2/videos/{id} → processing/encoding status."""
        if not self._access_token:
            return {"status": "unknown"}
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            resp = requests.get(
                f"{DAILY_API_BASE}/videos/{platform_video_id}",
                params={"fields": "id,status,encoding_status,encoding_progress,visibility"},
                headers=headers, timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status") or data.get("encoding_status") or "processing"
                return {"status": status, **data}
        except Exception:
            pass
        return {"status": "unknown"}

    async def get_stats(self, platform_video_id: str) -> dict:
        """GET /v2/videos/{id} with public counters (views/likes/comments)."""
        if not self._access_token:
            return {}
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            resp = requests.get(
                f"{DAILY_API_BASE}/videos/{platform_video_id}",
                params={"fields": "id,views_total,ratings_total,comments_total"},
                headers=headers, timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "views": int(data.get("views_total", 0) or 0),
                    "likes": int(data.get("ratings_total", 0) or 0),
                    "comments": int(data.get("comments_total", 0) or 0),
                }
        except Exception as exc:
            logger.debug("[Dailymotion] get_stats error: %s", exc)
        return {}


# ── Auto-register ─────────────────────────────────────────────

register_publisher("dailymotion", DailymotionPublisher())
