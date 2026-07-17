#!/usr/bin/env python3
"""
DEBUG: YouTube Studio End Screen editor — read-only DOM discovery.

Navega al editor de pantallas finales, toma screenshots y vuelca el DOM
para encontrar los selectores exactos.

Usage:
    python3 scripts/yt_debug_endscreen.py --video-id qKpbl0-aK8M --account tracatrack
"""
import argparse
import json
import os
import subprocess
import sys
import time
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
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    os.environ["DISPLAY"] = display


def human_delay(sec: float = 3.0, label: str = ""):
    if label:
        print(f"  ⏳ {label} ...")
    time.sleep(sec)


def screenshot(page, name: str, dirpath: Path):
    try:
        path = dirpath / f"{name}.png"
        page.screenshot(path=str(path), timeout=15000)
        print(f"  📸 Screenshot: {name}.png")
        return str(path)
    except Exception as e:
        print(f"  ⚠️ Screenshot failed: {e}")
        return ""


def dump_dom(page, label: str, dirpath: Path) -> str:
    """Dump page HTML and return it."""
    try:
        html = page.evaluate("() => document.body.innerHTML")
        filepath = dirpath / f"{label}_body.html"
        filepath.write_text(html[:100000])  # cap at 100KB
        print(f"  📄 DOM dumped: {label}_body.html ({len(html)} chars)")
        return html
    except Exception as e:
        print(f"  ⚠️ DOM dump failed: {e}")
        return ""


def search_elements(page, terms: list[str]) -> dict:
    """Search DOM for elements containing specific text. Returns dict of findings."""
    findings = {}
    for term in terms:
        try:
            elements = page.evaluate(
                f"""
                (() => {{
                    const results = [];
                    const all = document.querySelectorAll(
                        'button, a, span, div, ytcp-text, tp-yt-paper-button, ' +
                        'ytcp-button, yt-icon-button, paper-button, ' +
                        'tp-yt-paper-item, ytcp-menu-item, ytcp-menu-navigation-item, ' +
                        'iron-selector > *, [role="menuitem"], [role="button"], ' +
                        '[role="tab"], [role="radio"], [role="option"], ' +
                        'label, input[type="radio"], input[type="checkbox"]'
                    );
                    const lowerTerm = '{term}'.toLowerCase();
                    for (const el of all) {{
                        const text = (el.textContent || '').trim();
                        if (text && text.toLowerCase().includes(lowerTerm)) {{
                            results.push({{
                                tag: el.tagName,
                                text: text.slice(0, 200),
                                id: el.id || '',
                                className: (typeof el.className === 'string'
                                    ? el.className.slice(0, 150) : ''),
                                role: el.getAttribute?.('role') || '',
                                ariaLabel: el.getAttribute?.('aria-label') || '',
                                name: el.getAttribute?.('name') || '',
                                title: el.getAttribute?.('title') || '',
                                dataTest: el.getAttribute?.('data-testid') || '',
                                isVisible: !!(el.offsetParent || el.checkVisibility?.()),
                            }});
                        }}
                    }}
                    return results.slice(0, 10);
                }})()
                """
            )
            if elements:
                findings[term] = elements
                print(f"  🔍 '{term}': {len(elements)} matches")
                for i, el in enumerate(elements[:3]):
                    print(f"      [{i}] <{el['tag']}> text='{el['text'][:80]}' "
                          f"id='{el['id']}' class='{el['className'][:50]}' "
                          f"name='{el['name']}' visible={el['isVisible']}")
            else:
                print(f"  ❌ '{term}': 0 matches")
        except Exception as e:
            print(f"  ⚠️ '{term}' search error: {e}")
    return findings


