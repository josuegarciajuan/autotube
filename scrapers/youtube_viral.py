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
from config.voice_timing import duration_for_words

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────

_YTDLP_BIN = "yt-dlp"
_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
_TEMP_DIR = _OUTPUT_DIR / "temp"
_VIRAL_AUDIO_DIR = _TEMP_DIR / "viral_audio"
_SCRIPTS_DIR = _TEMP_DIR / "viral_scripts"

# Default viral scoring thresholds (channel configs can override)
DEFAULT_MIN_VIEWS = 500_000
DEFAULT_MAX_AGE_DAYS = 29
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

_TITLE_CREATION_SYSTEM_PROMPT = """You are a YouTube SEO expert who creates viral Spanish video titles.

Your task: Given a viral English video title, create a COMPLETELY FRESH Spanish title that captures
the SAME CORE TOPIC and CURIOSITY HOOK but uses DIFFERENT words, structure, and perspective.

CRITICAL ANTI-COPYRIGHT RULES:
- NEVER do a direct translation. You must create an ORIGINAL title.
- Change at least 60% of the content words (nouns, verbs, adjectives).
- Use a DIFFERENT sentence structure than the original (e.g., if original is a statement,
  make yours a question or a mystery hook).
- Remove ALL host/presenter/channel names. Keep only historical figures and subjects.
- Remove ALL duration claims ("4+ HOURS", "3 horas", etc.). Our video is shorter.
- Remove ALL season/episode markers (T01E06, Temporada 2, etc.).

TITLE FORMULAS (pick the best one for this topic):
1. QUESTION HOOK: "¿[Pregunta intrigante]?" — e.g., "¿Qué ocultan realmente las pirámides?"
2. REVELATION: "Lo que [sujeto] no quiere que sepas sobre [tema]"
3. MYSTERY: "El misterio de [tema] que la ciencia no puede explicar"
4. SHOCKING FACT: "[Dato impactante] que cambia todo lo que sabías"
5. JOURNEY: "[Tema]: La historia que nadie te contó"
6. CONTROVERSY: "Por qué [tema establecido] podría ser mentira"
7. LIST/COUNTDOWN: "[Número] [cosas/datos/misterios] sobre [tema] que te sorprenderán"

TONE: Drama, misterio, curiosidad. Spanish for a Latin American / Spain audience.
Keep it under 100 characters. Make it CLICKABLE.
Output ONLY the final title — no explanations, no quotes, no prefixes."""

_TITLE_ANTI_PLAGIARISM_RETRY_PROMPT = """Your previous title was too similar to the original. 
Create a COMPLETELY DIFFERENT title using a different formula. Do NOT use any of the same key 
phrases. Use different vocabulary. Make it shorter and punchier.

Output ONLY the new title — no explanations."""

