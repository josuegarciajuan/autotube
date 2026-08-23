# Autotube Project

## Régimen de herramientas
- **Backend:** Python 3.10+, FastAPI, SQLite, MoviePy, edge-tts, OpenAI/DeepSeek
- **Frontend:** React + TypeScript + Vite, compilado en `frontend/dist/`
- **Servidor:** Uvicorn (`python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000`)

## Canales (v1 multi-channel)
| ID | Slug | Nombre | YouTube | Token (OAuth) | Proyecto GCP (cuota compartida) |
|----|------|--------|---------|---------------|----------------------------------|
| 1 | canal2 | Sincronías | @cleanthelistemaillistclean7103 | tokens/canal2.pickle | `youtube-uploads-automation` |
| 2 | canal3 | Civilizaciones Olvidadas | — | tokens/canal3.pickle | `youtube-uploads-automation` |
| 3 | canal4 | Expediciones sin retorno | — | tokens/canal4.pickle | `autotube-expediciones` |
| 4 | canal5 | Anomalías Médicas | — | tokens/canal5.pickle | `autotube-expediciones` |

> **Cuota por token compartido:** la cuota del YouTube Data API es por **proyecto GCP**, no por canal.
> - canal2 + canal3 comparten `youtube-uploads-automation` (cuenta Google `tracatrack`).
> - canal4 + canal5 comparten `autotube-expediciones` (cuenta Google `burrianacasa2026`).
> El circuit-breaker de cuota y los avisos (`quota_exhausted`) son **por proyecto**: un 403 de un canal
> pausa solo los canales de su proyecto, y el alert nombra el proyecto + los canales que lo comparten.

> **Recolección de stats sin cuota (scraping):** cuando la cuota del Data API está agotada, el botón
> "Recolectar stats" entra automáticamente en **modo scraping** (`use_data_api=False`): usa yt-dlp para
> leer vistas/likes/comentarios/subs públicos (0 cuota) y el Analytics API para watch-time. Funciona
> incluso para canales sin token OAuth (datos públicos). Ver `pipeline/youtube_stats_scraper.py`.

## 🆕 Crear un canal nuevo — metodología plantillable

**Principio:** El código es genérico. Los datos son del canal. No se hardcodea nada.

### Arquitectura que lo hace posible

| Capa | Archivo | Qué aporta |
|---|---|---|
| **Defaults heredables** | `config/defaults.py` | ~135 parámetros compartidos (test mode, media, transiciones, música, shorts, etc.) |
| **Prompts parametrizados** | `prompts/base_prompts.py` | Templates LLM que derivan el lenguaje del nicho desde `CANAL_NARRATIVE_STYLE` |
| **Config por canal** | `config/{slug}_config.py` | Solo ~40-60 parámetros específicos del nicho (identidad, tono, fuentes, estilo visual) |
| **Frontend dinámico** | `frontend/src/lib/channelConfig.ts` | Colores asignados automáticamente por `channel.id`, abreviaturas desde `CANAL_INITIALS` |
| **Google Accounts** | `channels.google_account` (DB) | Se consulta en runtime, sin mapas hardcodeados |

### Proceso de creación (2 pasos)

#### Paso 1 — Diseño conceptual (conversación)
El usuario describe la idea del canal. Extraemos y consensuamos:

