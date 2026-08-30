# Runbook: checkpoints de recuperación de Sincronías

El loop `schedule_checker` revisa vídeos publicados a las 48 h, 4 días y 7 días.
La revisión de 14 días solo se crea si existe el checkpoint de 7 días. El proceso
lee únicamente `video_stats_history`: no activa `STATS_AUTO_COLLECT`, no llama a la
API de YouTube y no modifica títulos, miniaturas ni publicaciones.

Cada resultado aparece en `pipeline_alerts` como `recovery_checkpoint_<horas>h`.
El JSON de `metadata` contiene `execution_time`, `metrics_available`, métricas,
`classification`, `recommendation` y `next_checkpoint_hours`. Las clasificaciones
son diagnósticas: `low_ctr` sugiere revisar packaging manualmente; `low_impressions`
puede indicar distribución/visibilidad; `early_retention_drop` sugiere revisar
tema o gancho; `metrics_unavailable` se reintentará en el siguiente ciclo.

La tabla `recovery_checkpoints` hace el proceso idempotente y resistente a reinicios.
La cuota agotada no impide los avisos. No ejecutar scripts de repack o cambios de
packaging basándose solo en una alerta: mantener el perfil de cadencia anti-strike.
