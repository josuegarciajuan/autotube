"""AI image generation via Pollo AI — delegates to the proven worker subprocess.

This module is a thin wrapper around ``tools/pollo_image_worker.py`` (copied
verbatim from the lamami "publicista" project, which is the reference working
integration). We invoke the worker exactly like publicista does — via CLI
subprocess — so behaviour is byte-identical to the production-proven path.

Auth: session cookie (env ``POLLO_SESSION_COOKIE`` override, else the shared
lamami ``settings.json``). The worker uses curl-cffi to impersonate Chrome and
bypass Cloudflare, polls the generation, and downloads the no-watermark image.

Dual-cookie rotation (new):
When ``pollo_accounts[]`` is present in the shared settings.json, this module
activates dual-account mode: randomly selects a primary account from those with
credits, passes a different account as fallback to the worker. The worker
automatically switches to the fallback if the primary is out of credits or has
an expired session. Alerts are created in ``pipeline_alerts`` when an account
is exhausted.

Public interface (unchanged, consumed by pipeline/thumbnail_maker.py):
    gen = AIImageGenerator(model="pollo-image-v2", db=db)
    path = gen.generate("dramatic surprised face", Path("output/thumb.jpg"))
    paths = gen.generate_batch(["prompt A", "prompt B"], Path("output/"))

Scene image generation (new — for video scene backgrounds):
    scene_gen = SceneImageGenerator()
    path = scene_gen.generate_scene_image("dark forest clearing", theme=ctx)
"""
import hashlib
import json
import logging
import os
import random
import subprocess
import sys
import tempfile
import time
import zlib
from datetime import date, datetime
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.theme_extractor import ThemeContext

logger = logging.getLogger(__name__)

# Worker copied verbatim from the lamami publicista project (self-contained).
WORKER_PATH = Path(__file__).resolve().parent.parent / "tools" / "pollo_image_worker.py"

DEFAULT_MODEL = "pollo-image-v2"
DEFAULT_ASPECT_RATIO = "16:9"
MAX_PROMPT_CHARS = 2000
WORKER_TIMEOUT = 420          # seconds passed to the worker (--timeout)
SUBPROCESS_TIMEOUT = 520      # hard kill for the subprocess itself

# Shared paths between Autotube and CRM (lamamionline-control)
LAMAMI_SETTINGS_PATH = "/root/lamamionline-control/data/settings.json"
POLLO_STATUS_FILE = os.getenv(
    "POLLO_STATUS_FILE",
    "/root/lamamionline-control/data/pollo_accounts_status.json",
)

# Kept for compatibility (callers may reference these).
MODEL_CONFIG: dict[str, dict] = {
    "pollo-image-v2": {"modelName": "pollo-image-v2", "aspectRatio": "1:1"},
    "pollo-image-v1-6": {"modelName": "pollo-image-v1-6", "aspectRatio": "1:1"},
    "flux-dev": {"modelName": "flux-dev", "aspectRatio": "2:3"},
    "seedream": {"modelName": "seedream", "aspectRatio": "2:3"},
    "nano-banana": {"modelName": "nano-banana", "aspectRatio": "4:3"},
}


class PolloAIError(Exception):
    """Raised when Pollo AI generation fails."""


# ── Cookie resolution ──────────────────────────────────────

def _read_settings_json() -> dict:
    """Read the shared lamami settings.json. Returns empty dict on failure."""
    try:
        with open(LAMAMI_SETTINGS_PATH, "r") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _read_cookie() -> str:
    """Resolve Pollo session cookie from env override or lamami settings.json
    (legacy single-cookie fallback)."""
    override = os.getenv("POLLO_SESSION_COOKIE", "").strip()
    if override:
        return override

    data = _read_settings_json()
    raw = str(data.get("pollo_session_cookie", "")).strip()
    return raw


# ── Dual-cookie account management ─────────────────────────

def _read_pollo_accounts() -> list[dict]:
    """Read ``pollo_accounts[]`` from the shared settings.json.
    Returns list of {cookie, expires, label} dicts. Empty list if not found."""
    settings = _read_settings_json()
    accounts = settings.get("pollo_accounts", [])
    if isinstance(accounts, list):
        return accounts
    return []


def _read_pollo_status() -> dict:
    """Read the shared pollo_accounts_status.json.
    Returns dict keyed by account label. Empty dict if not found."""
    try:
        with open(POLLO_STATUS_FILE, "r") as fh:
            return json.loads(fh.read() or "{}")
    except Exception:
        return {}


