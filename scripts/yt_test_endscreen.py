#!/usr/bin/env python3
"""
TEST: End screen automation - FULL FLOW test.

1. Navigate to video editor
2. Click "Añadir pantallas finales" → click "Suscribirse"
3. Click "Añadir pantallas finales" → click "Vídeo" → select video
4. Click "Guardar"
"""
import os, sys, time, json
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

PROJECT_ROOT = Path("/root/autotube")
SCREENSHOTS_DIR = PROJECT_ROOT / "logs" / "endscreen_debug"
os.environ["DISPLAY"] = ":99"


def screenshot(page, name, out_dir):
    try:
        page.screenshot(path=str(out_dir / f"{name}.png"), timeout=15000)
        print(f"  📸 {name}.png")
    except:
        pass


def human_delay(sec=3.0, label=""):
    if label:
        print(f"  ⏳ {label} ({sec}s)")
    time.sleep(sec)


def click_add_endscreen(page):
    """Click the Añadir pantallas finales button via JavaScript."""
    try:
        page.evaluate("document.querySelector('#add-endscreen-icon-button')?.click()")
        return True
    except:
        return False


def run_test(account: str, video_id: str, apply: bool = False):
    """
    Test end screen automation.
    If apply=False, just discover without making changes.
    If apply=True, actually add the end screen elements and save.
    """
    profile_dir = PROJECT_ROOT / "tokens" / f"{account}_browser_profile"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = SCREENSHOTS_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    mode = "APPLY" if apply else "DISCOVER-ONLY"
    print(f"📂 {out_dir}")
    print(f"🔬 Mode: {mode} | Account: {account} | Video: {video_id}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-gpu", "--disable-software-rasterizer",
            ],
            viewport={"width": 1280, "height": 900},
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )
        page = context.pages[0] if context.pages else context.new_page()

        # ── 1. Navigate to editor ──────────────────────────────────
        editor_url = f"https://studio.youtube.com/video/{video_id}/editor"
        print(f"\n🔗 1. Navigating to editor...")
        page.goto(editor_url, wait_until="commit", timeout=60000)
        human_delay(8, "Editor load")
        screenshot(page, "01_editor", out_dir)
        print(f"    Title: {page.title()}")

        # ── 2. Click "Añadir pantallas finales" ────────────────────
        print(f"\n🖱️  2. Clicking 'Añadir pantallas finales'...")
        click_add_endscreen(page)
        human_delay(4, "Menu open")
        screenshot(page, "02_menu_open", out_dir)

        # Verify menu items are visible
        for item_id, label in [
            ("text-item-0", "Aplicar plantilla"),
            ("text-item-1", "Vídeo"),
            ("text-item-2", "Lista reproducción"),
            ("text-item-3", "Suscribirse"),
            ("text-item-4", "Canal"),
            ("text-item-5", "Enlace"),
        ]:
            try:
                el = page.query_selector(f"#{item_id}")
                if el and el.is_visible():
                    print(f"  ✅ {item_id}: '{label}' visible")
                else:
                    print(f"  ❌ {item_id}: not visible")
            except:
                print(f"  ❌ {item_id}: error")

        # ── 3. Add "Suscribirse" element ───────────────────────────
        print(f"\n🖱️  3. Clicking 'Suscribirse'...")
        try:
            sub_btn = page.wait_for_selector("#text-item-3", timeout=5000, state="visible")
            if apply:
                sub_btn.click()
                human_delay(3, "Wait for element placement")
                screenshot(page, "03_subscribe_added", out_dir)
                print(f"  ✅ Suscribirse clicked (element added to timeline)")
            else:
                print(f"  ⏭️  DISCOVER MODE: would click here (not applying)")
        except PlaywrightTimeout:
            print(f"  ❌ text-item-3 not found!")

        # ── 4. Click "Añadir pantallas finales" again ──────────────
        print(f"\n🖱️  4. Clicking 'Añadir pantallas finales' again...")
        click_add_endscreen(page)
        human_delay(4, "Menu open")
        screenshot(page, "04_menu_reopen", out_dir)

        # ── 5. Add "Vídeo" element ─────────────────────────────────
        print(f"\n🖱️  5. Clicking 'Vídeo'...")
        try:
            video_btn = page.wait_for_selector("#text-item-1", timeout=5000, state="visible")
            if apply:
                video_btn.click()
                human_delay(4, "Wait for video picker")
                screenshot(page, "05_video_picker", out_dir)

                # ── 6. Handle video selection ───────────────────────
                print(f"\n🔍 6. Looking for video selection options...")
                # Check what appeared after clicking "Vídeo"
                dump_visible_elements(page, out_dir, "06_after_video")

                # Try to find "Última subida" / "recent upload" / "choose" option
                video_selectors = [
                    "text=Última subida", "text=La más reciente",
                    "text=Recent upload", "text=Most recent",
                    "text=Best for viewer", "text=Mejor para el espectador",
                    "text=Elegir un vídeo", "text=Choose a video",
                    "[aria-label*='reciente' i]", "[aria-label*='recent' i]",
                    "[aria-label*='última' i]",
                ]
                for sel in video_selectors:
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            text = (el.text_content() or "").strip()[:80]
                            print(f"  ✅ FOUND: '{sel}' text='{text}'")
                    except:
                        pass
            else:
                print(f"  ⏭️  DISCOVER MODE: would click here")
        except PlaywrightTimeout:
            print(f"  ❌ text-item-1 not found!")
            screenshot(page, "05_no_video_btn", out_dir)

        # ── 7. Find Save button ────────────────────────────────────
        print(f"\n🔍 7. Looking for Save/Guardar button...")
        save_selectors = [
            "#save-buttons button", "#save-buttons ytcp-button",
            "button:has-text('Guardar')", "text=Guardar",
            "ytcp-button:has-text('Guardar')",
            "[aria-label*='guardar' i]", "[aria-label*='save' i]",
        ]
        for sel in save_selectors:
            try:
                els = page.query_selector_all(sel)
                for el in els:
                    if el.is_visible():
                        text = (el.text_content() or "").strip()[:80]
                        eid = el.get_attribute("id") or ""
                        print(f"  ✅ SAVE: '{sel}' id='{eid}' text='{text}'")
                        if apply:
                            print(f"  🖱️  Would click SAVE here...")
                            # el.click()
            except:
                pass

        # ── Dump final state ───────────────────────────────────────
        screenshot(page, "99_final", out_dir)

        # Dump visible elements
        dump_visible_elements(page, out_dir, "99_final")

        context.close()

    print(f"\n✅ Test complete: {out_dir}")
    action = "applied changes" if apply else "DISCOVER ONLY - no changes made"
    print(f"   Mode: {action}")


