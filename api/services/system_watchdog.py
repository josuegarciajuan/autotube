"""Watchdog de procesos node atascados en estado D (io_uring).

Contexto (sep 2026): un `vite build` quedó en estado `D (disk sleep)` dentro de
`io_uring_cancel_generic` → `do_exit`, sin poder ser matado por ningún signal y
bloqueando ocasionalmente el lock del hook post-merge. Este watchdog detecta
procesos node atascados y avisa para poder reiniciar el host en ventana segura.

Mitigación preventiva: `UV_USE_IO_URING=0` (en `apply_changes.sh`, en el driver
de Playwright y en `autotube-panel.service`).
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger("autotube.system_watchdog")

DEFAULT_MIN_SECONDS = 600  # 10 minutos
ALERT_TYPE = "node_io_uring_stuck"


def find_stuck_node_processes(min_seconds: int = DEFAULT_MIN_SECONDS) -> list[dict]:
    """Procesos node en estado D durante >= ``min_seconds``.

    Lee ``ps`` (barato y estable). Devuelve [{pid, elapsed_s, args}].
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,stat=,etimes=,comm=,args="],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        logger.debug("ps failed: %s", exc)
        return []

    stuck: list[dict] = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid_s, stat, etimes_s, comm, args = parts
        if not stat.startswith("D"):
            continue
        if "node" not in comm.lower() and "node" not in args.lower():
            continue
        try:
            elapsed = int(etimes_s)
        except ValueError:
            continue
        if elapsed >= min_seconds:
            stuck.append({"pid": int(pid_s), "elapsed_s": elapsed,
                          "args": args[:160]})
    return stuck


def check_node_io_uring(db, min_seconds: int = DEFAULT_MIN_SECONDS) -> dict:
    """Emite alerta si hay node atascados; resuelve la alerta cuando se limpian."""
    stuck = find_stuck_node_processes(min_seconds=min_seconds)
    try:
        from api.services.lifecycle_monitor import create_alert
    except Exception as exc:  # noqa: BLE001
        logger.debug("create_alert no disponible: %s", exc)
        return {"stuck": stuck, "alerted": False}

    if stuck:
        create_alert(
            db, entity_type="system", entity_id=0,
            alert_type=ALERT_TYPE, severity="warning",
            title=f"⚠️ {len(stuck)} proceso(s) node atascado(s) en estado D",
            message=(
                "Procesos node en sueño ininterrumpible (posible io_uring). "
                "No se pueden matar; se limpian con un reinicio del host. "
                "No bloquean los deploys, pero conviene reiniciar en ventana segura. "
                + "; ".join(f"pid={p['pid']} ({p['elapsed_s']//60} min): {p['args'][:80]}"
                            for p in stuck[:5])
            ),
            metadata={"stuck": stuck, "min_seconds": min_seconds},
        )
        return {"stuck": stuck, "alerted": True}

    # Sin atascados → resolver la alerta si existía.
    try:
        with db._connect() as conn:
            cur = conn.execute(
                "UPDATE pipeline_alerts SET resolved = 1, resolved_at = datetime('now') "
                "WHERE alert_type = ? AND resolved = 0", (ALERT_TYPE,))
            conn.commit()
            if cur.rowcount:
                logger.info("Watchdog: %d alerta(s) %s resueltas", cur.rowcount, ALERT_TYPE)
    except Exception as exc:  # noqa: BLE001
        logger.debug("resolve %s failed: %s", ALERT_TYPE, exc)
    return {"stuck": [], "alerted": False}