def _parse_expires(value) -> Optional[date]:
    """Parse the optional ``expires`` field of a Pollo account (YYYY-MM-DD)."""
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _account_rank(account: dict, status: dict) -> tuple:
    """Ranking tuple: lower is better. Never excludes an account.

    All flags are only *hints*: an expired cookie can have been renewed without
    clearing the status file, so we still try it (as primary or fallback).
    """
    label = account["label"]
    info = status.get(label, {}) if isinstance(status.get(label), dict) else {}
    credits = bool(info.get("credits_exhausted"))
    session = bool(info.get("session_expired"))
    exp = _parse_expires(account.get("expires"))
    date_expired = exp is not None and exp < date.today()
    return (credits, session, date_expired)


def _select_pollo_account_pair() -> tuple:
    """Select a random primary account plus a *different* fallback account.

    Returns:
        (primary_cookie, primary_label, fallback_cookie, fallback_label)
        or ("single_cookie", "legacy", None, None) if pollo_accounts[] is empty.

    The fallback is always a different account whenever a second account with a
    cookie exists, even if every account is flagged (credits exhausted or
    session expired). Previously, when all accounts were flagged, only the
    first one was returned (``fallback=none``), so the second account was never
    attempted and the system silently degraded to one account.
    """
    accounts = _read_pollo_accounts()
    if not accounts:
        # Fall back to legacy single-cookie mode
        cookie = _read_cookie()
        return (cookie, "legacy", None, None) if cookie else (None, None, None, None)

    status = _read_pollo_status()

    usable = []
    for acc in accounts:
        label = str(acc.get("label", "")).strip()
        cookie = str(acc.get("cookie", "")).strip()
        if not label or not cookie:
            continue
        usable.append({"label": label, "cookie": cookie, "expires": acc.get("expires")})

    if not usable:
        return (None, None, None, None)

    def _pick(pool):
        random.shuffle(pool)
        return pool[0] if pool else None

    healthy = [a for a in usable if _account_rank(a, status) == (False, False, False)]
    primary = _pick(healthy) or _pick(list(usable))

    candidates = [a for a in usable if a["label"] != primary["label"]]
    healthy_candidates = [
        a for a in candidates if _account_rank(a, status) == (False, False, False)
    ]
    fallback = _pick(healthy_candidates) or _pick(candidates)

    logger.info(
        "Pollo accounts: primary=%s fallback=%s usable=%d healthy=%d flagged=%d",
        primary["label"],
        fallback["label"] if fallback else "none",
        len(usable),
        len(healthy),
        len(usable) - len(healthy),
    )
    return (
        primary["cookie"],
        primary["label"],
        fallback["cookie"] if fallback else None,
        fallback["label"] if fallback else None,
    )


def _persist_alert_flags(flags: dict) -> None:
    """Merge alert bookkeeping into the shared status file under its lock.

    The CRM (PHP) and the worker write this file too, so we never overwrite it
    blindly: we re-read it inside the same ``.lock`` file and only touch the
    alert keys we are changing.
    """
    if not flags:
        return
    import fcntl

    path = os.path.abspath(POLLO_STATUS_FILE)
    lock_path = path + ".lock"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(lock_path, "a+") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                try:
                    with open(path, "r") as fh:
                        raw = fh.read()
                    data = json.loads(raw) if raw.strip() else {}
                except FileNotFoundError:
                    data = {}
                except Exception:
                    logger.warning("Pollo status file malformed; alert flags not persisted")
                    return
                if not isinstance(data, dict):
                    return
                for label, changes in flags.items():
                    entry = data.get(label)
                    if not isinstance(entry, dict):
                        entry = {}
                    for key, value in changes.items():
                        if value is None:
                            entry.pop(key, None)
                        else:
                            entry[key] = value
                    data[label] = entry
                tmp = path + ".tmp.%d" % os.getpid()
                with open(tmp, "w") as fh:
                    json.dump(data, fh, indent=2, ensure_ascii=False)
                os.replace(tmp, path)
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
    except Exception as exc:
        logger.warning("Failed to write alert status to %s: %s", POLLO_STATUS_FILE, exc)


