#!/usr/bin/env python3
"""Reconstruye/transfiere la imagen y reinicia todos los gateways egress.

Uso:
    python3 scripts/sync_egress_gateways.py            # build + transfer + restart + verify
    python3 scripts/sync_egress_gateways.py --skip-build
    python3 scripts/sync_egress_gateways.py --skip-transfer
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "config" / "egress_agents.json"
IMAGE = "autotube-egress:1"


def _load() -> dict:
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _host(slug: str, cfg: dict) -> str:
    if cfg.get("ssh_host"):
        return str(cfg["ssh_host"])
    return urlparse(str(cfg.get("url") or "")).hostname or ""


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--skip-transfer", action="store_true")
    args = ap.parse_args()

    agents = _load()
    if not agents:
        print("No hay gateways en el registro")
        return 0

    if not args.skip_build:
        print("🔨 Construyendo imagen local...")
        r = _run(["bash", str(ROOT / "deploy/egress-node/build.sh")],
                 env={"IMAGE": IMAGE, **__import__("os").environ}, timeout=900)
        if r.returncode != 0:
            print("❌ build falló:", r.stderr[-800:], file=sys.stderr)
            return 1
        print("✅ imagen construida")

    hosts = sorted({h for s, c in agents.items() if (h := _host(s, c))})
    if not args.skip_transfer:
        for host in hosts:
            print(f"📦 Transfiriendo imagen a {host}...")
            save = subprocess.Popen(["docker", "save", IMAGE], stdout=subprocess.PIPE)
            gz = subprocess.Popen(["gzip", "-1"], stdin=save.stdout, stdout=subprocess.PIPE)
            load = _run(["ssh", "-o", "StrictHostKeyChecking=no", f"root@{host}",
                         "gunzip | docker load"], stdin=gz.stdout, timeout=1800)
            save.stdout.close(); gz.stdout.close()
            if load.returncode != 0:
                print(f"❌ transfer falló en {host}: {load.stderr[-500:]}", file=sys.stderr)
                return 1
            print(f"✅ imagen cargada en {host}")

    failed = 0
    for slug, cfg in agents.items():
        host = _host(slug, cfg)
        if not host:
            print(f"⚠️  {slug}: sin ssh_host/url", file=sys.stderr)
            failed += 1
            continue
        r = _run(["ssh", "-o", "StrictHostKeyChecking=no", f"root@{host}",
                  f"systemctl restart egress-{slug}.service && echo OK"], timeout=120)
        if r.returncode != 0 or "OK" not in r.stdout:
            print(f"❌ {slug}: restart falló: {r.stderr[-300:]}", file=sys.stderr)
            failed += 1
            continue
        # Verificación de IP real con reintentos: systemd tarda unos segundos.
        import requests
        url = str(cfg["url"]).rstrip("/") + "/healthz"
        data = {}
        ok = False
        last_err = ""
        for _ in range(12):
            try:
                resp = requests.get(
                    url, headers={"X-Agent-Token": str(cfg.get("token", ""))},
                    timeout=10,
                )
                data = resp.json()
                ok = bool(data.get("ok")) and bool(data.get("egress_ok"))
                if ok:
                    break
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
            time.sleep(5)
        print(f"{'✅' if ok else '❌'} {slug}: {data.get('egress_ip')} "
              f"(esperada {data.get('expected_ip')})")
        if not ok:
            if last_err:
                print(f"   último error: {last_err}", file=sys.stderr)
            failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
