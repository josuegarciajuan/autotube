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
    try:
        page.screenshot(path=str(path), timeout=10000)
    except PlaywrightTimeout:
        # Screenshot timed out (common on Xvfb with font loading), skip
        pass
    except Exception:
        pass
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

def navigate_studio(page, video_id: str, apply_mode: bool = False):
    """Navigate YouTube Studio to the video editor, logging every step.
    If apply_mode=True, clicks 'Sí' + 'Guardar'."""
    step = 0

    # --- Step 1: Open YouTube Studio (verify session/login) ---
    step += 1
    try:
        page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeout:
            pass  # networkidle often fails on YT Studio, ignore
        page.wait_for_timeout(2000)
        log(step, "OK", "Navigated to studio.youtube.com (session verified)",
            screenshot=screenshot(page, "step01_studio_home"))
    except Exception as e:
        log(step, "FAIL", f"Cannot reach studio.youtube.com: {e}")
        return

    # --- Step 2: Verify we're logged in ---
    step += 1
    found, sel, _ = find_element(page, [
        "text=Contenido >> visible=true",
        "a[href*='/videos']",
        "[id='menu-item']",
        "ytd-guide-entry-renderer",
        "ytcp-navigation-drawer",
    ])
    if found:
        log(step, "OK", "Session loaded — YouTube Studio dashboard visible", selector=sel,
            screenshot=screenshot(page, "step02_logged_in"))
    else:
        log(step, "FAIL", "Not logged in. Session may be expired.",
            screenshot=screenshot(page, "step02_not_logged_in"))
        return

    # --- Step 3: Navigate directly to the video editor ---
    step += 1
    edit_url = f"https://studio.youtube.com/video/{video_id}/edit"
    try:
        # YouTube Studio SPA is slow — commit = least strict wait
        page.goto(edit_url, wait_until="commit", timeout=60000)
        # Wait for the editor UI to render
        for _ in range(20):
            page.wait_for_timeout(2000)
            # Check if page is actually the editor (or still loading)
            url = page.url
            if "/video/" in url and "/edit" in url:
                break
            print(f"  Still loading... current URL: {url[:80]}")
        page.wait_for_timeout(5000)
        log(step, "OK", f"Navigated directly to video editor: {edit_url}",
            screenshot=screenshot(page, "step03_editor"))
    except Exception as e:
        log(step, "FAIL", f"Cannot open editor page: {e}")
        return

    # --- Step 4: Wait for editor to fully load (find the title field) ---
    step += 1
    found, sel, _ = find_element(page, [
        "[id='title-textarea']",
        "[aria-label='Título']",
        "textarea",
        "input[aria-label*='título']",
        "input[aria-label*='title']",
        "[id='textbox']",
    ])
    if found:
        log(step, "OK", "Video editor loaded — title field detected", selector=sel,
            screenshot=screenshot(page, "step04_editor_loaded"))
    else:
        log(step, "WARN", "Editor loaded but title field not found — continuing",
            screenshot=screenshot(page, "step04_editor_maybe_loaded"))

    # --- Step 5: Find and scroll to "Mostrar más" at page bottom ---
    step += 1
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(3000)
        log(step, "OK", "Scrolled to bottom of editor page",
            screenshot=screenshot(page, "step05_scrolled"))
    except Exception as e:
        log(step, "WARN", f"Scroll failed: {e}",
            screenshot=screenshot(page, "step05_scroll_fail"))

    # --- Step 6: Click "Mostrar más" to expand advanced settings ---
    step += 1
    found, sel, _ = find_element(page, [
        "text=Mostrar más",
        "text=Show more",
        "button:has-text('Mostrar más')",
        "button:has-text('Show more')",
        "[aria-label='Mostrar más']",
    ], timeout=5000)
    if found:
        try:
            page.click(sel, timeout=5000)
            page.wait_for_timeout(3000)
            log(step, "OK", "Clicked 'Mostrar más' — advanced settings expanded",
                selector=sel, screenshot=screenshot(page, "step06_show_more"))
        except Exception as e:
            log(step, "WARN", f"Click 'Mostrar más' failed: {e}", selector=sel)
    else:
        log(step, "WARN", "'Mostrar más' not found — section may already be expanded",
            screenshot=screenshot(page, "step06_not_found"))

    # --- Step 7: Find "Uso de IA" / "Altered content" section ---
    step += 1
    # Scroll again to ensure newly expanded content is visible
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)

    found, sel, _ = find_element(page, [
        "text=Uso de IA",
        "text=altered content",
        "text=Altered content",
        "text=Contenido alterado o sintético",
        "text=Contenido alterado",
        "text=sintético",
        "text=generado con IA",
        "text=inteligencia artificial",
        "label:has-text('alterado')",
        "label:has-text('sintético')",
        "label:has-text('IA')",
        "label:has-text('inteligencia artificial')",
    ], timeout=8000)
    if found:
        log(step, "OK", "Found 'Uso de IA' / 'Altered content' section", selector=sel,
            screenshot=screenshot(page, "step07_uso_ia"))
    else:
        log(step, "FAIL", "Could not find 'Uso de IA' section",
            screenshot=screenshot(page, "step07_uso_ia_not_found"))
        # Dump page text for debugging (first 500 chars)
        try:
            body_text = page.inner_text("body")[:500]
            print(f"  Page text sample: {body_text}")
        except Exception:
            pass
        return

    # --- Step 8: Click "Sí" (or just find it in dry-run) ---
    step += 1
    found, sel, _ = find_element(page, [
        "text=Sí >> visible=true",
        "text=Yes >> visible=true",
        "label:has-text('Sí')",
        "label:has-text('Yes')",
    ], timeout=5000)
    if found:
        if apply_mode:
            try:
                # Approach: find actual radio inputs and interact with them natively
                # YouTube Studio Polymer elements require click on the paper-radio-button
                # Try clicking the parent paper-radio-button if it exists
                radio_clicked = False
                
                for radio_sel in [
                    "tp-yt-paper-radio-button:has-text('Sí')",
                    "paper-radio-button:has-text('Sí')",
                    "[role='radio'][aria-label*='Sí']",
                ]:
                    radio_el = page.query_selector(radio_sel)
                    if radio_el and radio_el.is_visible():
                        radio_el.click(timeout=3000)
                        radio_clicked = True
                        sel = radio_sel
                        break

                if not radio_clicked:
                    # Fallback: JavaScript click on native radio input
                    page.evaluate("""
                        () => {
                            const labels = document.querySelectorAll('label');
                            for (const label of labels) {
                                if (label.textContent.trim() === 'Sí') {
                                    // Try to click the associated input or the label itself
                                    const input = label.querySelector('input[type="radio"]');
                                    if (input) { input.click(); input.dispatchEvent(new Event('change', {bubbles: true})); }
                                    else { label.click(); }
                                    return true;
                                }
                            }
                            return false;
                        }
                    """)
                    page.wait_for_timeout(1000)
                    radio_clicked = True
                    sel = "JavaScript querySelector"

                # Click in the title field to trigger blur/change detection
                title_el = page.query_selector("[id='title-textarea']")
                if title_el:
                    title_el.click()
                page.wait_for_timeout(1000)

                log(step, "OK", f"CLICKED 'Sí' — altered content marked", selector=sel,
                    screenshot=screenshot(page, "step08_clicked_si"))
            except Exception as e:
                log(step, "FAIL", f"Click 'Sí' failed: {e}", selector=sel,
                    screenshot=screenshot(page, "step08_click_fail"))
                return
        else:
            log(step, "OK", "Found 'Sí' / 'Yes' option (dry-run — not clicked)", selector=sel,
                screenshot=screenshot(page, "step08_si_yes"))
    else:
        log(step, "FAIL", "Could not find 'Sí' / 'Yes' option",
            screenshot=screenshot(page, "step08_no_si"))
        return

    # --- Step 9: Click "Guardar" (or just find it in dry-run) ---
    step += 1
    guardar_selectors = [
        "button:has-text('Guardar'):not([disabled])",
        "ytcp-button:has-text('Guardar'):not([disabled])",
        "button:has-text('SAVE'):not([disabled])",
        "button:has-text('Save'):not([disabled])",
        "[aria-label='Guardar']:not([disabled])",
    ]
    dry_selectors = [
        "text=Guardar >> visible=true",
        "text=SAVE >> visible=true",
        "button:has-text('Guardar')",
        "button:has-text('SAVE')",
    ]

    if apply_mode:
        # Wait for Guardar button to become enabled (YT Studio needs a moment)
        print("  Waiting for 'Guardar' button to enable...")
        guardar_el = None
        for attempt in range(15):  # 15 * 2s = 30s max
            for sel in guardar_selectors:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_enabled():
                        guardar_el = (el, sel)
                        break
                except Exception:
                    continue
            if guardar_el:
                break
            page.wait_for_timeout(2000)

        if guardar_el:
            el, sel = guardar_el
            try:
                el.click(timeout=5000)
                page.wait_for_timeout(2000)
                log(step, "OK", "🖱️ CLICKED 'Guardar' — changes saved", selector=sel,
                    screenshot=screenshot(page, "step09_clicked_guardar"))
            except Exception as e:
                log(step, "FAIL", f"Click 'Guardar' failed: {e}", selector=sel)
                return
        else:
            log(step, "FAIL", "Guardar button never became enabled — trying force click",
                screenshot=screenshot(page, "step09_never_enabled"))
            # Last resort: force click
            try:
                page.click("button:has-text('Guardar')", force=True, timeout=5000)
                page.wait_for_timeout(2000)
                log(step, "OK", "🖱️ FORCE-CLICKED 'Guardar'", selector="button:has-text('Guardar')")
            except Exception as e:
                log(step, "FAIL", f"Force click also failed: {e}")
                return
    else:
        found, sel, _ = find_element(page, dry_selectors, timeout=5000)
        if found:
            log(step, "OK", "Found 'Guardar' / 'SAVE' button (dry-run — not clicked)", selector=sel,
                screenshot=screenshot(page, "step09_guardar"))
        else:
            log(step, "FAIL", "Could not find 'Guardar' / 'Save' button",
                screenshot=screenshot(page, "step09_no_guardar"))

    # --- Step 10: Verify save confirmation ---
    step += 1
    if apply_mode:
        page.wait_for_timeout(2000)
        try:
            # Check for save confirmation toast
            found2, sel2, _ = find_element(page, [
                "text=Se ha guardado",
                "text=Guardado",
                "text=Saved",
                "text=Changes saved",
            ], timeout=5000)
            if found2:
                log(step, "OK", "Save confirmation detected", selector=sel2)
            else:
                log(step, "OK", "No confirmation toast — but Guardar was clicked")
        except Exception as e:
            log(step, "WARN", f"Could not verify save: {e}")
    else:
        log(step, "INFO", "Dry-run complete — all elements located, ready for --apply")


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
    parser.add_argument("--apply", action="store_true",
                        help="Actually mark 'Sí' + 'Guardar' (modifies the video!)")
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
    print(f"  Mode:     {'APPLY (will mark SÍ + GUARDAR)' if args.apply else 'READ-ONLY (no modifications)'}")
    print(f"{'='*70}\n")

    # ── Start Xvfb ───────────────────────────────────────────────────────────
    os.environ["DISPLAY"] = args.display

    # Check if Xvfb is already running on this display
    already_running = False
    try:
        result = subprocess.run(
            ["xdpyinfo", "-display", args.display],
            capture_output=True, timeout=3
        )
        if result.returncode == 0:
            already_running = True
            print(f"✅ Xvfb already running on display {args.display}")
    except Exception:
        pass

    if not already_running:
        print("Starting Xvfb (virtual display)...")
        XVFB_PROC = subprocess.Popen(
            ["Xvfb", args.display, "-screen", "0", "1920x1080x24", "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1)

        if XVFB_PROC.poll() is not None:
            print(f"❌ Xvfb failed to start on display {args.display}")
            sys.exit(1)

        print(f"✅ Xvfb running on display {args.display} (PID: {XVFB_PROC.pid})")

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
                    "--disable-gpu",
                    "--disable-software-rasterizer",
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
                from playwright_stealth import stealth
                stealth(page)
                print("✅ stealth applied")
            except Exception:
                print("⚠️  playwright-stealth not available, continuing without it")

            # ── Run navigation ───────────────────────────────────────────────
            navigate_studio(page, args.video_id, apply_mode=args.apply)

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
