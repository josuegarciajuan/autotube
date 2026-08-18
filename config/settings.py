"""Autotube configuration loader.

Loads settings from environment variables (.env file) and provides
type-safe access to all configuration values.
"""

import json
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
AI_SCENES_DIR = OUTPUT_DIR / "ai_scenes"
AI_CACHE_DIR = AI_SCENES_DIR / "cache"
AI_BENCHMARKS_DIR = AI_SCENES_DIR / "benchmarks"
AI_TEST_DIR = AI_SCENES_DIR / "test"
DATABASE_PATH = os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "autotube.db"))

# Ensure output dirs exist
for d in [OUTPUT_DIR, AUDIO_DIR, IMAGES_DIR, VIDEOS_DIR, THUMBNAILS_DIR, TOKENS_DIR,
           LOGS_DIR, AI_SCENES_DIR, AI_CACHE_DIR, AI_BENCHMARKS_DIR, AI_TEST_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# ── API Keys ───────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
PIXABAY_API_TIMEOUT = int(os.getenv("PIXABAY_API_TIMEOUT", "30"))
GOOGLE_CLIENT_SECRET_PATH = os.getenv(
    "GOOGLE_CLIENT_SECRET_PATH",
    str(PROJECT_ROOT / "config" / "client_secret.json"),
)

# ── Channels ───────────────────────────────────────────────────
def _load_active_channels() -> list[str]:
    """Load active channels from DB, falling back to env var or empty list."""
    env_override = os.getenv("ACTIVE_CHANNELS", "")
    if env_override:
        return [c.strip() for c in env_override.split(",") if c.strip()]
    try:
        import sqlite3
        db_path = os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "autotube.db"))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT slug FROM channels WHERE active = 1 ORDER BY id"
        ).fetchall()
        conn.close()
        return [r["slug"] for r in rows]
    except Exception:
        return []

ACTIVE_CHANNELS = _load_active_channels()


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

# ── LLM Credit Monitoring ──────────────────────────────────────
# How often to check DeepSeek balance / OpenAI quota errors (hours)
LLM_CREDIT_CHECK_INTERVAL_HOURS = int(os.getenv("LLM_CREDIT_CHECK_INTERVAL_HOURS", "12"))
# DeepSeek balance below this threshold triggers a "low" warning
LLM_CREDIT_LOW_THRESHOLD_USD = float(os.getenv("LLM_CREDIT_LOW_THRESHOLD_USD", "2.00"))


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

# ── Shorts frequency targets ──────
# Floor and ceiling enforced by the scheduler (compute_daily_shorts_slots).
# Fase 0 (ago 2026): reducido de 8/10 a 3/4 para evitar "mass-produced content"
# (riesgo de ban/desmonetización) y el consumo de cuota (~75% era shorts).
MIN_DAILY_SHORTS = int(os.getenv("MIN_DAILY_SHORTS", "2"))
MAX_DAILY_SHORTS = int(os.getenv("MAX_DAILY_SHORTS", "2"))

# Conservative QUOTA cap: YouTube API default 10,000 units/day.
# v2: aligned with updated per-channel target (4 native + ~6 clips = ~10/day).
MAX_DAILY_SHORTS_PER_CHANNEL_QUOTA_SAFE = int(os.getenv(
    "MAX_DAILY_SHORTS_QUOTA_SAFE", "10"
))  # Safe with default 10,000 unit quota

# ── Fase cuota (ago 2026): SIN tope global de subidas ──
# El único tope es POR PROYECTO GCP: get_project_automatic_budget_units(project)
# (cuota real − reservados) repartido por la planificación quota-aware
# (planning_service.compute_daily_upload_allocation). Cada subida (video largo
# o short) consume 1.600 ud. No existe un cap global: dos proyectos con presupuesto
# sano suben cada uno su cupo (~6 subidas/día/proyecto).

# Emergency remediation gate. While true, no automatic upload path may start a
# new YouTube operation. It defaults to safe-on after the August 2026 quota
# incident and is only disabled after the operator validates the preflight.
YT_REMEDIATION_MODE = os.getenv("YT_REMEDIATION_MODE", "true").lower() == "true"

