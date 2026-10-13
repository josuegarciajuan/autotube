# Offload de long-form y shorts a la flota SuperServer

Estado: **propuesta de diseño (no implementado)**. Fecha: oct 2026.
Ámbito: repartir por la flota el trabajo pesado de CPU/RAM del pipeline
long-form y de los shorts, reduciendo la carga local del nodo casa
`liveyourdre2` sin romper las invariantes del proyecto.

> Documento de diseño. No describe código a implementar todavía: fija el mapa
> de fases, la frontera de delegación, el contrato de invocación, los invariantes
> de concurrencia y el plan por fases con criterios de reversión y gates.

---

## 0. Contexto verificado

### 0.1 Nodo casa y carga actual

- `liveyourdre2` es el nodo casa del proyecto autotube (`home` en
  `/root/superserver/projects/autotube.json`) y además el plano de control de
  SuperServer. 10 cores. `systemctl` observa ~86–91 % CPU sostenido.
- El **pool de SuperServer no tiene jobs activos** (0 jobs). La carga **no**
  viene del motor distribuido: viene de procesos locales del host lanzados por
  el propio autotube.

Procesos locales observados (a confirmar con muestreo, §3.4):

| Proceso | CPU observada | RAM | Nota |
|---|---|---|---|
| `api/services/full_pipeline_worker.py` ×2 (`--action generate_only`) | ~375 % + ~318 % | ~8 GB | incluye fases locales + ensamblado/fallback de render |
| `api/services/shorts_worker.py` + varias `ffmpeg` | variable | 2–4 GB pico | render de shorts con `libx264` |
| `tradingBot live_meta` | — | — | **NO es autotube**; `primaryOnly`; no se toca |

### 0.2 Distribución ya existente y ACTIVA

`/root/autotube/pipeline_dist/` es un pipeline **aditivo**: no modifica
`orchestrator.py` ni `full_pipeline_worker.py` (salvo `make_orchestrator()`,
`orchestrator.py:3655`). Contiene `orchestrator_dist.py`
(`DistributedOrchestrator`), el puente `dsl_client.py` (escribe en el spool de
SuperServer, sin cookies), `render_plan.py`, `scene_plan.py` y workers puros.

Flags activos en producción
(`/etc/systemd/system/autotube-panel.service.d/10-superserver-dist.conf`):

| Flag | Fase que delega | Definición SuperServer |
|---|---|---|
| `AUTOTUBE_DIST_RENDER_V2=1` | render de escena (video-only) | `autotube-render-scene` |
| `AUTOTUBE_DIST_CONCAT=1` | concat por batches (body) | `autotube-concat-batch` |
| `AUTOTUBE_DIST_IMAGES=1` | imágenes IA (SD 1.5 CPU) | `autotube-ai-image` |
| `AUTOTUBE_DIST_YTDLP=1` | audio/clip YouTube (IP residencial) | `autotube-ytdlp-audio`, `autotube-ytdlp-clip` |
| `AUTOTUBE_DIST_SD_THREADS=2`, `AUTOTUBE_DIST_SD_MAX_INFLIGHT=6` | tuning de imágenes IA | — |
| `AUTOTUBE_DIST_EXCLUDED_NODES=liveyourdre2` | **nuevo**; aparta la casa de las ejecuciones distribuidas | — |

`make_orchestrator()` (`orchestrator.py:3655-3674`) devuelve
`DistributedOrchestrator` si `AUTOTUBE_DIST_RENDER_V2` / `_CONCAT` / `_IMAGES`
están activos, con **fail-open a local** si falla el import.

`settings.effective_max_concurrent_longform_jobs()` (`config/settings.py:267`)
devuelve **2** solo con `AUTOTUBE_DIST_RENDER_V2=1`; si no, **1**.

### 0.3 Manifiesto del proyecto en SuperServer

`/root/superserver/projects/autotube.json`:

- `home: liveyourdre2`, `maxConcurrent: 1`, `adapterStatus: experimental`.
- `primaryOnly: ["upload","publish","oauth","tokens","panel","db","planned_slots"]`.
- `tts_kokoro` (`adapter: autotube`, `spec.kind: "tts"`) → **candidate**.
- `generate_longform` (`kind:"render"`) → **blocked**, con nota: el worker real
  no acepta `--phase` y escribe checkpoints/progreso en `autotube.db`; hay que
  separar persistencia y decidir el contrato. Invariante: 1 long-form a la vez.
- Nota del proyecto: generación en hold; reanudar exige confirmación
  `REANUDAR_GENERACION` (`api/services/generation_hold.py:45`).

El adapter `server/adapters/autotube.js` **solo** implementa `kind:"tts"`
(`prepareTts`). Para `render`/`media`/`script`/`metadata` lanza
`"aún no adaptado"` (`prepare()`), y `forbidden: ['upload','publish','oauth','browser']`.

---

## 1. Objetivo y alcance

### 1.1 Objetivo

Delegar a la flota **tanto como sea posible** del cómputo pesado (CPU/RAM) del
pipeline **long-form** y de **shorts**, dejando en la casa `liveyourdre2` solo
lo que por seguridad debe quedarse (DB, uploads/OAuth/panel/cuotas,
planificación) y el orquestador ligero.

