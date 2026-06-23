/** Channel configuration — mirrors config/canal1_config.py fields. */
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
  created_at: string
  updated_at: string
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
  type: 'text' | 'list' | 'dict' | 'number' | 'boolean' | 'select'
  options?: { value: string; label: string }[]  // for 'select' type
}

/** Pre-defined sections matching canal1_config.py structure */
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
      { key: 'VOICE_ID', label: 'Voz Principal', affectsVideo: true, type: 'text' },
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
