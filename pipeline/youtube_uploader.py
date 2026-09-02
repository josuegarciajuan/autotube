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
import zlib
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
POST_UPLOAD_VERIFY_RETRIES = 4
POST_UPLOAD_VERIFY_DELAY = 10  # seconds — YouTube processing may take a few secs


class QuotaExhaustedError(RuntimeError):
    """YouTube API daily quota exhausted. Raised when quotaExceeded is detected.
    
    Caught by orchestrator, upload scheduler, and generation service to:
    - Auto-pause the scheduler (scheduler_paused=true in system_state)
    - Create a pipeline_alert in the monitoring dashboard
    - Keep videos in awaiting_upload without consuming retry attempts
    """
    pass


class UploadAdmissionDeniedError(RuntimeError):
    """The local quota dispatcher denied admission for a NON-quota reason
    (reference collision, invalid budget, unknown project, remediation mode).

    Unlike QuotaExhaustedError, this does NOT mean the YouTube daily quota is
    exhausted: raising this must NOT trip the per-project quota circuit breaker
    nor create a "quota agotada" alert. Callers keep the video retryable
    (awaiting_upload) without pausing the project.
    """
    pass


class AccountDailyCapExceededError(UploadAdmissionDeniedError):
    """La cuenta Google alcanzó su cap diario de subidas (antiban, ago 2026).

    Se trata como una denegación de admisión local (retryable, sin impacto de
    cuota ni señal de spam): el vídeo se conserva en awaiting_upload y se
    reintenta al día siguiente. Evita que dos canales hermanos saturen la
    cuenta compartida (los strikes de YouTube son por cuenta/proyecto).
    """
    pass


class InvalidPublishAtError(ValueError):
    """publishAt is in the past (or within the safety buffer).

    Raised fail-closed BEFORE emitting the billable videos.insert request, so a
    stale scheduled publish time never burns 1600 quota units and never triggers
    a retry loop. Callers should recompute target_public_at via
    pipeline.publish_scheduler.ensure_future_target_public_at and retry.
    """
    pass


class SpamRemovalError(RuntimeError):
    """YouTube removed the just-uploaded video (spam / IA / policy detection).

    Raised by ``_verify_upload_exists`` when the video is confirmed missing or
    reported as "Deleted video" AFTER a successful upload.

    HARD FILTER contract (shorts):
    - The slot is CANCELED (never retried) — retrying after a removal only
      feeds more spam signals to YouTube.
    - The channel's shorts are BLOCKED for a cooling period (circuit breaker)
      so the channel never keeps posting into an active spam flag.
    """
    pass


class ChannelSpamBlockedError(RuntimeError):
    """El canal está bloqueado por una penalización de spam de YouTube.

    Se lanza en ``upload()`` ANTES de autenticar o emitir cualquier request,
    cuando ``is_channel_spam_blocked(channel_id)`` es True. Cubre tanto shorts
    como vídeos normales: mientras dure la penalización no se sube nada.
    Los callers NO deben reintentar; deben mantener el job/video en espera
    hasta que expire el bloque.
    """
    pass

# ── Token file concurrency protection ────────────────────────
# Two threads sharing the same channel token must not race on
# read → refresh → write.  A per-account mutex serialises all
# token I/O for a given pickle file.
_TOKEN_LOCKS: dict[str, threading.Lock] = {}
_TOKEN_LOCKS_GUARD = threading.Lock()
_TOKEN_REFRESH_COOLDOWN = 5  # seconds — if file was saved <5s ago, re-read instead of deleting


