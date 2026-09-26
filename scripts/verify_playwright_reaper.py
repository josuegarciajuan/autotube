#!/usr/bin/env python3
"""Verificación del ciclo de vida de Playwright y del reaper de drivers.

Tres modos:

  --count                 Cuenta los drivers ``run-driver`` hijos de un PID
                          (por defecto este script). Solo lee /proc.

  --simulate-stale        Spawnea un falso driver (argv contiene ``run-driver``)
                          como hijo de ESTE script y comprueba que el reaper lo
                          mata cuando se viola el TTL (ttl=0). Después spawnea
                          otro y comprueba que NO se mata con TTL normal (>1h);
                          lo termina a mano. Seguro: el reaper solo mira hijos
                          de este PID.

  --cycles N --account A  Ejecuta N ciclos de la ruta de sesión de navegador
                          (``check_session_valid``) con timeout corto y compara
                          el número de drivers hijos antes/después. Debe NO
                          crecer. Requiere que exista el perfil del account.

Nunca mata drivers recientes ni de otros PIDs fuera del modo --simulate-stale,
donde el objetivo es un proceso falso creado por el propio script.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.playwright_reaper import (  # noqa: E402
    list_driver_children,
    reap_once,
)


def count_drivers(parent_pid: int | None = None) -> int:
    return len(list_driver_children(parent_pid))


def _spawn_fake_driver() -> subprocess.Popen:
    """Fake Node driver: argv contains ``run-driver``, sleeps quietly."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", "run-driver"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_visible(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(c["pid"] == pid for c in list_driver_children()):
            return True
        time.sleep(0.1)
    return False


def cmd_count(args) -> int:
    parent = args.parent_pid or os.getpid()
    drivers = list_driver_children(parent)
    print(f"PID padre: {parent}")
    print(f"Drivers run-driver hijos: {len(drivers)}")
    for d in drivers:
        age = d.get("age_seconds")
        print(f"  pid={d['pid']} age={age:.0f}s" if age is not None else f"  pid={d['pid']}")
    return 0


def cmd_simulate_stale(args) -> int:
    os.environ["PLAYWRIGHT_REAPER_ENABLED"] = "true"
    parent = os.getpid()
    failures = 0

    # ── 1. TTL violado (ttl=0) → debe morir ──
    stale = _spawn_fake_driver()
    if not _wait_for_visible(stale.pid):
        print(f"[FAIL] el driver simulado {stale.pid} no apareció en /proc")
        return 1
    stats = reap_once(ttl=0, parent_pid=parent, registry_entries=[])
    time.sleep(0.3)
    alive = stale.poll() is None
    if stale.pid in stats.get("killed_pids", []) and not alive:
        print(f"[OK] reaper mató el driver con TTL violado (pid={stale.pid})")
    else:
        print(f"[FAIL] el reaper NO mató el driver con TTL violado (pid={stale.pid}, stats={stats})")
        failures += 1
        try:
            stale.kill()
        except Exception:
            pass

    # ── 2. TTL holgado → NO debe morir ──
    fresh = _spawn_fake_driver()
    if not _wait_for_visible(fresh.pid):
        print(f"[FAIL] el driver simulado {fresh.pid} no apareció en /proc")
        return 1
    stats2 = reap_once(ttl=3600, parent_pid=parent, registry_entries=[])
    time.sleep(0.2)
    alive2 = fresh.poll() is None
    if alive2 and fresh.pid not in stats2.get("killed_pids", []):
        print(f"[OK] reaper respetó el driver reciente (pid={fresh.pid}, ttl=3600)")
    else:
        print(f"[FAIL] el reaper mató un driver reciente (pid={fresh.pid}, stats={stats2})")
        failures += 1
    try:
        fresh.terminate()
        fresh.wait(timeout=5)
    except Exception:
        try:
            fresh.kill()
        except Exception:
            pass

    return 1 if failures else 0


def cmd_cycles(args) -> int:
    from pipeline.youtube_browser import check_session_valid

    parent = os.getpid()
    before = count_drivers(parent)
    print(f"Drivers antes: {before}")
    failures = 0
    for i in range(1, args.cycles + 1):
        try:
            asyncio.run(asyncio.wait_for(
                check_session_valid(args.account, cache_seconds=0),
                timeout=args.cycle_timeout,
            ))
        except Exception as exc:  # noqa: BLE001
            print(f"  ciclo {i}/{args.cycles}: error/timeout ({exc.__class__.__name__}: {exc})")
            failures += 1
        mid = count_drivers(parent)
        print(f"  ciclo {i}/{args.cycles}: drivers hijos={mid}")
    after = count_drivers(parent)
    print(f"Drivers después: {after}")
    if after > before:
        print(f"[FAIL] fuga: {before} → {after} drivers tras {args.cycles} ciclos")
        return 1
    print(f"[OK] sin crecimiento de drivers ({before} → {after})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", action="store_true")
    parser.add_argument("--simulate-stale", action="store_true")
    parser.add_argument("--cycles", type=int, default=0)
    parser.add_argument("--account", default=None)
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--cycle-timeout", type=float, default=60.0)
    args = parser.parse_args()

    if args.simulate_stale:
        return cmd_simulate_stale(args)
    if args.cycles:
        if not args.account:
            parser.error("--cycles requiere --account")
        return cmd_cycles(args)
    return cmd_count(args)


if __name__ == "__main__":
    raise SystemExit(main())
