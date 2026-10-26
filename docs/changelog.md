## 2026-10-26 — Reprogramación autoritativa asíncrona, recuperable y acotada

- `POST /api/planning/full-replan/authoritative` devuelve **202** con `run_id`;
  el replan completo corre fuera de la petición HTTP y conserva `_REPLAN_LOCK`.
- Estado persistido en `system_state["authoritative_replan_state"]` con fases,
  progreso, canales completados, lease por PID+start ticks y estados
  `stale`/`orphaned`/`interrupted`.
- Nuevo `GET .../authoritative/status` y `POST .../authoritative/recover`
  (recuperación explícita y acotada; nunca automática).
- La UI hace polling, reanuda el seguimiento tras recargar y muestra progreso
  por fase/canal, incidencias y acción de recuperación.
- `videos.update` usa timeout corto y reintentos solo transitorios; el replan
  comparte un presupuesto remoto con la cobertura diaria. Si YouTube acepta y
  la DB falla, se intenta rollback remoto; cualquier discrepancia restante se
  marca `partial` y se registra como crítica.
- Docs: `docs/adr/ADR-20261026-authoritative-replan-async.md`,
  `docs/runbook-authoritative-replan.md` y contrato actualizado en
  `specs/planificacion-publicos-subidas.md`.
- Tests nuevos: `tests/test_authoritative_replan_async.py`,
  `tests/test_replan_remote_updates.py`.

## 2026-10-25 — P00–P05 engineering closure + controlled canary (opt-in)

- P00–P05 externally approved and closed: `AUTOTUBE_SEMANTIC_VISUAL_SELECTION_ENGINEERING_COMPLETE`.
  Readiness: **READY_FOR_CONTROLLED_CANARY**, not `READY_FOR_DEFAULT`. P06 not started.
- Added optional job scoping (`P05_CANARY_JOB_IDS`) so P04/P05 can be enabled for
  one job in a shared service; the API injects `AUTOTUBE_JOB_ID` into workers.
  Empty allowlist keeps the historic behavior.
- Added `benchmarks/media_semantics/p05_live_canary.py`: real-script,
  few-scene, no-upload legacy-vs-P05 canary with review pack and auto-evaluation.
- Added `config/canary/p05-preview.profile` and the runbook `docs/p05-controlled-canary.md`.
- No default changed: `MEDIA_SELECTOR=legacy`, `P04_SOURCE_ROUTER=0`,
  `P05_INTEGRATED=0`; `FULL_VISUAL` remains globally disabled.
- Closure record: `docs/adr/ADR-20261025-p00-p05-engineering-closure.md`.

## 2026-10-25 — P04 source expansion, fallback intelligence and coherence (opt-in)

- Added `p04-source-router-1`: 13 scene categories, category-specific asset
  ladders, provider budgets and deterministic fallback.
- Added official Wikimedia Commons and NASA Image Library providers with
  provenance/licence capture.
- Images are first-class candidates; deterministic Ken Burns motion plan is
  consumed by the renderer only for P04-sourced stills.
- Added controlled AI image fallback policy/prompt builder, explicit conceptual
  and generic fallback labels.
- Added dHash/pHash/URL/embedding duplicate detection, recent-scene coherence,
  shot/style signals and a unified attribution manifest.
- Defaults unchanged: `MEDIA_SELECTOR=legacy`, `P02_VISUAL_MODE=off`,
  `P04_SOURCE_ROUTER=0`.
- Evidence: `benchmarks/media_semantics/evidence/p04-20261025/`; tests:
  `tests/test_p04_source_fallback.py`.

# Autotube Changelog

## P01-R — reconciliación

- Una sola implementación canónica: `pipeline/media_semantics/`.
- Portados desde la rama supervisada: acción derivada del fragmento final en
  subescenas, query de narración, stripping de `must_avoid`, veto de
  anacronismo antes del ranking, `media_request_id` explícito, replay
  determinista, CLI y fixture de benchmark.
- Eliminada la segunda arquitectura (`pipeline/media_selection`) de `main`.

## [Unreleased]

### Pollo.ai — cuentas: sesión caducada ≠ sin créditos
- El worker distingue `session_expired` (401, renovar cookie) de
  `credits_exhausted` (403 sin créditos) y escribe el estado con el mismo lock
  `pollo_accounts_status.json.lock` que usa el CRM (antes podía pisarlo).
- El selector de cuentas devuelve **siempre** un fallback distinto cuando hay 2
  cuentas, aunque ambas estén marcadas; antes solo probaba la primera y la
  segunda quedaba muerta.
- Las cookies viajan al worker por fichero temporal `0600` (`--cookie-file`) en
  lugar de la línea de comandos (`/proc/<pid>/cmdline`).
- Alertas separadas (`pollo_session_expired` / `pollo_credits_exhausted`) con
  flags por condición y limpieza al recuperarse.
- `SceneImageGenerator` ya acierta la caché `.jpg` (antes solo miraba `.png` y
  regeneraba cada escena, gastando créditos) y la clave incluye el modelo.
