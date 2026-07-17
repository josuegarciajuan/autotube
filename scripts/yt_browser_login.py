#!/usr/bin/env python3
"""One-time login script to generate a persistent browser session for YouTube Studio.

Opens Chromium, user logs in manually, session gets auto-saved when login detected.

Usage:
    DISPLAY=:99 python3 scripts/yt_browser_login.py --account tracatrack
    DISPLAY=:99 python3 scripts/yt_browser_login.py --account burrianacasa2026

Output: tokens/{account}_browser_session.json
"""

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

TOKENS_DIR = Path(__file__).resolve().parent.parent / "tokens"


def main():
    parser = argparse.ArgumentParser(description="YouTube Studio browser login")
    parser.add_argument(
        "--account", required=True,
        help="Account name (e.g., tracatrack, burrianacasa2026)"
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
            print(f"⚠️  Network error: {e}")
            context.close()
            sys.exit(1)

        page.wait_for_timeout(3000)

        print("\n>>> CHROME WINDOW OPENED <<<")
        print("    Log in with your Google account (email, password, 2FA)")
        print("    Script will auto-detect when you reach YouTube Studio dashboard...\n")

        # Auto-detect login: poll URL + dashboard element
        print("Waiting for login... (max 10 minutes)")
        logged_in = False
        for i in range(300):  # 300 * 2s = 10 min
            url = page.url
            if "studio.youtube.com" in url and "accounts.google.com" not in url:
                # Check for dashboard elements
                for selector in [
                    "text=Panel",
                    "text=Contenido",
                    "[id='menu-item']",
                    "ytd-guide-entry-renderer",
                ]:
                    try:
                        el = page.query_selector(selector)
                        if el and el.is_visible():
                            logged_in = True
                            print(f"\n  ✅ Login detected (found: '{selector}')")
                            break
                    except Exception:
                        continue
                if logged_in:
                    break
            # Progress indicator every 15 seconds
            if i % 15 == 0 and i > 0:
                print(f"  ⏳ Still waiting... ({i * 2}s elapsed)")
            time.sleep(2)

        if not logged_in:
            print("\n⚠️  Auto-detection timed out (10 min). Saving session anyway...")

        # Extra wait for page to fully render
        page.wait_for_timeout(3000)

        # Save storage state (cookies + localStorage)
        context.storage_state(path=str(session_file))
        print(f"\n✅ Session saved: {session_file}")
        print(f"\nNow run the dry-run test:")
        print(f"  python3 scripts/yt_dry_run.py --session {account} --video-id YOUR_VIDEO_ID")

        context.close()


if __name__ == "__main__":
    main()
