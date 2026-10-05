#!/usr/bin/env python3
"""Diagnóstico de Pollinations: verifica tier anónimo y con token.

Pollinations puso la API legacy de imagen tras un muro de pago x402: el tier
anónimo sirve ~1 imagen y el resto devuelve 402. Este script comprueba el
estado real y si `POLLINATIONS_TOKEN` restaura el servicio.

Uso:
    python3 scripts/check_pollinations_token.py
    POLLINATIONS_TOKEN=xxxx python3 scripts/check_pollinations_token.py
"""
from __future__ import annotations

import os
import sys
import urllib.parse
from pathlib import Path

import requests

BASE = "https://image.pollinations.ai/prompt"


def _load_env_token() -> str:
    tok = os.getenv("POLLINATIONS_TOKEN", "").strip()
    if tok:
        return tok
    envp = Path(__file__).resolve().parent.parent / ".env"
    if envp.exists():
        for line in envp.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("POLLINATIONS_TOKEN"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def probe(prompt: str, params: dict, headers: dict | None = None):
    url = f"{BASE}/{urllib.parse.quote(prompt)}?" + urllib.parse.urlencode(params)
    try:
        r = requests.get(url, headers=headers or {}, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR {exc}", False
    ct = r.headers.get("content-type", "")
    ok = r.status_code == 200 and ct.startswith("image")
    return f"HTTP {r.status_code} ct={ct} bytes={len(r.content)}", ok


def main() -> int:
    tok = _load_env_token()
    print("Token Pollinations:", (f"configurado ({tok[:6]}…)") if tok else "NO configurado (tier anónimo)")

    print("— Tier anónimo (2 peticiones seguidas) —")
    anon_ok = 0
    for i in range(2):
        msg, ok = probe(f"autotube check {i}", {"width": 512, "height": 512, "model": "flux", "nologo": "true"})
        anon_ok += 1 if ok else 0
        print(f"  req{i + 1}: {msg}")

    if tok:
        print("— Con token (3 peticiones seguidas) —")
        for i in range(3):
            msg, _ = probe(
                f"autotube token check {i}",
                {"width": 512, "height": 512, "model": "flux", "nologo": "true", "token": tok},
                headers={"Authorization": f"Bearer {tok}"},
            )
            print(f"  req{i + 1}: {msg}")
    else:
        print("→ Para evitar el muro x402: regístrate en https://auth.pollinations.ai")
        print("  y exporta POLLINATIONS_TOKEN=... en el .env de Autotube.")

    return 0 if anon_ok > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
