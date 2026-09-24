# Experimento: recuperación de alcance y ritmo de crecimiento

> **Estado:** en curso · **Inicio (instrumentación):** ver `system_state["experiment_started_at"]`
> **Ámbito:** los 4 canales activos (canal2, canal3, canal4, canal5)
> **Restricción dura:** 100 % automatizado — sin voz humana, sin persona en cámara,
> sin intervención editorial manual.

Este documento fija, sin ambigüedad, **qué conducta del pasado causó la caída**,
**qué se cambia ahora**, **cómo se mide** y **qué decidiremos en cada checkpoint**.
Sin este contrato, el experimento no es auditable.

---

## 1. Hipótesis

> Si sustituimos la producción en serie de contenido plantilla/repetido por contenido
> con **demanda real, novedad semántica y estructura variable** —publicado a un ritmo
> menor y con long-form que retiene— el **alcance por vídeo y la distribución del
> long-form se recuperan**, y **cesan los avisos de política de YouTube**.

Existe una restricción reconocida: no podemos aportar autoría humana directa. La única
vía legítima es que la automatización produzca **contenido sustancialmente original y
valioso por vídeo** + **divulgación de IA**. No se simula humanidad: eso sería engañoso
y agravaría el problema.

## 2. Evidencia que motiva el experimento (medida en el baseline)

| Señal | Valor |
|---|---|
| Vistas long-form (media/vídeo) | canal2 3 · canal3 43 · canal4 7 · canal5 11 |
| Reparto de vistas long-form vs shorts | 97-99,8 % shorts |
| Alcance Shorts a 7 días (cohorte semana) | canal2 733→29 · canal3 1138→44 · canal5 727→147 |
| Retención long-form | 15-29 % |
| Repetición | canal5 "El hombre que no podía morir" ×7; "…dormir" ×6; canal3 "Secretos…" ×5 |
| Strikes | canal4 (22/08), canal5 (25 y 29/08) |
| Vídeos `unavailable`/`removed` | canal4 11+3 · canal3 5+1 |

