# Autotube V1 — Plan de Ejecución

## Estado: ✅ V1 COMPLETA

## Canal Sincronías
- **YouTube:** https://www.youtube.com/@cleanthelistemaillistclean7103
- **Video test subido:** https://www.youtube.com/watch?v=IGIYmnPAwUE (unlisted)
- **Token:** `tokens/canal2.pickle` ✅
- **Config:** `config/canal2_config.py` → 118 campos en DB ✅
- **Keywords YouTube:** sincronizadas ✅
- **País/Idioma:** ES/es sincronizados ✅

## Pendiente manual (YouTube Studio)
- [ ] Cambiar nombre: "CleanTheList Email list Cleaning" → "Sincronías"
- [ ] Subir banner: `output/thumbnails/canal2/banner.jpg` (2560x1440)
- [ ] Subir avatar: `output/thumbnails/canal2/avatar.jpg` (800x800)
- [ ] Pegar descripción en YouTube Studio

## Próximos pasos (V1.1+)
- [ ] Limpiar imports residuales de `canal1_config` en scrapers y helpers
- [ ] Proxy residencial (activable con env vars)
- [ ] Auto-scraping con proxies para evitar rate limits de Reddit
- [ ] Subtítulos automáticos vía captions API
- [ ] Estadísticas avanzadas desde YouTube Analytics API
- [ ] Multi-cuenta: crear canales nuevos desde el panel

---

## BLOQUES COMPLETADOS

### ✅ BLOQUE 1: Core Refactor (5 archivos)
- `config/settings.py` — vars proxy
- `database/db_extended.py` — migración perfil + métodos stats
- `database/schema_v3.sql` — tablas histórico
- `pipeline/youtube_uploader.py` — refactor multi-canal, scopes, proxy
- `api/services/proxy_manager.py` — gestor proxy

### ✅ BLOQUE 2: Nuevos Módulos (3 archivos)
- `api/routers/auth.py` — endpoints OAuth
- `pipeline/youtube_channel_manager.py` — gestor canal YouTube
- `pipeline/youtube_stats.py` — fetcher stats

### ✅ BLOQUE 3: API Routes + Orchestrator (4 archivos)
- `api/main.py` — router auth, stats collector
- `api/routers/channels.py` — sync-youtube, manual-setup, youtube-stats
- `api/routers/videos.py` — stats reales, stats history
- `orchestrator.py` — channel_slug a YouTubeUploader

### ✅ BLOQUE 4: Frontend (3 archivos + build)
- `frontend/src/lib/api.ts` — nuevos endpoints
- `frontend/src/pages/ChannelDetail.tsx` — auth UI, manual setup, stats
- `frontend/src/pages/Dashboard.tsx` — stats reales
- Build frontend: OK

### ✅ AUTH: Autenticación canal2
- OAuth via oficina: refresh token capturado
- `tokens/canal2.pickle` creado
- Canal conectado: UC32VJJKqpbiEExfEHYGxdNw

### ✅ BLOQUE 5: Config Canal
- Keywords, país, idioma → sincronizado vía API
- Banner + avatar → generados localmente (pendiente subida manual)
- Descripción → pendiente manual (API bloquea para canales no verificados)

### ✅ BLOQUE 6: Test Upload
- Video subido: https://www.youtube.com/watch?v=IGIYmnPAwUE
- Metadata completa: título, descripción, tags (10), categoría, idioma
- Warnings: subtítulos, playlist, end screens (esperado)

### ✅ BLOQUE 7: Hardening + Docs
- AGENTS.md actualizado (multi-channel, nuevos endpoints, proxy config)
- Plan spec actualizado (status final)
- Imports residuales documentados (no críticos para MVP)

---

## PRE-FLIGHT

### P0: Túnel IP Residencial → SALTADO
- `josue` (PC casa, Ubuntu 16, obsoleto) — SSH roto. Saltamos.
- `oficina` (PC trabajo, activa en Tailscale) — acceso limitado. Backup.
- **Decisión:** MVP sin proxy. Módulo de proxy implementado, desactivado. 3 vars de entorno para activar cuando se migre PC de casa.

### P1: Credenciales OAuth → COMPLETADO ✅
- App OAuth: `youtube-uploads-automation` (client_id `415608242228-...`)
- Token generado en oficina vía `run_local_server()` → refresh token copiado
- Creado `tokens/canal2.pickle` en el server
- Canal conectado: `CleanTheList Email list Cleaning` (UC32VJJKqpbiEExfEHYGxdNw)

---

## Datos Subibles vs Manuales (YouTube API)

| Dato | API | Manual | Acción sistema |
|------|-----|--------|----------------|
| Nombre canal | ❌ | ✅ | Avisar + sugerir nombre |
| Descripción canal | ✅ | - | Subir automático |
| Keywords canal | ✅ | - | Subir automático |
| Banner (2560x1440) | ❌ | ✅ | Generar archivo + instrucciones |
| Avatar (800x800) | ❌ | ✅ | Generar archivo + instrucciones |
| Título video | ✅ | - | Subir automático |
| Descripción video | ✅ | - | Subir automático |
| Tags video | ✅ | - | Subir automático |
| Miniatura video | ✅ | - | Subir automático |
| Categoría | ✅ | - | Subir automático |
| Subtítulos/CC | ✅ API aparte | - | Opcional futuro |
| Playlist | ✅ API aparte | - | Opcional futuro |
| End screens | ❌ | ✅ | Avisar |

---

## Config Proxy (futuro)
```env
PROXY_ENABLED=true
PROXY_TYPE=socks5
PROXY_HOST=127.0.0.1
PROXY_PORT=1080
PROXY_CHANNELS=canal2
```
