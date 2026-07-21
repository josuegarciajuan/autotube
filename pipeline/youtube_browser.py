#!/usr/bin/env python3
"""
YouTube Studio Browser Automation — Core Module.

Reusable browser automation for YouTube Studio actions:
  - Mark video/short as "Altered content / Uso de IA" = Sí
  - Configure end screens (Subscribe + Video recommendation)

Uses Playwright + Xvfb for headless operation.
Persistent sessions stored in tokens/{account}_browser_session.json.
"""

import logging
import os
import random
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKENS_DIR = PROJECT_ROOT / "tokens"

# -- Selectors (confirmed working 2026-07-17) --
SEL_MOSTRAR_MAS = "text=Mostrar más"
SEL_RADIO_YES = '[name="VIDEO_HAS_ALTERED_CONTENT_YES"]'
SEL_GUARDAR_ENABLED = "button:has-text('Guardar'):not([disabled])"
SEL_SAVE_CONFIRM = "text=Guardado"

# -- End screen selectors (confirmed 2026-07-17) --
SEL_ADD_ENDSCREEN_BTN = "#add-endscreen-icon-button"
SEL_ADD_ELEMENT_MENU = "#add-element-menu-button"
SEL_ENDSCREEN_SAVE = "#save-button"
SEL_EDIT_BTN = "button:has-text('Editar'), ytcp-button:has-text('Editar')"
SEL_OVERLAY_PROMO = "ytcp-promo-page"
SEL_OVERLAY_WELCOME = "ytve-warm-welcome"
# Menu items use text-based selectors (IDs shift after adding elements)

# -- Channel -> Google Account mapping --
CHANNEL_ACCOUNT_MAP = {
    "canal2": "tracatrack",
    "canal3": "tracatrack",
    "canal4": "burrianacasa2026",
    "canal5": "burrianacasa2026",
}

# -- Browser pool --
_browser_instances: dict = {}
_browser_lock = threading.Lock()
_xvfb_display = ":99"
_xvfb_proc = None


