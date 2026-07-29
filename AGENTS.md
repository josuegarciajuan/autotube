# Autotube Project

## Régimen de herramientas
- **Backend:** Python 3.10+, FastAPI, SQLite, MoviePy, edge-tts, OpenAI/DeepSeek
- **Frontend:** React + TypeScript + Vite, compilado en `frontend/dist/`
- **Servidor:** Uvicorn (`python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000`)

## Canales (v1 multi-channel)
| ID | Slug | Nombre | YouTube | Token |
|----|------|--------|---------|-------|
| 1 | canal2 | Sincronías | @cleanthelistemaillistclean7103 | tokens/canal2.pickle |
| 2 | canal3 | Civilizaciones Olvidadas | — | tokens/canal3.pickle |
| 3 | canal4 | Expediciones sin retorno | — | — |

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
- Stats de YouTube se recolectan automáticamente cada 6h (scheduler en `api/main.py`)
- Se almacenan en `video_stats_history` y `channel_stats_history`
- `POST /api/videos/stats` devuelve stats en tiempo real

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
