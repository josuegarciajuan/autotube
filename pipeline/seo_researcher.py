"""Real-time keyword research for YouTube SEO.

Uses Google Trends (pytrends) and YouTube autocomplete for trending keywords.
Provides fallback chains so the pipeline never blocks on external API failures.
"""

import json
import logging
import re
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

# Try pytrends — graceful fallback to autocomplete-only if unavailable
try:
    from pytrends.request import TrendReq
    _PYTRENDS_AVAILABLE = True
except ImportError:
    _PYTRENDS_AVAILABLE = False
    logger.info("pytrends not available — falling back to YouTube autocomplete only")


class SEOResearcher:
    """Real-time keyword research for YouTube SEO optimization.

    Uses a fallback chain:
      1. Google Trends (pytrends) → related trending queries
      2. YouTube autocomplete scraping (no API key needed)
      3. Static keywords from channel config

    All methods gracefully degrade — the pipeline never blocks.
    """

    _AUTOCOMPLETE_URL = "http://suggestqueries.google.com/complete/search"
    # Simple in-memory cache for autocomplete results (5-min TTL by convention,
    # cleared between pipeline runs via fresh instantiation)
    _cache: dict[str, list[str]] = {}

    def __init__(self, channel_slug: str, config):
        """Initialize with channel identity.

        Args:
            channel_slug: e.g. "canal2", "canal3"
            config: SimpleNamespace from config_bridge with CHANNEL_KEYWORDS, etc.
        """
        self.slug = channel_slug
        self.config = config

        self._pytrends: Optional[TrendReq] = None
        if _PYTRENDS_AVAILABLE:
            try:
                self._pytrends = TrendReq(hl="es-ES", tz=360, timeout=(5, 10))
            except Exception as exc:
                logger.warning("SEOResearcher: pytrends init failed — %s", exc)
                self._pytrends = None

        self._static_keywords = list(getattr(config, "CHANNEL_KEYWORDS", []) or [])

    # ── Main entry point ──────────────────────────────────────────

    def get_trending_keywords(self, topic: str, geo: str = "ES") -> list[str]:
        """Get trending related keywords for a topic.

        Fallback chain (each step returns immediately on success):
        1. pytrends.related_queries → rising + top queries
        2. YouTube autocomplete → suggestions for topic
        3. Static config keywords (CHANNEL_KEYWORDS, shuffled subset)

        Returns up to 10 trending keywords.
        """
        # Step 1 — Google Trends
        if self._pytrends is not None:
            try:
                kw = self._pytrends_trending(topic, geo)
                if kw:
                    logger.debug(
                        "SEOResearcher(%s): pytrends → %d keywords for '%s'",
                        self.slug, len(kw), topic,
                    )
                    return kw[:10]
            except Exception as exc:
                logger.debug("SEOResearcher(%s): pytrends failed for '%s' — %s", self.slug, topic, exc)

        # Step 2 — YouTube autocomplete
        try:
            kw = self.get_youtube_autocomplete(topic)
            if kw:
                logger.debug(
                    "SEOResearcher(%s): autocomplete → %d keywords for '%s'",
                    self.slug, len(kw), topic,
                )
                return kw[:10]
        except Exception as exc:
            logger.debug("SEOResearcher(%s): autocomplete failed for '%s' — %s", self.slug, topic, exc)

        # Step 3 — static fallback
        import random
        pool = list(self._static_keywords)[:20]
        if pool:
            n = min(10, len(pool))
            result = random.sample(pool, n) if n < len(pool) else pool[:n]
            logger.debug("SEOResearcher(%s): static fallback → %d keywords", self.slug, len(result))
            return result

        return []

    # ── pytrends integration ─────────────────────────────────────

    def _pytrends_trending(self, topic: str, geo: str) -> list[str]:
        """Query pytrends for related rising/top queries."""
        if self._pytrends is None:
            return []

        self._pytrends.build_payload([topic], timeframe="today 3-m", geo=geo)
        related = self._pytrends.related_queries()

        # related is a dict: {"topic_name": {"top": DataFrame, "rising": DataFrame}}
        results: list[str] = []
        topic_key = list(related.keys())[0] if related else None
        if topic_key and isinstance(related[topic_key], dict):
            for label in ("rising", "top"):
                df = related[topic_key].get(label)
                if df is not None and not df.empty:
                    # DataFrame columns: query, value
                    queries = df["query"].head(10).tolist() if "query" in df.columns else []
                    results.extend([str(q).strip().lower() for q in queries if str(q).strip()])

        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for kw in results:
            if kw not in seen and kw.lower() != topic.lower():
                seen.add(kw)
                deduped.append(kw)

        return deduped[:10]

    # ── YouTube autocomplete (no API key needed) ──────────────────

    def get_youtube_autocomplete(self, query: str) -> list[str]:
        """Scrape YouTube search suggestions.

        Calls the public Google Suggest API endpoint used by the YouTube
        search bar (no API key required).  Returns a list of suggested
        search strings.

        Args:
            query: Search phrase (e.g. "civilizaciones antiguas").

        Returns:
            List of autocomplete suggestions, up to 10 items.
        """
        cache_key = f"yt:{query.lower().strip()}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            resp = requests.get(
                self._AUTOCOMPLETE_URL,
                params={
                    "client": "youtube",
                    "ds": "yt",
                    "q": query,
                },
                timeout=5.0,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
            resp.raise_for_status()

            # Response is JSONP: window.google.ac.h(["query", [...], ...])
            text = resp.text
            # Extract the JSON array from inside the callback
            # Pattern: window.google.ac.h( [...] )
            match = re.search(r"window\.google\.ac\.h\((.*)\)\s*$", text, re.DOTALL)
            if not match:
                logger.debug("SEOResearcher: could not parse autocomplete response")
                return []

            raw = match.group(1)
            data = json.loads(raw)

            # data[0] = input query, data[1] = list of [suggestion, ...] arrays
            suggestions: list[str] = []
            if isinstance(data, list) and len(data) > 1:
                items = data[1]
                for item in items:
                    if isinstance(item, list) and len(item) > 0:
                        suggestion = str(item[0]).strip()
                        if suggestion and suggestion.lower() != query.lower().strip():
                            suggestions.append(suggestion)

            self._cache[cache_key] = suggestions[:10]
            return suggestions[:10]

        except (json.JSONDecodeError, requests.RequestException, Exception) as exc:
            logger.debug("SEOResearcher: YouTube autocomplete failed for '%s': %s", query, exc)
            return []

    # ── Tag optimization ──────────────────────────────────────────

    def optimize_tags(self, base_tags: list[str], topic: str) -> list[str]:
        """Merge base tags from config with trending keywords.

        Strategy:
        1. Start with trending keywords (higher priority, fresh)
        2. Add base tags not already included
        3. Limit to ~500 chars total (YouTube tag field limit)
        4. Order by relevance (trending first, then niche-specific)

        Args:
            base_tags: Static tag list (e.g. from YT_DEFAULT_TAGS).
            topic: Video topic to research trending keywords for.

        Returns:
            Optimized deduplicated tag list.
        """
        # Get trending keywords first
        trending = self.get_trending_keywords(topic)

        seen: set[str] = set()
        result: list[str] = []
        total_chars = 0
        max_tag_chars = 500

        def _add_tag(tag: str) -> bool:
            """Add a tag if it fits within 500 chars. Returns True if added."""
            nonlocal total_chars
            clean = tag.strip().lower()
            if not clean or len(clean) < 2 or clean in seen:
                return False
            # Estimate char cost: tag + comma + optional quotes
            cost = len(clean) + (2 if " " in clean else 0) + 1
            if total_chars + cost > max_tag_chars:
                return False
            seen.add(clean)
            result.append(clean)
            total_chars += cost
            return True

        # Layer 1: trending keywords (subject-specific, highest priority)
        for kw in trending:
            if len(result) >= 10:
                break
            _add_tag(kw)

        # Layer 2: base tags from config (not already included)
        base_pool = list(base_tags or []) + self._static_keywords
        for tag in base_pool:
            if len(result) >= 10:
                break
            _add_tag(str(tag))

        return result[:10]


# ── Module-level utility (used by metadata_generator) ──────────

def _format_seconds(sec: float) -> str:
    """Format seconds as M:SS or H:MM:SS."""
    total = int(sec)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
