# Autotube Changelog

## [Unreleased]

### Fix — YouTube quota circuit breaker para schedulers
- Conectado el governor de cuota (10.000 ud/proyecto) al dispatch de subidas long-form y shorts; si supera el 85% marca `quota_exhausted_at` y deja backlog en espera.
- `quotaExceeded` ahora dispara el circuit breaker desde uploads, verificación post-upload, thumbnails, cambios de privacidad/descripción y health checks.
- Reducido ritmo: `videos_per_day=1`, shorts 2/día por canal, `GLOBAL_DAILY_UPLOAD_CAP=12`; `yt-dlp` pineado a `<2026` por compatibilidad con Python 3.10.
- Limpieza operativa: cancelados clips pendientes inválidos/del día tras el incidente y reforzado cleanup de Playwright para no dejar driver Node huérfano.

### Fase A — FIX: Duplicación (1 generación = 1 registro) + yt_video_id en registro trackeado
- **orchestrator.py**: añadido `db_video_id` opcional; `phase_video`/`phase_upload` usan `update_video` en vez de `insert_video` cuando viene de API.
- **api/services/generation_service.py**: pasa `db_video_id` al orquestador; guarda `yt_video_id`/`yt_url` en el registro trackeado tras subida exitosa.
- **pipeline/youtube_uploader.py**: suprimido `_log_to_db` en ruta API (`uploader.db = None`).
- **DB**: limpiados registros huérfanos duplicados del canal 3 (ids 27, 29 → consolidados en 26).

### Fase B — FIX: Duración de video derivada de VIDEO_OPTIMAL_DURATION_MINUTES (sin hardcodear)
- **prompts/canal2_prompts.py** y **prompts/canal1_prompts.py**: la rama de producción deriva `duration_target`, `words_min/max` y `blocks_min/max` del campo canónico `VIDEO_OPTIMAL_DURATION_MINUTES` (~150 palabras/min, ±15%, bloques proporcionales). Sin valores fijos por canal. TEST_MODE preservado para pruebas rápidas.
- **DB**: TEST_MODE desactivado en canales 1 y 3 → la siguiente generación usará ~10 min (1275-1724 palabras).

### Fase C — Pollo funcionando (vía worker idéntico a publicista) + thumbnails virales + badge 4K x2
- **tools/pollo_image_worker.py**: copiado verbatim del proyecto lamami (referencia publicista). Mismo contrato `text2Image.create`, usa curl-cffi, resuelve sin watermark.
- **pipeline/ai_image_generator.py**: wrapper que invoca el worker por subprocess igual que publicista (`--model pollo-image-v2 --aspect-ratio 16:9`). Cookie desde `POLLO_SESSION_COOKIE` o lamami settings.json. Firmas `generate`/`generate_batch` preservadas.
- **pipeline/thumbnail_style_engine.py**: eliminado veto de caras humanas (`"no human faces"`) del suffix y negativo de `build_pollo_prompt`.
- **pipeline/thumbnail_brainstorm.py**: prompts reescritos para forzar rostro humano en primer plano con expresión de sorpresa/shock (estilo "MrBeast face").
- **pipeline/thumbnail_maker.py**: `_last_raw_base` guarda la imagen Pollo cruda (antes de composición); badge `_draw_4k_badge` a 128×60, fuente 32.
- **orchestrator.py**: `thumbnail_base_path` usa imagen cruda → fin de la doble-composición entre `phase_video` y `phase_metadata`.
- **DB**: eliminado huérfano id=28; miniatura regenerada para video 26 (verificada: Pollo genera, QC 7.0/10, 141 KB).

### Fase D — Títulos virales + texto de imagen más intrigante
- **pipeline/metadata_generator.py**: prompt sistema reescrito con patrones de alto CTR probados (curiosity gap, emotional arousal, números impares, preguntas retóricas, power words). TEXTO MINIATURA ampliado con formatos de alto impacto (pregunta corta, cifra, palabra-gancho). Regla COHERENCIA TÍTULO↔IMAGEN (trabajan sin repetirse). Char cap de 15 → 24.
- **pipeline/thumbnail_brainstorm.py**: agente marketing actualizado con reglas de texto overlay viral (~24 chars, formatos de intriga, complementariedad con el título).
- **Regenerado**: metadatos + miniatura video 26 con texto viral (servible via API).

### Fase E — Progreso granular en el panel (30+ hitos en vez de 18)
- **orchestrator.py**: añadido `progress_callback` + `_emit_progress()`. Sub-pasos en scrape (fuentes), script (LLM, palabras), TTS (narración, segundos), imágenes, video (renderizado, miniatura Pollo), upload (auth, subida).
- **api/services/generation_service.py**: callback thread-safe (`asyncio.run_coroutine_threadsafe`) para WebSocket desde hilos.
- **frontend**: `GenerationProgressBar` mensaje más visible; `ChannelDetail` barra de progreso + fase + porcentaje en tiempo real.

### Fase F — Borrar mp4 tras subida OK + embed YouTube en panel
- **api/services/generation_service.py**: tras subida exitosa, borra el mp4 local y marca `video_path=""` en DB (ahorra espacio).
- **api/main.py**: `/api/video-file/{id}` comprueba existencia real del archivo; si fue borrado y tiene `yt_video_id` → redirect HTTP 302 a YouTube.
- **frontend/VideoEditor.tsx**: si `yt_video_id` existe → embed YouTube `<iframe>`; fallback `<video>` local; placeholder si ninguno.
