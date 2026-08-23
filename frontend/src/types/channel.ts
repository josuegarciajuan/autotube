/** Channel configuration — mirrors config/canal2_config.py fields. */
export interface ChannelConfig {
  canal_display_name: string
  canal_tagline: string
  canal_outro_tagline: string
  canal_narrative_style: string

  voice_id: string
  voice_rate: string
  voice_pitch: string
  voice_volume: string
  voice_secondary: string

  reddit_subreddits: string[]
  wikipedia_categories: string[]

  image_style_modifiers: string
  color_palette: Record<string, number[]>
  film_grain_opacity: number
  ken_burns_zoom_min: number
  ken_burns_zoom_max: number
  thumbnail_width: number
  thumbnail_height: number
  thumbnail_font_size: number
  thumbnail_border_width: number

  yt_category_id: string
  yt_privacy_status: string
  yt_default_tags: string[]
  description_template: string

  seo_primary_keyword: string
  seo_secondary_keywords: string[]
  seo_hashtags: string[]

  canal_tone: string
  script_hook_rule: string
  script_end_hook: string
  script_emotional_arc: Record<string, string>

  title_formulas: string[]
  title_power_words: string[]

  content_pillars: { name: string; ratio: number; desc: string }[]

  // Duration / Generation
  video_average_duration_min: number
  video_duration_discrepancy_min: number
  test_mode: boolean
  test_script_words_min: number
  test_script_words_max: number
  test_script_scenes_min: number
  test_script_scenes_max: number
  test_script_blocks_min: number
  test_script_blocks_max: number
  test_video_duration_target: number
  quick_test_script_words_min: number
  quick_test_script_words_max: number
  quick_test_script_scenes_min: number
  quick_test_script_scenes_max: number
  quick_test_script_blocks_min: number
  quick_test_script_blocks_max: number
  quick_test_video_duration_target: number
  quick_test_images_per_scene: number
  prod_script_words_min: number
  prod_script_words_max: number
  prod_script_scenes_min: number
  prod_script_scenes_max: number
  prod_script_blocks_min: number
  prod_script_blocks_max: number
  prod_video_duration_min: number
  prod_video_duration_max: number
}

/** Channel entity — matches the API GET /api/channels response. */
export interface Channel {
  id: number
  name: string
  slug: string
  config_json: ChannelConfig | Record<string, any>
  active: boolean
  description: string | null
  banner_url: string | null
  avatar_url: string | null
  yt_channel_id: string | null
  yt_channel_url: string | null
  google_account: string | null
  yt_studio_url: string | null
  created_at: string
  updated_at: string
}

/** Aggregate shorts statistics for a channel. */
export interface ShortsStats {
  total: number
  published: number
  pending: number
  ready: number
  rendering: number
  failed: number
  total_views: number
  total_likes: number
  total_comments: number
}

/** Sections for the config viewer UI */
export interface ConfigSection {
  key: string
  label: string
  fields: ConfigField[]
}

export interface ConfigField {
  key: string
  label: string
  affectsVideo: boolean   // ⚡ badge
  type: 'text' | 'list' | 'dict' | 'number' | 'boolean' | 'select' | 'voice-select'
  options?: { value: string; label: string }[]  // for 'select' type
}

