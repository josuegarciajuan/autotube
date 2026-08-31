#!/usr/bin/env python3
"""Debug script: dump the HTML structure around "Uso de IA" section in YT Studio."""

import json
import sys
import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKENS_DIR = PROJECT_ROOT / "tokens"
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="Browser session name")
    parser.add_argument("--video-id", required=True, help="YouTube video ID")
    args = parser.parse_args()
    session = args.session
    video_id = args.video_id
    session_file = TOKENS_DIR / f"{session}_browser_session.json"
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

        # Navigate to editor
        edit_url = f"https://studio.youtube.com/video/{video_id}/edit"
        print(f"Opening: {edit_url}")
        page.goto(edit_url, wait_until="commit", timeout=60000)
        page.wait_for_timeout(6000)
        print(f"URL: {page.url}")

        # Scroll to bottom
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(3000)

        # Click "Mostrar más"
        for sel in ["text=Mostrar más", "text=Show more", "button:has-text('Mostrar más')"]:
            try:
                el = page.wait_for_selector(sel, timeout=3000)
                if el and el.is_visible():
                    el.click()
                    print(f"Clicked: {sel}")
                    page.wait_for_timeout(3000)
                    break
            except PlaywrightTimeout:
                continue

        # Scroll again
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)

        # Now dump everything around "Uso de IA"
        print("\n" + "="*70)
        print("DUMP: All text on page containing 'IA' or 'alterado' or 'Sí' or 'No'")
        print("="*70)

        # Find all elements with relevant text
        result = page.evaluate("""
            () => {
                const results = [];
                // Find the "Uso de IA" section
                const allText = document.querySelectorAll('*');
                const keywords = ['Uso de IA', 'alterado', 'sintético', 'inteligencia', 'IA', 'altered'];
                
                for (const el of allText) {
                    if (el.children.length === 0 && el.textContent) {
                        const txt = el.textContent.trim();
                        for (const kw of keywords) {
                            if (txt.includes(kw)) {
                                // Get the parent structure
                                let parent = el.parentElement;
                                let hierarchy = [];
                                for (let i = 0; i < 4 && parent; i++) {
                                    hierarchy.push(parent.tagName + (parent.id ? '#' + parent.id : '') + (parent.className ? '.' + parent.className.split(' ')[0] : ''));
                                    parent = parent.parentElement;
                                }
                                results.push({
                                    tag: el.tagName,
                                    text: txt.substring(0, 80),
                                    id: el.id || '',
                                    classes: el.className || '',
                                    parents: hierarchy.join(' > '),
                                    innerHTML: el.parentElement ? el.parentElement.innerHTML.substring(0, 200) : ''
                                });
                                break;
                            }
                        }
                    }
                }
                return results;
            }
        """)

        for i, r in enumerate(result):
            print(f"\n[{i}] <{r['tag']}#{r['id']}> text='{r['text'][:80]}'")
            print(f"    Parents: {r['parents'][:120]}")
            print(f"    HTML: {r.get('innerHTML', '')[:200]}")

        # Also find all radio buttons and Sí/No near IA section
        print("\n" + "="*70)
        print("RADIO BUTTONS near 'Uso de IA':")
        print("="*70)

        radio_info = page.evaluate("""
            () => {
                // Find the altered content container
                const container = document.querySelector('#altered-content');
                if (!container) return JSON.stringify({error: '#altered-content not found'});
                
                // Find radio buttons within the container
                const radios = container.querySelectorAll('tp-yt-paper-radio-button, paper-radio-button, [role="radio"]');
                const results = [];
                radios.forEach(r => {
                    results.push({
                        tag: r.tagName,
                        text: (r.textContent || '').trim().substring(0, 60),
                        role: r.getAttribute('role') || '',
                        value: r.getAttribute('value') || '',
                        name: r.getAttribute('name') || '',
                        checked: r.getAttribute('aria-checked') || r.checked || '',
                        disabled: r.getAttribute('aria-disabled') || r.disabled || '',
                        outerHTML: r.outerHTML ? r.outerHTML.substring(0, 300) : ''
                    });
                });
                
                // Also check for any 'Sí'/'No' text inside container
                const allEls = container.querySelectorAll('*');
                const siNoEls = [];
                allEls.forEach(el => {
                    if (el.children.length === 0) {
                        const txt = el.textContent.trim();
                        if (txt === 'Sí' || txt === 'No') {
                            siNoEls.push({
                                tag: el.tagName,
                                text: txt,
                                id: el.id,
                                className: el.className,
                                parentTag: el.parentElement ? el.parentElement.tagName : '',
                                isClickable: el.onclick !== null || el.getAttribute('role') === 'button',
                                outer: el.outerHTML ? el.outerHTML.substring(0, 200) : ''
                            });
                        }
                    }
                });
                
                return JSON.stringify({
                    radioCount: results.length,
                    radios: results,
                    siNoTextElements: siNoEls
                });
            }
        """)

        data = json.loads(radio_info) if isinstance(radio_info, str) else radio_info
        print(f"\nRadios found in #altered-content: {data.get('radioCount', 0)}")
        for r in data.get('radios', []):
            print(f"  <{r['tag']}> role={r['role']} value={r['value']} checked={r['checked']} disabled={r['disabled']} text='{r['text']}'")
            print(f"    HTML: {r.get('outerHTML', '')[:200]}")
        
        print(f"\nSí/No text elements in #altered-content: {len(data.get('siNoTextElements', []))}")
        for s in data.get('siNoTextElements', []):
            print(f"  <{s['tag']}> text='{s['text']}' parent={s['parentTag']} class={s['className'][:50]} id={s['id']}")
            print(f"    HTML: {s.get('outer', '')[:200]}")

        # Screenshot
        page.screenshot(path="/tmp/debug_uso_ia.png")
        print("\nScreenshot saved: /tmp/debug_uso_ia.png")

        browser.close()

if __name__ == "__main__":
    main()
