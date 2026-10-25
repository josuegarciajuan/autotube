"""P04-D/E — official archive providers (Wikimedia Commons, NASA).

Both providers use documented JSON APIs, never HTML scraping.  They return
plain dicts shaped like the existing stock-provider results so the P01
normaliser and the legacy downloader can consume them unchanged.

No facts are invented:

* ``public_domain`` is ``True`` only when the provider metadata explicitly
  says public domain / CC0; a known proprietary licence is ``False``;
  everything else stays ``None`` (UNKNOWN).
* an empty creator/date/category is kept empty.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

logger = logging.getLogger(__name__)

WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
NASA_API = "https://images-api.nasa.gov/search"

_PD_RE = re.compile(
    r"(public\s*domain|public-domain|cc0|creativecommons\.org/publicdomain|"
    r"no\s+known\s+copyright|pd-us|pd-old)",
    re.IGNORECASE,
)
_ATTRIBUTION_RE = re.compile(
    r"(cc[-\s]?by|attribution|share[-\s]?alike|gfdl|fal|fal-1\.3|art\s+licence)",
    re.IGNORECASE,
)
_HTML_RE = re.compile(r"<[^>]+>")


def _strip_html(value: Any) -> str:
    text = str(value or "")
    text = _HTML_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _norm_license(short_name: str, license_url: str = "", usage_terms: str = "") -> tuple[str, bool | None, bool | None]:
    """Return ``(license, public_domain, attribution_required)`` from metadata."""
    label = _strip_html(short_name) or _strip_html(usage_terms)
    url = str(license_url or "").strip()
    joined = f"{label} {url} {usage_terms}"
    if not label and not url:
        return "", None, None
    if _PD_RE.search(joined):
        return label or "Public domain", True, False
    if _ATTRIBUTION_RE.search(joined):
        return label or "Attribution required", False, True
    # A declared licence that is neither PD nor a recognised attribution
    # licence is still a known non-public-domain licence.
    return label or "UNKNOWN", False, True


@dataclass
class ProviderBudget:
    max_requests: int = 6
    page_size: int = 12
    timeout: float = 20.0
    cache_ttl: float = 3600.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_requests": self.max_requests,
            "page_size": self.page_size,
            "timeout": self.timeout,
            "cache_ttl": self.cache_ttl,
        }


class _HTTP:
    """Small shared HTTP helper with retries, rate handling and a TTL cache."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        retries: int = 2,
        cache_ttl: float = 3600.0,
        user_agent: str = "autotube-p04/1.0 (+https://josue.ink)",
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.cache_ttl = float(cache_ttl)
        self._cache: dict[str, tuple[float, Any]] = {}
        self.requests_made = 0
        self.bytes_received = 0
        try:
            self.session.headers.update({"User-Agent": user_agent})
        except Exception:
            pass

    def get_json(self, url: str, params: dict[str, Any]) -> Any:
        cache_key = url + "?" + json.dumps(params, sort_keys=True, ensure_ascii=False)
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached and now - cached[0] <= self.cache_ttl:
            return cached[1]
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                self.requests_made += 1
                if response.status_code in (429, 503):
                    retry_after = response.headers.get("Retry-After")
                    delay = min(float(retry_after or 2), 10.0)
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                self.bytes_received += len(response.content or b"")
                payload = response.json()
                self._cache[cache_key] = (time.time(), payload)
                return payload
            except Exception as exc:  # network/JSON — retry once, then fail
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"archive request failed: {last_error}")

    def get_bytes(self, url: str) -> bytes:
        response = self.session.get(url, timeout=self.timeout)
        self.requests_made += 1
        response.raise_for_status()
        data = response.content
        self.bytes_received += len(data or b"")
        return data


class ArchiveProvider:
    """Common download/cache contract for archive providers."""

    name = "archive"

    def __init__(
        self,
        *,
        budget: ProviderBudget | None = None,
        session: requests.Session | None = None,
        enabled: bool = True,
    ) -> None:
        self.budget = budget or ProviderBudget()
        self.enabled = bool(enabled)
        self.http = _HTTP(
            session=session,
            timeout=self.budget.timeout,
            cache_ttl=self.budget.cache_ttl,
        )
        self._cache_dir = Path("/tmp/autotube-p04-archive-cache")

    def search(self, query: str, *, media_type: str = "image", limit: int | None = None) -> list[dict]:
        raise NotImplementedError

    def download(self, candidate: dict, output_dir: Path | str) -> Path | None:
        url = str(candidate.get("download_url") or candidate.get("url") or "")
        if not url:
            return None
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        ext = Path(url.split("?", 1)[0]).suffix.lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm", ".mov"):
            ext = ".mp4" if candidate.get("type") == "video" else ".jpg"
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        target = out / f"{self.name}_{digest}{ext}"
        if target.exists() and target.stat().st_size > 0:
            return target
        try:
            data = self.http.get_bytes(url)
            if not data:
                return None
            target.write_bytes(data)
            return target
        except Exception as exc:
            logger.warning("%s download failed (%s): %s", self.name, url[:100], exc)
            return None