/** Pre-defined sections matching canal2_config.py structure */
export const CONFIG_SECTIONS: ConfigSection[] = [
  {
    key: 'identity',
    label: 'Identidad',
    fields: [
      { key: 'CANAL_DISPLAY_NAME', label: 'Nombre', affectsVideo: true, type: 'text' },
      { key: 'CANAL_TAGLINE', label: 'Tagline', affectsVideo: false, type: 'text' },
      { key: 'CANAL_OUTRO_TAGLINE', label: 'Outro', affectsVideo: true, type: 'text' },
      { key: 'CANAL_NARRATIVE_STYLE', label: 'Estilo Narrativo', affectsVideo: true, type: 'text' },
    ],
  },
  {
    key: 'voice',
    label: '🎙️ Voz (TTS)',
    fields: [
      { key: 'VOICE_SELECT', label: 'Voz Narradora', affectsVideo: true, type: 'voice-select' },
      { key: 'VOICE_RATE', label: 'Velocidad', affectsVideo: true, type: 'text' },
      { key: 'VOICE_PITCH', label: 'Tono', affectsVideo: true, type: 'text' },
      { key: 'VOICE_VOLUME', label: 'Volumen', affectsVideo: true, type: 'text' },
      { key: 'VOICE_SECONDARY', label: 'Voz Secundaria', affectsVideo: true, type: 'text' },
    ],
  },
  {
    key: 'sources',
    label: '📥 Fuentes de Contenido',
    fields: [
      { key: 'REDDIT_SUBREDDITS', label: 'Subreddits', affectsVideo: true, type: 'list' },
      { key: 'WIKIPEDIA_CATEGORIES', label: 'Categorías Wikipedia', affectsVideo: true, type: 'list' },
    ],
  },
  {
    key: 'viral',
    label: '🦠 Viral Mirror',
    fields: [
      { key: 'VIRAL_ENABLED', label: 'Activar búsqueda viral', affectsVideo: true, type: 'boolean' },
      { key: 'NICHE_KEYWORDS_ENG', label: 'Keywords en inglés', affectsVideo: true, type: 'list' },
      { key: 'VIRAL_MIN_VIEWS', label: 'Mínimo de vistas', affectsVideo: true, type: 'number' },
      { key: 'VIRAL_MAX_AGE_DAYS', label: 'Antigüedad máxima (días)', affectsVideo: false, type: 'number' },
      { key: 'VIRAL_MAX_QUERIES', label: 'Queries máximas/día', affectsVideo: false, type: 'number' },
      { key: 'VIRAL_MAX_CANDIDATES', label: 'Candidatos máximos', affectsVideo: false, type: 'number' },
    ],
  },
  {
    key: 'visual',
    label: '🎨 Estilo Visual',
    fields: [
      { key: 'THUMBNAIL_VISUAL_STYLE', label: 'Estilo de Miniatura', affectsVideo: false, type: 'select',
        options: [
          { value: 'auto', label: '🤖 Auto-detectar (IA)' },
          { value: 'dark_cinematic', label: '🌑 Dark Cinematic — terror, misterio' },
          { value: 'vintage_archive', label: '📜 Vintage Archive — historia, documentos' },
          { value: 'realistic_documentary', label: '🎬 Realistic Documentary — educativo, científico' },
          { value: 'institutional_cold', label: '🏥 Institutional Cold — médico, experimentos' },
          { value: 'dramatic_contrast', label: '⚡ Dramatic Contrast — true crime, drama' },
          { value: 'moody_atmospheric', label: '🌫️ Moody Atmospheric — filosófico, arte' },
          { value: 'minimalist_clean', label: '🧊 Minimalist Clean — tecnología, datos' },
        ] },
      { key: 'IMAGE_STYLE_MODIFIERS', label: 'Modificadores de Imagen', affectsVideo: true, type: 'text' },
      { key: 'FILM_GRAIN_OPACITY', label: 'Grano de Película', affectsVideo: true, type: 'number' },
      { key: 'KEN_BURNS_ZOOM_MIN', label: 'Zoom Ken Burns (min)', affectsVideo: true, type: 'number' },
      { key: 'KEN_BURNS_ZOOM_MAX', label: 'Zoom Ken Burns (max)', affectsVideo: true, type: 'number' },
    ],
  },
  {
    key: 'youtube',
    label: '▶️ YouTube Metadata',
    fields: [
      { key: 'YT_CATEGORY_ID', label: 'Categoría', affectsVideo: true, type: 'text' },
      { key: 'YT_PRIVACY_STATUS', label: 'Privacidad', affectsVideo: true, type: 'text' },
      { key: 'YT_DEFAULT_TAGS', label: 'Tags por Defecto', affectsVideo: true, type: 'list' },
    ],
  },
  {
    key: 'seo',
    label: '🔍 SEO',
    fields: [
      { key: 'SEO_PRIMARY_KEYWORD', label: 'Keyword Principal', affectsVideo: true, type: 'text' },
      { key: 'SEO_SECONDARY_KEYWORDS', label: 'Keywords Secundarias', affectsVideo: true, type: 'list' },
      { key: 'SEO_HASHTAGS', label: 'Hashtags', affectsVideo: true, type: 'list' },
    ],
  },
  {
    key: 'script',
    label: '📝 Estructura de Guion',
    fields: [
      { key: 'CANAL_TONE', label: 'Tono Narrativo', affectsVideo: true, type: 'text' },
      { key: 'SCRIPT_HOOK_RULE', label: 'Regla del Hook', affectsVideo: true, type: 'text' },
      { key: 'SCRIPT_END_HOOK', label: 'CTA Final', affectsVideo: true, type: 'text' },
    ],
  },
  {
    key: 'titles',
    label: '🔤 Títulos',
    fields: [
      { key: 'TITLE_FORMULAS', label: 'Fórmulas', affectsVideo: true, type: 'list' },
      { key: 'TITLE_POWER_WORDS', label: 'Power Words', affectsVideo: true, type: 'list' },
    ],
  },
  {
    key: 'pillars',
    label: '📊 Pilares de Contenido',
    fields: [
      { key: 'CONTENT_PILLARS', label: 'Pilares', affectsVideo: false, type: 'dict' },
    ],
  },
  // ── Duration / Generation (agrupado) ──────────────────────
  {
    key: 'duration',
    label: '⏱️ Duración — Objetivo',
    fields: [
      { key: 'VIDEO_AVERAGE_DURATION_MIN', label: 'Duración media (min)', affectsVideo: true, type: 'number' },
      { key: 'VIDEO_DURATION_DISCREPANCY_MIN', label: 'Discrepancia de la media (min)', affectsVideo: true, type: 'number' },
    ],
  },
  {
    key: 'prod',
    label: '🚀 Producción',
    fields: [
      { key: 'PROD_VIDEO_DURATION_MIN', label: 'Duración (min) — mínimo', affectsVideo: true, type: 'number' },
      { key: 'PROD_VIDEO_DURATION_MAX', label: 'Duración (min) — máximo', affectsVideo: true, type: 'number' },
      { key: 'PROD_SCRIPT_WORDS_MIN', label: 'Palabras (min)', affectsVideo: true, type: 'number' },
      { key: 'PROD_SCRIPT_WORDS_MAX', label: 'Palabras (max)', affectsVideo: true, type: 'number' },
      { key: 'PROD_SCRIPT_SCENES_MIN', label: 'Escenas (min)', affectsVideo: true, type: 'number' },
      { key: 'PROD_SCRIPT_SCENES_MAX', label: 'Escenas (max)', affectsVideo: true, type: 'number' },
      { key: 'PROD_SCRIPT_BLOCKS_MIN', label: 'Bloques (min)', affectsVideo: true, type: 'number' },
      { key: 'PROD_SCRIPT_BLOCKS_MAX', label: 'Bloques (max)', affectsVideo: true, type: 'number' },
    ],
  },
  {
    key: 'test',
    label: '🧪 Modo Test',
    fields: [
      { key: 'TEST_MODE', label: 'Activo', affectsVideo: true, type: 'boolean' },
      { key: 'TEST_VIDEO_DURATION_TARGET', label: 'Duración (min)', affectsVideo: true, type: 'number' },
      { key: 'TEST_SCRIPT_WORDS_MIN', label: 'Palabras (min)', affectsVideo: true, type: 'number' },
      { key: 'TEST_SCRIPT_WORDS_MAX', label: 'Palabras (max)', affectsVideo: true, type: 'number' },
      { key: 'TEST_SCRIPT_SCENES_MIN', label: 'Escenas (min)', affectsVideo: true, type: 'number' },
      { key: 'TEST_SCRIPT_SCENES_MAX', label: 'Escenas (max)', affectsVideo: true, type: 'number' },
      { key: 'TEST_SCRIPT_BLOCKS_MIN', label: 'Bloques (min)', affectsVideo: true, type: 'number' },
      { key: 'TEST_SCRIPT_BLOCKS_MAX', label: 'Bloques (max)', affectsVideo: true, type: 'number' },
    ],
  },
  {
    key: 'quick_test',
    label: '⚡ Quick Test',
    fields: [
      { key: 'QUICK_TEST_VIDEO_DURATION_TARGET', label: 'Duración (min)', affectsVideo: true, type: 'number' },
      { key: 'QUICK_TEST_SCRIPT_WORDS_MIN', label: 'Palabras (min)', affectsVideo: true, type: 'number' },
      { key: 'QUICK_TEST_SCRIPT_WORDS_MAX', label: 'Palabras (max)', affectsVideo: true, type: 'number' },
      { key: 'QUICK_TEST_SCRIPT_SCENES_MIN', label: 'Escenas (min)', affectsVideo: true, type: 'number' },
      { key: 'QUICK_TEST_SCRIPT_SCENES_MAX', label: 'Escenas (max)', affectsVideo: true, type: 'number' },
      { key: 'QUICK_TEST_SCRIPT_BLOCKS_MIN', label: 'Bloques (min)', affectsVideo: true, type: 'number' },
      { key: 'QUICK_TEST_SCRIPT_BLOCKS_MAX', label: 'Bloques (max)', affectsVideo: true, type: 'number' },
      { key: 'QUICK_TEST_IMAGES_PER_SCENE', label: 'Imágenes por escena', affectsVideo: true, type: 'number' },
    ],
  },
]