| Dato | Ejemplo | Obligatorio |
|---|---|---|
| **Nombre del canal** | `"Anomalías Médicas"` | ✅ |
| **Slug** | `"canal6"` o `"anomalias-medicas"` | ✅ |
| **Nicho / temática** | Casos clínicos inexplicables, enfermedades raras | ✅ |
| **Estilo narrativo** | `"documental médico de asombro"` | ✅ |
| **YouTube Handle** | `@AnomaliasMedicas` | Opcional (si ya existe el canal) |
| **Cuenta Google** | `tracatrack` o `burrianacasa2026` | Opcional (para auto-marcado IA) |
| **Tagline** | Frase de 1-2 líneas que define el canal | ✅ |
| **Duración objetivo** | 8-14 min, 6-10 min, etc. | ✅ (afecta PROD_SCRIPT_WORDS) |
| **Categoría YouTube** | 24 (Entertainment) o 27 (Education) | ✅ |
| **Tono** | Descripción detallada para el LLM | ✅ |
| **Audiencia** | Demográficos y psicográficos | ✅ |
| **Fuentes de contenido** | Reddit subs, Wikipedia cats, RSS feeds | ✅ |
| **Estilo visual** | Paleta de colores, modificadores de imagen | ✅ |

#### Paso 2 — Ejecución (el agente crea todo)
Una vez claros los datos, se ejecutan estas acciones en orden:

```
1. Crear config/{slug}_config.py
   → Solo parámetros específicos (~500 líneas máx)
   → No importa defaults.py directamente — el bridge hace merge automático

2. Crear el canal en el panel web:
   → Ir a http://localhost:5173 → Canales → Nuevo Canal
   → Rellenar nombre, slug, youtube_handle, google_account
   → La API genera config_json automáticamente desde defaults + identidad

3. Sincronizar config Python → DB:
   → Botón "Sync Python" en la página del canal
   → O: curl -X POST localhost:8000/api/channels/{id}/sync-config

4. Verificar que la config carga correctamente:
   python3 -c "from config.config_bridge import get_channel_config; cfg = get_channel_config('{slug}'); print(cfg.CANAL_DISPLAY_NAME, cfg.SUBTITLE_FONT_SIZE)"

5. (Opcional) Autenticar YouTube:
   → API: POST /api/channels/{id}/auth-start → da URL de OAuth
   → Abrir URL en navegador, autorizar, copiar código
   → API: POST /api/channels/{id}/auth-code → guarda token en tokens/{slug}.pickle

6. (Opcional) Generar assets del canal:
   → API: POST /api/channels/{id}/generate-profile
   → Genera banner + avatar + descripción vía Pollo AI
   → O hacerlo manualmente en YouTube Studio

7. Configurar planning:
   → Ir a Planificación → ajustar videos/día, shorts/día, etc.

8. Test rápido de generación:
   python3 test_video.py --canal {slug} --skip-scrape --quick
```

### Parámetros clave del archivo de config

El archivo `config/{slug}_config.py` debe definir estos bloques (el resto se hereda de `defaults.py`):

