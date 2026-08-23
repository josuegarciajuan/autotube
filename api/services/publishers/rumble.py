"""Rumble video publisher — Upload API.

Rumble (https://rumble.com) pays creators through:
- Rumble Player: revenue share on embedded player views
- Licensing: viral content licensed to Yahoo, MSN, etc. (upfront + rev share)
- No minimum subscriber requirements to monetize

Upload flow:
1. POST /upload/init → get upload_url + video_id
2. PUT upload_url with video binary (streaming chunked)
3. POST /upload/complete → finalize with metadata
4. Poll until status = "published"

Credentials stored in channel_social_accounts:
    platform = 'rumble'
    username = Rumble channel name
    encrypted_password = API key / upload token
"""

import asyncio
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

RUMBLE_API_BASE = "https://rumble.com/api/v1"
CHUNK_SIZE = 256 * 1024  # 256 KB
UPLOAD_TIMEOUT = 1800     # 30 minutes max


class RumblePublisher(AbstractVideoPublisher):
    """Upload full videos to Rumble via their Upload API."""

    platform = "rumble"

    def __init__(self):
        super().__init__()
        self._api_key: str | None = None
        self._channel_slug: str | None = None

    # ── Auth ────────────────────────────────────────────────

    def _authenticate(self, channel_id: int) -> bool:
        """Load and validate API key from channel_social_accounts."""
        if self._api_key:
            return True
        creds = self._get_credentials(channel_id)
        if not creds:
            logger.warning("[Rumble] No enabled account for channel_id=%s", channel_id)
            return False
        decrypted = self._decrypt_password(creds.get("encrypted_password", ""))
        if not decrypted:
            logger.warning("[Rumble] Empty API key for channel_id=%s", channel_id)
            return False
        self._api_key = decrypted
        self._channel_id = channel_id
        logger.info("[Rumble] Authenticated for channel_id=%s", channel_id)
        return True

    # ── Upload ──────────────────────────────────────────────

    async def upload(self, metadata: VideoMetadata,
                     progress_cb=None) -> UploadResult:
        """Upload a video to Rumble.

        Steps:
          1. Initialize upload → get upload_url + video_id
          2. PUT binary data to upload_url (chunked)
          3. Complete upload with metadata
          4. Wait for processing
        """
        if not self._api_key:
            return UploadResult(
                success=False, platform="rumble",
                error="Not authenticated — no API key loaded",
            )

        video_path = metadata.video_path
        if not os.path.exists(video_path):
            return UploadResult(
                success=False, platform="rumble",
                error=f"Video file not found: {video_path}",
            )

        file_size = os.path.getsize(video_path)
        logger.info("[Rumble] Starting upload: %s (%d MB)",
                     os.path.basename(video_path), file_size // (1024 * 1024))

        try:
            # Step 1: Initialize upload
            init_result = self._init_upload(metadata, file_size)
            if not init_result:
                return UploadResult(
                    success=False, platform="rumble",
                    error="Failed to initialize upload",
                )

            upload_url = init_result.get("upload_url", "")
            rumble_video_id = init_result.get("video_id", "")

            if not upload_url or not rumble_video_id:
                return UploadResult(
                    success=False, platform="rumble",
                    error=f"Invalid init response: {init_result}",
                )

            # Step 2: Upload binary
            self._upload_binary(upload_url, video_path, file_size, progress_cb)

            # Step 3: Complete upload with metadata
            self._complete_upload(rumble_video_id, metadata)

            # Step 4: Wait for processing (poll)
            status = await self._wait_for_processing(rumble_video_id)

            if status == "published":
                video_url = f"https://rumble.com/v{rumble_video_id}"
                return UploadResult(
                    success=True, platform="rumble",
                    platform_video_id=rumble_video_id,
                    platform_video_url=video_url,
                    status="published",
                )
            else:
                return UploadResult(
                    success=False, platform="rumble",
                    platform_video_id=rumble_video_id,
                    status=status,
                    error=f"Video stuck in status: {status}",
                )

        except requests.exceptions.Timeout:
            return UploadResult(
                success=False, platform="rumble",
                error="Upload timed out after 30 min",
            )
        except requests.exceptions.ConnectionError as exc:
            return UploadResult(
                success=False, platform="rumble",
                error=f"Connection error: {exc}",
            )
        except Exception as exc:
            logger.exception("[Rumble] Upload failed")
            return UploadResult(
                success=False, platform="rumble",
                error=f"Upload failed: {exc}",
            )

    def _init_upload(self, metadata: VideoMetadata, file_size: int) -> dict | None:
        """Initialize a Rumble upload session."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "title": metadata.title,
            "description": self._build_description(metadata),
            "file_size": file_size,
            "privacy": metadata.privacy if metadata.privacy != "scheduled" else "public",
        }
        for attempt in range(self._MAX_RETRIES):
            try:
                resp = requests.post(
                    f"{RUMBLE_API_BASE}/upload/init",
                    json=payload, headers=headers, timeout=30,
                )
                if resp.status_code == 200:
                    return resp.json()
                logger.warning(
                    "[Rumble] Init failed (attempt %d/%d): HTTP %d — %s",
                    attempt + 1, self._MAX_RETRIES, resp.status_code, resp.text[:200],
                )
                if resp.status_code in (401, 403):
                    break  # auth error, don't retry
            except Exception as exc:
                logger.warning("[Rumble] Init error (attempt %d): %s", attempt + 1, exc)
            if attempt < self._MAX_RETRIES - 1:
                time.sleep(self._RETRY_BASE_DELAY * (2 ** attempt))
        return None

    def _upload_binary(self, upload_url: str, video_path: str,
                       file_size: int, progress_cb=None) -> None:
        """Upload the video binary via PUT with chunked streaming."""
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(file_size),
        }
        uploaded = 0
        last_pct = -1

        with open(video_path, "rb") as f:
            # Generator-based streaming to avoid loading entire file in RAM
            def read_chunks():
                nonlocal uploaded, last_pct
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    uploaded += len(chunk)
                    pct = int(uploaded * 100 / file_size)
                    if pct != last_pct:
                        last_pct = pct
                        if progress_cb:
                            try:
                                progress_cb(pct)
                            except Exception:
                                pass
                    yield chunk

            resp = requests.put(
                upload_url,
                data=read_chunks(),
                headers=headers,
                timeout=UPLOAD_TIMEOUT,
            )

        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"Binary upload failed: HTTP {resp.status_code} — {resp.text[:200]}")

        logger.info("[Rumble] Binary upload complete: %d bytes", file_size)

    def _complete_upload(self, rumble_video_id: str, metadata: VideoMetadata) -> dict:
        """Finalize the upload with metadata."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "video_id": rumble_video_id,
            "title": metadata.title,
            "description": self._build_description(metadata),
            "tags": metadata.tags[:20] if metadata.tags else [],
        }
        resp = requests.post(
            f"{RUMBLE_API_BASE}/upload/complete",
            json=payload, headers=headers, timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Complete upload failed: HTTP {resp.status_code}")
        return resp.json()

    async def _wait_for_processing(self, rumble_video_id: str,
                                   max_wait_min: int = 30) -> str:
        """Poll Rumble until video is published or timeout."""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        start = time.time()
        max_wait = max_wait_min * 60

        while time.time() - start < max_wait:
            await asyncio.sleep(30)
            try:
                resp = requests.get(
                    f"{RUMBLE_API_BASE}/upload/status/{rumble_video_id}",
                    headers=headers, timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get("status", "processing")
                    logger.debug("[Rumble] Video %s status: %s", rumble_video_id, status)
                    if status == "published":
                        return "published"
                    if status == "failed":
                        return "failed"
            except Exception as exc:
                logger.warning("[Rumble] Status poll error: %s", exc)

        logger.warning("[Rumble] Processing timeout for video %s", rumble_video_id)
        return "processing"

    # ── Helpers ──────────────────────────────────────────────

    def _build_description(self, metadata: VideoMetadata) -> str:
        """Build description, optionally appending YouTube link."""
        desc = metadata.description or ""
        if metadata.cross_reference_yt and metadata.yt_video_url:
            desc += f"\n\n——\n📺 Video original en YouTube: {metadata.yt_video_url}"
        return desc

    async def validate(self, channel_id: int) -> dict:
        """Valida la API key: carga y hace una llamada autenticada ligera.

        Rumble no expone /me; usamos /upload/status con un id dummy:
        401/403 → key inválida; cualquier otra respuesta → auth OK.
        """
        if not self._authenticate(channel_id):
            return {"ok": False, "message": "API key no configurada o vacía"}
        try:
            resp = requests.get(
                f"{RUMBLE_API_BASE}/upload/status/validate-test",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=15,
            )
            if resp.status_code in (401, 403):
                return {"ok": False, "message": f"API key rechazada (HTTP {resp.status_code})"}
            return {"ok": True, "message": "API key válida"}
        except Exception as exc:
            return {"ok": False, "message": f"Error al validar: {exc}"}

    async def get_status(self, platform_video_id: str) -> dict:
        """Get current Rumble video status."""
        if not self._api_key:
            return {"status": "unknown"}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            resp = requests.get(
                f"{RUMBLE_API_BASE}/upload/status/{platform_video_id}",
                headers=headers, timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {"status": "unknown"}


# ── Auto-register ─────────────────────────────────────────────

register_publisher("rumble", RumblePublisher())