Éxito medible:

1. La carga local de `liveyourdre2` baja de ~86–91 % a un objetivo
   `< 50 %` durante un long-form y un short completos (medido con el mismo
   número de jobs concurrentes).
2. El resultado de cada artefacto delegado (segmento, batch, mp3, imagen,
   mp4) es **funcionalmente equivalente** al local (mismos parámetros/encoding;
   no se exige bit-a-bit en fases estocásticas como SD).
3. Se respetan íntegras las invariantes duras (§1.2).
4. El fallback a local **siempre** existe y no deja estado inconsistente.

### 1.2 Invariantes duras (no negociables)

| ID | Invariante |
|---|---|
| REQ-SEC-1 | `primaryOnly` = `upload`, `publish`, `oauth`, `tokens`, `panel`, `planned_slots` **NUNCA** se delegan. |
| REQ-SEC-2 | `autotube.db` tiene **un único escritor**: el proceso casa. Nunca se monta, copia ni escribe en un nodo. |
| REQ-SEC-3 | Los nodos **no abren puertos**; toda conexión es saliente desde la casa. |
| REQ-SEC-4 | El borrado sigue siendo exclusivo de `bin/taildeck-cleanup` (rutas acotadas). |
| REQ-SEC-5 | No se rompe el flujo de subida ni las cuotas (`quota_tracker`, `planned_slots`). |
| REQ-CONC-1 | Invariante de long-form: máx. `effective_max_concurrent_longform_jobs()` (1 sin render distribuido, 2 con él). |
| REQ-CONC-2 | Explícito: `generate_longform` (job completo) sigue **blocked**; no se delega el worker entero. |
| REQ-CONC-3 | Un slot de render lógico por long-form (`pipeline_phase='render'`) sigue serializando el ensamblado. |
| REQ-OPS-1 | Reanudar generación real exige el operador humano (`REANUDAR_GENERACION`). |
| REQ-REUSE-1 | Reutilizar `pipeline_dist`, `dsl_client`, las definiciones existentes y el motor distribuido; no reinventar. |

---

## 2. Mapa de fases

### 2.1 Long-form

Orden real de fases: `_PHASE_ORDER` (`api/services/full_pipeline_worker.py:803`):

```
scrape → script → pre_validate → tts → media → video → metadata → post_validate → upload
```

| Fase (método) | Qué hace | CPU/RAM | ¿Distribuida hoy? | Escribe DB | Veredicto |
|---|---|---|---|---|---|
| `phase_scrape` | Scrapers externos (Reddit/Wikipedia/News/RSS…), inserta `raw_content` | CPU baja; I/O red | ❌ | ✅ inserta `raw_content` | **local** (estado/DB) |
| `phase_generate_script` | LLM (DeepSeek/OpenAI), produce `guion`/`bloques_json`/`escenas_json`, inserta `scripts` | CPU baja; espera red | ❌ | ✅ inserta `scripts` | **local** (secretos + DB) |
| `phase_pre_validate` | Validación de calidad del guion (texto/LLM) | baja | ❌ | ✅ checkpoint | **local** |
| `phase_tts` | Kokoro torch CPU (narración + CTA) | **alta**, ~3 GB RAM pico | ⚠️ adaptada (adapter `kind:"tts"`, M3) pero condicionada a modo `superserver` + nodos `kokoro` | ✅ checkpoint + lock canal | **delegable** (ya adaptada) |
| `phase_media` | Stock (Unsplash/Pixabay/Pexels/Pollo) + **imágenes IA `local_sd`** (SD 1.5 CPU) + procesado | **muy alta** si cae a SD local (~3 cores, 4.5 GB) | ⚠️ parcial: `_prefetch_ai_images` pre-genera IA en flota; stock sigue local | ✅ checkpoint + locks | **delegable** (IA) / stock local |
| `phase_images` (legacy) | `ImageFetcher` + procesado | media | ❌ | ✅ log | **local** (legacy, no usado) |
| `phase_video` | **Punto caliente**: render por escena (`VideoEditor._render_scene_segment`, ffmpeg/moviepy) + concat con xfade (`_concat_body_batched`) + mux final + thumbnail | **muy alta** (multi-core, GBs en xfade) | ⚠️ parcial: escenas → `autotube-render-scene`; batches → `autotube-concat-batch`; **ensamblado final local** | ✅ `pipeline_phase='post'`, checkpoint | **delegable** (nuevas piezas) |
| `phase_metadata` | LLM + Pollo (título/desc/tags/thumbnail) | baja/media; red | ❌ | ✅ escribe metadata | **local** (secretos + DB) |
| `phase_post_validate` | Validación final | baja | ❌ | ✅ checkpoint | **local** |
| `phase_upload` | Subida a YouTube (Playwright/OAuth) | baja CPU; red | ❌ (prohibido) | ✅ `yt_video_id`, `video_ab_tests` | **primaryOnly** |

**Post-generación dentro de `run_job` (acciones):**

- A/B thumbnails (`ThumbnailMaker` + `ThumbnailBrainstorm`, `_ensure_ab_row`)
  → CPU media + LLM; escribe `videos.thumbnail_path` y `video_ab_tests`.
  **local** (DB).
