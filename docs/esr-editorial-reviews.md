# Revisiones editoriales ESR

La revisión es opt-in por canal mediante `EDITORIAL_RECOVERY_REVIEW`, heredado
desde `config/defaults.py` y sobreescrito en `config_json` por el bridge. El
perfil de canal4 activa tres checkpoints por cada subida nueva (+48 h, +7 d,
+14 d) y una auditoría diaria. No se hace backfill: los vídeos históricos no
se ocultan, borran, retitulan ni modifican.

El ledger `editorial_reviews` es idempotente y conserva estado (`scheduled`,
`running`, `succeeded`, `failed`), intentos, error y resumen. El loop
`editorial_reviews` ejecuta vencidas cada hora. Los fallos se reintentan según
el perfil y siempre generan una alerta visible; los éxitos también generan
`editorial_review_success`.

La comprobación de integridad es de solo lectura y usa `yt-dlp` (sin YouTube
Data API): duplicados de `yt_video_id`, registros huérfanos, estado externo y
miniatura ausente. Sin stats recientes se informa
`insufficient_data_manual_collection`; la recolección sigue siendo manual y
`STATS_AUTO_COLLECT` no se activa. Con muestra suficiente se distingue
`no_impressions`, `low_ctr`, `low_retention` y `healthy`.

Antes de una subida nueva, `validate_new_video()` aplica términos bloqueados,
keywords y requisito de miniatura configurados por canal. Un rechazo genera
`editorial_preflight_blocked` y no llama a YouTube. Si el servicio de revisión
no está disponible, el preflight es fail-open para no convertir una incidencia
del monitor en una mutación parcial del pipeline.

## Operación

- Consultar `pipeline_alerts` para éxitos, fallos y bloqueos.
- Solicitar manualmente **Recolectar stats** cuando el resumen indique muestra
  insuficiente.
- No resolver alertas de integridad como si fueran correcciones automáticas:
  el sistema informa, pero las acciones sobre YouTube siguen siendo manuales.
