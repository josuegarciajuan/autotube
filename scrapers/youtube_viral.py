"""YouTube Viral Mirror scraper.

Discovers high-performing YouTube videos in English-speaking markets,
then transcribes, translates, paraphrases and adapts them into Spanish
scripts that feed into the existing Autotube pipeline.

This is a content source like Reddit or Wikipedia — it populates the
raw_content table with pre-processed viral scripts.
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from types import SimpleNamespace

from scrapers.base import BaseScraper, register_scraper

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────

_YTDLP_BIN = "yt-dlp"
_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
_TEMP_DIR = _OUTPUT_DIR / "temp"
_VIRAL_AUDIO_DIR = _TEMP_DIR / "viral_audio"
_SCRIPTS_DIR = _TEMP_DIR / "viral_scripts"

# Default viral scoring thresholds (channel configs can override)
DEFAULT_MIN_VIEWS = 500_000
DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_MAX_QUERIES = 8
DEFAULT_RESULTS_PER_QUERY = 15
DEFAULT_MAX_CANDIDATES = 20


# ── User-Agent pool (rotation to avoid yt-dlp detection) ─────────────

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0",
]


# ── Translate & Paraphrase system prompt ─────────────────────────────

_TRANSLATE_SYSTEM_PROMPT = """You are a professional content localizer specializing in YouTube.
Your job: translate English viral video scripts into Spanish, then PARAPHRASE ~30% of the words
so the result is NOT a direct translation — it's a fresh adaptation that preserves the viral hook,
emotional arc, and search keywords but reads like original native Spanish content.

RULES:
1. Translate accurately first, preserving all factual content, dates, and numbers.
2. CRITICAL — REMOVE or REPLACE any host/narrator/creator/presenter names (e.g. "Danny Trejo",
   "Joe Rogan", "MrBeast", "Johnny Harris", any channel name or on-screen personality).
   Historical figures and subjects of the story (e.g. "Cleopatra", "Einstein") should be kept.
   The adapted script must NOT sound like it was narrated by, written by, or associated with
   the original content creator. Replace host references with neutral phrasing like
   "el documental", "los investigadores", "los expertos", "este video", or omit them entirely.
3. Then rewrite ~30% of words using synonyms, different sentence structures, and regional
   Spanish variations. Change word order where natural.
4. Keep the emotional tone and pacing of the original (if it builds suspense, keep it).
5. Preserve SEO keywords — these must survive the paraphrasing (translate them but keep
   them as close equivalents).
6. Output ONLY the final adapted Spanish text — no explanations, no markers, no prefixes.
7. If the text contains [MUSIC], [APPLAUSE], or similar production markers, drop them."""

_ADAPT_DURATION_SYSTEM_PROMPT = """You are a video script editor. Your job: adapt a Spanish script
to fit a target duration of {target_minutes}-{target_max} minutes.

The current script is approximately {current_words} words (~{current_minutes} min at Spanish
narration pace of ~150 words/min).

TARGET: {target_minutes}-{target_max} minutes ({target_words_range} words).

RULES:
1. If the script is TOO SHORT: expand with additional detail, context, related cases, or
   rhetorical questions that deepen the narrative. Add 1-2 new paragraphs that fit naturally.
2. If the script is TOO LONG: condense by merging paragraphs, cutting secondary details,
   removing repetitive statements. Keep the strongest hook and the climax.
