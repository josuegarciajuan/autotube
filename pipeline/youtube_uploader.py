"""YouTube Data API v3 uploader with OAuth2 token management.

Multi-channel aware: each channel uses its own token file and loads
configuration via config_bridge instead of hardcoded imports.

Supports:
- Headless OAuth via run_console() (prints URL → user pastes code)
- Per-channel proxy (SOCKS5 / HTTP) for residential IP routing
- Resumable upload with exponential backoff
- Full metadata: title, description, tags, category, thumbnail,
  language, embeddable, public stats
"""

import logging
import os
import pickle
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from config.settings import GOOGLE_CLIENT_SECRET_PATH, TOKENS_DIR

# ── Quota tracking (passive diagnostic — no behavioral change) ──
from api.services.quota_tracker import track_quota
from config.settings import PROXY_ENABLED, PROXY_TYPE, PROXY_HOST, PROXY_PORT, PROXY_CHANNELS

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
MAX_RETRIES = 5
RETRY_BASE_DELAY = 2  # seconds
POST_UPLOAD_VERIFY_RETRIES = 3
POST_UPLOAD_VERIFY_DELAY = 5  # seconds — YouTube processing may take a few secs


class QuotaExhaustedError(RuntimeError):
    """YouTube API daily quota exhausted. Raised when quotaExceeded is detected.
    
    Caught by orchestrator, upload scheduler, and generation service to:
    - Auto-pause the scheduler (scheduler_paused=true in system_state)
    - Create a pipeline_alert in the monitoring dashboard
    - Keep videos in awaiting_upload without consuming retry attempts
    """
    pass

# ── Token file concurrency protection ────────────────────────
# Two threads sharing the same channel token must not race on
# read → refresh → write.  A per-account mutex serialises all
# token I/O for a given pickle file.
_TOKEN_LOCKS: dict[str, threading.Lock] = {}
_TOKEN_LOCKS_GUARD = threading.Lock()
_TOKEN_REFRESH_COOLDOWN = 5  # seconds — if file was saved <5s ago, re-read instead of deleting


