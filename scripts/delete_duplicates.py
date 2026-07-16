#!/usr/bin/env python3
"""Delete duplicate YouTube videos created by the upload retry bug.

Bug: orchestrator.py phase_upload crashed after successful upload
     (script.get('id') on None), causing the same video to be
     re-uploaded 11-13 times.

This script cleans up the duplicates, keeping only the canonical
YouTube IDs assigned to videos #322 and #323.

Usage:
    python3 scripts/delete_duplicates.py --dry-run    # preview only
    python3 scripts/delete_duplicates.py --execute     # actually delete
"""

import argparse
import logging
import pickle
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger("delete_duplicates")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TOKEN_PATH = Path("tokens/canal2.pickle")

# Canonical IDs — KEEP these
KEEP = {"iaOMXoqxLos", "PYLOsZF9TTc"}

# Duplicates to DELETE
DUPLICATES = {
    # "6 de junio de 1944" (video #322) — keep iaOMXoqxLos
    "STA5ZKgvZVA": "6 de junio de 1944",
    "keFWbnWv-vY": "6 de junio de 1944",
    "TmVN3EGVRKc": "6 de junio de 1944",
    "CeKQHNStCOo": "6 de junio de 1944",
    "_FwIaINDagM": "6 de junio de 1944",
    "q3Iy1A4PcYo": "6 de junio de 1944",
    "phsOBjV76E8": "6 de junio de 1944",
    "5I4p64RlwAQ": "6 de junio de 1944",
    "UT6rOhppsWc": "6 de junio de 1944",
    "VZsV7T33Ymg": "6 de junio de 1944",
    "NuAJlHX4VDw": "6 de junio de 1944",
    # "Sueños que continúan..." (video #323) — keep PYLOsZF9TTc
    "YeJDIbyQ_MQ": "Sueños que continúan...",
    "GxJWOLEbXnw": "Sueños que continúan...",
}


def authenticate() -> "google.auth.credentials.Credentials":
    """Load canal2 token or fail."""
    if not TOKEN_PATH.exists():
        logger.error("Token not found: %s", TOKEN_PATH)
        sys.exit(1)

    with open(TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)

    if creds.expired and creds.refresh_token:
        logger.info("Token expired — refreshing...")
        creds.refresh(Request())
        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)
        logger.info("Token refreshed and saved.")

    if not creds.valid:
        logger.error("Token invalid — re-authentication required.")
        sys.exit(1)

    logger.info("Authenticated successfully (scopes: %s)", creds.scopes)
    return creds


def verify_video(service, video_id: str) -> dict | None:
    """Check if a video exists and return its snippet. Returns None if not found."""
    try:
        resp = service.videos().list(part="snippet,status", id=video_id).execute()
        items = resp.get("items", [])
        if not items:
            return None
        item = items[0]
        return {
            "title": item["snippet"]["title"],
            "privacy": item["status"]["privacyStatus"],
        }
    except HttpError as exc:
        if exc.resp.status == 404:
            return None
        raise


def run(dry_run: bool = True) -> dict:
    """Verify and optionally delete duplicate videos.

    Returns {"deleted": int, "failed": int, "not_found": int}.
    """
    creds = authenticate()
    service = build("youtube", "v3", credentials=creds)

    # ── Step 1: Verify all IDs ──────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Verificando {len(DUPLICATES)} videos duplicados...")
    print(f"{'=' * 60}\n")

    to_delete = []
    not_found = []

    for vid, origin in DUPLICATES.items():
        info = verify_video(service, vid)
        if info is None:
            print(f"  ❓ {vid} — NO ENCONTRADO en YouTube (quizás ya borrado)")
            not_found.append(vid)
        else:
            print(f"  📹 {vid} — \"{info['title'][:70]}\" [{info['privacy']}]")
            to_delete.append(vid)

    if not_found:
        print(f"\n{len(not_found)} videos ya no existen en YouTube — se omiten.")

    if not to_delete:
        print("\n✅ No hay duplicados que borrar. Todo limpio.")
        return {"deleted": 0, "failed": 0, "not_found": len(not_found)}

    # ── Step 2: Confirm ─────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Se borrarán {len(to_delete)} videos duplicados:")
    for vid in to_delete:
        print(f"    {vid}")
    print(f"\nSe CONSERVAN (NO se tocan):")
    for vid in sorted(KEEP):
        print(f"    {vid}")
    print(f"{'=' * 60}")

    if dry_run:
        print("\n🔍 MODO DRY-RUN: no se borró nada. Usa --execute para borrar.")
        return {"deleted": 0, "failed": 0, "not_found": len(not_found)}

    response = input("\n⚠️  ¿Confirmas borrar estos {n} videos? (escribe 'si'): ".format(n=len(to_delete)))
    if response.strip().lower() != "si":
        print("Cancelado.")
        return {"deleted": 0, "failed": 0, "not_found": len(not_found)}

    # ── Step 3: Delete ──────────────────────────────────────────
    deleted = 0
    failed = 0

    for vid in to_delete:
        try:
            service.videos().delete(id=vid).execute()
            print(f"  ✅ {vid} — borrado")
            deleted += 1
        except HttpError as exc:
            print(f"  ❌ {vid} — error: {exc}")
            failed += 1

    # ── Step 4: Verify canonical IDs still exist ─────────────────
    print(f"\n{'=' * 60}")
    print("Verificando videos canónicos...")
    for vid in sorted(KEEP):
        info = verify_video(service, vid)
        if info:
            print(f"  ✅ {vid} — \"{info['title'][:70]}\" [{info['privacy']}] — INTACTO")
        else:
            print(f"  ❌ {vid} — ¡NO ENCONTRADO! (¿borrado por error?)")

    print(f"\n{'=' * 60}")
    print(f"Resultado: {deleted} borrados, {failed} fallos, {len(not_found)} ya inexistentes")
    print(f"{'=' * 60}")

    return {"deleted": deleted, "failed": failed, "not_found": len(not_found)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Delete duplicate YouTube videos")
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually delete videos (default: dry-run only)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Preview without deleting (default)",
    )
    args = parser.parse_args()

    dry_run = not args.execute
    run(dry_run=dry_run)
