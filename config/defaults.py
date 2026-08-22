"""Default configuration values shared by all channels.

Every channel inherits these defaults automatically via ``config_bridge.py``.
Per-channel config modules (``config/{slug}_config.py``) only need to declare
the parameters that DIFFER from these defaults.

Merge order (highest wins):
  1. DB ``channels.config_json``  (UI edits, immediate effect)
  2. ``config/{slug}_config.py``  (per-channel overrides)
  3. ``config/defaults.py``       (this file — universal fallback)

To add a new parameter for ALL channels: add it here.
To override for a specific channel: add it to that channel's config module.
"""

from config.settings import DEFAULT_VIDEO_PROVIDERS, DEFAULT_VIDEO_FALLBACK_QUERIES

# ═══════════════════════════════════════════════════════════════════
# IDENTITY PLACEHOLDERS (override per channel)
# ═══════════════════════════════════════════════════════════════════

CANAL_NAME = "new_channel"
CANAL_DISPLAY_NAME = "New Channel"
CANAL_TAGLINE = ""
CANAL_OUTRO_TAGLINE = ""
CANAL_NARRATIVE_STYLE = "documental"
CANAL_STYLE_DESCRIPTION = ""
YOUTUBE_HANDLE = ""
YOUTUBE_CHANNEL_URL = ""
CHANNEL_ABOUT_SECTION = ""
CHANNEL_KEYWORDS = []
CANAL_INITIALS = "NC"
LANGUAGE = "es"                # narración: "es", "en", etc.
LOGO_SIZE = 140
LOGO_PATH = ""

# ═══════════════════════════════════════════════════════════════════
# TEST MODE
# ═══════════════════════════════════════════════════════════════════

TEST_MODE = False

TEST_SCRIPT_WORDS_MIN = 450
TEST_SCRIPT_WORDS_MAX = 700
TEST_SCRIPT_SCENES_MIN = 10
TEST_SCRIPT_SCENES_MAX = 12
TEST_SCRIPT_BLOCKS_MIN = 3
TEST_SCRIPT_BLOCKS_MAX = 5
TEST_VIDEO_DURATION_TARGET = 3.5

QUICK_TEST_SCRIPT_WORDS_MIN = 80
QUICK_TEST_SCRIPT_WORDS_MAX = 120
QUICK_TEST_SCRIPT_SCENES_MIN = 3
QUICK_TEST_SCRIPT_SCENES_MAX = 4
QUICK_TEST_SCRIPT_BLOCKS_MIN = 2
QUICK_TEST_SCRIPT_BLOCKS_MAX = 3
QUICK_TEST_VIDEO_DURATION_TARGET = 0.5
QUICK_TEST_IMAGES_PER_SCENE = 3

# ═══════════════════════════════════════════════════════════════════
# PRODUCTION DEFAULTS (override per channel)
# ═══════════════════════════════════════════════════════════════════

PROD_SCRIPT_WORDS_MIN = 2000
PROD_SCRIPT_WORDS_MAX = 3500
PROD_SCRIPT_SCENES_MIN = 10
PROD_SCRIPT_SCENES_MAX = 18
PROD_SCRIPT_BLOCKS_MIN = 10
PROD_SCRIPT_BLOCKS_MAX = 18
PROD_VIDEO_DURATION_MIN = 10
PROD_VIDEO_DURATION_MAX = 14

# Scene duration enforcement
# Media-specific limits are used by the production timeline.  The legacy
# SCENE_DURATION_* values remain supported for older direct VideoEditor callers.
IMAGE_SCENE_DURATION_MIN = 4.0
IMAGE_SCENE_DURATION_MAX = 6.0
VIDEO_SCENE_DURATION_MIN = 4.0
VIDEO_SCENE_DURATION_MAX = 7.0
SCENE_SYNC_TOLERANCE_SEC = 0.15
SCENE_DURATION_MIN = 8.0
SCENE_DURATION_MAX = 16.0