def build_upload_status(*, privacy: str, publish_at: str | None,
                        content_type: str = "long") -> dict:
    """Build the YouTube status payload without allowing scheduled Shorts."""
    if content_type == "short":
        return {"privacyStatus": "public"}
    status = {"privacyStatus": "private" if publish_at else privacy}
    if publish_at:
        status["publishAt"] = publish_at
    return status


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

    def _http_error_reason(self, exc: HttpError) -> str:
        """Extract the YouTube API error reason from a googleapiclient HttpError."""
        try:
            import json as _json
            content = exc.content if hasattr(exc, "content") else b""
            data = _json.loads(content) if content else {}
            return data.get("error", {}).get("errors", [{}])[0].get("reason", "") or ""
        except Exception:
            return ""

    def _mark_quota_exhausted(self, caller: str = "", readonly: bool = False) -> None:
        """Trip the YouTube quota circuit breaker for this channel's project.

        Fase cuota (ago 2026):
        - El breaker es per PROJECT (set_quota_exhausted resuelve el proyecto).
        - readonly=True (stats, playlists, verificaciones): NO fija el breaker
          de subidas — solo logea. Un 403 de lectura no debe parar las subidas
          del proyecto (las llamadas de lectura son 1 ud y reintentables).
        """
        channel = self.channel_slug or self.account_name or "unknown"
        if readonly:
            logger.warning(
                "[%s] quotaExceeded en operación de solo lectura (%s) — "
                "no se activa el breaker de subidas",
                channel, caller or "unknown",
            )
            return
        try:
            from database.db_extended import ExtendedDatabase
            _qdb = ExtendedDatabase()
            _qdb.set_quota_exhausted(channel_slug=channel)
        except Exception:
            pass
        try:
            from api.services.lifecycle_monitor import create_alert
            from database.db_extended import ExtendedDatabase as _E2
            _adb = _E2()

            # Resolve the shared GCP project + affected channels so the alert
            # is a PER-TOKEN notice (sincronías+civilizaciones share a project,
            # expediciones+anomalías share another).
            project_id = "unknown"
            shared_channels: list = []
            alert_entity_id = None
            try:
                from api.services.quota_tracker import get_channel_project, project_entity_id
                project_id = get_channel_project(channel)
                for ch in (_adb.get_channels(active_only=False) or []):
                    s = ch.get("slug")
                    if s and get_channel_project(s) == project_id:
                        shared_channels.append(s)
                alert_entity_id = project_entity_id(project_id)
            except Exception:
                pass

            channels_label = ", ".join(shared_channels) or channel
            # entity_id deriva del project_id (crc32 estable) para que CADA
            # cuenta/proyecto tenga su propia alerta activa de quota_exhausted
            # (el dedup de create_alert es por entity_type+entity_id+alert_type).
            create_alert(
                _adb,
                entity_type="system", entity_id=alert_entity_id,
                channel_id=None,
                alert_type="quota_exhausted", severity="critical",
                title=f"YouTube API quota agotada — {project_id}",
                message=(
                    f"Cuota diaria agotada para el proyecto GCP '{project_id}' "
                    f"(token compartido). Canales afectados: {channels_label}. "
                    "Subidas y comprobaciones YT pausadas para este proyecto "
                    "hasta el reset PT."
                ),
                metadata={
                    "channel": channel,
                    "caller": caller,
                    "project_id": project_id,
                    "channels": shared_channels,
                },
            )
        except Exception:
            pass

    def _raise_if_quota_exceeded(self, exc: HttpError, caller: str = "",
                                 readonly: bool = False) -> None:
        if self._http_error_reason(exc) == "quotaExceeded":
            self._mark_quota_exhausted(caller=caller, readonly=readonly)
            raise QuotaExhaustedError(
                "Cuota diaria de YouTube API agotada. Generación sigue activa. Reintentar tras el reset PT."
            ) from exc

    @staticmethod
    def _channel_alert_entity_id(slug: str) -> int:
        """Stable positive entity_id for a channel slug (crc32).

        pipeline_alerts deduplica por (entity_type, entity_id, alert_type);
        hash() está randomizado entre procesos, así que se usa crc32 para que
        el dedup sobreviva reinicios de la API.
        """
        try:
            return abs(zlib.crc32(str(slug or "unknown").encode("utf-8"))) % (10 ** 6)
        except Exception:
            return 0

    def _alert_yt_upload_error(self, error_reason: str, message: str,
                                severity: str = "warning", caller: str = ""):
        """Alert on a non-quota YouTube upload rejection (policy/auth/limit).

        La cuota ya tiene su alerta dedicada (quota_exhausted). Cualquier otro
        rechazo de YouTube (cuenta no verificada, signup, auth, invalidTitle,
        política...) bloquea la subida y necesita acción del operador — no debe
        quedar en silencio. Dedup por canal: una sola alerta activa.
        """
        try:
            from api.services.lifecycle_monitor import emit_alert
            from database.db_extended import ExtendedDatabase
            _adb = ExtendedDatabase()
            slug = self.channel_slug or self.account_name or "unknown"
            emit_alert(
                _adb, entity_type="system",
                entity_id=self._channel_alert_entity_id(slug),
                channel_id=None,
                alert_type="yt_upload_error", severity=severity,
                title=f"YouTube rechazó la subida ({error_reason or 'error'}) — {slug}",
                message=message,
                metadata={"channel": slug, "reason": error_reason,
                          "caller": caller},
            )
        except Exception:
            pass

    def _resolve_yt_token_alert(self):
        """Auto-resolve yt_token_invalid alerts once the token refreshes/works."""
        try:
            from database.db_extended import ExtendedDatabase
            _adb = ExtendedDatabase()
            slug = self.account_name or "unknown"
            with _adb._connect() as conn:
                conn.execute(
                    """UPDATE pipeline_alerts
                       SET resolved = 1, resolved_at = datetime('now'),
                           message = message || ' [Auto-resuelto: token YT renovado]'
                       WHERE alert_type = 'yt_token_invalid' AND resolved = 0
                         AND entity_id = ?""",
                    (self._channel_alert_entity_id(slug),),
                )
                conn.commit()
        except Exception:
            pass

    def _alert_token_invalid(self, exc: Exception):
        """Critical alert: the OAuth token could not be refreshed.

        Un token YT inválido/revocado para la cuenta Google bloquea las
        subidas de TODOS los canales que la comparten — debe verse.
        """
        try:
            from api.services.lifecycle_monitor import emit_alert
            from database.db_extended import ExtendedDatabase
            _adb = ExtendedDatabase()
            slug = self.account_name or "unknown"
            emit_alert(
                _adb, entity_type="system",
                entity_id=self._channel_alert_entity_id(slug),
                channel_id=None,
                alert_type="yt_token_invalid", severity="critical",
                title=f"Token de YouTube inválido/revocado — cuenta {slug}",
                message=(
                    f"El refresh token de la cuenta '{slug}' falló: {exc}\n\n"
                    f"🔧 Acción requerida: re-autenticar el canal "
                    f"(OAuth flow: Canales → Autenticar, o scripts/oauth_quick.py). "
                    f"Las subidas de los canales que comparten esta cuenta "
                    f"quedarán bloqueadas hasta renovarlo."
                ),
                metadata={"account": slug, "error": str(exc)[:500]},
            )
        except Exception:
            pass

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

    def _update_egress_transfer_state(self, video_path, state: str,
                                      vps_staged_path: str = None) -> None:
        """Registra el estado intermedio de transferencia al VPS (best-effort).

        ``state`` ∈ transferring | staged | done. ``vps_staged_path`` se guarda
        cuando el archivo ya está en el VPS. Fail-open: nunca rompe la subida.
        """
        try:
            from database.db_extended import ExtendedDatabase
            _db = ExtendedDatabase()
            with _db._connect() as _conn:
                _row = _conn.execute(
                    "SELECT id FROM videos WHERE video_path=? LIMIT 1",
                    (str(video_path),),
                ).fetchone()
            if not _row:
                return
            _vid = _row["id"]
            if vps_staged_path is not None:
                _db.update_video(_vid, egress_transfer_state=state,
                                 vps_staged_path=vps_staged_path)
            else:
                _db.update_video(_vid, egress_transfer_state=state)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] egress transfer state update skipped: %s",
                         self.channel_slug, exc)

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
                        self._resolve_yt_token_alert()
                    except Exception as exc:
                        logger.warning("Token refresh failed: %s", exc)
                        self._alert_token_invalid(exc)
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
        progress_callback_detail=None,
        suggested_video_filename: str = None,
        suggested_thumb_filename: str = None,
        publish_at: str = None,
        quota_reference_id: str | None = None,
        content_type: str = "long",
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
        - progress_callback_detail: optional callable(dict) invoked on each chunk
          with {"pct": int, "bytes_done": int|None, "bytes_total": int|None}
          so the UI can show real MB + speed + ETA.
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
        # Keep the legacy entry point fail-closed before authentication or any
        # possible Google request.  The dispatcher repeats this guard at its
        # own boundary so direct dispatcher consumers receive the same safety.
        from config.settings import YT_REMEDIATION_MODE
        if YT_REMEDIATION_MODE:
            raise QuotaExhaustedError(
                "Remediation mode active: upload dispatch is blocked fail-closed."
            )

        # ── Gate central de bloqueo por spam (shorts + vídeos normales) ──
        # Si el canal está penalizado por YouTube, no se sube NADA durante el
        # periodo. Se resuelve el channel_id desde el slug y se consulta el
        # estado. Se lanza ANTES de autenticar/emitir requests para no gastar
        # cuota ni alimentar la señal de spam.
        # Robustez: se usa channel_slug, con fallback a account_name (la mayoría
        # de callers pasan `YouTubeUploader(slug)` → account_name=slug), para que
        # TODAS las rutas de subida queden cubiertas durante el ban.
        _gate_slug = self.channel_slug or (
            self.account_name if self.account_name and self.account_name != "default" else None
        )
        if _gate_slug:
            try:
                from database.db_extended import ExtendedDatabase
                _spam_db = ExtendedDatabase()
                _ch_row = _spam_db.get_channel_by_slug(_gate_slug)
                if _ch_row and _spam_db.is_channel_spam_blocked(int(_ch_row["id"])):
                    raise ChannelSpamBlockedError(
                        f"channel '{_gate_slug}' is spam-blocked by YouTube — upload held"
                    )
            except ChannelSpamBlockedError:
                raise
            except Exception as _sb_err:
                logger.warning(
                    "[%s] spam-block gate check failed (fail-open): %s",
                    _gate_slug, _sb_err,
                )

        # ── Cap de subidas por cuenta Google (antiban, ago 2026) ──
        # Los strikes son por cuenta/proyecto GCP: dos canales hermanos pueden
        # saturar la cuenta aunque cada uno cumpla su cap individual. Si la
        # cuenta ya subió su tope diario, la subida se rechaza localmente
        # (AccountDailyCapExceededError → retryable, sin impacto de cuota ni
        # señal de spam). Se comprueba ANTES de autenticar/emitir requests.
        if _gate_slug:
            try:
                from api.services.spam_mitigation import (
                    account_upload_slots_available, get_channel_account,
                )
                _account = get_channel_account(_gate_slug)
                if _account and not account_upload_slots_available(_account):
                    raise AccountDailyCapExceededError(
                        f"account '{_account}' reached its daily upload cap — upload held (retry tomorrow)"
                    )
            except AccountDailyCapExceededError:
                raise
            except Exception as _cap_err:
                logger.warning(
                    "[%s] account-cap gate check failed (fail-open): %s",
                    _gate_slug, _cap_err,
                )
        video_path = Path(video_path)
        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")

        # ── Canonical path for quota reference ────────────────────
        # Capture BEFORE the SEO rename below: the reference_id used for quota
        # admission must be stable and unique per video. Using the renamed temp
        # copy (e.g. "video.mp4" when the title is empty) made two different
        # videos collide on the same reference and one got denied with
        # "already_consumed" → false "quota agotada" breaker trip (ago 2026).
        original_video_path = video_path.resolve()

        # ── v25: fail-closed publishAt guard ────────────────────
        # YouTube rejects a publishAt in the past with a 403 that already bills
        # the 1600-unit videos.insert. Validate BEFORE issuing the request so a
        # stale scheduled publish time fails cleanly (InvalidPublishAtError)
        # instead of burning quota and triggering the retry loop.
        if publish_at:
            from api.time_utils import youtube_rfc3339
            publish_at = youtube_rfc3339(publish_at)
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            _pa = str(publish_at).strip()
            try:
                if _pa.endswith(("Z", "z")):
                    pa_dt = _dt.fromisoformat(_pa.replace("z", "Z").replace("Z", "+00:00"))
                else:
                    pa_dt = _dt.fromisoformat(_pa.replace(" ", "T"))
                if pa_dt.tzinfo is None:
                    pa_dt = pa_dt.replace(tzinfo=_tz.utc)
                if pa_dt.astimezone(_tz.utc) < _dt.now(_tz.utc) + _td(minutes=5):
                    raise InvalidPublishAtError(
                        f"publishAt is in the past (or within safety buffer): {publish_at}"
                    )
            except InvalidPublishAtError:
                raise
            except (ValueError, TypeError) as _pe:
                raise InvalidPublishAtError(
                    f"Unparseable publishAt: {publish_at!r}"
                ) from _pe

        # Final central boundary: direct/manual callers cannot accidentally
        # upload a scheduled channel publicly without a future publishAt.
        from api.services.publication_policy import validate_upload_visibility
        if content_type == "short":
            privacy, publish_at = "public", None
        configured_mode = self._get_config_attr("PUBLISH_MODE", "immediate")
        validate_upload_visibility(
            publish_mode=str(configured_mode or "immediate").lower(),
            privacy=privacy,
            publish_at=publish_at,
            content_type=content_type,
        )

        service = None
        _egress_client = None
        if self.channel_slug:
            from api.services.egress_delegation import egress_client_for
            _egress_client = egress_client_for(self.channel_slug)
        if _egress_client is None:
            service = self._get_service()

        # ── Anti-strike: espaciado global de subidas entre canales ──
        # YouTube elimina subidas en ráfagas cruzadas (varios canales a la vez
        # desde la misma IP). Antes de emitir la subida, espera el hueco mínimo
        # desde la última subida de OTRO canal (por canal lo gobierna el cooldown
        # existente). Se espera en bucle (chunked) sin bloquear el event loop.
        self._wait_global_upload_spacing()

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
                    **build_upload_status(
                        privacy=privacy, publish_at=publish_at,
                        content_type=content_type,
                    ),
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

            if _egress_client is not None:
                # ── Subida delegada al agente egress (IP aislada), 2 pasos ──
                # 1) /stage: el server transfiere el mp4(+thumb) al VPS y guarda
                #    el estado intermedio (transferring → staged/awaiting_vps_upload).
                # 2) /upload: el VPS sube a YouTube desde su IP residencial con la
                #    programación del server (título, desc, tags, publishAt...).
                # El server conserva el bookkeeping de DB con el resultado.
                _meta = {
                    "title": body["snippet"]["title"],
                    "description": body["snippet"]["description"],
                    "tags": body["snippet"].get("tags", []),
                    "category_id": body["snippet"]["categoryId"],
                    "language": body["snippet"].get("defaultLanguage", "es"),
                    "privacy": privacy,
                    "publish_at": publish_at,
                    "self_declared_made_for_kids": body["status"].get("selfDeclaredMadeForKids", False),
                    "embeddable": body["status"].get("embeddable", True),
                    "public_stats_viewable": body["status"].get("publicStatsViewable", True),
                }
                _thumb = str(thumbnail_path) if thumbnail_path and Path(thumbnail_path).exists() else None
                self._update_egress_transfer_state(video_path, "transferring")
                logger.info("[%s] etapa 1/2: transfiriendo vídeo al VPS (%s)", self.channel_slug, _egress_client.base_url)
                _stage = _egress_client.stage(str(video_path), ref=str(original_video_path),
                                              thumbnail_path=_thumb)
                if not _stage.get("ok"):
                    raise RuntimeError(_stage.get("error", "stage vía agente falló"))
                _staged_path = _stage.get("staged_path", "")
                self._update_egress_transfer_state(video_path, "staged",
                                                   vps_staged_path=_staged_path)
                if _thumb:
                    _meta["thumbnail_path"] = _thumb
                logger.info("[%s] etapa 2/2: subiendo desde el VPS (staged=%s)", self.channel_slug, _staged_path)
                agent_res = _egress_client.upload(_staged_path, _meta)
                if not agent_res.get("ok"):
                    raise RuntimeError(agent_res.get("error", "subida vía agente falló"))
                self._update_egress_transfer_state(video_path, "done")
                response = {
                    "id": agent_res["video_id"],
                    "status": {"uploadStatus": "processed",
                               "privacyStatus": "private" if publish_at else privacy},
                }
                _egress_agent_uploaded = True
            else:
                _egress_agent_uploaded = False
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
                # The dispatcher reserves exactly 1,600 units atomically.  The
                # transport marks the boundary immediately before next_chunk(),
                # where the first billable videos.insert request is issued.
                from api.services.youtube_upload_dispatcher import (
                    UploadDispatchBlocked,
                    YouTubeUploadDispatcher,
                )
                # Callers with a durable database/job ID can supply it explicitly;
                # legacy callers retain a deterministic canonical-path fallback
                # (original pre-rename path — unique per generated video).
                reference_id = quota_reference_id or (
                    f"upload:{self.channel_slug or self.account_name}:{original_video_path}"
                )
                try:
                    response = YouTubeUploadDispatcher(self.channel_slug or self.account_name).dispatch(
                        reference_id=reference_id,
                        content_class="short" if "short" in str(video_path).lower() else "long",
                        transport=lambda request_started: self._upload_transport(
                            request, request_started, heartbeat_callback, progress_callback,
                            progress_callback_detail,
                        ),
                    )
                except UploadDispatchBlocked as exc:
                    # Local admission denial (reference collision, budget, unknown
                    # project...). NOT a YouTube quota error: must not trip the
                    # per-project breaker nor create a "quota agotada" alert.
                    raise UploadAdmissionDeniedError(str(exc)) from exc

            # ── Validate YouTube response ─────────────────────────
            self._validate_upload_response(response)

            video_id: str = response["id"]

            # ── Track quota (diagnostic) ──────────────────────────
            track_quota(self.channel_slug, "videos.insert", 1600,
                        yt_id=video_id, caller="YouTubeUploader.upload")
            youtube_url = f"https://www.youtube.com/watch?v={video_id}"
            logger.info("Upload complete: %s", youtube_url)

            # ── Anti-strike: registrar la subida para el espaciado global ──
            try:
                from api.services.upload_spacing import record_upload
                record_upload(self.channel_slug or self.account_name)
            except Exception as _sp_exc:
                logger.debug("record_upload failed: %s", _sp_exc)

            # ── Post-upload verification: confirm video exists on YouTube ──
            # (El agente ya verifica post-subida desde su red; se omite local.)
            if not _egress_agent_uploaded:
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
            if thumbnail_path and Path(thumbnail_path).exists() and not _egress_agent_uploaded:
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
        try:
            list_response = service.videos().list(
                part="snippet",
                id=video_id,
            ).execute()
        except HttpError as exc:
            self._raise_if_quota_exceeded(exc, "update_description.fetch_title")
            raise

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

        try:
            service.videos().update(
                part="snippet",
                body=body,
            ).execute()
        except HttpError as exc:
            self._raise_if_quota_exceeded(exc, "update_description")
            raise

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

        try:
            service.videos().update(
                part="status",
                body=body,
            ).execute()
        except HttpError as exc:
            self._raise_if_quota_exceeded(exc, "set_privacy")
            raise

        # ── Track quota (diagnostic) ──────────────────────────────
        track_quota(self.channel_slug, "videos.update", 50,
                    yt_id=video_id, caller="set_privacy")

        logger.info("[%s] Privacy set to %s for video %s", self.channel_slug, privacy, video_id)
        return {"updated": True, "yt_video_id": video_id, "privacy": privacy}

    def set_publish_at(self, video_id: str, publish_at: str) -> dict:
        """Re-programar el publishAt de un vídeo ya subido (private).

        Usado por la remediación de "calentando": si un vídeo se subió como
        private con un publishAt a días vista, esto acerca la publicación.
        YouTube solo admite publishAt en vídeos con privacyStatus='private',
        así que se envía ambas cosas. Quota cost: 50 units.

        Args:
            video_id: ID de YouTube del vídeo.
            publish_at: ISO 8601 (UTC) del nuevo instante de publicación.

        Returns:
            {updated: True, yt_video_id, publish_at} o lanza HttpError.
        """
        service = self._get_service()

        body = {
            "id": video_id,
            "status": {
                "privacyStatus": "private",
                "publishAt": publish_at,
            },
        }

        try:
            service.videos().update(
                part="status",
                body=body,
            ).execute()
        except HttpError as exc:
            self._raise_if_quota_exceeded(exc, "set_publish_at")
            raise

        # ── Track quota (diagnostic) ──────────────────────────────
        track_quota(self.channel_slug, "videos.update", 50,
                    yt_id=video_id, caller="set_publish_at")

        logger.info(
            "[%s] publishAt reprogramado para video %s → %s",
            self.channel_slug, video_id, publish_at,
        )
        return {"updated": True, "yt_video_id": video_id, "publish_at": publish_at}

    # ── Thumbnail ───────────────────────────────────────────────

    def _set_thumbnail(
        self, service: Any, video_id: str, thumbnail_path: Path
    ) -> bool:
        """Set custom thumbnail for a YouTube video. Returns True on success.

        Note: YouTube Data API v3 has NO `thumbnails().list()` method — only
        `thumbnails().set()`. A previous revision tried to call the non-existent
        `list()`, which raised `AttributeError: 'Resource' object has no
        attribute 'list'` AFTER the upload succeeded, aborting the whole upload
        and causing endless re-upload loops (duplicate videos). `set()` returns
        204 No Content on success and raises HttpError on real failures, so no
        extra verification is needed.
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

            # set() succeeded without raising — thumbnail is applied.
            return True

        except HttpError as exc:
            try:
                self._raise_if_quota_exceeded(exc, "_set_thumbnail")
            except QuotaExhaustedError:
                raise
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
    
    def _record_spam_strike_if_needed(self, video_id: str = None,
                                      reason: str = None) -> None:
        """Registra un strike de spam para el canal (shorts Y vídeos normales).

        Se llama desde _verify_upload_exists cuando YouTube elimina una subida.
        Es el PUNTO ÚNICO de detección/registro: bloquea el canal durante el
        periodo de penalización y cubre tanto shorts como vídeos normales sin
        doble conteo (los catch de SpamRemovalError de shorts ya NO registran).

        ``video_id`` / ``reason`` se guardan para el informe de situación
        (por qué se bloqueó el canal).
        """
        try:
            if not self.channel_slug:
                return
            from database.db_extended import ExtendedDatabase
            _db = ExtendedDatabase()
            _ch = _db.get_channel_by_slug(self.channel_slug)
            if not _ch:
                return
            from api.services.shorts_scheduler import _record_short_spam_strike
            _record_short_spam_strike(
                int(_ch["id"]), self.channel_slug, db=_db,
                video_id=video_id, reason=reason,
            )
        except Exception as _e:
            logger.warning("[%s] spam-strike record failed: %s", self.channel_slug, _e)

    def _wait_global_upload_spacing(self, timeout_sec: int = 3600) -> None:
        """Anti-strike: espera el hueco mínimo entre subidas de CANALES DISTINTOS.

        Duerme en tramos (30 s) hasta que pase el espaciado global desde la
        última subida de otro canal, o hasta agotar ``timeout_sec`` (seguridad,
        nunca bloquear indefinidamente). Si el último upload fue del MISMO
        canal no espera (lo gobierna el cooldown por canal).
        """
        try:
            from api.services.upload_spacing import remaining_spacing_seconds
            channel = self.channel_slug or self.account_name
            waited = 0
            while waited < timeout_sec:
                remaining = remaining_spacing_seconds(channel)
                if remaining <= 0:
                    return
                sleep_for = min(30, remaining)
                logger.info(
                    "[%s] Global upload spacing: esperando %ds (restan ~%ds de %s)",
                    channel, sleep_for, remaining,
                    "otra subida reciente de otro canal",
                )
                time.sleep(sleep_for)
                waited += sleep_for
        except Exception as _exc:
            logger.debug("[%s] upload spacing wait skipped: %s", self.channel_slug, _exc)

    def _watch_page_status(self, video_id: str) -> str:
        """Clasifica el estado público del vídeo sin gastar cuota de API.

        Devuelve:
          - "removed"   : la watch page reporta no disponible/eliminado.
          - "private"   : existe pero no es público (LOGIN_REQUIRED / privado).
          - "available" : público y visible (status OK).
          - "unknown"   : no se pudo determinar (red, parseo, etc.).

        Usado como fallback en `_verify_upload_exists` para NO registrar un
        strike por un lag de indexado de la API: si la API devuelve vacío pero
        la watch page dice "private"/"available", el vídeo NO fue eliminado.
        """
        import urllib.request
        try:
            url = f"https://www.youtube.com/watch?v={video_id}&hl=es"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept-Language": "es-ES,es;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read(250_000).decode("utf-8", "ignore")
        except Exception as exc:
            logger.debug("[%s] watch-page check failed for %s: %s", self.channel_slug, video_id, exc)
            return "unknown"

        if '"status":"OK"' in html or '"status":"LIVE_STREAM_OFFLINE"' in html:
            return "available"
        if '"status":"LOGIN_REQUIRED"' in html:
            return "login_required"
        # LOGIN_REQUIRED y los mensajes genéricos de disponibilidad no prueban
        # una eliminación; solo marcadores explícitos son removal candidates.
        for marker in (
            "This video isn't available anymore",
            "This video has been removed",
        ):
            if marker in html:
                return "removed"
        return "unknown"

    def _watch_page_removal_confirmed(self, video_id: str) -> bool:
        """Require two explicit watch-page observations before a strike."""
        from api.services.channel_policy import should_create_removal_alert
        first = self._watch_page_status(video_id)
        if first != "removed":
            return False
        second = self._watch_page_status(video_id)
        return should_create_removal_alert(first, int(second == "removed"))

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
                try:
                    resp = service.videos().list(
                        part="status,snippet", id=video_id
                    ).execute()
                except HttpError as exc:
                    self._raise_if_quota_exceeded(exc, "_verify_upload_exists")
                    raise

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
                    # ── Anti-strike: confirmar con la watch page antes de
                    # declarar strike. La API puede devolver vacío por lag de
                    # indexado de un vídeo recién subido (o private programado).
                    # Si la página dice private/available, NO es una eliminación.
                    _wp = self._watch_page_status(video_id)
                    if _wp in ("private", "scheduled", "login_required", "available",
                               "unknown", "error", "unavailable"):
                        logger.warning(
                            "Post-upload verification: API vacía para %s pero watch "
                            "page=%s (lag de indexado / private programado) — NO se "
                            "registra strike; se tratará como pendiente.",
                            video_id, _wp,
                        )
                        return
                    # Solo dos señales explícitas de eliminación permiten strike.
                    if not self._watch_page_removal_confirmed(video_id):
                        return
                    self._record_spam_strike_if_needed(
                        video_id=video_id,
                        reason="video no aparece en YouTube tras la subida (eliminado por spam/IA)",
                    )
                    raise SpamRemovalError(
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
                    # ── Anti-strike: confirmar con la watch page (lag/falso positivo) ──
                    _wp2 = self._watch_page_status(video_id)
                    if _wp2 in ("private", "scheduled", "login_required", "available",
                                "unknown", "error", "unavailable"):
                        logger.warning(
                            "Post-upload verification: título 'Deleted video' para %s "
                            "pero watch page=%s — NO se registra strike (falso positivo).",
                            video_id, _wp2,
                        )
                        return
                    if not self._watch_page_removal_confirmed(video_id):
                        return
                    self._record_spam_strike_if_needed(
                        video_id=video_id,
                        reason="Deleted video — YouTube marcó la subida como 'Deleted video' (spam/IA/política)",
                    )
                    raise SpamRemovalError(
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

    def _upload_transport(self, request: Any, request_started, heartbeat_callback=None,
                          progress_callback=None, progress_callback_detail=None) -> dict:
        """Explicit dispatcher transport boundary for a resumable upload."""
        request_started()
        return self._resumable_upload(
            request,
            heartbeat_callback=heartbeat_callback,
            progress_callback=progress_callback,
            progress_callback_detail=progress_callback_detail,
        )

    def _resumable_upload(self, request: Any, heartbeat_callback=None,
                           progress_callback=None, progress_callback_detail=None) -> dict:
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
                    if pct != last_reported_pct:
                        last_reported_pct = pct
                        # ── Propagate progress to external callback ──
                        if progress_callback:
                            try:
                                progress_callback(pct)
                            except Exception:
                                pass
                        # ── Richer payload: bytes for MB/speed/ETA in the UI ──
                        if progress_callback_detail:
                            try:
                                progress_callback_detail({
                                    "pct": pct,
                                    "bytes_done": getattr(status, "resumable_progress", None),
                                    "bytes_total": getattr(status, "total_size", None),
                                })
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
                    error_reason = self._http_error_reason(exc)
                    if error_reason == "uploadLimitExceeded":
                        _msg = (
                            "YouTube rechazó el vídeo: la cuenta NO está verificada y el vídeo supera 15 minutos. "
                            "Verifica la cuenta en youtube.com/verify."
                        )
                        self._alert_yt_upload_error(error_reason, _msg,
                                                     severity="critical", caller="_resumable_upload")
                        raise RuntimeError(_msg) from exc
                    if error_reason == "youtubeSignupRequired":
                        _msg = (
                            "La cuenta de YouTube requiere registro adicional (youtube.com/create_channel)."
                        )
                        self._alert_yt_upload_error(error_reason, _msg,
                                                     severity="critical", caller="_resumable_upload")
                        raise RuntimeError(_msg) from exc
                    if error_reason == "quotaExceeded":
                        self._mark_quota_exhausted(caller="_resumable_upload")
                        raise QuotaExhaustedError(
                            "Cuota diaria de YouTube API agotada. Generación sigue activa. Reintentar tras el reset PT."
                        ) from exc
                    logger.error("Auth/permission error (%s): %s", error_reason or "unknown", exc)
                    self._alert_yt_upload_error(
                        error_reason or "auth_error",
                        f"Error de autenticación/permisos en la subida ({error_reason or 'unknown'}): {exc}. "
                        "Revisar el token OAuth de la cuenta.",
                        severity="critical", caller="_resumable_upload",
                    )
                    raise
                if exc.resp.status in (500, 502, 503, 504):
                    if consecutive_errors >= MAX_RETRIES:
                        _msg = f"Upload failed after {MAX_RETRIES} consecutive server errors: {exc}"
                        self._alert_yt_upload_error("server_error", _msg,
                                                     severity="warning", caller="_resumable_upload")
                        raise RuntimeError(_msg)
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
                        _msg = (
                            "YouTube rechazó el vídeo: supera el límite de duración permitido. "
                            "Verifica la cuenta en youtube.com/verify para subir vídeos de más de 15 min."
                        )
                        self._alert_yt_upload_error(error_reason, _msg,
                                                     severity="warning", caller="_resumable_upload")
                        raise RuntimeError(_msg) from exc
                    if error_reason == "invalidTitle":
                        _msg = f"Título inválido: {exc}"
                        self._alert_yt_upload_error(error_reason, _msg,
                                                     severity="warning", caller="_resumable_upload")
                        raise RuntimeError(_msg) from exc
                    _msg = f"YouTube rechazó la subida (400 {error_reason}): {exc}"
                    self._alert_yt_upload_error(error_reason or "bad_request", _msg,
                                                 severity="warning", caller="_resumable_upload")
                    raise RuntimeError(_msg) from exc
                raise
            except (OSError, ConnectionError) as exc:
                consecutive_errors += 1
                if consecutive_errors >= MAX_RETRIES:
                    _msg = f"Upload failed after {MAX_RETRIES} consecutive network errors: {exc}"
                    self._alert_yt_upload_error("network_error", _msg,
                                                 severity="warning", caller="_resumable_upload")
                    raise RuntimeError(_msg)
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
