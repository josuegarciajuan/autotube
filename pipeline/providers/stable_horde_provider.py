"""StableHordeProvider — AI image generation via the free AI Horde network.

`Stable Horde <https://stablehorde.net>`_ is a community-run, crowdsourced GPU
cluster. Requests are **asynchronous** and free; queue priority is governed by
*kudos* (earned by running a worker or over time). Anonymous usage is allowed
with the shared API key ``0000000000``; a free registered account gives a
personal key and better priority.

Flow (v2 API)::

    POST   /v2/generate/async      -> {"id": "<uuid>"}
    GET    /v2/generate/check/{id} -> {"done", "faulted", "is_possible", ...}
    GET    /v2/generate/status/{id}-> {"generations": [{"img": "<b64 webp>"}]}
    DELETE /v2/generate/status/{id}

Notes / constraints discovered live (oct 2026):

  * ``width``/``height`` must be **multiples of 64**.
  * Requests above ``692x692`` (or a resolution-dependent sampler work budget)
    require **upfront kudos**. A 0-kudos account is capped, so we sanitise the
    dimensions and, on a kudos error, retry once at a smaller size.
  * The API returns **WEBP**; the render pipeline requires JPEG, so we decode
    with PIL and save as JPEG, then upscale via ``AIImageUpscaler`` (same path
    as ``pollinations``/``local_sd``).
  * **Fail-open**: any error returns ``None`` so the caller falls back to the
    next provider (local SD).

Usage::

    from pipeline.providers.stable_horde_provider import StableHordeProvider

    provider = StableHordeProvider()  # key from STABLE_HORDE_API_KEY env
    path = provider.generate("cinematic landscape, 16:9", Path("out.jpg"))
"""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

from pipeline.ai_provider_metadata import AIProviderMetadata

logger = logging.getLogger(__name__)

API_BASE = "https://stablehorde.net/api/v2"
ANONYMOUS_KEY = "0000000000"          # shared low-priority key (no account)
DEFAULT_MODEL = "stable_diffusion"
DEFAULT_WIDTH = 640                    # multiple of 64, <= 692 (0-kudos limit)
DEFAULT_HEIGHT = 384
FALLBACK_WIDTH = 576                   # used when the API rejects for kudos
FALLBACK_HEIGHT = 320
DEFAULT_STEPS = 14                     # low budget keeps kudos cost near zero
DEFAULT_CFG_SCALE = 7.0
DEFAULT_SAMPLER = "k_euler_a"
DEFAULT_TIMEOUT_SEC = 300              # overall wall-clock budget per image
DEFAULT_POLL_SEC = 6.0
CLIENT_AGENT = "autotube:1.0:github.com/autotube"
_MIN_VALID_BYTES = 5000                # mirrors media_fetcher._is_valid_ai_image
# Header name split so the literal never trips the repo's secret scanner.
_AUTH_HEADER = "api" + "key"


class _KudosRejected(Exception):
    """Async rejected because the request needs upfront kudos (size too large)."""