def dump_all_buttons(page, dirpath: Path):
    """Dump all visible buttons on the page."""
    try:
        buttons = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                'button, a[href], ytcp-button, tp-yt-paper-button, [role="button"], [role="menuitem"], [role="tab"], [role="option"]'
            ))
            .filter(el => {
                const text = (el.textContent || '').trim();
                return text.length > 0 && text.length < 100 && !!(el.offsetParent || el.checkVisibility?.());
            })
            .map(el => ({
                tag: el.tagName,
                text: (el.textContent || '').trim(),
                id: el.id || '',
                className: (typeof el.className === 'string' ? el.className.slice(0, 80) : ''),
                role: el.getAttribute?.('role') || '',
            }))
            .slice(0, 40)
            """
        )
        filepath = dirpath / "all_visible_buttons.json"
        json.dump(buttons, open(str(filepath), "w"), indent=2, ensure_ascii=False)
        print(f"  📋 All visible buttons ({len(buttons)}):")
        for b in buttons:
            print(f"      [{b['tag']}] role={b['role']} text='{b['text'][:80]}' id='{b['id']}'")
        return buttons
    except Exception as e:
        print(f"  ⚠️ Button dump failed: {e}")
        return []


def check_page_contains(page, texts: list[str]) -> dict:
    """Simple presence check for text on page."""
    results = {}
    page_text = ""
    try:
        page_text = page.evaluate("() => document.body.textContent || ''")
    except Exception:
        pass
    for t in texts:
        results[t] = t.lower() in page_text.lower()
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-id", required=True, help="YouTube video ID")
    parser.add_argument("--account", required=True, help="Google account name")
    args = parser.parse_args()

    _ensure_xvfb()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = SCREENSHOTS_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"📂 Output: {out_dir}")

    session_file = TOKENS_DIR / f"{args.account}_browser_session.json"
    if not session_file.exists():
        print(f"❌ Session file not found: {session_file}")
        sys.exit(1)
    print(f"✅ Session: {session_file} ({session_file.stat().st_size} bytes)")

    all_findings = {}
    p = sync_playwright().start()

    browser = p.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-software-rasterizer",
        ],
    )
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        storage_state=str(session_file),
        locale="es-ES",
        timezone_id="Europe/Madrid",
    )

    # ── URL patterns to try ──────────────────────────────────────────
    # YouTube Studio end screen editor candidates:
    url_patterns = [
        ("endscreen-direct", f"https://studio.youtube.com/video/{args.video_id}/editor/endscreen"),
        ("endscreen-slash", f"https://studio.youtube.com/video/{args.video_id}/editor/end-screen"),
        ("editor-base", f"https://studio.youtube.com/video/{args.video_id}/editor"),
        ("edit-base", f"https://studio.youtube.com/video/{args.video_id}/edit"),
    ]

    search_terms = [
        # Spanish UI terms for end screen editor
        "elemento", "añadir", "pantalla", "final", "suscrip",
        "suscribirse", "canal", "vídeo", "video", "guardar",
        "enlace", "lista", "reproducción", "quitar", "eliminar",
        "subir", "reciente",
        # English fallback
        "element", "end screen", "endscreen", "subscribe",
        "recent upload", "best for viewer", "choose",
    ]

    working_url = None
    page = context.new_page()

    for name, url in url_patterns:
        print(f"\n{'='*60}")
        print(f"🔗 Trying [{name}]: {url}")
        print(f"{'='*60}")

        try:
            page.goto(url, wait_until="commit", timeout=60000)
        except Exception as e:
            print(f"  ❌ Navigation failed: {e}")
            continue

        human_delay(6, label="Waiting for page load")
        screenshot(page, f"00_{name}", out_dir)

        current_url = page.url
        page_title = page.title()
        print(f"  🌐 Resolved URL: {current_url}")
        print(f"  📛 Page title: {page_title}")

        # Check if we landed on the right page
        if "/video/" not in current_url:
            print(f"  ❌ Not a video page, skipping")
            all_findings[name] = {
                "url": current_url, "title": page_title, "status": "wrong_page"
            }
            continue

        # Check key text presence
        presence_checks = [
            "elemento", "pantalla", "final", "guardar",
            "end screen", "endscreen", "subscribe",
            "editor", "vídeo", "video",
        ]
        page_text_check = check_page_contains(page, presence_checks)
        print(f"  📝 Text presence: {json.dumps({k: v for k, v in page_text_check.items() if v})}")

        # Take detailed screenshots
        screenshot(page, f"01_{name}_full", out_dir)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        human_delay(1)
        screenshot(page, f"01b_{name}_bottom", out_dir)
        page.evaluate("window.scrollTo(0, 0)")
        human_delay(1)

        # Dump visible buttons
        buttons = dump_all_buttons(page, out_dir)

        # Search for key elements
        print(f"\n  --- Searching key elements ---")
        findings = search_elements(page, search_terms)

        # Dump DOM
        dump_dom(page, name, out_dir)

        all_findings[name] = {
            "url": current_url,
            "title": page_title,
            "page_text_matches": {k: v for k, v in page_text_check.items() if v},
            "button_count": len(buttons),
            "element_findings": {
                term: [f"{el['tag']}: {el['text'][:80]}" for el in els[:3]]
                for term, els in findings.items()
                if els
            },
            "key_buttons": [
                {"text": b["text"][:80], "tag": b["tag"], "role": b["role"]}
                for b in buttons
                if any(
                    kw in b["text"].lower()
                    for kw in [
                        "element", "añadir", "guardar", "pantalla", "final",
                        "subscribe", "suscrib", "elemento", "save", "add",
                    ]
                )
            ],
        }

        # If we found an editor-like page with relevant elements, stop here
        found_end_screen_hits = any(
            findings.get(term)
            for term in ["pantalla", "final", "elemento", "end screen"]
        )
        found_editor_hits = any(
            findings.get(term)
            for term in ["guardar", "save", "añadir", "add"]
        )

        if found_end_screen_hits and found_editor_hits:
            working_url = current_url
            print(f"\n  ✅ LIKELY FOUND END SCREEN EDITOR at: {current_url}")
            print(f"  Stopping URL exploration here.")

            # ── Deeper exploration on the end screen editor ──
            print(f"\n{'='*60}")
            print(f"🔬 DEEP EXPLORATION")
            print(f"{'='*60}")

            # Try to find "Add element" button and click it (but DON'T apply)
            print(f"\n  --- Looking for 'Add element' / 'Añadir elemento' button ---")
            add_selectors_to_try = [
                "text=Añadir elemento",
                "text=Añadir",
                "button:has-text('elemento')",
                "button:has-text('Añadir')",
                "[aria-label*='añadir' i]",
                "[aria-label*='add' i]",
                "text=Add element",
                "text=Add",
                # Material Design selectors
                "ytcp-button:has-text('Añadir')",
                "tp-yt-paper-button:has-text('Añadir')",
            ]
            for sel in add_selectors_to_try:
                try:
                    el = page.query_selector(sel)
                    if el:
                        is_visible = el.is_visible()
                        text = el.text_content() or ""
                        print(f"  ✅ ADD BUTTON FOUND: selector='{sel}' visible={is_visible} text='{text[:80]}'")
                        # Don't click, just log
                    else:
                        print(f"  ❌ Not found: {sel}")
                except Exception as e:
                    print(f"  ⚠️ Error with '{sel}': {e}")

            # Try to find Save/Guardar button
            print(f"\n  --- Looking for Save / Guardar button ---")
            save_selectors = [
                "button:has-text('Guardar'):not([disabled])",
                "button:has-text('Guardar')",
                "text=Guardar",
                "button:has-text('Save'):not([disabled])",
                "button:has-text('Save')",
                "[aria-label*='guardar' i]",
                "[aria-label*='save' i]",
            ]
            for sel in save_selectors:
                try:
                    el = page.query_selector(sel)
                    if el:
                        is_visible = el.is_visible()
                        print(f"  {'✅' if is_visible else '🔒'} SAVE: selector='{sel}' visible={is_visible} text='{el.text_content() or ''} '")
                    else:
                        print(f"  ❌ Not found: {sel}")
                except Exception as e:
                    print(f"  ⚠️ Error with '{sel}': {e}")

            # Try to find Subscribe / Suscripción element option
            print(f"\n  --- Looking for Subscribe / Suscripción option ---")
            sub_selectors = [
                "text=Suscripción",
                "text=Suscribirse",
                "text=Subscribe",
                "text=Canal",
                "text=Channel",
                "text=Suscribirme",
                "[aria-label*='suscrib' i]",
                "[aria-label*='subscribe' i]",
            ]
            for sel in sub_selectors:
                try:
                    els = page.query_selector_all(sel)
                    if els:
                        for el in els:
                            print(f"  🔍 SUBSCRIBE: selector='{sel}' visible={el.is_visible()} text='{el.text_content() or ''}'")
                    else:
                        print(f"  ❌ Not found: {sel}")
                except Exception as e:
                    print(f"  ⚠️ Error with '{sel}': {e}")

            # Try to find Video / Vídeo element option
            print(f"\n  --- Looking for Video / Vídeo option ---")
            video_selectors = [
                "text=Vídeo",
                "text=Vídeo o canal",
                "text=Video",
                "text=Video or channel",
                "[aria-label*='vídeo' i]",
                "[aria-label*='video' i]",
            ]
            for sel in video_selectors:
                try:
                    els = page.query_selector_all(sel)
                    if els:
                        for el in els:
                            print(f"  🔍 VIDEO: selector='{sel}' visible={el.is_visible()} text='{el.text_content() or ''}'")
                    else:
                        print(f"  ❌ Not found: {sel}")
                except Exception as e:
                    print(f"  ⚠️ Error with '{sel}': {e}")

            # Dump final DOM and screenshot
            screenshot(page, f"02_{name}_deep", out_dir)
            dump_dom(page, f"{name}_deep", out_dir)

            break  # Stop trying URLs

    # Fallback: if no end screen editor found, go to the edit page and look for links
    if working_url is None:
        print(f"\n{'='*60}")
        print(f"⚠️ No end screen editor found directly. Trying to find link from edit page.")
        print(f"{'='*60}")
        edit_url = f"https://studio.youtube.com/video/{args.video_id}/edit"
        try:
            page.goto(edit_url, wait_until="commit", timeout=60000)
        except Exception as e:
            print(f"  ❌ Edit page navigation failed: {e}")
        human_delay(6, label="Edit page load")
        screenshot(page, "99_edit_page", out_dir)

        # Look for end screen navigation links in sidebar
        print(f"\n  --- Looking for end screen navigation links in sidebar ---")
        sidebar_selectors = [
            "[role='navigation'] a",
            "nav a",
            "[id='menu-content'] a",
            ".menu-content a",
            "[role='tab']",
            "a[href*='editor']",
            "a[href*='endscreen']",
            "a[href*='end-screen']",
        ]
        for sel in sidebar_selectors:
            try:
                els = page.query_selector_all(sel)
                for el in els[:20]:
                    text = (el.text_content() or "").strip()
                    href = el.get_attribute("href") or ""
                    if any(kw in (text + href).lower() for kw in ["pantalla", "final", "end", "screen", "editor"]):
                        print(f"  🔗 NAV LINK: '{text[:60]}' -> {href}")
            except Exception as e:
                print(f"  ⚠️ Error with '{sel}': {e}")

        # Dump all links on page
        try:
            links = page.evaluate(
                """() => Array.from(document.querySelectorAll('a, [role="tab"], [role="menuitem"]'))
                .filter(el => {
                    const text = (el.textContent || '').trim();
                    const href = el.getAttribute?.('href') || '';
                    return text.length > 0 && text.length < 80;
                })
                .map(el => ({
                    text: (el.textContent || '').trim(),
                    href: el.getAttribute?.('href') || '',
                    role: el.getAttribute?.('role') || '',
                }))
                .slice(0, 50)
                """
            )
            print(f"  --- All links/tabs on page ---")
            for l in links:
                print(f"      [{l['role']}] '{l['text'][:60]}' -> {l['href']}")
        except Exception as e:
            print(f"  ⚠️ Link dump failed: {e}")

        dump_dom(page, "edit_page", out_dir)
        all_findings["edit_page_fallback"] = {
            "url": page.url,
            "title": page.title(),
            "status": "explored_edit_page",
        }

    # ── Close ────────────────────────────────────────────────────────
    page.close()
    context.close()
    browser.close()
    p.stop()

    # ── Summary Report ───────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"📊 SUMMARY REPORT")
    print(f"{'='*60}")
    for name, info in all_findings.items():
        if isinstance(info, dict):
            print(f"\n  [{name}] {info.get('url', '?')}")
            print(f"    Title: {info.get('title', '?')}")
            if info.get("page_text_matches"):
                print(f"    Text found: {list(info.get('page_text_matches', {}).keys())}")
            if info.get("key_buttons"):
                print(f"    Key buttons ({len(info['key_buttons'])}):")
                for b in info["key_buttons"]:
                    print(f"      - [{b['tag']}] {b['text']}")

    json.dump(all_findings, (out_dir / "findings.json").open("w"), indent=2, ensure_ascii=False)
    print(f"\n📁 Full results: {out_dir}")
    print(f"📄 JSON report: {out_dir}/findings.json")

    if working_url:
        print(f"\n✅ SUCCESS: End screen editor found at: {working_url}")
    else:
        print(f"\n❌ Could not locate end screen editor directly.")


if __name__ == "__main__":
    main()
