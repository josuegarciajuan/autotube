#!/usr/bin/env python3
"""
DEBUG 2: Interactive exploration of the end screen editor panel.

1. Opens editor page
2. Clicks "Añadir pantallas finales" button
3. Explores the end screen panel for: Subscribe, Video, Save selectors
4. Takes screenshots at each step
"""
import argparse, json, os, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKENS_DIR = PROJECT_ROOT / "tokens"
SCREENSHOTS_DIR = PROJECT_ROOT / "logs" / "endscreen_debug"


def _ensure_xvfb():
    display = ":99"
    try:
        result = subprocess.run(
            ["xdpyinfo", "-display", display], capture_output=True, timeout=3
        )
        if result.returncode == 0:
            os.environ["DISPLAY"] = display
            return
    except Exception:
        pass
    subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1920x1080x24", "-ac"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    os.environ["DISPLAY"] = display


def screenshot(page, name: str, dirpath: Path):
    try:
        page.screenshot(path=str(dirpath / f"{name}.png"), timeout=15000)
        print(f"  📸 {name}.png")
    except Exception as e:
        print(f"  ⚠️ Screenshot failed: {e}")


def dump_dom(page, label: str, dirpath: Path):
    try:
        html = page.evaluate("() => document.body.innerHTML")
        filepath = dirpath / f"{label}.html"
        filepath.write_text(html[:150000])
        print(f"  📄 {label}.html ({len(html)} chars)")
        return html
    except Exception as e:
        print(f"  ⚠️ DOM dump failed: {e}")
        return ""


