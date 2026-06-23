# Autotube Project

## Régimen de herramientas
- **Backend:** Python 3.10+, FastAPI, SQLite, MoviePy, edge-tts, OpenAI/DeepSeek
- **Frontend:** React + TypeScript + Vite, compilado en `frontend/dist/`
- **Servidor:** Uvicorn (`python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000`)

## Canales (v1 multi-channel)
| ID | Slug | Nombre | YouTube | Token |
|----|------|--------|---------|-------|
| 1 | canal1 | Psicología Oculta | @historiasimpactantes | tokens/canal1.pickle |
| 2 | pruebas | Pruebas | @pruebas | — |
| 3 | canal2 | Sincronías | @cleanthelistemaillistclean7103 | tokens/canal2.pickle |

## OAuth por canal
- Cada canal usa su propio `client_secret` (priority: `config/client_secret_{slug}.json` → `config/client_secret.json`)
- Tokens se guardan en `tokens/{slug}.pickle` (pickle)
- Scopes: `youtube` + `yt-analytics.readonly`
- Para autenticar un canal nuevo (headless): `python3 scripts/oauth_quick.py` en máquina con navegador, o usar endpoints `/api/channels/{id}/auth-start` + `/api/channels/{id}/auth-code`

## Reglas obligatorias

### Frontend — recompilación tras cambios
**Cada vez que se modifique cualquier archivo en `frontend/src/`**, se debe recompilar el frontend:

```bash
cd frontend && npm run build
```

### API — reinicio tras cambios en Python
```bash
pkill -f "uvicorn api.main" && sleep 1 && cd /root/autotube && nohup python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --log-level info > logs/api.log 2>&1 &
```

### Base de datos — schema y migraciones
- Schema base: `database/schema.sql`
- Schema v2 (panel): `database/schema_v2.sql`  
- Schema v3 (stats): `database/schema_v3.sql`
- Migraciones automáticas en `database/db_extended.py::migrate_v2()` (idempotentes, incluye v3)

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

*Descripción del canal: API bloquea `snippet.description` para canales no verificados.

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
curl localhost:8000/api/channels/3/youtube-stats
```
