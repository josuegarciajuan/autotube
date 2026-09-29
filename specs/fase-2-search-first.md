# Fase 2 — Search-first: temas por demanda de búsqueda

> **Estado:** MVP implementado (sep 2026) · **Ámbito:** los 4 canales activos
> **Contrato padre:** `specs/experimento-recuperacion-alcance.md` (§3, §6).
> **Restricción dura:** 100 % automatizado, 0 cuota en la fase de selección.

## 1. Problema

El experimento de recuperación de alcance fijó como contrato (§3) "demanda real
(autocomplete/tendencias) + novelty semántico + veto entre canales". Hasta ahora
la demanda **solo reordenaba** candidatos ya scrapeados
(`orchestrator.phase_generate_script` → `pipeline/topic_demand.rank_labels`): no
gobernaba la generación de temas. Los shorts, además, se ideaban con LLM puro
sin ninguna señal de búsqueda real.

## 2. Objetivo

Sembrar **consultas** desde el autocompletado público de YouTube a partir de las
keywords SEO del canal, puntuarlas por demanda y persistirlas como candidatos
auditables, de modo que:

- los **shorts** basen al menos la mitad de sus ideas en consultas reales;
- el **long-form** use la demanda como señal de ranking (compuesto).

## 3. Diseño

| Pieza | Archivo |
|---|---|
| Recolección/seed (0 cuota) | `pipeline/topic_seeding.py` |
| Autocompletado | `pipeline/topic_demand.fetch_suggestions` (Google Suggest) |
| Persistencia | tabla `topic_demand_candidates` (migración v65) |
| Helpers DB | `db_extended.py`: `save_topic_demand_candidates`, `get_recent_topic_demand_candidates`, `count_topic_demand_candidates` |
| Wiring long-form | `orchestrator._seed_topics_from_demand()` + ranking compuesto |
| Wiring shorts | `pipeline/shorts_standalone.discover_standalone_topics` (bloque de consultas) |
| Métrica | `GET /api/analytics/experiment` → `seeded.topics_seeded_from_demand` |

### 3.1 Semillas por canal
Prioridad: `TOPIC_SEED_QUERIES` (explícitas) → `SEO_PRIMARY_KEYWORD` →
`SEO_SECONDARY_KEYWORDS` (máx 5) → `NICHE_KEYWORDS_ENG` (máx 5).

### 3.2 Scoring
- Una sugerencia real de autocompletado implica demanda: `max(0.6, 1 - i*0.05)`
  (decae con la posición).
- El seed solo entra si `score_demand(seed, suggestions) > 0`.
- Se persisten `TOPIC_SEED_MAX_QUERIES` (12) candidatos ordenados por score.

### 3.3 Long-form
`phase_generate_script` recolecta y persiste candidatos **antes** de
`get_unused_content`, y ordena las fuentes por un ranking compuesto:

```
score = 0.75 * demanda(tema) + 0.25 * solapamiento(tema, consultas_semilla)
```

Es un ranking, no un bloqueo: sin candidatos se comporta como antes (fail-open).

## 4. Reglas duras

1. **0 cuota**: solo Google Suggest; nunca Data API en esta fase.
2. **Fail-open**: cualquier fallo de red deja el pipeline como estaba.
3. **No fabricar contenido**: el long-form sigue necesitando fuente scrapeada;
   la demanda solo ordena. Los shorts sí pueden idear desde la consulta.
4. **Auditable**: cada candidato queda en `topic_demand_candidates` con score,
   fuente y fecha.
5. **`topic_dedup` y `content_safety` son innegociables**: la siembra no los
   relaja.

## 5. Kill-switch

- `TOPIC_SEEDING_ENABLED=False` (por canal / defaults).
- `system_state["topic_seeding_disabled"]="true"` (global, sin despliegue).

## 6. Métricas (checkpoint)

- `seeded.topics_seeded_from_demand` (cuántas consultas se sembraron).
- **Primaria Fase 2**: impresiones vía búsqueda (`reach_funnel` por canal) y %
  de tráfico de búsqueda en el long-form.
- **Leading**: impresiones/vídeo long-form (spec padre §11.2).

## 7. Rollback

`TOPIC_SEEDING_ENABLED=False` revierte el comportamiento sin tocar código; la
tabla queda inerte.

## 8. Fuera de alcance (backlog)

- Volumen de búsqueda real (no proxy) e integración de Google Trends en la fase
  de selección.
- UI/endpoint de inspección de `topic_demand_candidates`.
- Generación de long-form **desde** una consulta sin fuente scrapeada.