def dump_visible_elements(page, out_dir, label):
    """Dump all visible interactive elements."""
    try:
        elements = page.evaluate("""
            () => {
                const els = Array.from(document.querySelectorAll(
                    'button, ytcp-button, tp-yt-paper-button, ytcp-icon-button, ' +
                    '[role="button"], [role="menuitem"], [role="option"], [role="tab"], ' +
                    '[role="radio"], paper-item, ytcp-menu-item, tp-yt-paper-item, ' +
                    'input[type="radio"], input[type="checkbox"]'
                ));
                const visible = els.filter(el => {
                    const text = (el.textContent || '').trim();
                    if (text.length === 0 || text.length > 200) return false;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return false;
                    if (rect.bottom < 0 || rect.top > window.innerHeight) return false;
                    return true;
                });
                return visible.map(el => ({
                    tag: el.tagName,
                    text: (el.textContent || '').trim().slice(0, 130),
                    id: el.id || '',
                    ariaLabel: el.getAttribute?.('aria-label') || '',
                    role: el.getAttribute?.('role') || '',
                    name: el.getAttribute?.('name') || '',
                })).slice(0, 60);
            }
        """)
        filepath = out_dir / f"{label}_elements.json"
        json.dump(elements, open(str(filepath), "w"), indent=2, ensure_ascii=False)
        print(f"  📋 {len(elements)} visible elements saved to {filepath.name}")
        return elements
    except Exception as e:
        print(f"  ⚠️ Dump failed: {e}")
        return []


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="Actually make changes (DANGER: without this, just discovers)")
    args = parser.parse_args()
    run_test(args.account, args.video_id, apply=args.apply)
