# Autotube Changelog

## [Unreleased]

### Fase A — FIX: Duplicación (1 generación = 1 registro) + yt_video_id en registro trackeado
- **orchestrator.py**: añadido `db_video_id` opcional; `phase_video`/`phase_upload` usan `update_video` en vez de `insert_video` cuando viene de API.
- **api/services/generation_service.py**: pasa `db_video_id` al orquestador; guarda `yt_video_id`/`yt_url` en el registro trackeado tras subida exitosa.
- **pipeline/youtube_uploader.py**: suprimido `_log_to_db` en ruta API (`uploader.db = None`).
- **DB**: limpiados registros huérfanos duplicados del canal 3 (ids 27, 29 → consolidados en 26).