- Post-proceso respeta el formato real (PNG/JPEG/WebP) en lugar de escribir
  JPEG dentro de ficheros `.png`.
- Nuevo `scripts/verify_pollo_accounts.py` (probe 0 créditos / `--real` /
  `--system`) y `pollo_image_worker.py probe`.

### P01 — Núcleo de recuperación semántica de media
- Nuevo paquete `pipeline/media_semantics/` con contratos `SceneSpec`,
  `Candidate` y `SelectionDecision`, generación de queries por escena final,
  pool global multi-query/multi-proveedor, ranking textual/semántico local,
  top-K global, threshold, `NO_GOOD_CANDIDATE` y descarga posterior al ranking.
- `MEDIA_SELECTOR=legacy|semantic` y `MEDIA_SELECTOR_SHADOW`; el default sigue
  siendo legacy. Backend de embeddings local con fallback (hashing determinista),
  sin embeddings visuales ni nuevos proveedores.
- Migración v72 (aditiva): columnas de identidad de escena final en
  `video_scenes` y tabla `media_selection_decisions` para provenance completa.
- Replay offline (`ReplayHarness`) y captura de benchmark sin etiquetas
  inventadas. Documentación: `docs/p01-semantic-retrieval.md` y
  `docs/adr/ADR-20261024-semantic-retrieval.md`.

### Experimento de recuperación de alcance — Fases 1, 2 y 3
- **Fase 1 (packaging/CTR):** activados A/B de miniaturas, saneo determinista de
  títulos y composición 64/26 en canal2 y canal4 (antes solo canal3/canal5). El
  worker A/B ahora rota por **todas** las variantes, persiste
  `winner_thumbnail_variant` y respeta `AB_TEST_THUMBNAIL_VARIANTS`; el panel de
  CTR por estrategia baja su umbral a 100 impresiones para ser medible.
- **Fase 3 (retención):** la directiva de retención pasa de texto genérico a
  instrucciones **por fase** usando el `retention_anchor`, la `description` y el
  `scene_pacing` de `SCRIPT_STRUCTURE`; se persiste `phase_directives` en la señal.
- **Fase 2 (search-first):** nuevo `pipeline/topic_seeding.py` que siembra
  consultas desde el autocompletado público de YouTube (0 cuota) a partir de las
  keywords SEO del canal y las persiste en `topic_demand_candidates` (migración
  v65). Los shorts idean sobre consultas reales y el long-form las usa como señal
  de ranking compuesto. Spec: `specs/fase-2-search-first.md`.
- **Ops:** reprogramación autoritativa sin cancelar renders, cupo de drenaje de
  canal5 a 3/día durante el backlog y recolección profunda (reach OK).

### Fase final — Gate de hardcodes operativos
- Parametrizados los últimos mapas/defaults operativos: la migración legacy de
  cuentas usa `LEGACY_CHANNEL_GOOGLE_ACCOUNTS`, `test_config` hereda defaults
  compartidos y el orquestador exige el canal explícito.
- Diagnósticos y scripts auxiliares resuelven rutas desde `config.settings`;
  `diagnose_all_channels.py` conserva selección dinámica desde DB/runtime.
- Añadido `scripts/check_operational_hardcodes.py` y el test de contrato que
  rechazan nuevos slugs/IDs, imports de configs de producción, mapas de cuenta
  o proyecto, defaults de canal y rutas de despliegue. La whitelist requiere
  una anotación explícita para migraciones/fixtures.
- Se mantienen intactos fixtures, documentación histórica y el backfill de
  links de shorts permanentemente desactivado.

### Fix — YouTube quota circuit breaker para schedulers
- Conectado el governor de cuota (10.000 ud/proyecto) al dispatch de subidas long-form y shorts; si supera el 85% marca `quota_exhausted_at` y deja backlog en espera.
- `quotaExceeded` ahora dispara el circuit breaker desde uploads, verificación post-upload, thumbnails, cambios de privacidad/descripción y health checks.
- Reducido ritmo: `videos_per_day=1`, shorts 2/día por canal, `GLOBAL_DAILY_UPLOAD_CAP=12`; `yt-dlp` pineado a `<2026` por compatibilidad con Python 3.10.
- Limpieza operativa: cancelados clips pendientes inválidos/del día tras el incidente y reforzado cleanup de Playwright para no dejar driver Node huérfano.

### Fase 0 — Política efectiva por canal y verificación de visibilidad
- Documentada la línea base de fuentes canónicas, puntos de enforcement y dependencias legacy en `docs/channel-policy-audit-2026-08-31.md`.
- Registrada la decisión arquitectónica de separar strikes históricos, bloqueos activos y ritmo personalizado en `docs/adr/ADR-20260831-channel-policy.md`.
- Estados `private`, `scheduled`, `unknown`, `error` y `LOGIN_REQUIRED` ya no se consideran eliminaciones automáticamente.

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
