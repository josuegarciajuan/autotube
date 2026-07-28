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

# -- Selectors (confirmed working 2026-07-17, updated 2026-07-21) --
SEL_MOSTRAR_MAS = "text=Mostrar más"
# Fallback selectors for "Mostrar más" (YouTube changes locales/classes often)
_MOSTRAR_MAS_FALLBACKS = [
    "text=Mostrar más",
    "text=Show more",
    "button:has-text('Mostrar más')",
    "button:has-text('Show more')",
    "[aria-label='Mostrar más']",
    "[aria-label='Show more']",
]
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

# Shared Playwright instances — one PER THREAD because sync_playwright()'s
# greenlet is tied to the calling thread.  Using a thread that didn't create
# the Playwright instance causes "cannot switch to a different thread".
_thread_local = threading.local()
_playwright_lock = threading.Lock()


def _get_or_create_playwright():
    """Return a SyncPlaywright instance bound to the CURRENT thread.

    Each OS thread gets its own instance because sync_playwright()'s
    greenlet-based event loop is tied to the calling thread.  A global
    singleton shared across threads is what caused the "cannot switch
    to a different thread (which happens to have exited)" errors.
    """
    pw = getattr(_thread_local, "playwright", None)
    if pw is not None:
        return pw
    with _playwright_lock:
        # Double-check after acquiring lock
        pw = getattr(_thread_local, "playwright", None)
        if pw is not None:
            return pw
        _ensure_xvfb()
        pw = sync_playwright().start()
        _thread_local.playwright = pw
        logger.debug("Playwright instance started for thread %s", str(threading.get_ident())[-6:])
        return pw


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
        self.user_data_dir = TOKENS_DIR / f"{account}_browser_profile"
        self._playwright = None
        self._context = None
        self._owning_thread_id: int | None = None  # thread that created _context
        self._lock = threading.Lock()
        if not self.user_data_dir.exists():
            raise FileNotFoundError(
                f"Browser profile not found: {self.user_data_dir}\n"
                f"Run: python3 scripts/yt_browser_login.py --account {account}"
            )

    def _ensure_browser(self):
        """Ensure browser context exists and belongs to the CURRENT thread.

        Playwright's sync API uses greenlets tied to the thread that called
        ``sync_playwright().start()``.  If a daemon thread creates the context
        and exits, the next thread reusing the cached context hits
        "cannot switch to a different thread (which happens to have exited)".

        This method detects thread changes and recreates the context (and
        the underlying Playwright instance) so every caller thread owns its
        own greenlet.
        """
        current_thread = threading.get_ident()

        # ── Same thread — context is valid ──
        if self._context is not None and self._owning_thread_id == current_thread:
            return

        # ── Different thread (or first call) — tear down old context ──
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
            self._owning_thread_id = None

        # ── Get a Playwright instance owned by THIS thread ──
        self._playwright = _get_or_create_playwright()

        # Clean up stale Chromium singleton locks from killed/interrupted sessions
        self._cleanup_stale_locks()

        # ── Launch with retry for browser session contention ──
        last_error = None
        for attempt in range(1, 4):
            try:
                self._context = self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.user_data_dir),
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-gpu", "--disable-software-rasterizer"],
                    viewport={"width": 1280, "height": 900},
                    locale="es-ES",
                    timezone_id="Europe/Madrid",
                )
                self._owning_thread_id = current_thread
                logger.info("Browser ready for account: %s (thread %s, attempt %d)",
                            self.account, str(current_thread)[-6:], attempt)
                return
            except Exception as e:
                last_error = e
                err_msg = str(e)
                if "existing browser session" in err_msg.lower() or "singletonlock" in err_msg.lower():
                    if attempt < 3:
                        wait_s = 15 * attempt
                        logger.warning(
                            "[ES] Browser profile locked for %s (attempt %d/3) — "
                            "killing orphans and retrying in %ds...",
                            self.account, attempt, wait_s,
                        )
                        self._kill_profile_orphans()
                        self._remove_singleton_files()
                        time.sleep(wait_s)
                    else:
                        logger.error(
                            "[ES] Browser profile still locked after 3 attempts for %s",
                            self.account,
                        )
                else:
                    logger.error("Browser launch failed for %s: %s", self.account, err_msg)
                    break

        raise RuntimeError(
            f"Failed to launch browser for {self.account} after 3 attempts: {last_error}"
        )

    def _cleanup_stale_locks(self):
        """Remove Chromium singleton locks left by killed/interrupted sessions.

        Chromium writes hostname:PID to SingletonLock. If this method is called
        when we don't own a browser yet, ANY Chrome process using this profile
        is an orphan (from a crashed run or a transient check_session_valid call)
        and must be killed before we can launch our own.

        The browser profile (cookies, logins) is preserved.
        """
        lock_file = self.user_data_dir / "SingletonLock"
        if not lock_file.exists():
            return
        try:
            lock_content = lock_file.read_text().strip()
            if not lock_content:
                self._remove_singleton_files()
                return
            lock_pid = lock_content.split(":")[-1]
            pid = int(lock_pid)
            # Check if the PID is alive
            try:
                os.kill(pid, 0)
            except (OSError, ValueError):
                # PID is dead — but there may still be orphan renderer/GPU
                # processes holding file handles on the profile directory.
                # Kill them all before launching a fresh browser.
                logger.info("Lock PID %s is dead for %s — cleaning orphans", pid, self.account)
                self._kill_profile_orphans()
                self._remove_singleton_files()
                logger.info("Cleaned stale browser locks (dead PID %s) for %s", pid, self.account)
                return

            # PID is alive. If this process is NOT the one we own,
            # it's an orphan from a previous crashed run or a transient
            # session check. Wait briefly for it to die, then force-kill.
            logger.warning("Profile in use by PID %s for %s — waiting for release...",
                           pid, self.account)
            for attempt in range(15):  # 15 × 2s = 30s max wait
                time.sleep(2)
                try:
                    os.kill(pid, 0)
                except OSError:
                    # PID died while we waited — kill orphans + clean locks
                    self._kill_profile_orphans()
                    self._remove_singleton_files()
                    logger.info("Lock released after %.0fs for %s", (attempt + 1) * 2, self.account)
                    return
            # Still alive after 30s — kill everything using this profile
            logger.warning("Force-killing stale Chrome PID %s for %s", pid, self.account)
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            self._kill_profile_orphans()
            time.sleep(1)
            self._remove_singleton_files()
        except Exception as e:
            logger.debug("Lock check skipped: %s", e)

    def _kill_profile_orphans(self):
        """Kill all Chrome processes using this account's browser profile.

        Handles zombie renderers, GPU processes, and crashed browser instances
        that survived after the main browser PID died or was reused.
        """
        profile_dir = str(self.user_data_dir)
        import subprocess
        try:
            # pgrep is faster and more reliable than pkill -f
            r = subprocess.run(
                ["pgrep", "-f", profile_dir],
                capture_output=True, text=True, timeout=5,
            )
            if r.stdout.strip():
                pids = r.stdout.strip().split()
                logger.debug("Found %d orphan processes for %s: %s",
                             len(pids), self.account, " ".join(pids))
                subprocess.run(
                    ["kill", "-9"] + pids,
                    capture_output=True, timeout=5,
                )
        except subprocess.TimeoutExpired:
            logger.debug("pgrep timed out for %s — falling back to pkill", self.account)
            try:
                subprocess.run(
                    ["pkill", "-9", "-f", profile_dir],
                    capture_output=True, timeout=8,
                )
            except Exception:
                pass
        except Exception:
            pass

    def _remove_singleton_files(self):
        """Delete Chromium singleton lock files for this account's profile."""
        for fname in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            fpath = self.user_data_dir / fname
            if fpath.exists():
                try:
                    fpath.unlink()
                except OSError:
                    pass

    def _scroll_page_to_bottom(self, page):
        """Scroll YouTube Studio's inner scrollable containers to the bottom.

        YouTube Studio renders content inside nested scrollable divs — not just
        the outer window. window.scrollTo() alone doesn't work.
        This scrolls ALL inner containers that have overflow:auto/scroll,
        plus the outer window as fallback.
        """
        page.evaluate("""() => {
            const all = document.querySelectorAll('*');
            for (const el of all) {
                try {
                    const style = getComputedStyle(el);
                    if ((style.overflowY === 'auto' || style.overflowY === 'scroll')
                        && el.scrollHeight > el.clientHeight + 5) {
                        el.scrollTop = el.scrollHeight;
                    }
                } catch (_) {}
            }
            window.scrollTo(0, document.body.scrollHeight);
        }""")

    def _find_and_click_mostrar_mas(self, page) -> bool:
        """Try multiple selectors to find and click 'Mostrar más' button.

        YouTube changes DOM classes/locales frequently — this tries 6 variants.
        Returns True if found and clicked, False otherwise.
        """
        for sel in _MOSTRAR_MAS_FALLBACKS:
            try:
                el = page.wait_for_selector(sel, timeout=5000, state="visible")
                if el:
                    logger.debug("'Mostrar más' found via: %s", sel)
                    el.click()
                    return True
            except PlaywrightTimeout:
                continue
        logger.warning("'Mostrar más' not found with any of %d selectors",
                       len(_MOSTRAR_MAS_FALLBACKS))
        return False

    def close(self):
        with self._lock:
            try:
                if self._context:
                    self._context.close()
            except Exception:
                pass
            self._context = None
            self._owning_thread_id = None

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
            page.goto(edit_url, wait_until="domcontentloaded", timeout=60000)
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

            # Scroll inner containers (not just window) to reveal "Mostrar más" button
            self._scroll_page_to_bottom(page)
            human_delay(1.0, 2.0, "post-scroll")

            # Try multiple selectors for "Mostrar más" with fallbacks
            found_mostrar = self._find_and_click_mostrar_mas(page)
            if found_mostrar:
                human_delay(1.5, 3.0, "expand")
            else:
                # Section may already be expanded, continue
                human_delay(0.5, 1.0, "no expand needed")

            # Scroll again to reveal radio buttons after expansion
            self._scroll_page_to_bottom(page)
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
            logger.info("[ES] Navigating to end screen editor: %s", editor_url)
            page.goto(editor_url, wait_until="commit", timeout=60000)
            human_delay(6.0, 10.0, "endscreen: page load")

            logger.info("[ES] Landed on URL: %s | title: %s", page.url[:120], page.title())
            if "/video/" not in page.url:
                logger.error("[ES] Navigation failed — not a video page: %s", page.url[:120])
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
            logger.info("[ES] Overlay removed (promo + welcome)")
            human_delay(2.0, 4.0, "endscreen: post-overlay")

            # -- Enter edit mode if needed --
            edit_clicked = self._enter_edit_mode(page)
            logger.info("[ES] Edit mode entry: %s", "clicked" if edit_clicked else "not needed")
            human_delay(2.0, 4.0, "endscreen: edit mode settle")

            # -- Check if end screens already exist --
            existing = self._check_existing_elements(page)
            logger.info("[ES] Existing end screen elements found: %d", existing)
            if existing > 0:
                logger.info("[ES] Video %s: %d end screen elements already exist — verifying save", video_id, existing)
                # Still try to ensure save if there are unsaved changes
                if self._has_save_button(page):
                    logger.info("[ES] Save button visible — saving pending changes")
                    return self._click_save(page, video_id)
                logger.info("[ES] No save needed — elements already saved")
                page.close()
                return True

            # -- Add "Suscribirse" element --
            human_delay(1.0, 2.5, "endscreen: open menu for subscribe")
            logger.info("[ES] Clicking 'Add element' button (subscribe)")
            self._click_add_button(page)
            human_delay(1.5, 3.0, "endscreen: menu open")
            logger.info("[ES] Selecting 'Suscribirse' from menu")
            self._click_menu_item(page, "Suscribirse")
            logger.info("[ES] 'Suscribirse' element added")
            human_delay(2.0, 4.0, "endscreen: subscribe added")

            # -- Add "Vídeo" element --
            human_delay(1.0, 2.5, "endscreen: open menu for video")
            logger.info("[ES] Clicking 'Add element' button (video)")
            self._click_add_button(page)
            human_delay(1.5, 3.0, "endscreen: menu open for video")
            logger.info("[ES] Selecting 'Vídeo' from menu")
            self._click_menu_item(page, "Vídeo")
            logger.info("[ES] 'Vídeo' element selected — waiting for video picker")
            human_delay(8.0, 15.0, "endscreen: video picker loading")
            self._handle_video_picker(page)
            logger.info("[ES] Video picker handled")
            human_delay(2.0, 4.0, "endscreen: video picker done")

            # -- Save --
            logger.info("[ES] Attempting to save end screens...")
            return self._click_save(page, video_id)

        except PlaywrightTimeout as e:
            logger.error("[ES] End screen timeout for %s: %s", video_id, e)
            try: page.close()
            except Exception: pass
            return False
        except Exception as e:
            logger.error("[ES] End screen error for %s: %s", video_id, e)
            try: page.close()
            except Exception: pass
            return False

    def _enter_edit_mode(self, page) -> bool:
        """Click 'Editar' if visible to enter end screen edit mode."""
        try:
            edit_btn = page.query_selector(SEL_EDIT_BTN)
            if edit_btn and edit_btn.is_visible():
                logger.info("[ES] Clicking 'Editar' to enter end screen mode")
                edit_btn.click()
                human_delay(2.0, 4.0, "edit mode enter")
                return True
        except Exception:
            pass
        logger.info("[ES] Edit mode not needed — already in editor")
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
        found = page.evaluate("""() => {
            const btn = document.querySelector('#add-element-menu-button')
                || document.querySelector('#add-endscreen-icon-button');
            if (btn) { btn.click(); return true; }
            return false;
        }""")
        logger.info("[ES] Add-element button %s", "clicked" if found else "NOT FOUND")

    def _click_menu_item(self, page, text: str):
        """Click a menu item by exact text match (text-based, not ID-based)."""
        found = page.evaluate("""(text) => {
            const items = document.querySelectorAll('tp-yt-paper-item, paper-item');
            for (const item of items) {
                if ((item.textContent || '').trim() === text && item.offsetParent) {
                    item.click();
                    return true;
                }
            }
            return false;
        }""", text)
        logger.info("[ES] Menu item '%s' %s", text, "clicked" if found else "NOT FOUND")

    def _handle_video_picker(self, page):
        """Handle the video picker dialog: select first option or close it."""
        try:
            dlg = page.query_selector('[role="dialog"], ytcp-dialog')
            if not dlg or not dlg.is_visible():
                logger.info("[ES] No video picker dialog — skipped")
                return

            human_delay(1.0, 2.0, "video picker visible")
            logger.info("[ES] Video picker dialog detected — waiting for options")
            # Wait for content to load (up to 20s)
            for i in range(10):
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
                    logger.info("[ES] Video picker dialog closed")
                    break
                if option_count > 0:
                    logger.info("[ES] Video picker: %d options found, selecting first", option_count)
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
                logger.debug("[ES] Video picker: waiting for options... (round %d)", i + 1)
                time.sleep(2)

            # Dismiss dialog if still open
            human_delay(1.0, 2.0, "dismiss video picker")
            try:
                close_btn = dlg.query_selector('button:has-text("Cerrar")')
                if close_btn and close_btn.is_visible():
                    close_btn.click()
                    logger.info("[ES] Video picker closed via 'Cerrar' button")
            except Exception:
                page.keyboard.press("Escape")
                logger.info("[ES] Video picker dismissed via Escape")

        except Exception as e:
            logger.debug("[ES] Video picker handling: %s", e)
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
            logger.warning("[ES] Save button not available for %s", video_id)
            page.close()
            return False

        logger.info("[ES] Save button found and enabled — clicking...")
        human_delay(0.8, 2.0, "click save")
        page.evaluate("document.querySelector('#save-button')?.click()")
        human_delay(8.0, 16.0, "save processing")

        # Verify: save button disappears on success
        for i in range(10):
            gone = page.evaluate(
                "() => !document.querySelector('#save-button')?.offsetParent"
            )
            if gone:
                logger.info("[ES] ✅ End screens saved successfully for %s (confirmed at round %d)", video_id, i + 1)
                page.close()
                return True
            time.sleep(1)

        logger.warning("[ES] Save button still visible after save for %s — may not have applied", video_id)
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
    # Clean up thread-local Playwright instances.
    # Each thread must stop its own — we can only stop the current thread's.
    pw = getattr(_thread_local, "playwright", None)
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass
        _thread_local.playwright = None


