# Autotube — Plan de fases

## Fase A: Duplicación (1 generación = 1 registro en DB) + yt_video_id en registro trackeado ✅ COMPLETADA

**Archivos modificados:**
- `orchestrator.py` — añadir `db_video_id`, condicional `update_video` vs `insert_video`
- `api/services/generation_service.py` — pasar `db_video_id`, llamar `mark_video_uploaded`
- `pipeline/youtube_uploader.py` — suprimir `_log_to_db` en ruta API

**Tareas:**
- [x] A1. `PipelineOrchestrator.__init__` acepta `db_video_id: Optional[int] = None`
- [x] A2. `phase_video`: si `db_video_id` → `update_video`; si no → `insert_video`
- [x] A3. `phase_upload`: mismo patrón; además `uploader.db = None` si API
- [x] A4. `run_full_pipeline` (skip_upload): mismo patrón (preservado CLI-only)
- [x] A5. `generation_service.start_generation_job`: pasa `db_video_id=video_id` al orquestador
- [x] A6. `generation_service`: tras subida OK → `db.mark_video_uploaded(video_id, yt_id, url)`
- [x] A7. Limpieza de registros duplicados existentes en canal 3
- [x] A8. Reiniciar API y verificar

## Fase B: Duración basada en VIDEO_OPTIMAL_DURATION_MINUTES ✅ COMPLETADA

**Archivos modificados:**
- `prompts/canal2_prompts.py` — rama producción deriva duración/palabras/bloques de `VIDEO_OPTIMAL_DURATION_MINUTES`
- `prompts/canal1_prompts.py` — mismo cambio (paridad)

**Tareas:**
- [x] B1. Reemplazar hardcodeo `PROD_SCRIPT_WORDS_*`/`PROD_VIDEO_DURATION_*` por derivación desde `VIDEO_OPTIMAL_DURATION_MINUTES`
- [x] B2. Mantener TEST_MODE como override para pruebas rápidas
- [x] B3. Verificar palabras (~1275-1724 para 10 min), bloques (15-21), duración (10-14)
- [x] B4. DB: TEST_MODE=False en canales 1 y 3
- [x] B5. Verificar sintaxis + prompt generado correctamente

## Fase C: Pollo API oficial + thumbnails virales + badge 4K x2 ✅ COMPLETADA

**Archivos modificados:**
- `tools/pollo_image_worker.py` — copiado verbatim de lamami
- `pipeline/ai_image_generator.py` — wrapper subprocess (invoca worker como publicista)
- `pipeline/thumbnail_style_engine.py` — sin veto de caras
- `pipeline/thumbnail_brainstorm.py` — prompts con cara de sorpresa
- `pipeline/thumbnail_maker.py` — `_last_raw_base` + badge 128×60
- `orchestrator.py` — `thumbnail_base_path` usa imagen cruda

**Tareas:**
- [x] C1. Copiar worker + reescribir ai_image_generator.py (subprocess wrapper)
- [x] C2. Quitar veto de caras + brainstorm con rostro de sorpresa/shock
- [x] C3. Guardar imagen Pollo cruda, fin doble-composición
- [x] C4. Badge 4K a 128×60, fuente 32
- [x] C5. Limpiar huérfano id=28
- [x] C6. Generar miniatura para id=26 + verificar URL pública

## Fase D: Títulos virales

## Fase E: Progreso granular en el panel

## Fase F: Borrar mp4 tras subida OK + embed YouTube en el panel
