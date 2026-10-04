"""Cliente del motor distribuido de SuperServer (sin cookies).

Escribe peticiones en el spool que consume `server/distengine.js` y espera el
resumen publicado en `results/<execId>.json`. Se evita `ss-execute` a propósito:
construimos el JSON con `json.dumps` (sin problemas de escapado) y no dependemos
de su instalación en `/usr/local/bin`.

Rutas (configurables por entorno para pruebas):
  TAILDECK_DIST_DIR  -> raíz del motor (default /var/lib/taildeck/distributed)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Optional

from .model import atomic_write_json, sanitize_id

DIST_DIR = os.environ.get("TAILDECK_DIST_DIR", "/var/lib/taildeck/distributed")
SPOOL_DIR = os.path.join(DIST_DIR, "spool")
CANCEL_DIR = os.path.join(DIST_DIR, "cancel")
RESULTS_DIR = os.path.join(DIST_DIR, "results")
ARTIFACTS_DIR = os.path.join(DIST_DIR, "artifacts")

TERMINAL = {"done", "failed", "cancelled"}


class DistributedClientError(RuntimeError):
    pass


def _ensure_dirs() -> None:
    for d in (SPOOL_DIR, CANCEL_DIR, RESULTS_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as exc:
            raise DistributedClientError(
                f"No se pudo preparar el spool distribuido en {DIST_DIR}: {exc}. "
                "¿Está SuperServer (taildeck) instalado en el control plane?"
            ) from exc


def submit(
    definition: str,
    params: dict,
    *,
    exec_id: Optional[str] = None,
    label: Optional[str] = None,
    max_inflight: Optional[int] = None,
    max_attempts: Optional[int] = None,
    deadline_sec: Optional[int] = None,
    max_units: Optional[int] = None,
    req: Optional[dict] = None,
) -> str:
    """Encola una ejecución. Devuelve el id (sanitizado)."""
    _ensure_dirs()
    eid = sanitize_id(exec_id) if exec_id else sanitize_id(
        f"atube-{int(time.time())}-{os.getpid()}"
    )
    payload: dict[str, Any] = {"id": eid, "definition": definition, "params": params}
    if label:
        payload["label"] = label
    if max_inflight is not None:
        payload["maxInflight"] = int(max_inflight)
    if max_attempts is not None:
        payload["maxAttempts"] = int(max_attempts)
    if deadline_sec is not None:
        payload["deadlineSec"] = int(deadline_sec)
    if max_units is not None:
        payload["maxUnits"] = int(max_units)
    if req:
        payload["req"] = req
    atomic_write_json(os.path.join(SPOOL_DIR, f"{eid}.json"), payload)
    return eid


def status(exec_id: str) -> Optional[dict]:
    """Resumen publicado de una ejecución, o None si aún no existe."""
    path = os.path.join(RESULTS_DIR, f"{sanitize_id(exec_id)}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def wait(
    exec_id: str,
    *,
    timeout: float = 3600.0,
    poll: float = 2.0,
    progress_cb: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Espera a estado terminal. Devuelve el resumen; lanza si se agota el tiempo."""
    eid = sanitize_id(exec_id)
    deadline = time.time() + timeout
    last: Optional[dict] = None
    while time.time() < deadline:
        st = status(eid)
        if st is not None:
            if progress_cb is not None:
                try:
                    progress_cb(st)
                except Exception:  # el progreso nunca debe romper la espera
                    pass
            last = st
            if st.get("status") in TERMINAL:
                return st
        time.sleep(poll)
    raise DistributedClientError(
        f"Timeout esperando la ejecución distribuida {eid} "
        f"(último estado: {(last or {}).get('status', 'desconocido')})"
    )


def cancel(exec_id: str) -> None:
    """Pide la cancelación (best-effort) de una ejecución."""
    _ensure_dirs()
    eid = sanitize_id(exec_id)
    atomic_write_json(os.path.join(CANCEL_DIR, f"{eid}.json"), {"id": eid, "cancel": True})


def artifacts_dir(exec_id: str) -> str:
    return os.path.join(ARTIFACTS_DIR, sanitize_id(exec_id))


def accepted_scenes(summary: dict) -> list:
    """Extrae la lista de escenas verificables del resultado de `combine`."""
    result = (summary or {}).get("result") or {}
    scenes = result.get("scenes")
    return scenes if isinstance(scenes, list) else []
