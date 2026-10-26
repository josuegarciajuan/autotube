"""Coordinador single-flight de recolección de stats de YouTube.

Sustituye al watchdog por duración total (`_STATS_COLLECTION_TIMEOUT=900` en
api/main.py, origen del falso "Timed out after 901s" de oct 2026) por un ciclo
de vida explícito:

  - **Single-flight real**: un flag `_busy` bajo mutex garantiza que solo corre
    UNA recolección a la vez (global o por canal). Se libera SOLO cuando el
    hilo recolector termina (``finish``/``release_if_owner``), nunca por tiempo.
  - **Progreso persistido**: heartbeat + fase + canal actual + contador de
    canales, escrito en ``system_state['stats_collection_state']`` tras cada
    hito → el panel ve progreso real y un reload no pierde el estado.
  - **`stalled` diagnóstico, NO terminal**: si no hay progreso en
    ``STATS_STALL_THRESHOLD_S`` se marca ``stalled=True`` (calculado al leer;
    el GET /status sigue siendo de solo lectura), pero el run NO se libera ni
    se marca error: una recolección deep legítima tarda 30-40 min (4 canales
    en serie con Reporting API).
  - **Deadlines entre operaciones seguras**: el recolector comprueba
    ``run_deadline_exceeded()``/``channel_deadline_exceeded()`` ENTRE
    operaciones (nunca mata el hilo) y aborta ordenadamente con resultado
    parcial y lo ya recolectado intacto.
  - **Recuperación en arranque**: un estado ``running`` persistido no puede
    estar vivo tras un reinicio (el hilo muere con el proceso) → se marca
    ``error: Interrupted by server restart``.

Consumidores del estado persistido (compatibilidad garantizada):
  - ``api/services/lifecycle_monitor.py::_check_stats_collection_failed``
    (lee ``status`` y ``error`` del JSON en system_state).
  - Panel (Dashboard.tsx): ``status``, ``error``, ``channels[]``, ``result``,
    ``reach``, ``scrape_mode``, ``finished_at``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger("autotube.stats")

STATE_KEY = "stats_collection_state"


def _env_int(name: str, default: int) -> int:
    """Lee un entero de env con suelo de 60 s (evita valores absurdos)."""
    try:
        return max(60, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


# Sin progreso durante este tiempo → `stalled=True` (DIAGNÓSTICO: no libera el
# run ni marca error; un canal lento con Reporting API puede tardar ~8-10 min).
STATS_STALL_THRESHOLD_S = _env_int("STATS_STALL_THRESHOLD_S", 900)

# Deadline TOTAL de la recolección: el recolector lo comprueba ENTRE canales y
# entre fases (operaciones seguras) y aborta ordenadamente con lo recolectado.
# Un run deep real tarda ~30-40 min → 60 min da margen holgado.
STATS_RUN_MAX_DURATION_S = _env_int("STATS_RUN_MAX_DURATION_S", 3600)

# Deadline POR CANAL: registrado en el estado y comprobado tras el canal;
# el corte intra-canal (deadline propagado al fetcher) llega con la fase 2.
STATS_CHANNEL_MAX_DURATION_S = _env_int("STATS_CHANNEL_MAX_DURATION_S", 900)


def _fresh_state() -> dict:
    """Estado base (misma forma que el legacy STATS_COLLECTION_STATE + campos nuevos)."""
    return {
        # Campos legacy (NO renombrar: UI y lifecycle_monitor los leen)
        "status": "idle",           # idle | running | success | error
        "started_at": None,
        "finished_at": None,
        "channels": [],
        "error": None,
        "result": None,             # success | partial | error (cobertura real)
        "reach": None,
        "scrape_mode": False,
        # Campos nuevos del coordinador
        "run_id": None,
        "scope": "all",             # all | channel
        "channel_id": None,
        "deep": False,
        "force": False,
        "heartbeat_at": None,
        "last_progress_at": None,
        "run_deadline_at": None,
        "channel_deadline_at": None,
        "phase": None,              # starting | channel | revenue | milestones | finalizing
        "current_channel_slug": None,
        "completed_channels": 0,
        "total_channels": 0,
        "deadline_hit": False,
    }


class StatsCollectionCoordinator:
    """Estado + single-flight + progreso persistido de la recolección de stats."""

    def __init__(self):
        self._mutex = threading.Lock()
        self._busy = False
        self._state: dict = _fresh_state()

    # ── Ciclo de vida ─────────────────────────────────────────

    def try_acquire(self, deep: bool = False, force: bool = False,
                    scope: str = "all", channel_id: Optional[int] = None) -> tuple[bool, dict]:
        """Intenta iniciar un run. Devuelve (acquired, snapshot).

        Si hay un run activo devuelve (False, snapshot_del_run_activo): el
        llamador (endpoint) debe adjuntarse a él, nunca lanzar otro.
        """
        with self._mutex:
            if self._busy:
                return False, self._snapshot_locked()
            self._busy = True
            now = time.time()
            self._state = _fresh_state()
            self._state.update({
                "run_id": uuid.uuid4().hex[:12],
                "status": "running",
                "scope": scope,
                "channel_id": channel_id,
                "deep": bool(deep),
                "force": bool(force),
                "started_at": now,
                "heartbeat_at": now,
                "last_progress_at": now,
                "run_deadline_at": now + STATS_RUN_MAX_DURATION_S,
                "phase": "starting",
            })
            self._pin_locked()
            logger.info("stats run %s acquired (scope=%s deep=%s force=%s)",
                        self._state["run_id"], scope, deep, force)
            return True, self._snapshot_locked()

    def release_if_owner(self, run_id: Optional[str]) -> None:
        """Red de seguridad: libera SOLO si `run_id` es el run actual.

        `finish()` ya libera; esto cubre salidas anticipadas por excepción.
        Nunca libera el run de otro (evita que un hilo antiguo desarme uno nuevo).
        """
        with self._mutex:
            if self._busy and run_id is not None and self._state.get("run_id") == run_id:
                self._busy = False
                logger.warning("stats run %s released without explicit finish "
                               "(revisar: debería pasar por finish())", run_id)

    def current_run_id(self) -> Optional[str]:
        with self._mutex:
            return self._state.get("run_id") if self._busy else None

    def is_busy(self) -> bool:
        with self._mutex:
            return self._busy

    # ── Progreso (llamado SOLO por el hilo recolector) ───────

    def heartbeat(self, phase: Optional[str] = None,
                  channel_slug: Optional[str] = None,
                  completed: Optional[int] = None,
                  total: Optional[int] = None,
                  channel_deadline_at: Optional[float] = None,
                  **extra: Any) -> None:
        """Actualiza heartbeat/fase/canal y persiste. Reinicia `stalled`."""
        with self._mutex:
            now = time.time()
            self._state["heartbeat_at"] = now
            self._state["last_progress_at"] = now
            if phase is not None:
                self._state["phase"] = phase
            if channel_slug is not None:
                self._state["current_channel_slug"] = channel_slug
            if completed is not None:
                self._state["completed_channels"] = completed
            if total is not None:
                self._state["total_channels"] = total
            if channel_deadline_at is not None:
                self._state["channel_deadline_at"] = channel_deadline_at
            for k, v in extra.items():
                self._state[k] = v
            self._pin_locked()

    def record_channel(self, summary: dict) -> None:
        """Registra el resumen de un canal terminado (ok o fallido) y persiste."""
        with self._mutex:
            chans = self._state.get("channels") or []
            chans = chans + [summary]
            self._state["channels"] = chans
            self._state["completed_channels"] = len(chans)
            now = time.time()
            self._state["heartbeat_at"] = now
            self._state["last_progress_at"] = now
            self._pin_locked()

    def finish(self, status: str = "success", result: Optional[str] = None,
               error: Optional[str] = None, reach: Optional[dict] = None,
               scrape_mode: Optional[bool] = None,
               deadline_hit: bool = False) -> dict:
        """Cierra el run (terminal), persiste y libera el single-flight.

        El éxito SIEMPRE limpia `error` (bug oct 2026: el estado success
        arrastraba el error stale del watchdog).
        """
        with self._mutex:
            now = time.time()
            if result is not None:
                self._state["result"] = result
            if reach is not None:
                self._state["reach"] = reach
            if scrape_mode is not None:
                self._state["scrape_mode"] = scrape_mode
            self._state["deadline_hit"] = deadline_hit
            self._state["error"] = error if status == "error" else None
            self._state["status"] = status
            self._state["finished_at"] = now
            self._state["heartbeat_at"] = now
            self._state["current_channel_slug"] = None
            self._state["channel_deadline_at"] = None
            self._pin_locked()
            self._busy = False
            logger.info("stats run %s finished: status=%s result=%s error=%s",
                        self._state.get("run_id"), status, self._state.get("result"), error)
            return self._snapshot_locked()

    # ── Deadlines (comprobar ENTRE operaciones seguras) ──────

    def run_deadline_exceeded(self) -> bool:
        with self._mutex:
            dl = self._state.get("run_deadline_at")
            return bool(self._busy and dl and time.time() > dl)

    def channel_deadline_exceeded(self) -> bool:
        with self._mutex:
            dl = self._state.get("channel_deadline_at")
            return bool(self._busy and dl and time.time() > dl)

    # ── Lectura ───────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Copia del estado (thread-safe). `stalled` se calcula al leer: el
        GET /status es de solo lectura y nunca muta el ciclo de vida."""
        with self._mutex:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict:
        s = dict(self._state)
        s["channels"] = list(self._state.get("channels") or [])
        if s.get("status") == "running":
            now = time.time()
            last = s.get("last_progress_at") or s.get("started_at") or now
            age = max(0, now - last)
            s["heartbeat_age_s"] = int(age)
            s["elapsed_s"] = int(now - s["started_at"]) if s.get("started_at") else 0
            s["stalled"] = age > STATS_STALL_THRESHOLD_S
        else:
            s["heartbeat_age_s"] = None
            s["elapsed_s"] = None
            s["stalled"] = False
        return s

    # ── Persistencia ─────────────────────────────────────────

    def _pin_locked(self) -> None:
        """Vuelca el estado a system_state (conexión nueva por llamada:
        se llama desde el event loop Y desde el hilo recolector)."""
        try:
            from database.db_extended import ExtendedDatabase
            _db = ExtendedDatabase()
            _db.set_system_state(STATE_KEY, json.dumps(self._state, default=str))
        except Exception as exc:
            logger.warning("Could not pin stats state: %s", exc)

    def recover_on_startup(self) -> None:
        """Sustituto de `_unpin_stats_state`: restaura el estado persistido.

        - `running` persistido → `error: Interrupted by server restart`
          (el hilo recolector muere con el proceso; no puede seguir vivo).
        - Estados terminales → se restauran tal cual (histórico).
        Nunca re-arma el flag `_busy`.
        """
        try:
            from database.db_extended import ExtendedDatabase
            _db = ExtendedDatabase()
            raw = _db.get_system_state(STATE_KEY)
            if not raw:
                return
            saved = json.loads(raw)
            if saved.get("status") == "running":
                saved.update({
                    "status": "error",
                    "error": "Interrupted by server restart",
                    "finished_at": time.time(),
                })
            with self._mutex:
                self._state = {**_fresh_state(), **saved}
                self._busy = False
                self._pin_locked()
        except Exception as exc:
            logger.warning("Stats state recovery skipped: %s", exc)


# ── Singleton ────────────────────────────────────────────────
_coordinator: Optional[StatsCollectionCoordinator] = None


def get_stats_coordinator() -> StatsCollectionCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = StatsCollectionCoordinator()
    return _coordinator
