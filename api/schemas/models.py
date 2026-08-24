"""Pydantic schemas for API request/response models."""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ── Channel ──────────────────────────────────────────────────

class ChannelConfig(BaseModel):
    """Full channel configuration — synced with config/canal2_config.py."""
    # Identity
    canal_display_name: str = ""
    canal_tagline: str = ""
    canal_outro_tagline: str = ""
    canal_narrative_style: str = "archivo oscuro"

    # Voice
    voice_id: str = "es-MX-JorgeNeural"
    voice_rate: str = "-3%"
    voice_pitch: str = "-12Hz"
    voice_volume: str = "+0%"
    voice_secondary: str = ""

    # Content Sources
    reddit_subreddits: list[str] = Field(default_factory=list)
    wikipedia_categories: list[str] = Field(default_factory=list)

    # Visual Style
    image_style_modifiers: str = ""
    color_palette: dict = Field(default_factory=dict)
    film_grain_opacity: int = 8
    ken_burns_zoom_min: int = 3
    ken_burns_zoom_max: int = 8
    thumbnail_width: int = 1280
    thumbnail_height: int = 720
    thumbnail_font_size: int = 56
    thumbnail_border_width: int = 5

    # YouTube Metadata
    yt_category_id: str = "27"
    yt_privacy_status: str = "public"
    yt_default_tags: list[str] = Field(default_factory=list)
    description_template: str = ""

    # SEO
    seo_primary_keyword: str = ""
    seo_secondary_keywords: list[str] = Field(default_factory=list)
    seo_hashtags: list[str] = Field(default_factory=list)

    # Script / Narrative
    canal_tone: str = ""
    script_hook_rule: str = ""
    script_end_hook: str = ""
    script_emotional_arc: dict = Field(default_factory=dict)

    # Titles
    title_formulas: list[str] = Field(default_factory=list)
    title_power_words: list[str] = Field(default_factory=list)

    # Content Pillars
    content_pillars: list[dict] = Field(default_factory=list)

    # ── Video Duration / Generation ─────────────────────────
    video_average_duration_min: int = 15  # target average duration (minutes)
    video_duration_discrepancy_min: float = 3  # ±discrepancy around mean (minutes)

    test_mode: bool = False
    test_script_words_min: int = 200
    test_script_words_max: int = 600
    test_script_scenes_min: int = 3
    test_script_scenes_max: int = 6
    test_script_blocks_min: int = 3
    test_script_blocks_max: int = 5
    test_video_duration_target: int = 2

    quick_test_script_words_min: int = 80
    quick_test_script_words_max: int = 120
    quick_test_script_scenes_min: int = 3
    quick_test_script_scenes_max: int = 4
    quick_test_script_blocks_min: int = 2
    quick_test_script_blocks_max: int = 3
    quick_test_video_duration_target: float = 0.5
    quick_test_images_per_scene: int = 3

    prod_script_words_min: int = 2000
    prod_script_words_max: int = 3500
    prod_script_scenes_min: int = 10
    prod_script_scenes_max: int = 18
    prod_script_blocks_min: int = 10
    prod_script_blocks_max: int = 18
    prod_video_duration_min: int = 8
    prod_video_duration_max: int = 14

    class Config:
        extra = "allow"  # Accept extra fields from config that aren't explicitly modeled


class ChannelCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    slug: str = Field(..., min_length=2, max_length=50, pattern=r"^[a-z0-9_-]+$")
    config: ChannelConfig = Field(default_factory=ChannelConfig)
    youtube_handle: Optional[str] = None
    google_account: Optional[str] = None


class ChannelUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    slug: Optional[str] = Field(default=None, min_length=2, max_length=50, pattern=r"^[a-z0-9_-]+$")
    config: Optional[ChannelConfig] = None
    active: Optional[bool] = None
    banner_url: Optional[str] = None
    avatar_url: Optional[str] = None
    description: Optional[str] = None
    yt_channel_id: Optional[str] = None
    yt_channel_url: Optional[str] = None
    google_account: Optional[str] = None
    yt_studio_url: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None


