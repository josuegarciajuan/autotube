# Auditoría Fase 0: políticas y compatibilidad legacy

**Fecha de corte:** 2026-08-31  
**Commit de referencia:** `c8d8683`  
**Estado:** baseline para migración

## Objetivo

Establecer una línea base reproducible de las fuentes de configuración,
enforcement de cadencia, estado de strikes y dependencias legacy antes de
parametrizar scripts operativos o retirar compatibilidad.

## Fuentes canónicas

| Área | Fuente |
|---|---|
| Identidad y cuenta | `channels` (`id`, `slug`, `google_account`) |
| Configuración efectiva | `config.config_bridge.get_channel_config()` |
| Perfil global | `system_state["pacing_profile"]` y `pacing_*` |
| Política por canal | `system_state["channel_delivery_policy_<id>"]` |
| Estado de riesgo | `api.services.channel_policy` |
| Cuota | Proyecto GCP resuelto por `quota_tracker` |
| Rutas runtime | `config.settings` y variables de entorno |

El orden de seguridad es monotónico: una capa inferior no puede relajar un
bloqueo, cap o separación impuestos por una capa superior.

## Puntos de enforcement

- `api/services/planning_service.py`: caps y separación de planificación.
- `api/services/publish_repack.py`: reprogramación de publicaciones.
- `api/services/upload_scheduler.py`: ventanas y colisiones de subida.
- `pipeline/publish_scheduler.py`: hora objetivo, spread y colisiones.
- `api/services/spam_mitigation.py`: bloqueos, retención y caps de cuenta.
- `api/services/shorts_scheduler.py`: caps y cooldown de shorts.
- `api/services/yt_state_reconciler.py`: visibilidad externa.
- `pipeline/youtube_uploader.py`: verificación post-subida.
- `scripts/check_video_removals.py`: sweep de eliminaciones.

## Hallazgos iniciales

| ID | Prioridad | Hallazgo | Próxima acción |
|---|---|---|---|
| F0-01 | Alta | `frontend/dist/index.html` modificado y archivos locales no clasificados en el árbol principal. | No incluirlos en cambios de política; clasificar por separado. |
| F0-02 | Alta | `sync_channel_policy.py` contiene una función mutadora aunque su CLI es dry-run. | Mover la mutación a una migración explícita. |
| F0-03 | Alta | Algunas rutas de planificación aún pueden invocar helpers sin contexto de canal. | Hacer obligatorio `channel_id` en enforcement. |
| F0-04 | Alta | El valor `longs_per_day=0` puede convertirse en 1 mediante `or 1`. | Definir cero como desactivado y cubrirlo con tests. |
| F0-05 | Media | Permanecen lectores directos legacy de estado de strikes. | Dual-read temporal y posterior retirada. |
| F0-06 | Media | Scripts de diagnóstico/mantenimiento contienen listas o IDs históricos. | Parametrizar selectores y exigir dry-run/confirmación. |
| F0-07 | Media | `pipeline-spec.md` y changelog no reflejan aún toda la arquitectura de políticas. | Actualizar documentación junto con la siguiente fase. |

## Dependencias legacy a retirar

- `shorts_spam_strikes_<channel_id>` como fuente principal.
- `shorts_spam_blocked_until_<channel_id>` como fuente principal.
- `MAX_LONGFORM_PUBLISH_PER_DAY`.
- `MIN_SAME_CHANNEL_UPLOAD_GAP_HOURS`.
- `ACCOUNT_DAILY_UPLOAD_CAP` cuando se use como override por canal.
- `PUBLISH_JITTER_MIN` una vez completada la migración a spread.
- Fallbacks a `ACTIVE_CHANNELS[0]` en código productivo.
- Imports directos de `config.canal*_config`.

## Comandos reproducibles

```bash
python3 scripts/sync_channel_policy.py --dry-run
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q \
  tests/test_channel_policy.py tests/test_channel_policy_phase2.py \
  tests/test_channel_policy_phase3.py tests/test_sync_channel_policy.py
```

El snapshot debe conservarse antes y después de cada migración para comparar
política solicitada, política efectiva, origen del valor y restricciones.

## Criterio de salida de Fase 0

- Inventario versionado y clasificado.
- Snapshot reproducible de todos los canales.
- Hallazgos F0-01 a F0-07 asignados a una fase posterior.
- Ningún secreto, token, backup o artefacto generado incluido en commits.