// ── Promotion / Lifecycle ────────────────────────────────────

export type LifecycleActionType =
  | 'playlist_add'
  | 'first_comment'
  | 'comment_reply_1'
  | 'comment_reply_2'
  | 'ctr_check'
  | 'metadata_reoptimize'
  | 'social_clip_tiktok'
  | 'social_thread_twitter'
  | 'social_reel_instagram'
  | 'social_post_facebook'
  | 'social_post_reddit'
  | 'social_link_bluesky'
  | 'social_link_mastodon'

export const LIFECYCLE_ACTION_LABELS: Record<LifecycleActionType, string> = {
  playlist_add: 'Añadir a playlists',
  first_comment: 'Primer comentario',
  comment_reply_1: 'Responder comentarios (12h)',
  comment_reply_2: 'Responder comentarios (24h)',
  ctr_check: 'Análisis de CTR',
  metadata_reoptimize: 'Reoptimizar metadata',
  social_clip_tiktok: 'Publicar clip en TikTok',
  social_thread_twitter: 'Publicar hilo en Twitter/X',
  social_reel_instagram: 'Publicar Reel en Instagram',
  social_post_facebook: 'Publicar en Facebook',
  social_post_reddit: 'Publicar en Reddit',
  social_link_bluesky: 'Publicar teaser en Bluesky',
  social_link_mastodon: 'Publicar teaser en Mastodon',
}