class ChannelResponse(BaseModel):
    id: int
    name: str
    slug: str
    config_json: str
    active: bool
    banner_url: Optional[str] = None
    avatar_url: Optional[str] = None
    description: Optional[str] = None
    yt_channel_id: Optional[str] = None
    yt_channel_url: Optional[str] = None
    google_account: Optional[str] = None
    yt_studio_url: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ChannelConfigUpdate(BaseModel):
    """Update channel configuration and profile fields."""
    name: Optional[str] = None
    description: Optional[str] = None
    banner_url: Optional[str] = None
    avatar_url: Optional[str] = None
    yt_channel_url: Optional[str] = None
    google_account: Optional[str] = None
    yt_studio_url: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None


# ── Video ────────────────────────────────────────────────────

class VideoGenerateRequest(BaseModel):
    channel_id: int
    action: str = "generate_and_upload"
    content_id: Optional[int] = None
    test_mode: bool = False  # fast-test: low res, no upload, no effects
    upload: bool = True  # si False, genera el video sin subir a YouTube
    source_mode: Optional[str] = "original"  # "original" | "viral"
    viral_candidate_id: Optional[int] = None  # raw_content.id for viral mode


class VideoUpdate(BaseModel):
    titulo_final: Optional[str] = None
    description: Optional[str] = None
    tags_json: Optional[str] = None
    title_options: Optional[str] = None
    privacy_status: Optional[str] = None
    target_public_at: Optional[str] = None  # ISO8601 UTC — manual schedule override


class VideoResponse(BaseModel):
    id: int
    channel_id: Optional[int] = None
    channel_name: Optional[str] = None
    script_id: Optional[int] = None
    canal: str = ""
    video_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    audio_path: Optional[str] = None
    yt_video_id: Optional[str] = None
    yt_url: Optional[str] = None
    titulo_final: Optional[str] = None
    description: Optional[str] = None
    tags_json: Optional[str] = None
    title_options: Optional[str] = None
    duracion_seg: Optional[int] = None
    privacy_status: str = "public"
    status: str = "draft"
    progress: int = 0
    progress_phase: Optional[str] = None
    timing_data: Optional[str] = None
    uploaded_at: Optional[str] = None
    created_at: Optional[str] = None


# ── Scene ────────────────────────────────────────────────────

class SceneUpdate(BaseModel):
    script_text: Optional[str] = None
    description: Optional[str] = None
    image_path: Optional[str] = None
    image_url: Optional[str] = None
    scene_order: Optional[int] = None


class SceneResponse(BaseModel):
    id: int
    video_id: int
    scene_order: int
    description: Optional[str] = None
    script_text: Optional[str] = None
    audio_path: Optional[str] = None
    image_path: Optional[str] = None
    image_url: Optional[str] = None
    subtitle_text: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ── Job ──────────────────────────────────────────────────────

class JobResponse(BaseModel):
    id: int
    channel_id: int
    video_id: Optional[int] = None
    action: str
    status: str
    progress: int
    phase: Optional[str] = None
    error_msg: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: Optional[str] = None


# ── Content ──────────────────────────────────────────────────

class ContentResponse(BaseModel):
    id: int
    source: str
    subreddit: Optional[str] = None
    url: str
    title: str
    text: str
    score: int
    scraped_at: Optional[str] = None
    used: bool
    canal: str
    status: Optional[str] = "pending"
    scheduled_at: Optional[str] = None


class ContentCreate(BaseModel):
    title: str = Field(..., min_length=1)
    text: str = Field(..., min_length=10)
    source: str = "manual"
    canal: Optional[str] = None
    subreddit: Optional[str] = None
    url: Optional[str] = None
    score: int = 0


class ContentUpdate(BaseModel):
    title: Optional[str] = None
    text: Optional[str] = None
    source: Optional[str] = None
    status: Optional[str] = None


class ContentSchedule(BaseModel):
    scheduled_at: str  # ISO datetime string


# ── Script ───────────────────────────────────────────────────

