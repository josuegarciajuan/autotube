# Auditoría de mejoras y backlog — 2026-11-09

Alcance: 4 canales activos (canal2 Sincronías, canal3 Civilizaciones Olvidadas,
canal4 Expediciones sin retorno, canal5 Anomalías Médicas). Datos de alcance
(`video_reach_daily`) hasta **2026-11-06**. Ventanas comparadas:
**2026-10-10 → 2026-11-06** vs **2026-09-12 → 2026-10-09**.

> Este documento separa **hechos medidos**, **cambios ya implementados** y
> **experimentos pendientes de validación**. No se presentan como probadas
> mejoras que aún no tienen muestra suficiente.

---

## 1. Resumen ejecutivo

1. **Los shorts sostienen las vistas; los largos sostienen el watch time.**
   En los 4 canales, los shorts aportan la mayoría de vistas y suscriptores,
   mientras los largos aportan la mayor parte de minutos vistos. La estrategia
   de crecimiento debe proteger ambos, no sustituir uno por otro.
2. **El cuello de botella de los largos es distinto por canal:**
   - canal2: prácticamente **sin distribución** (47 largos subidos, 254 vistas).
   - canal5: **mucha distribución y CTR muy bajo** (65.767 impresiones, 1,56%).
   - canal3: distribución alta y CTR bajo (101.016 impresiones, 1,90%).
   - canal4: distribución moderada y mejor CTR (18.899 impresiones, 2,95%).
3. **La duración real supera el objetivo** en canal3 y canal4. El cambio de
   política ya está implementado; hay que medir los próximos 15–20 largos.
4. **Varias recomendaciones de Insight AI tocaban claves inertes** (no leídas
   por el pipeline) o se perdían por duplicado de mayúsculas. Ya corregido el
   mecanismo; la calidad de las recomendaciones queda como P1.
5. **Datos suficientes para decidir, con límites claros:** 28 días completos,
   ~40k filas de alcance, 650 filas sin impresiones. Los shorts no permiten
   calcular CTR como vistas/impresiones; debe usarse `impressions_ctr`.

---

## 2. Datos base (28 días recientes)

| Canal | Vistas | Impresiones | CTR pond. | Min. vistos | Subs netos | Retención media* |
|---|---:|---:|---:|---:|---:|---:|
| Sincronías | 23.524 | 8.696 | 1,53% | 3.973 | +46 | 11,6% |
| Civilizaciones Olvidadas | 14.993 | 138.353 | 1,76% | 13.815 | +34 | 21,6% |
| Expediciones sin retorno | 22.638 | 43.321 | 1,93% | 14.734 | +40 | 20,3% |
| Anomalías Médicas | 27.010 | 80.683 | 1,60% | 6.981 | +41 | 14,3% |

\* Última instantánea por vídeo; no es una ventana de 28 días.

### Reparto por formato (28 días recientes)

| Canal | Formato | Vídeos | Vistas | Impresiones | CTR pond. | Min. vistos | Subs |
|---|---|---:|---:|---:|---:|---:|---:|
| canal2 | largo | 130 | 254 | 1.791 | 3,02% | 653 | 0 |
| canal2 | short | 353 | 23.267 | 6.870 | 1,14% | 3.319 | +46 |
| canal3 | largo | 166 | 4.670 | 101.016 | 1,90% | 11.784 | +17 |
| canal3 | short | 405 | 10.289 | 36.203 | 1,39% | 1.884 | +16 |
| canal4 | largo | 163 | 2.161 | 18.899 | 2,95% | 9.902 | +9 |
| canal4 | short | 408 | 20.466 | 24.313 | 1,14% | 4.817 | +33 |
| canal5 | largo | 147 | 1.609 | 65.767 | 1,56% | 4.991 | +8 |
| canal5 | short | 369 | 25.400 | 14.900 | 1,78% | 1.990 | +34 |

> El CTR de shorts y largos **no es comparable**: las impresiones del feed de
> Shorts no tienen la misma semántica. Se reporta `impressions_ctr` y se evita
> el ratio derivado vistas/impresiones.

