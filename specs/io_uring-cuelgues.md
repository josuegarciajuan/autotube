# Cuelgues de node por io_uring (estado D)

## Síntoma

Un `vite build` (o el driver node de Playwright) queda en estado
**`D (disk sleep)`** y **no se puede matar** con ningún signal (SIGKILL queda
pendiente). El proceso se reparenta a PID 1 y sobrevive horas/días. Durante ese
tiempo puede **retener el lock del hook `post-merge`** y bloquear deploys.

Stack del kernel observado (`/proc/<pid>/stack`):

```
io_uring_del_tctx_node ← io_uring_cancel_generic ← __io_uring_cancel
  ← do_exit ← do_group_exit ← get_signal ← ...
```

Es un cuelgue en el **teardown de io_uring** durante la salida del proceso:
libuv/node usó io_uring y el kernel queda esperando una petición que nunca
completa.

## Causa

io_uring de libuv (Node.js) en kernel `5.15.0-191-generic` (VM QEMU). Bajo carga
alta (load > 20) el riesgo aumenta.

## Mitigación (preventiva)

`UV_USE_IO_URING=0` en todos los paths que lanzan node:

1. `scripts/apply_changes.sh` (build de frontend).
2. `pipeline/youtube_browser.py` (driver de Playwright; hereda el env).
3. `autotube-panel.service` (`Environment=UV_USE_IO_URING=0`).

## Detección

`api/services/system_watchdog.py::check_node_io_uring` se ejecuta en el
`_health_monitor_loop` (cada 90 s): si hay un proceso node en estado `D` durante
≥ 10 min emite la alerta **`node_io_uring_stuck`** (warning); cuando se limpia,
la resuelve automáticamente.

## Remediación

Un proceso en estado `D` **solo se limpia reiniciando el host** (no hay vía
userland). El watchdog avisa para elegir una ventana segura:

1. `systemctl stop autotube-ia-backfill.{service,timer}`.
2. Confirmar 0 generaciones/upload activos.
3. `systemctl stop autotube-panel && reboot`.
4. Verificar servicios/timers y relanzar el backfill.

El coste de dejarlo es bajo (RAM/fds), así que puede posponerse al próximo
reinicio natural.
