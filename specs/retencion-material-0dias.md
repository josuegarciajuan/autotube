# Retención de material de generación — 0 días

> **Estado:** implementado (v64, sep 2026) + fix fuga segments (oct 2026)
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
| **Segments de render** | `output/videos/segments/<video_id>/` (scene_*.mp4 + subdirs `_dist_staging*`) — ver §10 |
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
las segundas llamadas no liberan bytes ni fallan. Para vídeos long-form el purge
post-subida borra también el **directorio de segments** del vídeo
(`output/videos/segments/<video_id>`, §10): la retención 0 días se aplica **en la
fuente** y el barrido diario queda como red de seguridad, no como limpiador principal.

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
  `--read-only` añade un dry-run **estricto de cero escrituras**: la BD se abre
  con URI `sqlite mode=ro` (shim `ReadOnlyDB`; ni PRAGMAs WAL/optimize) y la
  purga por entidad se limita a `would_delete`. Incompatible con `--execute`.
  Pensado para inspeccionar producción en vivo desde una worktree con
  `DATABASE_PATH`/`OUTPUT_DIR` apuntando a los datos reales.
- **Periódico (automático):** **systemd timer** `autotube-media-retention.timer`
  (diario 06:30, `RandomizedDelaySec=30min`, `Persistent=true`), unidades
  versionadas en `deploy/autotube-media-retention.{service,timer}`.
  Ejecuta en una pasada, con `flock` anti-solapamiento y prioridad baja
  (`Nice=10`, IO best-effort):
  1. `scripts/cleanup_residuals_batch.py --execute --min-age-hours 24`
     (material de entidades subidas + huérfanos/caché),
  2. `scripts/purge_logs_and_backups.py --execute --days 14`
     (logs rotados + backups de BD viejos; **los archivos trackeados en git se
     omiten**, p. ej. los `autotube.db.bak*` versionados, para no ensuciar el árbol).
  Solo borra archivos **huérfanos o de entidades ya subidas**, nunca bloqueados,
  de entidades pendientes, ni recientes (`--min-age-hours 24`).
  El loop in-app (`_media_retention_loop`) queda **desactivado**
  (`MEDIA_RETENTION_SWEEP_ENABLED=false`) para que la única fuente sea el timer.
  La purga post-subida (retención 0 días) **sí** está activa siempre.
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

## 10. Ciclo de vida de los segments de render (fix oct 2026)

`output/videos/segments/<video_id>/` contiene los `scene_NNNN.mp4` que genera el
render por escenas (local y distribuido; los subdirs `_dist_staging_v2/` son
hardlinks de staging del mismo vídeo). Es el material más pesado de un vídeo
(2–3 GB por long-form) y **no está referenciado por ninguna columna de la BD**,
por lo que el barrido por-file no podía verlo: hasta octubre de 2026 era
INVISIBLE a la retención (fuga confirmada: 24.5 GB, §13).

Reglas nuevas:

1. **En la fuente:** `_purge_video()` (purga post-subida) elimina además
   `segments/<video_id>` completo (archivos → rmdir bottom-up del árbol, sólo
   si queda vacío). Idempotente y lock-safe: nombres bloqueados o referenciados
   por entidades pendientes se conservan y el dir queda para el barrido.
   `video_editor.render()` ya borraba el dir al terminar con éxito; esto cubre
   el caso upload-primera-vez-con-checkpoint-residual y los vacíos que `rmtree`
   no alcanzaba.
2. **En el barrido (red de seguridad):** nueva categoría `segments` en
   `SWEEP_CATEGORIES`, clasificada a **granularidad de directorio**
   (`DIR_SWEEP_CATEGORIES`), clave = nombre del dir (video_id numérico).
   Reglas, evaluadas en orden:
   - nombre **no entero puro** → PRESERVE `non_numeric_dir` (dueño desconocido;
     conservador por diseño).
   - fila `videos` existe y `yt_video_id` no vacío (SUBIDO) → **RECLAIM**
     (`video_uploaded`); con job `running/queued` sobre ese vídeo → PRESERVE
     `video_active_job`; por archivo se respetan igualmente `media_file_locks`,
     `protected_refs` y artefactos preservados (`.srt`/`_timestamps.json`);
     tras borrar archivos se elimina el dir si queda vacío (subdirs de staging
     incluidos: son intermedios del vídeo ya subido).
   - fila existe, NO subido → PRESERVE con reason derivada del estado:
     `video_active_job` (job running/queued o archivos bloqueados),
     `video_error_recent` (error/failed/retrying dentro de la ventana de
     reensamblado de 48 h), `video_error_stale` (error fuera de ventana),
     `video_terminal_dead` (`discarded`/`cancelled`/`validation_failed`) y
     `video_pending` (resto: awaiting_upload/generating/…).
   - **sin fila** (huérfano) → RECLAIM (`orphan_stale`) sólo si el mtime más
     reciente del dir es anterior a `min_age_hours` Y ningún nombre está en
     `protected_refs` Y ningún archivo está bloqueado; si no:
     `orphan_protected` / `orphan_locked` / `orphan_recent`.
   - dirs vacíos: un dir huérfano vacío envejecido o de vídeo subido se
     **elimina** (fin de la basura de dirs residuales).