# Average video duration target
VIDEO_AVERAGE_DURATION_MIN = 12
VIDEO_DURATION_DISCREPANCY_MIN = 2

# ═══════════════════════════════════════════════════════════════════
# NARRATIVE TONE PLACEHOLDERS (override per channel)
# ═══════════════════════════════════════════════════════════════════

CANAL_TONE = ""
TARGET_AUDIENCE = ""
TARGET_AUDIENCE_PSYCHOGRAPHIC = {}

# ═══════════════════════════════════════════════════════════════════
# REDDIT DEFAULTS
# ═══════════════════════════════════════════════════════════════════

REDDIT_SORT = "top"
REDDIT_TIME = "month"
REDDIT_LIMIT = 25

# ═══════════════════════════════════════════════════════════════════
# GOOGLE NEWS DEFAULTS
# ═══════════════════════════════════════════════════════════════════

GOOGLE_NEWS_LANGUAGE = "es"
GOOGLE_NEWS_COUNTRY = "ES"

# ═══════════════════════════════════════════════════════════════════
# CONTENT SOURCE DEFAULTS (override per channel)
# ═══════════════════════════════════════════════════════════════════

REDDIT_SUBREDDITS = []
WIKIPEDIA_CATEGORIES = []
SCRAPE_SOURCES = [
    {"plugin": "reddit", "priority": 1},
    {"plugin": "wikipedia", "priority": 2},
    {"plugin": "atlas_obscura", "priority": 3},
    {"plugin": "rss", "priority": 4},
    {"plugin": "google_news", "priority": 5},
]
ATLAS_OBSCURA_CATEGORIES = ["wonders", "history", "unique"]
RSS_FEEDS = []
GOOGLE_NEWS_QUERIES = []

# ═══════════════════════════════════════════════════════════════════
# VISUAL STYLE DEFAULTS (override per channel)
# ═══════════════════════════════════════════════════════════════════

IMAGE_STYLE_MODIFIERS = "cinematic documentary photography, 16:9, atmospheric"
# Shared AI treatment for every channel. Individual channels may override only
# AI_VISUAL_COLOR_GRADING to preserve this hybrid documentary / YouTube-impact
# visual language while retaining their own palette.
AI_VISUAL_IMPACT_STYLE = (
    "hybrid cinematic documentary photography with high-impact YouTube visual "
    "storytelling, immediate readable focal subject, vivid natural colour, "
    "strong subject-background separation, premium editorial detail"
)
AI_VISUAL_COLOR_GRADING = (
    "vivid readable cinematic colour grade, natural skin tones, preserved "
    "highlight and shadow detail"
)
COLOR_PALETTE = {
    "primary": (40, 40, 60),
    "secondary": (15, 20, 35),
    "accent": (200, 150, 50),
    "text": (240, 240, 240),
    "text_shadow": (8, 8, 8),
    "tertiary": (35, 35, 45),
    "warning": (220, 180, 30),
}
FILM_GRAIN_OPACITY = 5
FILM_GRAIN_FRAMES = 8
KEN_BURNS_ZOOM_MIN = 4
KEN_BURNS_ZOOM_MAX = 12

# ═══════════════════════════════════════════════════════════════════
# AI IMAGE UPSCALING (shared across all channels)
# ═══════════════════════════════════════════════════════════════════