def _check_and_alert_pollo_credits(db) -> None:
    """Create pipeline alerts for Pollo accounts that need operator action.

    Distinguishes two independent conditions:
      * ``credits_exhausted`` → recharge credits;
      * ``session_expired``   → renew the session cookie.

    Duplicate alerts are prevented with per-condition flags (``credits_alerted``
    / ``session_alerted``) stored in the shared status file. When an account
    recovers, its flags are cleared so a future incident alerts again.

    Args:
        db: Database module/connection (must have a ``_connect()`` method via
            ``api/services/lifecycle_monitor.create_alert``).
    """
    try:
        status = _read_pollo_status()
        accounts = _read_pollo_accounts()
    except Exception:
        return

    try:
        from api.services.lifecycle_monitor import create_alert
    except ImportError:
        logger.warning("Cannot import create_alert — alerts disabled")
        return

    stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    flags: dict = {}

    conditions = (
        (
            "credits_exhausted",
            "credits_alerted",
            "pollo_credits_exhausted",
            "sin créditos",
            "Se agotaron los créditos de la cuenta {label} en Pollo.ai. "
            "Recarga créditos; se usará otra cuenta como fallback si está disponible.",
        ),
        (
            "session_expired",
            "session_alerted",
            "pollo_session_expired",
            "cookie caducada",
            "La cookie de sesión de la cuenta {label} en Pollo.ai ha caducado (401). "
            "Renueva la cookie en Josue → ConfigM → Pollo.ai · Cuentas.",
        ),
    )

    for acc in accounts:
        label = str(acc.get("label", "")).strip()
        if not label:
            continue
        info = status.get(label, {})
        if not isinstance(info, dict):
            info = {}

        for flag_key, alerted_key, alert_type, title_suffix, message in conditions:
            active = bool(info.get(flag_key))
            already = bool(info.get(alerted_key)) or (
                flag_key == "credits_exhausted" and bool(info.get("alerted"))
            )

            if active and not already:
                # zlib.crc32 keeps entity_id stable across runs (hash() is salted).
                entity_id = zlib.crc32(f"{label}:{flag_key}".encode("utf-8")) & 0x7FFFFFFF
                create_alert(
                    db,
                    entity_type="system",
                    entity_id=entity_id,
                    alert_type=alert_type,
                    severity="warning",
                    title=f"Pollo.ai: cuenta {label} {title_suffix}",
                    message=message.format(label=label),
                    metadata={"account": label, "condition": flag_key},
                )
                flags.setdefault(label, {})[alerted_key] = True
                flags[label][alerted_key + "_at"] = stamp
                logger.info("Alert created for Pollo account %s (%s)", label, flag_key)

            elif not active and (info.get(alerted_key) or info.get("alerted")):
                flags.setdefault(label, {})[alerted_key] = None
                flags[label][alerted_key + "_at"] = None
                if flag_key == "credits_exhausted":
                    flags[label]["alerted"] = None
                    flags[label]["alerted_at"] = None

    _persist_alert_flags(flags)


# ═══════════════════════════════════════════════════════════
# AIImageGenerator — thumbnail + scene generation
# ═══════════════════════════════════════════════════════════