- `generate_only` → pre-render de **clip shorts** (`pre_render_clip_shorts_for_video`,
  `full_pipeline_worker.py:1940`) → dispara `shorts_worker`/ffmpeg. **local hoy**.

**Qué ffmpeg concreto consume los ~700 % (hipótesis a confirmar en §3.4):**
dos `full_pipeline_worker.py` en `phase_video` ejecutan en la casa:

1. `VideoEditor._render_scene_segment` por escena (encode ffmpeg/moviepy), y/o
2. `VideoEditor._concat_body_batched` (ffmpeg `filter_complex` con xfade),
3. más el **ensamblado final** (mux de audio, grain/vignette, thumbnail) que
   **no** está cubierto por `autotube-concat-batch` (que solo reparte batches
   `> batch_size`).

Si el render distribuido falla o no hay nodo elegible, todo lo anterior cae a
local (fail-open). A esto se suma, cuando aplica, Kokoro TTS local, `local_sd`
local y el render de shorts. **La instrumentación de F0 debe separar estas
fuentes antes de tocar nada.**

### 2.2 Shorts

Entrypoints: `api/services/shorts_worker.py` (subproceso standalone) →
`shorts_scheduler._dispatch_{native,clip,standalone}_short`.

| Sub-fase | Qué hace | CPU/RAM | ¿Distribuida hoy? | DB | Veredicto |
|---|---|---|---|---|---|
| Resolución de fuente | Local o `yt-dlp` (clip de vídeo YouTube) | I/O red | ✅ `autotube-ytdlp-clip` (tag `ytdlp`) | — | **delegada (descarga)** |
| Guion/hook | LLM (títulos/hook) | baja | ❌ | ✅ | **local** |
| TTS | Voz del short | alta si Kokoro | ⚠️ como long-form | ✅ | **delegable** |
| **Render** | `ShortsRenderer._render_with_ffmpeg`: crop 16:9→9:16, `scale=1080:1920:flags=lanczos`, subtítulos quemados, `drawtext` hook, `libx264 preset medium crf 20` (`pipeline/shorts_renderer.py:310-327`) | **alta** | ❌ | ✅ `shorts_planned_slots` | **delegable (nuevo)** |
| Finalización | `_finalize_short_dispatch`: enlaza slot, estados, reintentos, alertas | baja | ❌ | ✅ | **local** (DB) |

`tradingBot live_meta` no pertenece a autotube: **fuera de alcance**.

---

## 3. Acoplamiento con `autotube.db`

### 3.1 Inventario exacto de escrituras (long-form)

En `api/services/full_pipeline_worker.py`:

| Punto | Línea | Efecto DB |
|---|---|---|
| `_load_checkpoint` | 810 | `db.get_video` → lee `checkpoint_data`, `progress_phase` |
| `_save_checkpoint` | 844 | `db.update_video(checkpoint_data=…)` (merge por fase) |
| `_acquire_render_slot` | 602 | SQL directo `db._connect()`: lee/actualiza `generation_jobs.pipeline_phase='render'`; reclama holders muertos/congelados |
| `_progress_to_db` | 1019 | `db.update_video(progress, progress_phase, progress_current, progress_total)` |
| `_global_heartbeat` | 995 | `db.update_heartbeat(job_id)` cada 30 s + ghost guard (`db.get_job`) |
| `_ensure_ab_row` | 416 | INSERT/UPDATE `video_ab_tests` |
| `db.update_job(...)` | 903/967/1435/1513/1748/… | `generation_jobs` (status, error, `pipeline_phase`, `worker_pid`) |
| `db.update_video(status/progress_phase/…)` | múltiple en `run_job` | `videos` en cada fase |
| A/B thumbnail persist | 1841 | `videos.thumbnail_path` |
| upload | 2086 | `video_ab_tests.yt_video_id` |
| `_preflight_cleanup` | 745 | `ExtendedDatabase` → `get_locked_file_paths`, `get_error_video_media_paths` |

`_load_checkpoint`/`_save_checkpoint` usan la columna `videos.checkpoint_data`
(JSON con claves por fase) y `videos.progress_phase` como índice de resume
(`_PHASE_ORDER` / `_PHASE_INDEX`).

### 3.2 Inventario exacto de escrituras (shorts)

En `api/services/shorts_worker.py`:

| Punto | Línea | Efecto DB |
|---|---|---|
| `_init_db` | 74 | `init_db()` + `migrate_v2()` + `ExtendedDatabase()` |
| registro worker | `main` | `db.update_job(status="running", worker_pid, pipeline_phase="shorts")` |
| `_start_heartbeat` | 114 | `db.update_heartbeat(job_id)` + ghost guard `db.get_job` |
| `_dispatch_*_short` | — | `shorts_planned_slots`, `shorts`, `generation_jobs` (via `shorts_scheduler`) |
| `_finalize_short_dispatch` | 3455 | `UPDATE shorts_planned_slots` (status/short_id/error), reintentos, `generation_jobs` |
| `_slot_error_message` | 86 | lee `shorts_planned_slots.error_message` |

### 3.3 Contrato propuesto: "casa única escritora"

**Regla dura (REQ-SEC-2):** ningún nodo recibe ni escribe `autotube.db`.

