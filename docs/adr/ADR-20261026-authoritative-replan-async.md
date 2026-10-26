# ADR-20261026 — Reprogramación autoritativa asíncrona y acotada

## Estado

Aceptada. Implementada en fases 1–4 (oct 2026).

## Contexto

El botón **Reprogramar Ahora** ejecutaba el replan completo dentro de la
petición HTTP: borraba slots, recorría todos los canales, actualizaba
`publishAt` en YouTube y reconstruía longs + shorts. La operación podía durar
más que el timeout de Apache. El navegador recibía un `502 Proxy Error`
mientras FastAPI seguía trabajando y terminaba la reprogramación minutos
después. El operador no podía saber si el trabajo había terminado y un segundo
clic podía lanzar trabajo concurrente.

## Decisión

1. **Responder antes de trabajar.** `POST /api/planning/full-replan/authoritative`
   crea un run persistido y responde `202` con `run_id` y estado. El trabajo
   continúa fuera del ciclo HTTP.
2. **Mantener el lock de planificación.** El run se ejecuta en un hilo daemon
   del proceso API para conservar `_REPLAN_LOCK` frente a los bucles
   automáticos de planificación. No se mueve a un proceso separado sin un lock
   entre procesos.
3. **Un único run.** El coordinador persiste estado en
   `system_state["authoritative_replan_state"]`; si hay un run vivo, un nuevo
   POST se une a él. Un run huérfano se marca interrumpido antes de iniciar otro.
4. **Lease de ownership.** El propietario se identifica con `PID` +
   `/proc/<pid>/stat` start ticks. Un PID reutilizado no puede parecer un worker
   vivo.
5. **Estados explícitos.** `idle`, `queued`, `running`, `success`, `blocked`,
   `error`, `interrupted`; además `stale`, `orphaned`, `recoverable` y
   `recovery_available`.
6. **Recuperación explícita y acotada.** Un reinicio durante el run lo deja
   `interrupted`. `POST .../authoritative/recover` lo reintenta, con máximo de
   intentos configurable. No hay reintentos silenciosos.
7. **Seguimiento en el panel.** `GET .../authoritative/status` expone fase,
   progreso, canal actual, canales completados, heartbeat, duración y
   contadores remotos. El modal hace polling y reanuda el seguimiento tras
   recargar.
8. **Trabajo remoto acotado.** El replan comparte un presupuesto de
   `videos.update` entre repack y cobertura diaria. Las escrituras de estado
   usan timeout corto y reintentos solo para errores transitorios.
9. **Consistencia DB/YouTube.** Si YouTube acepta y la DB falla, se intenta
   restaurar el `publishAt` anterior en YouTube. Si el rollback también falla,
   se registra una discrepancia y el resultado queda marcado como parcial.

## Alternativas descartadas

- **Subir el timeout de Apache.** Habría ocultado el problema, mantenía el
  trabajo ambiguo y no permitía recuperación ni seguimiento.
- **FastAPI `BackgroundTasks` sin estado persistido.** No sobrevive a reinicios,
  no deduplica y no da progreso fiable.
- **Proceso independiente sin lock compartido.** El lock actual es de proceso;
  un worker aparte podría competir con los replans automáticos.
- **Reintentar cualquier error de YouTube.** Un 403 de cuota, auth o una
  petición inválida no son transitorios; reintentarlos solo gasta cuota y
  retrasa el diagnóstico.

## Consecuencias

Positivas:

- El botón deja de generar falsos `Proxy Error`.
- El operador ve progreso real y puede cerrar el modal sin cancelar.
- Un reinicio no produce runs duplicados y la recuperación es auditable.
- El gasto remoto está acotado y los fallos parciales son visibles.

Negativas y límites:

- Solo se conserva el último run (es una operación, no un historial).
- Si el proceso API muere, el hilo también; el run queda interrumpido y requiere
  recuperación explícita.
- El presupuesto remoto puede dejar vídeos para una pasada posterior. Es
  intencional: no se escribe la DB si YouTube no quedó sincronizado.
- El soporte es de un único proceso API. Varios procesos compartiendo DB no
  están soportados por el lock actual.

## Operación

- Runbook: `docs/runbook-authoritative-replan.md`.
- Contrato de planificación: `specs/planificacion-publicos-subidas.md`.
- Implementación: `api/services/authoritative_replan_service.py`,
  `api/services/planning_service.py`, `api/services/publish_repack.py`,
  `api/services/publish_coverage.py`, `pipeline/youtube_uploader.py`.
- UI: `frontend/src/pages/Scheduling.tsx`, `frontend/src/lib/api.ts`.