def find_all_interactive(html: str, search_terms: list) -> list:
    """Find interactive elements in HTML that match search terms."""
    import re
    found = []
    for term in search_terms:
        pattern = re.compile(
            r'<[^>]*\b(?:' + re.escape(term) + r')[^>]*>',
            re.IGNORECASE
        )
        for m in pattern.finditer(html):
            tag = m.group(0)
            if len(tag) < 500:
                found.append((term, tag))
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--account", required=True)
    args = parser.parse_args()

    _ensure_xvfb()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = SCREENSHOTS_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"📂 Output: {out_dir}")

    session_file = TOKENS_DIR / f"{args.account}_browser_session.json"
    if not session_file.exists():
        print(f"❌ Session not found: {session_file}")
        sys.exit(1)

    p = sync_playwright().start()
    browser = p.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-gpu", "--disable-software-rasterizer",
        ],
    )
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        storage_state=str(session_file),
        locale="es-ES",
        timezone_id="Europe/Madrid",
    )
    page = context.new_page()

    # ── Step 1: Navigate to editor ────────────────────────────────
    editor_url = f"https://studio.youtube.com/video/{args.video_id}/editor"
    print(f"🔗 Navigating to: {editor_url}")
    page.goto(editor_url, wait_until="commit", timeout=60000)
    time.sleep(8)  # Let editor fully load
    screenshot(page, "01_editor_loaded", out_dir)
    print(f"   URL: {page.url}")
    print(f"   Title: {page.title()}")

    # ── Step 2: Click "Añadir pantallas finales" ──────────────────
    print(f"\n🖱️  Clicking 'Añadir pantallas finales' button...")
    add_endscreen_btn = page.wait_for_selector(
        "#add-endscreen-icon-button, [aria-label='Añadir pantallas finales']",
        timeout=15000, state="visible"
    )
    print(f"   Button found: {add_endscreen_btn.is_visible()}")
    add_endscreen_btn.click()
    time.sleep(5)  # Let panel/dialog open
    screenshot(page, "02_after_click_endscreen", out_dir)

    # ── Step 3: Dump DOM after clicking ───────────────────────────
    print(f"\n📄 Dumping DOM after clicking end screen button...")
    html = dump_dom(page, "02_after_click", out_dir)

    # ── Step 4: Search for key elements in the opened panel ───────
    print(f"\n🔍 Searching for end screen elements...")

    # Search selectors
    selector_checks = [
        # Subscribe / Suscripción
        ("Suscribirse", ["text=Suscribirse", "text=Suscripción",
                         "text=Subscribe", "text=Channel",
                         "[aria-label*='suscrib' i]", "[aria-label*='subscribe' i]",
                         "button:has-text('Suscribirse')", "button:has-text('Suscrip')"]),
        # Video / Vídeo
        ("Vídeo", ["text=Vídeo", "text=Video", "text=Vídeo o canal",
                   "text=Video or channel", "text=Última subida",
                   "text=reciente", "text=upload", "text=Mejor",
                   "[aria-label*='video' i]", "[aria-label*='vídeo' i]"]),
        # Save / Guardar
        ("Guardar", ["button:has-text('Guardar')",
                     "button:has-text('Save')",
                     "#save-buttons", "#save-container",
                     "[aria-label*='guardar' i]", "[aria-label*='save' i]",
                     "text=Guardar"]),
        # Add element
        ("Añadir elemento", ["text=Añadir elemento",
                             "text=Add element",
                             "[aria-label*='añadir elemento' i]"]),
        # Remove / delete existing
        ("Eliminar/Quitar", ["text=Eliminar", "text=Quitar",
                             "text=Remove", "text=Delete"]),
        # Timeline
        ("Timeline", ["[role='slider']", ".timeline", "#timeline"]),
    ]

    for label, selectors in selector_checks:
        found_any = False
        for sel in selectors:
            try:
                els = page.query_selector_all(sel)
                for el in els[:3]:
                    if el.is_visible():
                        text = (el.text_content() or "").strip()[:80]
                        tag = el.evaluate("el => el.tagName")
                        eid = el.get_attribute("id") or ""
                        aria = el.get_attribute("aria-label") or ""
                        if any(c in text.lower() or c in aria.lower()
                               for c in ["saltar", "skip", "not your"]):
                            continue
                        print(f"  ✅ [{label}] {sel} → <{tag}> id='{eid}' text='{text}' aria='{aria}'")
                        found_any = True
            except Exception as e:
                pass
        if not found_any:
            print(f"  ❌ [{label}] No matches found")

    # ── Step 5: Look for the element type selector panel ──────────
    print(f"\n🔍 Looking for element type picker...")
    # Try clicking around to find the element picker
    picker_selectors = [
        "#add-endscreen-element-button",
        "[aria-label*='elemento' i]",
        "button:has-text('Elemento')",
        "ytcp-icon-button:has-text('Añadir')",
        "button:has-text('Añadir')",
    ]
    for sel in picker_selectors:
        try:
            el = page.query_selector(sel)
            if el:
                print(f"  🔍 ELEMENT PICKER: {sel} visible={el.is_visible()} text='{(el.text_content() or '')[:60]}'")
        except Exception:
            pass

    # ── Step 6: Look inside any dialog/modal/panel ──────────────────
    print(f"\n🔍 Looking for dialogs/modals/panels...")
    dialog_selectors = [
        "[role='dialog']", "[role='alertdialog']",
        "ytcp-dialog", "paper-dialog",
        ".style-scope.ytve-endscreen-editor",
        "#endscreen-editor", "#endscreen-panel",
        "[id*='endscreen']", "[class*='endscreen']",
        "[id*='panel']", "[class*='panel']",
    ]
    for sel in dialog_selectors:
        try:
            els = page.query_selector_all(sel)
            for el in els[:5]:
                is_vis = el.is_visible()
                eid = el.get_attribute("id") or ""
                eclass = (el.get_attribute("class") or "")[:80]
                if is_vis:
                    print(f"  ✅ DIALOG/PANEL: {sel} → id='{eid}' class='{eclass}'")
        except Exception:
            pass

    # ── Step 7: List ALL visible buttons after panel opens ─────────
    print(f"\n📋 ALL visible buttons in end screen panel:")
    try:
        buttons = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                'button, ytcp-button, tp-yt-paper-button, ytcp-icon-button, ' +
                '[role="button"], [role="menuitem"], [role="option"], [role="tab"], ' +
                'a[href="#"]'
            ))
            .filter(el => {
                const text = (el.textContent || '').trim();
                return text.length > 0 && text.length < 120 && !!(el.offsetParent || el.checkVisibility?.());
            })
            .map(el => ({
                tag: el.tagName,
                text: (el.textContent || '').trim().slice(0, 100),
                id: el.id || '',
                ariaLabel: el.getAttribute?.('aria-label') || '',
                role: el.getAttribute?.('role') || '',
            }))
            .slice(0, 50)
            """
        )
        for b in buttons:
            tag_label = f"[{b['tag']}]"
            id_label = f"id='{b['id']}'" if b['id'] else ""
            aria_label = f"aria='{b['ariaLabel']}'" if b['ariaLabel'] else ""
            print(f"  {tag_label:25s} {id_label:30s} {aria_label:35s} text='{b['text']}'")
        json.dump(buttons, (out_dir / "all_buttons.json").open("w"), indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠️ Button dump failed: {e}")

    # ── Step 8: Try to find "Suscribirse" by searching the DOM ─────
    print(f"\n🔍 DOM text search for key terms...")
    page_text = ""
    try:
        page_text = page.evaluate("() => document.body.textContent || ''")
    except Exception:
        pass
    for term in ["suscribirse", "suscripción", "subscribe", "añadir elemento",
                 "guardar", "save", "vídeo", "video", "elemento", "eliminar",
                 "quitar", "última", "reciente", "canal", "listo"]:
        found_count = page_text.lower().count(term.lower())
        if found_count > 0:
            print(f"  📝 '{term}' found {found_count} times in page text")

    # ── Cleanup ────────────────────────────────────────────────────
    screenshot(page, "03_final_state", out_dir)
    page.close()
    context.close()
    browser.close()
    p.stop()
    print(f"\n📁 Results: {out_dir}")


if __name__ == "__main__":
    main()