Fuentes normativas: [YouTube channel monetization policies](https://support.google.com/youtube/answer/1311392)
(contenido inauténtico, genérico/repetitivo, personas IA en temas sensibles) y
[advertiser-friendly guidelines](https://support.google.com/youtube/answer/6162278).

## 3. Antes vs Ahora (contrato de conducta)

| Dimensión | **ANTES** (conducta del pasado → causa de la caída) | **AHORA** (experimento) |
|---|---|---|
| Selección de tema | Reutilización; mismo tema varias veces con título cosmético | Demanda real (autocomplete/tendencias) + novelty semántico + veto entre canales |
| Dedup | Solo coincidencia exacta normalizada | Similitud semántica (embeddings del proveedor IA) con umbral configurable |
| Guion | Plantilla con esquema fijo | Arquetipo narrativo variable + tesis/ángulo propio por vídeo |
| Validación de guion | No había gate de similitud | Rechazo automático si se parece demasiado al histórico; se regenera el tema |
| Volumen | Ráfagas de 70-90 shorts/semana | Híbrido: canal5/canal2 reducidos; canal3/canal4 moderados |
| Long-form | 0-43 vistas, duraciones de hasta 70 min | 8-12 min, hook + arcos, objetivo retención >40 % |
| Visuales | Stock/IA genérico en bucle | Assets programáticos propios (mapas, timelines, gráficos) + variedad |
| canal5 | Tono "AI doctor" con temas médicos | Documental con fuentes; sin diagnóstico, consejo ni claim sanitario |
| Divulgación IA | Parcial | En todos los vídeos ("contenido alterado/sintético") |
| Catálogo | Duplicados publicados | Auditoría; peores casos → `unlisted` (sin borrado masivo) |

## 4. Qué NO cambia
- Automatización total del pipeline.
- Los nichos (canal5 sigue médico, con filtrado de temas y framing).
- Los invariantes del sistema: 1 long-form simultáneo, assets únicos, no borrar `output/thumbnails/`.

## 5. Métricas y umbrales

**Primarias**
| Métrica | Baseline | Éxito a T+45 |
|---|---|---|
| Avisos de política activos | 0 | 0 |
| Distribución long-form (browse/suggested) | ≈0 | > 0 y creciente |
| Retención long-form | 15-29 % | > 40 % |
| Alcance Shorts a 7 d | 29-320 | > 300 |
| Watch-hours/día (long-form) | ~0 | tendencia positiva sostenida |

**Secundarias**
- Subs **netos** por canal (no brutos).
- Ratio de temas rechazados por dedup/novedad (indicador de que el gate trabaja).
- Porcentaje de long-forms con >100 vistas.
- CTR y retención por formato.

## 6. Reglas de decisión (objetivas)

En cada checkpoint se aplica esta matriz, sin interpretación:

| Condición | Decisión |
|---|---|
| Avisos = 0 **y** alcance 7 d > baseline **y** retención long-form sube | **Continuar** (escalar lo que funcione) |
| Avisos = 0 pero alcance/retención planos | **Refinar** (temas, hooks, packaging) sin cambiar el marco |
| Aparece ≥1 aviso de política | **Revertir el cambio señalado** + priorizar cumplimiento |
| Terminación/suspensión | Activar plan de apelación (21 días) y pausa de publicación |

## 7. Checkpoints

Programados como `scheduled_reminders` (se emiten solos como alerta de sistema vía el
loop `reminders` de `api/main.py`). Fechas relativas al inicio del experimento:

| Checkpoint | Día | Qué se revisa |
|---|---|---|
| 1 | **T+7** | ¿Bajan/suben avisos? ¿Alcance 7 d del formato nuevo > baseline? ¿Algún long-form con distribución? |
| 2 | **T+21** | Análisis principal: retención, subs netos, watch-hours; refinar tema/guion/packaging |
| 3 | **T+45** | Decisión de rumbo: escalar lo que funcione o reajustar la ruta a YPP |

Cada alerta incluye en su mensaje las métricas a mirar y los umbrales, para que el
análisis no dependa de la memoria.

## 8. Rollback
Cada subsistema tiene kill-switch sin despliegue:
- `TOPIC_DEDUP_ENABLED` / umbrales (config del canal).
- `content_safety_disabled` (`system_state`).
- Caps de cadencia (planning / perfil de pacing).
- Reversión de código vía merge de reversión (nunca `reset --hard`).

## 9. Instrumentación
- `scripts/experiment_tracker.py`:
  - `baseline` → sella `experiment_started_at` + `experiment_baseline` (JSON) en `system_state`.
  - `schedule --start <YYYY-MM-DD>` → crea los recordatorios de checkpoint (idempotente).
  - `status` → imprime baseline y recordatorios.
- `scripts/audit_catalog_duplicates.py` → reporte (solo lectura) de near-duplicados,
  plantillas y disclosure faltante.

### 9.1 Embudo de alcance (impresiones → CTR → retención) — v51 (sep 2026)
La **Analytics API no expone impresiones orgánicas ni su CTR**: la métrica
`impressions` no existe (se renombró a `adImpressions`, impresiones de anuncios) y
pedirla devolvía 400, invalidando además la consulta bulk de retención/subs. La
fuente correcta es el **YouTube Reporting API** (bulk, cuota propia):

- `pipeline/youtube_reach.py` → reach reports `channel_reach_basic_a1`
  (`video_thumbnail_impressions`, `video_thumbnail_impressions_ctr`) y
  `channel_basic_a3` (retención/watch/subs).
- `scripts/collect_reach_reports.py` → recolección **manual** (respeta
  `STATS_AUTO_COLLECT=False`); también se dispara en la recolección profunda
  ("Recolectar stats" con `deep=true`).
- Persistencia: tabla `video_reach_daily` (migración v60).
- Endpoint: `GET /api/channels/{id}/analytics/funnel?days=30`.
- Panel: signos vitales **Impresiones** y **CTR** en el Dashboard + panel
  "Embudo de alcance (Nd)" en ChannelDetail.

**Latencia esperada (importante):** el Reporting API **no** sirve datos al
instante. Tras crear el job, el primer informe tarda **hasta 48 h** (el del día de
creación) y en esos primeros días publica también el **backfill de los 30 días
previos**. Por tanto, tras activar la API y recolectar, `video_reach_daily` puede
estar vacía legítimamente durante ~2 días — NO es un fallo de parseo ni un cero
real. Estados explícitos que emite `ReachReportClient.sync` / `result["reach"]`:
`disabled` (API no habilitada en el proyecto GCP → alerta accionable
`reach_reporting_api_disabled`), `awaiting_reports` (jobs creados, informes aún no
publicados), `no_jobs`, `no_auth`, `collected`. El panel se apoya además en
`has_reach_data` / `reach_status` (`pending` vs `reporting_api`) para no leer un 0
como dato real. La primera sincronización usa `REACH_REPORTS_MAX_PER_JOB=0` =
**backfill completo** (tope duro 90 informes/job) para bajar los 30 días
históricos en una sola pasada.

Métricas secundarias expuestas en `GET /api/analytics/experiment`:
`rejected.topic_dedup_rejected`, `rejected.script_novelty_blocked` y
`reach_funnel` por canal.

### 9.2 Cadencia congelada durante el experimento
`pacing_profile.auto_transition_enabled()` devuelve **False** mientras el
experimento esté activo (T+45), para no escalar a `normal` en plena recuperación.
`scripts/freeze_pacing_for_experiment.py` fija el perfil (`recovery` por defecto)
y desactiva la auto-transición; `--off` revierte.

## 10. Criterio de cierre
El experimento se cierra cuando, a T+45: avisos = 0, alcance por vídeo recuperado y
long-form con distribución y retención > 40 %, o cuando una regla de decisión obligue a
revertir. El resultado se anota en este documento con fecha.

---

## 11. Bitácora (fuente única para "¿cómo va el experimento?")

> Esta sección es el punto de verdad. Cada intervención se registra aquí y en
> `system_state["experiment_interventions"]` el día que se despliega.

### 11.1 Cómo consultar el estado (0 cuota, solo lectura)
```
python3 scripts/experiment_report.py            # informe legible + bitácora
python3 scripts/experiment_report.py --json     # salida JSON
python3 scripts/experiment_report.py --log "texto de la intervención"
```
También vía API: `GET /api/analytics/experiment/report?days=14`.

### 11.2 KPIs oficiales
**Leading** (responden en días a packaging/contenido):
| KPI | Baseline | Objetivo |
|---|---|---|
| CTR long-form | ~1,9 % | ≥ 4 % |
| Impresiones/vídeo long-form | ~400–3.900 (vida) | tendencia ↑ sostenida |
| Retención long-form | ~25 % | > 40 % |

**Lagging** (tardan semanas): % browse/suggested (> 0), subs netos, alcance Shorts a 7 d.
El Reporting API tiene latencia de hasta 48 h: los últimos 1-2 días pueden faltar.

### 11.3 Diagnóstico T+7 (24/9/2026)
- **Long-form:** 0 browse/suggested; tráfico solo de search/related/subs; CTR ~1,9 %;
  retención ~25 %. → el cuello de botella es **packaging (CTR)** + impresiones.
- **Shorts:** alcance 7 d por vídeo cayó ~50 % (de ~450 en jul a ~170-260 en sep),
  coincidiendo con la ola de strikes de agosto. Es supresión de distribución, no clics.
- Vistas/día post-experimento −39 % a −64 % (parcialmente esperado por recortar shorts).
- **Avisos de política/strikes = 0** desde 11/9 (único objetivo cumplido).

### 11.4 Intervenciones registradas
| Fecha | Intervención | Efecto esperado |
|---|---|---|
| 17/9 | Instrumentación del experimento (baseline + checkpoints T+7/21/45) | Medir sin ambigüedad |
| 24/9 | Drenaje de shorts por canal (fix inanición canal2/canal3) | Restaurar producción de shorts |
| 24/9 | Congelación de cadencia en `recovery` (auto-transición off) | Ritmo prudente durante el experimento |
| (Fase 1) | Packaging: A/B por canal + caras selectivas + composición + saneo títulos (canal3/canal5) | Subir CTR e impresiones/vídeo |
| (Fase 2) | Search-first: temas por demanda de búsqueda | Impresiones vía búsqueda |
| (Fase 3) | Retención: hooks/estructura + bucle de feedback | Retención > 40 % |

### 11.5 Cómo decidir
Aplicar la matriz de §6 con los KPIs leading: si en 2-4 semanas CTR/impresiones-por-vídeo
suben, continuar y propagar; si están planos, refinar packaging/temas; si aparecen avisos,
revertir el cambio señalado.
