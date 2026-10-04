#!/usr/bin/env python3
"""Distribuye la imagen hermética ``autotube-worker:1`` a los nodos de la flota.

Patrón: ``docker save <img> | ssh <nodo> docker load`` + smoke. Solo imagen
determinista (mismo toolchain). No copia código ni datos.

Uso:
    python3 deploy/docker/distribute_worker_runtime.sh [nodo ...]
    # sin args: todos los nodos enabled con tag `render`
"""
from __future__ import annotations

import json
import subprocess
import sys

IMAGE = "autotube-worker:1"
STATE = "/root/superserver/data/state.json"


def _run(cmd, timeout=900):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def main(argv: list[str]) -> int:
    nodes = json.load(open(STATE)).get("nodes", {})
    targets = argv[1:] or [
        n for n, d in nodes.items()
        if d.get("enabled") and "render" in (d.get("tags") or [])
        and d.get("transport") != "local"
    ]
    if not targets:
        print("Sin nodos destino.")
        return 1

    for name in targets:
        d = nodes.get(name) or {}
        ip = d.get("ip")
        user = d.get("user") or "root"
        if not ip:
            print(f"  {name}: sin ip, salto")
            continue
        dst = f"{user}@{ip}"
        print(f"→ {name} ({ip}) ...")
        # docker save | ssh docker load
        p1 = subprocess.Popen(["docker", "save", IMAGE], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=no", dst, "docker load"],
            stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        out, _ = p2.communicate(timeout=1800)
        p1.wait()
        ok = p2.returncode == 0
        # Smoke
        sm = _run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                   dst, f"docker run --rm {IMAGE} ffmpeg -version | head -1"])
        print(f"  {'✅' if ok else '❌'} load | {sm.stdout.strip() or sm.stderr.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