### Producción en la ventana

- Largos subidos: canal2 47, canal3 46, canal4 51, canal5 49.
- Shorts publicados: canal2 97, canal3 99, canal4 95, canal5 93.
- Duración real de largos (mediana): canal2 12,7 min; canal3 19,6;
  canal4 22,6; canal5 18,7.
- Objetivo efectivo tras la corrección: canal2 11±3; canal3 16±4;
  canal4 14±4; canal5 17±3.

---

## 3. Hallazgos de la auditoría

### P0 — Corregidos en este cambio

| ID | Hallazgo | Evidencia | Corrección |
|---|---|---|---|
| P0-1 | Cambios aplicados en minúsculas quedaban ocultos o se borraban al arrancar | canal3 `title_formulas` vs `TITLE_FORMULAS`; log `skipping duplicate key` | Normalización canónica al escribir; los duplicados con valores distintos ya no se borran |
| P0-2 | «Aplicar» no verificaba el efecto real | El endpoint solo fusionaba y marcaba aplicado | Escritura atómica + verificación contra configuración efectiva + rollback |
| P0-3 | Aplicaciones concurrentes podían pisarse | Read-merge-write sin lock | Lock por canal |
| P0-4 | Dos recomendaciones podían sobrescribirse sin aviso | canal3: preguntas + Göbekli | Estado `superseded` + claves solapadas |
| P0-5 | Error de aplicación invisible en el panel | `console.error` únicamente | Banner de error + recarga desde servidor |
| P0-6 | Duración contradictoria entre objetivo y legacy | 4 canales con rangos incompatibles | `config/duration_policy.py` como fuente única |
| P0-7 | Recomendaciones aceptadas perdidas | Se reprodujo en canal3 | Script de reparación con replay de aceptadas |
| P0-8 | Largo de **25 segundos** subido como long-form (canal2) | `videos.id=2852`, `duracion_seg=25` | Gate bloqueante si duración < 50% del mínimo objetivo |

### P1 — Pendientes (impacto alto, sin implementar aún)

| ID | Hallazgo | Evidencia | Cambio propuesto |
|---|---|---|---|
| P1-1 | **Claves inertes**: Insight AI proponía cambios que el pipeline no lee | `CONTENT_PILLARS` no se consumía; `script_end_hook` no tiene consumidor | **Implementado:** pilares cableados al ranking; `script_end_hook` retirado de la superficie |
| P1-2 | Pilares con dos esquemas | canal2 usaba `{pillar,pct,description}` | **Implementado:** `config/content_pillars.py` normaliza ambos; script de migración |
| P1-3 | Deduplicación ocultaba recomendaciones con valores distintos | Compara título + claves, no valores | **Implementado:** conflicto visible, no oculto |
| P1-4 | Analítica etiquetada “30 días” sin filtro temporal | `get_channel_ctr_summary` agregaba todo el histórico | **Implementado:** ventana real, CTR ponderado, ceros como ausencia |
| P1-5 | Insight AI solo ve 20 vídeos y 15 puntos de crecimiento | `channel_analyzer.py` LIMIT 20/15 | **Implementado:** agregados últimos 28 días por formato + errores del periodo |
| P1-6 | Errores sin periodo ni denominador | 17 errores `video` en canal3 en 28 días | **Implementado:** tasa por 100 ejecuciones por fase |
| P1-7 | Duración real fuera de objetivo | canal4 mediana 22,6 min vs objetivo 10–18 | **Implementado:** gate de outliers + alerta `duration_drift` |
| P1-8 | Títulos en inglés en canal2 | “Dreamt the earthquake before it happened…” | **Implementado:** guardia de idioma (warning; bloqueo opcional) |
| P1-9 | Fase `video` concentra fallos | Errores de octubre; 0 en los últimos 7 días | **Implementado:** monitor de regresión 24h; concat estable desde 31-oct |
| P1-10 | **canal2: largos sin distribución** | 47 subidos, 254 vistas; **1–17 impresiones por vídeo**, todos públicos | **Acción aplicada (2026-11-09):** cadencia de largos a **1/día** y generación a **1/día**; revisar el **2026-11-23**. Sin strikes ni eliminaciones; revisar Studio |