3. **Invariante de cobertura:** toda subcarpeta de `output/videos` debe ser
   categoría de barrido o estar documentada como nunca-barrida
   (`canal5/canalA/canalB`: dirs legacy vacíos, 0 bytes). El escaneo de
   `videos_root` es no-recursive; si una subcarpeta nueva aparece, NO queda
   invisible silenciosamente: añadir su categoría.

## 11. `stale_pending_entities` — informe de protección muerta (report-only)

Filas NO subidas que mantienen material `referenced_by_pending` para siempre
sin camino real de subida. El barrido **nunca** borra este material ni cambia
estados automáticamente (una fila `error` puede resucitar con reassembly; sólo
un humano puede declararla muerta). El manifiesto incluye:

- candidatas: long-form sin `yt_video_id`, sin job `running/queued` y en estado
  terminal (`discarded`/`cancelled`/`validation_failed`) O `error/failed/retrying`
  fuera de la ventana de 48 h O inactivas > 45 días; shorts sin `youtube_id`,
  sin job y `cancelled/error/failed` o > 45 días.
- por entidad: archivos/bytes que protege (sólo si TODOS sus protectores son
  stale), y tamaño de su `segments/<id>` si existe.
- `reclaimable_if_reconciled_bytes`: lo que liberaría el barrido diario tras
  reconciliar (nunca se borra sin ese paso).

**Cómo reconcilar (runbook operador):**

1. Revisar el informe en `logs/retention_latest.json` →
   `stale_pending_entities.entities`.
2. long-form: `python3 scripts/discard_videos.py --ids <ids>` (con
   `--archive-dir` si se quiere conservar algo) o UPDATE del `status` a un
   estado terminal tras confirmar que no hay reintentos previstos. El script
   exige cero jobs activos y resuelve las alertas abiertas.
3. shorts `cancelled`: nada que reanimar — borrar/purgar la fila de material
   tras confirmación manual.
4. La noche siguiente el timer (`autotube-media-retention.service`) reclaima
   automáticamente: el MP4 pasa de `referenced_by_pending` a huérfano
   (fin de semana de gracia por `min-age-hours 24`), y el dir de segments pasa
   de `video_terminal_dead`/`video_error_stale` a `orphan_*` según la fila que
   quede. No hace falta borrar a mano nada más.

## 12. Adiciones al manifiesto JSON (backward-compatible)

Claves existentes intactas; claves nuevas:

- `orphans_classify.categories.segments`: `dirs_total`, `dirs_reclaim`,
  `files_total`, `files_reclaim`, `bytes_total`, `reclaim_bytes`,
  `protected_bytes`, `reasons` (dirs preservados por reason),
  `reclaim_reasons` (`video_uploaded` / `orphan_stale`) + las legacy
  (`files`, `bytes`, `reclaim_files`, `reclaim_bytes`, `reasons`).
- `orphans_classify.stale_pending_entities` y duplicado top-level
  `stale_pending_entities` (§11).
- `orphans.categories.segments` en modo execute añade `dirs_removed`.
- `purge_entity_media` (report por entidad) puede añadir `dirs_removed` y
  `skipped_protected` con rutas de segments.

## 13. Auditoría oct 2026 (evidencia de la fuga)

- `output/videos/segments`: **24.5 GB / 25 dirs** — invisibles (no estaba en
  `SWEEP_CATEGORIES` y `videos_root` no es recursivo) y nunca purgados por
  `_purge_video()`. Composición: 5 dirs `video_error_stale`, 4
  `video_terminal_dead` (discarded/cancelled), 12 `video_active_job` (2747
  renderizando + colisiones de basename bloqueado), 4 dirs vacíos reclamables
  (3 huérfanos + 1 de vídeo subido, #2157 — subido y `media_purged_at` sellado
  el mismo día, el dir vacío seguía ahí: prueba del agujero de limpieza).
- Protección perpetua por filas muertas: dry-run report-only marca **409 filas
  stale** (174 `error` >48 h, 23 `discarded`, 2 `cancelled`, 5
  `validation_failed`, 1 `ready`, 200 shorts `cancelled`, 4 shorts generados
  >45 días) protegiendo **12.7 GB en archivos** + **11.4 GB en 13 dirs de
  segments** = **24.1 GB `reclaimable_if_reconciled`**.
- Reclaim inmediato en dry-run: 292 MB huérfanos (assets de cola de shorts +
  imágenes) + 0 B de segments (todo el peso pertenece a filas pendientes/stale
  aún no reconciliadas; los 4 dirs vacíos sí se eliminan).
- Prueba de cero escrituras del dry-run: wrapper `sqlite3.connect` guardado —
  7 463 conexiones ro-URI, 1 intento rw (lectura de `ACTIVE_CHANNELS` al
  importar `config/settings.py`, bloqueada antes de abrir y sin uso alguno en
  el pipeline de retención), 0 llamadas de borrado.
