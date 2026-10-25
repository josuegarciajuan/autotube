#!/usr/bin/env python3
"""Verify the Pollo.ai accounts configured for Autotube.

Modes:
  --probe   (default) Check that each session cookie is still authenticated.
            Costs 0 credits.
  --real    Generate one real image per account (costs 1 credit/account).
  --system  Run one generation through ``AIImageGenerator`` (the same path used
            by thumbnail creation, including dual-account fallback).

Cookies are passed to the worker through a 0600 temp file, never in argv.
The shared status file is NOT modified by this script.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER = REPO_ROOT / "tools" / "pollo_image_worker.py"
SETTINGS = Path(
    os.getenv(
        "POLLO_ACCOUNTS_SETTINGS_PATH",
        "/root/lamamionline-control/data/settings.json",
    )
)


def load_accounts(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    accounts = data.get("pollo_accounts") or []
    if not accounts:
        legacy = str(data.get("pollo_session_cookie", "")).strip()
        if legacy:
            accounts = [{"label": "legacy", "cookie": legacy}]
    return [
        {"label": str(a.get("label", "")).strip(), "cookie": str(a.get("cookie", "")).strip()}
        for a in accounts
        if str(a.get("label", "")).strip() and str(a.get("cookie", "")).strip()
    ]


def _cookie_file(payload: dict) -> str:
    fd, path = tempfile.mkstemp(prefix="pollo_verify_", suffix=".json")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    return path


def _run(cmd: list[str], timeout: int) -> dict:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    payload = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except Exception:
            continue
    if not isinstance(payload, dict):
        payload = {"ok": False, "error": (proc.stderr or proc.stdout or "no output")[-400:]}
    payload["exit_code"] = proc.returncode
    return payload


def probe(account: dict) -> dict:
    path = _cookie_file({"cookie": account["cookie"], "account_label": account["label"]})
    try:
        result = _run(
            [
                sys.executable, str(WORKER), "probe",
                "--cookie-file", path, "--account-label", account["label"],
            ],
            timeout=180,
        )
    finally:
        os.unlink(path)
    result["account"] = account["label"]
    return result


def generate_real(account: dict, out_dir: Path) -> dict:
    out = out_dir / f"{account['label']}.jpg"
    json_out = out_dir / f"{account['label']}.json"
    path = _cookie_file({"cookie": account["cookie"], "account_label": account["label"]})
    try:
        cmd = [
            sys.executable, str(WORKER), "generate",
            "--cookie-file", path,
            "--account-label", account["label"],
            "--prompt",
            "cinematic documentary photograph of an ancient library, dramatic light, no text",
            "--model", os.getenv("POLLO_IMAGE_MODEL", "pollo-image-v2"),
            "--aspect-ratio", "16:9",
            "--num-outputs", "1",
            "--output-image", str(out),
            "--output-json", str(json_out),
            "--timeout", "600",
        ]
        result = _run(cmd, timeout=720)
    finally:
        os.unlink(path)
    result["account"] = account["label"]
    result["image_path"] = str(out) if out.exists() else ""
    return result


def generate_system(out_dir: Path) -> dict:
    code = (
        "import json,sys\n"
        "from pathlib import Path\n"
        "from pipeline.ai_image_generator import AIImageGenerator\n"
        f"out=Path({str(out_dir / 'system.jpg')!r})\n"
        "g=AIImageGenerator()\n"
        "p=g.generate('cinematic documentary photograph of a mysterious corridor, no text', out)\n"
        "print(json.dumps({'ok':True,'image_path':str(p),'primary':g._primary_label,"
        "'fallback':g._fallback_label}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=720,
    )
    payload = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except Exception:
            continue
    if not isinstance(payload, dict):
        payload = {"ok": False, "error": (proc.stderr or proc.stdout or "no output")[-500:]}
    payload["exit_code"] = proc.returncode
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", default=str(SETTINGS))
    parser.add_argument("--real", action="store_true", help="Generate 1 image per account")
    parser.add_argument("--system", action="store_true", help="Also test the system path")
    parser.add_argument("--output-dir", default="/tmp/pollo_verify")
    parser.add_argument("--json", action="store_true", help="Only print the JSON report")
    args = parser.parse_args()

    accounts = load_accounts(Path(args.settings))
    if not accounts:
        print("No hay cuentas Pollo configuradas en %s" % args.settings, file=sys.stderr)
        return 2

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "accounts": []}

    for account in accounts:
        if not args.json:
            print("== %s ==" % account["label"])
        probe_result = probe(account)
        entry = {"account": account["label"], "probe": probe_result}
        if args.real:
            entry["generation"] = generate_real(account, out_dir)
        report["accounts"].append(entry)
        if not args.json:
            print("   probe:", "OK" if probe_result.get("valid") else "FAIL", probe_result.get("error", ""))

    if args.system:
        report["system"] = generate_system(out_dir)

    print(json.dumps(report, ensure_ascii=False, indent=2))

    ok = all(a["probe"].get("valid") for a in report["accounts"])
    if args.real:
        ok = ok and all(a.get("generation", {}).get("ok") for a in report["accounts"])
    if args.system:
        ok = ok and bool(report["system"].get("ok"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