Modelo por fase delegada:

```
casa (única escritora)                          flota (pura, sin DB)
────────────────────────                        ─────────────────────
1. orquestador congela decisiones y los
   escritos en staging/ (manifest/request)
2. dsl_client.submit(definición, params)
   ────────────────────────────────────────▶   unidad pura:
                                                - sin red (render/SD/TTS)
                                                - DATABASE_PATH=/tmp/…
                                                - escribe result.json
                                                  {ok, sha256, bytes,…}
3. combine() valida cobertura y refs     ◀────  artefactos verificables
4. casa copia artefactos a output/*
5. casa aplica estado a autotube.db
   (_save_checkpoint, update_video,
    pipeline_phase, heartbeat…)
```

Garantías:

- **Idempotencia**: `combine` valida cobertura exacta; artefacto repetido por
  sha256 se descarta. La casa solo aplica el estado **después** de validar el
  artefacto localmente (`Path.exists()` + tamaño/`validate_scene_result`).
- **Sin DB remota**: los workers ya fijan `DATABASE_PATH=/tmp/autotube_worker.db`
  (`pipeline_dist/render_worker.py`); las imágenes SD y TTS no tocan DB.
- **Ruta de retorno**: `dsl_client.accepted_scenes()` / `result.audios|images|clips`
  → `outDir` + `filename` verificables (patrón ya usado en render/concat/imágenes).
- **Fallo**: si el engine no publica unidades o falla, la casa **no** escribe
  estado parcial; hace fallback local (all-or-nothing, patrón ya vigente) o
  reencola el job (`dist_repair_exhausted`, ver `specs/resiliencia-distribuida.md`).

> La ruta TTS actual usa el **pool** de SuperServer (adapter `autotube.js
> kind:"tts"` + `api/services/tts_bridge.py`), no `dsl_client`. Sigue el mismo
> contrato "sin DB, la casa aplica" (opción A de M3). Debe documentarse la
> equivalencia de contrato entre ambas vías (pool vs motor distribuido).

### 3.4 Instrumentación necesaria (F0) para cerrar el diagnóstico

Antes de decidir la frontera, medir **de dónde** salen los ~700 %:

- `ps -eo pid,pcpu,rss,etimes,cmd --sort=-pcpu | head` y `py-spy dump --pid <pid>`
  sobre los dos `full_pipeline_worker`.
- `cat /proc/<pid>/status | grep -i threads` y conteo de hijos `ffmpeg`.
- `db.get_video(video_id).progress_phase` y `generation_jobs.pipeline_phase`
  de los jobs activos → indican si están en `video` (render/fallback),
  `media` (SD local) o `tts`.
- Observabilidad ya existente: eventos `dist_render_repair`,
  `dist_ai_prefetch_fallback_local`, logs de `make_orchestrator` (fallback local).
- Correlacionar con si las ejecuciones distribuidas tienen nodos elegibles
  (`GET /api/distributed/executions`) — la exclusión de la casa debe dejar
  nodos libres (`mail`, `oficina`, `vps649560`, `dedi3133109`).

Salida esperada de F0: informe de atribución CPU por fase y **tasa de fallback
local** de render/concat/imágenes. Solo entonces se elige la secuencia óptima
del plan de §8.

---

## 4. Frontera de delegación

### 4.1 Qué se delega (reutilizando lo existente)

| Pieza | Definición | Notas |
|---|---|---|
| Render de escena video-only | `autotube-render-scene` ✅ | Ya activa; `plan/buildUnit/combine` por escena; `--network=none`. |
| Concat de batches (body) | `autotube-concat-batch` ✅ | Ya activa; ejecuta el MISMO `filter_complex`/encoding que local. |
| Imágenes IA (SD 1.5) | `autotube-ai-image` ✅ | Pre-generación con caché; `local_sd` sirve desde caché. |
| Audio/clip YouTube | `autotube-ytdlp-audio` / `-clip` ✅ | Requiere tag `ytdlp` (IP residencial). |
| **TTS Kokoro** | adapter `autotube.js` `kind:"tts"` ✅ (M3) | Pool, no `dsl_client`; worker puro sin DB. |

### 4.2 Qué se delega (propuesto, nuevo)

| Pieza | Propuesta | Justificación |
|---|---|---|
| **Render de shorts** | Nueva definición `autotube-short-render` | Es el mayor CPU local hoy sin delegar (ffmpeg `libx264` por short). Reutiliza el comando exacto de `ShortsRenderer._render_with_ffmpeg` enviado congelado (crop/scale/subtitles/drawtext/preset) — igual que `autotube-concat-batch` congela el filtro. |
| **Ensamblado final long-form** | Nueva definición `autotube-assemble-video` (o `autotube-concat-batch` extendido) | Cierra el ffmpeg local residual (head/tail + mux audio + grain/vignette). Debe congelarse el `filter_complex`/encoding en la casa. Requiere subir el body silencioso + audio (pesado) → evaluar coste/beneficio frente a local. |
| Media stock (opcional) | Nueva definición con red (tag tipo `stock`/`ytdlp`) | Solo si F0 demuestra que domina CPU; añade egress a nodos y no hay hoy tag de stock. Prioridad baja. |

### 4.3 Qué se queda local (siempre)

