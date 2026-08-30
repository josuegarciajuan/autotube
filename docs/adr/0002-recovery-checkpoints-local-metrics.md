# ADR-0002: Checkpoints de recuperación con métricas locales

**Status:** Accepted  
**Date:** 2026-08-31

## Contexto

La recuperación de Sincronías necesita distinguir problemas de packaging,
tema, distribución y visibilidad sin repetir el incidente de cuota. La API
puede reiniciarse mientras los workers continúan ejecutándose y los datos de
YouTube pueden estar ausentes o retrasados.

## Decisión

El `schedule_checker` ejecutará revisiones a 48 h, 4 d y 7 d usando únicamente
la última fila local de `video_stats_history`. La revisión de 14 d será
condicional al checkpoint de 7 d. Cada checkpoint se reclama en una tabla
idempotente y emite una alerta `pipeline_alerts`; la ausencia de métricas no
marca el checkpoint como completado y se reintenta sin llamar a YouTube.

## Trade-offs

- **Elegido:** persistencia SQLite + alertas existentes. Resiste reinicios,
  reduce coordinación y mantiene `STATS_AUTO_COLLECT=false`, pero depende de
  que una recolección manual haya dejado datos.
- **Rechazado:** consulta automática de Analytics en cada checkpoint: aportaría
  más frescura, pero consumiría cuota y violaría el modo de recuperación.
- **Rechazado:** cambios automáticos de título/miniatura: acelera experimentos,
  pero aumenta riesgo de spam; las recomendaciones quedan bajo revisión humana.

## Consecuencias

Se obtienen señales operativas visibles y auditables sin relajar pacing,
concurrencia ni reglas anti-strike. El operador debe recolectar stats
manualmente cuando proceda y decidir cualquier cambio de packaging.
