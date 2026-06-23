"""AI image generation via Pollo AI (tRPC web endpoint, cookie auth).

Replaces the old platform/x-api-key integration with the proven cookie-based
tRPC approach from the lamami publicista worker.  Uses curl_cffi to bypass
Cloudflare and authenticates with __Secure-next-auth.session-token.

Auth:   session cookie (obtained from lamami control panel or .env override).
Model:  pollo-image-v2 (configurable).
Flow:   text2Image.create → poll generation.queryRecordDetail →
        resolve noWatermarkUrl → download.
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── HTTP backend ─────────────────────────────────────────────────
try:
    from curl_cffi import requests as cffi_requests
    HTTP_BACKEND = "curl-cffi"
except ImportError:
    HTTP_BACKEND = "none"

BASE_URL = "https://pollo.ai/api/trpc"
POLL_INTERVAL = 4          # seconds between status checks
MAX_POLL_TIME = 300        # timeout seconds
INTER_GENERATION_DELAY = 5.0  # seconds between sequential generations
MAX_PROMPT_CHARS = 2000

COMMON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://pollo.ai/app/ai-image",
    "Origin": "https://pollo.ai",
    "x-trpc-source": "nextjs-react",
}

# ── Model registry ───────────────────────────────────────────────
MODEL_CONFIG: dict[str, dict] = {
    "pollo-image-v2": {
        "name": "Pollo Image v2 (default)",
        "modelName": "pollo-image-v2",
        "aspectRatio": "1:1",
    },
    "pollo-image-v1-6": {
        "name": "Pollo Image v1.6",
        "modelName": "pollo-image-v1-6",
        "aspectRatio": "1:1",
    },
    "flux-dev": {
        "name": "FLUX Dev (Black Forest Labs)",
        "modelName": "flux-dev",
        "aspectRatio": "2:3",
    },
    "seedream": {
        "name": "Seedream (ByteDance)",
        "modelName": "seedream",
        "aspectRatio": "2:3",
    },
    "nano-banana": {
        "name": "Nano Banana (Google Gemini)",
        "modelName": "nano-banana",
        "aspectRatio": "4:3",
    },
}

DEFAULT_MODEL = "pollo-image-v2"
DEFAULT_ASPECT_RATIO = "16:9"


class PolloAIError(Exception):
    """Raised when Pollo AI generation fails."""


# ── Cookie resolution ────────────────────────────────────────────

def _read_cookie() -> str:
    """Resolve Pollo session cookie from env override or lamami settings.json."""
    # 1) Explicit override
    override = os.getenv("POLLO_SESSION_COOKIE", "").strip()
    if override:
        return _extract_cookie_value(override)

    # 2) Read from lamami shared settings
    LAMAMI_SETTINGS_PATH = "/root/lamamionline-control/data/settings.json"
    try:
        with open(LAMAMI_SETTINGS_PATH, "r") as fh:
            data = json.load(fh)
        raw = str(data.get("pollo_session_cookie", "")).strip()
        if raw:
            return _extract_cookie_value(raw)
    except Exception:
        pass

    return ""


def _extract_cookie_value(cookie_str: str) -> str:
    """Strip the known NextAuth cookie prefix if present."""
    prefix = "__Secure-next-auth.session-token="
    if cookie_str.startswith(prefix):
        return cookie_str[len(prefix):]
    return cookie_str


# ── Public API ────────────────────────────────────────────────────

class AIImageGenerator:
    """Generate images via Pollo AI (tRPC + session cookie).

    Usage::

        gen = AIImageGenerator(model="pollo-image-v2")
        path = gen.generate("dramatic dark corridor", Path("output/thumb.jpg"))
        paths = gen.generate_batch(["prompt A", "prompt B"], Path("output/"))
    """

    def __init__(
        self,
        api_key: Optional[str] = None,          # ignored, kept for compat
        model: Optional[str] = None,
        cookie_value: Optional[str] = None,
    ) -> None:
        if HTTP_BACKEND == "none":
            raise PolloAIError(
                "curl_cffi not installed. Run: pip install curl-cffi"
            )

        self._model = model or os.getenv("POLLO_IMAGE_MODEL", DEFAULT_MODEL)
        if self._model not in MODEL_CONFIG:
            raise PolloAIError(
                f"Unknown model {self._model!r}. Available: {list(MODEL_CONFIG)}"
            )
        self._model_cfg = MODEL_CONFIG[self._model]

        self._cookie = cookie_value or _read_cookie()
        if not self._cookie:
            raise PolloAIError(
                "Pollo session cookie not set. Set POLLO_SESSION_COOKIE in .env "
                "or ensure /root/lamamionline-control/data/settings.json has "
                "a valid pollo_session_cookie field."
            )

        self._session = self._make_client()
        logger.info("AIImageGenerator initialized (model=%s backend=%s)",
                     self._model, HTTP_BACKEND)

    def generate(
        self,
        prompt: str,
        output_path: Path,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    ) -> Path:
        """Generate a single image and save to *output_path*.

        Returns the resolved output path.  Raises PolloAIError on failure.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if len(prompt) > MAX_PROMPT_CHARS:
            raise PolloAIError(
                f"Prompt exceeds {MAX_PROMPT_CHARS} chars ({len(prompt)}). Truncate it."
            )

        gen_id = self._create_generation(prompt, aspect_ratio, num_outputs=1)
        result = self._poll_generation(gen_id, timeout=MAX_POLL_TIME, expected_outputs=1)
        items = self._extract_output_items(result)
        if not items:
            raise PolloAIError("No output items in generation result")
        url = self._get_nowatermark_url(items[0], gen_id)
        return self._download_image(url, output_path)

    def generate_batch(
        self,
        prompts: list[str],
        output_dir: Path,
        prefix: str = "pollo_",
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    ) -> list[Path]:
        """Generate multiple images sequentially (respects rate limits).

        Images are saved as ``{output_dir}/{prefix}01.jpg`` etc.
        Returns a list of saved Path objects.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        results: list[Path] = []

        for i, prompt in enumerate(prompts):
            if i > 0:
                logger.info("Waiting %.1fs before next generation...", INTER_GENERATION_DELAY)
                time.sleep(INTER_GENERATION_DELAY)

            filename = f"{prefix}{i + 1:02d}.jpg"
            output_path = output_dir / filename

            try:
                path = self.generate(prompt, output_path, aspect_ratio=aspect_ratio)
                results.append(path)
                logger.info("Batch image %d/%d saved: %s", i + 1, len(prompts), path)
            except PolloAIError as exc:
                logger.error("Batch image %d/%d failed: %s", i + 1, len(prompts), exc)

        return results

    # ── Internal: client ──────────────────────────────────────

    def _make_client(self):
        """Build curl_cffi session with cookie + common headers."""
        session = cffi_requests.Session(impersonate="chrome110")
        session.headers.update(COMMON_HEADERS)
        session.cookies.set(
            "__Secure-next-auth.session-token",
            self._cookie,
            domain="pollo.ai",
            secure=True,
        )
        return session

    # ── Internal: tRPC calls ──────────────────────────────────

    def _create_generation(
        self, prompt: str, aspect_ratio: str, num_outputs: int = 1
    ) -> str:
        """POST text2Image.create → return generation ID."""
        url = BASE_URL + "/text2Image.create?batch=1"
        payload = {
            "prompt": prompt,
            "modelName": self._model_cfg["modelName"],
            "aspectRatio": aspect_ratio or self._model_cfg["aspectRatio"],
            "entryCode": "web",
            "numOutputs": int(num_outputs),
        }
        body = {"0": {"json": payload}}

        logger.info("Creating Pollo AI task (model=%s, prompt=%r...)",
                     self._model, prompt[:80])

        resp = self._session.post(url, json=body, timeout=30)

        if resp.status_code == 401:
            raise PolloAIError(
                "Session expired (401). Renew the cookie in the lamami control panel "
                "(Josue > ConfigM → Pollo session cookie)."
            )
        if resp.status_code == 403:
            raise PolloAIError(
                "Access denied (403). curl_cffi could not bypass Cloudflare."
            )
        if resp.status_code not in (200, 201):
            raise PolloAIError(
                f"HTTP {resp.status_code}: {resp.text[:600]}"
            )

        inner = self._parse_trpc(resp, "text2Image.create")
        gen_id = inner.get("id") or inner.get("generationId")
        if not gen_id:
            raise PolloAIError(
                f"No generation ID in response: {json.dumps(inner)[:400]}"
            )
        logger.info("Pollo AI task created: %s", gen_id)
        return str(gen_id)

    def _poll_generation(
        self, gen_id: str, timeout: int = 300, expected_outputs: int = 1
    ) -> dict:
        """Poll generation.queryRecordDetail until complete. Returns the result dict."""
        params = _compact_json({"0": {"json": {"id": int(gen_id)}}})
        import urllib.parse
        encoded = urllib.parse.quote(params)
        url = BASE_URL + "/generation.queryRecordDetail?batch=1&input=" + encoded

        elapsed = 0
        last_success = None

        while elapsed < timeout:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            try:
                resp = self._session.get(url, timeout=15)
            except Exception:
                continue

            if resp.status_code != 200:
                continue

            try:
                inner = self._parse_trpc(resp, "generation.queryRecordDetail")
            except Exception:
                continue

            status = str(inner.get("status") or inner.get("state") or "").lower()

            if status in ("succeed", "success", "completed", "done", "finished"):
                outputs = self._extract_output_items(inner)
                if expected_outputs <= 1 or len(outputs) >= expected_outputs:
                    return inner
                last_success = inner
                continue

            if status in ("failed", "error", "cancelled"):
                reason = str(inner.get("failReason") or inner.get("error") or "unknown")
                raise PolloAIError(f"Generation failed: {reason}")

        if last_success is not None:
            return last_success

        raise PolloAIError(f"Timeout after {timeout}s waiting for image.")

    def _get_nowatermark_url(
        self, item: dict, generation_id: Optional[str] = None
    ) -> str:
        """Extract the watermark-free download URL (faithfully ported from lamami).

        Order:
        1. Explicit no-watermark keys on the item (noWatermarkUrl, originalUrl, …).
        2. Actively resolve via video.getVideoNoWatermarkUrl endpoint (specific payloads).
        3. Base URLs as last resort (imageUrl, url, cover, thumbnail).
        4. Generic payloads (id-only) as final attempt.
        """
        # 1) Explicit no-watermark keys
        explicit = self._collect_values_for_keys(
            item,
            {"noWatermarkUrl", "withoutWatermarkUrl", "originalUrl",
             "oriUrl", "originUrl", "downloadUrl"},
            limit=20,
        )
        for vals in explicit.values():
            for val in vals:
                if isinstance(val, str) and val.startswith(("http://", "https://")):
                    return _add_original_download_query(val)

        # 2) Resolve via getVideoNoWatermarkUrl endpoint (specific payloads)
        specific, generic = self._build_nowatermark_payload_groups(item, generation_id)
        for payload in specific:
            try:
                resp = self._session.post(
                    BASE_URL + "/video.getVideoNoWatermarkUrl?batch=1",
                    json=payload,
                    timeout=20,
                )
                if resp.status_code == 200:
                    inner = self._parse_trpc(resp, "getVideoNoWatermarkUrl")
                    url = self._find_first_url(inner)
                    if url:
                        return _add_original_download_query(url)
            except Exception:
                continue

        # 3) Base URLs (last resort before generic)
        base = self._collect_values_for_keys(
            item,
            {"videoUrl", "imageUrl", "url", "cover", "thumbnail"},
            limit=20,
        )
        for vals in base.values():
            for val in vals:
                if isinstance(val, str) and val.startswith(("http://", "https://")):
                    return _add_original_download_query(val)

        # 4) Generic payloads (id-only) — final attempt
        for payload in generic:
            try:
                resp = self._session.post(
                    BASE_URL + "/video.getVideoNoWatermarkUrl?batch=1",
                    json=payload,
                    timeout=20,
                )
                if resp.status_code == 200:
                    inner = self._parse_trpc(resp, "getVideoNoWatermarkUrl")
                    url = self._find_first_url(inner)
                    if url:
                        return _add_original_download_query(url)
            except Exception:
                continue

        raise PolloAIError("No valid image URL found in generation result.")

    def _download_image(self, url: str, output_path: Path) -> Path:
        """Download image from URL and save to *output_path*. Skips if cached."""
        if output_path.exists() and output_path.stat().st_size > 0:
            logger.info("Image already cached: %s", output_path)
            return output_path

        logger.info("Downloading: %s", url[:100])
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0.0.0",
            "Referer": "https://pollo.ai/",
        }

        resp = self._session.get(url, headers=headers, timeout=120)
        if resp.status_code != 200:
            raise PolloAIError(f"Download HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.content

        if not data or len(data) < 1000:
            raise PolloAIError(f"Downloaded data too small ({len(data)} bytes).")

        # Validate magic bytes
        head = data[:16]
        is_image = (
            head[:8] == b'\x89PNG\r\n\x1a\n'
            or head[:3] == b'\xff\xd8\xff'
            or (head[:4] == b'RIFF' and head[8:12] == b'WEBP')
            or head[:6] in (b'GIF87a', b'GIF89a')
        )
        if not is_image and len(data) < 5000:
            raise PolloAIError(
                f"Downloaded bytes don't look like an image: {head[:20].hex()}"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
        logger.info("Downloaded: %s (%d bytes)", output_path, len(data))
        return output_path

    # ── tRPC helpers ──────────────────────────────────────────

    @staticmethod
    def _parse_trpc(resp, context: str) -> dict:
        """Parse the nested tRPC batch response structure."""
        try:
            data = resp.json()
        except Exception:
            raise PolloAIError(
                f"Response is not JSON in {context}: {resp.text[:400]}"
            )

        if isinstance(data, list) and len(data) > 0:
            try:
                return data[0]["result"]["data"]["json"]
            except (KeyError, IndexError):
                pass
            try:
                return data[0]["result"]["data"]
            except (KeyError, IndexError):
                pass
        if isinstance(data, dict):
            return data

        raise PolloAIError(
            f"Unexpected tRPC structure in {context}: {str(data)[:400]}"
        )

    @staticmethod
    def _find_first_url(obj) -> str:
        """Recursively find the first http(s) URL in a nested structure."""
        if isinstance(obj, str) and obj.startswith(("http://", "https://")):
            return obj
        if isinstance(obj, dict):
            preferred = [
                "noWatermarkUrl", "withoutWatermarkUrl", "originalUrl",
                "oriUrl", "originUrl", "downloadUrl", "videoUrl",
                "imageUrl", "url", "cover", "thumbnail", "src",
            ]
            for key in preferred:
                val = obj.get(key)
                if isinstance(val, str) and val.startswith(("http://", "https://")):
                    return val
                if isinstance(val, list):
                    for item in val:
                        u = AIImageGenerator._find_first_url(item)
                        if u:
                            return u
            for val in obj.values():
                u = AIImageGenerator._find_first_url(val)
                if u:
                    return u
        if isinstance(obj, list):
            for item in obj:
                u = AIImageGenerator._find_first_url(item)
                if u:
                    return u
        return ""

    @staticmethod
    def _extract_output_items(result: dict) -> list[dict]:
        """Extract output items from a poll result."""
        candidates = []
        for key in ("generations", "outputs", "results", "images", "assets",
                     "records", "imageList", "videoList", "items", "medias"):
            items = result.get(key) if isinstance(result, dict) else None
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        candidates.append(item)
                    elif isinstance(item, str):
                        candidates.append({"url": item})
        if not candidates:
            direct = AIImageGenerator._find_first_url(result)
            if direct:
                candidates.append({"url": direct})
        return candidates

    @staticmethod
    def _build_nowatermark_payload_groups(
        item: dict, generation_id: Optional[str] = None
    ) -> tuple[list[dict], list[dict]]:
        """Build specific and generic payload sets for getVideoNoWatermarkUrl.

        Returns (specific_payloads, generic_payloads).
        """
        specific: list[dict] = []
        generic: list[dict] = []
        seen_specific: set[str] = set()
        seen_generic: set[str] = set()

        id_keys = [
            "videoId", "id", "generationId", "recordId", "assetId",
            "taskId", "projectId", "resourceId", "mediaId",
        ]
        url_keys = ["videoUrl", "imageUrl", "url", "cover", "thumbnail", "downloadUrl"]

        def _push(target: list[dict], seen: set[str], payload: dict) -> None:
            serial = _compact_json(payload)
            if serial in seen:
                return
            seen.add(serial)
            target.append(payload)

        values = AIImageGenerator._collect_values_for_keys(
            item or {}, set(id_keys + url_keys), limit=60,
        )

        for key in id_keys:
            for val in values.get(key, []):
                _push(specific, seen_specific, {"0": {"json": {key: val}}})
                if generation_id not in (None, ""):
                    _push(specific, seen_specific,
                          {"0": {"json": {key: val, "generationId": generation_id}}})
                    _push(specific, seen_specific,
                          {"0": {"json": {key: val, "id": generation_id}}})

        for key in url_keys:
            for val in values.get(key, []):
                _push(specific, seen_specific, {"0": {"json": {key: val}}})
                if generation_id not in (None, ""):
                    _push(specific, seen_specific,
                          {"0": {"json": {key: val, "generationId": generation_id}}})

        if generation_id not in (None, ""):
            _push(generic, seen_generic, {"0": {"json": {"id": generation_id}}})
            _push(generic, seen_generic, {"0": {"json": {"generationId": generation_id}}})
            _push(generic, seen_generic, {"0": {"json": {"videoId": generation_id}}})

        return specific, generic

    @staticmethod
    def _collect_values_for_keys(
        obj, wanted_keys: set, _found: dict | None = None, limit: int = 40,
    ) -> dict:
        """Recursively collect values for *wanted_keys* from a nested dict/list.

        Returns a dict mapping key → list of unique values found.
        """
        if _found is None:
            _found = {}
        if len(_found) >= limit:
            return _found
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in wanted_keys and value not in (None, ""):
                    bucket = _found.setdefault(key, [])
                    if value not in bucket:
                        bucket.append(value)
                AIImageGenerator._collect_values_for_keys(value, wanted_keys, _found, limit)
        elif isinstance(obj, list):
            for item in obj:
                AIImageGenerator._collect_values_for_keys(item, wanted_keys, _found, limit)
        return _found


# ── Helpers ───────────────────────────────────────────────────────

def _compact_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _add_original_download_query(url: str) -> str:
    if not url:
        return ""
    if "type=download" in url and "format=original" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}type=download&quality=high&format=original"
