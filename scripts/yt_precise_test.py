#!/usr/bin/env python3
"""Targeted test: click Sí radio by NAME attribute, verify aria-checked change, then Guardar."""

import json
import sys
import time
import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKENS_DIR = PROJECT_ROOT / "tokens"
RADIO_YES = '[name="VIDEO_HAS_ALTERED_CONTENT_YES"]'
RADIO_NO = '[name="VIDEO_HAS_ALTERED_CONTENT_NO"]'

def get_radio_state(page):
    """Return aria-checked state of both radios."""
    yes = page.query_selector(RADIO_YES)
    no = page.query_selector(RADIO_NO)
    return {
        "yes_checked": yes.get_attribute("aria-checked") if yes else "NOT FOUND",
        "no_checked": no.get_attribute("aria-checked") if no else "NOT FOUND",
    }

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="Browser session name")
    parser.add_argument("--video-id", required=True, help="YouTube video ID")
    args = parser.parse_args()
    session_file = TOKENS_DIR / f"{args.session}_browser_session.json"
    if not session_file.exists():
        print(f"Session not found: {session_file}")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-gpu", "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            storage_state=str(session_file),
            locale="es-ES",
        )
        page = ctx.new_page()

        # 1. Navigate to editor
        edit_url = f"https://studio.youtube.com/video/{args.video_id}/edit"
        print(f"[1] Opening: {edit_url}")
        page.goto(edit_url, wait_until="commit", timeout=60000)
        page.wait_for_timeout(6000)

        # 2. Scroll to bottom & click "Mostrar más"
        print("[2] Scrolling...")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)

        for sel in ["text=Mostrar más", "button:has-text('Mostrar más')"]:
            try:
                el = page.wait_for_selector(sel, timeout=3000)
                if el and el.is_visible():
                    el.click()
                    print(f"[3] Clicked: {sel}")
                    break
            except PlaywrightTimeout:
                continue
        page.wait_for_timeout(3000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)

        # 4. Check initial state
        initial = get_radio_state(page)
        print(f"\n[4] INITIAL STATE: yes={initial['yes_checked']}, no={initial['no_checked']}")

        # 5. Click "Sí" radio
        yes_radio = page.query_selector(RADIO_YES)
        if not yes_radio:
            print("ERROR: Radio 'Sí' not found!")
            page.screenshot(path="/tmp/error_no_radio.png")
            browser.close()
            sys.exit(1)

        print(f"[5] Clicking 'Sí' radio (name={yes_radio.get_attribute('name')})")
        yes_radio.click(timeout=5000)
        page.wait_for_timeout(2000)

        # 6. Check state after click
        after_click = get_radio_state(page)
        print(f"[6] AFTER CLICK:  yes={after_click['yes_checked']}, no={after_click['no_checked']}")

        if after_click['yes_checked'] != 'true':
            print("⚠️  Radio 'Sí' aria-checked did NOT change to 'true'!")
            page.screenshot(path="/tmp/error_no_change.png")
        else:
            print("✅ Radio 'Sí' aria-checked changed to 'true'!")

        # 7. Wait for Guardar to enable
        print("[7] Waiting for Guardar to enable...")
        guardar_clicked = False
        for sel in [
            "button:has-text('Guardar'):not([disabled])",
            "ytcp-button:has-text('Guardar'):not([disabled])",
        ]:
            for attempt in range(20):
                el = page.query_selector(sel)
                if el and el.is_enabled():
                    print(f"    Guardar enabled after {attempt*2}s, selector={sel}")
                    el.click(timeout=5000)
                    guardar_clicked = True
                    break
                time.sleep(2)
            if guardar_clicked:
                break

        if guardar_clicked:
            print("[8] ✅ Clicked Guardar")
            page.wait_for_timeout(4000)
            
            # Check for save confirmation
            for confirm_sel in ["text=Guardado", "text=Se ha guardado", "text=Saved"]:
                try:
                    el = page.wait_for_selector(confirm_sel, timeout=3000)
                    if el:
                        print(f"[9] ✅ Save confirmed: {confirm_sel}")
                        break
                except PlaywrightTimeout:
                    continue
        else:
            print("[8] ❌ Guardar never enabled!")
            page.screenshot(path="/tmp/error_guardar_disabled.png")

        # Final state check
        final = get_radio_state(page)
        print(f"\n[FINAL] yes={final['yes_checked']}, no={final['no_checked']}")

        page.screenshot(path="/tmp/final_state.png")
        print("\nScreenshots: /tmp/final_state.png")
        browser.close()

if __name__ == "__main__":
    main()
