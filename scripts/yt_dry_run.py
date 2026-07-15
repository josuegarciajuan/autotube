#!/usr/bin/env python3
"""
DRY-RUN: YouTube Studio navigation test — NO MODIFICATIONS MADE.

Loads a saved browser session, navigates YouTube Studio following the exact
path to the "Altered Content / Uso de IA" section, and logs every element found.

Usage:
    python3 scripts/yt_dry_run.py --session tracatrack --video-id "abc123XYZ"

What it does:
    1. Starts Xvfb (virtual display :99)
    2. Launches Chromium + stealth + loads saved session
    3. Navigates: Studio → Contenido → Videos → hover video → Edit
    4. Scrolls → "Mostrar más" → finds "Uso de IA" section
    5. Logs all elements found — DOES NOT click "Sí", "No", or "Guardar"
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKENS_DIR = PROJECT_ROOT / "tokens"
SCREENSHOTS_DIR = PROJECT_ROOT / "logs" / "dry_run_screenshots"

RESULTS = []
START_TIME = None
XVFB_PROC = None

# ── helpers ──────────────────────────────────────────────────────────────────

def log(step: int, status: str, msg: str, selector: str = "", screenshot: str = ""):
    ts = datetime.now().strftime("%H:%M:%S")
    icon = {"OK": "✅", "FAIL": "❌", "SKIP": "⏭️", "WARN": "⚠️", "INFO": "ℹ️"}.get(status, "•")
    entry = {
        "step": step,
        "status": status,
        "message": msg,
        "selector": selector,
        "screenshot": screenshot,
        "time": ts,
    }
    RESULTS.append(entry)
    print(f"  [{ts}] {icon} Step {step:02d}: {msg}")
    if selector:
        print(f"          Selector: {selector}")

def screenshot(page, name: str) -> str:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOTS_DIR / f"{START_TIME.strftime('%Y%m%d_%H%M%S')}_{name}.png"
    page.screenshot(path=str(path))
    return str(path)

def find_element(page, selectors: list[str], timeout: int = 5000) -> tuple:
    """Try multiple selectors, return (found, selector_used, element)."""
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=timeout, state="attached")
            return (True, sel, el)
        except PlaywrightTimeout:
            continue
    return (False, selectors[0], None)

def cleanup():
    global XVFB_PROC
    if XVFB_PROC:
        XVFB_PROC.terminate()
        try:
            XVFB_PROC.wait(timeout=3)
        except subprocess.TimeoutExpired:
            XVFB_PROC.kill()
        XVFB_PROC = None

def signal_handler(sig, frame):
    print("\nInterrupted. Cleaning up...")
    cleanup()
    sys.exit(1)

# ── main navigation ──────────────────────────────────────────────────────────

def navigate_studio(page, video_id: str):
    """Navigate YouTube Studio to the video editor, logging every step."""
    step = 0

    # --- Step 1: Open YouTube Studio ---
    step += 1
    try:
        page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        log(step, "OK", "Navigated to studio.youtube.com",
            screenshot=screenshot(page, "step01_studio_home"))
    except Exception as e:
        log(step, "FAIL", f"Cannot reach studio.youtube.com: {e}")
        return

    # --- Step 2: Verify we're logged in (left nav visible) ---
    step += 1
    found, sel, el = find_element(page, [
        "text=Contenido >> visible=true",
        "a[href*='/videos']",
        "[id='menu-item']",
        "ytd-guide-entry-renderer",
    ])
    if found:
        log(step, "OK", "Session loaded — left navigation visible", selector=sel,
            screenshot=screenshot(page, "step02_logged_in"))
    else:
        log(step, "FAIL", "Not logged in or YouTube Studio UI changed. Session may be expired.",
            screenshot=screenshot(page, "step02_not_logged_in"))
        return

    # --- Step 3: Click "Contenido" in left menu ---
    step += 1
    found, sel, _ = find_element(page, [
        "text=Contenido >> visible=true",
        "a[href='/channel/']:has-text('Contenido')",
        "[role='navigation'] text=Contenido",
        "tp-yt-paper-item >> text=Contenido",
    ])
    if found:
        try:
            page.click(sel, timeout=5000)
            page.wait_for_timeout(3000)
            log(step, "OK", "Clicked 'Contenido' in left menu", selector=sel,
                screenshot=screenshot(page, "step03_contenido"))
        except Exception as e:
            log(step, "FAIL", f"Click on 'Contenido' failed: {e}", selector=sel)
            return
    else:
        log(step, "FAIL", "Cannot find 'Contenido' in left menu")
        return

    # --- Step 4: Locate "Videos" tab in submenu ---
    step += 1
    found, sel, _ = find_element(page, [
        "text=Videos >> visible=true",
        "[role='tab']:has-text('Videos')",
        "tp-yt-paper-tab >> text=Videos",
        "a[href*='/videos']",
    ])
    if found:
        try:
            # Check if we need to click it (sometimes already selected)
            el = page.query_selector(sel)
            is_selected = el.get_attribute("aria-selected") == "true" if el else False
            if not is_selected:
                page.click(sel, timeout=5000)
                page.wait_for_timeout(2000)
            log(step, "OK", f"Located 'Videos' tab (selected: {is_selected})", selector=sel,
                screenshot=screenshot(page, "step04_videos_tab"))
        except Exception as e:
            log(step, "WARN", f"Click 'Videos' tab (optional): {e}", selector=sel)
    else:
        log(step, "WARN", "Cannot find 'Videos' tab — continuing anyway",
            screenshot=screenshot(page, "step04_no_videos_tab"))

    page.wait_for_timeout(2000)

    # --- Step 5: Locate the video in the list ---
    step += 1
    found, sel, _ = find_element(page, [
        f"a[href*='{video_id}']",
        f"ytcp-video-row:has(a[href*='{video_id}'])",
        f"[data-video-id='{video_id}']",
        f"tr:has(a[href*='{video_id}'])",
        "ytcp-video-list-cell:first-child",
    ])
    if found:
        log(step, "OK", f"Video {video_id} found in list", selector=sel,
            screenshot=screenshot(page, "step05_video_found"))
    else:
        # Fallback: just use the first video in the list
        found2, sel2, _ = find_element(page, [
            "ytcp-video-list-cell",
            "ytcp-video-row",
            "[id='video-list'] tr",
            "a[id='video-title']",
        ])
        if found2:
            log(step, "WARN", f"Video {video_id} not found by ID, using first video in list",
                selector=sel2, screenshot=screenshot(page, "step05_first_video"))
            sel = sel2
        else:
            log(step, "FAIL", "No videos found in list — is the channel empty?",
                screenshot=screenshot(page, "step05_no_videos"))
            return

    # --- Step 6: Hover over video to reveal action buttons ---
    step += 1
    target = page.query_selector(sel)
    if target:
        box = target.bounding_box()
        if box:
            page.mouse.move(box["x"] + 50, box["y"] + box["height"] / 2)
            page.wait_for_timeout(1000)
            log(step, "OK", f"Hovered over video at ({box['x']:.0f}, {box['y']:.0f})",
                screenshot=screenshot(page, "step06_hover"))
        else:
            log(step, "WARN", "Could not get bounding box — trying hover anyway",
                screenshot=screenshot(page, "step06_hover_fallback"))
    else:
        log(step, "FAIL", "Video element disappeared from DOM",
            screenshot=screenshot(page, "step06_element_gone"))
        return

    # --- Step 7: Find and click the "Edit" button (first of 5 hover buttons) ---
    step += 1
    found, sel, _ = find_element(page, [
        "[aria-label='Editar']",
        "[aria-label='Edit']",
        "button[aria-label*='ditar']",
        "ytcp-icon-button[icon='pencil']",
        "[test-id='video-row-edit-button']",
        "ytcp-video-list-cell button:first-child",
    ])
    if found:
        log(step, "OK", "Found 'Edit' button on hover toolbar", selector=sel)
        # DO NOT click — just log it
        screenshot(page, "step07_edit_button")
    else:
        log(step, "FAIL", "Edit button not found on hover",
            screenshot=screenshot(page, "step07_no_edit_button"))
        return

    # --- Step 8: Navigate directly to video editor page ---
    step += 1
    edit_url = f"https://studio.youtube.com/video/{video_id}/edit"
    try:
        page.goto(edit_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)
        log(step, "OK", f"Navigated to video editor: {edit_url}",
            screenshot=screenshot(page, "step08_editor"))
    except Exception as e:
        log(step, "FAIL", f"Cannot open editor page: {e}")
        return

    # --- Step 9: Wait for editor to fully load ---
    step += 1
    found, sel, _ = find_element(page, [
        "[id='title-textarea']",
        "[aria-label='Título']",
        "[id='textbox']",
        "textarea",  # any textarea on page
    ])
    if found:
        log(step, "OK", "Video editor loaded (title field detected)", selector=sel,
            screenshot=screenshot(page, "step09_editor_loaded"))
    else:
        log(step, "WARN", "Editor loaded but title field not found — continuing",
            screenshot=screenshot(page, "step09_editor_maybe_loaded"))

    # --- Step 10: Scroll to the bottom of the page ---
    step += 1
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)
        log(step, "OK", "Scrolled to bottom of editor page",
            screenshot=screenshot(page, "step10_scrolled"))
    except Exception as e:
        log(step, "WARN", f"Scroll failed: {e}",
            screenshot=screenshot(page, "step10_scroll_fail"))

    # --- Step 11: Click "Mostrar más" / "Show more" ---
    step += 1
    found, sel, _ = find_element(page, [
        "text=Mostrar más",
        "text=Show more",
        "button:has-text('Mostrar más')",
        "button:has-text('Show more')",
        "[aria-label='Mostrar más']",
        "[aria-label='Show more']",
    ])
    if found:
        # Log the button but DO NOT click
        log(step, "OK", "Found 'Mostrar más' (Show more) button", selector=sel,
            screenshot=screenshot(page, "step11_show_more"))
    else:
        # It might already be expanded — check if "Uso de IA" is already visible
        found2, sel2, _ = find_element(page, [
            "text=Uso de IA",
            "text=Altered content",
            "text=Contenido alterado",
            "text=sintético",
        ], timeout=2000)
        if found2:
            log(step, "SKIP", "'Mostrar más' not needed — content already expanded",
                screenshot=screenshot(page, "step11_already_expanded"))
        else:
            log(step, "FAIL", "'Mostrar más' button not found and content not expanded",
                screenshot=screenshot(page, "step11_not_found"))

    # --- Step 12: Find "Uso de IA" section ---
    step += 1
    found, sel, _ = find_element(page, [
        "text=Uso de IA",
        "text=Altered content",
        "text=Contenido alterado o sintético",
        "text=Contenido alterado",
        "label:has-text('altered')",
        "label:has-text('alterado')",
        "label:has-text('sintético')",
    ], timeout=3000)
    if found:
        log(step, "OK", "Found 'Uso de IA' / 'Altered content' section", selector=sel,
            screenshot=screenshot(page, "step12_uso_ia"))
    else:
        # Try broader search
        log(step, "FAIL", "Could not find 'Uso de IA' section",
            screenshot=screenshot(page, "step12_uso_ia_not_found"))
        return

    # --- Step 13: Find "Sí" / "Yes" radio/button ---
    step += 1
    found, sel, _ = find_element(page, [
        "text=Sí >> visible=true",
        "text=Yes >> visible=true",
        "label:has-text('Sí')",
        "label:has-text('Yes')",
        "input[value='yes']",
        "input[value='true']",
        "[role='radio'][aria-label*='Sí']",
        "[role='radio'][aria-label*='Yes']",
    ], timeout=3000)
    if found:
        log(step, "OK", "Found 'Sí' / 'Yes' option", selector=sel,
            screenshot=screenshot(page, "step13_si_yes"))
    else:
        log(step, "FAIL", "Could not find 'Sí' / 'Yes' option",
            screenshot=screenshot(page, "step13_no_si"))

    # --- Step 14: Find "No" radio/button ---
    step += 1
    found, sel, _ = find_element(page, [
        "text=No >> visible=true",
        "label:has-text('No')",
        "input[value='no']",
        "input[value='false']",
        "[role='radio'][aria-label*='No']",
    ], timeout=3000)
    if found:
        log(step, "OK", "Found 'No' option", selector=sel)
    else:
        log(step, "WARN", "Could not find 'No' option")

    # --- Step 15: Find "Guardar" / "Save" button (top right) ---
    step += 1
    found, sel, _ = find_element(page, [
        "text=Guardar >> visible=true",
        "text=SAVE >> visible=true",
        "text=Save >> visible=true",
        "button:has-text('Guardar')",
        "button:has-text('SAVE')",
        "button:has-text('Save')",
        "[aria-label='Guardar']",
        "ytcp-button:has-text('Guardar')",
    ], timeout=3000)
    if found:
        log(step, "OK", "Found 'Guardar' / 'SAVE' button", selector=sel,
            screenshot=screenshot(page, "step15_guardar"))
    else:
        log(step, "FAIL", "Could not find 'Guardar' / 'Save' button",
            screenshot=screenshot(page, "step15_no_guardar"))


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    global START_TIME, XVFB_PROC

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(
        description="YouTube Studio dry-run navigation test (NO modifications made)"
    )
    parser.add_argument("--session", required=True,
                        help="Session name (e.g., tracatrack, burrianacasa2026)")
    parser.add_argument("--video-id", required=True,
                        help="YouTube video ID to test with")
    parser.add_argument("--display", default=":99",
                        help="Xvfb display number (default: :99)")
    args = parser.parse_args()

    session_file = TOKENS_DIR / f"{args.session}_browser_session.json"
    if not session_file.exists():
        print(f"\n❌ Session file not found: {session_file}")
        print("   Run login script first:")
        print(f"   python3 scripts/yt_browser_login.py --account {args.session}")
        sys.exit(1)

    START_TIME = datetime.now()

    print(f"\n{'='*70}")
    print("YouTube Studio DRY-RUN Navigation Test")
    print(f"  Session:  {args.session}")
    print(f"  Video ID: {args.video_id}")
    print(f"  Display:  {args.display}")
    print(f"  Mode:     READ-ONLY (no modifications will be made)")
    print(f"{'='*70}\n")

    # ── Start Xvfb ───────────────────────────────────────────────────────────
    print("Starting Xvfb (virtual display)...")
    display_num = args.display.replace(":", "")
    XVFB_PROC = subprocess.Popen(
        ["Xvfb", args.display, "-screen", "0", "1920x1080x24", "-ac"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)

    if XVFB_PROC.poll() is not None:
        print(f"❌ Xvfb failed to start on display {args.display}")
        sys.exit(1)

    print(f"✅ Xvfb running on display {args.display} (PID: {XVFB_PROC.pid})")
    os.environ["DISPLAY"] = args.display

    # ── Load session ─────────────────────────────────────────────────────────
    print(f"Loading session: {session_file}")
    try:
        with open(session_file) as f:
            session_data = json.load(f)
        cookies_count = len(session_data.get("cookies", []))
        origins_count = len(session_data.get("origins", []))
        print(f"  Cookies: {cookies_count}, Origins: {origins_count}")
    except Exception as e:
        print(f"❌ Cannot read session file: {e}")
        cleanup()
        sys.exit(1)

    # ── Launch browser ───────────────────────────────────────────────────────
    print("Launching Chromium with stealth...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,  # NOT headless — uses Xvfb as display
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    f"--display={args.display}",
                ],
            )

            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                storage_state=str(session_file),
                locale="es-ES",
                timezone_id="Europe/Madrid",
            )

            page = context.new_page()

            # Apply stealth
            try:
                from playwright_stealth import stealth_sync
                stealth_sync(page)
                print("✅ stealth applied")
            except ImportError:
                print("⚠️  playwright-stealth not available, continuing without it")

            # ── Run navigation ───────────────────────────────────────────────
            navigate_studio(page, args.video_id)

            # ── Summary ──────────────────────────────────────────────────────
            print(f"\n{'='*70}")
            print("DRY-RUN COMPLETE — Summary")
            print(f"{'='*70}")
            ok_count = sum(1 for r in RESULTS if r["status"] == "OK")
            fail_count = sum(1 for r in RESULTS if r["status"] == "FAIL")
            warn_count = sum(1 for r in RESULTS if r["status"] == "WARN")
            skip_count = sum(1 for r in RESULTS if r["status"] == "SKIP")

            print(f"  ✅ OK:   {ok_count}")
            print(f"  ❌ FAIL: {fail_count}")
            print(f"  ⚠️ WARN: {warn_count}")
            print(f"  ⏭️ SKIP: {skip_count}")
            print(f"  📸 Screenshots: {SCREENSHOTS_DIR}/")
            print()

            if fail_count == 0:
                print("🎯 ALL STEPS PASSED — Ready for real test with --apply flag!")
            else:
                print("⚠️  Some steps failed. Check screenshots and selectors before proceeding.")
                print("    YouTube Studio UI may have changed.")

            browser.close()

    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        cleanup()
        print("Xvfb stopped.")


if __name__ == "__main__":
    main()