# Pollinations (flux) devuelve imágenes a su resolución nativa 1024×576
# aunque se pida 1920×1080; Local SD genera a 512×512. Para evitar el
# aspecto borroso al escalarlas en el render:
#   1. se upscalean localmente (ESPCN_x2 de OpenCV dnn_superres) hasta la
#      resolución mínima objetivo;
#   2. se aplica unsharp mask para recuperar nitidez percibida (el paso que
#      de verdad elimina la borrosidad — benchmark: +56% de nitidez).
AI_UPSCALE_ENABLED = True
AI_UPSCALE_MIN_WIDTH = 1920
AI_UPSCALE_MIN_HEIGHT = 1080
# Modelo de super-resolución: "espcn" (rápido, ~1s), "edsr" (lento ~300s),
# "lapsrn", "fsrcnn". Descargado una sola vez a output/models/.
AI_UPSCALE_MODEL = "espcn"
# Factor de escala del modelo (debe coincidir con el modelo elegido:
# espcn → 2, edsr/fsrcnn → 4, lapsrn → 8).
AI_UPSCALE_SCALE = 2
# Fuente del modelo (ESPCN_x2 ~86 KB, descargado una sola vez a output/models/)
AI_UPSCALE_MODEL_URL = (
    "https://raw.githubusercontent.com/fannymonori/TF-ESPCN/master/export/ESPCN_x2.pb"
)
# Unsharp mask post-upscale (nitidez percibida).
AI_UPSCALE_SHARPEN_ENABLED = True
AI_UPSCALE_SHARPEN_AMOUNT = 0.4   # 0 = desactivado; ~0.4 equilibrado
AI_UPSCALE_SHARPEN_SIGMA = 2.0    # radio del desenfoque gaussiano

# ═══════════════════════════════════════════════════════════════════
# SUBTITLE STYLE (shared across all channels)
# ═══════════════════════════════════════════════════════════════════

SUBTITLE_FONT_SIZE = 52
SUBTITLE_SHADOW_WIDTH = 3
SUBTITLE_POSITION_X = 0.5
SUBTITLE_POSITION_Y = 0.88
SUBTITLE_POP_START = 0.95
SUBTITLE_POP_END = 1.05
SUBTITLE_MAX_CHARS = 50
SUBTITLE_PHRASE_GAP = 0.4

SUBTITLES_ENABLED = False

# ═══════════════════════════════════════════════════════════════════
# MEDIA PROVIDER TIMEOUTS
# ═══════════════════════════════════════════════════════════════════

# HTTP timeout (seconds) for Pixabay API search requests.
# Increased from 15s to 30s to reduce timeouts under load.
PIXABAY_API_TIMEOUT = 30

# ═══════════════════════════════════════════════════════════════════
# MEDIA STRATEGY DEFAULTS
# ═══════════════════════════════════════════════════════════════════