class AIImageGenerator:
    """Generate images via Pollo AI by delegating to the worker subprocess.

    Supports dual-cookie rotation when ``pollo_accounts[]`` is configured in the
    shared settings.json. Falls back to single-cookie mode otherwise.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,   # ignored, kept for compat
        model: Optional[str] = None,
        cookie_value: Optional[str] = None,
        db: object = None,               # optional DB for alert creation
    ) -> None:
        if not WORKER_PATH.exists():
            raise PolloAIError(
                f"Pollo worker not found at {WORKER_PATH}. "
                "Expected tools/pollo_image_worker.py."
            )

        self._db = db
        self._model = model or os.getenv("POLLO_IMAGE_MODEL", DEFAULT_MODEL)

        # ── Dual-cookie mode vs single-cookie legacy ─────────
        self._primary_cookie: str = ""
        self._primary_label: str = ""
        self._fallback_cookie: Optional[str] = None
        self._fallback_label: Optional[str] = None
        self._use_dual_cookie: bool = False

        accounts = _read_pollo_accounts()
        if accounts:
            # Dual-cookie mode active
            primary, primary_label, fallback, fallback_label = _select_pollo_account_pair()
            if not primary:
                raise PolloAIError(
                    "Pollo accounts found in settings.json but all cookies are empty."
                )
            self._primary_cookie = primary
            self._primary_label = primary_label
            self._fallback_cookie = fallback
            self._fallback_label = fallback_label
            self._use_dual_cookie = True
            logger.info(
                "AIImageGenerator: dual-cookie mode (primary=%s, fallback=%s, model=%s)",
                primary_label, fallback_label or "none", self._model,
            )
        else:
            # Legacy single-cookie mode
            self._primary_cookie = cookie_value or _read_cookie()
            if not self._primary_cookie:
                raise PolloAIError(
                    "Pollo session cookie not set. Set POLLO_SESSION_COOKIE in .env "
                    "or ensure /root/lamamionline-control/data/settings.json has a "
                    "valid pollo_session_cookie or pollo_accounts field."
                )
            logger.info("AIImageGenerator: single-cookie mode (model=%s)", self._model)

    # ── Public API ────────────────────────────────────────────

    def generate(
        self,
        prompt: str,
        output_path: Path,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    ) -> Path:
        """Generate a single image and save to *output_path*.

        Returns the resolved output path. Raises PolloAIError on failure.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if len(prompt) > MAX_PROMPT_CHARS:
            raise PolloAIError(
                f"Prompt exceeds {MAX_PROMPT_CHARS} chars ({len(prompt)}). Truncate it."
            )

        json_out = output_path.with_suffix(".pollo.json")

        # Cookies are passed through a 0600 temp file, never as argv: command
        # lines are world-readable via /proc and would leak session tokens.
        cookie_file = self._write_cookie_file()
        try:
            cmd = [
                sys.executable, str(WORKER_PATH), "generate",
                "--cookie-file", cookie_file,
                "--account-label", self._primary_label or "primary",
                "--prompt", prompt,
                "--model", self._model,
                "--aspect-ratio", aspect_ratio or DEFAULT_ASPECT_RATIO,
                "--num-outputs", "1",
                "--output-image", str(output_path),
                "--output-json", str(json_out),
                "--timeout", str(WORKER_TIMEOUT),
            ]

            # Dual-cookie mode: always pass the shared status file so account
            # failures are recorded (even when there is only one usable account).
            if self._use_dual_cookie:
                cmd.extend(["--status-file", POLLO_STATUS_FILE])
                if self._fallback_cookie:
                    cmd.extend([
                        "--fallback-label", self._fallback_label or "fallback",
                    ])

            result = self._run_worker(cmd, json_out)
        finally:
            try:
                os.unlink(cookie_file)
            except OSError:
                pass

        # Log which account was used
        cookie_used = result.get("cookie_used", "")
        if cookie_used:
            logger.info("Pollo image generated with account: %s", cookie_used)

        # Check and create alerts for exhausted accounts
        if self._db is not None:
            try:
                _check_and_alert_pollo_credits(self._db)
            except Exception as exc:
                logger.debug("Pollo alert check skipped: %s", exc)

        img_path = result.get("image_path") or ""
        if not result.get("ok") or not img_path or not Path(img_path).exists():
            raise PolloAIError(result.get("error") or "Worker returned no image.")
        logger.info("Pollo image generated: %s", img_path)
        return Path(img_path)

    def generate_batch(
        self,
        prompts: list[str],
        output_dir: Path,
        prefix: str = "pollo_",
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    ) -> list[Path]:
        """Generate one image per prompt (prompts are distinct variants).

        Returns a list of saved Path objects (may be shorter than prompts on
        partial failure).
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        results: list[Path] = []

        for i, prompt in enumerate(prompts):
            out_path = output_dir / f"{prefix}{i + 1:02d}.jpg"
            try:
                path = self.generate(prompt, out_path, aspect_ratio=aspect_ratio)
                results.append(path)
                logger.info("Batch image %d/%d saved: %s", i + 1, len(prompts), path)
            except PolloAIError as exc:
                logger.error("Batch image %d/%d failed: %s", i + 1, len(prompts), exc)

        return results

    # ── Internal ──────────────────────────────────────────────

    def _write_cookie_file(self) -> str:
        """Persist the selected cookies in a 0600 temp file for the worker."""
        payload = {
            "cookie": self._primary_cookie,
            "account_label": self._primary_label or "primary",
        }
        if self._fallback_cookie:
            payload["fallback_cookie"] = self._fallback_cookie
            payload["fallback_label"] = self._fallback_label or "fallback"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        fd, path = tempfile.mkstemp(prefix="pollo_cookies_", suffix=".json")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    def _run_worker(self, cmd: list[str], json_out: Path) -> dict:
        """Run the worker subprocess and return its parsed JSON result."""
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise PolloAIError(f"Pollo worker timed out after {SUBPROCESS_TIMEOUT}s")
        except Exception as exc:
            raise PolloAIError(f"Failed to launch Pollo worker: {exc}")

        # Prefer the JSON result file; fall back to stdout.
        data: dict | None = None
        if json_out.exists():
            try:
                data = json.loads(json_out.read_text())
            except Exception:
                data = None
        if data is None and proc.stdout.strip():
            try:
                data = json.loads(proc.stdout.strip())
            except Exception:
                data = None

        if not isinstance(data, dict):
            stderr = (proc.stderr or "").strip()
            raise PolloAIError(
                "Pollo worker produced no readable JSON. "
                f"exit={proc.returncode} stderr={stderr[:300]}"
            )
        return data


# ═══════════════════════════════════════════════════════════
# SceneImageGenerator — AI image generation for video scenes
# ═══════════════════════════════════════════════════════════

class SceneImageGenerator:
    """Generate AI images for video scenes using Pollo AI.

    Unlike ``AIImageGenerator`` (thumbnail-focused, fast-fail), this class is
    designed for generating background/scene images with rich theming context.
    It caches results by prompt hash to avoid expensive regenerations (~7 min
    per image) and degrades gracefully (returns ``None`` instead of raising).
    """

    def __init__(
        self,
        session_cookie: str = "",
        cache_dir: str = "output/ai_scenes/",
        model: str = "",
        db: object = None,
    ) -> None:
        self._cookie = session_cookie or os.getenv("POLLO_SESSION_COOKIE", "") or _read_cookie()
        self._model = model or os.getenv("POLLO_IMAGE_MODEL", DEFAULT_MODEL)
        self._db = db
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._generator: Optional[AIImageGenerator] = None

    # ── Public API ──────────────────────────────────────────

    def generate_scene_image(
        self,
        description: str,
        theme: "Optional[ThemeContext]" = None,
        style: str = "cinematic",
    ) -> Optional[Path]:
        """Generate an AI image for a video scene.

        Args:
            description: What the scene should show.
            theme: ThemeContext for enriched prompting.
            style: Style hint for the model (currently unused, reserved).

        Returns:
            Path to generated image, or None on failure.
        """
        # Build prompt
        if theme:
            prompt = theme.to_pollo_prompt(description)
        else:
            prompt = (
                f"{description}, cinematic photography, 16:9, "
                "professional quality, no text, no watermark"
            )
        prompt = prompt[:MAX_PROMPT_CHARS]

        # Cache key includes the prompt and the model: the same prompt rendered
        # by a different model is a different image.
        cache_key = hashlib.md5(f"{self._model}|{prompt}".encode()).hexdigest()[:12]
        # The generator writes .jpg; checking only .png here made the cache
        # never hit and re-spent credits on every repeated scene.
        for suffix in (".jpg", ".jpeg", ".png", ".webp"):
            cache_path = self.cache_dir / f"{cache_key}{suffix}"
            if cache_path.exists():
                logger.info("Using cached scene image: %s", cache_path)
                return cache_path

        # Generate via worker subprocess
        try:
            img_path = self._call_pollo_api(prompt, cache_key)
            if img_path and img_path.exists():
                logger.info("Scene image generated: %s", img_path)
                return img_path
        except PolloAIError as exc:
            logger.error("Pollo AI scene generation failed: %s", exc)
        except Exception as exc:
            logger.error("Unexpected error in scene generation: %s", exc)

        return None

    # ── Internal ────────────────────────────────────────────

    def _call_pollo_api(self, prompt: str, cache_key: str) -> Optional[Path]:
        """Invoke the Pollo AI worker and return the output path."""
        # Allow dual-cookie accounts even when legacy cookie is empty
        has_accounts = bool(_read_pollo_accounts())
        if not self._cookie and not has_accounts:
            logger.warning("Pollo AI cookie not configured — skipping scene generation")
            return None

        if self._generator is None:
            try:
                self._generator = AIImageGenerator(
                    cookie_value=self._cookie if not _read_pollo_accounts() else None,
                    model=self._model,
                    db=self._db,
                )
            except PolloAIError as exc:
                logger.warning("Cannot init AIImageGenerator: %s", exc)
                return None

        output_path = self.cache_dir / f"{cache_key}.jpg"
        return self._generator.generate(
            prompt=prompt,
            output_path=output_path,
            aspect_ratio=DEFAULT_ASPECT_RATIO,
        )
