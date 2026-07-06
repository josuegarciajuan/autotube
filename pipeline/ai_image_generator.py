"""AI image generation via Pollo AI — delegates to the proven worker subprocess.

This module is a thin wrapper around ``tools/pollo_image_worker.py`` (copied
verbatim from the lamami "publicista" project, which is the reference working
integration). We invoke the worker exactly like publicista does — via CLI
subprocess — so behaviour is byte-identical to the production-proven path.

Auth: session cookie (env ``POLLO_SESSION_COOKIE`` override, else the shared
lamami ``settings.json``). The worker uses curl-cffi to impersonate Chrome and
bypass Cloudflare, polls the generation, and downloads the no-watermark image.

Public interface (unchanged, consumed by pipeline/thumbnail_maker.py):
    gen = AIImageGenerator(model="pollo-image-v2")
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
import subprocess
import sys
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


# ── Cookie resolution (same source as the publicista project) ────────

def _read_cookie() -> str:
    """Resolve Pollo session cookie from env override or lamami settings.json."""
    override = os.getenv("POLLO_SESSION_COOKIE", "").strip()
    if override:
        return override

    LAMAMI_SETTINGS_PATH = "/root/lamamionline-control/data/settings.json"
    try:
        with open(LAMAMI_SETTINGS_PATH, "r") as fh:
            data = json.load(fh)
        raw = str(data.get("pollo_session_cookie", "")).strip()
        if raw:
            return raw
    except Exception:
        pass
    return ""


class AIImageGenerator:
    """Generate images via Pollo AI by delegating to the worker subprocess."""

    def __init__(
        self,
        api_key: Optional[str] = None,   # ignored, kept for compat
        model: Optional[str] = None,
        cookie_value: Optional[str] = None,
    ) -> None:
        if not WORKER_PATH.exists():
            raise PolloAIError(
                f"Pollo worker not found at {WORKER_PATH}. "
                "Expected tools/pollo_image_worker.py."
            )

        self._model = model or os.getenv("POLLO_IMAGE_MODEL", DEFAULT_MODEL)
        self._cookie = cookie_value or _read_cookie()
        if not self._cookie:
            raise PolloAIError(
                "Pollo session cookie not set. Set POLLO_SESSION_COOKIE in .env "
                "or ensure /root/lamamionline-control/data/settings.json has a "
                "valid pollo_session_cookie field."
            )
        logger.info("AIImageGenerator initialized (model=%s, worker subprocess)", self._model)

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
            "--cookie", self._cookie,
            "--prompt", prompt,
            "--model", self._model,
            "--aspect-ratio", aspect_ratio or DEFAULT_ASPECT_RATIO,
            "--num-outputs", "1",
            "--output-image", str(output_path),
            "--output-json", str(json_out),
            "--timeout", str(WORKER_TIMEOUT),
        ]

        result = self._run_worker(cmd, json_out)
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
    ) -> None:
        self._cookie = session_cookie or os.getenv("POLLO_SESSION_COOKIE", "") or _read_cookie()
        self._model = model or os.getenv("POLLO_IMAGE_MODEL", DEFAULT_MODEL)
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
        if not self._cookie:
            logger.warning("Pollo AI cookie not configured — skipping scene generation")
            return None

        if self._generator is None:
            try:
                self._generator = AIImageGenerator(
                    cookie_value=self._cookie,
                    model=self._model,
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