```python
# IDENTITY (obligatorio)
CANAL_NAME = "{slug}"
CANAL_DISPLAY_NAME = "{Nombre}"
CANAL_TAGLINE = "{tagline}"
CANAL_OUTRO_TAGLINE = "{outro tagline}"
YOUTUBE_HANDLE = "@handle"          # opcional
YOUTUBE_CHANNEL_URL = "..."         # opcional
CANAL_NARRATIVE_STYLE = "documental de X"
CANAL_STYLE_DESCRIPTION = "{descripción larga para prompts}"
CHANNEL_ABOUT_SECTION = """..."""   # texto para YT Studio
CHANNEL_KEYWORDS = [...]            # 20 keywords
CANAL_INITIALS = "XX"               # 2-3 letras
LOGO_SIZE = 140                     # o 180

# PRODUCTION TARGETS
PROD_SCRIPT_WORDS_MIN = 2000
PROD_SCRIPT_WORDS_MAX = 3500
PROD_SCRIPT_SCENES_MIN/MAX = ...
PROD_VIDEO_DURATION_MIN/MAX = ...
VIDEO_AVERAGE_DURATION_MIN = ...

# NARRATIVE TONE
CANAL_TONE = "{descripción larga}"
TARGET_AUDIENCE = "{demográficos}"
TARGET_AUDIENCE_PSYCHOGRAPHIC = {{...}}

# TITLE OPTIMIZATION
TITLE_FORMULAS = [...]
TITLE_POWER_WORDS = [...]
TITLE_MAX_CHARS = 65

# SCRIPT STRUCTURE
SCRIPT_HOOK_RULE = "..."
SCRIPT_STRUCTURE = [...]
SCRIPT_END_HOOK = "..."
SCRIPT_EMOTIONAL_ARC = {{...}}
RETENTION_ANCHORS = {{...}}
VIRALITY_TRIGGERS = [...]

# VOICE / TTS
TTS_STRATEGY = {{...}}
VOICE_RATE, VOICE_PITCH, TTS_ENGINE, KOKORO_VOICE, KOKORO_BLOCK_SPEEDS

# CONTENT SOURCES
REDDIT_SUBREDDITS = [...]
WIKIPEDIA_CATEGORIES = [...]
SCRAPE_SOURCES = [...]
ATLAS_OBSCURA_CATEGORIES = [...]
RSS_FEEDS = [...]
GOOGLE_NEWS_QUERIES = [...]

# VISUAL STYLE
IMAGE_STYLE_MODIFIERS = "..."
COLOR_PALETTE = {{...}}
FILM_GRAIN_OPACITY, FILM_GRAIN_FRAMES
KEN_BURNS_ZOOM_MIN, KEN_BURNS_ZOOM_MAX

# MEDIA STRATEGY (incluir fallback_query y fallback_query_simple)
MEDIA_STRATEGY = {{...}}

# INTRO / OUTRO
INTRO_FONT_SIZE, INTRO_BG_COLOR
OUTRO_FONT_SIZE, OUTRO_BG_COLOR, OUTRO_TEXT
CTA_TEXT, CTA_TEXT_VARIANTS
INTRO_VOICE_TEXT, CTA_VOICE_TEXT, OUTRO_VOICE_TEXT

# YOUTUBE METADATA (solo lo que difiere de defaults)
YT_CATEGORY_ID, PUBLISH_MODE, PUBLISH_WARMUP_MIN
UPLOAD_WINDOWS, YT_DEFAULT_TAGS

# SEO
SEO_PRIMARY_KEYWORD, SEO_SECONDARY_KEYWORDS, SEO_HASHTAGS

# SHORTS
SHORTS_PER_DAY, SHORTS_HASHTAGS (SHORTS_SUBSCRIBE_CTA_VARIANTS opcional)

# DESCRIPTION
DESCRIPTION_TEMPLATE = """..."""

# THUMBNAIL
THUMBNAIL_VISUAL_STYLE, THUMBNAIL_MANUAL_STYLE, THUMBNAIL_STYLE, THUMBNAIL_TEMPLATES
THUMBNAIL_BORDER_WIDTH, THUMBNAIL_FONT_FAMILY, THUMBNAIL_BORDER_COLOR
THUMBNAIL_SHOW_4K_BADGE, THUMBNAIL_TEXT_STROKE_COLOR

# MONETIZATION
VIDEO_MIDROLL_STRATEGY, MONETIZATION_TARGET_CPM, MONETIZATION_VERTICALS

# END SCREEN
END_SCREEN_STRATEGY = {{...}}

# PLAYLISTS (5 definiciones con slug, name, description, type)
PLAYLISTS = [...]

# FIRST 48H / COMMUNITY / CROSS-PLATFORM / COLLAB
FIRST_48H_STRATEGY, COMMUNITY_TAB_PLAN, CROSS_PLATFORM
COLLABORATION_TARGETS, TRENDING_TOPIC_HOOKS, CONTENT_PILLARS

# MARATHON & VIRAL
NICHE_KEYWORDS_ENG, MARATHON_NARRATIVE_FORMAT, MARATHON_TITLE_FORMAT
VIRAL_PLAYLIST_KEYWORDS
```

### 🚫 Invariante de nuevas features

Toda feature nueva que afecte a canales debe seguir este contrato:

