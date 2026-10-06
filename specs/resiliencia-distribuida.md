# Resiliencia de generación y ejecución distribuida

Estado: implementado (oct 2026). Cubre la vigilancia de generaciones
encalladas, el relanzamiento de ejecuciones distribuidas fallidas y la
escalada hasta re-encolar el vídeo.

## 1. Detección de estancamiento (heartbeat-independent)

`_check_video_phase_stuck` (`api/services/lifecycle_monitor.py`) decide "no
atascado" por el **heartbeat** del worker, que sigue latiendo aunque un
render/concat distribuido delegado esté encallado (el worker solo espera al
motor). Por eso una fase congelada nunca alertaba.

`_check_generation_progress_freeze` (Check 1.5 de `check_all_health`, corre cada
90 s) mantiene en `system_state['gen_stall_watch']` la firma
`phase|pipeline_phase|progress` de cada long-form `running` y crea la alerta
**`generation_stalled`** (critical) si no cambia en `GENERATION_STALL_MIN`
minutos. Al avanzar (o desaparecer el job) la alerta se resuelve sola.

- `GENERATION_STALL_MIN` (default **25**).
- `GENERATION_STALL_WATCH` (default **true**): kill-switch del chequeo.

La limpieza de huérfanos **no** se reinventa: la hacen los barridos existentes
(`cleanup_orphaned_jobs`, `_kill_orphaned_workers`, `glass_box_recover_orphaned_videos`).

## 2. Relanzamiento de la ejecución distribuida (opción B)

El motor (`superserver/server/distengine.js::_maybeCombine`) **falla el exec
entero en cuanto una sola unidad agota sus reintentos** y deja `result=null`, es
decir no publica unidades parciales. Por tanto, desde autotube no se puede
"rellenar solo lo que falta".

Ante un exec incompleto **o** un estancamiento, `_dist_with_repair`
(`pipeline_dist/orchestrator_dist.py`) **relanza el exec COMPLETO con id nuevo**:
el scheduler lo re-planifica entre los nodos elegibles y las unidades vuelven a
tener intentos, normalmente en otro nodo. Se aplica a:

- `_pre_render_scenes_v2` (render de escenas)
- `_dist_concat_body_batched` (concat por batches)

Si se agotan las rondas, lanza `ScenePlanError("dist_repair_exhausted: …")` y
registra `logger.error` (el sistema de alertas `error_*` lo surface).

- `AUTOTUBE_DIST_REPAIR` (default **true**): kill-switch.
- `AUTOTUBE_DIST_REPAIR_ROUNDS` (default **2**): relanzamientos completos.
- Se mantienen `AUTOTUBE_DIST_{RENDER,CONCAT}_STALL_SEC` para la guardia de
  estancamiento de cada espera.

Eventos obs: `dist_render_repair`, `dist_concat_repair`.

## 3. Escalada: reparar → local → reiniciar

Orden ante fallo de un subproceso distribuido delegado:

1. **Reparar** en la flota (reintentos del motor + relanzamiento completo).
2. **Fallback local** (render/concat local, identidad garantizada).
3. Si el local **también** falla: se propaga el marcador
   `dist_repair_exhausted` y `_auto_retry_if_transient`
   (`api/services/generation_service.py`) **re-encola el job** (tope
   `MAX_RETRY_ATTEMPTS`, default 3) para reiniciar el vídeo.

## 4. Follow-up (pendiente de aprobación): relleno fino por unidad (opción A)

Para reencolar **solo** las unidades fallidas (en vez del exec completo) hace
falta que el motor exponga unidades parciales:

- Añadir `partialOk` a `distengine.js::_maybeCombine`/`_complete`: un exec con
  unidades fallidas termina `done` con `result.partial=true` + lista de
  aceptadas (en vez de `_failExec`).
- Que `autotube-render-scene`/`autotube-concat-batch` devuelvan las aceptadas en
  `combine`.
- Autotube calcula `faltantes = pedidas − aceptadas` y reencola solo esas.

Requiere tocar el **control plane de SuperServer** (repo distinto) y reiniciar
el engine; por eso queda fuera de este cambio hasta validación explícita.