- **`autotube.db` y todo el estado**: checkpoints, `progress_phase`,
  `pipeline_phase`, heartbeats, `video_ab_tests`, `planned_slots`, `shorts_planned_slots`.
- **Upload/publish/OAuth/tokens/panel/cuotas**: `primaryOnly`.
- **`scrape`** (inserta `raw_content`), **`script`**, **`metadata`**, A/B
  thumbnails: LLM + secretos + DB.
- **El orquestador** (`DistributedOrchestrator`) y el `full_pipeline_worker`:
  siguen siendo el proceso casa que decide, congela planes, aplica estado y
  hace fallback.

### 4.4 Sobre extender el adapter `autotube.js` `kind:"render"`

- **No** adaptar `generate_longform` (job completo) al adapter: REQ-CONC-2 lo
  mantiene `blocked`. El adapter recibe un `job` del pool y no tiene el modelo
  `plan/buildUnit/combine`; forzarlo acoplaría la DB al nodo.
- Las piezas nuevas se expresan como **definiciones del motor distribuido**
  (`server/definitions/`), invocadas por `dsl_client`, no como `kind` del
  adapter. Esto ya es la dirección de `pipeline_dist` y respeta `spec/20`/`22`.
- Alternativa a evaluar (más trabajo): un adapter `kind:"render"` que envuelva
  una definición; **no** es necesario para el objetivo y se descarta salvo que el
  humano quiera unificar la vía pool/dist.

---

## 5. Contrato de invocación hacia SuperServer

### 5.1 Puente y ciclo

`pipeline_dist/dsl_client.py` es el contrato vigente (sin cookies):

```
submit(definition, params, exec_id=..., label=..., max_inflight=...,
       max_attempts=..., deadline_sec=..., max_units=..., req=..., excluded_nodes=...)
status(exec_id) / wait(exec_id, timeout, poll, progress_cb) / cancel(exec_id)
```

Escribe `spool/<eid>.json`; el engine publica `results/<eid>.json`; los
artefactos quedan en `artifacts/` y `outDir`.

### 5.2 Definición `plan/buildUnit/combine` (spec/20 §4)

- **`plan`**: unidades disjuntas, una por pieza (escena / batch / short /
  imagen). Semillas deterministas por unidad (`sha1(video:idx:script)`), nunca
  por nodo/intento.
- **`buildUnit`**: script sh que empuja el staging validado (`push`) y ejecuta
  el worker thin en el contenedor; escribe `out/result.json` (`{ok, sha256,…}`).
  El solicitante **no** aporta comandos ni rutas libres.
- **`combine`**: puro, corre en el control plane; valida cobertura 1:1 e
  idempotencia; devuelve refs verificables.

### 5.3 Params, `req`, tags y afinidad

Plantilla de payload (adaptada de las definiciones existentes):

```jsonc
{
  "definition": "autotube-<pieza>",
  "params": {
    "stagingDir": "/var/lib/autotube/dist_staging/<run>",
    "image": "autotube-worker:1",
    "worker": "pipeline_dist/<worker>.py",
    "threads": 2,
    "memMb": 2048,
    "requiresTags": ["render"]
  },
  "req": { "cores": 2, "memMb": 2048 },   // reserva POR UNIDAD (motor)
  "excludedNodes": ["liveyourdre2"],       // afinidad: casa fuera
  "maxPerNode": 1,                          // opcional (spec/26)
  "deadlineSec": 4200,
  "maxAttempts": 3,
  "maxInflight": 8
}
```

- `requiresTags`: `render` (escenas/concat/ensamblado), `local-sd` (imágenes IA),
  `kokoro` (TTS), `ytdlp` (descargas), y **nuevo** `short-render` para shorts.
- `req {cores, memMb}` = reserva **local al nodo por unidad**; se suma a las
  puertas actuales del scheduler (headroom, carga, tags, presupuesto).
- **Afinidad** (`spec/26`): la casa ya envía `excludedNodes` =
  `AUTOTUBE_DIST_EXCLUDED_NODES` (`_dist_excluded_nodes()`,
  `orchestrator_dist.py:83`). Validación fail-closed: si la exclusión deja la
  ejecución sin nodos, el motor rechaza la petición → la casa hace fallback
  local. Opcional `maxPerNode`/`allowedNodes` para acotar concurrencia por nodo.
- **Enclave**: render/SD/TTS van `--network=none`; descargas (`ytdlp`) sí
  necesitan red y por eso usan tags/nodos residenciales.

### 5.4 Encaje de las fases actuales en el modelo

| Fase | `plan` produce | `combine` valida |
|---|---|---|
| render escena (`autotube-render-scene`) | 1 unidad/escena | cobertura 1:1, `sha256`+frames |
| concat (`autotube-concat-batch`) | 1 unidad/batch | cobertura 1:1, `sha256`+bytes |
| imágenes (`autotube-ai-image`) | 1 unidad/escena IA-tier | cobertura, `sha256`+dims |
| ytdlp | 1 unidad/URL o clip | cobertura, `sha256` |
| **TTS** (pool adapter) | 1 job/petición | artefactos mp3+srt+timestamps+cta |
| **short-render** (nuevo) | 1 unidad/short | cobertura, `sha256`+dims/duración |
| **assemble-video** (nuevo, opcional) | 1 unidad/ensamblado | cobertura, `sha256` |