export interface SocialAccount {
  id: number
  channel_id: number
  platform: string
  username: string
  enabled: boolean
  has_cookies: boolean
  last_login_at: string | null
  last_error: string | null
  created_at: string
  updated_at: string
}

export const SOCIAL_PLATFORMS = [
  {
    id: 'tiktok', label: 'TikTok', icon: '🎵', color: '#ff0050',
    description: 'Clip vertical 9:16 de 55-65s con cliffhanger. Subtítulos quemados.',
    strategy: 'El clip NO resuelve la historia — corta antes del desenlace. CTA: "Video completo en mi perfil". NUNCA incluye link de YouTube en el caption.',
    link: 'bio',
  },
  {
    id: 'twitter', label: 'Twitter/X', icon: '𝕏', color: '#1da1f2',
    description: 'Hilo de 5-7 tweets. Solo texto, sin imágenes ni video.',
    strategy: 'Contenido autónomo de alto valor. Cada tweet es una pieza independiente. El link al video SOLO en el último tweet.',
    link: 'last_tweet',
  },
  {
    id: 'instagram', label: 'Instagram', icon: '📷', color: '#e1306c',
    description: 'Reel vertical (mismo clip de TikTok). Caption con saltos de línea.',
    strategy: '6-8 hashtags: 3 nicho + 3 alcance medio + 2 masivos. CTA: "Link en bio". Sin link directo (no es cliqueable).',
    link: 'bio',
  },
  {
    id: 'facebook', label: 'Facebook', icon: '📘', color: '#1877f2',
    description: 'Post de texto con bullets + link de YouTube (OG card con thumbnail).',
    strategy: '3 puntos clave en formato lista. Link directo — Facebook genera preview automática. Sin hashtags.',
    link: 'direct',
  },
  {
    id: 'reddit', label: 'Reddit', icon: '🤖', color: '#ff4500',
    description: 'Post de texto largo (3-5 párrafos). Contenido 100% autónomo.',
    strategy: 'CERO autopromoción. El post debe ser valioso por sí mismo. Sin link. Solo mención sutil al final: "Investigué esto para un video".',
    link: 'none',
  },
  {
    id: 'rumble', label: 'Rumble', icon: '🎬', color: '#56c758',
    description: 'Video long-form completo. Monetización desde día 1 sin requisitos de suscriptores.',
    strategy: 'Sube el video completo vía API. Rumble licencia contenido viral a Yahoo/MSN/Newsweek generando ingresos adicionales por licensing.',
    link: 'none',
  },
  {
    id: 'dailymotion', label: 'Dailymotion', icon: '🎥', color: '#0066dc',
    description: 'Video long-form completo (espejo). API v2 gratuita, audiencia europea.',
    strategy: 'Re-subida del video completo con enlace a YouTube en la descripción. Credencial: client_id + client_secret (JSON).',
    link: 'direct',
  },
  {
    id: 'bluesky', label: 'Bluesky', icon: '🦋', color: '#0a7aff',
    description: 'Post de texto + link card (teaser). API gratuita, sin revisión.',
    strategy: 'Teaser con hook + enlace directo clicable a YouTube. Credencial: handle + app password.',
    link: 'direct',
  },
  {
    id: 'mastodon', label: 'Mastodon', icon: '🐘', color: '#6364ff',
    description: 'Post de texto + enlace (teaser). API libre, sin revisión.',
    strategy: 'Teaser con hashtags de nicho + enlace a YouTube. Credencial: user@instancia + token.',
    link: 'direct',
  },
  {
    id: 'odysee', label: 'Odysee', icon: '🧪', color: '#f06b01',
    description: 'Video espejo opcional (LBRY).',
    strategy: 'Re-subida completa. Opcional — solo si sobra capacidad.',
    link: 'none',
  },
] as const

