#!/usr/bin/env python3
"""One-time login script to generate a persistent browser session for YouTube Studio.

Run on a machine WITH a display. Opens Chromium, you log in manually, session gets saved.

Usage:
    python scripts/yt_browser_login.py --account tracatrack
    python scripts/yt_browser_login.py --account burrianacasa2026

Output: tokens/{account}_browser_session.json
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

TOKENS_DIR = Path(__file__).resolve().parent.parent / "tokens"


def main():
    parser = argparse.ArgumentParser(description="YouTube Studio browser login")
    parser.add_argument(
        "--account", required=True, help="Account name (e.g., tracatrack, burrianacasa2026)"
    )
    args = parser.parse_args()

    account = args.account
    session_file = TOKENS_DIR / f"{account}_browser_session.json"
    user_data_dir = TOKENS_DIR / f"{account}_browser_profile"

    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print("YouTube Studio Browser Login")
    print(f"  Account:         {account}")
    print(f"  Session file:    {session_file}")
    print(f"  Browser profile: {user_data_dir}")
    print(f"{'='*60}\n")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            viewport={"width": 1280, "height": 900},
        )

        page = context.pages[0] if context.pages else context.new_page()

        print("Opening YouTube Studio...")
        try:
            page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"⚠️  Network error reaching YouTube Studio: {e}")
            print("   Make sure you have internet access and can reach studio.youtube.com")
            context.close()
            sys.exit(1)

        page.wait_for_timeout(3000)

        print()
        print(">>> A CHROMIUM WINDOW HAS OPENED <<<")
        print()
        print("  Step 1: Log in with your Google account")
        print("          (email, password, 2FA if enabled)")
        print()
        print("  Step 2: Once you see the YouTube Studio DASHBOARD")
        print("          (the page with 'Panel', 'Contenido', etc. on the left)")
        print()
        print("  Step 3: Come back to this terminal and press ENTER")
        print()

        input("Press ENTER when you're fully logged into YouTube Studio... ")

        # Verify login by checking for key YouTube Studio elements
        print("\nVerifying login state...")
        verified = False
        for selector in [
            "text=Contenido >> visible=true",          # Left nav item
            "tp-yt-paper-item >> text=Contenido",       # Polymer paper item
            "[data-testid='menu-item']",                 # Generic menu item
        ]:
            try:
                page.wait_for_selector(selector, timeout=5000)
                verified = True
                print(f"  ✅ Login confirmed (found: {selector})")
                break
            except Exception:
                continue

        if not verified:
            print("  ⚠️  Could not auto-verify login state.")
            print("  If you're sure you're logged in, the session will still be saved.")

        # Save storage state (cookies + localStorage)
        context.storage_state(path=str(session_file))
        print(f"\n✅ Session saved: {session_file}")
        print()
        print("Next steps:")
        print(f"  1. Copy this file to your server:")
        print(f"     scp {session_file} your-server:/root/autotube/tokens/")
        print()
        print(f"  2. Run the dry-run test on the server:")
        print(f"     python3 scripts/yt_dry_run.py --session {account} --video-id YOUR_VIDEO_ID")

        context.close()


if __name__ == "__main__":
    main()
