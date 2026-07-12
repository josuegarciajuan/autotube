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
1. Translate accurately first, preserving all factual content, names, and numbers.
2. Then rewrite ~30% of words using synonyms, different sentence structures, and regional
   Spanish variations. Change word order where natural.
3. Keep the emotional tone and pacing of the original (if it builds suspense, keep it).
4. Preserve SEO keywords — these must survive the paraphrasing (translate them but keep
   them as close equivalents).
5. Output ONLY the final adapted Spanish text — no explanations, no markers, no prefixes.
6. If the text contains [MUSIC], [APPLAUSE], or similar production markers, drop them."""

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

    # ── LLM lazy init ────────────────────────────────────────────

    def _init_llm(self):
        """Lazy-init the LLM client from environment or config."""
        if self._llm_initialized:
            return

        # Try DeepSeek first (already used by script_generator), fallback to OpenAI
        from config.settings import (
            DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
            OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL,
        )

        if DEEPSEEK_API_KEY:
            self._llm_api_key = DEEPSEEK_API_KEY
            self._llm_base_url = getattr(self.config, 'DEEPSEEK_BASE_URL', DEEPSEEK_BASE_URL) \
                if self.config else DEEPSEEK_BASE_URL
            self._llm_model = getattr(self.config, 'DEEPSEEK_MODEL', DEEPSEEK_MODEL) \
                if self.config else DEEPSEEK_MODEL
        elif OPENAI_API_KEY:
            self._llm_api_key = OPENAI_API_KEY
            self._llm_base_url = getattr(self.config, 'OPENAI_BASE_URL', OPENAI_BASE_URL) \
                if self.config else OPENAI_BASE_URL
            self._llm_model = getattr(self.config, 'OPENAI_MODEL', OPENAI_MODEL) \
                if self.config else OPENAI_MODEL

        if not self._llm_api_key:
            logger.warning("[%s] No LLM API key configured — viral translation will use simple approach", self.canal)
        else:
            logger.info("[%s] Viral LLM client: model=%s base_url=%s", self.canal, self._llm_model, self._llm_base_url)

        self._llm_initialized = True

    def _call_llm(self, system_prompt: str, user_message: str, temperature: float = 0.7) -> str | None:
        """Call the LLM for translation/paraphrasing/adaptation."""
        self._init_llm()
        if not self._llm_api_key:
            return None

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
            logger.error("[%s] LLM call failed: %s", self.canal, e)
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
        """Extract relevant fields from a yt-dlp flat result."""
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
            upload_date_str = data.get("upload_date", "")
            upload_timestamp = data.get("timestamp")
            hours_since_pub = float("inf")

            if upload_date_str and len(upload_date_str) == 8:
                # yt-dlp format: YYYYMMDD
                try:
                    upload_dt = datetime.strptime(upload_date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
                    hours_since_pub = (datetime.now(timezone.utc) - upload_dt).total_seconds() / 3600
                except ValueError:
                    pass
            elif upload_timestamp:
                try:
                    upload_dt = datetime.fromtimestamp(upload_timestamp, tz=timezone.utc)
                    hours_since_pub = (datetime.now(timezone.utc) - upload_dt).total_seconds() / 3600
                except (ValueError, TypeError, OSError):
                    pass

            # Skip too old (> max_age_days)
            if hours_since_pub > (self.max_age_days * 24) or hours_since_pub <= 0:
                return None

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
            else:  # 14-30 days
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

    def _build_queries(self) -> list[str]:
        """Build search queries from niche keywords with viral hooks."""
        queries = []

        # Direct keyword queries
        for kw in self.keywords_eng[:self.max_queries]:
            queries.append(kw)

        # Add viral-formatted versions for top keywords
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

        # Shuffle to avoid predictable patterns
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

        # If already within range (±20%), accept without adaptation
        if abs(current_minutes - ((target_min + target_max) / 2)) <= max(1, (target_max - target_min)):
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

        return adapted or script_es

    def _build_script_blocks(self, script_es: str) -> list[dict]:
        """Build script blocks in Autotube format from adapted Spanish script.

        Returns a list of blocks compatible with pipeline/bloques_json format:
        [{"type": "hook"|"desarrollo"|"climax"|"reflexion"|"cierre",
          "text": "...", "voice_rate": "...", "voice_pitch": "..."}]
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
            return [{"type": "desarrollo", "text": script_es}]

        n = len(paragraphs)
        blocks = []

        if n == 1:
            blocks.append({"type": "hook", "text": paragraphs[0].split(".")[0] + "."})
            blocks.append({"type": "desarrollo", "text": paragraphs[0]})
        elif n <= 4:
            block_types = ["hook", "desarrollo", "climax", "cierre"][:n]
            for i, p in enumerate(paragraphs):
                blocks.append({"type": block_types[i], "text": p})
        else:
            # Distribute: hook, N-3 desarrollo, climax, reflexion, cierre
            blocks.append({"type": "hook", "text": paragraphs[0]})
            for p in paragraphs[1:-3]:
                blocks.append({"type": "desarrollo", "text": p})
            blocks.append({"type": "climax", "text": paragraphs[-3]})
            blocks.append({"type": "reflexion", "text": paragraphs[-2]})
            blocks.append({"type": "cierre", "text": paragraphs[-1]})

        return blocks

    # ── Main scrape method ──────────────────────────────────────

    def scrape(self) -> list[dict]:
        """Run the full viral discovery pipeline and return processed items.

        Returns:
            List of dicts with keys compatible with save_to_db():
            source, url, title, text, subreddit, score, AND additional
            viral-specific keys for insert_raw_content_viral().
        """
        logger.info("[%s] ========== VIRAL SCRAPE START ==========", self.canal)

        items = []

        # Step 1: Discover candidates
        candidates = self._discover_candidates(force_refresh=False)
        if not candidates:
            logger.info("[%s] No viral candidates found", self.canal)
            return items

        # Step 2: Process the top candidate (one per scrape call, to save resources)
        top = candidates[0]
        logger.info("[%s] Processing candidate: '%s' (score=%.1f)", self.canal, top["title"][:60], top["viral_score"])

        # Check if already in DB
        db = getattr(self, "_db", None)
        if db:
            existing = db.get_content_by_url(top["url"], self.canal)
            if existing:
                logger.info("[%s] Candidate already in DB, skipping: %s", self.canal, top["title"][:40])
                # Still return it so it shows up in get_viral_candidates
                return [existing]

        # Step 3: Download audio
        video_url = top["url"]
        video_id = top["video_id"]
        audio_path = self._download_audio(video_url, video_id)
        if not audio_path:
            logger.error("[%s] Audio download failed for %s — skipping", self.canal, video_id)
            return items

        # Step 4: Transcribe (English → text)
        transcript_en = self._transcribe(audio_path)
        if not transcript_en:
            logger.warning("[%s] Transcription empty — using title + description as fallback", self.canal)
            transcript_en = f"{top['title']}. {top.get('description', '')}"

        # Step 5: Translate + paraphrase
        translated_script, translated_title, translated_desc = self._translate_and_paraphrase(
            transcript_en, top["title"]
        )
        if not translated_script:
            logger.error("[%s] Translation failed — using raw transcription as fallback", self.canal)
            translated_script = transcript_en  # Will still be in English, but better than nothing

        # Step 6: Adapt duration to channel config
        adapted_script = self._adapt_duration(translated_script)
        if not adapted_script:
            adapted_script = translated_script

        # Step 7: Build block structure for TTS
        blocks = self._build_script_blocks(adapted_script)

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
            "text": transcript_en[:500],  # Store original EN snippet
            "subreddit": top["channel_name"],
            "score": int(top["viral_score"]),
            # Viral-specific fields
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

        logger.info("[%s] ========== VIRAL SCRAPE COMPLETE ==========", self.canal)
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
            # Build blocks_json if present
            blocks_json = item.pop("viral_blocks_json", None)

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