---

## 6. Concurrencia e invariantes

### 6.1 Slots y render-gate

- El **slot de render** (`_acquire_render_slot`, `pipeline_phase='render'`) se
  mantiene: serializa el ensamblado de long-forms en la casa. Con render
  distribuido, el worker espera al engine mientras retiene el slot lógico;
  el engine añade su propio **fencing por generación** (aceptación única por
  unidad) — no hay doble render.
- `make_orchestrator` + `_dist_with_repair` mantienen el patrón
  **reparar → fallback local → reencolar** (bounded por `MAX_RETRY_ATTEMPTS`).

### 6.2 `maxConcurrent` y `effective_max_concurrent_longform_jobs()`

- Hoy: 1 por defecto; **2** solo con `AUTOTUBE_DIST_RENDER_V2=1`
  (`config/settings.py:267`). El manifiesto declara `maxConcurrent: 1`.
- Propuesta: subir el límite efectivo a N (>1) **solo** cuando estén delegadas
  y verificadas todas las fases pesadas (render + concat + TTS + imágenes +
  ensamblado). Mientras quede una fase pesada local, N>1 vuelve a saturar la
  casa.
- El hold crea **tantos centinelas como el límite efectivo**
  (`generation_hold.desired_hold_counts()`), así que subir el límite cambia el
  número de sentinelas al liberar/re-asegurar el hold — coordinar con el
  operador.

### 6.3 Sin nodo elegible

- Comportamiento actual y deseado: **fail-open a local**. El motor rechaza la
  petición si la afinidad deja la ejecución sin nodos; `orchestrator_dist`
  captura y cae a local (render/concat/imágenes). Se mantiene.
- Riesgo: si **todos** los nodos están ocupados, la casa vuelve a asumir el
  cómputo → repunte de CPU. Mitigación: medir capacidad de la flota (F0), no
  subir `maxConcurrent` sin margen, y usar `maxPerNode` para repartir.

### 6.4 Gates de carga locales existentes

- `shorts_scheduler._autotube_render_process_count()` +
  `should_defer_shorts_for_longform_load()` (`shorts_scheduler.py:128-175`)
  difieren shorts cuando la carga de render propia es alta. Debe seguir
  funcionando tras delegar: si el render sale de la casa, el conteo local baja
  y los shorts podrán despacharse (o también delegarse, §4.2).

---

## 7. Contrato de estado y seguridad (resumen)

- La flota devuelve **artefactos + JSON de resultado**; la casa aplica estado
  a `autotube.db` (§3.3).
- No se envían secretos ni `autotube.db`; render/SD/TTS sin red.
- `bin/taildeck-cleanup` sigue siendo el único borrador; los artefactos
  distribuidos (`/var/lib/taildeck/distributed/artifacts/`, returns) deben
  quedar cubiertos por su política de purga (ver §10, pregunta abierta 3).
- Los nodos solo usan egress saliente.

---

## 8. Plan por fases incrementales

Cada fase: valor CPU esperado, riesgo, criterio de reversión y gates.
**Ninguna fase toca generación real sin `REANUDAR_GENERACION` y ventana
acordada.**

### F0 — Diagnóstico y baseline (sin cambio de comportamiento)

- **Valor**: atribuir los ~700 % (render/fallback vs SD local vs TTS vs shorts)
  y medir la tasa de fallback local. Habilita el resto del plan.
- **Riesgo**: nulo (solo observación; no cambia flags).
- **Tareas**: muestreo `ps`/`py-spy`, correlación con `progress_phase`, lectura
  de `GET /api/distributed/executions`, informe.
- **Reversión**: ninguna.
- **Gates**: informe con atribución CPU < ±10 %; sin tocar producción.

### F1 — Consolidar la frontera actual (render/concat/imágenes + exclusión casa)

- **Valor**: garantiza que render/concat/imágenes **nunca** corren en la casa y
  que la exclusión `AUTOTUBE_DIST_EXCLUDED_NODES=liveyourdre2` funciona.
  Libera CPU local si hoy cae a fallback por no excluir la casa.
- **Riesgo**: bajo; exclusión mal configurada dejaría la ejecución sin nodos
  (fail-closed → fallback local, visible).
- **Reversión**: quitar la línea `AUTOTUBE_DIST_EXCLUDED_NODES` +
  `daemon-reload` + restart; o `AUTOTUBE_DIST_RENDER_V2=0`.
- **Gates**: `npm run distributed:test`, `npm run affinity:test`,
  `npm run autotube:test`; smoke `scripts/run_distributed_video.py --canal canal2
  --mode quarter`; verificar en el panel que la casa no recibe unidades.

### F2 — TTS garantizado en la flota

- **Valor**: libera ~3 GB RAM y CPU de Kokoro en la casa.
- **Riesgo**: medio (calidad/bitrate; M5 ya documentó 64 kbps libsndfile vs
  192 kbps pydub). Si no hay nodos `kokoro`, cae a local.
- **Reversión**: modo de proyecto a `local` (`/var/lib/taildeck/projects/autotube.mode`)
  → `phase_tts` vuelve a la ruta local intacta.