3. Maintain the emotional arc: hook → buildup → climax → reflection.
4. NEVER change factual accuracy. NEVER invent false claims.
5. Output ONLY the adapted text — no explanations, no markers."""


@register_scraper("youtube_viral")
class YouTubeViralScraper(BaseScraper):
    """Discovers viral YouTube videos, transcribes, translates, and adapts them.

    On scrape(), this:
      1. Searches YouTube with English niche keywords via yt-dlp
      2. Calculates viral score for each result
      3. Downloads and transcribes the top candidate
      4. Translates, paraphrases, and adapts the script to Spanish
      5. Saves everything as raw_content with viral metadata
    """

    # ── LLM configuration (shared across instances) ─────────────────

    _llm_api_key: str | None = None
    _llm_base_url: str | None = None
    _llm_model: str | None = None
    _llm_initialized: bool = False

    def __init__(
        self,
        config: Optional[SimpleNamespace] = None,
        rate_limit: float = 3.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__(config=config, rate_limit=rate_limit, max_retries=max_retries)

        # Channel info
        self.canal = getattr(self.config, "CANAL_NAME", "unknown") if self.config else "unknown"
        self.slug = getattr(self.config, "slug", self.canal) if self.config else self.canal

        # Viral thresholds (channel config or defaults)
        if self.config:
            self.min_views = getattr(self.config, "VIRAL_MIN_VIEWS", DEFAULT_MIN_VIEWS)
            self.max_age_days = getattr(self.config, "VIRAL_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS)
            self.max_queries = getattr(self.config, "VIRAL_MAX_QUERIES", DEFAULT_MAX_QUERIES)
            self.max_candidates = getattr(self.config, "VIRAL_MAX_CANDIDATES", DEFAULT_MAX_CANDIDATES)
        else:
            self.min_views = DEFAULT_MIN_VIEWS
            self.max_age_days = DEFAULT_MAX_AGE_DAYS
            self.max_queries = DEFAULT_MAX_QUERIES
            self.max_candidates = DEFAULT_MAX_CANDIDATES

        # English keywords (pulled from channel config or viral_keywords module)
        self.keywords_eng: list[str] = []
        if self.config:
            from_attr = getattr(self.config, "NICHE_KEYWORDS_ENG", None)
            if from_attr:
                self.keywords_eng = from_attr

        # Fallback: load from viral_keywords module by slug
        if not self.keywords_eng:
            try:
                from config.viral_keywords import NICHE_KEYWORDS_ENG
                self.keywords_eng = NICHE_KEYWORDS_ENG.get(
                    self.slug,
                    NICHE_KEYWORDS_ENG.get("default", ["viral documentary"])
                )
            except ImportError:
                self.keywords_eng = ["viral documentary"]

        # Target video duration range (from channel config)
        if self.config:
            self.target_duration_min = getattr(self.config, "video_average_duration_min", 10) - \
                                       getattr(self.config, "video_duration_discrepancy_min", 3)
            self.target_duration_max = getattr(self.config, "video_average_duration_min", 10) + \
                                       getattr(self.config, "video_duration_discrepancy_min", 3)
        else:
            self.target_duration_min = 8
            self.target_duration_max = 14

        # Ensure output dirs exist
        _VIRAL_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        _SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

        # Persistent state
        self._cached_candidates: list[dict] = []
        self._last_search_time: float = 0.0
        self._search_cache_ttl: float = 3600.0  # 1 hour cache
        self._suggested_queries: list[str] = []  # external queries from query builder

    # ── LLM lazy init ────────────────────────────────────────────

    def _init_llm(self):
        """Lazy-init the LLM client from environment or config."""
        if self._llm_initialized:
            return

        # Try DeepSeek first (already used by script_generator), fallback to OpenAI
        from config.settings import (
            LLM_API_KEY, LLM_BASE_URL, LLM_MODEL,
            OPENAI_API_KEY, OPENAI_MODEL,
        )

        api_key = LLM_API_KEY or OPENAI_API_KEY
        if api_key:
            self._llm_api_key = api_key
            self._llm_base_url = LLM_BASE_URL
            self._llm_model = LLM_MODEL or OPENAI_MODEL

        if not self._llm_api_key:
            logger.warning("[%s] No LLM API key configured — viral translation will use simple approach", self.canal)
        else:
            logger.info("[%s] Viral LLM client: model=%s base_url=%s", self.canal, self._llm_model, self._llm_base_url)

        self._llm_initialized = True

    def _call_llm(self, system_prompt: str, user_message: str, temperature: float = 0.7) -> str | None:
        """Call the LLM for translation/paraphrasing/adaptation with retries."""
        self._init_llm()
        if not self._llm_api_key:
            return None

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                from openai import OpenAI
                client = OpenAI(api_key=self._llm_api_key, base_url=self._llm_base_url)
                resp = client.chat.completions.create(
                    model=self._llm_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=temperature,
                    max_tokens=4096,
                )
                content = resp.choices[0].message.content
                return content.strip() if content else None
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    wait = 2 ** attempt  # exponential backoff: 2s, 4s, 8s
                    logger.warning("[%s] LLM call failed (attempt %d/%d): %s — retrying in %ds",
                                   self.canal, attempt, self.max_retries, e, wait)
                    time.sleep(wait)
                else:
                    logger.error("[%s] LLM call failed after %d attempts: %s",
                                 self.canal, self.max_retries, e)

        return None

    # ── YouTube discovery via yt-dlp ────────────────────────────

    def _search_youtube(self, query: str, max_results: int = 15) -> list[dict]:
        """Search YouTube with yt-dlp and extract metadata. No API quota used."""
        ytdl_query = f"ytsearch{max_results}:{query}"
        results = []

        user_agent = random.choice(_USER_AGENTS)

        cmd = [
            _YTDLP_BIN,
            ytdl_query,
            "--dump-json",
            "--flat-playlist",
            "--no-warnings",
            "--no-check-certificate",
            "--user-agent", user_agent,
            "--extractor-args", "youtubetab:skip=webpage",
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )

            for line in proc.stdout.strip().split("\n"):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    results.append(data)
                except json.JSONDecodeError:
                    continue

            # Respect rate limit
            sleep_time = random.uniform(2.0, 4.0)
            time.sleep(sleep_time)

        except subprocess.TimeoutExpired:
            logger.warning("[%s] yt-dlp search timed out for query: %s", self.canal, query)
        except Exception as e:
            logger.error("[%s] yt-dlp search error for '%s': %s", self.canal, query, e)

        return results

    def _parse_ytdlp_result(self, data: dict) -> dict | None:
        """Extract relevant fields from a yt-dlp flat result.

        When using --flat-playlist, upload_date and timestamp are often NOT
        available. In that case we skip the age filter (assume recent) rather
        than discarding the candidate.
        """
        try:
            view_count = data.get("view_count")
            if view_count is None:
                return None

            # Convert view_count to int (sometimes it's a string)
            if isinstance(view_count, str):
                view_count = int(view_count) if view_count.isdigit() else 0
            view_count = int(view_count)

            # Skip videos with too few views
            if view_count < self.min_views:
                return None

            # Parse upload date
            upload_date_str = data.get("upload_date", "") or ""
            upload_timestamp = data.get("timestamp")
            hours_since_pub = float("inf")
            date_available = False

            if upload_date_str and len(str(upload_date_str)) == 8:
                # yt-dlp format: YYYYMMDD
                try:
                    upload_dt = datetime.strptime(str(upload_date_str), "%Y%m%d").replace(tzinfo=timezone.utc)
                    hours_since_pub = (datetime.now(timezone.utc) - upload_dt).total_seconds() / 3600
                    date_available = True
                except ValueError:
                    pass
            elif upload_timestamp:
                try:
                    upload_dt = datetime.fromtimestamp(float(upload_timestamp), tz=timezone.utc)
                    hours_since_pub = (datetime.now(timezone.utc) - upload_dt).total_seconds() / 3600
                    date_available = True
                except (ValueError, TypeError, OSError):
                    pass

            if date_available:
                # Only filter by age when we have real date data
                if hours_since_pub > (self.max_age_days * 24) or hours_since_pub <= 0:
                    logger.debug("[%s] Filtered out (age=%.1fh, max=%dh): %s",
                                 self.canal, hours_since_pub, self.max_age_days * 24,
                                 data.get("title", "")[:50])
                    return None
            else:
                # --flat-playlist does not include upload_date/timestamp.
                # Assume the video is recent enough to pass the age filter
                # but use a conservative score (24h) so it doesn't get an
                # unfair "freshness bonus" from a tiny divisor.
                hours_since_pub = 168  # ~7 days — flat penalty

            # Duration
            duration_sec = data.get("duration", 0) or 0
            if isinstance(duration_sec, str):
                duration_sec = int(duration_sec) if duration_sec.isdigit() else 0
            duration_sec = int(duration_sec)

            # Viral score: views per hour + freshness bonus
            viral_score = view_count / max(hours_since_pub, 1)

            # Freshness bonus: newer videos get a multiplier
            if hours_since_pub < 168:  # < 7 days
                viral_score *= 1.0
            elif hours_since_pub < 336:  # 7-14 days
                viral_score *= 0.7
            else:  # 14-30 days (or unknown age → 7-day bucket)
                viral_score *= 0.4

            return {
                "title": data.get("title", ""),
                "url": data.get("webpage_url", "") or data.get("url", ""),
                "video_id": data.get("id", ""),
                "views": view_count,
                "upload_date": str(upload_date_str) if upload_date_str else "",
                "duration_sec": duration_sec,
                "channel_name": data.get("channel", "") or data.get("uploader", ""),
                "channel_id": data.get("channel_id", ""),
                "description": data.get("description", "") or "",
                "thumbnail_url": data.get("thumbnail", "") or (
                    f"https://i.ytimg.com/vi/{data.get('id', '')}/maxresdefault.jpg"
                    if data.get("id") else ""
                ),
                "viral_score": round(viral_score, 1),
                "hours_since_pub": round(hours_since_pub, 1),
            }
        except Exception as e:
            logger.debug("[%s] Error parsing yt-dlp result: %s", self.canal, e)
            return None

    def _fetch_real_upload_date(self, video_url: str) -> datetime | None:
        """Fetch the real upload date for a single video using yt-dlp without --flat-playlist.

        --flat-playlist searches skip upload_date metadata. This method does a
        dedicated yt-dlp call for a specific video URL to get the real date.

        Returns:
            datetime object (naive, UTC), or None if date cannot be determined.
        """
        if not video_url:
            return None

        cmd = [
            _YTDLP_BIN,
            video_url,
            "--dump-json",
            "--skip-download",
            "--no-warnings",
            "--no-check-certificate",
            "--user-agent", random.choice(_USER_AGENTS),
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            data = json.loads(proc.stdout.strip()) if proc.stdout.strip() else {}
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
            logger.debug("[%s] _fetch_real_upload_date failed for %s: %s", self.canal, video_url, e)
            return None

        # Parse upload_date (yt-dlp format: YYYYMMDD)
        upload_date_str = str(data.get("upload_date", "") or "")
        if upload_date_str and len(upload_date_str) == 8:
            try:
                return datetime.strptime(upload_date_str, "%Y%m%d")
            except ValueError:
                pass

        # Fallback: try timestamp
        upload_timestamp = data.get("timestamp")
        if upload_timestamp:
            try:
                return datetime.fromtimestamp(float(upload_timestamp))
            except (ValueError, TypeError, OSError):
                pass

        return None

    def _build_queries(self) -> list[str]:
        """Build search queries.

        If suggested_queries was set externally (from viral_query_builder),
        use those directly. Otherwise fall back to the old static query building.
        """
        if self._suggested_queries:
            logger.info("[%s] Using %d suggested queries from viral_query_builder", self.canal, len(self._suggested_queries))
            return self._suggested_queries[:self.max_queries]

        # Legacy: build from niche keywords
        queries = []
        for kw in self.keywords_eng[:self.max_queries]:
            queries.append(kw)
        if len(self.keywords_eng) >= 3:
            viral_formats = [
                "most shocking {}",
                "top 5 {}",
                "the {} you won't believe",
                "{} documentary",
                "incredible {} stories",
            ]
            for kw in self.keywords_eng[:3]:
                for fmt in viral_formats:
                    formatted = fmt.format(kw)
                    if formatted not in queries and len(queries) < self.max_queries:
                        queries.append(formatted)
        random.shuffle(queries)
        return queries[:self.max_queries]

    def _discover_candidates(self, force_refresh: bool = False) -> list[dict]:
        """Run discovery: search → parse → score → deduplicate → cache.

        Returns the top candidates sorted by viral_score descending.
        """
        # Use cached results if fresh enough
        if not force_refresh and self._cached_candidates:
            cache_age = time.time() - self._last_search_time
            if cache_age < self._search_cache_ttl:
                logger.info("[%s] Using cached viral candidates (%d found, %.0f min old)",
                            self.canal, len(self._cached_candidates), cache_age / 60)
                return self._cached_candidates

        # Build queries
        queries = self._build_queries()
        logger.info("[%s] Viral discovery: %d queries → running...", self.canal, len(queries))

        seen_ids: set[str] = set()
        all_candidates: list[dict] = []

        for i, query in enumerate(queries):
            logger.info("[%s] Query %d/%d: '%s'", self.canal, i + 1, len(queries), query)
            raw_results = self._search_youtube(query, DEFAULT_RESULTS_PER_QUERY)

            for result in raw_results:
                parsed = self._parse_ytdlp_result(result)
                if parsed and parsed["video_id"] not in seen_ids:
                    seen_ids.add(parsed["video_id"])
                    all_candidates.append(parsed)

            if len(all_candidates) >= self.max_candidates * 2:
                break

        # Sort by viral_score descending, take top N
        all_candidates.sort(key=lambda x: x["viral_score"], reverse=True)
        top_candidates = all_candidates[:self.max_candidates]

        # ── Second pass: fetch real dates for candidates with unknown age ──
        # --flat-playlist often omits upload_date. For top candidates,
        # we do a dedicated yt-dlp call (without --flat-playlist) to get
        # the real upload date, then re-score and re-filter.
        candidates_with_unknown_age = [
            c for c in top_candidates
            if c.get("hours_since_pub") == 168  # sentinel: date was unavailable
        ]
        if candidates_with_unknown_age:
            logger.info("[%s] %d/%d candidates have unknown upload dates — fetching real dates...",
                        self.canal, len(candidates_with_unknown_age), len(top_candidates))
            for candidate in candidates_with_unknown_age[:10]:  # limit to top 10
                real_date = self._fetch_real_upload_date(candidate.get("url", ""))
                if real_date is not None:
                    # Recompute hours_since_pub and re-check age filter
                    upload_dt = real_date.replace(tzinfo=timezone.utc)
                    hours = (datetime.now(timezone.utc) - upload_dt).total_seconds() / 3600
                    candidate["hours_since_pub"] = round(hours, 1)
                    candidate["upload_date"] = real_date.strftime("%Y%m%d")
                    
                    # Re-score with real date
                    candidate["viral_score"] = round(
                        candidate["views"] / max(hours, 1) * (0.7 if hours < 336 else 0.4), 1
                    )
                    
                    # Filter out if too old
                    if hours > self.max_age_days * 24 or hours <= 0:
                        logger.info("[%s] Removed candidate after date verification: '%s' (age=%.1fh, max=%dh)",
                                    self.canal, candidate.get("title", "")[:50], hours,
                                    self.max_age_days * 24)
                        top_candidates = [c for c in top_candidates if c.get("video_id") != candidate.get("video_id")]
                    else:
                        logger.info("[%s] Verified date for '%s': %.1fh old (score=%.0f)",
                                    self.canal, candidate.get("title", "")[:50], hours, candidate["viral_score"])
                else:
                    # Could not get real date — candidate stays with penalty score
                    # but we log a warning so operators can investigate
                    logger.warning("[%s] Could not fetch real date for '%s' (%s) — "
                                   "keeping with penalty score (168h assumed)",
                                   self.canal, candidate.get("title", "")[:60],
                                   candidate.get("url", "")[:60])

        # Re-sort after date verification
        top_candidates.sort(key=lambda x: x["viral_score"], reverse=True)

        # Cache
        self._cached_candidates = top_candidates
        self._last_search_time = time.time()

        logger.info("[%s] Viral discovery complete: %d results → %d top candidates",
                    self.canal, len(all_candidates), len(top_candidates))

        if top_candidates:
            best = top_candidates[0]
            logger.info("[%s] Top candidate: '%s' (score=%.0f, views=%s, %sh ago)",
                        self.canal, best["title"][:60], best["viral_score"],
                        best["views"], best["hours_since_pub"])

        return top_candidates

    # ── Video download + transcription ──────────────────────────

    def _download_audio(self, video_url: str, video_id: str) -> str | None:
        """Download audio-only from a YouTube video using yt-dlp. Returns path to audio file."""
        audio_path = _VIRAL_AUDIO_DIR / f"{video_id}.mp3"
        if audio_path.exists():
            logger.info("[%s] Audio already downloaded: %s", self.canal, audio_path)
            return str(audio_path)

        logger.info("[%s] Downloading audio from: %s", self.canal, video_url)
        cmd = [
            _YTDLP_BIN,
            video_url,
            "-x", "--audio-format", "mp3",
            "--audio-quality", "128K",
            "-o", str(audio_path),
            "--no-warnings",
            "--no-check-certificate",
            "--user-agent", random.choice(_USER_AGENTS),
        ]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if audio_path.exists():
                size_mb = audio_path.stat().st_size / (1024 * 1024)
                logger.info("[%s] Audio downloaded: %.1f MB", self.canal, size_mb)
                return str(audio_path)
            else:
                logger.error("[%s] Audio download failed — file not found: %s", self.canal, audio_path)
                return None
        except subprocess.TimeoutExpired:
            logger.error("[%s] Audio download timed out (>600s)", self.canal)
            return None
        except Exception as e:
            logger.error("[%s] Audio download error: %s", self.canal, e)
            return None

    def _transcribe(self, audio_path: str) -> str | None:
        """Transcribe audio to English text using faster-whisper (local, no API cost)."""
        logger.info("[%s] Transcribing audio: %s", self.canal, Path(audio_path).name)
        try:
            from faster_whisper import WhisperModel

            # Use small model for speed (good enough for viral scripts)
            model_size = "small"
            model = WhisperModel(model_size, device="cpu", compute_type="int8")

            segments, info = model.transcribe(
                audio_path,
                language="en",
                beam_size=3,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )

            text_parts = []
            for segment in segments:
                text_parts.append(segment.text.strip())

            transcript = " ".join(text_parts)

            if not transcript or len(transcript) < 50:
                logger.warning("[%s] Transcription too short (%d chars) — likely failed", self.canal, len(transcript))
                return None

            word_count = len(transcript.split())
            logger.info("[%s] Transcription complete: %d words, detected language: %s",
                        self.canal, word_count, info.language)

            return transcript

        except ImportError:
            logger.error("[%s] faster-whisper not installed. Install with: pip install faster-whisper", self.canal)
            return None
        except Exception as e:
            logger.error("[%s] Transcription failed: %s", self.canal, e)
            return None

    # ── Translation + Paraphrasing + Adaptation ─────────────────

    def _translate_and_paraphrase(self, english_text: str, original_title: str) -> tuple[str | None, str | None, str | None]:
        """Translate + paraphrase the transcript, title, and description.

        Returns (translated_script, translated_title, translated_description) or None.
        """
        self._init_llm()

        translated_script = None
        translated_title = None
        translated_description = None

        # 1. Translate + paraphrase the full transcript
        if english_text:
            user_msg = f"Translate and paraphrase this viral video transcript into Spanish:\n\n{english_text[:8000]}"
            translated_script = self._call_llm(_TRANSLATE_SYSTEM_PROMPT, user_msg, temperature=0.7)

        # 2. Translate + paraphrase the title (using same system prompt, custom instruction)
        if original_title:
            title_msg = f"Translate and paraphrase this viral video TITLE into Spanish (keep it short and punchy, ~30% word changes):\n\n{original_title}"
            title_system = _TRANSLATE_SYSTEM_PROMPT + "\nIMPORTANT: This is a TITLE. Keep it under 100 characters. Make it clickable."
            translated_title = self._call_llm(title_system, title_msg, temperature=0.8)

        return translated_script, translated_title, translated_description

    def _adapt_duration(self, script_es: str) -> str | None:
        """Adapt the Spanish script to fit the channel's target video duration."""
        if not script_es:
            return None

        word_count = len(script_es.split())
        current_minutes = word_count / 150  # ~150 words/min for Spanish narration
        target_min = max(self.target_duration_min, 1)
        target_max = self.target_duration_max

        # If already within target range, accept without adaptation
        if target_min <= current_minutes <= target_max:
            logger.info("[%s] Script duration %.1f min already in range [%d-%d min] — skipping adaptation",
                        self.canal, current_minutes, target_min, target_max)
            return script_es

        self._init_llm()

        target_words_min = int(target_min * 150)
        target_words_max = int(target_max * 150)

        system_prompt = _ADAPT_DURATION_SYSTEM_PROMPT.format(
            target_minutes=target_min,
            target_max=str(target_max),
            current_words=word_count,
            current_minutes=f"{current_minutes:.0f}",
            target_words_range=f"{target_words_min}-{target_words_max}",
        )

        user_msg = f"Adapt this Spanish script to {target_min}-{target_max} minutes:\n\n{script_es[:8000]}"
        adapted = self._call_llm(system_prompt, user_msg, temperature=0.4)

        if adapted:
            new_words = len(adapted.split())
            logger.info("[%s] Duration adapted: %d → %d words (~%.1f → ~%.1f min)",
                        self.canal, word_count, new_words, current_minutes, new_words / 150)

            # Validate: adapted script must reach at least 50% of target min words
            min_acceptable_words = int(target_words_min * 0.5)
            if new_words < min_acceptable_words:
                logger.warning("[%s] Adapt duration FAILED: output %d words < %d minimum (50%% of target). "
                               "Input had %d words — gap too large for LLM to fill.",
                               self.canal, new_words, min_acceptable_words, word_count)
                return None

        return adapted or script_es

    def _build_script_blocks(self, script_es: str) -> list[dict]:
        """Build script blocks in Autotube format from adapted Spanish script.

        Returns a list of blocks compatible with pipeline/bloques_json format:
        [{"tipo": "hook"|"desarrollo"|"climax"|"reflexion"|"cierre",
          "texto": "...", "voice_rate": "...", "voice_pitch": "..."}]
        """
        if not script_es:
            return []

        paragraphs = [p.strip() for p in script_es.split("\n\n") if p.strip() and len(p.strip()) > 20]

        if not paragraphs:
            # Fallback: split by sentences
            import re
            sentences = re.split(r'(?<=[.!?])\s+', script_es)
            sentences = [s.strip() for s in sentences if len(s.strip()) > 20]
            if len(sentences) >= 5:
                paragraphs = sentences

        if not paragraphs:
            return [{"tipo": "desarrollo", "texto": script_es}]

        n = len(paragraphs)
        blocks = []

        if n == 1:
            first_sentence = paragraphs[0].split(".")[0] + "."
            blocks.append({"tipo": "hook", "texto": first_sentence})
            # Desarrollo: use the full paragraph, but skip the hook text to avoid duplication
            rest = paragraphs[0][len(first_sentence):].strip()
            blocks.append({"tipo": "desarrollo", "texto": rest if rest else paragraphs[0]})
        elif n <= 4:
            block_types = ["hook", "desarrollo", "climax", "cierre"][:n]
            for i, p in enumerate(paragraphs):
                blocks.append({"tipo": block_types[i], "texto": p})
        else:
            # Distribute: hook, N-3 desarrollo, climax, reflexion, cierre
            blocks.append({"tipo": "hook", "texto": paragraphs[0]})
            for p in paragraphs[1:-3]:
                blocks.append({"tipo": "desarrollo", "texto": p})
            blocks.append({"tipo": "climax", "texto": paragraphs[-3]})
            blocks.append({"tipo": "reflexion", "texto": paragraphs[-2]})
            blocks.append({"tipo": "cierre", "texto": paragraphs[-1]})

        return blocks

    # ── Main scrape method ──────────────────────────────────────

    def scrape(self) -> list[dict]:
        """Run the full viral discovery pipeline and return processed items.

        Returns:
            List of dicts with keys compatible with save_to_db():
            source, url, title, text, subreddit, score, AND additional
            viral-specific keys for insert_raw_content_viral().
        """
        t0 = time.time()
        logger.info("[%s] ========== VIRAL SCRAPE START ==========", self.canal)
        logger.info("[%s] Config: min_views=%d, max_age_days=%d, target_duration=[%d-%d]min, queries_limit=%d",
                    self.canal, self.min_views, self.max_age_days,
                    self.target_duration_min, self.target_duration_max, self.max_queries)

        items = []

        # Step 1: Discover candidates
        t1 = time.time()
        candidates = self._discover_candidates(force_refresh=False)
        if not candidates:
            logger.info("[%s] No viral candidates found after %.1fs", self.canal, time.time() - t0)
            return items
        logger.info("[%s] Step 1 (discover): %d candidates in %.1fs", self.canal, len(candidates), time.time() - t1)

        # Step 2: Select the best UNUSED candidate (skip already-in-DB ones)
        db = getattr(self, "_db", None)
        top = None
        for candidate in candidates:
            url = candidate.get("url", "")
            if db:
                existing = db.get_content_by_url(url, self.canal)
                if existing:
                    logger.info("[%s] Skipping already-in-DB candidate: '%s' (id=%d, used=%s)",
                                self.canal, candidate["title"][:50],
                                existing["id"], existing.get("used", "?"))
                    continue
            top = candidate
            break

        if not top:
            # All candidates already in DB — try expanded search with lower threshold
            saved_min_views = self.min_views
            self.min_views = max(50000, self.min_views // 5)
            logger.warning("[%s] All %d candidates already in DB — expanding search (min_views: %d→%d)",
                           self.canal, len(candidates), saved_min_views, self.min_views)
            self._cached_candidates = []  # force fresh search
            expanded = self._discover_candidates(force_refresh=True)
            self.min_views = saved_min_views  # restore
            if expanded:
                for candidate in expanded:
                    url = candidate.get("url", "")
                    if db:
                        existing = db.get_content_by_url(url, self.canal)
                        if existing:
                            continue
                    top = candidate
                    break

        if not top:
            logger.warning("[%s] No unused viral candidates available after expansion", self.canal)
            return items

        logger.info("[%s] Selected candidate: '%s'", self.canal, top["title"][:80])
        logger.info("[%s]   views=%d  score=%.1f  age=%.1fh  duration=%ds  channel='%s'",
                    self.canal, top["views"], top["viral_score"],
                    top["hours_since_pub"], top["duration_sec"], top["channel_name"])

        # Step 3: Download audio
        t3 = time.time()
        video_url = top["url"]
        video_id = top["video_id"]
        logger.info("[%s] Step 3: Downloading audio from %s...", self.canal, video_url[:80])
        audio_path = self._download_audio(video_url, video_id)
        if not audio_path:
            logger.error("[%s] Step 3 FAILED: Audio download failed for %s — skipping candidate", self.canal, video_id)
            return items
        logger.info("[%s] Step 3 (download): done in %.1fs → %s", self.canal, time.time() - t3, audio_path)

        # Step 4: Transcribe (English → text)
        t4 = time.time()
        logger.info("[%s] Step 4: Transcribing audio with faster-whisper...", self.canal)
        transcript_en = self._transcribe(audio_path)
        if not transcript_en:
            logger.warning("[%s] Step 4 WARNING: Transcription empty — using title+description as fallback", self.canal)
            transcript_en = f"{top['title']}. {top.get('description', '')}"
        else:
            logger.info("[%s] Step 4 (transcribe): done in %.1fs → %d words EN",
                        self.canal, time.time() - t4, len(transcript_en.split()))

        # Step 5: Translate + paraphrase
        t5 = time.time()
        logger.info("[%s] Step 5: Translating + paraphrasing (%d EN words → ES)...", self.canal, len(transcript_en.split()))
        translated_script, translated_title, translated_desc = self._translate_and_paraphrase(
            transcript_en, top["title"]
        )
        if not translated_script:
            logger.error("[%s] Step 5 FAILED: Translation returned empty — using raw EN transcription", self.canal)
            translated_script = transcript_en
        else:
            logger.info("[%s] Step 5 (translate): done in %.1fs → %d words ES, title='%s'",
                        self.canal, time.time() - t5, len(translated_script.split()),
                        (translated_title or "N/A")[:60])

        # Step 6: Adapt duration to channel config
        t6 = time.time()
        logger.info("[%s] Step 6: Adapting script duration to [%d-%d]min target...",
                    self.canal, self.target_duration_min, self.target_duration_max)
        adapted_script = self._adapt_duration(translated_script)
        if not adapted_script:
            logger.error("[%s] Step 6 FAILED: Cannot adapt %d-word script to [%d-%d]min target. "
                          "Content gap too large — aborting scrape.",
                          self.canal, len(translated_script.split()),
                          self.target_duration_min, self.target_duration_max)
            return items
        logger.info("[%s] Step 6 (adapt): done in %.1fs → %d words (~%.1f min)",
                    self.canal, time.time() - t6, len(adapted_script.split()),
                    len(adapted_script.split()) / 150)

        # Step 7: Build block structure for TTS
        blocks = self._build_script_blocks(adapted_script)
        logger.info("[%s] Step 7: Built %d blocks for TTS: %s",
                    self.canal, len(blocks),
                    [b["tipo"] for b in blocks])

        # Step 8: Construct metadata JSON (for viral_cloner use later)
        viral_meta = {
            "original_title": top["title"],
            "translated_title": translated_title,
            "translated_description": translated_desc or top.get("description", ""),
            "blocks": blocks,
            "original_views": top["views"],
            "original_channel": top["channel_name"],
            "original_url": video_url,
            "word_count": len(adapted_script.split()),
            "estimated_duration_min": round(len(adapted_script.split()) / 150, 1),
        }

        items.append({
            "source": "youtube_viral",
            "url": top["url"],
            "title": translated_title or top["title"],
            "text": transcript_en[:500],
            "subreddit": top["channel_name"],
            "score": int(top["viral_score"]),
            "source_mode": "viral",
            "viral_original_title": top["title"],
            "viral_original_description": top.get("description", ""),
            "viral_original_thumbnail_url": top["thumbnail_url"],
            "viral_original_video_url": video_url,
            "viral_views": top["views"],
            "viral_upload_date": top["upload_date"],
            "viral_duration_sec": top["duration_sec"],
            "viral_channel_name": top["channel_name"],
            "viral_score": top["viral_score"],
            "viral_script_es": adapted_script,
            "viral_meta_json": json.dumps(viral_meta, ensure_ascii=False),
            "viral_blocks_json": json.dumps(blocks, ensure_ascii=False),
        })

        # Step 9: Clean up audio file (save disk space)
        try:
            if audio_path and Path(audio_path).exists():
                Path(audio_path).unlink()
                logger.debug("[%s] Cleaned up audio file: %s", self.canal, Path(audio_path).name)
        except OSError:
            pass

        elapsed = time.time() - t0
        logger.info("[%s] ========== VIRAL SCRAPE COMPLETE (%.1fs total) ==========", self.canal, elapsed)
        logger.info("[%s] Result: script=%d words, title='%s', score=%.1f, blocks=%d",
                    self.canal, len(adapted_script.split()),
                    (translated_title or "N/A")[:60], top["viral_score"], len(blocks))
        return items

    def save_to_db(self, db) -> int:
        """Override to use insert_raw_content_viral() with all viral columns."""
        self._db = db
        items = self.scrape()
        inserted = 0

        canal = getattr(
            self, "canal",
            getattr(self.config, "CANAL_NAME", None)
            or getattr(self.config, "canal_name", "unknown"),
        )

        for item in items:
            row_id = db.insert_raw_content_viral(
                source=item["source"],
                url=item["url"],
                title=item["title"],
                text=item["text"],
                subreddit=item.get("subreddit"),
                score=item.get("score", 0),
                canal=canal,
                source_mode=item.get("source_mode", "viral"),
                viral_original_title=item.get("viral_original_title"),
                viral_original_description=item.get("viral_original_description"),
                viral_original_thumbnail_url=item.get("viral_original_thumbnail_url"),
                viral_original_video_url=item.get("viral_original_video_url"),
                viral_views=item.get("viral_views", 0),
                viral_upload_date=item.get("viral_upload_date"),
                viral_duration_sec=item.get("viral_duration_sec", 0),
                viral_channel_name=item.get("viral_channel_name"),
                viral_score=item.get("viral_score", 0.0),
                viral_script_es=item.get("viral_script_es"),
                viral_meta_json=item.get("viral_meta_json"),
            )
            if row_id is not None:
                inserted += 1

        self.logger.info("[%s] Viral scraper: saved %d/%d items to database", self.canal, inserted, len(items))
        return inserted


# ═══════════════════════════════════════════════════════════════════
# Standalone search function (testable from CLI)
# ═══════════════════════════════════════════════════════════════════

def search_viral_candidates(
    keywords: list[str],
    min_views: int = DEFAULT_MIN_VIEWS,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    max_queries: int = DEFAULT_MAX_QUERIES,
    results_per_query: int = DEFAULT_RESULTS_PER_QUERY,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> list[dict]:
    """Standalone viral candidate search — no DB, no config, no LLM.

    Args:
        keywords: List of English search queries.
        min_views: Minimum view count for a candidate.
        max_age_days: Maximum age in days (ignored if flat-playlist has no date).
        max_queries: Maximum number of search queries to run.
        results_per_query: Results per yt-dlp search.
        max_candidates: Maximum candidates to return.

    Returns:
        List of candidate dicts sorted by viral_score descending, with keys:
        title, url, video_id, views, upload_date, duration_sec,
        channel_name, channel_id, description, thumbnail_url,
        viral_score, hours_since_pub.
    """
    log = logging.getLogger("viral_search")

    seen_ids: set[str] = set()
    all_candidates: list[dict] = []

    queries = list(keywords[:max_queries])
    log.info("search_viral_candidates: %d queries, min_views=%d, max_age=%d days",
             len(queries), min_views, max_age_days)

    for i, query in enumerate(queries):
        log.info("Query %d/%d: '%s'", i + 1, len(queries), query)

        # Build the same yt-dlp command as the scraper
        user_agent = random.choice(_USER_AGENTS)
        ytdl_query = f"ytsearch{results_per_query}:{query}"
        cmd = [
            _YTDLP_BIN, ytdl_query,
            "--dump-json", "--flat-playlist",
            "--no-warnings", "--no-check-certificate",
            "--user-agent", user_agent,
            "--extractor-args", "youtubetab:skip=webpage",
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )

            for line in proc.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Parse with manual thresholds (same logic as _parse_ytdlp_result)
                view_count = data.get("view_count")
                if view_count is None:
                    continue
                if isinstance(view_count, str):
                    view_count = int(view_count) if view_count.isdigit() else 0
                view_count = int(view_count)
                if view_count < min_views:
                    continue

                video_id = data.get("id", "")
                if not video_id or video_id in seen_ids:
                    continue
                seen_ids.add(video_id)

                # Upload date parsing
                upload_date_str = data.get("upload_date", "") or ""
                upload_timestamp = data.get("timestamp")
                hours_since_pub = float("inf")
                date_available = False

                if upload_date_str and len(str(upload_date_str)) == 8:
                    try:
                        upload_dt = datetime.strptime(str(upload_date_str), "%Y%m%d").replace(tzinfo=timezone.utc)
                        hours_since_pub = (datetime.now(timezone.utc) - upload_dt).total_seconds() / 3600
                        date_available = True
                    except ValueError:
                        pass
                elif upload_timestamp:
                    try:
                        upload_dt = datetime.fromtimestamp(float(upload_timestamp), tz=timezone.utc)
                        hours_since_pub = (datetime.now(timezone.utc) - upload_dt).total_seconds() / 3600
                        date_available = True
                    except (ValueError, TypeError, OSError):
                        pass

                if date_available:
                    if hours_since_pub > (max_age_days * 24) or hours_since_pub <= 0:
                        continue
                else:
                    hours_since_pub = 168

                # Duration
                duration_sec = data.get("duration", 0) or 0
                if isinstance(duration_sec, str):
                    duration_sec = int(duration_sec) if duration_sec.isdigit() else 0
                duration_sec = int(duration_sec)

                # Score
                viral_score = view_count / max(hours_since_pub, 1)
                if hours_since_pub < 168:
                    viral_score *= 1.0
                elif hours_since_pub < 336:
                    viral_score *= 0.7
                else:
                    viral_score *= 0.4

                all_candidates.append({
                    "title": data.get("title", ""),
                    "url": data.get("webpage_url", "") or data.get("url", ""),
                    "video_id": video_id,
                    "views": view_count,
                    "upload_date": str(upload_date_str) if upload_date_str else "unknown",
                    "duration_sec": duration_sec,
                    "channel_name": data.get("channel", "") or data.get("uploader", ""),
                    "channel_id": data.get("channel_id", ""),
                    "description": (data.get("description", "") or "")[:200],
                    "thumbnail_url": data.get("thumbnail", "") or (
                        f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg" if video_id else ""
                    ),
                    "viral_score": round(viral_score, 1),
                    "hours_since_pub": round(hours_since_pub, 1),
                })

            # Rate limit between queries
            time.sleep(random.uniform(2.0, 4.0))

        except subprocess.TimeoutExpired:
            log.warning("yt-dlp search timed out for query: '%s'", query)
        except Exception as e:
            log.error("yt-dlp search error for '%s': %s", query, e)

    # Sort by viral_score, deduplicate, take top N
    all_candidates.sort(key=lambda x: x["viral_score"], reverse=True)
    top = all_candidates[:max_candidates]

    log.info("search_viral_candidates: %d total → %d returned", len(all_candidates), len(top))
    return top


# ═══════════════════════════════════════════════════════════════════
# CLI test entrypoint
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test Viral Candidate Search")
    parser.add_argument("--keywords", nargs="+", default=["medical mysteries"],
                        help="English keywords to search")
    parser.add_argument("--min-views", type=int, default=100000,
                        help="Minimum views (default: 100000)")
    parser.add_argument("--max-age", type=int, default=30,
                        help="Maximum age in days (default: 30)")
    parser.add_argument("--max-queries", type=int, default=5,
                        help="Max queries (default: 5)")
    parser.add_argument("--results-per-query", type=int, default=10,
                        help="Results per query (default: 10)")
    parser.add_argument("--max-candidates", type=int, default=10,
                        help="Max candidates to return (default: 10)")
    args_cli = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    print(f"\n{'='*60}")
    print(f"VIRAL CANDIDATE SEARCH")
    print(f"{'='*60}")
    print(f"Keywords: {args_cli.keywords}")
    print(f"Min views: {args_cli.min_views:,}")
    print(f"Max age: {args_cli.max_age} days")
    print(f"Queries: {args_cli.max_queries}")
    print(f"Results/query: {args_cli.results_per_query}")
    print(f"{'='*60}\n")

    candidates = search_viral_candidates(
        keywords=args_cli.keywords,
        min_views=args_cli.min_views,
        max_age_days=args_cli.max_age,
        max_queries=args_cli.max_queries,
        results_per_query=args_cli.results_per_query,
        max_candidates=args_cli.max_candidates,
    )

    if not candidates:
        print("NO candidates found.")
    else:
        print(f"TOP {len(candidates)} CANDIDATES:\n")
        print(f"{'#':<4} {'Score':<10} {'Views':<12} {'Age':<8} {'Dur':<8} {'Title'}")
        print(f"{'-'*4} {'-'*10} {'-'*12} {'-'*8} {'-'*8} {'-'*50}")
        for i, c in enumerate(candidates, 1):
            age = f"{c['hours_since_pub']:.0f}h" if c['hours_since_pub'] != 168 else "unknown"
            dur = f"{c['duration_sec']//60}m" if c['duration_sec'] else "?"
            print(f"{i:<4} {c['viral_score']:>10,.0f} {c['views']:>12,} {age:<8} {dur:<8} {c['title'][:50]}")
        print(f"\n{'='*60}")
        print(f"Full URLs:")
        for i, c in enumerate(candidates, 1):
            print(f"  {i}. {c['url']}")