| ❌ Prohibido | ✅ Correcto |
|---|---|
| `if slug == "canal2"` o `if channel_id == 1` | Leer comportamiento de `config_json` del canal |
| `from config.canal2_config import ...` en pipeline/scrapers/services | `config_bridge.get_channel_config(slug)` |
| Diccionarios hardcodeados slug→valor | Columna en DB o campo en `config_json` |
| Archivos de prompts por canal | `prompts.base_prompts.build_system_prompt(cfg)` |
| Defaults a un canal concreto en scripts | Argumento requerido o leer de DB |
| Colores/abreviaturas hardcodeados en frontend | `getChannelShort(ch)`, `getChannelStyles(ch)` |

Cada nuevo parámetro que necesite variar por canal:
1. Se añade a `config/defaults.py` con un valor por defecto sensato
2. Se sobrescribe en `config/{slug}_config.py` si el canal necesita un valor distinto
3. Se lee en el código vía `cfg = get_channel_config(slug); cfg.MI_PARAMETRO`

## OAuth por canal
- Cada canal usa su propio `client_secret` (priority: `config/client_secret_{slug}.json` → `config/client_secret.json`)
- Tokens se guardan en `tokens/{slug}.pickle` (pickle)
- Scopes: `youtube` + `yt-analytics.readonly`
- Para autenticar un canal nuevo (headless): `python3 scripts/oauth_quick.py` en máquina con navegador, o usar endpoints `/api/channels/{id}/auth-start` + `/api/channels/{id}/auth-code`

## Reglas obligatorias

### 📝 Git — cada cambio se commitea
- **Cada cambio en el código debe registrarse en git** con un commit atómico y descriptivo.
- Formato recomendado: `tipo: descripción breve` (ej. `feat: añadir soporte para thumbnails Pollo AI`, `fix: corregir cálculo de duración de voz`)
- Tipos: `feat`, `fix`, `refactor`, `docs`, `chore`, `test`, `style`
- **Nunca** commitear secretos: `.env`, `client_secret_*.json`, `*.pickle`

### 🚀 Modo desarrollo (hot-reload) — RECOMENDADO para trabajar con cambios
El sistema ahora soporta **hot-reload completo**: cambios en Python y frontend se reflejan
instantáneamente **sin matar la generación de video en curso**.

```bash
# Arrancar servidores de desarrollo (API con reload + Vite HMR)
bash scripts/start_dev.sh

# Parar servidores
bash scripts/stop_dev.sh
```

**Cómo funciona:**
- La **API** corre con `uvicorn --reload` — se reinicia sola al detectar cambios en `.py`
- El **frontend** corre con Vite dev server en `:5173` — HMR (Hot Module Replacement) en vivo
- La **generación de video** se lanza como **proceso independiente** (subprocess con `start_new_session`) — **NO muere cuando la API se reinicia**
- Modo subprocess activado por defecto: `USE_SUBPROCESS_WORKER = True` en `api/services/generation_service.py`

**URLs en desarrollo:**
- Frontend (HMR): `http://localhost:5173`
- API (Swagger): `http://localhost:8000/api/docs`
- Logs: `tail -f logs/api_dev.log` | `tail -f logs/vite_dev.log`

### 📦 Modo producción — aplicar cambios sin downtime
Cuando necesites aplicar cambios en producción sin interrumpir generaciones activas:

```bash
# Rebuild frontend + restart API (workers sobreviven)
bash scripts/apply_changes.sh
```

Este script:
1. Recompila el frontend (`npm run build`)
2. Reinicia la API (graceful kill + restart)
3. **NO mata workers de generación activos**

### 🛠️ Restart manual tradicional (solo si no hay generación activa)

```bash
# Frontend — recompilación
cd frontend && npm run build

# API — reinicio
PID=$(pgrep -f "uvicorn api.main:app") && kill $PID 2>/dev/null; sleep 1 && cd /root/autotube && nohup python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --log-level info > logs/api.log 2>&1 &
```