MEDIA_STRATEGY = {
    # ── Existing ────────────────────────────────────────────────
    "media_per_block": 1,
    "prefer_video": True,
    "max_video_blocks_pct": 80,
    "target_video_pct": 80,
    "max_placeholder_pct": 0,
    "video_fallback_to_image": True,
    "video_min_duration": 4,
    "video_max_duration": 20,
    "video_sources": ["pexels"],
    "video_providers": DEFAULT_VIDEO_PROVIDERS,
    "video_fallback_queries": DEFAULT_VIDEO_FALLBACK_QUERIES,
    "crossfade_min": 0.3,
    "crossfade_max": 0.7,
    "xfade_batch_size": 25,           # max segments per ffmpeg xfade invocation (RAM safety; 25 ≈ 1.5 GB peak)
    "ai_image_fallback": True,
    "ai_max_per_video": 5,

    # ── AI Image Primary (Phase 1) ─────────────────────────────
    # When True, scenes classified as 'ai_image' use AI generation
    # as their primary tier (before falling back to stock images).
    "ai_image_primary": True,
    # Ordered list of free AI providers to try: pollinations first
    # (fast, ~8s), then local_sd (slow, ~180s) as fallback.
    "ai_image_providers": ["pollinations", "local_sd"],
    # Optional global style prefix injected into every AI prompt.
    # None → auto-derived from channel COLOR_PALETTE + IMAGE_STYLE_MODIFIERS
    #   by the VisualCoherenceEngine.
    "ai_style_prefix": None,
    # Cache directory for AI-generated images (per-channel via output dir).
    "ai_cache_enabled": True,
    # Model override for Pollinations (None = use Pollinations default: flux).
    "ai_pollinations_model": None,
    # Local SD generation steps (fewer = faster, lower quality).
    "ai_local_sd_steps": 20,

    # ── Video Scene Control (Phase 2) ──────────────────────────
    # Minimum % of scenes that should try stock video.
    "video_scene_pct_min": 20,
    # Maximum % of scenes that may try stock video (if quality is high).
    "video_scene_pct_max": 30,
    # Absolute hard cap on number of video assets regardless of scene count.
    "video_scene_hard_cap": 12,
    # Only assign video to scenes within the first X% of total runtime.
    "video_first_half_pct": 40,
    # Minimum scene duration (seconds) to be eligible for video.
    # Matches the new VIDEO_SCENE_DURATION_MAX=7s pacing: scenes ≥6s in the
    # first half still qualify for stock video.
    "video_min_scene_duration": 6,
    # Avg quality threshold (0-1). Above this → keep searching up to 30%.
    "video_quality_threshold": 0.5,

    # ── Pollo AI (credits, kept as absolute last resort) ───────
    "pollo_ai_enabled": True,

    # ── Visual Bible (Phase 3, NUEVO) ──────────────────────────
    # When True, a second LLM call generates a visual bible JSON
    # (protagonist, recurring elements, scene visual concepts).
    "visual_bible_enabled": True,      # Phase 3; enabled in production
    # LLM model for visual bible. None = same model as script LLM.
    "visual_bible_model": None,
    # If visual bible LLM fails, fallback to the script's LLM.
    "visual_bible_fallback": True,

    # ── Shot Type Rotation (Phase 3) ───────────────────────────
    "shot_type_distribution": {
        "establishing": 0.15,
        "detail": 0.25,
        "mood": 0.30,
        "action": 0.20,
        "symbolic": 0.10,
    },
}

# ═══════════════════════════════════════════════════════════════════
# TRANSITIONS
# ═══════════════════════════════════════════════════════════════════

TRANSITION_ENABLED = True
TRANSITION_DURATION_MIN = 1.0
TRANSITION_DURATION_MAX = 5.0

# ═══════════════════════════════════════════════════════════════════
# BACKGROUND MUSIC
# ═══════════════════════════════════════════════════════════════════

BACKGROUND_MUSIC_ENABLED = True
BACKGROUND_MUSIC_VOLUME = -18.0
BACKGROUND_MUSIC_DUCK_VOLUME = -28.0

# ═══════════════════════════════════════════════════════════════════
# INTRO / OUTRO DEFAULTS
# ═══════════════════════════════════════════════════════════════════

INTRO_DURATION_SEC = 3.0
OUTRO_DURATION_SEC = 5.0

OUTRO_CTA_LIKE = "👍 Like"
OUTRO_CTA_SUBSCRIBE = "❤️ Suscríbete"
OUTRO_CTA_BELL = "📢 Comparte"

# ═══════════════════════════════════════════════════════════════════
# YOUTUBE METADATA DEFAULTS
# ═══════════════════════════════════════════════════════════════════

YT_CATEGORY_ID = "27"  # Education
YT_PRIVACY_STATUS = "public"

AUTO_MARK_ALTERED_CONTENT = True
AUTO_END_SCREENS = True

# Scheduled Publishing
PUBLISH_MODE = "immediate"
GENERATION_LEAD_HOURS = 36
PUBLISH_TIMEZONE = "Europe/Madrid"
PUBLISH_JITTER_MIN = 15
PUBLISH_WARMUP_MIN = 5
PUBLISH_WINDOW_SPREAD_MIN = 90

