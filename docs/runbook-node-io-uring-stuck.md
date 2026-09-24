# Runbook: procesos Node/npm atascados en estado D (io_uring)

## Síntoma
La alerta `node_io_uring_stuck` (warning, sistema) aparece con procesos `node`,
`npm`, `npx`, `pnpm`, `yarn` o `corepack` en estado `D (disk sleep)` durante
más de 10 minutos. Se observan bloqueados en `io_uring_cancel_generic` →
`do_exit` y **no responden a ningún signal** (ni `SIGKILL`).

```
ps -eo pid=,stat=,etimes=,comm=,args= | awk '$2 ~ /^D/'
```

## Impacto
- No bloquean los deploys por sí mismos, pero pueden quedarse con el lock del
  hook `post-merge` y dejar un `vite build` / `npm install` colgado.
- No hay forma de matarlos en caliente: solo se limpian con un **reinicio del host**.

## Mitigación aplicada (preventiva)
- `UV_USE_IO_URING=0` en `apply_changes.sh`, el driver de Playwright y
  `autotube-panel.service`.
- `kernel.io_uring_disabled=2` (parámetro de kernel).
El watchdog (`api/services/system_watchdog.py`) corre en el health loop (90 s):
emite la alerta si hay atascados y **la auto-resuelve** en cuanto desaparecen.

## Acción (reinicio en ventana segura)
1. Confirmar que no hay generación long-form activa (subprocess worker).
2. Reiniciar el host en una ventana sin renders:
   ```
   sudo systemctl reboot
   ```
3. Tras el arranque, verificar que la alerta `node_io_uring_stuck` se resolvió
   sola (el watchdog la cierra al no encontrar procesos en estado `D`).

> Si tras el reinicio reaparece, revisar que `UV_USE_IO_URING=0` siga presente en
> los tres puntos y que `io_uring_disabled=2` esté activo (`cat /proc/cmdline`).
