# Fase 4 — Ranking de temas multi-señal (curiosidad oscura)

> Fecha: **2026-10-23**. Implementación: `pipeline/topic_ranking.py` +
> integración en `orchestrator.py`, `pipeline/topic_seeding.py`,
> `api/services/shorts_scheduler.py`, migración **v71** de trazabilidad.
> Tests: `tests/test_topic_ranking.py` (**15 passed**) + 164 tests de las
> suites relacionadas en verde.

## Qué ordena (y qué NO)

El ranking **ordena candidatos ya elegibles**; no sustituye ningún gate:

| Señal | Peso por defecto | Fuente |
|---|---|---|
| `demand` | 0.40 | Autocompletado YouTube (fail-open, neutral 0.5) |
| `niche` | 0.20 | `niche_guard.niche_fit_score` (proxy, no gate) |
| `editorial` | `EDITORIAL_CURIOSITY_WEIGHT` (0.25) | Tensión narrativa (Fase 3) |
| `visual` | 0.10 | Pistas léxicas + cifras + nombres propios + longitud |
| `novelty` | 0.10 | `1 − Jaccard` frente a los últimos 20 títulos del canal |
| `source` | 0.05 | URL + cuerpo suficiente (calidad de fuente) |

Los pesos se **normalizan** entre sí. Nicho, `content_safety`, respaldo
factual y demanda siguen decidiendo elegibilidad por separado.

**Diversidad:** si el tipo de tensión dominante del candidato ya aparece en los
`EDITORIAL_RECENT_DIVERSITY_MIN` temas recientes, se resta
`TOPIC_RANKING_REPEAT_PENALTY` (0.20). Es una penalización, **nunca un veto**.

**Fail-open:** cualquier error o dato ausente devuelve el orden original con
scores neutros. Kill-switch: `TOPIC_RANKING_ENABLED=False` o
`system_state["topic_ranking_disabled"]="true"`.

## Dónde se aplica

| Ruta | Cambio |
|---|---|
| **Normal** (`phase_generate_script`) | Pool de 5 → `TOPIC_RANKING_POOL_SIZE` (12) y ranking multi-señal antes del niche-guard |
| **Fallback por demanda** | Mismo pool y ranking; las semillas editoriales del canal entran en el seeding (`EDITORIAL_TENSION_SEEDS`) |
| **Viral** (`A2`, candidatos en DB) | Se examinan 6 candidatos; bonus editorial **acotado** (±25 %) solo entre los que están a ≤50 % del mejor `viral_score` |
| **Fallback viral→original** | Pool + ranking (antes cogía `content_items[0]` a ciegas) |
| **Shorts standalone** | Orden estable de los temas seguros por señal editorial del canal |
| **Maratón** | Su selección es scraping profundo de Wikipedia (sin pool de candidatos): no se reordena, pero **sí se etiqueta** la tensión y el grupo piloto en la procedencia |

## Trazabilidad (migración v71)

`content_selection_provenance` añade tres columnas idempotentes:

| Columna | Contenido |
|---|---|
| `signals_json` | Desglose completo (`demand`, `niche`, `editorial`, `visual`, `novelty`, `source`, `similarity_max`, `tension`) |
| `editorial_label` | `editorial:<tipo>` o `editorial:none` |
| `pilot_group` | `pilot` \| `control` \| `disabled` (determinista por canal+día) |

El registro se hace en `_record_selection_provenance`:
- si hubo ranking, se adjunta la fila del tema elegido;
- si no hubo pool (maratón/viral explícito), se calcula la etiqueta editorial
  y el grupo piloto directamente desde el tema;
- `novelty_ok` se deriva de la señal de novedad;
- el `reason` incluye el desglose legible (`rank=…; demanda=…; nicho=…;
  editorial=…(tipo); …`).

## Invariantes

1. Sin `if slug ==` ni `channel_id ==` (test de invariante incluido).
2. Determinista: mismas entradas → mismo orden (los empates conservan el orden).
3. No filtra: todos los candidatos entran y salen (solo cambia el orden).
4. No añade coste de red: el ranking reutiliza la demanda ya consultada.

## Verificación

```
pytest tests/test_topic_ranking.py                    → 15 passed
pytest <topics + provenance + orchestrator + shorts>  → 164 passed
```

Preexistente (también falla en producción, no es regresión):
`test_orchestrator.py::test_accepts_script_above_threshold` (el fixture usa
contenido fuera de nicho con el guard estricto de canal2).

## Siguiente fase

Fase 5: alinear la promesa (fuentes → título → miniatura → arranque del guion)
y persistir la ficha editorial por vídeo.