_DESCRIPTION_CREATION_SYSTEM_PROMPT = """You are a YouTube SEO expert creating Spanish video descriptions.

Your job: Write an ORIGINAL Spanish description for a video, using ONLY the provided translated
script and topic. NEVER look at or translate the original English description.

CRITICAL RULES:
- Write entirely in Spanish for a Latin American/Spain audience.
- Do NOT include any URLs, links, email addresses, or social media handles.
- Do NOT include phrases like "Subscribe", "Join my Discord", "Follow me", "Like & Share",
  "Activate the bell", "Business inquiries", or any calls to external platforms.
- Do NOT mention the original creator, channel name, or host names.
- Do NOT copy or translate the original description — this must be ORIGINAL text.

STRUCTURE (in Spanish):
1. HOOK (1-2 sentences): Pregunta impactante o dato intrigante del video.
2. SUMMARY (3-5 sentences): De qué trata el video, los puntos más fascinantes.
3. KEYWORDS / TOPICS (1 line): Menciona 3-5 temas clave sin hashtags.
4. SOURCE NOTE (optional): "Basado en investigación y documentación histórica." or similar.

LENGTH: 400-800 characters. Keep it scannable with short paragraphs.
Output ONLY the description — no headers, no "Description:" prefixes, no quotes."""


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

        # Target video duration range (from panel "Duración — Objetivo", DB-authoritative)
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

    @staticmethod
    def _is_documentary_style_title(title: str, channel_name: str = "") -> bool:
        """Check if a video title looks like documentary/story content.

        Returns True if the title suggests documentary, educational, or storytelling
        content (not a personal vlog, reaction, or host-driven channel).

        Rejects titles that clearly come from a YouTuber/personality format:
        - Personal vlogs ("I tried...", "My experience...")
        - Reaction/prank/challenge videos
        - Gaming content
        - Titles that include the host/presenter name (e.g., "with John Smith")
        """
        if not title or len(title) < 10:
            return True  # too short to judge, let it through

        title_lower = title.lower().strip()
        import re

        # ── Hard rejects: personal YouTuber / Vlogger patterns ──
        _REJECT_PATTERNS = [
            r'\bi\s+(tried|tested|ate|visited|went|built|made|spent|survived|react)\b',
            r'\bmy\s+(experience|journey|first|top|favorite|worst|scariest)\b',
            r'\bwe\s+(tried|tested|went|built|made|found)\b',
            r'\b(reaction|reacting|reaccion)\s+to\b',
            r'\bprank\b', r'\bvlog\b', r'\bchallenge\b',
            r'\bvs\b.*\bvs\b',  # "X vs Y vs Z" clickbait
            r'\bgone\s+(wrong|wild|sexual|too far)\b',
            r'\b(try not to|24 hour|overnight)\b',
            r'\b(mukbang|asmr|unboxing|haul)\b',
            r'\b(roblox|minecraft|fortnite|gta\s*[56])\b',
        ]
        for pattern in _REJECT_PATTERNS:
            if re.search(pattern, title_lower):
                return False

        # ── Hard rejects: TV series / documentary episodes ──
        # These are part of a longer series, not standalone content.
        _SERIES_PATTERNS = [
            # Explicit season/episode markers (English + Spanish)
            r'\b[tT]\s*\d{1,2}\s*[eExX]\s*\d{1,2}\b',          # T01E06, T1 E6
            r'\b[sS]\s*\d{1,2}\s*[eE]\s*\d{1,2}\b',             # S01E06
            r'\b(season|temporada)\s+\d+\s*(episode|episodio|capitulo|capítulo)\s+\d+',
            r'\b(episodio|episode)\s+\d+\b',                     # Episodio 6
            r'\b(capitulo|capítulo|chapter)\s+\d+\b',            # Capítulo 12
            # Multi-part episode markers
            r'\bparte?\s+[ivxlcdm]+\b',                           # Parte IV
            r'\bpart\s+\d+\b',                                    # Part 3 of 4
        ]
        for pattern in _SERIES_PATTERNS:
            if re.search(pattern, title_lower):
                logger.debug("[viral] Title rejected — series episode detected: '%s'", title[:80])
                return False

        # ── Numbered documentary series: "13. Los Asirios – El Imperio de Hierro" ──
        # Titles starting with "N. " or "#N " that look like a chapter/series index.
        # Listicle titles (e.g., "10 Cosas que..." / "5 Reasons...") are NOT rejected.
        if re.match(r'^\d{1,2}[\.\):]\s+', title_lower):
            # Check if it's a listicle — if so, let it through
            _LISTICLE_KEYWORDS = [
                r'\b(cosas|things|razones|reasons|tips|ways|maneras|formas|secretos|secrets|datos|facts|curiosidades|misterios|leyendas|datos que|trucos|consejos)\b',
                r'\bque\s+(no\s+)?(sab[ií]as|conoc[ií]as|te\s+contaron|te\s+enseñaron)\b',
                r'\b(most|top|mejores|peores)\b',
            ]
            is_listicle = any(re.search(p, title_lower) for p in _LISTICLE_KEYWORDS)
            if not is_listicle:
                logger.debug("[viral] Title rejected — numbered series format (not listicle): '%s'", title[:80])
                return False

        # ── Host/presenter name patterns ──
        # Check if the channel name appears as a host credit in the title
        if channel_name and len(channel_name) > 2:
            ch_lower = channel_name.lower()
            # Patterns like "| ChannelName", "- ChannelName", "by ChannelName"
            _HOST_CREDIT_PATTERNS = [
                rf'[\|\-–—]\s*{re.escape(ch_lower)}',
                rf'\bby\s+{re.escape(ch_lower)}\b',
                rf'\bwith\s+{re.escape(ch_lower)}\b',
                rf'{re.escape(ch_lower)}\s+explains\b',
                rf'{re.escape(ch_lower)}\s+(reacts|reviews|presents|shows)\b',
            ]
            for pat in _HOST_CREDIT_PATTERNS:
                if re.search(pat, title_lower):
                    logger.debug("[viral] Title rejected — host credit detected: '%s'", title[:80])
                    return False

        # Check for host intro patterns (any name, not just channel name)
        _HOST_INTRO_PATTERNS = [
            # "| First Last" — excludes known documentary/show suffixes
            r'\|\s*(?!Full\s+Documentary|History\s+Channel|Discovery\s+Channel|National\s+Geographic|Real\s+Stories|DW\s+Documentary|BBC|PBS|Timeline)[A-Z][a-z]+\s+[A-Z][a-z]+',
            r'\bwith\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b',  # "with John Smith"
            r'\bfeat\.?\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b', # "feat. John Smith"
            r'\bpresented by\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b', # "presented by John Smith"
            # Note: "by" is NOT included because it's too ambiguous
            # ("History by Ancient Scholars" = valid topic, not a host)
        ]
        for pattern in _HOST_INTRO_PATTERNS:
            if re.search(pattern, title):
                logger.debug("[viral] Title rejected — host intro detected: '%s'", title[:80])
                return False

        # ── Positive signals: documentary/story format keywords ──
        _DOC_POSITIVES = [
            r'\b(documentary|documental)\b',
            r'\b(history|historia)\b',
            r'\b(explained|explicado|explicación)\b',
            r'\b(how|why|what)\s+(the|a|an|is|are|was|were)\b',
            r'\b(the|el|la|los|las)\s+(mystery|misterio|secret|secreto|truth|verdad)\b',
            r'\b(mystery|mysteries|misterios|misterio)\b',
            r'\b(ancient|antigu[oa]|lost|perdid[oa]|forgotten|olvidad[oa])\b',
            r'\b(discovery|descubrimiento|found)\b',
            r'\b(unsolved|sin\s*resolver)\b',
            r'\b(the story of|la historia de)\b',
            r'\b(what happened to|qué pasó con)\b',
            r'\b(investigation|investigación)\b',
            r'\b(conspiracy|conspiración)\b',
            r'\b(curse|maldición)\b',
            r'\b(legend|leyenda)\b',
            r'\b(phenomenon|fenómeno)\b',
            r'\b(evidence|evidencia)\b',
            r'\b(theory|teoría)\b',
            r'\b(secrets|secretos)\b',
            r'\b(revealed|revelad[oa])\b',
            r'\b(shocking|impactante)\b',
            r'\b(incredible|increíble)\b',
            r'\b(origins|origen|orígenes)\b',
            r'\b(extinction|extinción)\b',
            r'\b(truth about|verdad sobre)\b',
        ]
        for pattern in _DOC_POSITIVES:
            if re.search(pattern, title_lower):
                return True

        # ── Neutral: no strong signal either way ──
        # Let it through — will be filtered by age + views + DB dedup
        return True

    def _parse_ytdlp_result(self, data: dict) -> dict | None:
        """Extract relevant fields from a yt-dlp result.

        When using --flat-playlist, upload_date and timestamp are often NOT
        available. In that case the candidate is marked as date_verified=False
        and MUST go through a second-pass date verification in _discover_candidates()
        before being accepted. No fake dates are invented.
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

            title = data.get("title", "") or ""
            channel_name = data.get("channel", "") or data.get("uploader", "") or ""

            # ── Filter: reject non-documentary / youtuber-style titles ──
            if not self._is_documentary_style_title(title, channel_name):
                logger.debug("[%s] Filtered out (not documentary style): %s",
                             self.canal, title[:80])
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
                # Even if the video is older than max_age_days, keep it.
                # The second-pass in _discover_candidates() will decide with
                # a soft fallback: prefer recent videos, but fall back to the
                # highest-viewed older video if nothing recent exists.
                if hours_since_pub <= 0:
                    logger.debug("[%s] Filtered out (invalid age=%.1fh): %s",
                                 self.canal, hours_since_pub, title[:50])
                    return None
                date_verified = True
            else:
                # --flat-playlist does NOT include upload_date/timestamp.
                # DO NOT invent a fake date. Mark as unverified so
                # _discover_candidates() second pass can fetch the real date.
                hours_since_pub = -1  # sentinel: unknown
                date_verified = False
                upload_date_str = ""

            # Duration
            duration_sec = data.get("duration", 0) or 0
            if isinstance(duration_sec, str):
                duration_sec = int(duration_sec) if duration_sec.isdigit() else 0
            duration_sec = int(duration_sec)

            # Provisional score: views-based, neutral for unverified dates
            if date_verified:
                viral_score = view_count / max(hours_since_pub, 1)
                # Freshness bonus for verified dates
                if hours_since_pub < 168:       # < 7 days
                    viral_score *= 1.0
                elif hours_since_pub < 336:      # 7-14 days
                    viral_score *= 0.7
                else:                            # 14+ days
                    viral_score *= 0.4
            else:
                # No date = neutral score (views / 100 ≈ ~4-day equivalent).
                # This keeps high-view candidates ranked high for second-pass
                # verification, but without giving an unfair freshness bonus.
                viral_score = view_count / 100.0

            return {
                "title": title,
                "url": data.get("webpage_url", "") or data.get("url", ""),
                "video_id": data.get("id", ""),
                "views": view_count,
                "upload_date": upload_date_str,
                "duration_sec": duration_sec,
                "channel_name": channel_name,
                "channel_id": data.get("channel_id", ""),
                "description": data.get("description", "") or "",
                "thumbnail_url": data.get("thumbnail", "") or (
                    f"https://i.ytimg.com/vi/{data.get('id', '')}/maxresdefault.jpg"
                    if data.get("id") else ""
                ),
                "viral_score": round(viral_score, 1),
                "hours_since_pub": round(hours_since_pub, 1),
                "date_verified": date_verified,
            }
        except Exception as e:
            logger.debug("[%s] Error parsing yt-dlp result: %s", self.canal, e)
            return None

    def _fetch_real_upload_date(self, video_url: str) -> datetime | None:
        """Fetch the real upload date for a single video using yt-dlp without --flat-playlist.

        --flat-playlist searches skip upload_date metadata. This method does a
        dedicated yt-dlp call for a specific video URL to get the real date.

        Uses --print to extract just the upload_date field, which is faster than
        --dump-json because yt-dlp skips downloading all other metadata.

        Returns:
            datetime object (naive, UTC), or None if date cannot be determined.
        """
        if not video_url:
            return None

        cmd = [
            _YTDLP_BIN,
            video_url,
            "--print", "%(upload_date)s",
            "--skip-download",
            "--no-warnings",
            "--no-check-certificate",
            "--extractor-args", "youtube:skip=webpage",
            "--user-agent", random.choice(_USER_AGENTS),
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=90,  # increased from 60s for reliability
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            upload_date_str = proc.stdout.strip().split("\n")[0].strip()
        except subprocess.TimeoutExpired:
            logger.debug("[%s] _fetch_real_upload_date timed out for: %s", self.canal, video_url)
            return None
        except Exception as e:
            logger.debug("[%s] _fetch_real_upload_date failed for %s: %s", self.canal, video_url, e)
            return None

        # Parse upload_date (yt-dlp format: YYYYMMDD)
        if upload_date_str and len(upload_date_str) == 8 and upload_date_str.isdigit():
            try:
                return datetime.strptime(upload_date_str, "%Y%m%d")
            except ValueError:
                pass

        # Fallback: try --dump-json if --print didn't work (older yt-dlp versions)
        try:
            cmd[2] = "--dump-json"
            proc2 = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=90,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            data = json.loads(proc2.stdout.strip()) if proc2.stdout.strip() else {}
            upload_date_str = str(data.get("upload_date", "") or "")
            if upload_date_str and len(upload_date_str) == 8:
                return datetime.strptime(upload_date_str, "%Y%m%d")
            timestamp = data.get("timestamp")
            if timestamp:
                return datetime.fromtimestamp(float(timestamp))
        except Exception:
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
        """Run discovery: search → parse → score → deduplicate → verify dates → cache.

        ALL candidates without real upload dates go through mandatory date
        verification. Candidates that fail verification are discarded
        (no fake dates, no penalty scores for unverified content).

        Returns the top candidates sorted by views descending.
        """
        # Use cached results if fresh enough
        if not force_refresh and self._cached_candidates:
            cache_age = time.time() - self._last_search_time
            if cache_age < self._search_cache_ttl:
                logger.info("[%s] Using cached viral candidates (%d found, %.0f min old)",
                            self.canal, len(self._cached_candidates), cache_age / 60)
                return self._cached_candidates

        # ── Use extended 180-day window ──
        original_max_age_days = self.max_age_days
        if force_refresh:
            self.max_age_days = 180

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

        # ── Second pass: MANDATORY date verification for ALL unverified candidates ──
        # --flat-playlist often omits upload_date. Every candidate without a real
        # date MUST be verified via a dedicated yt-dlp call.
        #
        # v2: 180-day window, sorted by views descending. If nothing found
        # within 180 days, fall back to the highest-viewed older video.
        candidates_need_verification = [
            c for c in all_candidates
            if not c.get("date_verified", False)
        ]
        if candidates_need_verification:
            logger.info("[%s] %d/%d candidates have unverified upload dates — "
                         "MANDATORY date verification starting...",
                         self.canal, len(candidates_need_verification), len(all_candidates))

            verified = []
            discarded_count = 0

            for candidate in candidates_need_verification:
                real_date = self._fetch_real_upload_date(candidate.get("url", ""))
                if real_date is not None:
                    upload_dt = real_date.replace(tzinfo=timezone.utc)
                    hours = (datetime.now(timezone.utc) - upload_dt).total_seconds() / 3600

                    if hours <= 0:
                        logger.warning("[%s] Skipping candidate with invalid age: '%s'",
                                       self.canal, candidate.get("title", "")[:60])
                        discarded_count += 1
                        continue

                    # Update candidate with real date
                    candidate["upload_date"] = real_date.strftime("%Y%m%d")
                    candidate["hours_since_pub"] = round(hours, 1)
                    candidate["date_verified"] = True

                    # Re-score with real date
                    if hours < 168:
                        freshness = 1.0
                    elif hours < 336:
                        freshness = 0.7
                    else:
                        freshness = 0.4
                    candidate["viral_score"] = round(
                        candidate["views"] / max(hours, 1) * freshness, 1
                    )

                    tag = "✅" if hours <= self.max_age_days * 24 else "📦"
                    logger.warning("[%s] %s '%s' (%.0fh old, views=%s, score=%.0f)",
                                   self.canal, tag, candidate.get("title", "")[:60],
                                   hours, candidate["views"], candidate["viral_score"])
                    verified.append(candidate)
                else:
                    logger.warning("[%s] DISCARDING candidate with unknown age: '%s'",
                                   self.canal, candidate.get("title", "")[:60])
                    discarded_count += 1

            # Sort by views descending — take best regardless of age
            verified.sort(key=lambda x: x.get("views", 0), reverse=True)
            all_candidates = verified

            within_window = [c for c in verified if c.get("hours_since_pub", 999) <= self.max_age_days * 24]
            logger.info("[%s] Date verification: %d verified (%d within %dd window), %d discarded",
                         self.canal, len(verified), len(within_window),
                         self.max_age_days, discarded_count)

            # If nothing within 180d, mention fallback
            if not within_window and verified:
                logger.warning("[%s] ⚠ No videos within %dd window. Using highest-viewed: '%s' (views=%s)",
                               self.canal, self.max_age_days,
                               verified[0].get("title", "")[:60], verified[0].get("views", 0))

        # Sort by views descending
        all_candidates.sort(key=lambda x: x.get("views", 0), reverse=True)
        top_candidates = all_candidates[:self.max_candidates]

        # Cache
        self._cached_candidates = top_candidates
        self._last_search_time = time.time()

        logger.info("[%s] Viral discovery complete: %d candidates → %d top candidates",
                    self.canal, len(all_candidates), len(top_candidates))

        if top_candidates:
            best = top_candidates[0]
            age_str = f"{best.get('hours_since_pub', '?'):.0f}h ago" if best.get('hours_since_pub', -1) > 0 else "?"
            logger.info("[%s] Top candidate: '%s' (views=%s, %s, verified=%s)",
                        self.canal, best["title"][:60],
                        best["views"], age_str,
                        best.get("date_verified", False))

        # Restore original max_age_days after randomization
        self.max_age_days = original_max_age_days

        return top_candidates

    # ── Multi-strategy discovery ─────────────────────────────────────

    def _discover_with_queries(
        self, queries: list[str], min_views: int = None, max_age_days: int = None,
        max_candidates: int = None, force_refresh: bool = True,
    ) -> list[dict]:
        """Run discovery with specific queries and thresholds, bypassing cache.

        Used by discover_multi_strategy() for Strategy 1-6.
        """
        saved_views = self.min_views
        saved_age = self.max_age_days
        saved_max = self.max_candidates
        saved_cache = list(self._cached_candidates)
        saved_last = self._last_search_time

        try:
            if min_views is not None:
                self.min_views = min_views
            if max_age_days is not None:
                self.max_age_days = max_age_days
            if max_candidates is not None:
                self.max_candidates = max_candidates

            self._suggested_queries = queries
            self._cached_candidates = []
            self._last_search_time = 0.0

            return self._discover_candidates(force_refresh=force_refresh)
        finally:
            self.min_views = saved_views
            self.max_age_days = saved_age
            self.max_candidates = saved_max
            self._cached_candidates = saved_cache
            self._last_search_time = saved_last

    def _generate_title_queries(self, candidates: list[dict], count: int = 15) -> list[str]:
        """Strategy 5: Extract key phrases from top candidates' titles as new queries."""
        import re

        stopwords = {"the", "a", "an", "of", "in", "on", "to", "for", "and", "or",
                     "is", "it", "that", "this", "with", "was", "are", "be", "from",
                     "by", "at", "as", "but", "not", "we", "you", "all", "just",
                     "its", "has", "have", "had", "been", "can", "will", "would",
                     "what", "when", "where", "which", "who", "how"}

        queries = []
        seen = set()

        for c in candidates[:5]:
            title = c.get("title", "")
            # Extract 2-4 word phrases that look like searchable topics
            words = re.findall(r'\b[a-zA-Z]{3,}\b', title.lower())
            for i in range(len(words)):
                for length in [3, 4]:
                    if i + length <= len(words):
                        phrase = " ".join(words[i:i+length])
                        filtered = [w for w in phrase.split() if w not in stopwords]
                        if len(filtered) >= 2 and phrase not in seen:
                            seen.add(phrase)
                            queries.append(phrase)
                            if len(queries) >= count:
                                return queries[:count]

        # Fallback: use full titles as queries
        for c in candidates[:count]:
            title = c.get("title", "")
            if title and title not in seen:
                seen.add(title)
                queries.append(title)

        logger.info("[%s] Title expansion: %d queries generated from %d candidate titles",
                    self.canal, len(queries), min(len(candidates), 5))
        return queries[:count]

    def _get_genre_queries(self) -> list[str]:
        """Strategy 6: Genre-level fallback queries without niche specificity."""
        # Map niche keywords to genres (broad, always returns results on YouTube)
        genre_map = {
            "lost civilizations": [
                "ancient history documentary full length",
                "archaeology documentary 2024",
                "forgotten history documentary",
                "ancient mysteries documentary",
                "history documentary ancient world",
            ],
            "ancient mysteries": [
                "unsolved mysteries documentary",
                "ancient aliens documentary full",
                "mysterious discoveries documentary",
                "history's greatest mysteries",
                "unexplained phenomena documentary",
            ],
            "forgotten civilizations": [
                "lost cities documentary",
                "ancient civilizations documentary",
                "vanished empires documentary",
                "archaeological discoveries documentary",
                "prehistoric civilizations documentary",
            ],
            "ancient technology documentary": [
                "ancient engineering documentary",
                "lost technology documentary",
                "ancient inventions documentary",
                "history of technology documentary",
                "ancient marvels documentary",
            ],
            "archaeological discoveries": [
                "greatest archaeological finds documentary",
                "ancient artifacts documentary",
                "archaeology documentary 2024",
                "incredible discoveries documentary",
                "history documentary ancient",
            ],
            "ancient ruins unexplained": [
                "mysterious ancient structures documentary",
                "unexplained ruins documentary",
                "ancient monuments documentary",
                "lost temples documentary",
                "ancient architecture documentary",
            ],
        }

        # Match channel keywords to genres
        queries = []
        seen = set()
        for kw in self.keywords_eng:
            kw_lower = kw.lower()
            for genre_key, genre_queries in genre_map.items():
                if genre_key in kw_lower:
                    for q in genre_queries:
                        if q not in seen:
                            seen.add(q)
                            queries.append(q)

        # Fallback: universal genre queries
        if len(queries) < 8:
            universal = [
                "best documentary 2024 history",
                "incredible documentary full length",
                "fascinating documentary history",
                "amazing history documentary",
                "world history documentary 2024",
                "documentary history ancient civilizations",
                "top documentary history",
                "must watch documentary history",
            ]
            for q in universal:
                if q not in seen:
                    seen.add(q)
                    queries.append(q)

        logger.info("[%s] Genre queries: %d generated", self.canal, len(queries))
        return queries[:15]

    def discover_multi_strategy(self, db=None) -> list[dict]:
        """Run all 6 discovery strategies and return deduplicated candidates.

        This is the FASE A (Discovery) — cheap, no downloading, ~60 seconds.
        Strategies:
          1. Niche keywords + viral formats
          2. AI-generated semantic concepts (via viral_query_builder)
          3. Playlist-specific keywords
          4. Natural-language queries (via viral_query_builder)
          5. Title expansion from top results
          6. Genre-level fallback queries

        Returns list of candidate dicts with keys:
          video_id, title, url, views, viral_score, hours_since_pub,
          duration_sec, channel_name, thumbnail_url, description, upload_date
        """
        t0 = time.time()
        logger.info("[%s] ========== MULTI-STRATEGY DISCOVERY START ==========", self.canal)

        all_candidates: list[dict] = []
        strategy_results: dict[str, int] = {}

        # ── Collect playlist keyword info ──
        pl_keywords = []
        try:
            from database.db_extended import ExtendedDatabase
            ext_db = ExtendedDatabase() if db is None else db
            channel_id = None
            for row in ext_db._connect().execute(
                "SELECT id FROM channels WHERE slug = ?", (self.slug,)
            ).fetchall():
                channel_id = row["id"]
                break
            if channel_id:
                ch = ext_db.get_channel(channel_id)
                if ch:
                    import json
                    cj = ch.get("config_json", "{}")
                    if isinstance(cj, str):
                        cj = json.loads(cj) if cj else {}
                    generated = cj.get("PLAYLISTS_GENERATED", [])
                    for pl_cfg in generated:
                        pl_keywords.extend(pl_cfg.get("keywords_en", [])[:5])
        except Exception as e:
            logger.debug("[%s] Could not load playlist keywords: %s", self.canal, e)

        # ═════════════════════════════════════════════════════════
        # Strategy 1: Niche keywords + viral formats
        # ═════════════════════════════════════════════════════════
        logger.info("[%s] Strategy 1/6: Niche keywords + viral formats", self.canal)
        s1_queries = self._build_queries()  # uses current _suggested_queries or keywords
        if self._suggested_queries:
            s1_queries = list(self._suggested_queries)  # use playlist-targeted if set
            self._suggested_queries = []  # clear for subsequent strategies
        candidates = self._discover_with_queries(s1_queries)
        all_candidates.extend(candidates)
        strategy_results["1_niche_keywords"] = len(candidates)
        logger.info("[%s]   Strategy 1: %d candidates", self.canal, len(candidates))

        # ═════════════════════════════════════════════════════════
        # Strategy 2: AI-generated semantic concepts
        # ═════════════════════════════════════════════════════════
        logger.info("[%s] Strategy 2/6: AI semantic concepts", self.canal)
        try:
            from pipeline.viral_query_builder import build_expanded_queries
            s2_queries = build_expanded_queries(
                channel_slug=self.slug,
                channel_name=getattr(self.config, "CANAL_NAME", self.canal),
                channel_theme=getattr(self.config, "CANAL_THEME", ""),
                niche_keywords=self.keywords_eng[:5],
                count=20,
                db=db,
            )
        except Exception as e:
            logger.warning("[%s] Strategy 2 (AI concepts) failed: %s — using fallback", self.canal, e)
            s2_queries = [
                f"{kw} documentary 2024" for kw in self.keywords_eng[:10]
            ]
        if s2_queries:
            candidates = self._discover_with_queries(s2_queries)
            all_candidates.extend(candidates)
            strategy_results["2_ai_concepts"] = len(candidates)
            logger.info("[%s]   Strategy 2: %d candidates from %d queries",
                       self.canal, len(candidates), len(s2_queries))

        # ═════════════════════════════════════════════════════════
        # Strategy 3: Playlist-specific keywords
        # ═════════════════════════════════════════════════════════
        if pl_keywords:
            logger.info("[%s] Strategy 3/6: Playlist keywords (%d queries)", self.canal, len(pl_keywords))
            # Add viral formats to playlist keywords
            viral_fmts = ["{} documentary", "{} full documentary", "best {} stories"]
            s3_queries = list(pl_keywords)
            for kw in pl_keywords[:5]:
                for fmt in viral_fmts:
                    formatted = fmt.format(kw)
                    if formatted not in s3_queries and len(s3_queries) < 25:
                        s3_queries.append(formatted)
            candidates = self._discover_with_queries(s3_queries)
            all_candidates.extend(candidates)
            strategy_results["3_playlist_kw"] = len(candidates)
            logger.info("[%s]   Strategy 3: %d candidates", self.canal, len(candidates))
        else:
            logger.info("[%s] Strategy 3/6: SKIP — no playlist keywords", self.canal)
            strategy_results["3_playlist_kw"] = 0

        # ═════════════════════════════════════════════════════════
        # Strategy 4: Natural-language queries via LLM
        # ═════════════════════════════════════════════════════════
        logger.info("[%s] Strategy 4/6: Natural-language queries", self.canal)
        try:
            from pipeline.viral_query_builder import build_natural_language_queries
            s4_queries = build_natural_language_queries(
                channel_slug=self.slug,
                channel_name=getattr(self.config, "CANAL_NAME", self.canal),
                channel_theme=getattr(self.config, "CANAL_THEME", ""),
                niche_keywords=self.keywords_eng[:5],
                count=15,
                db=db,
            )
        except Exception as e:
            logger.warning("[%s] Strategy 4 (natural language) failed: %s", self.canal, e)
            s4_queries = [
                f"most interesting {kw} explained" for kw in self.keywords_eng[:8]
            ]
        if s4_queries:
            candidates = self._discover_with_queries(s4_queries)
            all_candidates.extend(candidates)
            strategy_results["4_natural_lang"] = len(candidates)
            logger.info("[%s]   Strategy 4: %d candidates", self.canal, len(candidates))

        # ═════════════════════════════════════════════════════════
        # Strategy 5: Title expansion from top results
        # ═════════════════════════════════════════════════════════
        logger.info("[%s] Strategy 5/6: Title expansion", self.canal)
        # Get unique top candidates so far, sorted by viral_score
        seen_ids = set()
        unique_so_far = []
        for c in all_candidates:
            vid = c.get("video_id")
            if vid and vid not in seen_ids:
                seen_ids.add(vid)
                unique_so_far.append(c)
        unique_so_far.sort(key=lambda x: x.get("views", 0), reverse=True)

        s5_queries = self._generate_title_queries(unique_so_far[:20], count=15)
        if s5_queries:
            candidates = self._discover_with_queries(s5_queries)
            all_candidates.extend(candidates)
            strategy_results["5_title_expansion"] = len(candidates)
            logger.info("[%s]   Strategy 5: %d candidates from title expansion",
                       self.canal, len(candidates))

        # ═════════════════════════════════════════════════════════
        # Strategy 6: Genre-level fallback
        # ═════════════════════════════════════════════════════════
        logger.info("[%s] Strategy 6/6: Genre queries", self.canal)
        s6_queries = self._get_genre_queries()
        candidates = self._discover_with_queries(s6_queries)
        all_candidates.extend(candidates)
        strategy_results["6_genre"] = len(candidates)
        logger.info("[%s]   Strategy 6: %d candidates", self.canal, len(candidates))

        # ── Merge + deduplicate + filter + final safety check ────
        seen_vids: set[str] = set()
        seen_urls: set[str] = set()
        unique: list[dict] = []
        unverified_pool: list[dict] = []
        unverified_discarded = 0

        for c in all_candidates:
            vid = c.get("video_id", "")
            url = c.get("url", "")
            if not vid or vid in seen_vids:
                continue
            # Check if already in DB (already used in another generation)
            if db and url:
                try:
                    existing = db.get_content_by_url(url, self.canal)
                    if existing:
                        continue
                except Exception:
                    pass
            # ── FINAL SAFETY: prefer verified, fall back to best unverified if needed ──
            if not c.get("date_verified", False):
                unverified_pool.append(c)
                unverified_discarded += 1
                continue
            seen_vids.add(vid)
            if url:
                seen_urls.add(url)
            unique.append(c)

        # If no verified candidates, promote best unverified as last resort
        if not unique and unverified_pool:
            unverified_pool.sort(key=lambda x: x.get("views", 0), reverse=True)
            best_unverified = unverified_pool[0]
            logger.warning("[%s] ⚠ No verified candidates. Falling back to best unverified: '%s' (views=%s)",
                           self.canal, best_unverified.get("title", "")[:60],
                           best_unverified.get("views", 0))
            unique = [best_unverified]
            unverified_discarded -= 1  # don't count this one as discarded

        # Sort by views descending — once age-filtered, pick the one with most views
        unique.sort(key=lambda x: x.get("views", 0), reverse=True)

        elapsed = time.time() - t0
        logger.info("[%s] ========== DISCOVERY COMPLETE (%.1fs) ==========", self.canal, elapsed)
        logger.info("[%s] Strategies: %s", self.canal,
                    ", ".join(f"{k}={v}" for k, v in strategy_results.items()))
        logger.info("[%s] Total raw candidates: %d → unique after dedup+DB+verification: %d",
                    self.canal, sum(strategy_results.values()), len(unique))
        if unverified_discarded > 0:
            logger.warning("[%s] ⚠ Discarded %d candidates in final merge (dates unverified)",
                           self.canal, unverified_discarded)

        if unique:
            best = unique[0]
            age_str = f"{best.get('hours_since_pub', '?'):.0f}h ago" if best.get('hours_since_pub', -1) > 0 else "?"
            logger.info("[%s] Best candidate: '%s' (views=%s, %s, verified=%s)",
                        self.canal, best.get("title", "")[:60],
                        best.get("views", 0), age_str,
                        best.get("date_verified", False))

        return unique

    def process_candidate(self, candidate: dict) -> dict | None:
        """FASE B: Process a single candidate through the full viral pipeline.

        Steps: download audio → transcribe → translate → adapt → build blocks.
        Does NOT save to DB (caller handles that).

        Returns full item dict (same shape as scrape() output) or None if fails.
        """
        video_url = candidate.get("url", "")
        video_id = candidate.get("video_id", "")

        logger.info("[%s] Processing candidate: '%s' (score=%.0f, views=%s)",
                    self.canal, candidate.get("title", "")[:60],
                    candidate.get("viral_score", 0), candidate.get("views", 0))

        # Step 1: Download audio
        audio_path = self._download_audio(video_url, video_id)
        if not audio_path:
            logger.warning("[%s] ✗ Candidate failed at DOWNLOAD: %s", self.canal, video_id)
            return None

        # Step 2: Transcribe
        transcript_en = self._transcribe(audio_path)
        if not transcript_en:
            logger.warning("[%s] ⚠ Transcription empty — using title+description as fallback", self.canal)
            transcript_en = f"{candidate.get('title', '')}. {candidate.get('description', '')}"
            if len(transcript_en.strip()) < 50:
                logger.warning("[%s] ✗ Candidate failed at TRANSCRIBE (fallback too short)", self.canal)
                return None
        logger.info("[%s]   Transcribed: %d words EN", self.canal, len(transcript_en.split()))

        # Step 3: Translate + paraphrase
        translated_script, translated_title, translated_desc = self._translate_and_paraphrase(
            transcript_en, candidate.get("title", "")
        )
        if not translated_script:
            logger.warning("[%s] ✗ Candidate failed at TRANSLATE", self.canal)
            return None
        logger.info("[%s]   Translated: %d words ES", self.canal, len(translated_script.split()))

        # Step 4: Adapt duration
        adapted_script = self._adapt_duration(translated_script)
        if not adapted_script:
            logger.warning("[%s] ✗ Candidate failed at ADAPT (duration gap too large)", self.canal)
            return None
        logger.info("[%s]   Adapted: %d words (~%.1f min)",
                    self.canal, len(adapted_script.split()),
                    duration_for_words(self.config, len(adapted_script.split())))

        # Step 5: Build blocks
        blocks = self._build_script_blocks(adapted_script)
        logger.info("[%s]   Blocks: %d — %s", self.canal, len(blocks),
                    [b.get("tipo", "?") for b in blocks[:6]])

        # Step 6: Construct metadata
        viral_meta = {
            "original_title": candidate.get("title", ""),
            "translated_title": translated_title,
            "translated_description": translated_desc or "",
            "blocks": blocks,
            "original_views": candidate.get("views", 0),
            "original_channel": candidate.get("channel_name", ""),
            "original_url": video_url,
            "word_count": len(adapted_script.split()),
            "estimated_duration_min": round(
                duration_for_words(self.config, len(adapted_script.split())), 1
            ),
        }

        # Cleanup audio
        try:
            if audio_path and Path(audio_path).exists():
                Path(audio_path).unlink()
        except OSError:
            pass

        return {
            "source": "youtube_viral",
            "url": video_url,
            "title": translated_title or candidate.get("title", ""),
            "text": transcript_en[:500],
            "subreddit": candidate.get("channel_name", ""),
            "score": int(candidate.get("viral_score", 0)),
            "source_mode": "viral",
            "viral_original_title": candidate.get("title", ""),
            "viral_original_description": candidate.get("description", ""),
            "viral_original_thumbnail_url": candidate.get("thumbnail_url", ""),
            "viral_original_video_url": video_url,
            "viral_views": candidate.get("views", 0),
            "viral_upload_date": candidate.get("upload_date", ""),
            "viral_duration_sec": candidate.get("duration_sec", 0),
            "viral_channel_name": candidate.get("channel_name", ""),
            "viral_score": candidate.get("viral_score", 0.0),
            "viral_script_es": adapted_script,
            "viral_meta_json": json.dumps(viral_meta, ensure_ascii=False),
            "viral_blocks_json": json.dumps(blocks, ensure_ascii=False),
        }

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

    @staticmethod
    def _title_similarity(original_en: str, new_es: str) -> float:
        """Calculate similarity between original English title and new Spanish title.
        
        Returns a float 0.0–1.0 where >0.5 means likely too similar (copyright risk).
        Uses character-level difflib ratio which catches structural similarity
        even across languages.
        """
        import difflib
        if not original_en or not new_es:
            return 0.0
        # Normalize: lowercase both
        a = original_en.lower()
        b = new_es.lower()
        return difflib.SequenceMatcher(None, a, b).ratio()

    @staticmethod
    def _strip_title_artifacts(title: str) -> str:
        """Remove common LLM artifacts from generated titles."""
        import re
        title = title.strip().strip('"').strip("'").strip()
        # Remove markdown-style formatting
        title = re.sub(r'^\*\*|\*\*$', '', title)
        title = re.sub(r'^#\s*', '', title)
        # Remove "Título:" or "Title:" prefixes
        title = re.sub(r'^[Tt]ítulo:?\s*', '', title)
        return title.strip()

    def _translate_and_paraphrase(self, english_text: str, original_title: str,
                                    original_description: str = "") -> tuple[str | None, str | None, str | None]:
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

        # 2. Generate a FRESH Spanish title (NOT a translation of the original)
        if original_title:
            # ── Step A: Generate title with creative formula ──
            title_msg = (
                f"Create a completely ORIGINAL Spanish YouTube title for a video about the topic:\n\n"
                f"\"{original_title}\"\n\n"
                f"REMEMBER: Do NOT translate this title. Create a fresh one with different words."
            )
            translated_title = self._call_llm(
                _TITLE_CREATION_SYSTEM_PROMPT, title_msg, temperature=0.85
            )

            # ── Step B: Anti-plagiarism check — retry if too similar ──
            if translated_title:
                similarity = self._title_similarity(original_title.lower(), translated_title.lower())
                retry_count = 0
                while similarity > 0.45 and retry_count < 3:
                    logger.warning(
                        "[%s] Title too similar to original (%.0f%%). Retrying...",
                        self.canal, similarity * 100,
                    )
                    retry_msg = (
                        f"Original title: \"{original_title}\"\n"
                        f"Your last attempt (too similar — {similarity:.0%}): \"{translated_title}\"\n\n"
                        f"Create a COMPLETELY DIFFERENT title. Use different words and structure."
                    )
                    translated_title = self._call_llm(
                        _TITLE_CREATION_SYSTEM_PROMPT + "\n" + _TITLE_ANTI_PLAGIARISM_RETRY_PROMPT,
                        retry_msg, temperature=0.9,
                    )
                    if not translated_title:
                        break
                    similarity = self._title_similarity(original_title.lower(), translated_title.lower())
                    retry_count += 1

                if translated_title:
                    translated_title = self._strip_title_artifacts(translated_title)
                    if retry_count > 0:
                        logger.info(
                            "[%s] Title accepted after %d anti-plagiarism retries (%.0f%% similarity)",
                            self.canal, retry_count, similarity * 100,
                        )
                    else:
                        logger.info(
                            "[%s] Title accepted (%.0f%% similarity to original — OK)",
                            self.canal, similarity * 100,
                        )

        # 3. Generate a FRESH Spanish description from the translated script + topic
        if translated_script:
            # Use the translated script as the source — NEVER the original description
            # Only the topic from the original title is used for context
            topic_hint = original_title[:200] if original_title else "contenido viral"
            desc_msg = (
                f"Write an ORIGINAL Spanish YouTube description for a video with this title:\n"
                f"\"{translated_title or topic_hint}\"\n\n"
                f"Here is the TRANSLATED SCRIPT (use this as your source material, NOT any "
                f"English description):\n\n{translated_script[:4000]}\n\n"
                f"IMPORTANT: Do NOT translate any English description. Create original text "
                f"based on the Spanish script above."
            )
            translated_description = self._call_llm(
                _DESCRIPTION_CREATION_SYSTEM_PROMPT, desc_msg, temperature=0.6
            )
            if translated_description:
                translated_description = translated_description.strip()
                logger.info(
                    "[%s] Description generated: %d chars",
                    self.canal, len(translated_description),
                )
            else:
                logger.warning("[%s] Description generation returned empty", self.canal)

        return translated_script, translated_title, translated_description

    def _adapt_duration(self, script_es: str) -> str | None:
        """Adapt the Spanish script to fit the channel's target video duration.

        Uses voice_timing.py (voice-rate-aware WPM) instead of a hardcoded
        150 words/min assumption.  Single source of truth.
        """
        if not script_es:
            return None

        from config.voice_timing import words_per_minute_real, words_for_duration, duration_for_words

        word_count = len(script_es.split())
        current_minutes = duration_for_words(self.config, word_count)
        target_min = max(self.target_duration_min, 1)
        target_max = self.target_duration_max

        # If already within target range, accept without adaptation
        if target_min <= current_minutes <= target_max:
            logger.info("[%s] Script duration %.1f min already in range [%d-%d min] — skipping adaptation",
                        self.canal, current_minutes, target_min, target_max)
            return script_es

        self._init_llm()

        target_words_min = words_for_duration(self.config, target_min)
        target_words_max = words_for_duration(self.config, target_max)

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
                        self.canal, word_count, new_words, current_minutes,
                        duration_for_words(self.config, new_words))

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
                    duration_for_words(self.config, len(adapted_script.split())))

        # Step 7: Build block structure for TTS
        blocks = self._build_script_blocks(adapted_script)
        logger.info("[%s] Step 7: Built %d blocks for TTS: %s",
                    self.canal, len(blocks),
                    [b["tipo"] for b in blocks])

        # Step 8: Construct metadata JSON (for viral_cloner use later)
        viral_meta = {
            "original_title": top["title"],
            "translated_title": translated_title,
            "translated_description": translated_desc or "",
            "blocks": blocks,
            "original_views": top["views"],
            "original_channel": top["channel_name"],
            "original_url": video_url,
            "word_count": len(adapted_script.split()),
            "estimated_duration_min": round(
                duration_for_words(self.config, len(adapted_script.split())), 1
            ),
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
