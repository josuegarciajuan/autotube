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
3. **Separación mínima por canal.** Dos publicaciones del mismo canal nunca
   quedan a menos de `same_channel_publish_gap_h`. Si no caben ese día, la
   segunda pasa al siguiente día; **nunca** se recorta a `23:xx` ni se desplaza
   un minuto.
4. **Cap diario.** Como máximo `longform_publish_cap` long-forms por canal y
   `native_shorts_per_day` shorts nativos por canal y día Madrid.
5. **Reparto de shorts.** Varios shorts del día usan franjas distintas, elegidas
   por ranking; el gap de shorts (`shorts_same_type_gap_min`) se aplica después
   de elegir las franjas.
6. **Histórico por publicación real.** El rendimiento histórico se agrupa por
   `actual_published_at` / `published_at` (convertido a hora local), **no** por
   `created_at` (que mide generación, no publicación).
7. **Alcance del replan.** Replanificar un canal no borra ni recrea slots
   pendientes de otros canales. La persistencia de shorts es por
   `(channel_id, date_key)`.
8. **Reprogramar Ahora cubre todo el backlog.** `awaiting_upload`,
   `uploaded_private`, `warming` y slots pendientes se recolocan con el mismo
   asignador. Los jobs `running` no se tocan.
9. **Idempotencia.** Repetir un replan con los mismos datos produce el mismo
   plan y no crea duplicados.

## Verificación

- `tests/test_publish_distribution_contract.py` fija estos invariantes como
  regresiones.
- Las fases 1-5 del plan de implementación van desmarcando los `xfail` a medida
  que cada regla queda implementada.
