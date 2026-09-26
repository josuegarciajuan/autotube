# Fuga de procesos Node del driver de Playwright

## Síntoma

El proceso `uvicorn api.main` acumulaba **~55 procesos hijos**
`node .../playwright/driver/package/cli.js run-driver` (uno por job de
scraping/marcado), con **~4,2 GB RSS** totales. Los drivers no se cerraban
nunca y sobrevivían a los hilos daemon que los crearon.

Detectar:

```bash
ps -eo pid,ppid,etimes,rss,args \
  | grep 'playwright/driver.*run-driver' | grep -v grep
# PPID debe ser el PID de uvicorn; etimes = antigüedad en segundos.
```

## Causa raíz (6 mecanismos)

La API `sync_playwright()` está atada por *greenlet* al hilo que llamó a
`start()`: `pw.stop()` **solo funciona desde el hilo propietario**. El código
ignoraba esto y encima descartaba la referencia tras el fallo, de modo que el
driver vivo quedaba fuera de todo registro.

1. **`_stop_playwright(pw)` cruzaba hilos y fallaba en silencio**
   (`pipeline/youtube_browser.py`): `_ensure_browser()` llamaba
   `_stop_playwright(self._playwright)` al detectar cambio de hilo. `pw.stop()`
   lanzaba `"cannot switch to a different thread"` → `except Exception: pass`;
   después **igual** hacía `_unregister_playwright(pw)`. El driver Node seguía
   vivo pero **ya no estaba registrado**, así que `atexit` /
   `_cleanup_all_playwrights()` no lo podían matar. **Fuga garantizada** en cada
   cambio de hilo sobre la instancia cacheada de `_browser_instances`.
2. **Hilos daemon por job** (`_auto_mark_altered_content`,
   `_auto_mark_ia_for_short`, `full_pipeline_worker`) crean un driver por hilo y
   llaman `cleanup_browser_thread()` en `finally`, pero al entrar en
   `_ensure_browser()` con un hilo distinto dejaban huérfano el driver anterior
   (punto 1).
3. **`_playwright_registry` era un `set` con referencias fuertes** y solo se
   vaciaba en `atexit`/`close_all`. En un uvicorn long-lived `atexit` no corre;
   un hilo que termina sin limpieza explícita dejaba el driver vivo y
   registrado… hasta que otro hilo lo desregistraba en falso (punto 1).
4. **`check_session_valid()` async**: `pw = await _async_pw().start()` estaba
   **fuera** del `try/finally` que hacía `pw.stop()`. Si
   `launch_persistent_context` / `new_page` / `goto` fallaba (o llegaba
   `asyncio.CancelledError`) antes del `try`, el driver async quedaba huérfano.
5. **`social_browser.BrowserSessionManager.start()`** no era atómico: si
   `launch()` o `new_context()` lanzaba tras `async_playwright().start()`,
   `self._playwright` quedaba iniciado y `__aexit__` **nunca** se ejecutaba
   (porque `__aenter__` falló). `stop()` tampoco toleraba errores ni
   cancelación paso a paso.
6. **`close()`** solo paraba un `self._playwright` sin verificar la muerte real
   del driver, y `close_all_browsers()`/`_cleanup_all_playwrights()` intentaban
   parar **todos** los drivers desde el hilo llamante: los ajenos fallaban (y se
   desregistraban) → más fugas.

Vector adicional real: `reconcile_recent_ia_marks()` (`api/services/
ia_marking_health.py`, invocado vía `asyncio.to_thread` desde
`_ia_mark_reconcile_loop`) usaba `get_browser()` sin **ningún**
`cleanup_browser_thread()`; los hilos del `ThreadPoolExecutor` de uvicorn son
persistentes, así que la fuga se acumulaba ahí.

## Arreglo

### `pipeline/youtube_browser.py` — ciclo de vida consciente del hilo propietario

- El registry pasa de `set` a `dict` `id(pw) -> {pw, owner_native_id,
  owner_ident, pid, started_at, driver_pid, stop_requested, orphaned, ...}`.
- `_stop_playwright(pw)` **solo** llama `pw.stop()` si el hilo actual es el
  propietario. Si el stop lanza, **no desregistra**: marca `orphaned` y deja la
  entrada para el reaper. Si el hilo es ajeno, marca `orphaned` sin llamar
  `stop()` (imposible desde otro hilo).
- `_ensure_browser()` en cambio de hilo **no para** el Playwright anterior: lo
  marca huérfano (`_mark_orphaned`) y crea el del hilo actual.
- `_cleanup_all_playwrights(force=False)` solo para los propios; `force=True`
  (atexit) intenta todos y después barre los drivers `run-driver` por PID.
- `close()` es owner-aware; `atexit` hace `kill_all_driver_children()`.
- Al arrancar cada instancia se registra el **PID del driver Node** (diff de
  hijos antes/después de `sync_playwright().start()`), para que el reaper pueda
  matarlo por PID.
- El async `check_session_valid()` envuelve **start + launch + uso** en un
  `try/finally` que siempre para `ctx` y `pw` (cubre excepción, timeout y
  `CancelledError`).

### `pipeline/social_browser.py`

- `start()` atómico: cualquier fallo tras `start()` → `await self.stop()` y
  re-lanza (cubre `BaseException`, incluido `CancelledError`).