export const LIFECYCLE_STATUS_LABELS: Record<string, string> = {
  pending: '⏳ Pendiente',
  executed: '✅ Ejecutado',
  failed: '❌ Fallido',
  skipped: '⏭️ Omitido',
  cancelled: '🚫 Cancelado',
}

export interface YouTubePlaylist {
  id: number
  channel_id: number
  slug: string
  yt_playlist_id: string
  name: string | null
  playlist_type: 'main' | 'onboarding' | 'thematic'
  created_at: string
}

export interface VideoPlaylist {
  id: number
  video_id: number
  playlist_id: number
  yt_playlist_item_id: string | null
  playlist_slug: string
  playlist_name: string | null
  playlist_type: string
  yt_playlist_id: string
  added_at: string
}

export interface LifecycleActionItem {
  id: number
  video_id: number
  action_type: LifecycleActionType
  scheduled_for: string | null
  executed_at: string | null
  status: 'pending' | 'executed' | 'failed' | 'skipped' | 'cancelled'
  result_json: string | null
  error_message: string | null
  retry_count: number
  config_json: string | null
  video_title?: string
}

// ── Insights AI (v20 — AI self-optimization) ───────────────────

export type InsightCategory =
  | 'duracion'
  | 'hora_publicacion'
  | 'keywords'
  | 'contenido'
  | 'errores'

