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
import time
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


def _select_pollo_account_pair() -> tuple:
    """Select a random primary account (with credits available) + a different
    active account as fallback.

    Returns:
        (primary_cookie, primary_label, fallback_cookie, fallback_label)
        or ("single_cookie", "legacy", None, None) if pollo_accounts[] is empty.

    If only one active account exists, fallback values are None.
    """
    accounts = _read_pollo_accounts()
    if not accounts:
        # Fall back to legacy single-cookie mode
        cookie = _read_cookie()
        return (cookie, "legacy", None, None) if cookie else (None, None, None, None)

    status = _read_pollo_status()

    # Filter active accounts (credits not exhausted)
    active = []
    for acc in accounts:
        label = str(acc.get("label", "")).strip()
        cookie = str(acc.get("cookie", "")).strip()
        if not label or not cookie:
            continue
        acc_status = status.get(label, {})
        if not acc_status.get("credits_exhausted", False):
            active.append({"label": label, "cookie": cookie})

    if not active:
        # All accounts exhausted — return first account anyway (worker will report error)
        first = accounts[0]
        label = str(first.get("label", "primary")).strip() or "primary"
        cookie = str(first.get("cookie", "")).strip()
        return (cookie, label, None, None)

    # Shuffle to randomize selection
    random.shuffle(active)

    primary = active[0]
    primary_cookie = primary["cookie"]
    primary_label = primary["label"]

    # Pick fallback: first different active account
    fallback_cookie = None
    fallback_label = None
    for acc in active[1:]:
        if acc["label"] != primary_label:
            fallback_cookie = acc["cookie"]
            fallback_label = acc["label"]
            break

    # If no different active account, try any account (even exhausted) as fallback
    if not fallback_cookie:
        for acc in accounts:
            candidate_label = str(acc.get("label", "")).strip()
            candidate_cookie = str(acc.get("cookie", "")).strip()
            if candidate_label and candidate_label != primary_label and candidate_cookie:
                fallback_cookie = candidate_cookie
                fallback_label = candidate_label
                break

    logger.info(
        "Pollo accounts: primary=%s fallback=%s active=%d",
        primary_label, fallback_label or "none", len(active),
    )
    return (primary_cookie, primary_label, fallback_cookie, fallback_label)


def _check_and_alert_pollo_credits(db) -> None:
    """Read the shared status file and create pipeline_alerts for any exhausted
    Pollo accounts that haven't been alerted yet.

    Uses an in-memory 'alerted' flag (stored in the status file under each
    account's ``alerted`` key) to prevent duplicate alerts.

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

    changed = False

    for acc in accounts:
        label = str(acc.get("label", "")).strip()
        if not label:
            continue

        acc_status = status.get(label, {})
        credits_exhausted = acc_status.get("credits_exhausted", False)
        already_alerted = acc_status.get("alerted", False)

        if credits_exhausted and not already_alerted:
            create_alert(
                db,
                entity_type="system",
                alert_type="pollo_credits_exhausted",
                severity="warning",
                title=f"Pollo.ai: cuenta {label} sin créditos",
                message=(
                    f"Se agotaron los créditos de la cuenta {label} en Pollo.ai. "
                    "Se usará otra cuenta como fallback si está disponible."
                ),
                metadata={"account": label},
            )
            # Mark as alerted in status
            if label not in status:
                status[label] = {}
            status[label]["alerted"] = True
            status[label]["alerted_at"] = time.strftime(
                "%Y-%m-%d %H:%M:%S UTC", time.gmtime()
            )
            changed = True
            logger.info("Alert created for exhausted Pollo account: %s", label)

    # Clear alerted flags for accounts that recovered credits
    for label, info in list(status.items()):
        if not info.get("credits_exhausted") and info.get("alerted"):
            del info["alerted"]
            del info["alerted_at"]
            changed = True

    if changed:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(POLLO_STATUS_FILE)), exist_ok=True)
            with open(POLLO_STATUS_FILE, "w") as fh:
                json.dump(status, fh, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning("Failed to write alert status to %s: %s", POLLO_STATUS_FILE, exc)


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
        cmd = [
            sys.executable, str(WORKER_PATH), "generate",
            "--cookie", self._primary_cookie,
            "--account-label", self._primary_label or "primary",
            "--prompt", prompt,
            "--model", self._model,
            "--aspect-ratio", aspect_ratio or DEFAULT_ASPECT_RATIO,
            "--num-outputs", "1",
            "--output-image", str(output_path),
            "--output-json", str(json_out),
            "--timeout", str(WORKER_TIMEOUT),
        ]

        # Add fallback args in dual-cookie mode
        if self._use_dual_cookie and self._fallback_cookie:
            cmd.extend([
                "--fallback-cookie", self._fallback_cookie,
                "--fallback-label", self._fallback_label or "fallback",
                "--status-file", POLLO_STATUS_FILE,
            ])

        result = self._run_worker(cmd, json_out)

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

        # Check cache (hash the prompt)
        cache_key = hashlib.md5(prompt.encode()).hexdigest()[:12]
        cache_path = self.cache_dir / f"{cache_key}.png"
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