class ScriptGenerateRequest(BaseModel):
    channel_id: int
    content_id: int


class ScriptResponse(BaseModel):
    id: int
    raw_content_id: Optional[int] = None
    canal: str
    titulo_options: str
    titulo_selected: Optional[str] = None
    guion: str
    escenas_json: str
    emociones_json: Optional[str] = None
    keywords_json: Optional[str] = None
    duracion_estimada: Optional[int] = None
    token_count: Optional[int] = None
    cost_estimate: Optional[float] = None
    created_at: Optional[str] = None
    used: bool = False


# ── Marketing ────────────────────────────────────────────────

class MarketingGenerateRequest(BaseModel):
    channel_id: int
    video_id: int
    script_text: str
    keywords: list[str] = Field(default_factory=list)


class MarketingResponse(BaseModel):
    titles: list[str]
    description: str
    tags: list[str]
    thumbnail_text: str


# ── Dashboard Stats ──────────────────────────────────────────

class DashboardStats(BaseModel):
    channels: int
    total_videos: int
    uploaded_videos: int
    generating_videos: int
    ready_videos: int
    unused_content: int
    unused_scripts: int


# ── Social Media Accounts ───────────────────────────────────

class SocialAccountCreate(BaseModel):
    platform: Optional[str] = None  # redundante con el path; el router usa el path
    username: str
    password: str  # API token/key — plaintext, server encrypts before storing
    enabled: bool = True
    # Identity fields (v45): correo de registro, contraseña del correo,
    # contraseña de login de la plataforma y notas. Se cifran en servidor.
    account_email: Optional[str] = None
    account_email_password: Optional[str] = None
    account_password: Optional[str] = None
    notes: Optional[str] = None


class SocialAccountUpdate(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None  # API token/key (plaintext)
    enabled: Optional[bool] = None
    account_email: Optional[str] = None
    account_email_password: Optional[str] = None
    account_password: Optional[str] = None
    notes: Optional[str] = None


class SocialRevealRequest(BaseModel):
    """Petición de revelado de una credencial concreta."""
    field: str  # 'api_key' | 'email_password' | 'account_password'


class SocialAccountResponse(BaseModel):
    id: int
    channel_id: int
    platform: str
    username: str
    enabled: bool
    has_cookies: bool = False
    last_login_at: Optional[str] = None
    last_error: Optional[str] = None
    created_at: str
    updated_at: str
    # Identity fields (v45) — los secretos se devuelven SOLO como flags.
    account_email: Optional[str] = None
    notes: Optional[str] = None
    has_email_password: bool = False
    has_account_password: bool = False
    has_api_key: bool = False


class SocialTimingUpdate(BaseModel):
    """Per-platform delay in minutes after go_public."""
    tiktok: Optional[int] = None
    twitter: Optional[int] = None
    instagram: Optional[int] = None
    facebook: Optional[int] = None
    reddit: Optional[int] = None
    rumble: Optional[int] = None


# ── Cross-Platform Publishing ──────────────────────────────


class CrossPlatformConfigUpdate(BaseModel):
    """Enable/disable auto-upload to each platform."""
    facebook: Optional[bool] = None
    rumble: Optional[bool] = None
    tiktok: Optional[bool] = None


class CrossPlatformConfigResponse(BaseModel):
    facebook: bool = False
    rumble: bool = False
    tiktok: bool = False
    settings: dict = {}


class PlatformVideoResponse(BaseModel):
    id: int
    video_id: int
    channel_id: int
    platform: str
    platform_video_id: Optional[str] = None
    platform_video_url: Optional[str] = None
    status: str
    privacy: str
    error_message: Optional[str] = None
    attempts: int
    uploaded_at: Optional[str] = None
    created_at: str
    updated_at: str


# ── WebSocket Messages ──────────────────────────────────────

class ProgressMessage(BaseModel):
    job_id: int
    status: str  # running, completed, failed
    progress: int = 0
    phase: str = ""
    message: str = ""
    video_id: Optional[int] = None