class YouTubeUploader:
    """Upload videos to YouTube via the Data API v3."""

    def __init__(
        self,
        account_name: str = "default",
        db: "Database | None" = None,  # noqa: F821
        channel_slug: Optional[str] = None,
    ):
        self.account_name = account_name
        self.db = db
        self.channel_slug = channel_slug
        self._token_path = TOKENS_DIR / f"{account_name}.pickle"
        self._credentials: Credentials | None = None
        self._service: Any = None
        self._config: Any = None

    # ── Client secret per channel ─────────────────────────────

    @property
    def _client_secret_path(self) -> Path:
        """Get the client secret file path for this channel.
        
        Priority: config/client_secret_{slug}.json → config/client_secret.json
        """
        if self.channel_slug:
            channel_specific = Path(GOOGLE_CLIENT_SECRET_PATH).parent / f"client_secret_{self.channel_slug}.json"
            if channel_specific.exists():
                return channel_specific
        return Path(GOOGLE_CLIENT_SECRET_PATH)

    # ── Config (lazy-loaded via bridge) ───────────────────────

    @property
    def config(self):
        if self._config is None and self.channel_slug:
            from config.config_bridge import get_channel_config

            self._config = get_channel_config(self.channel_slug)
        return self._config

    def _get_config_attr(self, name: str, fallback: Any = None) -> Any:
        """Get a config attribute, falling back gracefully."""
        if self.config:
            return getattr(self.config, name, fallback)
        return fallback

    def _get_token_lock(self) -> threading.Lock:
        """Obtain (and possibly create) the mutex for this account's token file."""
        with _TOKEN_LOCKS_GUARD:
            lock = _TOKEN_LOCKS.get(self.account_name)
            if lock is None:
                lock = threading.Lock()
                _TOKEN_LOCKS[self.account_name] = lock
            return lock

    def _safe_unlink(self) -> None:
        """Delete the token file, but only if no other thread just saved fresh credentials.

        If the file was modified less than _TOKEN_REFRESH_COOLDOWN seconds ago,
        another thread likely just refreshed the token — re-read it instead of
        destroying it.
        """
        if not self._token_path.exists():
            return
        try:
            mtime = self._token_path.stat().st_mtime
            if time.time() - mtime < _TOKEN_REFRESH_COOLDOWN:
                logger.warning(
                    "Token file %s was modified %.1fs ago — re-reading instead of deleting",
                    self._token_path, time.time() - mtime,
                )
                with open(self._token_path, "rb") as f:
                    self._credentials = pickle.load(f)
                return
        except (OSError, pickle.UnpicklingError, EOFError) as exc:
            logger.warning("Safe-unlink re-read failed (%s) — will delete", exc)
        self._token_path.unlink(missing_ok=True)

    # ── Proxy support ─────────────────────────────────────────

    def _should_use_proxy(self) -> bool:
        """Determine if this channel should use a proxy."""
        if not PROXY_ENABLED:
            return False
        if not PROXY_CHANNELS:
            return True  # empty = all channels
        return self.channel_slug in PROXY_CHANNELS

    def _configure_proxy(self, http) -> None:
        """Inject proxy settings into an httplib2.Http instance."""
        from api.services.proxy_manager import configure_proxy_for_http

        configure_proxy_for_http(http, PROXY_TYPE, PROXY_HOST, PROXY_PORT)
        logger.info(
            "Proxy enabled for channel %s: %s://%s:%s",
            self.channel_slug, PROXY_TYPE, PROXY_HOST, PROXY_PORT,
        )

    # ── Authentication ──────────────────────────────────────────

    def authenticate(self) -> bool:
        """Authenticate with OAuth2 console flow (headless-safe).

        1. Load pickle token if exists
        2. Refresh if expired
        3. If missing: run InstalledAppFlow.run_console()
           (prints a URL → user opens in browser → pastes code)
        4. Save refreshed token

        Returns True on success.

        Serialised per-account via a threading.Lock to prevent two
        threads from racing on the same pickle file.
        """
        lock = self._get_token_lock()
        with lock:
            self._credentials = None

            if self._token_path.exists():
                try:
                    with open(self._token_path, "rb") as f:
                        self._credentials = pickle.load(f)
                    logger.info("Loaded credentials from %s", self._token_path)
                except (pickle.UnpicklingError, EOFError, TypeError) as exc:
                    logger.warning(
                        "Corrupted token file: %s — will re-authenticate.", exc
                    )
                    self._safe_unlink()
                    self._credentials = None

            if self._credentials and self._credentials.expired:
                if self._credentials.refresh_token:
                    try:
                        self._credentials.refresh(Request())
                        logger.info("Token refreshed successfully.")
                    except Exception as exc:
                        logger.warning("Token refresh failed: %s", exc)
                        self._safe_unlink()
                        self._credentials = None
                else:
                    logger.warning("No refresh token; re-authentication required.")
                    self._safe_unlink()
                    self._credentials = None

            if not self._credentials or not self._credentials.valid:
                client_secret = self._client_secret_path
                if not client_secret.exists():
                    logger.error(
                        "Google client secret not found at %s", client_secret
                    )
                    return False

                flow = InstalledAppFlow.from_client_secrets_file(
                    str(client_secret), SCOPES
                )
                flow.redirect_uri = "http://localhost"
                
                # Headless approach: print URL, user authorises, copies code from redirect
                auth_url, _ = flow.authorization_url(
                    access_type="offline",
                    prompt="consent",
                    include_granted_scopes="true",
                )
                
                # Save flow state so user can complete auth separately
                import json as _json
                state_path = TOKENS_DIR / f"{self.account_name}_state.json"
                state_path.write_text(_json.dumps({
                    "client_secret_path": str(client_secret),
                    "scopes": SCOPES,
                    "code_verifier": getattr(flow, 'code_verifier', None),
                }))
                
                print("\n" + "=" * 60)
                print("🔐 ABRE ESTA URL EN TU NAVEGADOR PARA AUTORIZAR:")
                print("=" * 60)
                print(auth_url)
                print("=" * 60)
                print()
                print("Después de autorizar, el navegador intentará cargar 'localhost'.")
                print("ES NORMAL que dé error de conexión.")
                print()
                print("👉 COPIA el código de la barra de direcciones:")
                print("   Busca 'code=' en la URL y copia todo hasta '&'")
                print("   Ej: http://localhost/?code=4/0AanRRr...&scope=...")
                print("   Necesitas: 4/0AanRRr...")
                print()
                print("📋 Luego ejecuta en el server:")
                print(f"   python3 scripts/complete_auth.py {slug} 'EL_CODIGO'")
                return False  # needs manual code completion

        self._save_credentials()
        self._service = None  # force rebuild with fresh credentials
        return True

    def get_auth_url(self) -> Optional[str]:
        """Generate OAuth authorization URL (for web-based auth flow).
        
        Returns the URL the user must open, or None on failure.
        Saves PKCE code_verifier to state file so complete_auth_with_code() can resume.
        """
        client_secret = self._client_secret_path
        if not client_secret.exists():
            logger.error("Google client secret not found at %s", client_secret)
            return None

        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret), SCOPES,
        )
        flow.redirect_uri = "http://localhost"
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )

        # Save PKCE state so complete_auth_with_code() can use the same code_verifier
        import json as _json
        state_path = TOKENS_DIR / f"{self.account_name}_state.json"
        state_path.write_text(_json.dumps({
            "client_secret_path": str(client_secret),
            "scopes": SCOPES,
            "code_verifier": getattr(flow, 'code_verifier', None),
        }))

        return auth_url

    def complete_auth_with_code(self, code: str) -> bool:
        """Complete OAuth flow with authorization code (web-based auth).
        
        Args:
            code: The authorization code from Google's redirect.
        
        Returns True on success.
        """
        client_secret = self._client_secret_path
        if not client_secret.exists():
            logger.error("Google client secret not found")
            return False

        # Restore PKCE code_verifier saved by get_auth_url()
        import json as _json
        state_path = TOKENS_DIR / f"{self.account_name}_state.json"
        code_verifier = None
        if state_path.exists():
            try:
                state_data = _json.loads(state_path.read_text())
                code_verifier = state_data.get("code_verifier")
            except Exception as exc:
                logger.warning("Could not read state file: %s", exc)

        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secret), SCOPES
            )
            flow.redirect_uri = "http://localhost"
            if code_verifier:
                flow.code_verifier = code_verifier
            flow.fetch_token(code=code)
            self._credentials = flow.credentials
            self._save_credentials()

            # Clean up state file on success
            if state_path.exists():
                state_path.unlink()

            self._service = None
            logger.info("Web-based OAuth completed for %s", self.account_name)
            return True
        except Exception as exc:
            logger.error("OAuth code exchange failed: %s", exc)
            return False

    def check_auth_status(self) -> dict:
        """Check current authentication status."""
        if self._token_path.exists():
            try:
                with open(self._token_path, "rb") as f:
                    creds = pickle.load(f)
                if creds and creds.valid:
                    return {"authenticated": True, "status": "valid"}
                if creds and creds.expired and creds.refresh_token:
                    return {"authenticated": True, "status": "expired", "can_refresh": True}
                return {"authenticated": False, "status": "invalid"}
            except Exception:
                pass
        return {"authenticated": False, "status": "no_token"}

    def _save_credentials(self) -> None:
        if self._credentials is None:
            return
        TOKENS_DIR.mkdir(parents=True, exist_ok=True)
        with open(self._token_path, "wb") as f:
            pickle.dump(self._credentials, f)
            f.flush()
            os.fsync(f.fileno())  # ensure full write to disk before releasing lock
        logger.info("Token saved to %s", self._token_path)

    def _get_service(self) -> Any:
        """Build authenticated YouTube API service (with optional proxy)."""
        if self._service is None:
            if self._credentials is None or not self._credentials.valid:
                if not self.authenticate():
                    raise RuntimeError("Failed to authenticate with YouTube API")

            import httplib2

            # Build with credentials (proxy will be injected if needed)
            kwargs = {"credentials": self._credentials, "cache_discovery": False}
            
            if self._should_use_proxy():
                http = self._credentials.authorize(httplib2.Http())
                self._configure_proxy(http)
                kwargs["http"] = http

            self._service = build("youtube", "v3", **kwargs)
        return self._service

    # ── Upload ──────────────────────────────────────────────────

    def upload(
        self,
        video_path: Path,
        title: str,
        description: str,
        tags: list[str] | None = None,
        thumbnail_path: Path | None = None,
        category_id: str = "22",
        privacy: str = "public",
        language: str = "es",
        heartbeat_callback=None,
        progress_callback=None,
        suggested_video_filename: str = None,
        suggested_thumb_filename: str = None,
        publish_at: str = None,
    ) -> dict:
        """Upload video to YouTube.

        - Resumable upload with 256KB chunks
        - Sets snippet (title, desc, tags, category, language) + status (privacy)
        - Sets custom thumbnail via thumbnails().set()
        - Quota: 1600 upload + 50 thumbnail = 1650 units
        - heartbeat_callback: optional callable invoked between upload chunks
          to signal the orphan detector that the upload is still alive.
        - progress_callback: optional callable(pct: int) invoked on each chunk
          with the upload progress percentage (0-100).
        - suggested_video_filename: if provided, the video file is copied to a
          temp file with this name (preserving .mp4 extension) before upload.
          YouTube uses the filename for SEO — a keyword-rich stem helps ranking.
        - suggested_thumb_filename: if provided, the thumbnail is copied to a
          temp file with this name (preserving .jpg extension) before setting.
        - publish_at: ISO 8601 datetime (UTC) for scheduled publishing.
          When set, privacy is forced to "private" (YouTube requirement)
          and YouTube will auto-publish at the specified time.
          e.g. "2026-07-31T19:00:00.000Z"

        Returns {video_id: str, url: str, warnings: list}
        """
        video_path = Path(video_path)
        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")

        service = self._get_service()

        # ── SEO-friendly temp copies ────────────────────────────
        # YouTube's processing pipeline uses the uploaded file name as
        # a ranking signal.  We copy the originals to temp files with
        # keyword-rich names, upload those, and clean up afterward.
        # The original files are never modified — other phases may
        # still depend on their canonical paths.
        _tmp_video = None
        _tmp_thumb = None
        _cleanup_paths = []
        try:
            if suggested_video_filename:
                # Preserve extension (.mp4, .mov, etc.)
                ext = video_path.suffix
                _tmp_video = video_path.with_name(
                    f"{suggested_video_filename}{ext}"
                )
                shutil.copy2(video_path, _tmp_video)
                _cleanup_paths.append(_tmp_video)
                logger.info(
                    "SEO copy: %s → %s (uploading renamed)", video_path.name, _tmp_video.name
                )
                video_path = _tmp_video

            if suggested_thumb_filename and thumbnail_path:
                thumb_path = Path(thumbnail_path)
                if str(thumb_path).strip() and thumb_path.is_file():
                    ext = thumb_path.suffix or ".jpg"
                    _tmp_thumb = thumb_path.with_name(
                        f"{suggested_thumb_filename}{ext}"
                    )
                    shutil.copy2(thumb_path, _tmp_thumb)
                    _cleanup_paths.append(_tmp_thumb)
                    thumbnail_path = _tmp_thumb

            default_tags = self._get_config_attr("YT_DEFAULT_TAGS", [])
            tags = tags or default_tags
            
            # Sanitize tags: YouTube rejects tags with certain characters
            sanitized = []
            for tag in tags:
                # Remove quotes, newlines, special chars
                clean = str(tag).replace('"', '').replace("'", '').replace('\n', '').strip()
                if clean and len(clean) > 0 and len(clean) <= 30:
                    sanitized.append(clean)
            tags = sanitized[:60]  # Max 60 tags per YouTube

            body = {
                "snippet": {
                    "title": title[:100],
                    "description": description[:5000],
                    "tags": tags,
                    "categoryId": category_id,
                    "defaultLanguage": language,
                    "defaultAudioLanguage": language,
                },
                "status": {
                    "privacyStatus": "private" if publish_at else privacy,
                    "selfDeclaredMadeForKids": False,
                    "embeddable": True,
                    "publicStatsViewable": True,
                },
            }

            # ── Scheduled publishing via YouTube API (publishAt) ──
            if publish_at:
                body["status"]["publishAt"] = publish_at
                logger.info(
                    "Scheduled publish: video will auto-publish at %s UTC",
                    publish_at,
                )

            media = MediaFileUpload(
                str(video_path),
                mimetype="video/*",
                chunksize=256 * 1024,
                resumable=True,
            )

            request = service.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )

            logger.info("Uploading: %s (privacy=%s)", title, privacy)
            response = self._resumable_upload(request, heartbeat_callback=heartbeat_callback,
                                              progress_callback=progress_callback)

            # ── Validate YouTube response ─────────────────────────
            self._validate_upload_response(response)

            video_id: str = response["id"]

            # ── Track quota (diagnostic) ──────────────────────────
            track_quota(self.channel_slug, "videos.insert", 1600,
                        yt_id=video_id, caller="YouTubeUploader.upload")
            youtube_url = f"https://www.youtube.com/watch?v={video_id}"
            logger.info("Upload complete: %s", youtube_url)

            # ── Post-upload verification: confirm video exists on YouTube ──
            self._verify_upload_exists(service, video_id)

            # ── Build warnings for non-uploadable fields ─────────
            warnings = []
            warnings.append({
                "field": "subtitles",
                "reason": "Subtítulos requieren API aparte (captions.insert). Subir manualmente en YouTube Studio.",
                "ready": False,
            })
            warnings.append({
                "field": "playlist",
                "reason": "Añadir a playlist manualmente en YouTube Studio o vía playlistItems().insert().",
                "ready": False,
            })
            if thumbnail_path and Path(thumbnail_path).exists():
                if not self._set_thumbnail(service, video_id, Path(thumbnail_path)):
                    warnings.append({
                        "type": "thumbnail",
                        "field": "thumbnail",
                        "reason": "Thumbnail upload failed or could not be verified",
                        "ready": False,
                        "retry_needed": True,
                    })

            if self.db is not None:
                self._log_to_db(video_path, title, video_id, youtube_url)

            return {"video_id": video_id, "url": youtube_url, "warnings": warnings}

        finally:
            # ── Clean up SEO temp copies ───────────────────────
            for p in _cleanup_paths:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass

    def upload_from_script(
        self,
        video_path: Path,
        script: dict,
        thumbnail_path: Path | None = None,
        privacy: str = "public",
    ) -> dict:
        """Convenience: upload with metadata from script JSON.

        Expected keys: titulo, descripcion, tags, category_id, etc.
        """
        title = script.get("titulo", "Untitled")
        description = self._format_description(
            titulo=script.get("titulo", ""),
            descripcion=script.get("descripcion", ""),
        )
        tags = script.get("tags") or self._get_config_attr("YT_DEFAULT_TAGS", [])
        category = script.get("category_id", self._get_config_attr("YT_CATEGORY_ID", "22"))

        return self.upload(
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            thumbnail_path=thumbnail_path,
            category_id=category,
            privacy=privacy,
        )

    def update_description(self, video_id: str, description: str) -> dict:
        """Update the description of an existing YouTube video.

        Uses videos().list() to fetch current title (1 unit) + update() (50 units)
        because the YouTube API requires `title` on any snippet update.

        Returns {updated: True, yt_video_id: str} or raises HttpError.
        """
        service = self._get_service()

        # Fetch current title — YouTube API requires `title` on snippet updates
        # and rejects the request with HTTP 400 if title is missing or empty.
        list_response = service.videos().list(
            part="snippet",
            id=video_id,
        ).execute()

        # ── Track quota (diagnostic) ──────────────────────────────
        track_quota(self.channel_slug, "videos.list", 1,
                    yt_id=video_id, caller="update_description.fetch_title")

        items = list_response.get("items", [])
        if not items:
            raise ValueError(f"Video {video_id} not found or not accessible "
                             f"(channel: {self.channel_slug})")

        current_title = items[0]["snippet"]["title"]

        category_id = self._get_config_attr("YT_CATEGORY_ID", "22")
        body = {
            "id": video_id,
            "snippet": {
                "title": current_title,
                "description": description[:5000],
                "categoryId": category_id,
            },
        }

        service.videos().update(
            part="snippet",
            body=body,
        ).execute()

        # ── Track quota (diagnostic) ──────────────────────────────
        track_quota(self.channel_slug, "videos.update", 50,
                    yt_id=video_id, caller="update_description")

        logger.info("[%s] Description updated for video %s", self.channel_slug, video_id)
        return {"updated": True, "yt_video_id": video_id}

    def set_privacy(self, video_id: str, privacy: str) -> dict:
        """Update the privacy status of an existing YouTube video.

        Valid privacy values: 'public', 'unlisted', 'private'.
        Uses videos().update() — quota cost: 50 units.

        Returns {updated: True, yt_video_id: str, privacy: str} or raises HttpError.
        """
        if privacy not in ("public", "unlisted", "private"):
            raise ValueError(f"Invalid privacy status: {privacy}. Must be 'public', 'unlisted', or 'private'.")

        service = self._get_service()

        body = {
            "id": video_id,
            "status": {
                "privacyStatus": privacy,
            },
        }

        service.videos().update(
            part="status",
            body=body,
        ).execute()

        # ── Track quota (diagnostic) ──────────────────────────────
        track_quota(self.channel_slug, "videos.update", 50,
                    yt_id=video_id, caller="set_privacy")

        logger.info("[%s] Privacy set to %s for video %s", self.channel_slug, privacy, video_id)
        return {"updated": True, "yt_video_id": video_id, "privacy": privacy}

    # ── Thumbnail ───────────────────────────────────────────────

    def _set_thumbnail(
        self, service: Any, video_id: str, thumbnail_path: Path
    ) -> bool:
        """Set custom thumbnail for a YouTube video. Returns True on success.

        v24 (Aug 2026): Now verifies the thumbnail was actually applied by
        calling thumbnails().list() after set(). Returns False instead of
        silently swallowing errors, so callers can retry or log properly.
        """
        try:
            service.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(
                    str(thumbnail_path),
                    mimetype="image/jpeg",
                ),
            ).execute()
            logger.info("Thumbnail uploaded for video %s", video_id)

            # ── Track quota (diagnostic) ──────────────────────────
            track_quota(self.channel_slug, "thumbnails.set", 50,
                        yt_id=video_id, caller="_set_thumbnail")

            # Verify it was actually applied (1 quota unit)
            try:
                verify_resp = service.thumbnails().list(
                    videoId=video_id,
                ).execute()
                track_quota(self.channel_slug, "thumbnails.list", 1,
                            yt_id=video_id, caller="_set_thumbnail.verify")
                items = verify_resp.get("items", [])
                if items:
                    logger.info("Thumbnail verified for video %s (%d item(s))",
                                video_id, len(items))
                    return True
                else:
                    logger.warning(
                        "Thumbnail set() succeeded but list() returned no items for %s",
                        video_id,
                    )
                    return False
            except HttpError as verify_exc:
                logger.warning(
                    "Thumbnail set() succeeded but verification failed for %s: %s",
                    video_id, verify_exc,
                )
                return False  # uncertain — assume failure

        except HttpError as exc:
            reason = str(exc)[:200]
            if "youtube.com/verify" in reason.lower() or "phone" in reason.lower():
                logger.warning(
                    "Thumbnail upload skipped for %s — account may need phone "
                    "verification at youtube.com/verify. Error: %s", video_id, exc,
                )
            else:
                logger.warning("Thumbnail upload failed for %s: %s", video_id, exc)
            return False

    # ── Upload validation ───────────────────────────────────────

    @staticmethod
    def _validate_upload_response(response: dict) -> None:
        """Validate the YouTube API upload response before declaring success.
        
        Checks:
        - uploadStatus is 'uploaded' or 'processed'
        - No rejectionReason present
        - Video ID exists and is valid
        
        Raises RuntimeError with a clear message if validation fails.
        """
        status = response.get("status", {})
        upload_status = status.get("uploadStatus", "")
        rejection_reason = status.get("rejectionReason", "")
        failure_reason = status.get("failureReason", "")
        video_id = response.get("id", "")
        
        if not video_id:
            raise RuntimeError("YouTube no devolvió un video ID. La subida falló silenciosamente.")
        
        if failure_reason:
            raise RuntimeError(
                f"YouTube rechazó el vídeo durante el procesamiento: {failure_reason}. "
                f"Motivo: {failure_reason}"
            )
        
        if rejection_reason:
            raise RuntimeError(
                f"YouTube rechazó el vídeo: {rejection_reason}. "
                f"Revisa los lineamientos de contenido en youtube.com."
            )
        
        if upload_status not in ("uploaded", "processed"):
            raise RuntimeError(
                f"Estado de subida inesperado: '{upload_status}'. "
                f"El vídeo puede no estar disponible. Revisa YouTube Studio."
            )
    
    def _verify_upload_exists(self, service: Any, video_id: str) -> None:
        """Post-upload verification: confirm the video actually exists on YouTube.
        
        Retries up to POST_UPLOAD_VERIFY_RETRIES with delay because
        YouTube may take a few seconds to process the video.
        
        Detects:
        - 'Deleted video' (YouTube removed it silently)
        - Video not found (API returns empty)
        - Upload still processing (retries)
        
        Raises RuntimeError on permanent failures.
        """
        for attempt in range(1, POST_UPLOAD_VERIFY_RETRIES + 1):
            try:
                resp = service.videos().list(
                    part="status,snippet", id=video_id
                ).execute()

                # ── Track quota (diagnostic) ──────────────────────────
                track_quota(self.channel_slug, "videos.list", 1,
                            yt_id=video_id, caller="_verify_upload_exists")

                items = resp.get("items", [])
                
                if not items:
                    if attempt < POST_UPLOAD_VERIFY_RETRIES:
                        logger.info(
                            "Post-upload verification attempt %d/%d: video not yet available, retrying...",
                            attempt, POST_UPLOAD_VERIFY_RETRIES,
                        )
                        time.sleep(POST_UPLOAD_VERIFY_DELAY)
                        continue
                    raise RuntimeError(
                        f"Fallo en verificación post-subida: el vídeo {video_id} no aparece en YouTube. "
                        f"Posiblemente fue eliminado por los sistemas automatizados de YouTube."
                    )
                
                item = items[0]
                snippet = item.get("snippet", {})
                status = item.get("status", {})
                
                title = snippet.get("title", "")
                upload_status = status.get("uploadStatus", "")
                rejection = status.get("rejectionReason", "")
                processing_status = status.get("processingStatus", "")
                processing_failure = status.get("processingFailureReason", "")
                
                # Detect silently deleted videos
                if title == "Deleted video" or (not title and not snippet.get("description")):
                    raise RuntimeError(
                        f"Vídeo subido pero ELIMINADO por YouTube: {video_id}. "
                        f"YouTube lo marcó como 'Deleted video'. "
                        f"Posibles causas: contenido generado por IA detectado, "
                        f"política de contenido, o cuenta nueva con restricciones. "
                        f"Revisa YouTube Studio para más detalles."
                    )
                
                if rejection:
                    raise RuntimeError(
                        f"Vídeo rechazado por YouTube: {rejection}. "
                        f"Revisa los lineamientos de contenido."
                    )
                
                # ── Check processing status (encoding may fail AFTER upload succeeds) ──
                if processing_status == "failed":
                    raise RuntimeError(
                        f"YouTube processing FAILED for video {video_id}: "
                        f"{processing_failure or 'unknown reason'}. "
                        f"El video se subió pero YouTube no pudo procesarlo (encoding error). "
                        f"Se reintentará automáticamente."
                    )
                
                if processing_status == "suspended":
                    raise RuntimeError(
                        f"YouTube processing SUSPENDED for video {video_id}: "
                        f"posible violación de políticas o detección de contenido IA. "
                        f"Revisar YouTube Studio manualmente."
                    )
                
                if upload_status == "processed":
                    logger.info(
                        "Post-upload verification OK: video %s (status=%s, processing=%s, title=%s)",
                        video_id, upload_status, processing_status, title[:60],
                    )
                    return
                
                if upload_status == "uploaded":
                    # ── If processing hasn't started yet, give more time ──
                    if not processing_status and attempt < POST_UPLOAD_VERIFY_RETRIES:
                        extended_delay = POST_UPLOAD_VERIFY_DELAY * 2
                        logger.info(
                            "Post-upload verification attempt %d/%d: uploaded but processing not started yet, "
                            "waiting %ds...",
                            attempt, POST_UPLOAD_VERIFY_RETRIES, extended_delay,
                        )
                        time.sleep(extended_delay)
                        continue
                    
                    logger.info(
                        "Post-upload verification: video %s accepted (processing=%s).",
                        video_id, processing_status or "pending",
                    )
                    return
                
                if attempt < POST_UPLOAD_VERIFY_RETRIES:
                    logger.info(
                        "Post-upload verification attempt %d/%d: status=%s, retrying...",
                        attempt, POST_UPLOAD_VERIFY_RETRIES, upload_status,
                    )
                    time.sleep(POST_UPLOAD_VERIFY_DELAY)
                    continue
                
                logger.warning(
                    "Post-upload verification: video %s has unexpected status '%s'. "
                    "Proceeding but manual verification recommended.",
                    video_id, upload_status,
                )
                return
                
            except HttpError as exc:
                # Quota exceeded / access errors: log warning and skip verification
                if exc.resp.status in (403, 429):
                    logger.warning(
                        "Post-upload verification skipped (HTTP %s, likely quota): %s",
                        exc.resp.status, str(exc)[:200],
                    )
                    return  # Upload succeeded, verification unavailable — proceed
                if attempt < POST_UPLOAD_VERIFY_RETRIES:
                    logger.info(
                        "Post-upload verification attempt %d/%d: HTTP %s, retrying...",
                        attempt, POST_UPLOAD_VERIFY_RETRIES, exc.resp.status,
                    )
                    time.sleep(POST_UPLOAD_VERIFY_DELAY)
                    continue
                raise RuntimeError(
                    f"Fallo en verificación post-subida: error HTTP {exc.resp.status}: {exc}"
                ) from exc
        
        raise RuntimeError(
            f"No se pudo verificar el vídeo {video_id} tras {POST_UPLOAD_VERIFY_RETRIES} intentos. "
            f"Revisa YouTube Studio manualmente."
        )

    # ── Helpers ─────────────────────────────────────────────────

    def _resumable_upload(self, request: Any, heartbeat_callback=None,
                           progress_callback=None) -> dict:
        response = None
        error_count = 0
        consecutive_errors = 0
        last_reported_pct = -1
        
        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    logger.info("Upload progress: %d%%", pct)
                    # ── Propagate progress to external callback ──
                    if progress_callback and pct != last_reported_pct:
                        last_reported_pct = pct
                        try:
                            progress_callback(pct)
                        except Exception:
                            pass
                if response is not None:
                    return response
                consecutive_errors = 0  # reset on successful chunk
                # ── Heartbeat: signal orphan detector that upload is alive ──
                if heartbeat_callback:
                    try:
                        heartbeat_callback()
                    except Exception:
                        pass
            except HttpError as exc:
                consecutive_errors += 1
                if exc.resp.status in (403, 401):
                    # Distinguish between auth issues and YouTube-specific errors
                    error_content = b""
                    try:
                        error_content = exc.content if hasattr(exc, 'content') else b""
                    except Exception:
                        pass
                    error_reason = ""
                    try:
                        import json as _json
                        error_data = _json.loads(error_content) if error_content else {}
                        error_reason = error_data.get("error", {}).get("errors", [{}])[0].get("reason", "")
                    except Exception:
                        pass
                    if error_reason == "uploadLimitExceeded":
                        raise RuntimeError(
                            "YouTube rechazó el vídeo: la cuenta NO está verificada y el vídeo supera 15 minutos. "
                            "Verifica la cuenta en youtube.com/verify."
                        ) from exc
                    if error_reason == "youtubeSignupRequired":
                        raise RuntimeError(
                            "La cuenta de YouTube requiere registro adicional (youtube.com/create_channel)."
                        ) from exc
                    if error_reason == "quotaExceeded":
                        # ── Auto-pause scheduler + create monitoring alert ──
                        try:
                            from database.db_extended import ExtendedDatabase
                            _qdb = ExtendedDatabase()
                            _qdb.set_quota_exhausted(channel_slug=getattr(self, 'canal', ''))
                        except Exception:
                            pass
                        try:
                            from api.services.lifecycle_monitor import create_alert
                            from database.db_extended import ExtendedDatabase as _E2
                            _adb = _E2()
                            create_alert(_adb,
                                         entity_type='system', entity_id=None, channel_id=None,
                                         alert_type='quota_exhausted', severity='critical',
                                         title='YouTube API quota agotada',
                                         message='Cuota diaria de YouTube API agotada (10,000 unidades). '
                                                 'El scheduler se ha pausado automáticamente. '
                                                 'Se reanudará en 6 horas. Puedes reanudar manualmente '
                                                 'desde el panel de monitorización.',
                                         metadata={'channel': getattr(self, 'canal', 'unknown')})
                        except Exception:
                            pass
                        raise QuotaExhaustedError(
                            "Cuota diaria de YouTube API agotada (10,000 unidades). Reintentar mañana."
                        ) from exc
                    logger.error("Auth/permission error (%s): %s", error_reason or "unknown", exc)
                    raise
                if exc.resp.status in (500, 502, 503, 504):
                    if consecutive_errors >= MAX_RETRIES:
                        raise RuntimeError(
                            f"Upload failed after {MAX_RETRIES} consecutive server errors: {exc}"
                        )
                    delay = RETRY_BASE_DELAY * (2 ** consecutive_errors)
                    logger.warning(
                        "Server error %s — retrying in %ds (error %d/%d)",
                        exc.resp.status, delay, consecutive_errors, MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue
                if exc.resp.status == 400:
                    # Parse YouTube-specific error reason for bad requests
                    error_content = b""
                    try:
                        error_content = exc.content if hasattr(exc, 'content') else b""
                    except Exception:
                        pass
                    error_reason = ""
                    try:
                        import json as _json
                        error_data = _json.loads(error_content) if error_content else {}
                        error_reason = error_data.get("error", {}).get("errors", [{}])[0].get("reason", "")
                    except Exception:
                        pass
                    if error_reason == "uploadLimitExceeded":
                        raise RuntimeError(
                            "YouTube rechazó el vídeo: supera el límite de duración permitido. "
                            "Verifica la cuenta en youtube.com/verify para subir vídeos de más de 15 min."
                        ) from exc
                    if error_reason == "invalidTitle":
                        raise RuntimeError(
                            f"Título inválido: {exc}"
                        ) from exc
                    raise RuntimeError(f"YouTube rechazó la subida (400 {error_reason}): {exc}") from exc
                raise
            except (OSError, ConnectionError) as exc:
                consecutive_errors += 1
                if consecutive_errors >= MAX_RETRIES:
                    raise RuntimeError(
                        f"Upload failed after {MAX_RETRIES} consecutive network errors: {exc}"
                    )
                delay = RETRY_BASE_DELAY * (2 ** consecutive_errors)
                logger.warning(
                    "Network error: %s — retrying in %ds (error %d/%d)",
                    exc, delay, consecutive_errors, MAX_RETRIES,
                )
                time.sleep(delay)

        raise RuntimeError("Upload loop exited unexpectedly")

    def _format_description(self, titulo: str, descripcion: str = "") -> str:
        """Format description using channel's DESCRIPTION_TEMPLATE.
        
        Falls back to a simple template if config isn't loaded.
        """
        template = self._get_config_attr("DESCRIPTION_TEMPLATE")

        if template:
            return template.format(titulo=titulo, descripcion=descripcion)

        # Fallback template
        return f"""{titulo}

{descripcion}

#HistoriasReales #Documental"""

    def _log_to_db(
        self, video_path: Path, title: str, video_id: str, youtube_url: str
    ) -> None:
        """Log upload event to the pipeline_log table ONLY.

        IMPORTANT: Do NOT insert video records here — that is the
        orchestrator's / API layer's responsibility. Inserting here
        creates DB duplicates (1 per upload) because the orchestrator
        or API layer already manages the video record.

        This method exists for audit trail purposes (CLI standalone mode
        still gets a pipeline_log entry for the upload event).
        """
        if self.db is None:
            return
        try:
            canal_name = self._get_config_attr("CANAL_NAME", self.channel_slug or "unknown")
            self.db.log_pipeline(
                canal=canal_name,
                phase="upload",
                status="success",
                message=f"Uploaded {title} → {youtube_url}",
            )
        except Exception as exc:
            logger.error("Failed to log upload to database: %s", exc)