# ── Upload spacing (backlog drain) ─────────────────────────────
# Mínimo de horas entre subidas long-form del MISMO canal. Evita que
# un backlog de vídeos pasados se despache en ráfagas ("en fila") cuando
# vuelve la cuota. Se lee vía config_json en upload_scheduler.
# ago 2026 (antiban): 3h → 6h — el patrón de ráfagas alimentó el flag
# de spam de YouTube (strikes de ago-2026 en los 4 canales).
MIN_SAME_CHANNEL_UPLOAD_GAP_HOURS = 6

# ── Cap de subidas por cuenta Google (antiban, ago 2026) ─────────
# Los strikes de YouTube se registran POR CUENTA/PROYECTO GCP, no por canal.
# Dos canales hermanos que comparten cuenta pueden saturarla aunque cada uno
# cumpla su cap individual. Límite de SUBIDAS TOTALES (long-form + shorts)
# por cuenta Google y día. 0 = desactivado.
ACCOUNT_DAILY_UPLOAD_CAP = 4

# ═══════════════════════════════════════════════════════════════════
# SHORTS DEFAULTS
# ═══════════════════════════════════════════════════════════════════

SHORTS_ENABLED = True
SHORTS_MAX_CLIPS_PER_VIDEO = 5
SHORTS_CLIP_SCHEDULE = [
    {"offset_days": 1, "count": 1},
    {"offset_days": 3, "count": 1},
    {"offset_days": 5, "count": 1},
]

# ── Quota pruning (ago 2026): shorts se suben SIN playlist ni cross-promote ──
# Solo los videos long-form se añaden a playlist tras la subida (orchestrator).
# SHORTS_LONGFORM_LINK_ENABLED es el master switch: False apaga todo el
# cross-promote de shorts (run_post_publish_promotion) en las 5 rutas de subida.
SHORTS_LONGFORM_LINK_ENABLED = False
SHORTS_PLAYLIST_AUTO = False
SHORTS_FIRST_COMMENT_LINK = False
SHORTS_PER_VIDEO_PLAYLIST = False
SHORTS_PLAYLIST_NAME = "Shorts"

# ── v2: New distribution system (Aug 2026) ──────────────────────
SHORTS_NATIVE_RATIO = 0.50            # 50% native, 50% clip (equilibrado — Fase 0)
SHORTS_ADAPTIVE_DISTRIBUTION = True   # Auto-adjust based on 14-day performance
SHORTS_ADAPTIVE_CHECK_DAYS = 14       # Re-evaluation window
SHORTS_ADAPTIVE_RATIO_MIN = 0.20      # Floor (never below 20% native)
SHORTS_ADAPTIVE_RATIO_MAX = 0.60      # Ceiling (never above 60% native)
SHORTS_ADAPTIVE_STEP = 0.10           # ±10% adjustment per evaluation
SHORTS_MIN_NATIVE_PER_DAY = 1         # Minimum native shorts per day
# ago 2026 (antiban): clips desactivados por defecto (2 → 0). Los clips
# son contenido reciclado del long-form y el mayor imán de flag de spam
# de YouTube. Per-channel overridable vía shorts_planning_config.
SHORTS_CLIPS_PER_LONG = 0             # Fixed: clip shorts per long video (antiban: 0)
SHORTS_HOOK_OVERLAY_FONT_SIZE = 48    # Hook text overlay (3s)
SHORTS_HOOK_OVERLAY_DURATION_SEC = 3.0
SHORTS_HOOK_OVERLAY_FADE_SEC = 0.3

SHORTS_MAX_DURATION_SEC = 58.0          # YouTube Shorts max = 60 s; leave 2 s buffer. Per-channel overridable.

# ── Shorts en cola durante bloqueo por spam (ago 2026) ──────────
# Durante el ban, los shorts nativos se GENERAN y quedan con status 'generated'
# (sin subir) para despachar la cola al desbloquearse. Tope por canal para no
# llenar disco/LLM.
MAX_QUEUED_NATIVE_SHORTS_PER_CHANNEL = 30

