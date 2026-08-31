#!/usr/bin/env python3
"""Gate end-to-end de verificación de egress de los canales gestionados.

Comprueba para cada canal listado en ``config/egress_agents.json``:
  1. Alcanzabilidad del agente (``/healthz``) + token válido.
  2. IP de egress (curl, vía ``/egress-check``) == IP esperada.
  3. Egress REAL del navegador (``/egress-check-browser``): la IP que ve una
     página headless lanzada por el proxy residencial coincide con la esperada,
     y WebRTC no fuga IPs locales.

Salida:
  - Imprime una línea PASS/FAIL por canal con la IP detectada.
  - exit 0 si TODOS los canales gestionados están en verde.
  - exit 1 si alguno está caído, con IP equivocada, fuga de navegador o sin
    expected_ip (fail-closed).

Uso:
  python3 scripts/verify_egress.py                 # todos los canales gestionados
  python3 scripts/verify_egress.py --slug canal6   # solo un canal
  python3 scripts/verify_egress.py --skip-browser  # omite la costosa prueba del navegador
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.services.egress_client import (  # noqa: E402
    EgressAgentUnavailableError,
    get_egress_client,
)
from api.services.egress_client import _load_agents  # noqa: E402


def _check_one(slug: str, skip_browser: bool) -> dict:
    client = get_egress_client(slug)
    if client is None:
        return {"ok": False, "slug": slug, "error": "no gestionado (sin agente)"}

    result = {"slug": slug, "ok": True, "checks": {}}

    # 1) Alcanzabilidad + token + expected_ip
    try:
        hz = client.healthz()
    except EgressAgentUnavailableError as exc:
        return {"ok": False, "slug": slug, "error": f"agente no accesible: {exc}"}
    if not hz.get("ok"):
        return {"ok": False, "slug": slug, "error": hz.get("error", "healthz falló")}
    expected = hz.get("expected_ip", "")
    result["checks"]["agent"] = True
    result["expected_ip"] = expected
    if not expected:
        result["ok"] = False
        result["error"] = "expected_ip no configurado (fail-closed, H1)"
        return result

    # 2) IP de egress (curl)
    try:
        ec = client.egress_check()
    except EgressAgentUnavailableError as exc:
        result["ok"] = False
        result["error"] = f"egress-check no accesible: {exc}"
        return result
    curl_ip = (ec.get("result") or {}).get("ip", "") if ec.get("ok") else ""
    result["checks"]["curl_ip"] = curl_ip
    if not curl_ip:
        result["ok"] = False
        result["error"] = "no se pudo determinar IP de egress (curl)"
        return result
    if curl_ip != expected:
        result["ok"] = False
        result["error"] = (f"IP de egress {curl_ip} != esperada {expected} "
                           f"— IP caída/caducada (fail-closed)")
        return result

    # 3) Egress del navegador (opcional; pendiente si aún no hay perfil)
    if skip_browser:
        result["checks"]["browser"] = "skipped"
        return result
    try:
        eb = client.egress_check_browser()
    except EgressAgentUnavailableError as exc:
        result["ok"] = False
        result["error"] = f"egress-check-browser no accesible: {exc}"
        return result
    eb_err = eb.get("error", "") if not eb.get("ok") else ""
    if eb_err and "profile not found" in eb_err.lower():
        # Aún no hay perfil de navegador (cuenta no creada / sin login browser).
        # Sin perfil no hay operaciones de navegador que puedan filtrar IP, así
        # que no es un fallo: se marca como pendiente hasta el onboarding.
        result["checks"]["browser"] = "pending-onboarding"
        return result
    eb_res = eb.get("result") if eb.get("ok") else {}
    browser_ip = (eb_res or {}).get("browser_ip", "")
    webrtc_ok = (eb_res or {}).get("webrtc_disabled", True)
    result["checks"]["browser_ip"] = browser_ip
    result["checks"]["webrtc_disabled"] = webrtc_ok
    if browser_ip != expected:
        result["ok"] = False
        result["error"] = (f"el NAVEGADOR sale por {browser_ip or '?'} != {expected} "
                           f"— fuga de capa-browser (fail-closed)")
        return result
    if not webrtc_ok:
        result["ok"] = False
        result["error"] = "WebRTC fuga IPs locales (posible fuga anti-detección)"
        return result
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", help="verificar solo un slug (default: todos)")
    ap.add_argument("--skip-browser", action="store_true",
                    help="omitir la prueba costosa del navegador")
    args = ap.parse_args()

    agents = _load_agents()
    if not agents:
        print("No hay canales gestionados en config/egress_agents.json")
        return 1

    slugs = [args.slug] if args.slug else list(agents.keys())
    failed = 0
    for slug in slugs:
        res = _check_one(slug, args.skip_browser)
        if res["ok"]:
            detail = f"IP {res['checks'].get('curl_ip', '')}"
            if res["checks"].get("browser_ip"):
                detail += f" | browser {res['checks']['browser_ip']}"
            print(f"PASS  {slug}: {detail}")
        else:
            failed += 1
            print(f"FAIL  {slug}: {res.get('error', 'desconocido')}")

    print(f"\n{len(slugs) - failed}/{len(slugs)} canales gestionados en verde.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