# ── Per-project quota budgets (Fase cuota, ago 2026) ──────────────
# La cuota de YouTube Data API v3 es POR PROYECTO GCP. Varios canales pueden
# compartir proyecto (canal2+canal3 → youtube-uploads-automation,
# canal4+canal5 → autotube-expediciones) y por tanto comparten presupuesto.
# YT_PROJECT_BUDGET_UNITS es un JSON {project_id: units_dia} con la cuota
# REAL de cada proyecto en GCP Console (default 100000 — ambos proyectos
# tienen ampliación 10x sobre el free tier; ver yt_quota_log del 2026-08-12
# con 82k ud consumidas en un día sin bloqueo).
# YT_PROJECT_RESERVED_UNITS reserva unidades para operaciones esenciales
# (stats, verificación de publicación, reconciliación) fuera del presupuesto
# automático de subidas.
try:
    _raw_budgets = os.getenv("YT_PROJECT_BUDGET_UNITS", "")
    YT_PROJECT_BUDGET_UNITS = json.loads(_raw_budgets) if _raw_budgets.strip() else {}
except Exception:
    YT_PROJECT_BUDGET_UNITS = {}
YT_PROJECT_DEFAULT_BUDGET = int(os.getenv("YT_PROJECT_DEFAULT_BUDGET", "100000"))
YT_PROJECT_RESERVED_UNITS = int(os.getenv("YT_PROJECT_RESERVED_UNITS", "100"))
# Presupuesto automático (subidas) de un proyecto = cuota real - reservados.
# Por proyecto, NO un valor global hardcodeado.
YT_AUTOMATIC_BUDGET_UNITS = int(os.getenv("YT_AUTOMATIC_BUDGET_UNITS", "0"))  # 0 = derivar por proyecto

# Completion chaining caused burst uploads that bypassed the normal scheduler.
SHORTS_CHAIN_DISPATCH_ENABLED = os.getenv("SHORTS_CHAIN_DISPATCH_ENABLED", "false").lower() == "true"

# ── Collaboration engine (comentarios en canales nicho) ───────────
# v2 (ago 2026): descubrimiento de canales vía navegador (0 cuota API) —
# los searches de Data API (100 ud/call) estaban agotando el presupuesto.
# Los comentarios se publican vía Data API (50 ud) solo si el proyecto
# tiene >COLLAB_MIN_FREE_PCT% de cuota libre.
COLLAB_ENABLED = os.getenv("COLLAB_ENABLED", "false").lower() == "true"
COLLAB_MIN_FREE_PCT = float(os.getenv("COLLAB_MIN_FREE_PCT", "15"))  # % de cuota libre mínima


def get_project_budget_units(project_id: str) -> int:
    """Cuota real diaria (ud) de un proyecto GCP.

    Orden: YT_PROJECT_BUDGET_UNITS[project_id] → YT_PROJECT_DEFAULT_BUDGET.
    """
    if not project_id or project_id == "unknown":
        return YT_PROJECT_DEFAULT_BUDGET
    budget = YT_PROJECT_BUDGET_UNITS.get(project_id)
    if isinstance(budget, (int, float)) and budget > 0:
        return int(budget)
    return YT_PROJECT_DEFAULT_BUDGET


def get_project_automatic_budget_units(project_id: str) -> int:
    """Presupuesto automático (subidas) de un proyecto = cuota - reservados.

    Si YT_AUTOMATIC_BUDGET_UNITS > 0, se usa ese valor global (legacy).
    """
    if YT_AUTOMATIC_BUDGET_UNITS > 0:
        return YT_AUTOMATIC_BUDGET_UNITS
    return max(get_project_budget_units(project_id) - YT_PROJECT_RESERVED_UNITS, 1600)


# ── Upload batching por cuenta (planificación quota-aware, ago 2026) ──
# Los videos long-form (fase F2) de los canales que comparten cuenta Google se
# suben agrupados en 2 momentos al día (mañana y tarde) para dar sensación de
# subida manual. Se respeta siempre upload + warmup <= publicación; si un video
# no cabe en ningún batch del día, se sube en el batch de tarde del día anterior.
# Formato: "HH:MM,HH:MM" (hora local Europe/Madrid). Override por canal en
# config_json["UPLOAD_BATCH_TIMES"].
def _parse_batch_times(raw: str) -> list[tuple[int, int]]:
    times = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h, m = (int(x) for x in part.split(":"))
            if 0 <= h <= 23 and 0 <= m <= 59:
                times.append((h, m))
        except (ValueError, TypeError):
            continue
    return times or [(9, 30), (16, 0)]