def get_account_for_channel(channel_slug: str) -> Optional[str]:
    return CHANNEL_ACCOUNT_MAP.get(channel_slug)


# ── Session health check ───────────────────────────────────────

_session_check_cache: dict = {}  # {account: (timestamp, status_dict)}


async def check_session_valid(account: str, cache_seconds: int = 300) -> dict:
    """Check if a browser session is still authenticated.
    
    Launches a temporary browser with the persistent profile, navigates
    to YouTube Studio, and checks we aren't redirected to login.
    Results are cached for `cache_seconds` (default 5 min).
    
    Returns a dict with keys:
        status: "valid" | "expired" | "in_use" | "error" | "missing_profile"
        detail: human-readable explanation
    """
    import time as _time
    now = _time.time()
    if account in _session_check_cache:
        ts, cached = _session_check_cache[account]
        if now - ts < cache_seconds:
            return cached

    # ── Shortcut: if browser is already alive for this account, skip ──
    # the expensive Chromium launch. The persistent session stays valid
    # across the API's 5-minute check cycle. Re-check only on failure.
    if account in _browser_instances and _browser_instances[account]._context is not None:
        result = {"status": "valid", "detail": "Browser session alive (cached)"}
        _session_check_cache[account] = (now, result)
        return result

    from pathlib import Path as _Path
    user_data_dir = TOKENS_DIR / f"{account}_browser_profile"
    if not user_data_dir.exists():
        result = {"status": "missing_profile", "detail": f"Browser profile not found at {user_data_dir}"}
        _session_check_cache[account] = (now, result)
        logger.warning("Browser profile missing for %s: %s", account, user_data_dir)
        return result

    # ── Check if profile is already in use by a running Chromium ──
    lock_file = user_data_dir / "SingletonLock"
    if lock_file.exists():
        try:
            lock_content = lock_file.read_text().strip()
            if lock_content:
                lock_pid = lock_content.split(":")[-1]
                try:
                    os.kill(int(lock_pid), 0)  # signal 0 = check existence
                    result = {
                        "status": "in_use",
                        "detail": f"Browser profile is currently in use by PID {lock_pid}"
                    }
                    _session_check_cache[account] = (now, result)
                    logger.debug("Session check skipped for %s — profile in use by PID %s", account, lock_pid)
                    return result
                except (OSError, ValueError):
                    # PID is dead — clean stale locks
                    for fname in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                        fpath = user_data_dir / fname
                        if fpath.exists():
                            fpath.unlink()
                    logger.info("Cleaned stale browser locks for %s", account)
        except Exception:
            pass  # lock file corrupted/unreadable, proceed to launch

    status = "error"
    detail = ""
    try:
        _ensure_xvfb()
        from playwright.async_api import async_playwright as _async_pw
        pw = await _async_pw().start()
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-gpu", "--disable-software-rasterizer"],
            viewport={"width": 1280, "height": 900},
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=30000)
            import asyncio as _asyncio
            await _asyncio.sleep(3)
            current_url = page.url
            if "studio.youtube.com" in current_url and "accounts.google.com" not in current_url:
                status = "valid"
                detail = "Session is authenticated"
                logger.debug("Session valid for %s (url: %s)", account, current_url[:80])
            else:
                status = "expired"
                detail = "Redirected to login — re-authentication required"
                logger.warning("Session EXPIRED for %s (redirected to: %s)", account, current_url[:120])
        finally:
            try: await ctx.close()
            except Exception: pass
            try: await pw.stop()
            except Exception: pass
    except Exception as e:
        error_msg = str(e)
        # Detect profile-in-use by Chromium's error message (fallback if SingletonLock check missed it)
        if "already in use" in error_msg.lower() or "singletonlock" in error_msg.lower():
            status = "in_use"
            detail = f"Browser profile is currently in use by another process"
        else:
            status = "error"
            detail = f"Could not verify session: {error_msg[:200]}"
        # in_use is expected when a persistent browser is running — not a warning
        if status == "in_use":
            logger.debug("check_session_valid for %s: profile in use (expected)", account)
        else:
            logger.warning("check_session_valid error for %s: %s", account, e)

    result = {"status": status, "detail": detail}
    _session_check_cache[account] = (now, result)
    return result


async def get_all_browser_session_status() -> list:
    """Return status for all configured browser accounts.
    
    Returns: list of dicts with keys: account, valid, status, detail, channels, profile_exists
    """
    from pathlib import Path as _Path
    result = []
    seen_accounts = set()
    for slug, account in CHANNEL_ACCOUNT_MAP.items():
        if account in seen_accounts:
            # Append channel to existing entry
            for r in result:
                if r["account"] == account:
                    r["channels"].append(slug)
                    break
            continue
        seen_accounts.add(account)
        profile_exists = (TOKENS_DIR / f"{account}_browser_profile").exists()
        if profile_exists:
            status_dict = await check_session_valid(account)
        else:
            status_dict = {"status": "missing_profile", "detail": "Browser profile not found"}
        result.append({
            "account": account,
            "valid": status_dict["status"] == "valid",  # backward compat
            "status": status_dict["status"],
            "detail": status_dict["detail"],
            "profile_exists": profile_exists,
            "channels": [slug],
        })
    return result


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
