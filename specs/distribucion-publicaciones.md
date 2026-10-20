# Contrato de distribución de publicaciones

> **Contrato duro (oct 2026).** Todo planificador, replan o repack debe producir
> horarios que cumplan estas reglas. Si una ruta las incumple, está mal.

## Invariantes

1. **Una sola fuente de franjas.** Las franjas salen de
   `optimal_publish_slots` (long/short), ordenadas por `slot_rank`
   (`1` = mejor). El ranking no se sustituye por el orden del reloj.
2. **Zona horaria del canal.** Toda decisión de día/hora se hace en
   `PUBLISH_TIMEZONE` (por defecto `Europe/Madrid`). Al persistir se convierte a
   UTC, pero los caps y los gaps se calculan en hora local.
3. **Reparto franja-first.** Cada día usa sus franjas: si `N <= F`, N franjas
   distintas (máxima separación, rank desempata); si `N > F`, todas las franjas
   y los extras comparten franja con un offset secuencial. El gap
   `same_channel_publish_gap_h` es una preferencia, **no** un veto: nunca manda
   una publicación al día siguiente.
4. **Cap diario.** Como máximo `longform_publish_cap` long-forms por canal y
   `native_shorts_per_day` shorts nativos por canal y día Madrid.
5. **Reparto de shorts.** Varios shorts del día usan franjas distintas, elegidas
   por ranking; el gap de shorts (`shorts_same_type_gap_min`) se aplica después
   de elegir las franjas.
6. **Targets ya comprometidos.** El horizonte reparte sus slots alrededor de los
   `target_public_at` de vídeos `awaiting_upload`/`warming`/`uploaded_private`
   y de slots `running`: no puede dejarlos a menos del gap ni superar con ellos
   el cap diario.
7. **Histórico por publicación real.** El rendimiento histórico se agrupa por
   `actual_published_at` / `published_at` (convertido a hora local), **no** por
   `created_at` (que mide generación, no publicación).
8. **Alcance del replan.** Replanificar un canal no borra ni recrea slots
   pendientes de otros canales. La persistencia de shorts es por
   `(channel_id, date_key)`.
9. **Reprogramar Ahora cubre todo el backlog.** `awaiting_upload`,
   `uploaded_private`, `warming` y slots pendientes se recolocan con el mismo
   asignador. Los jobs `running` no se tocan.
10. **Idempotencia.** Repetir un replan con los mismos datos produce el mismo
   plan y no crea duplicados.

## Verificación

- `tests/test_publish_distribution_contract.py` fija estos invariantes como
  regresiones.
- Las fases 1-6 del plan de implementación van desmarcando los `xfail` a medida
  que cada regla queda implementada.

## Estado de implementación

| Fase | Estado |
|---|---|
| 0 — contrato + regresiones | ✅ |
| 1 — asignador único (`api/services/publish_allocator.py`) | ✅ |
| 2 — histórico por publicación real | ✅ |
| 3 — shorts por ranking de franja | ✅ |
| 4 — replan por canal | ✅ |
| 5 — Reprogramar Ahora completo | ✅ |
| 6 — verificación operativa (simulación + despliegue) | ✅ |
