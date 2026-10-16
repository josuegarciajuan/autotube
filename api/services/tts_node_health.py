#!/usr/bin/env python3
"""Registro de salud de nodos TTS (Fase 3).

Persiste, por nodo, el resultado de las delegaciones TTS para excluir de la
selección los nodos que fallan, sin depender del tag `kokoro` (que solo declara
intención, no capacidad real).

Estados:
  - `healthy`      : sin fallos recientes.
  - `degraded`     : ≥1 fallo; se excluye del siguiente intento del MISMO job.
  - `quarantined`  : ≥`AUTOTUBE_TTS_QUARANTINE_AFTER` fallos consecutivos; no se
                     vuelve a seleccionar hasta un smoke test satisfactorio
                     (certificación funcional, Fase 2), que limpia el registro.

El registro es JSON best-effort: un fallo de lectura/escritura NUNCA interrumpe
el pipeline (fail-open hacia "healthy").
"""
from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger("autotube.tts_health")

DEFAULT_HEALTH_FILE = "/var/lib/taildeck/autotube_tts_health.json"

HEALTHY = "healthy"
DEGRADED = "degraded"
QUARANTINED = "quarantined"


def _path() -> str:
    return os.environ.get("AUTOTUBE_TTS_HEALTH_FILE", DEFAULT_HEALTH_FILE)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def quarantine_after() -> int:
    try:
        return max(1, int(os.environ.get("AUTOTUBE_TTS_QUARANTINE_AFTER", "2")))
    except (TypeError, ValueError):
        return 2


def load_registry() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("nodes"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"nodes": {}}


def _save_registry(reg: dict) -> None:
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(reg, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as exc:  # best-effort: la salud nunca tumba el pipeline
        logger.debug("No se pudo guardar el registro de salud TTS (%s): %s", path, exc)


def state_of(node: str) -> str:
    entry = (load_registry().get("nodes") or {}).get(node) or {}
    return str(entry.get("state") or HEALTHY)


def is_available(node: str) -> bool:
    """Un nodo en cuarentena no se selecciona; degradado sí (con margen)."""
    return state_of(node) != QUARANTINED


def record_failure(node: str, error_code: str | None = None, detail: str | None = None) -> str:
    """Registra un fallo. Devuelve el estado resultante del nodo."""
    if not node:
        return HEALTHY
    reg = load_registry()
    nodes = reg.setdefault("nodes", {})
    entry = nodes.get(node) or {}
    failures = int(entry.get("failures") or 0) + 1
    state = QUARANTINED if failures >= quarantine_after() else DEGRADED
    entry.update({
        "failures": failures,
        "state": state,
        "last_error_code": error_code,
        "last_error": (detail or "")[:300],
        "updated_at": _now(),
    })
    if state == QUARANTINED and "quarantined_at" not in entry:
        entry["quarantined_at"] = _now()
    nodes[node] = entry
    _save_registry(reg)
    logger.warning("TTS node %s → %s (%d fallos, code=%s)", node, state, failures, error_code)
    return state


def record_success(node: str) -> None:
    """Un éxito limpia el historial del nodo (vuelve a healthy)."""
    if not node:
        return
    reg = load_registry()
    nodes = reg.setdefault("nodes", {})
    if node in nodes:
        nodes.pop(node, None)
        _save_registry(reg)


def reconcile_with_certification(certified: set | None) -> None:
    """Un nodo certificado reciente sale de cuarentena/degradación.

    Es la única vía de reincorporación: no basta con esperar un tiempo.
    """
    if not certified:
        return
    reg = load_registry()
    nodes = reg.setdefault("nodes", {})
    changed = False
    for node in list(nodes.keys()):
        if node in certified:
            nodes.pop(node, None)
            changed = True
    if changed:
        _save_registry(reg)
        logger.info("TTS salud: nodos reincorporados por certificación: %s", sorted(certified))


def reset() -> None:
    """Borra el registro (tests / operación)."""
    _save_registry({"nodes": {}})