# ═══════════════════════════════════════════════════════════════════
# THUMBNAIL DEFAULTS
# ═══════════════════════════════════════════════════════════════════

THUMBNAIL_WIDTH = 1280
THUMBNAIL_HEIGHT = 720
THUMBNAIL_FONT_SIZE = 56
THUMBNAIL_BORDER_WIDTH = 5
THUMBNAIL_FONT_FAMILY = "DejaVuSans-Bold"
THUMBNAIL_SHOW_4K_BADGE = True
THUMBNAIL_TEXT_STROKE_WIDTH = 3
THUMBNAIL_TEXT_STROKE_COLOR = "#000000"
THUMBNAIL_STYLE_OVERRIDE = True

# ═══════════════════════════════════════════════════════════════════
# VOICE / TTS DEFAULTS
# ═══════════════════════════════════════════════════════════════════

VOICE_ID = "es-ES-AlvaroNeural"
VOICE_SECONDARY = "es-MX-DaliaNeural"
VOICE_VOLUME = "+0%"

VOICE_SSML = {
    "break_after_hook": '<break time="800ms"/>',
    "break_before_climax": '<break time="1200ms"/>',
    "emphasis_numbers": '<emphasis level="strong">',
    "emphasis_end": '</emphasis>',
    "prosody_rate_slow": '<prosody rate="slow" pitch="-2st">',
    "prosody_end": '</prosody>',
}

TTS_ENGINE = "kokoro"
KOKORO_VOICE = "em_santa"
KOKORO_PAUSE_BETWEEN_BLOCKS = 0.8

# ═══════════════════════════════════════════════════════════════════
# DYNAMIC VIDEOS PER DAY (auto-adjust by recovery planner)
# ═══════════════════════════════════════════════════════════════════
# Master switch — set False to disable auto-adjust entirely
# Fase 0 estabilización (ago 2026): desactivado para evitar el feedback
# negativo (bugs → baja success_rate → Dynamic VPD baja videos_per_day).
DYNAMIC_VPD_ENABLED = False
# Starting point for all channels (overridable per-channel)
VIDEOS_PER_DAY_BASE = 2
# Floor and ceiling — dynamic algorithm will never go outside [MIN, MAX]
VIDEOS_PER_DAY_MIN = 1
VIDEOS_PER_DAY_MAX = 4
# Re-evaluate every N hours
DYNAMIC_VPD_CHECK_INTERVAL_H = 6
# Last N jobs to compute success rate
DYNAMIC_VPD_SUCCESS_WINDOW = 10
# Success rate threshold: >= this value → eligible for +1 boost
DYNAMIC_VPD_SUCCESS_THRESHOLD = 0.7
# If awaiting_upload >= this, may reduce slots (pipeline pressure)
DYNAMIC_VPD_BACKLOG_BOOST = 4
# Extra slots when recovering from scheduler pause (quota exhaustion)
DYNAMIC_VPD_CATCHUP_BOOST = 2
# Catch-up boost duration in hours after pause recovery
DYNAMIC_VPD_CATCHUP_DURATION_H = 48
# New channels (< this many published) get +1 slot boost
DYNAMIC_VPD_NEW_CHANNEL_THRESHOLD = 30
# Maximum total vpd for channels sharing a Google account
DYNAMIC_VPD_SHARED_ACCOUNT_MAX = 6

# ═══════════════════════════════════════════════════════════════════
# MARATHON DEFAULTS
# ═══════════════════════════════════════════════════════════════════

MARATHON_ENABLED = False
MARATHON_VIDEO_DURATION_TARGET = 60
MARATHON_NUM_SECTIONS = 12
MARATHON_SCRIPT_WORDS_MIN = 8000
MARATHON_SCRIPT_WORDS_MAX = 12000
MARATHON_SCRIPT_BLOCKS_MIN = 50
MARATHON_SCRIPT_BLOCKS_MAX = 90
MARATHON_OUTLINE_CHAPTERS = 15
# DEPRECATED: no se lee en runtime (el % de vídeo del marathon se define en
# full_pipeline_worker). Se conserva para no romper configs serializados.
MARATHON_MEDIA_VIDEO_PCT = 20
MARATHON_LLM_MAX_BATCHES = 150
MARATHON_LLM_MAX_EMPTY_STRIKES = 20
MARATHON_PUBLISH_MODE = "scheduled"