- **Gates**: `pytest tests/test_tts_worker.py tests/test_tts_bridge.py
  tests/test_tts_fallback.py`; `npm run autotube:test`; smoke con un request real.

### F3 — Render de shorts en la flota (nuevo `autotube-short-render`)

- **Valor**: **alto**; elimina el ffmpeg de shorts de la casa (hoy en
  `shorts_worker`/`in-process`).
- **Riesgo**: medio-alto (identidad del render: mismo ffmpeg/filtros/preset;
  toolchain del contenedor vs host). Fallback local obligatorio.
- **Tareas**: congelar el comando de `ShortsRenderer._render_with_ffmpeg`; nueva
  definición + worker thin; enganche **detrás de un flag nuevo**
  (`AUTOTUBE_DIST_SHORTS=0` por defecto) con fail-open.
- **Reversión**: `AUTOTUBE_DIST_SHORTS=0` (+ restart).
- **Gates**: `pytest tests/test_shorts_renderer_regression.py
  tests/test_shorts_subprocess_worker.py tests/test_shorts.py`;
  `npm run distributed:test`; A/B local vs flota de un short (comparar
  resolución/duración/subtítulos/hook y sha256).

### F4 — Ensamblado final long-form (nuevo `autotube-assemble-video`)

- **Valor**: medio (ffmpeg residual de la casa). Sujeto a F0: solo si domina CPU.
- **Riesgo**: medio-alto (subir body + audio grandes; identidad del mux).
- **Reversión**: flag nuevo `AUTOTUBE_DIST_ASSEMBLE=0` (default) + restart.
- **Gates**: `pytest tests/test_dist_concat_worker.py
  tests/test_full_pipeline_worker_regressions.py`; comparativa de sha256 del mp4
  (o equivalencia funcional si hay no-determinismo de mux).

### F5 — Media stock / LLM (opcional, requiere decisión humana)

- **Valor**: incierto; **riesgo alto** (egress con nodos y posibles secretos).
- **Decisión**: por defecto **NO delegar** LLM/secretos; media stock solo si F0
  lo justifica. Queda como fase condicional.

### Orden recomendado y paralelismo

```
F0 ──▶ F1 ──▶ F2 ──▶ F3 ──▶ F4 ──▶ (F5 condicional)
```

- F0 y F1 pueden solaparse (F1 solo configura/valida).
- F3 (shorts) es independiente de F2 (TTS); pueden ir en paralelo si hay
  capacidad de validación, pero **no** a la vez que se toca el hold.

---

## 9. Rollback y kill-switches

| Cambio | Kill-switch | Efecto |
|---|---|---|
| Render de escena | `AUTOTUBE_DIST_RENDER_V2=0` | `make_orchestrator` → orquestador local; `effective_max_…` → 1 |
| Concat | `AUTOTUBE_DIST_CONCAT=0` | `_install_dist_concat` no se activa |
| Imágenes IA | `AUTOTUBE_DIST_IMAGES=0` | `_prefetch_ai_images` no-op; SD local |
| yt-dlp | `AUTOTUBE_DIST_YTDLP=0` | `download_audio_dist`/`clip_dist` → None → ruta local/egress |
| Exclusión de la casa | borrar `AUTOTUBE_DIST_EXCLUDED_NODES` + `daemon-reload` | la casa vuelve a ser elegible |
| TTS | modo proyecto `local` (`autotube.mode`) | `phase_tts` ruta local intacta |
| Shorts (F3) | `AUTOTUBE_DIST_SHORTS=0` (nuevo, default off) | render local |
| Ensamblado (F4) | `AUTOTUBE_DIST_ASSEMBLE=0` (nuevo, default off) | ensamblado local |

Reglas operativas de rollback:

1. Todo flag nuevo arranca **apagado** (opt-in) y con **fail-open a local**.
2. Cambios de drop-in exigen `systemctl daemon-reload` + restart del servicio.
3. **`REANUDAR_GENERACION`** es obligatorio antes de tocar generación real: el
   hold se libera solo con confirmación explícita
   (`generation_hold.release_hold(confirm=...)`). Ninguna fase del plan debe
   alterar el hold ni crear/borrar sentinelas.
4. Revertir una fase no debe requerir tocar `autotube.db` a mano.
5. Si una fase degrada la carga local, se apaga el flag de esa fase y se
   conserva el resto.

---

## 10. Preguntas abiertas / decisiones pendientes

1. **Atribución de los ~700 % (F0).** ¿Son render local por *fallback* tras no
   haber nodo elegible, ensamblado final local, `local_sd` local, Kokoro local,
   shorts, o una combinación? Sin este dato, el orden de F2–F4 es hipotético.
2. **Ensamblado final (F4): ¿delegar o dejar local?** Subir el body silencioso +
   audio a la flota puede costar más en I/O que el CPU que ahorra. ¿Se acota a
   vídeos con `batch_size` alto o se deja local?
3. **Purga de artefactos distribuidos.** ¿`taildeck-cleanup` cubre ya
   `/var/lib/taildeck/distributed/artifacts/` y los `returns/`? ¿Qué retención?
   (Deuda: evitar la fuga de disco que ya se corrigió en `concat` con
   `TemporaryDirectory`.)
