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

### Defensa en profundidad a nivel kernel (host)

`UV_USE_IO_URING=0` no cubre a todo proceso node (p. ej. `npm install`/`npm ci`
lanzados a mano, o `vite build` que ya estuviera en vuelo). Refuerzo del host:

```conf
# /etc/sysctl.d/99-io-uring-disable.conf
kernel.io_uring_disabled=2
```

- `0` = io_uring habilitado (por defecto).
- `2` = **deshabilitado para todos** (incluido root): cualquier intento de crear
  un anillo io_uring falla con `EPERM`. Node/libuv hace fallback y no puede
  quedarse colgado en el teardown.
- Efecto inmediato para procesos nuevos: `sysctl --system` (no requiere reboot).
  Los procesos ya atascados siguen hasta el reinicio.

Comprobación: `cat /proc/sys/kernel/io_uring_disabled` → `2`.

## Detección

`api/services/system_watchdog.py::check_node_io_uring` se ejecuta en el
`_health_monitor_loop` (cada 90 s): si hay un proceso node en estado `D` durante
≥ 10 min emite la alerta **`node_io_uring_stuck`** (warning); cuando se limpia,
la resuelve automáticamente.

## Remediación

Un proceso en estado `D` **solo se limpia reiniciando el host** (no hay vía
userland). El watchdog avisa para elegir una ventana segura:

1. `systemctl stop autotube-ia-backfill.{service,timer}`.
2. Confirmar 0 generaciones/upload activos:
   `python3 scripts/deploy_safety.py` (aborta si hay long-form in-process) y
   `sqlite3 autotube.db "SELECT id,status FROM videos WHERE status IN
   ('generating','reassembling','uploading')"`.
3. Asegurar el sysctl persistente: `cat /proc/sys/kernel/io_uring_disabled` → `2`.
4. `systemctl stop autotube-panel && reboot`.
5. Verificar servicios/timers (`systemctl list-units 'autotube*'`), que
   `node_io_uring_stuck` se haya resuelto y relanzar el backfill.

El coste de dejarlo es bajo (RAM/fds), así que puede posponerse al próximo
reinicio natural. La alerta `node_io_uring_stuck` se resuelve sola en el primer
barrido del watchdog tras el reinicio (no hay procesos en estado `D`).