class WikimediaCommonsProvider(ArchiveProvider):
    """MediaWiki API search over Wikimedia Commons (images and video)."""

    name = "wikimedia_commons"
    PAGE_URL = "https://commons.wikimedia.org/wiki/File:"

    def search(self, query: str, *, media_type: str = "image", limit: int | None = None) -> list[dict]:
        if not self.enabled or not str(query or "").strip():
            return []
        filetype = "video" if media_type == "video" else "bitmap|drawing"
        search_terms = f"filetype:{filetype} {query}"
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            "gsrsearch": search_terms,
            "gsrnamespace": "6",
            "gsrlimit": str(min(limit or self.budget.page_size, self.budget.page_size)),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata|user|timestamp",
            "iiurlwidth": "1920",
            "iiextmetadatafilter": (
                "ImageDescription|Artist|LicenseShortName|LicenseUrl|UsageTerms|"
                "AttributionRequired|DateTimeOriginal|Categories|Credit|Copyrighted"
            ),
        }
        try:
            payload = self.http.get_json(WIKIMEDIA_API, params)
        except Exception as exc:
            logger.info("Wikimedia search failed: %s", exc)
            return []
        pages = (payload.get("query") or {}).get("pages") or []
        results: list[dict] = []
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            if not info:
                continue
            mime = str(info.get("mime") or "").lower()
            # Diagrams/icons/SVG are not usable as the only visual for a
            # documentary scene and cannot be evaluated by the still pipeline.
            if mime not in (
                "image/jpeg", "image/png", "image/webp", "image/gif",
                "video/mp4", "video/webm", "video/ogg", "video/quicktime",
            ):
                continue
            atype = "video" if mime.startswith("video/") else "image"
            if media_type == "video" and atype != "video":
                continue
            if media_type == "image" and atype != "image":
                continue
            metadata = info.get("extmetadata") or {}
            short = _strip_html((metadata.get("LicenseShortName") or {}).get("value"))
            license_url = _strip_html((metadata.get("LicenseUrl") or {}).get("value"))
            usage = _strip_html((metadata.get("UsageTerms") or {}).get("value"))
            license_label, public_domain, attribution = _norm_license(short, license_url, usage)
            categories_raw = _strip_html((metadata.get("Categories") or {}).get("value"))
            categories = [c.strip() for c in categories_raw.split("|") if c.strip()]
            title = str(page.get("title") or "").replace("File:", "")
            description = _strip_html((metadata.get("ImageDescription") or {}).get("value"))
            artist = _strip_html((metadata.get("Artist") or {}).get("value"))
            date = _strip_html((metadata.get("DateTimeOriginal") or {}).get("value"))
            page_url = self.PAGE_URL + str(page.get("title") or "").replace("File:", "")
            thumb = str(info.get("thumburl") or "")
            original = str(info.get("url") or "")
            results.append({
                "id": str(page.get("pageid") or title),
                "url": (thumb or original) if atype == "image" else (original or thumb),
                "download_url": (thumb or original) if atype == "image" else (original or thumb),
                "page_url": page_url,
                "title": title,
                "description": description,
                "tags": categories[:20],
                "categories": categories,
                "type": atype,
                "media_type": atype,
                "asset_type": "ARCHIVAL_VIDEO" if atype == "video" else "ARCHIVAL_IMAGE",
                "source": self.name,
                "provider": self.name,
                "width": info.get("width") or None,
                "height": info.get("height") or None,
                "duration": info.get("duration") or None,
                "mime": mime,
                "creator": artist,
                "license": license_label,
                "license_url": license_url,
                "attribution_required": attribution,
                "public_domain": public_domain,
                "date": date,
                "provenance": "Wikimedia Commons",
                "preview_url": thumb or original,
                "thumbnail_url": thumb or original,
                "preview_type": "video" if atype == "video" else "image",
                "visual_source": "wikimedia:thumb",
                "preview_status": "PREVIEW_AVAILABLE" if (thumb or original) else "PREVIEW_UNAVAILABLE",
            })
        return results


