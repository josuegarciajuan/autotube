#!/usr/bin/env python3
"""Alta asistida de un canal con gateway egress propio.

Hace todo lo que depende de software:
  - crea/marca el canal en la DB (egress_managed=1);
  - instala/actualiza el contenedor en el nodo vía SSH (install.sh);
  - escribe la entrada secreta en config/egress_agents.json (0600);
  - guarda el password VNC en /root/.config/autotube/;
  - imprime el túnel y el checklist.

NO crea la cuenta Google/YouTube/GCP: eso lo hace el operador por TightVNC.

Uso:
  python3 scripts/provision_egress_channel.py \
    --slug canal7 --name "Canal 7" \
    --ssh-host 100.90.30.60 --expected-ip 151.80.142.93 \
    --agent-port 9107 --vnc-port 5957 \
    --google-account canal7
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
REGISTRY = ROOT / "config" / "egress_agents.json"
INSTALL_SCRIPT = ROOT / "deploy" / "egress-node" / "install.sh"
VNC_DIR = Path("/root/.config/autotube")


def _load_registry() -> dict:
    if not REGISTRY.exists():
        return {}
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_registry(data: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, REGISTRY)


def _ssh(host: str, command: str, stdin=None, timeout: int = 180):
    return subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"root@{host}", command],
        stdin=stdin, capture_output=True, text=True, timeout=timeout,
    )


def _remote_json(host: str, path: str) -> dict:
    r = _ssh(host, f"cat {path}")
    if r.returncode != 0:
        raise RuntimeError(f"no se pudo leer {path}: {r.stderr[-300:]}")
    return json.loads(r.stdout)


def _tailnet_ip(host: str) -> str:
    r = _ssh(host, "tailscale ip -4 2>/dev/null | head -n1")
    return (r.stdout or "").strip()


def _ensure_channel(slug: str, name: str, expected_ip: str, label: str) -> int:
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    ch = db.get_channel_by_slug(slug)
    if not ch:
        from api.routers.channels import _build_channel_config
        from api.schemas.models import ChannelCreate
        data = ChannelCreate(name=name, slug=slug)
        ch_id = db.create_channel(name, slug, _build_channel_config(data))
        if ch_id is None:
            raise RuntimeError(f"no se pudo crear el canal {slug}")
    else:
        ch_id = int(ch["id"])
    db.set_channel_egress_managed(
        ch_id, True, expected_ip=expected_ip, mode="gateway",
        label=label, status="provisioning",
    )
    return ch_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--ssh-host", required=True)
    ap.add_argument("--expected-ip", required=True)
    ap.add_argument("--agent-port", type=int, required=True)
    ap.add_argument("--vnc-port", type=int, required=True)
    ap.add_argument("--google-account", default="")
    ap.add_argument("--bind-ip", default="auto")
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    slug = args.slug.strip()
    name = args.name.strip() or slug
    label = args.label or f"{args.ssh_host}-{args.expected_ip}"

    if not INSTALL_SCRIPT.exists():
        print(f"❌ Falta {INSTALL_SCRIPT}", file=sys.stderr)
        return 1

    # 1) Canal en DB con flag durable.
    ch_id = _ensure_channel(slug, name, args.expected_ip, label)
    print(f"✅ Canal '{slug}' (id={ch_id}) marcado egress_managed=1")

    # 2) Imagen presente en el nodo.
    r = _ssh(args.ssh_host, "docker image inspect autotube-egress:1 >/dev/null 2>&1 && echo OK || echo MISSING")
    if "OK" not in (r.stdout or ""):
        print("❌ La imagen autotube-egress:1 no está en el nodo. "
              "Constrúyela/transfiérela antes (deploy/egress-node/build.sh).",
              file=sys.stderr)
        return 1

    # 3) Instalar/actualizar el contenedor.
    cmd = [
        "bash", "-s", "--",
        "--slug", slug,
        "--agent-port", str(args.agent_port),
        "--vnc-port", str(args.vnc_port),
        "--expected-ip", args.expected_ip,
        "--google-account", args.google_account or slug,
        "--bind-ip", args.bind_ip,
        "--egress-label", label,
    ]
    with open(INSTALL_SCRIPT, "rb") as fh:
        inst = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", f"root@{args.ssh_host}",
             " ".join(cmd)],
            stdin=fh, capture_output=True, text=True, timeout=300,
        )
    if inst.returncode != 0:
        print("❌ install.sh falló:", file=sys.stderr)
        print(inst.stdout[-2000:], file=sys.stderr)
        print(inst.stderr[-1000:], file=sys.stderr)
        return 1

    # 4) Leer token/IP reales del nodo y escribir el registro.
    cfg_path = f"/opt/autotube-egress/{slug}/agent_config.json"
    remote = _remote_json(args.ssh_host, cfg_path)
    token = str(remote.get("auth_token") or "")
    expected = str(remote.get("expected_ip") or args.expected_ip)
    bind_ip = args.bind_ip
    if bind_ip in ("", "auto"):
        bind_ip = _tailnet_ip(args.ssh_host) or "127.0.0.1"
    url = f"http://{bind_ip}:{args.agent_port}"

    reg = _load_registry()
    reg[slug] = {
        "url": url,
        "token": token,
        "expected_ip": expected,
        "egress_label": label,
        "ssh_host": args.ssh_host,
        "node": args.ssh_host,
    }
    _save_registry(reg)
    os.chmod(REGISTRY, 0o600)
    print(f"✅ Registro actualizado: {slug} -> {url} (expected_ip={expected})")

    # 5) Guardar password VNC (sin imprimirlo).
    m = re.search(r"password:\s*([^\s]+)", inst.stdout or "")
    if m:
        VNC_DIR.mkdir(parents=True, exist_ok=True)
        p = VNC_DIR / f"egress-{slug}-vnc.txt"
        p.write_text(m.group(1) + "\n", encoding="utf-8")
        os.chmod(p, 0o600)
        print(f"✅ Password VNC guardado en {p}")

    print("\n── Siguiente paso (manual, por TightVNC) ──")
    print(f"ssh -L {args.vnc_port}:127.0.0.1:{args.vnc_port} root@{args.ssh_host}")
    print(f"TightVNC -> localhost:{args.vnc_port}")
    print(f"Password: cat {VNC_DIR / f'egress-{slug}-vnc.txt'}")
    print("Dentro del escritorio: /app/deploy/egress-node/navegador.sh")
    print("Verifica https://api.ipify.org == ", expected)
    print("Luego crea Gmail/YouTube/GCP y ejecuta deploy_channel_oauth.py + OAuth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