4. **Identidad del render de shorts (F3).** ¿Se garantiza mismo `ffmpeg`/preset
   /filtros que el host? ¿Basta equivalencia funcional o se exige sha256? (SD ya
   aceptó "no bit-a-bit"; shorts quizá sí exija determinismo.)
5. **TTS: bitrate/calidad.** ¿Es aceptable el MP3 64 kbps de libsndfile de M5, o
   se corrige la exportación antes de generalizar la delegación de TTS?
6. **¿La exclusión de la casa aplica al pool (TTS) o solo al motor
   distribuido?** `excludedNodes` es del motor (`spec/26`); la vía TTS usa el
   **pool** (scheduler/contribución), así que **podría** seguir cayendo en la
   casa. ¿Se deshabilita la casa para TTS por presupuesto/tag, o se migra TTS a
   una definición del motor?
7. **`maxConcurrent` objetivo.** ¿2, 3? Depende del presupuesto real de la flota
   y de que todas las fases pesadas estén delegadas. Cambia el nº de centinelas
   del hold al liberar.
8. **Alcance de F5 (LLM/media stock).** ¿Se delegan alguna vez llamadas LLM o
   fetch de stock, o se mantienen 100 % en la casa por secretos? Propuesta por
   defecto: **no**.
9. **Nueva tag de nodo.** ¿Se introduce `short-render` (F3) o se reutiliza
   `render`/`ffmpeg`? Reutilizar reduce provisión, pero mezcla toolchains.
10. **Ventana operativa.** El manifiesto dice generación en hold. ¿Cuándo se
    acuerda la ventana para `REANUDAR_GENERACION` y qué job de smoke se usa?

---

## 11. Trazabilidad

| Requisito | Diseño | Fase |
|---|---|---|
| REQ-SEC-1 (primaryOnly) | §4.3, §7 — nunca en frontera | Todas |
| REQ-SEC-2 (un escritor DB) | §3.3 contrato casa única escritora | F0–F5 |
| REQ-SEC-3 (sin puertos) | §5.3 `--network=none`, egress saliente | F1–F4 |
| REQ-SEC-4 (borrado) | §7, §10.3 | F3–F4 |
| REQ-SEC-5 (uploads/cuotas) | §4.3 local | Todas |
| REQ-CONC-1 (límite long-form) | §6.2 | F1–F4 |
| REQ-CONC-2 (`generate_longform` blocked) | §4.4 | Todas |
| REQ-CONC-3 (render slot) | §6.1 | F1–F4 |
| REQ-OPS-1 (`REANUDAR_GENERACION`) | §9.3 | F2–F5 |
| REQ-REUSE-1 (reutilizar) | §4.1, §5 | F1–F4 |

---

## Anexo A — Comandos de verificación

Autotube (en el worktree/producción, `pytest.ini` limita a `tests/`):

```sh
pytest tests/test_dist_scene_plan.py tests/test_dist_concat_worker.py \
       tests/test_dist_worker.py tests/test_dist_resilience.py
pytest tests/test_tts_worker.py tests/test_tts_bridge.py tests/test_tts_fallback.py
pytest tests/test_shorts_renderer_regression.py tests/test_shorts_subprocess_worker.py
pytest tests/test_full_pipeline_worker_regressions.py
python3 scripts/run_distributed_video.py --canal canal2 --mode quarter   # smoke (copia de DB)
python3 test_video.py --canal canal2 --skip-scrape --quick
```

SuperServer (`/root/superserver`):

```sh
npm run autotube:test        # adapter/definiciones autotube
npm run distributed:test     # motor distribuido
npm run affinity:test        # excludedNodes/allowedNodes/targetNode/maxPerNode
npm run projects:test        # validez del manifiesto
npm run distributed:bench    # local vs 1 nodo vs distribuido
ss-execute submit --definition autotube-render-scene --params '<json>' --id prueba --wait
curl -s localhost:8110/superserver/api/distributed/executions   # tras el gate
```

## Anexo B — Archivos y flags de referencia

- Carga y fases: `api/services/full_pipeline_worker.py` (`_PHASE_ORDER:803`,
  `_load_checkpoint:810`, `_save_checkpoint:844`, `_acquire_render_slot:602`,
  `_progress_to_db:1019`); `api/services/shorts_worker.py`;
  `api/services/shorts_scheduler.py` (`_render_with_ffmpeg` → `pipeline/shorts_renderer.py:310`).
- Distribución: `orchestrator.py:3655` (`make_orchestrator`),
  `pipeline_dist/orchestrator_dist.py` (`_dist_excluded_nodes:83`,
  `phase_video:114`, `_prefetch_ai_images:156`, `_install_dist_concat`),
  `pipeline_dist/dsl_client.py`.
- SuperServer: `server/definitions/autotube-{render-scene,concat-batch,ai-image,ytdlp-audio,ytdlp-clip}.js`,
  `server/adapters/autotube.js`, `projects/autotube.json`, `spec/20`, `spec/22`, `spec/26`.
- Flags: drop-in `/etc/systemd/system/autotube-panel.service.d/10-superserver-dist.conf`;
  `config/settings.py:267`; `api/services/generation_hold.py`.