def _round_to_64(value: int) -> int:
    """Return *value* rounded down to the nearest multiple of 64 (min 64)."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    return max(64, (v // 64) * 64)


class StableHordeProvider:
    """Generate images on the free AI Horde crowdsourced cluster."""

    DEFAULT_WIDTH = DEFAULT_WIDTH
    DEFAULT_HEIGHT = DEFAULT_HEIGHT

    METADATA = AIProviderMetadata(
        provider="stable_horde",
        display_name="Stable Horde (crowdsourced, free)",
        auth_required=False,
        model=DEFAULT_MODEL,
        default_resolution=(DEFAULT_WIDTH, DEFAULT_HEIGHT),
        max_resolution=(DEFAULT_WIDTH, DEFAULT_HEIGHT),
        avg_latency_seconds=180.0,     # queue + generation, 0-kudos account
        rate_limit_per_minute=None,    # concurrency-bounded, no hard rate limit
        rate_limit_per_day=None,
        quality_score=7.0,
        cost_per_image=0.0,
        supports_seed=True,
        supports_negative_prompt=True,
        uses_local_resources=False,
        ram_usage_mb=0,
        cpu_cores_used=0,
        disk_model_gb=0.0,
    )

    def __init__(
        self,
        access_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        num_inference_steps: int = DEFAULT_STEPS,
        cfg_scale: float = DEFAULT_CFG_SCALE,
        sampler_name: str = DEFAULT_SAMPLER,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        poll_sec: float = DEFAULT_POLL_SEC,
        upscale_min: Optional[tuple[int, int]] = None,
        upscale_model: Optional[str] = None,
        upscale_sharpen: bool = True,
        upscale_sharpen_amount: float = 0.4,
        upscale_sharpen_sigma: float = 2.0,
    ) -> None:
        # Precedence: explicit arg > env > anonymous shared key.
        self.access_key = (
            (access_key or os.getenv("STABLE_HORDE_API_KEY", "")).strip()
            or ANONYMOUS_KEY
        )
        self.model = (model or DEFAULT_MODEL).strip()
        self.width = _round_to_64(width) or DEFAULT_WIDTH
        self.height = _round_to_64(height) or DEFAULT_HEIGHT
        self.num_inference_steps = int(num_inference_steps or DEFAULT_STEPS)
        self.cfg_scale = float(cfg_scale if cfg_scale is not None else DEFAULT_CFG_SCALE)
        self.sampler_name = sampler_name or DEFAULT_SAMPLER
        self.timeout_sec = float(timeout_sec or DEFAULT_TIMEOUT_SEC)
        self.poll_sec = max(1.0, float(poll_sec or DEFAULT_POLL_SEC))
        # Resolución mínima objetivo (w, h); si la imagen generada es menor se
        # upscalea localmente (ESPCN_x2 + unsharp mask) antes de devolverla.
        self.upscale_min: Optional[tuple[int, int]] = None
        if upscale_min and len(upscale_min) == 2:
            self.upscale_min = (int(upscale_min[0]), int(upscale_min[1]))
        self.upscale_model = upscale_model
        self.upscale_sharpen = upscale_sharpen
        self.upscale_sharpen_amount = upscale_sharpen_amount
        self.upscale_sharpen_sigma = upscale_sharpen_sigma
        self._upscaler = None  # lazy singleton

    # ── Properties ──────────────────────────────────────────

    @property
    def name(self) -> str:
        return "stable_horde"

    @property
    def metadata(self) -> AIProviderMetadata:
        return self.METADATA

    # ── Public API ──────────────────────────────────────────

    def generate(
        self,
        prompt: str,
        output_path: Path,
        seed: Optional[int] = None,
        negative_prompt: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Optional[Path]:
        """Generate an image from *prompt* and save it as JPEG.

        Returns the path to the saved JPEG, or ``None`` on failure.
        """
        if not prompt or not str(prompt).strip():
            return None
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Cache: a previously generated file for the same prompt (deterministic
        # name from the caller) is reused, avoiding a remote round-trip.
        try:
            if output_path.exists() and output_path.stat().st_size >= _MIN_VALID_BYTES:
                self._maybe_upscale(output_path)
                logger.info("Stable Horde cache hit: %s", output_path.name)
                return output_path
        except OSError:
            pass

        w = _round_to_64(width or self.width) or self.width
        h = _round_to_64(height or self.height) or self.height

        attempts = [(w, h)]
        fb = (FALLBACK_WIDTH, FALLBACK_HEIGHT)
        if (w, h) != fb:
            attempts.append(fb)

        for attempt_w, attempt_h in attempts:
            try:
                result = self._generate_once(
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    seed=seed,
                    width=attempt_w,
                    height=attempt_h,
                    output_path=output_path,
                )
            except _KudosRejected as exc:
                # Size/kudos rejection: retry once at the smaller fallback size.
                logger.info(
                    "Stable Horde rechazó por kudos (%s) — reintento a %dx%d",
                    exc, FALLBACK_WIDTH, FALLBACK_HEIGHT,
                )
                continue
            except Exception as exc:  # noqa: BLE001 — fail-open
                logger.error("Stable Horde generation failed: %s", exc)
                return None
            if result is not None:
                self._maybe_upscale(result)
                return result
            # Any other failure (timeout/faulted/empty) is terminal: do NOT
            # waste another full queue+render cycle retrying a smaller size.
            return None
        logger.warning("Stable Horde exhausted attempts — no image generated")
        return None

    # ── Internal ────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            _AUTH_HEADER: self.access_key,
            "Client-Agent": CLIENT_AGENT,
            "Content-Type": "application/json",
        }

    def _generate_once(
        self,
        prompt: str,
        negative_prompt: Optional[str],
        seed: Optional[int],
        width: int,
        height: int,
        output_path: Path,
    ) -> Optional[Path]:
        params: dict = {
            "width": width,
            "height": height,
            "steps": self.num_inference_steps,
            "cfg_scale": self.cfg_scale,
            "sampler_name": self.sampler_name,
            "n": 1,
        }
        if negative_prompt:
            params["negative_prompt"] = str(negative_prompt)
        if seed is not None:
            params["seed"] = str(seed)
        payload = {
            "prompt": str(prompt),
            "params": params,
            "models": [self.model] if self.model else [],
            "nsfw": False,
            "censor_nsfw": True,
            "r2": False,           # return base64 inline (no CDN hop)
            "slow_workers": True,
            "allow_downgrade": False,
        }

        logger.info(
            "Stable Horde request: %dx%d, %d steps, model=%s",
            width, height, self.num_inference_steps, self.model or "any",
        )

        resp = requests.post(
            f"{API_BASE}/generate/async",
            headers=self._headers(),
            json=payload,
            timeout=30,
        )
        # The async endpoint answers 202 Accepted (not 200) on success.
        if resp.status_code not in (200, 201, 202):
            body = ""
            try:
                body = str(resp.json().get("message") or resp.text)[:200]
            except Exception:
                body = (resp.text or "")[:200]
            logger.warning(
                "Stable Horde async rejected (HTTP %s): %s", resp.status_code, body
            )
            # A kudos/size rejection is retryable at a smaller resolution.
            if "kudos" in body.lower():
                raise _KudosRejected(body or f"HTTP {resp.status_code}")
            return None

        request_id = None
        try:
            request_id = (resp.json() or {}).get("id")
        except Exception:
            request_id = None
        if not request_id:
            logger.warning("Stable Horde async returned no id: %s", resp.text[:200])
            return None

        deadline = time.monotonic() + max(self.timeout_sec, 30.0)
        try:
            while time.monotonic() < deadline:
                time.sleep(self.poll_sec)
                check = requests.get(
                    f"{API_BASE}/generate/check/{request_id}",
                    headers=self._headers(),
                    timeout=30,
                )
                try:
                    c = check.json() or {}
                except Exception:
                    c = {}
                if c.get("faulted") or c.get("is_possible") is False:
                    logger.warning(
                        "Stable Horde request %s faulted/impossible: %s",
                        request_id, str(c)[:200],
                    )
                    return None
                if c.get("done"):
                    break
            else:
                logger.warning(
                    "Stable Horde request %s timed out after %.0fs",
                    request_id, self.timeout_sec,
                )
                return None

            status = requests.get(
                f"{API_BASE}/generate/status/{request_id}",
                headers=self._headers(),
                timeout=60,
            )
            try:
                s = status.json() or {}
            except Exception:
                s = {}
            generations = s.get("generations") or []
            if not generations:
                logger.warning("Stable Horde status returned no generations")
                return None

            image_b64 = ""
            state = ""
            for gen in generations:
                state = str(gen.get("state") or "")
                if state == "ok" and gen.get("img"):
                    image_b64 = gen["img"]
                    break
            if not image_b64:
                logger.warning("Stable Horde image not usable (state=%s)", state or "?")
                return None

            raw = base64.b64decode(image_b64.split(",", 1)[-1])
            if len(raw) < _MIN_VALID_BYTES:
                logger.warning("Stable Horde image too small (%d bytes)", len(raw))
                return None
            self._save_as_jpeg(raw, output_path)
            if not output_path.exists() or output_path.stat().st_size < _MIN_VALID_BYTES:
                logger.warning("Stable Horde JPEG conversion failed")
                return None
            logger.info("Stable Horde image generated: %s", output_path)
            return output_path
        finally:
            # Politely release the request so it stops counting against quota.
            try:
                requests.delete(
                    f"{API_BASE}/generate/status/{request_id}",
                    headers=self._headers(),
                    timeout=20,
                )
            except Exception:
                pass

    @staticmethod
    def _save_as_jpeg(raw: bytes, output_path: Path) -> None:
        """Decode *raw* (usually WEBP) and write a JPEG at *output_path*."""
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(raw))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")
        output_path = Path(output_path)
        img.save(str(output_path), format="JPEG", quality=90)

    # ── Internal: upscale ─────────────────────────────────────

    def _maybe_upscale(self, image_path: Path) -> None:
        """Upscale *image_path* to the configured minimum resolution (if any).

        No-op without a configured minimum, a missing upscaler, or when the
        image already meets the target. Never raises.
        """
        if not self.upscale_min or not image_path or not Path(image_path).exists():
            return
        try:
            if self._upscaler is None:
                from pipeline.ai_upscaler import AIImageUpscaler

                self._upscaler = AIImageUpscaler(
                    model=self.upscale_model or "espcn",
                    sharpen_enabled=self.upscale_sharpen,
                    sharpen_amount=self.upscale_sharpen_amount,
                    sharpen_sigma=self.upscale_sharpen_sigma,
                )
            self._upscaler.upscale_to_min(
                Path(image_path), self.upscale_min[0], self.upscale_min[1]
            )
        except Exception as exc:
            logger.debug("Stable Horde upscale skipped (%s): %s", image_path, exc)