UPLOAD_BATCH_TIMES = _parse_batch_times(os.getenv("UPLOAD_BATCH_TIMES", "09:30,16:00"))

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

# Dual-cookie rotation: shared state file between Autotube and CRM.
# The worker writes exhausted-account flags here; AIImageGenerator reads them
# and creates pipeline_alerts when an account runs out of credits.
POLLO_STATUS_FILE = os.getenv(
    "POLLO_STATUS_FILE",
    "/root/lamamionline-control/data/pollo_accounts_status.json",
)
POLLO_ACCOUNTS_SETTINGS_PATH = os.getenv(
    "POLLO_ACCOUNTS_SETTINGS_PATH",
    "/root/lamamionline-control/data/settings.json",
)

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
ENABLE_DAILY_VIEW_GAP_CHECK = os.getenv("ENABLE_DAILY_VIEW_GAP_CHECK", "false").lower() == "true"
VIEW_GAP_THRESHOLD = int(os.getenv("VIEW_GAP_THRESHOLD", "500"))
VIEW_GAP_SCAN_UNREGISTERED = os.getenv("VIEW_GAP_SCAN_UNREGISTERED", "true").lower() == "true"
VIEW_GAP_INTERVAL_HOURS = int(os.getenv("VIEW_GAP_INTERVAL_HOURS", "24"))

# ── Video Lifecycle (post-upload promotion) ────────────────────
LIFECYCLE_ENABLED = os.getenv("LIFECYCLE_ENABLED", "false").lower() == "true"
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
COMMENT_REPLY_ENABLED = os.getenv("COMMENT_REPLY_ENABLED", "false").lower() == "true"

# ── Metadata reoptimization ────────────────────────────────────
METADATA_OPTIMIZE_ENABLED = os.getenv("METADATA_OPTIMIZE_ENABLED", "false").lower() == "true"
METADATA_OPTIMIZE_CTR_THRESHOLD = float(os.getenv("METADATA_OPTIMIZE_CTR_THRESHOLD", "3.0"))

# ── A/B Testing (sequential title/thumbnail optimization) ───────
# Global flag — enables the ABTestWorker in the scheduler loop.
# When enabled, videos are tracked post-upload and titles/thumbnails
# are optimized sequentially (never both in the same 48h window).
# Quota-aware: prioritizes local DB reads, falls back to YouTube
# Analytics API only when data is stale (>24h).
ENABLE_AB_TESTING = os.getenv("ENABLE_AB_TESTING", "false").lower() == "true"
AB_TEST_CTR_THRESHOLD = float(os.getenv("AB_TEST_CTR_THRESHOLD", "3.0"))
AB_TEST_FIRST_CHECK_HOURS = int(os.getenv("AB_TEST_FIRST_CHECK_HOURS", "48"))
AB_TEST_SECOND_CHECK_HOURS = int(os.getenv("AB_TEST_SECOND_CHECK_HOURS", "48"))
AB_TEST_MIN_IMPRESSIONS = int(os.getenv("AB_TEST_MIN_IMPRESSIONS", "100"))
AB_TEST_MAX_STALE_DAYS = int(os.getenv("AB_TEST_MAX_STALE_DAYS", "7"))
AB_TEST_MIN_IMPRESSIONS_POST_CHANGE = int(os.getenv("AB_TEST_MIN_IMPRESSIONS_POST_CHANGE", "50"))

# ── Quota-consuming background services (ago 2026) ─────────────
# Desactivados por defecto: solo subidas + playlist + stats manual
# consumen quota de YouTube Data API. Reversibles desde .env.
THUMBNAIL_VERIFY_ENABLED = os.getenv("THUMBNAIL_VERIFY_ENABLED", "false").lower() == "true"
UPLOAD_HEALTH_CHECKER_ENABLED = os.getenv("UPLOAD_HEALTH_CHECKER_ENABLED", "false").lower() == "true"

# ── Logging ────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE = LOGS_DIR / "autotube.log"