- `stop()` limpia referencias primero y cierra con `asyncio.shield` cada
  recurso, tolerando errores y re-lanzando la cancelación solo al final.
- `__del__` avisa (no puede awaited) si el GC se lleva una sesión abierta.

### `api/services/ia_marking_health.py`

`reconcile_recent_ia_marks()` envuelve el bucle en `try/finally` y llama
`cleanup_browser_thread()` si abrió navegador local.

## Reaper (salvaguarda) — `pipeline/playwright_reaper.py`

Loop daemon lanzado desde `api/main.py` como `_playwright_reaper_loop`
(supervisado, cada `PLAYWRIGHT_REAPER_INTERVAL_SECONDS` = 600 s). Cada pasada:

1. Enumera **hijos directos** `run-driver` **de este proceso** vía
   `/proc/<pid>/task/*/children` (fallback `ps --ppid`). Nunca mira otros PIDs.
2. Mata con **SIGKILL** solo los que superan
   `PLAYWRIGHT_DRIVER_TTL_SECONDS` (default **3600 s**). Los drivers recientes
   (jobs en curso, típicamente ~20 min) **nunca** se tocan: el TTL es > 1 h.
3. Además, las entradas del registry marcadas `orphaned`/`stop_requested` o cuyo
   hilo propietario ya no existe se matan tras
   `PLAYWRIGHT_REAPER_ORPHAN_GRACE_SECONDS` (default 120 s), porque un driver sin
   dueño no puede estar en uso.
4. Antes de cada kill re-verifica `PPID == parent` y que el argv siga siendo
   `run-driver` (ventana de reutilización de PID).
5. Kill-switch: `PLAYWRIGHT_REAPER_ENABLED=false`.
6. Métricas/estado: `GET /api/system/playwright-drivers` y
   `playwright_reaper.get_reaper_stats()`.

El reaper **no** rompe la invariante de concurrencia (solo mata procesos Node
ya huérfanos; no lanza ni altera generaciones).

## Verificación

- Unitarios (`tests/test_playwright_lifecycle.py`):
  - no se llama `stop` cross-thread y la entrada **no** se desregistra;
  - `_stop_playwright` conserva el registry si el stop falla;
  - `_ensure_browser` en cambio de hilo marca huérfano (no para);
  - `check_session_valid` para `pw` si el launch falla;
  - `social_browser.start()` no deja `_playwright` vivo si falla y `stop()`
    tolera errores y cancelación;
  - el reaper mata solo drivers viejos, respeta el kill-switch y el PID ajeno.
- Script real (`scripts/verify_playwright_reaper.py`):
  ```bash
  # conteo de hijos run-driver de este proceso
  python3 scripts/verify_playwright_reaper.py --count
  # reaper mata un driver simulado con TTL violado y respeta uno reciente
  python3 scripts/verify_playwright_reaper.py --simulate-stale
  # N ciclos reales de la ruta de sesión: el conteo NO debe crecer
  YT_BROWSER_TOKENS_DIR=/tmp/pw_verify_tokens \
    python3 scripts/verify_playwright_reaper.py --cycles 2 --account X --cycle-timeout 80
  ```

## Runbook — limpieza manual de drivers antiguos

> Solo si el reaper está desactivado o hay que limpiar sin reiniciar.
> **Regla de oro: verifica la edad antes de matar. Nunca mates drivers < TTL ni
> de otro PPID.**

1. Identifica el PID de uvicorn y la antigüedad de cada driver:
   ```bash
   API_PID=$(pgrep -f "uvicorn api.main:app" | head -1)
   ps -o pid,ppid,etimes,rss,args --ppid "$API_PID" \
     | grep run-driver | grep -v grep
   ```
2. Mata **solo** los de más de 1 hora (`etimes > 3600`) e hijos de la API:
   ```bash
   API_PID=$(pgrep -f "uvicorn api.main:app" | head -1)
   ps -o pid=,etimes=,ppid= --ppid "$API_PID" \
     | awk -v p="$API_PID" '$2 > 3600 && $3 == p {print $1}' \
     | xargs -r kill -9
   ```
3. Verifica: `... --count` o el comando del paso 1 debe quedar en 0.
4. Si el reaper está activo y con TTL correcto, normalmente no hace falta:
   espera una pasada (≤10 min) o `GET /api/system/playwright-drivers`.

Comprobación previa **siempre** (`etimes`): un driver de ~20 min pertenece al
job en curso y **no** debe matarse.

## Invariantes

1. **Nunca** llamar `pw.stop()` desde un hilo distinto al que creó la instancia.
   Si el hilo cambió, se abandona y el driver lo mata el reaper.
2. **Nunca** desregistrar un driver que sigue vivo. Desregistrar ≠ parar.
3. Todo `async_playwright().start()` va dentro de un `try` cuyo `finally` cierra
   contexto y Playwright (incluido antes del launch).
4. `BrowserSessionManager.start()` es atómico: fallo intermedio ⇒ `stop()`.
5. El reaper solo mata `run-driver` **hijos de este PID** con edad > TTL. Es la
   única limpieza automática permitida. Kill-switch:
   `PLAYWRIGHT_REAPER_ENABLED=false`.