### ⚙️ Arquitectura del worker independiente

El worker de generación (`api/services/full_pipeline_worker.py`) es un script standalone que:
- Se ejecuta como proceso independiente (`subprocess.Popen` con `start_new_session=True`)
- Escribe progreso en la tabla `videos` (columna `progress` + `progress_phase`)
- La API monitorea el progreso desde la DB y lo transmite por WebSocket
- Si la API se reinicia, el worker sigue corriendo — al volver, la API reanuda el monitoreo

**No mezclar modos:** Si `USE_SUBPROCESS_WORKER=True`, todas las generaciones nuevas
usan el worker independiente. Si `False`, usan el modo legacy (in-process, mueren con la API).

**⚠️ Si hay una generación corriendo en modo legacy (in-process), NO reiniciar la API
porque matará la generación.** Usa `scripts/apply_changes.sh` que detecta esto y advierte.

### Base de datos — schema y migraciones
- Schema base: `database/schema.sql`
- Schema v2 (panel): `database/schema_v2.sql`  
- Schema v3 (stats): `database/schema_v3.sql`
- Migraciones automáticas en `database/db_extended.py::migrate_v2()` (idempotentes, incluye v3)

### ⚠️ Preservación de imágenes — NO borrar en limpiezas
**El directorio `output/thumbnails/` contiene thumbnails, banners y avatares recuperados de YouTube.**
- `output/thumbnails/{slug}/banner.jpg` — banner del canal (descargado de YouTube API)
- `output/thumbnails/{slug}/avatar.jpg` — foto de perfil del canal (descargado de YouTube API)
- `output/thumbnails/{slug}/thumb_{video_id}.jpg` — thumbnail original de cada video (descargado de YouTube CDN)
- **NUNCA borrar `output/thumbnails/` ni sus subdirectorios en limpiezas del sistema.**
- Si se borran accidentalmente, recuperar con: `python3 scripts/recover_thumbnails.py`

### Config Bridge
- `config/config_bridge.py` → `get_channel_config(slug)` — fuente única de configuración
- Al arrancar la API, `sync_all_configs_to_db()` sincroniza configs Python → DB
- El pipeline lee la config vía bridge (DB tiene prioridad sobre .py)
- Per-channel client secrets: `config/client_secret_{slug}.json`