def _ensure_xvfb():
    global _xvfb_proc
    try:
        result = subprocess.run(
            ["xdpyinfo", "-display", _xvfb_display], capture_output=True, timeout=3
        )
        if result.returncode == 0:
            os.environ["DISPLAY"] = _xvfb_display
            return
    except Exception:
        pass
    _xvfb_proc = subprocess.Popen(
        ["Xvfb", _xvfb_display, "-screen", "0", "1920x1080x24", "-ac"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    os.environ["DISPLAY"] = _xvfb_display


def human_delay(min_s: float = 0.3, max_s: float = 1.5, label: str = ""):
    delay = random.uniform(min_s, max_s)
    if label:
        logger.debug("Human delay: %.1fs (%s)", delay, label)
    time.sleep(delay)


class YouTubeBrowser:
    """Browser automation for YouTube Studio per Google account."""

    def __init__(self, account: str):
        self.account = account
        self.session_file = TOKENS_DIR / f"{account}_browser_session.json"
        self._playwright = None
        self._browser = None
        self._context = None
        self._lock = threading.Lock()
        if not self.session_file.exists():
            raise FileNotFoundError(
                f"Browser session not found: {self.session_file}\n"
                f"Run: python3 scripts/yt_browser_login.py --account {account}"
            )

    def _ensure_browser(self):
        if self._browser is not None:
            return
        _ensure_xvfb()
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-gpu", "--disable-software-rasterizer"],
        )
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            storage_state=str(self.session_file),
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )
        logger.info("Browser ready for account: %s", self.account)

    def close(self):
        with self._lock:
            try:
                if self._context:
                    self._context.close()
                if self._browser:
                    self._browser.close()
            except Exception:
                pass
            self._context = None
            self._browser = None

    def mark_altered_content(self, youtube_video_id: str) -> bool:
        with self._lock:
            try:
                self._ensure_browser()
                page = self._context.new_page()
                return self._do_mark(page, youtube_video_id)
            except Exception as e:
                logger.error("mark_altered_content failed for %s: %s", youtube_video_id, e)
                return False

    def _do_mark(self, page, video_id: str) -> bool:
        try:
            human_delay(1.0, 3.0, "initial")
            edit_url = f"https://studio.youtube.com/video/{video_id}/edit"
            page.goto(edit_url, wait_until="commit", timeout=60000)
            human_delay(4.0, 8.0, "page load")
            if "/video/" not in page.url or "/edit" not in page.url:
                logger.error("Navigation failed: %s", page.url[:120])
                page.close()
                return False

            try:
                page.wait_for_selector("[id='title-textarea']", timeout=10000, state="visible")
            except PlaywrightTimeout:
                logger.warning("Title field not found, continuing")
            human_delay(2.0, 4.0, "editor settle")

            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            human_delay(1.0, 2.0, "post-scroll")

            mostrar_el = page.wait_for_selector(SEL_MOSTRAR_MAS, timeout=15000, state="visible")
            human_delay(0.5, 1.5, "find mostrar")
            mostrar_el.click()
            human_delay(1.5, 3.0, "expand")

            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            human_delay(1.0, 2.0, "post-expand scroll")

            yes_radio = page.wait_for_selector(SEL_RADIO_YES, timeout=8000, state="visible")
            human_delay(0.5, 1.5, "find radio")
            already_checked = yes_radio.get_attribute("aria-checked")
            if already_checked == "true":
                logger.info("Video %s already marked (aria-checked=true)", video_id)
                page.close()
                return True

            yes_radio.click()
            human_delay(0.5, 1.5, "click radio")
            checked = yes_radio.get_attribute("aria-checked")
            if checked != "true":
                yes_radio.click()
                human_delay(1.0, 2.0, "retry radio")
                checked = yes_radio.get_attribute("aria-checked")
                if checked != "true":
                    logger.error("Radio not checked after retry (%s)", checked)
                    page.close()
                    return False
            logger.info("Radio confirmed (aria-checked=true)")

            human_delay(1.0, 2.0, "pre-guardar")
            guardar_el = None
            for _ in range(30):
                guardar_el = page.query_selector(SEL_GUARDAR_ENABLED)
                if guardar_el and guardar_el.is_enabled():
                    break
                time.sleep(1)
            if not guardar_el:
                logger.error("Guardar never enabled")
                page.close()
                return False

            human_delay(0.8, 2.0, "click guardar")
            guardar_el.click()
            human_delay(2.0, 4.0, "save settling")

            try:
                page.wait_for_selector(SEL_SAVE_CONFIRM, timeout=5000, state="attached")
                logger.info("Save confirmed for %s", video_id)
            except PlaywrightTimeout:
                logger.info("No save toast for %s (clicked anyway)", video_id)

            page.close()
            return True
        except PlaywrightTimeout as e:
            logger.error("Timeout for %s: %s", video_id, e)
            try: page.close()
            except Exception: pass
            return False
        except Exception as e:
            logger.error("Error for %s: %s", video_id, e)
            try: page.close()
            except Exception: pass
            return False


    def add_end_screens(self, youtube_video_id: str) -> bool:
        """Configure end screen: Subscribe button + Video recommendation.
        
        Runs in the SAME browser session as mark_altered_content.
        Uses the same self._lock for thread safety.
        """
        with self._lock:
            try:
                self._ensure_browser()
                page = self._context.new_page()
                return self._do_add_endscreen(page, youtube_video_id)
            except Exception as e:
                logger.error("add_end_screens failed for %s: %s", youtube_video_id, e)
                return False

    def _do_add_endscreen(self, page, video_id: str) -> bool:
        """Internal: execute end screen creation flow on a fresh page."""
        try:
            human_delay(1.0, 3.0, "endscreen: initial")
            editor_url = f"https://studio.youtube.com/video/{video_id}/editor"
            logger.info("End screen editor: %s", editor_url)
            page.goto(editor_url, wait_until="commit", timeout=60000)
            human_delay(6.0, 10.0, "endscreen: page load")

            if "/video/" not in page.url:
                logger.error("End screen navigation failed: %s", page.url[:120])
                page.close()
                return False

            # -- Remove warm welcome overlay (blocks everything) --
            human_delay(1.0, 2.0, "endscreen: remove overlay")
            page.evaluate("""
                () => {
                    document.querySelector('%s')?.remove();
                    document.querySelector('%s')?.remove();
                }
            """ % (SEL_OVERLAY_PROMO, SEL_OVERLAY_WELCOME))
            human_delay(2.0, 4.0, "endscreen: post-overlay")

            # -- Enter edit mode if needed --
            edit_clicked = self._enter_edit_mode(page)
            human_delay(2.0, 4.0, "endscreen: edit mode settle")

            # -- Check if end screens already exist --
            existing = self._check_existing_elements(page)
            if existing > 0:
                logger.info("Video %s: %d end screen elements already exist", video_id, existing)
                # Still try to ensure save if there are unsaved changes
                if self._has_save_button(page):
                    logger.info("Save button visible — saving pending changes")
                    return self._click_save(page, video_id)
                page.close()
                return True

            # -- Add "Suscribirse" element --
            human_delay(1.0, 2.5, "endscreen: open menu for subscribe")
            self._click_add_button(page)
            human_delay(1.5, 3.0, "endscreen: menu open")
            self._click_menu_item(page, "Suscribirse")
            human_delay(2.0, 4.0, "endscreen: subscribe added")

            # -- Add "Vídeo" element --
            human_delay(1.0, 2.5, "endscreen: open menu for video")
            self._click_add_button(page)
            human_delay(1.5, 3.0, "endscreen: menu open for video")
            self._click_menu_item(page, "Vídeo")
            human_delay(8.0, 15.0, "endscreen: video picker loading")
            self._handle_video_picker(page)
            human_delay(2.0, 4.0, "endscreen: video picker done")

            # -- Save --
            return self._click_save(page, video_id)

        except PlaywrightTimeout as e:
            logger.error("End screen timeout for %s: %s", video_id, e)
            try: page.close()
            except Exception: pass
            return False
        except Exception as e:
            logger.error("End screen error for %s: %s", video_id, e)
            try: page.close()
            except Exception: pass
            return False

    def _enter_edit_mode(self, page) -> bool:
        """Click 'Editar' if visible to enter end screen edit mode."""
        try:
            edit_btn = page.query_selector(SEL_EDIT_BTN)
            if edit_btn and edit_btn.is_visible():
                logger.debug("Clicking 'Editar' to enter end screen mode")
                edit_btn.click()
                human_delay(2.0, 4.0, "edit mode enter")
                return True
        except Exception:
            pass
        return False

    def _check_existing_elements(self, page) -> int:
        """Count existing end screen elements on the timeline."""
        try:
            count = page.evaluate("""() => {
                const triggers = document.querySelectorAll('ytcp-dropdown-trigger');
                let n = 0;
                for (const t of triggers) {
                    if (t.offsetParent) n++;
                }
                return n;
            }""")
            return int(count)
        except Exception:
            return 0

    def _has_save_button(self, page) -> bool:
        """Check if save button is visible and enabled."""
        try:
            result = page.evaluate("""() => {
                const btn = document.querySelector('#save-button');
                if (!btn || !btn.offsetParent) return false;
                return !(btn.hasAttribute('disabled') || btn.getAttribute('aria-disabled') === 'true');
            }""")
            return bool(result)
        except Exception:
            return False

    def _click_add_button(self, page):
        """Click the button to open the add-element menu."""
        page.evaluate("""() => {
            const btn = document.querySelector('#add-element-menu-button')
                || document.querySelector('#add-endscreen-icon-button');
            if (btn) btn.click();
        }""")

    def _click_menu_item(self, page, text: str):
        """Click a menu item by exact text match (text-based, not ID-based)."""
        page.evaluate("""(text) => {
            const items = document.querySelectorAll('tp-yt-paper-item, paper-item');
            for (const item of items) {
                if ((item.textContent || '').trim() === text && item.offsetParent) {
                    item.click();
                    return;
                }
            }
        }""", text)

    def _handle_video_picker(self, page):
        """Handle the video picker dialog: select first option or close it."""
        try:
            dlg = page.query_selector('[role="dialog"], ytcp-dialog')
            if not dlg or not dlg.is_visible():
                return

            human_delay(1.0, 2.0, "video picker visible")
            # Wait for content to load (up to 20s)
            for _ in range(10):
                option_count = page.evaluate("""() => {
                    const d = document.querySelector('[role="dialog"], ytcp-dialog');
                    if (!d || !d.offsetParent) return -1;
                    const items = d.querySelectorAll(
                        '[role="radio"], [role="option"], tp-yt-paper-item, paper-item');
                    let n = 0;
                    for (const it of items) {
                        if (it.offsetParent && (it.textContent || '').trim().length > 3) n++;
                    }
                    return n;
                }""")
                if option_count == -1:
                    logger.debug("Video picker dialog closed")
                    break
                if option_count > 0:
                    logger.debug("Video picker: %d options, selecting first", option_count)
                    page.evaluate("""() => {
                        const d = document.querySelector('[role="dialog"]');
                        const items = d.querySelectorAll(
                            '[role="radio"], [role="option"], tp-yt-paper-item');
                        for (const it of items) {
                            if (it.offsetParent && (it.textContent || '').trim().length > 3) {
                                it.click();
                                return;
                            }
                        }
                    }""")
                    human_delay(1.0, 2.0, "video option selected")
                    break
                time.sleep(2)

            # Dismiss dialog if still open
            human_delay(1.0, 2.0, "dismiss video picker")
            try:
                close_btn = dlg.query_selector('button:has-text("Cerrar")')
                if close_btn and close_btn.is_visible():
                    close_btn.click()
            except Exception:
                page.keyboard.press("Escape")

        except Exception as e:
            logger.debug("Video picker handling: %s", e)
            try: page.keyboard.press("Escape")
            except Exception: pass

    def _click_save(self, page, video_id: str) -> bool:
        """Click the save button and verify success."""
        human_delay(1.0, 2.0, "pre-save")
        save_visible = page.evaluate("""() => {
            const btn = document.querySelector('#save-button');
            if (!btn || !btn.offsetParent) return false;
            return !(btn.hasAttribute('disabled') || btn.getAttribute('aria-disabled') === 'true');
        }""")

        if not save_visible:
            logger.warning("Save button not available for %s", video_id)
            page.close()
            return False

        human_delay(0.8, 2.0, "click save")
        page.evaluate("document.querySelector('#save-button')?.click()")
        human_delay(8.0, 16.0, "save processing")

        # Verify: save button disappears on success
        for _ in range(10):
            gone = page.evaluate(
                "() => !document.querySelector('#save-button')?.offsetParent"
            )
            if gone:
                logger.info("End screens saved successfully for %s", video_id)
                page.close()
                return True
            time.sleep(1)

        logger.warning("Save button still visible after save for %s", video_id)
        page.close()
        return False


def get_browser(account: str) -> YouTubeBrowser:
    with _browser_lock:
        if account not in _browser_instances:
            _browser_instances[account] = YouTubeBrowser(account)
        return _browser_instances[account]


def close_all_browsers():
    with _browser_lock:
        for b in _browser_instances.values():
            try: b.close()
            except Exception: pass
        _browser_instances.clear()


def get_account_for_channel(channel_slug: str) -> Optional[str]:
    return CHANNEL_ACCOUNT_MAP.get(channel_slug)


if __name__ == "__main__":
    import argparse, sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--video-id", required=True)
    args = parser.parse_args()
    browser = get_browser(args.account)
    success = browser.mark_altered_content(args.video_id)
    if success:
        print(f"Video {args.video_id} marked as AI-generated content")
    else:
        print(f"Failed to mark video {args.video_id}")
        sys.exit(1)
