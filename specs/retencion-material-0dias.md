# Retención de material de generación — 0 días

> **Estado:** implementado (v64, sep 2026)
> **Módulo central:** `pipeline/media_retention.py`
> **Migración:** `database/db_extended.py::_migrate_v64` (sellos `purged_at` / `media_purged_at`)

## 1. Regla

En cuanto un vídeo o short se **sube correctamente a YouTube**, su material pesado
se borra de disco local. No hay ventana de retención: 0 días.

Se elimina:

| Material | Detalle |
|---|---|
| MP4 final | ruta `videos.video_path` (incluye variantes de reassemble) |
| Audio de narración | MP3 principal |
| Audio CTA | MP3 + `_timestamps.json` + `_subtitles.srt` derivados del CTA |
| Escenas | `video_scenes.image_path` / `audio_path` + sidecars `.pollo.json` |
| Assets para dedup | `video_asset_history.file_path` |
| Short MP4 | `shorts.file_path` (nativo y clip) |
| Assets de short | `short_asset_history.file_path` (clips/imágenes descargados) |

Se **preserva** a propósito:

| Preservado | Motivo |
|---|---|
| Thumbnails (`output/thumbnails/`) | los usa el panel |
| SRT de narración principal (`*_subtitles.srt`) | SEO / subtítulos |
| Timestamps JSON principal (`*_timestamps.json`) | capítulos / análisis |
| `asset_url` en las tablas de historial | dedup cross-video sin el fichero local |

## 2. Señal de "subido correctamente"

La fuente de verdad es el id de YouTube, no el `status`:

- long-form: `videos.yt_video_id` no vacío;
- short: `shorts.youtube_id` no vacío.

Estados asociados reconocidos: `published`, `uploaded`, `uploaded_private`,
`deleted_on_yt`, `unlisted`, `private_quality_issue` (long) y `published` /
`scheduled` (shorts). Un vídeo/short sin id **nunca** se purga.

## 3. Punto de enganche único

Toda ruta de subida llama a `pipeline.media_retention.purge_entity_media(db, kind, id)`:

| Ruta | Archivo |
|---|---|
| generate_and_upload / upload_only | `api/services/full_pipeline_worker.py` |
| Subida programada / legacy | `api/services/generation_service.py::start_upload_job` |
| Generación in-process | `api/services/generation_service.py` (fin de pipeline) |
| **Reassemble** (fuga histórica) | `api/services/generation_service.py::_do_reassembly` |
| Cola de shorts (native/clip) | `api/services/shorts_scheduler.py::_upload_queued_short` |
| Shorts vía API | `api/routers/shorts.py` (3 endpoints) |
| Standalone | `pipeline/shorts_standalone.py` |
| Short programado→público | `api/services/yt_state_reconciler.py` |
| Egress/VPS | `pipeline/youtube_uploader.py` tras `egress_transfer_state="done"` (+ `/cleanup` remoto) |

`purge_entity_media` es **idempotente**: si ya se purgó (`purged_at`/`media_purged_at`),
las segundas llamadas no liberan bytes ni fallan.

## 4. Consistencia de BD

Se **conservan las filas** y se sellan:

- `video_scenes.purged_at` + `image_path=''`, `audio_path=''`
- `video_asset_history.purged_at` + `file_path=''`
- `short_asset_history.purged_at` + `file_path=''`
- `videos.media_purged_at` + `video_path=''`
- `shorts.media_purged_at` + `file_path=''`

Los `asset_url` quedan intactos → el dedup cross-video sigue funcionando sin el
fichero local. No quedan referencias colgantes a rutas borradas.

## 5. Barrido retroactivo y periódico

- **Retroactivo (manual, con revisión):**
  `python3 scripts/cleanup_residuals_batch.py` (dry-run por defecto) →
  revisar manifiesto → `--execute`.
  Modos: `--entity-videos`, `--entity-shorts`, `--orphans`, `--category`,
  `--min-age-hours`, `--canal`, `--json-out`.
- **Periódico (automático):** loop `media_retention` en `api/main.py` (cada 6 h,
  0 cuota). Solo borra archivos **huérfanos o de entidades ya subidas**, nunca
  bloqueados ni de entidades pendientes ni recientes
  (`MEDIA_RETENTION_MIN_AGE_HOURS`, def. 6 h).
  Kill-switch: `MEDIA_RETENTION_SWEEP_ENABLED=false`.
- **Política de cachés/pools** (`ai_cache/pollinations`, `shorts_clips`,
  `images`, `ai_images`): solo se purgan huérfanos / material subido. **Sin tope
  de tamaño ni TTL** (decisión operativa sep 2026).

## 6. Reglas de seguridad (nunca relajadas)

1. Nunca borrar material de una entidad **no** confirmada como subida.
2. Nunca borrar un asset **compartido** que otra entidad pendiente referencia
   (`protected_refs`).
3. Nunca borrar rutas en `media_file_locks` ni de vídeos en `error` recientes
   (< 48 h, reensamblado).
4. Nunca borrar thumbnails ni el SRT/timestamps principal.
5. El barrido solo toca archivos con antigüedad > `min_age_hours`.
6. Los fallos de borrado se **loguean** (nunca `except: pass`) y, en la purga
   post-subida, emiten alerta no fatal `media_purge`.

## 7. Observabilidad

- Log: `media_retention: purged <kind> #<id> (<reason>) — freed X MB, deleted N files`.
- Alerta de disco: `scripts/daily_health_report.py` revisa
  `shutil.disk_usage("/")` y emite `disk_space_low` si libres <
  `DISK_FREE_WARN_GB` (def. 30 GB); incluye el tamaño de `output/`.
- Manifiesto JSON de cada dry-run/execute vía `--json-out`.

## 8. Variables de entorno

| Variable | Def. | Descripción |
|---|---|---|
| `MEDIA_RETENTION_SWEEP_ENABLED` | `true` | activa el barrido periódico |
| `MEDIA_RETENTION_MIN_AGE_HOURS` | `6` | guard de antigüedad del barrido |
| `MEDIA_RETENTION_SWEEP_INTERVAL_S` | `21600` | intervalo del loop (6 h) |
| `DISK_FREE_WARN_GB` | `30` | umbral de alerta de disco |

## 9. Auditoría sep 2026 (evidencia)

- Fugas confirmadas: 28 mp4 residuales de `action='reassemble'`, 13 de
  `generate_only` subidos por rutas sin limpieza, 3 de `upload_only`, y 555
  shorts subidos que conservaban su MP4 (11.7 GB).
- Egress REFUTADO como causa actual (solo `canal6`, sin servicio; todos los
  `egress_transfer_state` en NULL), pero se endureció igualmente.
- Dry-run sobre datos reales: **~191.5 GB** reclamables (único, sin doble
  conteo) = material de entidades subidas + huérfanos.
