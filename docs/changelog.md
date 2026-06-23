# Autotube Changelog

## [Unreleased]

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