# Mín backlog por canal (awaiting_upload + uploaded_private) para disparar marathon.
# Umbral total = MARATHON_BACKLOG_PER_CHANNEL × canales_activos.
MARATHON_BACKLOG_PER_CHANNEL = 4

# Cooldown entre marathons del MISMO canal (horas). Un canal recién maratoneado
# no vuelve a ser elegible hasta que pasen estas horas. Se lee del config_json
# del canal (MARATHON_COOLDOWN_HOURS) con fallback a este default.
MARATHON_COOLDOWN_HOURS = 24

# ── MARATHON TITLE STRATEGY ──

# Fórmulas de título para maratones. {topic} se reemplaza con el tema.
# Se elige una aleatoriamente para cada maratón.
MARATHON_TITLE_FORMULAS = [
    "{topic}: El Documental Definitivo",
    "La Historia COMPLETA de {topic} | Documental",
    "{topic} — Lo Que NADIE Te Ha Contado",
    "De Principio a Fin: {topic}",
    "El Misterio de {topic} | Documental Completo HD",
    "{topic}: La Verdad Que Cambió Todo",
    "ATENCIÓN: {topic} — Documental Impactante",
    "Lo Que Descubrieron Sobre {topic} Es Increíble",
]

# Tipos de hook para el título del maratón. Define la emoción que debe evocar.
MARATHON_HOOK_TYPES = [
    "revelacion_impactante",    # "Descubrieron algo que cambió la historia"
    "misterio_sin_resolver",    # "Nadie ha podido explicar esto"
    "conocimiento_exclusivo",   # "Información que pocos conocen"
    "prohibido_u_oculto",       # "Lo que no quieren que sepas"
    "asombro_cientifico",       # "La ciencia no puede explicarlo"
    "amenaza_inminente",        # "Esto podría cambiar tu vida"
    "secreto_ancestral",        # "Secretos que estuvieron enterrados siglos"
]

# Validación de título pre-publicación (usar LLM)
MARATHON_VALIDATE_TITLE = True

# Score mínimo de "viralidad" para aprobar el título (1-10)
MARATHON_MIN_VIRALITY_SCORE = 7

# Requisitos que debe cumplir un título de maratón
MARATHON_TITLE_REQUIREMENTS = [
    "Debe generar curiosidad inmediata en las primeras 5 palabras",
    "Debe incluir al menos 1 power word del nicho",
    "NO debe ser clickbait engañoso (debe cumplir lo que promete)",
    "Debe tener entre 40 y 80 caracteres",
    "Debe sonar a documental/película, no a video amateur de YouTube",
    "NO debe usar mayúsculas en toda la frase (solo palabras clave)",
]

# ═══════════════════════════════════════════════════════════════════
# VIRAL MIRROR DEFAULTS
# ═══════════════════════════════════════════════════════════════════

VIRAL_ENABLED = True
VIRAL_CONTENT_MODE = "rewrite"
VIRAL_MAX_AGE_DAYS = 29

# ═══════════════════════════════════════════════════════════════════
# CROSS-PLATFORM DEFAULTS
# ═══════════════════════════════════════════════════════════════════

# Text-based cross-promotion flags (used by social caption/publisher system).
CROSS_PLATFORM = {
    "facebook": False,
    "tiktok": False,
    "twitter": False,
}

