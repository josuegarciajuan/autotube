# Plan — Fiabilidad y visibilidad de native shorts

Fecha: 2026-08-24 · Estado: aprobado, en implementación

## Contexto / problemas

| # | Problema | Síntoma observado |
|---|---|---|
| P0 | Fallos silenciosos de native short | La barra de generación "salta" (job aparece, falla en ~1 min, se auto-descarta) y **no se crea ninguna alerta** en `pipeline_alerts`. 39 slots nativos cancelados + decenas de jobs `failed` en 7 días con **0 alertas sin resolver**. |
| P1 | Filtro de títulos similares rechaza en bucle | `_title_similar_to_recent()` compara **títulos** (overlap de tokens ≥ 0.6), pero al LLM solo se le pasan los **temas** (`get_recent_short_topics`). Al rechazar hace `return None` y el slot reintenta con el **mismo prompt** → rechazos repetidos con títulos 100% idénticos (logs 22:07/22:08 slot #31336). |
| P2 | Shorts `generated` invisibles en Programación | `_fill_native_short_queue()` genera con `generate_only=True` y `_dispatch_short_async()` marca el slot `completed` aunque el short solo está `generated` (sin subir). Los endpoints `/planning/shorts-slots/today|week` solo leen `shorts_planned_slots`; la cola real (`shorts.status='generated'`, 27 shorts) no se muestra. |
| P3 | *(opcional)* La generación muere en reinicios de API | Native shorts corren in-process (`asyncio.to_thread`); cada reinicio mata el job ("Server restarted — old process no longer exists"). `shorts_worker.py` existe pero no está cableado. |

## Cambios

### Cambio 1 — Motivo de fallo + alerta al agotar reintentos (P0)

**`api/services/shorts_scheduler.py`**

- Añadir parámetro `slot_id: int = None` a `_dispatch_native_short()` (ya lo recibe `_dispatch_short_async`).
- Helper `_native_fail(slot_id, job_id, reason)`: escribe `error_message` en el slot y en el job con el motivo real. Reemplazar cada `return None` de fallo (título similar, seguridad, render degradado, TTS, script inválido, auth) por `_native_fail(...)`.
- En `_dispatch_short_async`, rama "retries agotados → cancelled": leer el `error_message` del slot y emitir `create_alert(alert_type="short_dispatch_failed", entity_type="short", entity_id=slot_id, ...)`. Dedup automático por `create_alert`.

### Cambio 2 — Títulos que nacen distintos (P1, A+B+D)

**`api/services/shorts_scheduler.py`**

- **B — Títulos recientes en el prompt:** reutilizar `_recent_short_titles()` + `_recent_longform_titles()` para inyectar ~15 títulos recientes como "negativos" con regla explícita: *"el título NO debe compartir >50% de palabras con ninguno"*. Hoy solo se pasan los temas (punto ciego).
- **A — Bucle cerrado de regeneración:** envolver `llm_json_call → validate → title-check` en un bucle de máx 3 intentos. En intento ≥2, añadir feedback concreto del conflicto: *"tu título 'X' choca con 'Y' (publicado); genera título y tema totalmente distintos"*. Solo si los 3 fallan → `_native_fail`.
- **D — Similitud sin stopwords:** filtrar palabras función españolas en `_titles_too_similar()` antes del overlap. Reduce falsos rechazos en títulos cortos. Función compartida con long-form (menos falsos positivos también en `warn_if_title_similar`).

### Cambio 3 — Estado real `generated` + visibilidad en Programación (P2)

**`api/services/shorts_scheduler.py`**

- En `_dispatch_short_async`, cuando `generate_only=True` tenga éxito → slot a `generated` (no `completed`).
- En `_upload_queued_native_short`: cambiar el look-up del slot a `status IN ('generated','completed')` (retrocompatible) y, al publicar con éxito, pasar el slot a `completed`.

**`api/routers/planning.py`**

- `/planning/shorts-slots/today`: añadir lista + contador `queued` desde `shorts WHERE status='generated'` (título, canal, `created_at`, `file_path` existe, `short_id`).

**`frontend/src/lib/api.ts` + `frontend/src/pages/Scheduling.tsx`**

- Renderizar estado `generated` como badge "en cola · pendiente de subir".
- Bloque "Cola de shorts generados": título, canal, fecha, estado del archivo, botón "Subir ahora" (opcional).

### Cambio 4 — *(opcional)* Sobrevivir reinicios (P3)

- Cablear `api/services/shorts_worker.py` como subprocess (patrón `full_pipeline_worker.py`) en `dispatch_next_due_shorts_slot`, o mínimo: alertar cuando el job muere por "Server restarted".

## Testing

- Unit: `_titles_too_similar` con stopwords (distintos → no falso positivo; duplicados → sí); bucle de retry con feedback; transición de slot `running → generated → completed`.
- Manual: generar native scheduled y ver progreso + estado en Programación; forzar fallo de título y ver alerta `short_dispatch_failed`.

## Ejecución

Orden **1 → 2 → 3** (4 opcional). Vía worktree + commits atómicos + `scripts/apply_changes.sh` al integrar.
