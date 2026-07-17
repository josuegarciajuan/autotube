#!/usr/bin/env python3
"""
DEBUG 3: Interactive exploration of end screen editor.
Includes session refresh via main studio dashboard first.
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


def human_delay(sec=3.0, label=""):
    if label:
        print(f"  ⏳ {label} ({sec}s)...")
    time.sleep(sec)


def screenshot(page, name, dirpath):
    try:
        page.screenshot(path=str(dirpath / f"{name}.png"), timeout=15000)
        print(f"  📸 {name}.png")
    except Exception as e:
        print(f"  ⚠️ Screenshot failed: {e}")


def dump_dom(page, label, dirpath):
    try:
        html = page.evaluate("() => document.body.innerHTML")
        f = dirpath / f"{label}.html"
        f.write_text(html[:150000])
        print(f"  📄 {label}.html ({len(html)} chars)")
        return html
    except Exception as e:
        print(f"  ⚠️ DOM dump failed: {e}")
        return ""


def wait_for_any(page, selectors, timeout=20000):
    """Wait for any of multiple selectors to appear."""
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=min(timeout/len(selectors), 5000), state="visible")
            return (sel, el)
        except PlaywrightTimeout:
            continue
    return (None, None)


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
    print(f"✅ Session: {session_file} ({session_file.stat().st_size} bytes)")

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

    # ── Step 0: Refresh session via main studio page ─────────────
    print(f"🔗 Step 0: Refreshing session via studio dashboard...")
    page.goto("https://studio.youtube.com/", wait_until="commit", timeout=60000)
    human_delay(8, "Dashboard load")

    current_url = page.url
    print(f"   URL: {current_url}")
    print(f"   Title: {page.title()}")

    if "signin" in current_url or "accounts.google.com" in current_url:
        print(f"❌ Session expired! Redirected to: {current_url}")
        print(f"   Please recreate session: python3 scripts/yt_browser_login.py --account {args.account}")
        screenshot(page, "00_session_expired", out_dir)
        page.close()
        context.close()
        browser.close()
        p.stop()
        sys.exit(1)

    screenshot(page, "00_dashboard", out_dir)

    # ── Step 1: Find video in content page to verify session ────
    print(f"\n🔗 Step 1: Navigating to content page...")
    page.goto("https://studio.youtube.com/channel/UCejkjoNtUs99-LPBEYC7rPQ/videos", wait_until="commit", timeout=60000)
    human_delay(5, "Content page load")
    screenshot(page, "01_content", out_dir)

    # ── Step 2: Navigate directly to editor ──────────────────────
    editor_url = f"https://studio.youtube.com/video/{args.video_id}/editor"
    print(f"\n🔗 Step 2: Navigating to editor: {editor_url}")
    page.goto(editor_url, wait_until="commit", timeout=60000)
    human_delay(8, "Editor page load")
    screenshot(page, "02_editor", out_dir)
    print(f"   URL: {page.url}")
    print(f"   Title: {page.title()}")

    if "signin" in page.url:
        print(f"❌ Session expired on editor page!")
        sys.exit(1)

    # ── Step 3: Click "Añadir pantallas finales" ──────────────────
    print(f"\n🖱️  Step 3: Clicking 'Añadir pantallas finales'...")
    btn_sel = "#add-endscreen-icon-button"
    try:
        add_btn = page.wait_for_selector(btn_sel, timeout=15000, state="visible")
        text_nearby = page.evaluate(f"""
            () => {{
                const el = document.querySelector('{btn_sel}');
                const parent = el?.closest('.row, [class*="entrypoint"]');
                return parent?.textContent?.trim()?.slice(0, 80) || '';
            }}
        """)
        print(f"   Button found! Nearby text: '{text_nearby}'")
        add_btn.click()
        human_delay(5, "End screen panel open")
        screenshot(page, "03_after_click", out_dir)
    except PlaywrightTimeout:
        print(f"❌ End screen button not found! Timeout.")
        screenshot(page, "03_no_button", out_dir)
        dump_dom(page, "03_no_button", out_dir)
        page.close()
        context.close()
        browser.close()
        p.stop()
        sys.exit(1)

    # ── Step 4: Explore what opened ──────────────────────────────
    print(f"\n🔍 Step 4: Exploring end screen editor panel...")
    html = dump_dom(page, "04_endscreen_panel", out_dir)
    screenshot(page, "04_endscreen_panel", out_dir)

    # Look for key buttons in the panel
    button_selectors = [
        ("Suscripción", [
            "text=Suscribirse", "text=Suscripción", "text=Subscribe",
            "[aria-label*='suscrib' i]", "[aria-label*='subscribe' i]",
        ]),
        ("Vídeo", [
            "text=Vídeo", "text=Vídeo o canal", "text=Video",
            "[aria-label*='vídeo' i]", "[aria-label*='video' i]",
        ]),
        ("Guardar", [
            "button:has-text('Guardar')", "button:has-text('Save')",
            "#save-buttons", "#save-container", "text=Guardar",
        ]),
        ("Eliminar/Quitar", [
            "text=Eliminar", "text=Quitar", "text=Remove", "text=Delete",
            "[aria-label*='eliminar' i]", "[aria-label*='quitar' i]",
        ]),
        ("Listo/Hecho", [
            "text=Listo", "text=Hecho", "text=Done", "text=Aceptar",
        ]),
        ("Añadir elemento", [
            "text=Añadir", "text=Añadir elemento", "text=Add element",
        ]),
    ]

    found_buttons = {}
    for label, selectors in button_selectors.items():
        matches = []
        for sel in selectors:
            try:
                els = page.query_selector_all(sel)
                for el in els:
                    if el.is_visible():
                        text = (el.text_content() or "").strip()[:100]
                        tag = el.evaluate("el => el.tagName")
                        eid = el.get_attribute("id") or ""
                        aria = el.get_attribute("aria-label") or ""
                        # Skip nav/generic text
                        if any(skip in text.lower() for skip in ["saltar", "skip", "not your", "contraseña"]):
                            continue
                        matches.append({"sel": sel, "tag": tag, "text": text, "id": eid, "aria": aria})
            except Exception:
                pass
        if matches:
            found_buttons[label] = matches
            for m in matches:
                print(f"  ✅ [{label}] <{m['tag']}> id='{m['id']}' text='{m['text']}'")
        else:
            print(f"  ❌ [{label}] Not found")

    # ── Step 5: Dump ALL visible interactive elements ────────────
    print(f"\n📋 ALL visible interactive elements:")
    try:
        elements = page.evaluate(
            """() => {
                const els = Array.from(document.querySelectorAll(
                    'button, ytcp-button, tp-yt-paper-button, ytcp-icon-button, ' +
                    '[role="button"], [role="menuitem"], [role="option"], [role="tab"], ' +
                    '[role="radio"], [role="checkbox"], a[href="#"], ' +
                    'input[type="radio"], input[type="checkbox"], ' +
                    'ytcp-select, paper-dropdown-menu, ytcp-dropdown-trigger, ' +
                    'ytcp-text-dropdown-trigger, paper-item, ytcp-menu-item'
                ));
                const visible = els.filter(el => {
                    const text = (el.textContent || '').trim();
                    if (text.length === 0 || text.length > 200) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return false;
                    // Check if element is within viewport
                    if (rect.bottom < 0 || rect.top > window.innerHeight) return false;
                    return true;
                });
                return visible.map(el => ({
                    tag: el.tagName,
                    text: (el.textContent || '').trim().slice(0, 120),
                    id: el.id || '',
                    ariaLabel: el.getAttribute?.('aria-label') || '',
                    ariaChecked: el.getAttribute?.('aria-checked') || '',
                    role: el.getAttribute?.('role') || '',
                    name: el.getAttribute?.('name') || '',
                    type: el.getAttribute?.('type') || '',
                    dataTest: el.getAttribute?.('data-testid') || '',
                })).slice(0, 60);
            }"""
        )
        for i, el in enumerate(elements):
            extra = ""
            if el['ariaChecked']:
                extra = f"aria-checked={el['ariaChecked']} "
            if el['name']:
                extra += f"name='{el['name']}' "
            if el['dataTest']:
                extra += f"data-testid='{el['dataTest']}' "
            print(f"  [{i:02d}] <{el['tag']:20s}> role={el['role']:12s} {extra}"
                  f"text='{el['text']}'")
        json.dump(elements, (out_dir / "all_interactive.json").open("w"), indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠️ Failed: {e}")

    # ── Step 6: Search DOM for specific patterns ──────────────────
    print(f"\n🔍 DOM text search:")
    try:
        page_text = page.evaluate("() => document.body.textContent || ''")
    except Exception:
        page_text = ""
    terms = ["suscribirse", "suscripción", "subscribe", "vídeo o canal",
             "video or channel", "última subida", "recent upload",
             "best for viewer", "mejor para", "añadir elemento",
             "add element", "guardar", "save", "eliminar", "quitar",
             "listo", "done", "hecho", "aceptar"]
    for term in terms:
        count = page_text.lower().count(term.lower())
        if count > 0:
            print(f"  📝 '{term}' → {count} occurrences")

    # ── Cleanup ────────────────────────────────────────────────────
    page.close()
    context.close()
    browser.close()
    p.stop()
    print(f"\n📁 All results saved to: {out_dir}")
    print(f"   check screenshots and dumps for full context")


if __name__ == "__main__":
    main()