export interface InsightRecommendation {
  id: string
  category: InsightCategory
  title: string
  detail: string
  confidence: number
  expected_impact: 'alta' | 'media' | 'baja'
  config_changes: Record<string, any>
  data_cited: Record<string, string>
  requires_code?: boolean
  opencode_prompt?: string
  rationale_brief?: string
  rationale_for_reuse?: string
  applied?: boolean
  discarded?: boolean
  // ── v20.1: validation & refinement ──
  validation?: ValidationResult
  refined_versions?: RefinedVersion[]
  // ── v21.1: dedup badges ──
  hidden_as_duplicate?: boolean
  duplicate_of?: string
  similarity_score?: number
  cross_channel_similar?: boolean
  cross_channel_name?: string
  similarity_to_previous?: number
}

export interface ValidationResult {
  status: 'resolved' | 'partial' | 'not_resolved'
  summary: string
  evidence: string[]
  confidence: number
  validated_at: string
  validated_by: string
}

export interface RefinedVersion {
  revised_config_changes: Record<string, any>
  explanation: string
  triggered_by: string
  refined_at: string
}

export interface KeyMetric {
  label: string
  value: string
  sparkline: number[]
  delta: string
  delta_positive: boolean
}

export interface ChannelInsight {
  id: number
  channel_id: number
  status: 'processing' | 'completed' | 'failed'
  current_phase: string | null
  phase_detail: string | null
  insights_json: {
    analysis_summary: string
    recommendations: InsightRecommendation[]
    health_score?: number
    key_metrics?: KeyMetric[]
  }
  raw_patterns: any | null
  raw_hypotheses: any | null
  error_msg: string | null
  model_used: string | null
  tokens_input: number
  tokens_output: number
  generation_time_ms: number
  retry_count: number
  heartbeat_at: string | null
  generated_at: string | null
  applied_at: string | null
  applied_by: string | null
}

export const INSIGHT_CATEGORY_META: Record<
  InsightCategory,
  { label: string; icon: string; color: string; bg: string; border: string }
> = {
  duracion: {
    label: 'Duracion',
    icon: '⏱',
    color: 'text-neon-cyan',
    bg: 'bg-neon-cyan/10',
    border: 'border-neon-cyan/30',
  },
  hora_publicacion: {
    label: 'Hora pub.',
    icon: '🕐',
    color: 'text-neon-gold',
    bg: 'bg-neon-gold/10',
    border: 'border-neon-gold/30',
  },
  keywords: {
    label: 'Keywords',
    icon: '🔑',
    color: 'text-purple-400',
    bg: 'bg-purple-400/10',
    border: 'border-purple-400/30',
  },
  contenido: {
    label: 'Contenido',
    icon: '📝',
    color: 'text-green-400',
    bg: 'bg-green-400/10',
    border: 'border-green-400/30',
  },
  errores: {
    label: 'Errores',
    icon: '⚠️',
    color: 'text-neon-red',
    bg: 'bg-neon-red/10',
    border: 'border-neon-red/30',
  },
  // v20.2: errores category is deprecated — analysis is now marketing-only.
  // Kept in type union for backward compatibility with existing DB insights.
  // Not included in categoryOrder — errores recs fall to end of list.
}

export function getCategoryMeta(
  category: InsightCategory
): (typeof INSIGHT_CATEGORY_META)[InsightCategory] {
  return INSIGHT_CATEGORY_META[category] || INSIGHT_CATEGORY_META.contenido
}

// ══════════════════════════════════════════════════════════
// Cross-Platform Publishing (v27)
// ══════════════════════════════════════════════════════════

export interface CrossPlatformConfig {
  facebook: boolean
  rumble: boolean
  tiktok: boolean
  settings?: Record<string, any>
}

export interface PlatformVideo {
  id: number
  video_id: number
  channel_id: number
  platform: string
  platform_video_id: string | null
  platform_video_url: string | null
  status: 'pending' | 'uploading' | 'processing' | 'published' | 'failed'
  privacy: string
  error_message: string | null
  attempts: number
  uploaded_at: string | null
  created_at: string
  updated_at: string
}
