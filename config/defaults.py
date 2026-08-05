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
# MEDIA STRATEGY DEFAULTS
# ═══════════════════════════════════════════════════════════════════

MEDIA_STRATEGY = {
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
    "ai_image_fallback": True,
    "ai_max_per_video": 5,
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

# ═══════════════════════════════════════════════════════════════════
# SHORTS DEFAULTS
# ═══════════════════════════════════════════════════════════════════

SHORTS_ENABLED = True
SHORTS_PER_DAY = 3
SHORTS_MAX_CLIPS_PER_VIDEO = 5
SHORTS_CLIP_SCHEDULE = [
    {"offset_days": 1, "count": 1},
    {"offset_days": 3, "count": 1},
    {"offset_days": 5, "count": 1},
]

SHORTS_LONGFORM_LINK_ENABLED = True
SHORTS_PLAYLIST_AUTO = True
SHORTS_FIRST_COMMENT_LINK = True
SHORTS_PER_VIDEO_PLAYLIST = True
SHORTS_PLAYLIST_NAME = "Shorts"

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
# MARATHON DEFAULTS
# ═══════════════════════════════════════════════════════════════════

MARATHON_ENABLED = True
MARATHON_VIDEO_DURATION_TARGET = 60
MARATHON_NUM_SECTIONS = 12
MARATHON_SCRIPT_WORDS_MIN = 8000
MARATHON_SCRIPT_WORDS_MAX = 12000
MARATHON_SCRIPT_BLOCKS_MIN = 50
MARATHON_SCRIPT_BLOCKS_MAX = 90
MARATHON_OUTLINE_CHAPTERS = 15
MARATHON_MEDIA_VIDEO_PCT = 20
MARATHON_LLM_MAX_BATCHES = 150
MARATHON_LLM_MAX_EMPTY_STRIKES = 20
MARATHON_PUBLISH_MODE = "scheduled"

# ═══════════════════════════════════════════════════════════════════
# VIRAL MIRROR DEFAULTS
# ═══════════════════════════════════════════════════════════════════

VIRAL_ENABLED = True
VIRAL_CONTENT_MODE = "rewrite"
VIRAL_MAX_AGE_DAYS = 29

# ═══════════════════════════════════════════════════════════════════
# CROSS-PLATFORM DEFAULTS
# ═══════════════════════════════════════════════════════════════════

CROSS_PLATFORM = {
    "facebook": False,
    "tiktok": False,
    "twitter": False,
}