class NASAImageLibraryProvider(ArchiveProvider):
    """NASA Image and Video Library API (images.nasa.gov)."""

    name = "nasa_image_library"
    PAGE_URL = "https://images.nasa.gov/details/"

    def search(self, query: str, *, media_type: str = "image", limit: int | None = None) -> list[dict]:
        if not self.enabled or not str(query or "").strip():
            return []
        params = {
            "q": str(query),
            "media_type": "image" if media_type == "image" else "video",
            "page": "1",
            "page_size": str(min(limit or self.budget.page_size, self.budget.page_size)),
        }
        try:
            payload = self.http.get_json(NASA_API, params)
        except Exception as exc:
            logger.info("NASA search failed: %s", exc)
            return []
        items = ((payload.get("collection") or {}).get("items") or [])
        results: list[dict] = []
        for item in items:
            data = (item.get("data") or [{}])[0]
            links = item.get("links") or []
            preview = ""
            for link in links:
                href = str(link.get("href") or "")
                if href:
                    preview = href
                    if str(link.get("rel") or "") == "preview":
                        break
            nasa_id = str(data.get("nasa_id") or "")
            atype = str(data.get("media_type") or media_type)
            if atype not in ("image", "video"):
                atype = media_type
            title = str(data.get("title") or "").strip()
            description = str(data.get("description") or "").strip()
            keywords = data.get("keywords") or []
            if isinstance(keywords, str):
                keywords = [k.strip() for k in keywords.split(",") if k.strip()]
            page_url = self.PAGE_URL + nasa_id
            # The asset manifest is resolved on download; the preview still is
            # used for P02 visual inspection without a second request.
            download_url = str(data.get("asset_url") or "") or preview
            results.append({
                "id": nasa_id,
                "url": download_url,
                "download_url": download_url,
                "page_url": page_url,
                "title": title,
                "description": description,
                "tags": list(keywords)[:20],
                "categories": list(keywords)[:20],
                "type": atype,
                "media_type": atype,
                "asset_type": "SCIENTIFIC_IMAGE",
                "source": self.name,
                "provider": self.name,
                "width": None,
                "height": None,
                "duration": None,
                "creator": str(data.get("center") or data.get("secondary_creator") or "NASA"),
                "license": "NASA media usage guidelines",
                "license_url": "https://www.nasa.gov/nasa-brand-center/images-and-media/",
                "attribution_required": True,
                "public_domain": None,  # NASA terms are not blanket PD; keep UNKNOWN.
                "date": str(data.get("date_created") or ""),
                "provenance": "NASA Image and Video Library",
                "preview_url": preview,
                "thumbnail_url": preview,
                "preview_type": "video" if atype == "video" else "image",
                "visual_source": "nasa:preview",
                "preview_status": "PREVIEW_AVAILABLE" if preview else "PREVIEW_UNAVAILABLE",
                "_nasa_item_href": str(item.get("href") or ""),
            })
        return results

    def download(self, candidate: dict, output_dir: Path | str) -> Path | None:
        """Resolve the NASA asset manifest, then reuse the base downloader."""
        if candidate.get("download_url") and not candidate.get("_nasa_item_href"):
            return super().download(candidate, output_dir)
        href = str(candidate.get("_nasa_item_href") or "")
        if href:
            try:
                manifest = self.http.get_json(href, {})
                assets = manifest if isinstance(manifest, list) else []
                preferred = next(
                    (a for a in assets if str(a).lower().endswith((".jpg", ".jpeg", ".png"))),
                    None,
                )
                if preferred:
                    candidate = dict(candidate)
                    candidate["download_url"] = str(preferred)
            except Exception as exc:
                logger.info("NASA asset manifest failed: %s", exc)
        return super().download(candidate, output_dir)


def provider_registry(
    *,
    session: requests.Session | None = None,
    enabled: bool = True,
    budget: ProviderBudget | None = None,
) -> dict[str, ArchiveProvider]:
    return {
        WikimediaCommonsProvider.name: WikimediaCommonsProvider(
            session=session, enabled=enabled, budget=budget,
        ),
        NASAImageLibraryProvider.name: NASAImageLibraryProvider(
            session=session, enabled=enabled, budget=budget,
        ),
    }
