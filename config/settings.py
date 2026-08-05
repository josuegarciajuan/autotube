"""Autotube configuration loader.

Loads settings from environment variables (.env file) and provides
type-safe access to all configuration values.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=True)


# ── Paths ──────────────────────────────────────────────────────
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(PROJECT_ROOT / "output")))
AUDIO_DIR = OUTPUT_DIR / "audio"
IMAGES_DIR = OUTPUT_DIR / "images"
VIDEOS_DIR = OUTPUT_DIR / "videos"
THUMBNAILS_DIR = OUTPUT_DIR / "thumbnails"
ASSETS_DIR = PROJECT_ROOT / "assets"
TOKENS_DIR = PROJECT_ROOT / "tokens"
LOGS_DIR = PROJECT_ROOT / "logs"
DATABASE_PATH = os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "autotube.db"))

# Ensure output dirs exist
for d in [OUTPUT_DIR, AUDIO_DIR, IMAGES_DIR, VIDEOS_DIR, THUMBNAILS_DIR, TOKENS_DIR, LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# ── API Keys ───────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
GOOGLE_CLIENT_SECRET_PATH = os.getenv(
    "GOOGLE_CLIENT_SECRET_PATH",
    str(PROJECT_ROOT / "config" / "client_secret.json"),
)

# ── Channels ───────────────────────────────────────────────────
ACTIVE_CHANNELS = [
    c.strip()
    for c in os.getenv("ACTIVE_CHANNELS", "canal2,canal3,canal4").split(",")
    if c.strip()
]


# ── Scheduling ─────────────────────────────────────────────────
WEEK1_VIDEOS_PER_DAY = int(os.getenv("WEEK1_VIDEOS_PER_DAY", "1"))
WEEK2_VIDEOS_PER_DAY = int(os.getenv("WEEK2_VIDEOS_PER_DAY", "2"))
WEEK3_VIDEOS_PER_DAY = int(os.getenv("WEEK3_VIDEOS_PER_DAY", "3"))

# Pipeline start date (ISO format: YYYY-MM-DD). Defaults to today.
PIPELINE_START_DATE = os.getenv("PIPELINE_START_DATE", "")

# ── Generation worker mode ─────────────────────────────────────
# True  = generation runs in independent subprocess (survives API restarts)
# False = generation runs in-process (legacy behavior, dies with API)
USE_SUBPROCESS_WORKER = os.getenv("USE_SUBPROCESS_WORKER", "true").lower() in ("1", "true", "yes")


# ── LLM Provider (DeepSeek / OpenAI) ──────────────────────────
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_API_KEY = os.getenv("LLM_API_KEY", OPENAI_API_KEY)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")

# Multi-model tiers for DeepSeek V4 (env-configurable, defaults below)
# Tier 1: Script generation — highest quality, thinking enabled
LLM_MODEL_SCRIPT = os.getenv("LLM_MODEL_SCRIPT", "deepseek-v4-pro")
# Tier 2: Creative tasks (metadata, thumbnails, shorts, marketing) — thinking enabled
LLM_MODEL_CREATIVE = os.getenv("LLM_MODEL_CREATIVE", "deepseek-v4-flash")
# Tier 3: Default (theme extraction, comments, classification) — no thinking
# (uses LLM_MODEL above — currently deepseek-v4-flash)
# Tier 4: Insights / AI self-optimization (multi-pass analysis) — thinking enabled
LLM_MODEL_INSIGHTS = os.getenv("LLM_MODEL_INSIGHTS", LLM_MODEL_SCRIPT)

# ── Multi-model pool for script generation (v21 failover) ────
# Comma-separated list of provider:model_id entries in priority order.
# Each model is tried with up to LLM_POOL_RETRIES_PER_MODEL attempts
# before failing over to the next. If all models fail, emergency mode
# generates a minimal script from the source content.
# Format: "deepseek:deepseek-v4-pro,openai:gpt-4o-mini,deepseek:deepseek-v4-flash"
LLM_POOL_MODELS = os.getenv(
    "LLM_POOL_MODELS",
    "deepseek:deepseek-v4-pro,openai:gpt-4o-mini",
)
LLM_POOL_RETRIES_PER_MODEL = os.getenv("LLM_POOL_RETRIES_PER_MODEL", "3")

# OpenAI (legacy / fallback) ────────────────────────────────────
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.8"))
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "4096"))

# ── Vision model (image analysis: thumbnails, etc.) ────────────
# Uses OpenAI by default (gpt-4o-mini supports vision/multimodal)
# Override with VISION_MODEL / VISION_API_KEY / VISION_BASE_URL env vars
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o-mini")
VISION_API_KEY = os.getenv("VISION_API_KEY", OPENAI_API_KEY)
VISION_BASE_URL = os.getenv("VISION_BASE_URL", "https://api.openai.com/v1")


# ── Video ──────────────────────────────────────────────────────
VIDEO_FPS = int(os.getenv("VIDEO_FPS", "24"))
VIDEO_RESOLUTION = (1920, 1080)
VIDEO_BITRATE = os.getenv("VIDEO_BITRATE", "6000k")
VIDEO_CODEC = os.getenv("VIDEO_CODEC", "libx264")
VIDEO_MIN_DURATION = int(os.getenv("VIDEO_MIN_DURATION", "480"))   # seconds (8 min)
VIDEO_MAX_DURATION = int(os.getenv("VIDEO_MAX_DURATION", "840"))   # seconds (14 min)

# ffmpeg encoding preset: ultrafast > veryfast > faster > fast > medium (default) > slow > slower > veryslow
# "fast" gives ~2x speed over "medium" with negligible quality loss
FFMPEG_PRESET_DEFAULT = os.getenv("FFMPEG_PRESET_DEFAULT", "fast")

# ── Memory guard thresholds (MB) ───────────────────────────────
VIDEO_MEMORY_GUARD_MB = int(os.getenv("VIDEO_MEMORY_GUARD_MB", "2000"))
LOW_MEMORY_WARN_MB = int(os.getenv("LOW_MEMORY_WARN_MB", "1500"))
MIN_FREE_FOR_RENDER_MB = int(os.getenv("MIN_FREE_FOR_RENDER_MB", "3000"))
MIN_FREE_FOR_DISPATCH_MB = int(os.getenv("MIN_FREE_FOR_DISPATCH_MB", "4000"))

# ── Render timeout ─────────────────────────────────────────────
# Timeout = min(max(video_duration * RENDER_TIMEOUT_MULTIPLIER, RENDER_TIMEOUT_MIN_SEC), RENDER_TIMEOUT_MAX_SEC)
RENDER_TIMEOUT_MULTIPLIER = float(os.getenv("RENDER_TIMEOUT_MULTIPLIER", "5.0"))
RENDER_TIMEOUT_MIN_SEC = int(os.getenv("RENDER_TIMEOUT_MIN_SEC", "7200"))  # 2h floor — long renders (50+ scenes + effects)
RENDER_TIMEOUT_MAX_SEC = int(os.getenv("RENDER_TIMEOUT_MAX_SEC", "14400"))  # 4h ceiling — generous for complex videos

# ── Orphan detection ───────────────────────────────────────────
HEARTBEAT_ORPHAN_TIMEOUT_MIN = int(os.getenv("HEARTBEAT_ORPHAN_TIMEOUT_MIN", "60"))  # 1h — heartbeat every ~30s, 120 missed = dead
MAX_RETRY_ATTEMPTS = int(os.getenv("MAX_RETRY_ATTEMPTS", "3"))

# ── Shorts ─────────────────────────────────────────────────────
SHORTS_RESOLUTION = (1080, 1920)
SHORTS_FPS = 30
SHORTS_BITRATE = "6000k"
SHORTS_MAX_CLIPS_PER_VIDEO = 5
SHORTS_CLIP_SCHEDULE = [
    {"offset_days": 1, "count": 1},
    {"offset_days": 2, "count": 1},
    {"offset_days": 3, "count": 1},
]
SHORTS_NATIVE_SCHEDULE = [
    {"week": 1, "per_day": 1},
    {"week": 2, "per_day": 2},
    {"week": 3, "per_day": 3},
]
SHORTS_NATIVE_MAX_DAILY = 4

# ── Shorts frequency targets (v14: 4/day per channel) ──────
# Floor and ceiling enforced by the scheduler (compute_daily_shorts_slots).
# v14: lowered from 6 to 4 — matches per-channel shorts_native_per_day (4).
# Recovery planner no longer uses this floor (uses native_target + clip_target directly).
MIN_DAILY_SHORTS = int(os.getenv("MIN_DAILY_SHORTS", "4"))
MAX_DAILY_SHORTS = int(os.getenv("MAX_DAILY_SHORTS", "6"))

# Conservative QUOTA cap: YouTube API default 10,000 units/day.
# v14: aligned with updated per-channel config (4 native + ~6 clips = ~10/day).
MAX_DAILY_SHORTS_PER_CHANNEL_QUOTA_SAFE = int(os.getenv(
    "MAX_DAILY_SHORTS_QUOTA_SAFE", "6"
))  # Safe with default 10,000 unit quota

# ── Shorts video/image mix strategy ──────────────────────────
SHORTS_VIDEO_PCT = float(os.getenv("SHORTS_VIDEO_PCT", "0.55"))     # target fraction of scenes using video
SHORTS_MIN_VIDEO_PCT = float(os.getenv("SHORTS_MIN_VIDEO_PCT", "0.30"))  # minimum acceptable video ratio
SHORTS_KEN_BURNS_ZOOM = 0.0015  # zoom increment per frame for Ken Burns on still images
SHORTS_CROSSFADE_DUR = 1.0      # crossfade seconds between scenes

# Generic fallback queries when block-specific queries return no results
SHORTS_FALLBACK_QUERIES = [
    "dramatic mystery atmosphere cinematic vertical",
    "historical documentary archival photography",
    "nature landscape exploration discovery cinematic",
    "ancient ruins archaeological artifacts cinematic",
    "dark moody atmospheric mysterious cinematic",
    "documentary storytelling dramatic cinematic portrait",
]


# ── Global media provider configuration ──────────────────────
# Shared by all channels. Each channel's MEDIA_STRATEGY should reference
# this list via `DEFAULT_VIDEO_PROVIDERS` rather than duplicating.
# ── Video providers in priority order ──────────────────────────
# Providers are ALGORITHM-LEVEL, not per-channel. All channels share
# this list via DEFAULT_VIDEO_PROVIDERS reference (see per-channel configs).
# coverr re-enabled 2026-07-21: search URL fixed (/search → /s)
DEFAULT_VIDEO_PROVIDERS = [
    {"name": "pexels", "api_key_env": "PEXELS_API_KEY"},
    {"name": "pixabay", "api_key_env": "PIXABAY_API_KEY"},
    {"name": "mixkit"},
    {"name": "coverr"},
    {"name": "youtube_cc"},
]

# Global fallback queries for generic scenes — shared across channels
DEFAULT_FALLBACK_QUERY = "cinematic atmosphere light rays nature 16:9"
DEFAULT_FALLBACK_QUERY_SIMPLE = "cinematic nature atmospheric hopeful"

DEFAULT_VIDEO_FALLBACK_QUERIES = [
    "drone aerial landscape nature cinematic 4k",
    "cinematic documentary b-roll atmospheric lighting",
    "slow motion nature water clouds sky dramatic",
]

# ── Pollo AI (image generation for thumbnails) ─────────────────
# Session cookie (tRPC web endpoint, bypasses Cloudflare via curl_cffi).
# Override with POLLO_SESSION_COOKIE env var, otherwise reads from
# /root/lamamionline-control/data/settings.json (shared with lamami).
POLLO_SESSION_COOKIE = os.getenv("POLLO_SESSION_COOKIE", "")
POLLO_IMAGE_MODEL = os.getenv("POLLO_IMAGE_MODEL", "pollo-image-v2")
# Legacy x-api-key (kept for reference; the active integration uses cookie+tRPC).
POLLO_AI_API_KEY = os.getenv("POLLO_AI_API_KEY", "")

# ── Thumbnail Quality Control ──────────────────────────────────
THUMBNAIL_QUALITY_THRESHOLD = int(os.getenv("THUMBNAIL_QUALITY_THRESHOLD", "7"))  # 0-10
THUMBNAIL_MAX_QC_ATTEMPTS = int(os.getenv("THUMBNAIL_MAX_QC_ATTEMPTS", "2"))

# ── Reddit API (OAuth — opcional, auto-activa la fuente primaria) ─
# Registrar app en https://www.reddit.com/prefs/apps (tipo "script")
# y copiar client_id y secret aquí para habilitar la fuente OAuth.
# Sin credenciales, el scraper usa mirrors (PullPush → Arctic Shift).
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = os.getenv(
    "REDDIT_USER_AGENT",
    "AutotubeContentBot/2.0 (by /u/yourusername)",
)

# ── Proxy (IP residencial para llamadas a YouTube) ─────────────
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() == "true"
PROXY_TYPE = os.getenv("PROXY_TYPE", "socks5")  # socks5 | http
PROXY_HOST = os.getenv("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.getenv("PROXY_PORT", "1080"))
PROXY_CHANNELS = [
    c.strip()
    for c in os.getenv("PROXY_CHANNELS", "").split(",")
    if c.strip()
]  # vacío = todos los canales usan proxy

# ── YouTube Stats collection ───────────────────────────────────
STATS_ENABLED = os.getenv("STATS_ENABLED", "true").lower() == "true"

# ⛔ INVARIANTE: stats auto-collection NUNCA debe estar activa.
# La recoleccion automatica cada 6h consume quota de API.
# Solo se permite recoleccion manual via POST /api/stats/collect (boton dashboard).
# STATS_AUTO_COLLECT esta hardcodeado a False — NO se puede cambiar con .env.
STATS_AUTO_COLLECT = False  # INVARIANTE: hardcodeado, no depende de env

# ── View Gap Monitor ─────────────────────────────────────────────
# Daily check comparing YT channel total views vs DB-tracked views.
# When the gap grows beyond the threshold in 24h, an alert is raised
# and (optionally) unregistered video IDs are auto-scanned via YT API.
ENABLE_DAILY_VIEW_GAP_CHECK = os.getenv("ENABLE_DAILY_VIEW_GAP_CHECK", "true").lower() == "true"
VIEW_GAP_THRESHOLD = int(os.getenv("VIEW_GAP_THRESHOLD", "500"))
VIEW_GAP_SCAN_UNREGISTERED = os.getenv("VIEW_GAP_SCAN_UNREGISTERED", "true").lower() == "true"
VIEW_GAP_INTERVAL_HOURS = int(os.getenv("VIEW_GAP_INTERVAL_HOURS", "24"))

# ── Video Lifecycle (post-upload promotion) ────────────────────
LIFECYCLE_ENABLED = os.getenv("LIFECYCLE_ENABLED", "true").lower() == "true"
LIFECYCLE_CHECK_INTERVAL_MIN = int(os.getenv("LIFECYCLE_CHECK_INTERVAL_MIN", "15"))

# ── First comment ──────────────────────────────────────────────
# Disabled by default — requires commentThreads OAuth scope which most
# channel tokens don't have, causing 403 errors on every attempt.
FIRST_COMMENT_ENABLED = os.getenv("FIRST_COMMENT_ENABLED", "false").lower() == "true"

# Timeline por defecto — delays relativos al momento de publicación
# Cada canal puede sobrescribir via LIFECYCLE_TIMELINE en su config
LIFECYCLE_DEFAULT_TIMELINE = [
    {"action": "first_comment",      "offset_minutes": 5},
    {"action": "comment_reply_1",    "offset_hours": 12},
    {"action": "comment_reply_2",    "offset_hours": 24},
    {"action": "ctr_check",          "offset_hours": 48},
    {"action": "metadata_reoptimize", "offset_hours": 72},
]

# ── Comment reply ──────────────────────────────────────────────
COMMENT_REPLY_MAX_PER_VIDEO = int(os.getenv("COMMENT_REPLY_MAX_PER_VIDEO", "5"))
COMMENT_REPLY_ENABLED = os.getenv("COMMENT_REPLY_ENABLED", "true").lower() == "true"

# ── Metadata reoptimization ────────────────────────────────────
METADATA_OPTIMIZE_ENABLED = os.getenv("METADATA_OPTIMIZE_ENABLED", "true").lower() == "true"
METADATA_OPTIMIZE_CTR_THRESHOLD = float(os.getenv("METADATA_OPTIMIZE_CTR_THRESHOLD", "3.0"))

# ── Logging ────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE = LOGS_DIR / "autotube.log"
