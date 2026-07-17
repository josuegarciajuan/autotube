#!/usr/bin/env python3
"""
TEST FINAL: End screen automation for video qKpbl0-aK8M (canal3 / Civilizaciones Olvidadas).

Uses existing YouTubeBrowser infrastructure (get_browser) with tracatrack session.
Adds: Subscribe button + Video recommendation → Save.
"""
import logging
import sys
import time

sys.path.insert(0, "/root/autotube")

from pipeline.youtube_browser import get_browser

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("endscreen_test")

VIDEO_ID = "qKpbl0-aK8M"
ACCOUNT = "tracatrack"


def main():
    log.info("== End Screen Test for %s ==", VIDEO_ID)

    # ── 1. Get browser (reuses existing session) ─────────────────
    log.info("Connecting to browser session: %s...", ACCOUNT)
    browser = get_browser(ACCOUNT)
    browser._ensure_browser()
    page = browser._context.new_page()

    try:
        # ── 2. Navigate to editor ────────────────────────────────
        editor_url = f"https://studio.youtube.com/video/{VIDEO_ID}/editor"
        log.info("Navigating to: %s", editor_url)
        page.goto(editor_url, wait_until="commit", timeout=60000)
        time.sleep(10)
        log.info("Page loaded: %s", page.title())

        if "signin" in page.url.lower():
            log.error("Session expired! URL: %s", page.url)
            return False

        # ── 3. Remove warm welcome overlay ────────────────────────
        log.info("Removing overlays...")
        page.evaluate("""
            () => {
                document.querySelector('ytcp-promo-page')?.remove();
                document.querySelector('ytve-warm-welcome')?.remove();
            }
        """)
        time.sleep(2)

        # ── 4. Check for existing end screen elements (already configured?) ──
        existing = page.evaluate("""() => {
            const triggers = document.querySelectorAll('ytcp-dropdown-trigger');
            let count = 0;
            for (const t of triggers) {
                if (t.offsetParent) count++;
            }
            return count;
        }""")
        log.info("Existing end screen elements: %d", existing)
        if existing >= 1:
            log.info("End screens may already be configured. Proceeding anyway...")

        # ── 5. Add "Suscribirse" element ─────────────────────────
        log.info("Adding 'Suscribirse' element...")
        # Open add menu
        page.evaluate("document.querySelector('#add-endscreen-icon-button')?.click()")
        time.sleep(3)

        # Click "Suscribirse" — using text-based selector
        sub_clicked = page.evaluate("""() => {
            const items = document.querySelectorAll('tp-yt-paper-item, paper-item');
            for (const item of items) {
                const t = (item.textContent || '').trim();
                if (t === 'Suscribirse' && item.offsetParent) {
                    item.click();
                    return 'clicked';
                }
            }
            return 'not_found';
        }""")
        log.info("  Suscribirse: %s", sub_clicked)
        time.sleep(4)

        # ── 6. Add "Vídeo" element ───────────────────────────────
        log.info("Adding 'Vídeo' element...")
        # Reopen menu (using add-element-menu-button which appears in toolbar)
        page.evaluate("""
            () => {
                const btn = document.querySelector('#add-element-menu-button')
                    || document.querySelector('#add-endscreen-icon-button');
                if (btn) btn.click();
            }
        """)
        time.sleep(4)

        # Click "Vídeo" — text-based
        vid_clicked = page.evaluate("""() => {
            const items = document.querySelectorAll('tp-yt-paper-item, paper-item');
            for (const item of items) {
                const t = (item.textContent || '').trim();
                if (t === 'Vídeo' && item.offsetParent) {
                    item.click();
                    return 'clicked';
                }
            }
            return 'not_found';
        }""")
        log.info("  Vídeo: %s", vid_clicked)
        time.sleep(12)  # Long wait for video picker to load

        # ── 7. Handle video picker dialog ────────────────────────
        log.info("Handling video picker dialog...")
        try:
            dlg = page.query_selector('[role="dialog"], ytcp-dialog')
            if dlg and dlg.is_visible():
                log.info("  Dialog found, looking for video options...")
                # Wait for content to load
                for i in range(10):
                    has_options = page.evaluate("""() => {
                        const dlg = document.querySelector('[role="dialog"], ytcp-dialog');
                        if (!dlg || !dlg.offsetParent) return -1;
                        const items = dlg.querySelectorAll('[role="radio"], [role="option"], tp-yt-paper-item, paper-item');
                        return Array.from(items).filter(el => el.offsetParent && (el.textContent||'').trim().length > 3).length;
                    }""")
                    if has_options > 0:
                        log.info("  Found %d video options", has_options)
                        # Click first option (usually "La más reciente"/"Most recent")
                        page.evaluate("""() => {
                            const dlg = document.querySelector('[role="dialog"], ytcp-dialog');
                            const items = dlg.querySelectorAll('[role="radio"], [role="option"], tp-yt-paper-item');
                            for (const it of items) {
                                if (it.offsetParent && (it.textContent||'').trim().length > 3) {
                                    it.click();
                                    return;
                                }
                            }
                        }""")
                        time.sleep(2)
                        break
                    elif has_options == -1:
                        log.info("  Dialog closed")
                        break
                    time.sleep(2)
                else:
                    log.info("  No options loaded, closing dialog")
                # Close dialog if still open
                close_btn = dlg.query_selector('button:has-text("Cerrar")')
                if not close_btn:
                    close_btn = dlg.query_selector('button:has-text("Close")')
                if close_btn and close_btn.is_visible():
                    close_btn.click()
                    log.info("  Clicked Cerrar")
            else:
                log.info("  No dialog visible")
        except Exception as e:
            log.info("  Dialog handling error: %s", e)
            page.keyboard.press("Escape")

        time.sleep(3)
        # Press Escape to dismiss any remaining panels
        page.keyboard.press("Escape")
        time.sleep(2)

        # ── 8. Check save button state ───────────────────────────
        save_info = page.evaluate("""() => {
            const btn = document.querySelector('#save-button');
            return {
                vis: btn ? !!(btn.offsetParent) : false,
                disabled: btn ? (btn.hasAttribute('disabled') || btn.getAttribute('aria-disabled') === 'true') : true,
            };
        }""")
        log.info("Save button: vis=%s, disabled=%s", save_info['vis'], save_info['disabled'])

        if not save_info['vis']:
            log.info("No save button — end screens may already be configured")
            return True

        if save_info['disabled']:
            log.warning("Save button is DISABLED — trying to enable...")
            # Try clicking on the timeline to "place" elements
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
            page.keyboard.press("Escape")
            time.sleep(1)
            # Re-check
            save_info = page.evaluate("""() => {
                const btn = document.querySelector('#save-button');
                return {vis: btn ? !!(btn.offsetParent) : false,
                        disabled: btn ? (btn.hasAttribute('disabled') || btn.getAttribute('aria-disabled')==='true') : true};
            }""")
            log.info("After retry: vis=%s, disabled=%s", save_info['vis'], save_info['disabled'])

        if save_info['disabled']:
            log.error("Could not enable save button. You may need to configure manually.")
            return False

        # ── 9. SAVE ──────────────────────────────────────────────
        log.info("Saving end screens...")
        page.evaluate("document.querySelector('#save-button')?.click()")
        time.sleep(12)

        # ── 10. Verify ───────────────────────────────────────────
        btn_gone = page.evaluate("() => !document.querySelector('#save-button')?.offsetParent")
        log.info("Save button disappeared: %s", btn_gone)

        page_text = page.evaluate("() => document.body.textContent || ''")
        if "guardado" in page_text.lower():
            log.info("✅ 'Guardado' confirmation found!")
            return True

        if btn_gone:
            log.info("✅ Save successful (button disappeared)!")
            return True

        log.warning("⚠️ No confirmation detected — check manually")
        return False

    except Exception as e:
        log.exception("Error: %s", e)
        return False
    finally:
        try:
            page.close()
        except Exception:
            pass


if __name__ == "__main__":
    success = main()
    if success:
        log.info("🏁 TEST PASSED — End screens configured on %s", VIDEO_ID)
        log.info("   Verify: https://studio.youtube.com/video/%s/editor", VIDEO_ID)
        sys.exit(0)
    else:
        log.error("❌ TEST FAILED")
        sys.exit(1)
