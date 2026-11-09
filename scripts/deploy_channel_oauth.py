#!/usr/bin/env python3
"""Copia el client_secret de un canal SOLO al nodo de su gateway.

Uso:
    python3 scripts/deploy_client_secret.py --slug canal6 --file client_secret_canal6.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "config" / "egress_agents.json"


def _host_for(slug: str, cfg: dict) -> str:
    if cfg.get("ssh_host"):
        return str(cfg["ssh_host"])
    return urlparse(str(cfg.get("url") or "")).hostname or ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--file", required=True)
    args = ap.parse_args()

    try:
        reg = json.loads(REGISTRY.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        print(f"❌ No se pudo leer {REGISTRY}: {exc}", file=sys.stderr)
        return 1
    cfg = reg.get(args.slug)
    if not cfg:
        print(f"❌ {args.slug} no está en {REGISTRY}", file=sys.stderr)
        return 1
    host = _host_for(args.slug, cfg)
    if not host:
        print("❌ No hay ssh_host/url para el nodo", file=sys.stderr)
        return 1

    src = Path(args.file)
    if not src.exists():
        print(f"❌ No existe {src}", file=sys.stderr)
        return 1

    remote_dir = f"/opt/autotube-egress/{args.slug}"
    remote_file = f"{remote_dir}/client_secret_{args.slug}.json"
    r = subprocess.run(
        ["scp", "-o", "StrictHostKeyChecking=no", str(src),
         f"root@{host}:{remote_file}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print("❌ scp falló:", r.stderr[-500:], file=sys.stderr)
        return 1
    r = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"root@{host}",
         f"chown 1000:1000 {remote_file} && chmod 600 {remote_file} && "
         f"systemctl restart egress-{args.slug}.service && echo OK"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or "OK" not in r.stdout:
        print("❌ No se pudo instalar/reiniciar:", r.stderr[-500:], file=sys.stderr)
        return 1
    print(f"✅ client_secret desplegado en {host}:{remote_file} y gateway reiniciado")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
