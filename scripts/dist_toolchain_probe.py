#!/usr/bin/env python3
"""Sonda de toolchain para el pipeline distribuido (F3).

Inventaría las versiones de ffmpeg relevantes para garantizar que el resultado
distribuido sea **idéntico** al local:

  - control plane: ffmpeg de sistema (concat/transcode) e imageio-ffmpeg (render
    de segmento vía MoviePy).
  - cada nodo de la flota: ffmpeg de sistema, vía SSH (si está accesible).

Salida: tabla + veredicto. Si las versiones difieren de la referencia local,
el contenedor worker DEBE traer el ffmpeg pineado (nunca usar el del host).

Uso:
    python3 scripts/dist_toolchain_probe.py [--state /root/superserver/data/state.json]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or r.stderr).strip().splitlines()
        return out[0].strip() if out else "(sin salida)"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR {exc}"


def _local_toolchain() -> dict:
    info = {"system_ffmpeg": _run(["ffmpeg", "-version"])}
    try:
        import imageio_ffmpeg  # type: ignore
        info["imageio_ffmpeg_version"] = getattr(imageio_ffmpeg, "__version__", "?")
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        info["imageio_ffmpeg_exe"] = exe
        info["imageio_ffmpeg_ffmpeg"] = _run([exe, "-version"])
    except Exception as exc:  # noqa: BLE001
        info["imageio_ffmpeg"] = f"ERROR {exc}"
    return info


def _node_ffmpeg(name: str, node: dict) -> str:
    ip = node.get("ip")
    user = node.get("user") or "root"
    if node.get("transport") == "local":
        return _run(["sh", "-c", "ffmpeg -version 2>/dev/null | head -1"])
    if not ip:
        return "(sin ip)"
    return _run([
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        "-o", "StrictHostKeyChecking=no",
        f"{user}@{ip}", "ffmpeg -version 2>/dev/null | head -1",
    ], timeout=20)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sonda de toolchain ffmpeg local vs flota")
    ap.add_argument("--state", default="/root/superserver/data/state.json")
    args = ap.parse_args()

    local = _local_toolchain()
    print("=== CONTROL PLANE (referencia local) ===")
    print("system ffmpeg :", local.get("system_ffmpeg", "?"))
    print("imageio-ffmpeg:", local.get("imageio_ffmpeg_version", "?"))
    print("imageio bin   :", local.get("imageio_ffmpeg_ffmpeg", "?"))

    ref_sys = local.get("system_ffmpeg", "")

    print("\n=== FLOTA ===")
    try:
        state = json.load(open(args.state))
    except Exception as exc:  # noqa: BLE001
        print(f"No pude leer {args.state}: {exc}")
        return 1

    mismatched = 0
    for name, node in state.get("nodes", {}).items():
        if not node.get("enabled"):
            continue
        ver = _node_ffmpeg(name, node)
        same = "(= local)" if ver[:40] == ref_sys[:40] else "(DISTINTO)"
        if same == "(DISTINTO)":
            mismatched += 1
        print(f"  {name:14} {ver[:90]} {same}")

    print("\n=== VEREDICTO ===")
    print(f"ffmpeg del host en {mismatched} nodo(s) distinto(s) del local.")
    print("=> El contenedor worker DEBE traer el ffmpeg PINeado (no usar el del host) "
          "para garantizar identidad bit-a-bit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