## Endpoints nuevos (v1)
| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/api/channels/{id}/auth-start` | Inicia flow OAuth |
| POST | `/api/channels/{id}/auth-code` | Completa auth con código |
| GET | `/api/channels/{id}/auth-status` | Estado de autenticación |
| POST | `/api/channels/{id}/sync-youtube` | Sync keywords+country+language vía API |
| GET | `/api/channels/{id}/manual-setup` | Instrucciones para setup manual (banner, avatar, etc.) |
| GET | `/api/channels/{id}/youtube-stats` | Stats en tiempo real del canal |
| GET | `/api/videos/{id}/stats-history` | Histórico de stats del video |

### 🔒 Invariante de concurrencia — SOLO UNA generación a la vez
**NUNCA debe haber más de una generación de video larga (long-form) ejecutándose simultáneamente.**

- La concurrencia de renders de ffmpeg causa contención de RAM que puede matar decoders y producir videos corruptos o incompletos.
- **Mecanismo de enforcement:**
  - `_DISPATCH_LOCK` (`threading.Lock` en `api/services/generation_service.py`) — serializa todos los puntos de entrada de dispatch.
  - `count_active_jobs()` en `database/db_extended.py` — guardia global.
  - Guardia secundario en `start_generation_job_subprocess()` — cierra ventana TOCTOU residual.
- **Si un segundo dispatch pasa los guards por error**, el guardia secundario lo bloquea y limpia los registros huérfanos (video → `error`, planned_slot → `pending`, job → `failed`).
- Los shorts (generate_native_short, generate_clip_short) y uploads F2 están **excluidos** de este límite (pueden correr en paralelo con una generación long-form).
- Los reassemble también están limitados: solo uno a la vez.

### 🎬 Invariante de escenas — NO repetir jamás
**Un video generado NUNCA repetirá escenas visuales.** Cada escena debe tener un asset visual único (video o imagen distinta).

- Si un provider de video devuelve una URL ya usada en este mismo video, se descarta y se busca otra.
- Si no hay más videos únicos disponibles para un topic, se usan imágenes alternativas (Pixabay tiene cientos de imágenes por query).
- El dedup se aplica tanto a URLs de video/imagen como a `pixabay_photo image id` para evitar imágenes duplicadas.
- **Si se agotan TODOS los assets únicos → placeholder o fallback genérico, pero nunca repetir un asset ya usado.**

### 🚫 Invariante: Shorts Backfill DESACTIVADO
**El backfill de links long-form en shorts antiguos está PERMANENTEMENTE DESACTIVADO.**

- Los shorts nuevos ya incluyen el link al video long-form en su descripción al subirse mediante `build_short_description()` en los 7 code paths de upload.
- `api/services/shorts_backfill_service.py` tiene `BACKFILL_ENABLED = False` hardcodeado.
- El loop `_shorts_backfill_loop()` en `api/main.py` está comentado con `shorts_backfill_task = None`.
- `scripts/backfill_shorts_links.py` tiene un `sys.exit(0)` al inicio con aviso de deprecación.
- La migración v32 en `db_extended.py` marcó todos los shorts existentes como `longform_linked = 1`.
- **Consumo evitado:** ~36,720 unidades/día de YouTube API quota.
- **Para reactivarlo:** justificar impacto en cuota, cambiar `BACKFILL_ENABLED = True`, descomentar el loop en `api/main.py`, y quitar el `sys.exit(0)` en el script.

## 🛡️ Invariantes anti-strike de YouTube (ago 2026)

Tras 3 strikes por spam/IA en 4 días (canal5 19/8, canal4 20/8, canal3 22/8), rigen
estas reglas DURAS. No relajarlas sin justificación:

1. **Nunca subir un short con render degradado (solid bg).** `api/services/shorts_scheduler.py`
   rechaza (`return None`) el render si `_valid_assets_total == 0` o la fracción de
   escenas con asset real < `SHORTS_MIN_VALID_ASSET_RATIO` (0.5). El fallback de fondo
   liso ya no se sube.
2. **Máx 1 short/día por canal (hard cap).** Valor por defecto del **perfil de pacing
   `strike`** (`shorts_per_channel_day`), forzado en dispatch normal, force-dispatch
   (NO saltable por bypass) y cola de nativos.
3. **Espaciado global anti-ráfaga entre canales.** `api/services/upload_spacing.py`
   impone `global_upload_spacing_min` (perfil `strike` = 45 min) entre subidas de
   CANALES DISTINTOS. Se aplica en `pipeline/youtube_uploader.py::upload()` (choke
   point único) vía `_wait_global_upload_spacing()`, y se registra con
   `record_upload()` tras subir.
4. **Filtro duro de temas sensibles.** `pipeline/content_safety.py` rechaza menores
   (en contexto médico/criminal), autolesión/suicidio, claims médicos de cura,
   violencia gráfica y desinformación sanitaria. Se aplica en shorts (native y
   standalone) y en long-forms (`orchestrator.phase_generate_script`, pre y post-guion).
5. **Verificación post-subida endurecida.** `POST_UPLOAD_VERIFY_RETRIES=4`,
   `POST_UPLOAD_VERIFY_DELAY=10`, con fallback a la watch page (0 cuota) para NO
   registrar strike por lag de indexado (watch page "private"/"available" ⇒ no strike).
6. **Sweep diario de eliminaciones silenciosas.** `scripts/check_video_removals.py`
   barre los últimos N vídeos/shorts (0 cuota) y crea alerta `silent_removal` si YouTube
   borró algo retroactivamente que constaba como publicado.

### 🎛️ Perfil central de cadencia ("strike mode") — `api/services/pacing_profile.py`

Todas las reglas de **frecuencia y espaciado** se centralizan en un ÚNICO perfil
persistido en `system_state["pacing_profile"]` (`strike` | `recovery` | `normal`).
Relajar los strikes = **cambiar el perfil en un clic** (panel → Programación →
"Perfil de Cadencia" o `PUT /api/pacing/profile`) y todo el sistema se reajusta solo:

| Clave | strike (hoy) | recovery | normal |
|---|---|---|---|
| `shorts_per_channel_day` | 1 | 2 | 3 |
| `shorts_global_day` | 6 | 8 | 12 |
| `max_longform_publish_day` | 1 | 1 | 2 |
| `same_channel_publish_gap_h` | 24 | 12 | 6 |
| `same_channel_upload_gap_h` | 6 | 4 | 3 |
| `global_upload_spacing_min` | 45 | 30 | 20 |
| `account_daily_upload_cap` | 4 | 6 | 8 |
| `shorts_cooldown_min` | 180 | 120 | 90 |
| `shorts_same_type_gap_min` | 240 | 180 | 120 |
| `shorts_cross_type_gap_min` | 20 | 20 | 20 |

- Resolución: override manual `system_state["pacing_<clave>"]` (kill-switch puntual)
  > perfil activo > perfil `strike` (fallback). Para las claves de pacing, el perfil
  **GANA sobre config_json por canal** (deprecados como override: `MAX_LONGFORM_PUBLISH_PER_DAY`,
  `MIN_SAME_CHANNEL_UPLOAD_GAP_HOURS`, `ACCOUNT_DAILY_UPLOAD_CAP`).
- `content_safety_disabled` (kill-switch del filtro) y `global_upload_spacing_min`
  (override manual legacy) siguen funcionando con máxima prioridad.

Consumidores actuales: `upload_spacing`, `shorts_scheduler` (caps duros, cooldown,
gaps), `publish_scheduler` (gap mismo-canal + tope diario repack), `spam_mitigation`
(cap por cuenta), `upload_scheduler` (gap de subida mismo-canal).

## Proxy residencial (implementado, desactivado)
```env
PROXY_ENABLED=false           # true = activar
PROXY_TYPE=socks5             # socks5 (SSH tunnel) o http
PROXY_HOST=127.0.0.1
PROXY_PORT=1080
PROXY_CHANNELS=canal2         # vacío = todos los canales
```

## Campos subibles vs manuales (YouTube API)
| Dato | API | Manual |
|------|-----|--------|
| Nombre canal | ❌ | ✅ YouTube Studio |
| Descripción canal | ❌* | ✅ YouTube Studio |
| Keywords canal | ✅ | — |
| País/Idioma | ✅ | — |
| Banner (2560x1440) | ❌ | ✅ YouTube Studio |
| Avatar (800x800) | ❌ | ✅ YouTube Studio |
| Título video | ✅ | — |
| Descripción video | ✅ | — |
| Tags | ✅ | — |
| Miniatura video | ✅ | — |
| Categoría | ✅ | — |
| Subtítulos | ✅ (API aparte) | — |
| Playlists | ✅ (API aparte) | — |
| End screens | ❌ | ✅ YouTube Studio |
| Contenido alterado/IA | ❌ | ✅ YouTube Studio > Contenido > ¿Contenido alterado o sintético? > Sí |
| Pantallas finales | ❌ | ✅ YouTube Studio > Pantalla final > 2 elementos (vídeo recomendado + suscribirse) |
| Hora de publicación | ✅ `publishAt` | — |

*Descripción del canal: API bloquea `snippet.description` para canales no verificados.

## Publicación programada (nuevo modo scheduled)
- **Activación por canal:** `PUBLISH_MODE = "scheduled"` en `config/canalX_config.py`.
- **Flujo:** sube el vídeo como `private` → período de "calentamiento" (warmup) → se publica automáticamente a la **hora pico** calculada (con ±X minutos de jitter).
- **Hora pico:** heurística por nicho/palabras clave (tabla en `pipeline/publish_scheduler.py`) + auto-ajuste con histórico de rendimiento del canal (`video_stats_history`).
- **Config relevante por canal:** `PUBLISH_MODE`, `PUBLISH_TIMEZONE`, `PUBLISH_TARGET_HOUR`, `PUBLISH_JITTER_MIN`, `PUBLISH_WARMUP_MIN`.
- **Auto-marcado IA + pantallas finales:** tras cada subida, un daemon thread ejecuta automáticamente `pipeline/youtube_browser.py::mark_altered_content()` (marca "contenido alterado/IA" en YouTube Studio vía Playwright) y `add_end_screens()` (configura Suscribirse + Vídeo recomendado), controlado por `AUTO_MARK_ALTERED_CONTENT` y `AUTO_END_SCREENS` en la config del canal (activado en todos los canales).
- **Playlist inteligente:** cada vídeo se asigna automáticamente a la playlist que mejor encaja con su contenido (clasificador LLM en `pipeline/youtube_playlists.py`).

## Stats collection
- **⛔ INVARIANTE: STATS_AUTO_COLLECT NUNCA se activa.** La recolección automática cada 6h consume quota de YouTube API innecesariamente.
- La recolección de stats **SOLO se activa manualmente** desde el botón "Recolectar stats" del dashboard (`POST /api/stats/collect`).
- `STATS_AUTO_COLLECT` está hardcodeado a `False` en `config/settings.py:246` — no depende de `.env`.
- `api/main.py:41` tiene un guard al startup que mata el servidor si `STATS_AUTO_COLLECT=True`.
- `STATS_ENABLED=false` en `.env` desactiva análisis adicionales (power words, view gap).
- Los stats se almacenan en `video_stats_history` y `channel_stats_history`.
- `POST /api/videos/stats` devuelve stats en tiempo real desde DB (no consume quota).

## Comandos frecuentes

```bash
# Test rápido de video
python3 test_video.py --canal canal2 --skip-scrape --quick

