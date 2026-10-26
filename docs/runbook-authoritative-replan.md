# Runbook: Reprogramación autoritativa ("Reprogramar Ahora")

## Qué hace

`POST /api/planning/full-replan/authoritative` encola una reprogramación total:

1. borra slots long-form/shorts pendientes y cancela jobs en cola (nunca toca
   `running`);
2. reasigna pendientes y vídeos calentando;
3. fuerza `publishAt` en YouTube dentro de un presupuesto acotado;
4. cubre el déficit de públicos de hoy;
5. reconstruye horizonte y calendario de shorts.

La petición devuelve **`202` inmediatamente**. `202` significa **encolado**, no
terminado. El progreso se consulta aparte.

## Endpoints

| Método | Ruta | Uso |
|---|---|---|
| POST | `/api/planning/full-replan/authoritative` | Crea o se une al run activo. Devuelve `run_id`. |
| GET | `/api/planning/full-replan/authoritative/status` | Último run. Admite `?run_id=`. |
| POST | `/api/planning/full-replan/authoritative/recover` | Reintenta un run `interrupted`. Admite `?run_id=`. |

```bash
curl -sS -X POST http://127.0.0.1:8000/api/planning/full-replan/authoritative
curl -sS http://127.0.0.1:8000/api/planning/full-replan/authoritative/status | python3 -m json.tool
```

No lanzar dos POST "por si acaso": el segundo se une al run activo y devuelve
`joined=true`.

## Estados

| Estado | Significado | Acción |
|---|---|---|
| `idle` | No hay run reciente. | Ninguna. |
| `queued` | Persistido, aún sin hilo/worker. | Esperar. |
| `running` | En ejecución. | Seguir `phase`/`progress_pct`. |
| `success` | Terminó. | Revisar `summary`; si `partial=true`, ver incidencias. |
| `blocked` | Terminó sin aplicar (p. ej. scheduler pausado). | Revisar `error`/motivo. |
| `error` | Falló el worker. | Revisar log y lanzar un run nuevo. |
| `interrupted` | El proceso API murió o el owner desapareció. | `POST .../recover`. |

Campos diagnósticos:

- `stale=true`: el owner vive, pero no hay heartbeat reciente. **No** se lanza
  otro run automáticamente.
- `orphaned=true`: el estado estaba activo pero el owner ya no vive.
- `recoverable` / `recovery_available`: hay recuperación explícita disponible.
- `heartbeat_age_s`, `stale_after_s`, `duration_s`.
- `channels[]`: resumen de canales completados.
- `summary.yt_*`: intentos, éxitos, fallos, omitidos y rollbacks remotos.
- `summary.partial`: hubo escrituras DB fallidas o discrepancias DB/YouTube.

## Recuperación tras reinicio

Un reinicio de la API mata el hilo. Al arrancar, el coordinador marca el run
`interrupted` y conserva `run_id`, progreso y canales completados.

```bash
curl -sS -X POST "http://127.0.0.1:8000/api/planning/full-replan/authoritative/recover"
```

La recuperación es explícita y acotada por
`AUTHORITATIVE_REPLAN_MAX_RECOVERY_ATTEMPTS` (por defecto `1`). Si se agota,
lanza una reprogramación nueva desde el panel. La operación es idempotente.

Si otro proceso vivo es el owner, este proceso API se une al run y no intenta
duplicarlo.

## Presupuesto remoto de YouTube

El presupuesto es **por run y por vídeo** (los reintentos HTTP no cuentan como
vídeos adicionales). Se comparte entre repack y cobertura de hoy. Cuando se
agota, los vídeos que requieren YouTube **no se escriben en DB**; se reintentarán
en una pasada posterior. Así DB y YouTube no quedan desalineados.

| Variable | Default | Efecto |
|---|---:|---|
| `AUTHORITATIVE_REPLAN_MAX_YT_UPDATES` | `100` | Máximo de vídeos con intento de `videos.update`. `0` = no tocar YouTube. |
| `YT_STATUS_UPDATE_HTTP_TIMEOUT_S` | `20` | Timeout de socket para actualizaciones de estado; no afecta a subidas. |
| `YT_STATUS_UPDATE_RETRIES` | `1` | Reintentos adicionales solo para 408/429/5xx, rate limits y red. |
| `YT_STATUS_UPDATE_RETRY_BASE_DELAY_S` | `1.0` | Backoff exponencial base. |
| `AUTHORITATIVE_REPLAN_STALE_S` | `1800` | Umbral diagnóstico de heartbeat. |
| `AUTHORITATIVE_REPLAN_MAX_RECOVERY_ATTEMPTS` | `1` | Recuperaciones explícitas permitidas por run interrumpido. |

> Estas variables se documentan aquí porque `.env.example` está protegido por el
> hook de commit. Se leen del entorno/.env en el arranque.

## Fallos parciales y discrepancias

Orden de escritura:

1. Se actualiza YouTube (`videos.update`).
2. Si YouTube falla: **no se toca la DB**; cuenta como fallo remoto.
3. Si YouTube acepta y la DB falla: se intenta devolver YouTube al target
   anterior (rollback).
4. Si el rollback falla: se incrementa `summary.yt_db_mismatches` y se emite un
   log **CRITICAL**. El resultado queda `partial=true`.

Consultar:

```bash
journalctl -u autotube-panel --since '2 hours ago' | grep -Ei 'authoritative_replan|MISMATCH|YouTube updates|rollback'
```

Ante una discrepancia:

- no relanzar en bucle; revisar los vídeos afectados en YouTube Studio;
- corregir el target en DB o volver a ejecutar un replan cuando el canal no
  tenga un run activo;
- recordar que el replan usa `force_yt=True`, por lo que una pasada posterior
  puede resincronizar YouTube con el plan vigente.

## Logs y verificación rápida

```bash
journalctl -u autotube-panel -f | grep -E 'authoritative replan|MISMATCH|YouTube updates'
curl -sS http://127.0.0.1:8000/api/planning/full-replan/authoritative/status
```

No se debe usar `GET /status` como sustituto de `GET /api/planning/slots/today`:
el run puede estar `success` y la planificación necesitar unos segundos para
reflejar el nuevo calendario.

## Pruebas de referencia

```bash
python3 -m pytest -q \
  tests/test_authoritative_replan_async.py \
  tests/test_replan_remote_updates.py \
  tests/test_authoritative_replan_requeues.py \
  tests/test_publish_replan_backlog.py
```

Cubren ciclo asíncrono, lease/recuperación, presupuesto remoto, retries,
timeout, rollback DB/YouTube y no regresión del replan.