# Auto-upload full videos to monetizable platforms after YouTube publishing.
# True = upload the same video file to this platform via API.
# Requires: valid credentials in channel_social_accounts for each platform.
CROSS_PLATFORM_UPLOAD = {
    "facebook": False,   # Graph API v18+ video upload (In-Stream Ads)
    "rumble": False,     # Upload API (Rumble Player + licensing)
    "tiktok": False,     # Content Posting API (clip upload)
}

# Per-platform settings for video uploads.
# ═══════════════════════════════════════════════════════════════════
# A/B TESTING DEFAULTS (sequential title/thumbnail optimization)
# ═══════════════════════════════════════════════════════════════════

ENABLE_AB_TESTING = False
AB_TEST_CTR_THRESHOLD = 3.0
AB_TEST_FIRST_CHECK_HOURS = 48
AB_TEST_SECOND_CHECK_HOURS = 48
AB_TEST_MIN_IMPRESSIONS = 100
AB_TEST_THUMBNAIL_VARIANTS = 3

CROSS_PLATFORM_SETTINGS = {
    "facebook": {
        "privacy": "public",
        "cross_reference_yt": True,         # append YT link in description
        "sync_metadata": True,              # use same title/desc/tags as YT
        "upload_delay_min": 0,              # delay after YT upload
    },
    "rumble": {
        "privacy": "public",
        "cross_reference_yt": True,
        "sync_metadata": True,
        "upload_delay_min": 5,
    },
    "tiktok": {
        "privacy": "public",
        "clip_duration_sec": 60,
        "cross_reference_yt": False,        # TikTok penalizes external links
        "upload_delay_min": 30,
    },
}

# ═══════════════════════════════════════════════════════════════════
# SEO OPTIMIZATION DEFAULTS
# ═══════════════════════════════════════════════════════════════════

# Enable real-time keyword research via Google Trends / YouTube autocomplete.
# When True, the MetadataGenerator queries trending keywords for each video topic
# and injects them into the LLM prompt for higher search relevance.
SEO_ENABLE_REALTIME_RESEARCH = True

# Number of trending keywords to fetch per topic.
SEO_TRENDING_KEYWORDS_COUNT = 10

# Optimized description template (timestamp-first format).
# The LLM will produce descriptions following this structure:
#   1. Chapter timestamps (YouTube indexes these as Key Moments)
#   2. Hook paragraph (~150 chars) with primary keyword
#   3. 2-3 paragraphs with secondary keywords
#   4. CTA (subscribe + related video link)
#   5. 3-5 hashtags
# Channels can override with their own DESCRIPTION_TEMPLATE.
OPTIMIZED_DESCRIPTION_STRUCTURE = {
    "timestamps_first": True,
    "hook_max_chars": 150,
    "body_paragraphs": 3,
    "max_hashtags": 5,
    "include_related_video_link": True,
    "include_sources_section": True,
}

# SEO scoring weights (used by /api/channels/{id}/seo-score).
# Score 0-10 based on:
#   title_length     (3 pts): avg title 40-65 chars = perfect
#   power_words      (2 pts): % of titles with at least 1 power word
#   description_len  (2 pts): avg description > 1500 chars
#   timestamps       (2 pts): % of videos with chapter timestamps
#   tag_count        (1 pts): avg tags per video (ideal: 7-10)
SEO_SCORE_WEIGHTS = {
    "title_length": 3,
    "power_words": 2,
    "description_length": 2,
    "timestamps": 2,
    "tag_count": 1,
}

# ── Stats collection: public-scraping fallback (zero Data API quota) ──
# When the YouTube Data API v3 quota is exhausted, the "Recolectar stats"
# button falls back to scraping public metrics (views/likes/comments/subs)
# via yt-dlp instead of failing or zeroing likes/comments. Also works for
# channels without an OAuth token (public data needs no auth).
STATS_SCRAPE_FALLBACK_ENABLED = True
# Parallel yt-dlp requests when scraping (1 request per video/short).
STATS_SCRAPE_MAX_CONCURRENCY = 6