# Pipeline completo
python3 main.py run --canal canal2

# Subir video manual
python3 main.py upload --canal canal2

# Stats
python3 main.py stats --canal canal2

# Stats YouTube reales via API
curl localhost:8000/api/channels/1/youtube-stats
```

## 🧵 Cambios en paralelo (worktree + merge a producción)

El árbol principal (`/root/autotube`) queda **fijo en la rama de producción** (`master`).
Cada CAMBIO se trabaja en una copia aislada:

1. **`/git-workflow start`** crea la copia: `git worktree add -b work/<id> /root/.opencode-worktrees/autotube/<id>`.
   Se edita SOLO en la copia, nunca en el árbol principal.
2. Trabajar con commits atómicos `tipo: descripción` en la copia (el hook bloquea
   `.env`, `tokens/*.pickle`, `client_secret*.json` y secretos).
3. **`/git-workflow finish`**: merge `--no-ff` de `work/<id>` → `master` (bajo flock),
   resolviendo conflictos con marcadores de git si es necesario. Después ejecuta
   `bash scripts/apply_changes.sh` para publicar el cambio en el servicio en vivo.
4. **Push**: solo si existe remoto. Sin remoto, el merge ya dejó el código en producción
   (pendiente de apply_changes); reportar la acción manual exacta para la nube. Nunca inventar un remoto.
5. Si se pide OTRO cambio en la misma sesión, repetir el ciclo completo (nueva copia).
```
