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
