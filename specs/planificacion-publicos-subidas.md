# Planificación: públicos vs. subidas y ventana de calentando

> **REGLA DURA del sistema (sep 2026).** Cualquier cambio futuro en
> planificación, subida, publicación o reprogramación DEBE respetar este
> contrato. Si una feature nueva lo incumple, está mal.

## Principio

El sistema tiene **dos presupuestos independientes** por canal:

| Presupuesto | Valor | Fuente | Controla |
|---|---|---|---|
| **Públicos/día** | 2 longs + 3 shorts | plan inicial (panel) | `target_public_at` / `publishAt` |
| **Subidas privadas/día** | derivado = públicos long/día | `channel_policy` | cuántos pasan a `warming` |
| **Ventana de calentando** | máx **48 h** | `MAX_WARMUP_HOURS` | `target_public_at − uploaded_at` |

La **publicación** (hacerse público) es lo que fija el plan. La **subida**
(privada, "calentando") solo sirve para habilitar esa publicación y debe
hacerse lo más cerca posible de ella.

## Reglas duras

1. **Públicos/día = plan.** 2 long-forms y 3 shorts por canal y día
   (`longform_publish_cap` / `native_shorts_per_day`). Es la única fuente de
   `publishAt`. La generación NO puede recortar ni superar este plan.
2. **Subidas/día derivado.** `upload_capacity_per_day = public_longform_per_day`
   (no editable desde el panel). El mínimo necesario para cubrir el plan dentro
   de la ventana de 48 h.
3. **Ventana de calentando ≤ 48 h.** Un vídeo solo se sube si
   `target_public_at − ahora ≤ 48 h`. Si no cabe, permanece en
   `awaiting_upload` (preferido). Nunca se sube privado días antes.
4. **Cumplimiento diario obligatorio.** Si a lo largo del día el canal va por
   debajo de su cap de públicos y hay vídeos `warming`, se adelanta su
   `publishAt` para cumplir HOY (`remediate_today_public_deficit`, en
   `publish_coverage`). Si no hay material, alerta `publish_coverage_deficit`.
5. **Solo cuentan subidas reales.** El cap diario por cuenta Google cuenta
   `uploaded_at` (subidas reales), NUNCA `published_at`. Publicar un privado de
   un día anterior no consume cupo de subida de hoy.
6. **Nunca `error` por cap ni por ventana.** Un vídeo que no se puede subir por
   tope de cuenta/canal/ventana permanece reintentable (`awaiting_upload`); el
   scheduler hace cortocircuito cuando la cuenta está al tope (espera al reset
   de medianoche Madrid) en vez de martillear denegaciones.
7. **Anti-spam intacto.** No se relajan caps, gaps ni espaciado. El objetivo es
   *cumplir* el plan, no subir más rápido.

## "Reprogramar Ahora" (botón de Programación)

`POST /api/planning/full-replan/authoritative` → `authoritative_replan()`
(`api/services/planning_service.py`). Al pulsarlo:

1. Borra **todos** los `planned_slots` y `shorts_planned_slots` pendientes y
   cancela los `generation_jobs` en cola (protege `running`: invariante de una
   sola generación).
2. Reasigna `target_public_at` + `scheduled_upload_at` de pendientes y
   calentando (`apply_publish_repack`, `force_yt=True`).
3. Fuerza `publishAt` en YouTube de los ya subidos (videos.update, 50 u/vídeo).
4. Cubre el déficit de HOY (`remediate_today_public_deficit`).
5. Regenera shorts (3/día) y reconstruye el horizonte de generación.

Es **idempotente** y está pensado para pulsarse cuando algo se descuadre: un
clic y el sistema vuelve a estar al día.

## Dónde vive cada pieza

| Pieza | Archivo |
|---|---|
| Cap por cuenta (solo subidas reales) | `api/services/spam_mitigation.py`, `database/db_extended.py` |
| Presupuestos y `max_warmup_hours` | `api/services/channel_policy.py`, `config/defaults.py` |
| Ventana 48 h en repack | `pipeline/publish_scheduler.py` |
| Gates de subida + prioridad + cortocircuito | `api/services/upload_scheduler.py` |
| Catch-up diario | `api/services/publish_coverage.py` |
| Reprogramación total | `api/services/planning_service.py` |
| Botón | `frontend/src/pages/Scheduling.tsx`, `frontend/src/lib/api.ts` |

## Cómo verificar

- `used` de subidas de una cuenta = nº real de `uploaded_at` de hoy.
- Ningún vídeo `warming` con `target_public_at − uploaded_at > 48 h`.
- Cada canal termina el día con 2 longs + 3 shorts públicos (o alerta de déficit).
- `logs/api.log` sin bucles de `reached its daily upload cap`.
