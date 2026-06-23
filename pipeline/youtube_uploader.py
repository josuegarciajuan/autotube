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
from config.settings import PROXY_ENABLED, PROXY_TYPE, PROXY_HOST, PROXY_PORT, PROXY_CHANNELS

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
MAX_RETRIES = 5
RETRY_BASE_DELAY = 2  # seconds


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
        """
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
                self._token_path.unlink(missing_ok=True)
                self._credentials = None

        if self._credentials and self._credentials.expired:
            if self._credentials.refresh_token:
                try:
                    self._credentials.refresh(Request())
                    logger.info("Token refreshed successfully.")
                except Exception as exc:
                    logger.warning("Token refresh failed: %s", exc)
                    self._token_path.unlink(missing_ok=True)
                    self._credentials = None
            else:
                logger.warning("No refresh token; re-authentication required.")
                self._token_path.unlink(missing_ok=True)
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
            print(f"   python3 scripts/complete_auth.py canal2 'EL_CODIGO'")
            return False  # needs manual code completion

        self._save_credentials()
        self._service = None  # force rebuild with fresh credentials
        return True

    def get_auth_url(self) -> Optional[str]:
        """Generate OAuth authorization URL (for web-based auth flow).
        
        Returns the URL the user must open, or None on failure.
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

        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secret), SCOPES
            )
            flow.fetch_token(code=code)
            self._credentials = flow.credentials
            self._save_credentials()
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
        privacy: str = "unlisted",
        language: str = "es",
    ) -> dict:
        """Upload video to YouTube.

        - Resumable upload with 256KB chunks
        - Sets snippet (title, desc, tags, category, language) + status (privacy)
        - Sets custom thumbnail via thumbnails().set()
        - Quota: 1600 upload + 50 thumbnail = 1650 units

        Returns {video_id: str, url: str, warnings: list}
        """
        video_path = Path(video_path)
        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")

        service = self._get_service()

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
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
                "embeddable": True,
                "publicStatsViewable": True,
            },
        }

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
        response = self._resumable_upload(request)

        video_id: str = response["id"]
        youtube_url = f"https://www.youtube.com/watch?v={video_id}"
        logger.info("Upload complete: %s", youtube_url)

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
        warnings.append({
            "field": "end_screens",
            "reason": "Pantallas finales solo configurables en YouTube Studio.",
            "ready": False,
        })

        if thumbnail_path and Path(thumbnail_path).exists():
            self._set_thumbnail(service, video_id, Path(thumbnail_path))

        if self.db is not None:
            self._log_to_db(video_path, title, video_id, youtube_url)

        return {"video_id": video_id, "url": youtube_url, "warnings": warnings}

    def upload_from_script(
        self,
        video_path: Path,
        script: dict,
        thumbnail_path: Path | None = None,
        privacy: str = "unlisted",
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

    # ── Thumbnail ───────────────────────────────────────────────

    def _set_thumbnail(
        self, service: Any, video_id: str, thumbnail_path: Path
    ) -> None:
        try:
            service.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(
                    str(thumbnail_path),
                    mimetype="image/jpeg",
                ),
            ).execute()
            logger.info("Thumbnail set for video %s", video_id)
        except HttpError as exc:
            logger.error("Failed to set thumbnail: %s", exc)

    # ── Helpers ─────────────────────────────────────────────────

    def _resumable_upload(self, request: Any) -> dict:
        response = None
        error_count = 0
        consecutive_errors = 0
        
        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    logger.info("Upload progress: %d%%", pct)
                if response is not None:
                    return response
                consecutive_errors = 0  # reset on successful chunk
            except HttpError as exc:
                consecutive_errors += 1
                if exc.resp.status in (403, 401):
                    logger.error("Auth/permission error: %s", exc)
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

    @staticmethod
    def _format_description(titulo: str, descripcion: str = "") -> str:
        """Format description using channel's DESCRIPTION_TEMPLATE.
        
        Falls back to a simple template if config isn't loaded.
        """
        # Try channel-specific template via the instance config
        import inspect
        frame = inspect.currentframe()
        # Walk up to find the instance (caller's self)
        instance = None
        try:
            caller_frame = frame.f_back if frame else None
            if caller_frame:
                instance = caller_frame.f_locals.get("self")
        finally:
            del frame

        template = None
        if instance and hasattr(instance, "_get_config_attr"):
            template = instance._get_config_attr("DESCRIPTION_TEMPLATE")

        if template:
            return template.format(titulo=titulo, descripcion=descripcion)

        # Fallback template
        return f"""{titulo}

{descripcion}

#HistoriasReales #Documental"""

    def _log_to_db(
        self, video_path: Path, title: str, video_id: str, youtube_url: str
    ) -> None:
        if self.db is None:
            return
        try:
            canal_name = self._get_config_attr("CANAL_NAME", self.channel_slug or "unknown")
            privacy_status = self._get_config_attr("YT_PRIVACY_STATUS", "unlisted")
            video_db_id = self.db.insert_video(
                script_id=None,
                canal=canal_name,
                video_path=str(video_path),
                titulo_final=title,
                privacy_status=privacy_status,
            )
            self.db.mark_video_uploaded(video_db_id, video_id, youtube_url)
            self.db.log_pipeline(
                canal=canal_name,
                phase="upload",
                status="success",
                message=f"Uploaded {title} → {youtube_url}",
            )
        except Exception as exc:
            logger.error("Failed to log upload to database: %s", exc)
