# Incidente y Recuperación — Autotube (agosto 2026)

> Documento vivo de la crisis de cuota/crecimiento y las acciones de estabilización
> aplicadas. Última actualización: 13 agosto 2026.

## Resumen ejecutivo

El sistema pasó de un régimen estable (~16-20 subidas/día totales, sin problemas de
cuota) a un caos autoalimentado: un pico de **over-generation** (2-4 ago: 26/28/17
long-form + 114/58/126 shorts) agotó la cuota de YouTube, disparó reintentos y
duplicados, y un ajustador automático (**Dynamic VPD**) bajó el ritmo del canal
insignia (canal2 a 1 vídeo/día).

**El problema no era la planificación de long-form** (2-3/canal era sostenible),
sino: la sobreproducción de shorts, el bucle de re-subida, y las subidas que morían
en cada reinicio.

## Causas raíz

| # | Causa | Evidencia | Impacto |
|---|---|---|---|
| 1 | Sobreproducción de shorts (`MAX_DAILY_SHORTS=10/canal`) | `settings.py`; ~40-48 shorts/día | ~75% de la cuota (~64k de 84k ud/día) + spam |
| 2 | Bucle de re-subida infinita | 48 "no video ID"/4d; video 2121 re-despachado 8× | 1.653 ud por reintento |
| 3 | Subidas in-process mueren en reinicios | 77 "Server restarted"/4d | Cuota perdida + duplicados |
| 4 | Duplicados + `deleted_on_yt` | limpieza borró legítimos (commit `dda48d3`) | Vídeos repetidos |
| 5 | Miniaturas nunca verificadas | `thumbnail_verified=0` en 315 vídeos | Re-subidas manuales |
| 6 | Livelock shorts (clips sin source) | 4 slots clip canal3 "publish conflict" + "no source" | Pipeline trabado |
| 7 | Dynamic VPD baja canal2 a 1 | `planning_service.py` | Crecimiento frenado |
| 8 | Cuota mal medida (2 proyectos GCP tratados como 1 pool) | `quota_tracker.py` | Alertas incorrectas |

## YouTube borrando vídeos IA (shorts)

Los **shorts nativos** (voz IA + imagen IA + guion LLM, subidos en masa desde cuentas
nuevas `not_eligible`) son el vector de riesgo: 94 "no aparece en YouTube" en 14 días
(pico de 38 el 2 ago). El **long-form NO se borra**. Los 34 `deleted_on_yt` fueron
nuestro propio bug de limpieza de duplicados, no YouTube.

Riesgo principal: **"mass-produced content"** → rechazo de monetización (YPP
"reused content") y riesgo de ban. Mitigación: bajar volumen de shorts, priorizar
clips (contenido ya aprobado) sobre nativos, y espaciar subidas.

## Cambios aplicados

### Fase 0 — Estabilizar
- **Marathon desactivado** (`MARATHON_ENABLED=False` en `defaults.py` + `config_json`).
- **Dynamic VPD desactivado** (`DYNAMIC_VPD_ENABLED=False`).
- **Backoff de subida mínimo 10 min** (antes el primer fallo daba 0s → re-subida cada 5 min).
- **canal2 restaurado a `videos_per_day=2`**.
- Limpieza de backlog de shorts (114 nativos + 36 clips cancelados).

### Fase 0.2 — Ajuste shorts + tope global
- Shorts a **12-16/día (50/50)**: `MAX_DAILY_SHORTS 10→4`, `MIN 8→3`,
  `SHORTS_NATIVE_RATIO 0.35→0.50`, `SHORTS_CLIPS_PER_LONG 3→1`.
- **Tope global de subidas `GLOBAL_DAILY_UPLOAD_CAP=24`** (long-form + shorts,
  ambos = 1.600 ud de cuota). Gates en `dispatch_due_uploads` y
  `dispatch_next_due_shorts_slot`.
- Fix migración que revertía `shorts_native_per_day` a 3.

### Fase 1 — Causas raíz
- **1.3 Subidas a subproceso** (`upload_scheduler._spawn_upload_worker` +
  worker `--action upload_only` saltando generación). Las subidas sobreviven
  reinicios del API.
- **1.4 Governor de cuota por proyecto** (`get_channel_project` + `should_throttle_global`
  por proyecto). 2 proyectos: `youtube-uploads-automation` (canal2+3) y
  `autotube-expediciones` (canal4+5).
- **1.5 Miniaturas** (`mark_video_uploaded` marca `thumbnail_verified=1`).
- **1.6 Livelock shorts** (clips sin source se cancelan, no se saltan en bucle).
- **upload_only no genera variantes A/B** (ahorra créditos Pollo/LLM en F2).

## Configuración objetivo

| Tipo | Por canal | Total (4 canales) |
|---|---|---|
| Long-form | 2-3/día | 8-12 |
| Shorts (50/50) | 3-4/día | 12-16 |
| **Tope global** | — | **24/día** |

## Commits

- `ae21604`, `7aa1728`, `0b14aae` — Fase 0 (shorts 50/50, marathon off, backoff, migración)
- `3bbd52f` — Fase 0.2 (tope global + clips a 1)
- `446073d` — Fase 1.6 (livelock shorts)
- `d551370` — Fase 1.5 (thumbnails)
- `606deeb` — Fase 1.4 (quota por proyecto)
- `968910f`, `e384cce` — Fase 1.3 (subidas subproceso + sin A/B en F2)

## Pendiente / próximos pasos

- **Fase 2**: presupuesto diario + rampa monitorizada; consolidar ~20 scripts de
  cleanup/recovery que compiten entre sí.
- **Acción del usuario**: confirmar límite real de cuota en GCP Console
  (YouTube Data API v3 → "Queries per day") para ambos proyectos, para validar
  el tope de 24/día.
- **Observación menor**: el worker loguea `Scene saving failed (non-fatal): local
  variable 'json' referenced before assignment` — bug pre-existente no crítico.