### P2 — Experimentos de crecimiento (requieren aprobación y plazo)

| ID | Hipótesis | Métrica principal | Guardarraíl | Muestra / plazo |
|---|---|---|---|---|
| E1 | En canal5, títulos con cifra + misterio y miniatura de alto contraste suben el CTR de largos | CTR ponderado de largos | Retención no cae >2 pp | 20 largos / 4 semanas |
| E2 | En canal3, fórmulas de pregunta + payoff completo suben el CTR sin perder watch time | CTR ponderado de largos | Minutos vistos por vídeo | 20 largos / 4 semanas |
| E3 | En canal2, largos derivados de los temas de shorts ganadores recuperan distribución | Vistas e impresiones de largos | Cadencia de shorts intacta | 10 largos / 4 semanas |
| E4 | Ajustar canal4 al objetivo de duración reduce abandono y sube retención | Retención media + duración media vista | Vistas totales no caen | 15 largos / 3 semanas |
| E5 | Un puente explícito shorts→largo (CTA + tema espejo) aumenta el tráfico de shorts al largo | Vistas del largo desde `SHORTS_FEED`/sugeridos | Sin cambios en frecuencia de subida | 20 shorts + 5 largos / 4 semanas |

Cada experimento debe registrarse antes de empezar con: fecha de inicio,
ventana de comparación, métrica principal, guardarraíl y criterio de reversión.

---

## 4. Límites del análisis

- El CTR de shorts no se puede derivar de vistas/impresiones.
- La retención disponible es la última instantánea, no una ventana homogénea.
- Los vídeos tienen edades distintas; no se comparan curvas de vida completas.
- No hay atribución causal shorts→largo: solo asociación temporal.
- 650 filas de alcance sin impresiones: se tratan como ausencia, no como CTR 0.
- Los datos de alcance terminan el 2026-11-06; los cambios de hoy no están
  reflejados todavía.

---

## 5. Backlog consolidado

| Prioridad | ID | Cambio | Estado |
|---|---|---|---|
| P0 | P0-1…P0-8 | Aplicación fiable, reparación y duración única | **Implementado en este cambio** |
| P1 | P1-1 | Cablear o invalidar claves inertes | **Implementado** |
| P1 | P1-2 | Unificar esquema de pilares | **Implementado** |
| P1 | P1-3 | Dedup por valores | **Implementado** |
| P1 | P1-4 | Ventana y CTR ponderado en analítica | **Implementado** |
| P1 | P1-5 | Agregados completos para Insight AI | **Implementado** |
| P1 | P1-6 | Errores por periodo | **Implementado** |
| P1 | P1-7 | Monitor de duración real | **Implementado** |
| P1 | P1-8 | Validación de idioma de título | **Implementado (warning)** |
| P1 | P1-9 | Causa raíz de fallos de render | **Monitor implementado; estable** |
| P1 | P1-10 | Distribución de largos canal2 | Cadencia reducida a 1/día (revisar 2026-11-23) |
| P2 | E1–E5 | Experimentos de crecimiento | Pre-registrados en `docs/experiments/`; ejecución pendiente de ventana |

---

## 6. Verificación de este cambio

```bash
python3 -m pytest tests/test_insight_apply.py \
  tests/test_config_bridge_canonical.py tests/test_duration_policy.py \
  tests/test_channel_analyzer.py tests/test_config_validator.py \
  tests/test_voice_timing.py -q
cd frontend && npm run build
python3 scripts/audit_insight_applications.py --since-id 43
python3 scripts/repair_insight_applications.py --dry-run --replay-applied
python3 scripts/reconcile_duration_config.py --dry-run
python3 scripts/global_growth_analysis.py --end 2026-11-06
```
