#!/usr/bin/env python3
"""
YouTube Studio Browser Automation — Core Module.

Reusable browser automation for YouTube Studio actions:
  - Mark video/short as "Altered content / Uso de IA" = Sí
  - Configure end screens (Subscribe + Video recommendation)

Uses Playwright + Xvfb for headless operation.
Persistent sessions stored in tokens/{account}_browser_session.json.
"""

import atexit
import logging
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# TOKENS_DIR override: permite ejecutar scripts desde una worktree (git) apuntando
# a los tokens/sesiones reales del árbol de producción (p. ej. emergencias).
# Ej: YT_BROWSER_TOKENS_DIR=/root/autotube/tokens python3 scripts/hold_...
TOKENS_DIR = Path(os.getenv("YT_BROWSER_TOKENS_DIR") or (PROJECT_ROOT / "tokens"))

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

# ── Global Playwright registry (fix: prevent driver process leaks) ──
# Every sync_playwright().start() spawns a Node.js driver child process.
# Without explicit .stop(), the driver survives parent thread/process exit.
# This registry tracks ALL created instances so they can be cleaned up.
_playwright_registry: set = set()
_registry_lock = threading.Lock()


def _register_playwright(pw):
    """Track a Playwright instance for eventual cleanup."""
    with _registry_lock:
        _playwright_registry.add(pw)


def _unregister_playwright(pw):
    """Remove a Playwright instance from the registry (after it was stopped)."""
    with _registry_lock:
        _playwright_registry.discard(pw)


def _cleanup_all_playwrights():
    """Stop ALL known Playwright instances and clear the registry.

    Called by atexit and by close_all_browsers(). Safe to call multiple times.
    """
    with _registry_lock:
        instances = list(_playwright_registry)
        _playwright_registry.clear()
    for pw in instances:
        try:
            pw.stop()
        except Exception:
            pass


def _cleanup_current_thread_playwright():
    """Stop the Playwright instance owned by the CURRENT thread (if any).

    Used in daemon thread finally blocks — each thread should clean up
    its own instance to prevent leaked driver processes.
    """
    pw = getattr(_thread_local, "playwright", None)
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass
        _unregister_playwright(pw)
        _thread_local.playwright = None


# ── Register cleanup on normal process exit ──
atexit.register(_cleanup_all_playwrights)


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
        _register_playwright(pw)  # track for global cleanup
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
        current_thread = threading.get_native_id()

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
            # Reset playwright ref so _get_or_create_playwright() creates a clean instance
            # for the current thread (prevents "cannot switch to a different thread"
            # when old greenlet died with the previous thread)
            # CRITICAL: stop the old instance first to prevent driver process leaks
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                _unregister_playwright(self._playwright)
            self._playwright = None
            time.sleep(1.5)  # let Singletons-lock fully release

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

    # ── Comment reading / replying (watch page, 0 quota API) ────────────
    # Superficie: https://www.youtube.com/watch?v={id} (ytd-comment-*).
    # DOM validado 2026-08-24 contra videos reales. Cero cuota de Data API.

    def _wait_rotate_cookies(self, page, timeout_s: int = 60) -> bool:
        """Espera a que YouTube termine la rotación de cookies de sesión.

        Tras lanzar el navegador, YouTube a veces muestra un frame
        accounts.youtube.com/RotateCookiesPage que bloquea la sección de
        comentarios hasta que termina. Devuelve True cuando no queda ninguno.
        """
        for _ in range(max(1, timeout_s // 5)):
            try:
                if not any("RotateCookies" in f.url for f in page.frames):
                    return True
            except Exception:
                return True
            logger.debug("Esperando rotación de cookies...")
            time.sleep(5)
        logger.warning("Rotación de cookies no terminó en %ds", timeout_s)
        return False

    def _goto_watch_comments(self, page, video_id: str):
        """Navega a la watch page y hace scroll hasta la sección de comentarios."""
        url = f"https://www.youtube.com/watch?v={video_id}"
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self._wait_rotate_cookies(page)
        human_delay(4.0, 6.0, "watch load")
        try:
            page.evaluate(
                '() => document.getElementById("comments")?.scrollIntoView('
                '{behavior: "instant", block: "start"})'
            )
        except Exception:
            pass
        human_delay(2.0, 4.0, "comments scroll")
        try:
            page.mouse.wheel(0, 800)
        except Exception:
            pass
        human_delay(1.5, 3.0, "wheel settle")
        return page

    def _parse_comment_threads(self, page, max_comments: int = 50) -> list[dict]:
        """Extrae los hilos de comentarios visibles de la watch page.

        Returns [{index, author, text, reply_authors, has_reply_button, likes}].
        index = posición del hilo en el DOM (para targetear la respuesta).
        """
        try:
            data = page.evaluate("""(maxN) => {
                const threads = document.querySelectorAll('ytd-comment-thread-renderer');
                const out = [];
                for (let i = 0; i < threads.length && out.length < maxN; i++) {
                    const t = threads[i];
                    const author = (t.querySelector('#author-text')?.textContent || '')
                        .trim().replace(/^@/, '');
                    const content = (t.querySelector('#content-text')?.textContent || '')
                        .trim();
                    if (!content) continue;
                    const replyAuthors = Array.from(
                        t.querySelectorAll('ytd-comment-renderer #author-text')
                    ).map(e => (e.textContent || '').trim().replace(/^@/, ''));
                    const hasReplyBtn = !!(
                        t.querySelector('#reply-button-end') ||
                        t.querySelector('#reply-button') ||
                        t.querySelector('[aria-label*="Responder"]') ||
                        t.querySelector('[aria-label*="Reply"]')
                    );
                    const likes = (t.querySelector('#vote-count-middle')?.textContent || '')
                        .trim();
                    out.push({
                        index: i,
                        author,
                        text: content,
                        reply_authors: replyAuthors,
                        has_reply_button: hasReplyBtn,
                        likes,
                    });
                }
                return out;
            }""", max_comments)
            return data or []
        except Exception as exc:
            logger.warning("Could not parse comment threads: %s", exc)
            return []

    def list_video_comments(self, video_id: str, max_comments: int = 50) -> list[dict]:
        """Lee los comentarios públicos de un video vía watch page (0 cuota).

        Devuelve lista de dicts: {index, author, text, reply_authors,
        has_reply_button, likes}. Vacía si no hay comentarios o no carga.
        """
        with self._lock:
            self._ensure_browser()
            page = self._context.new_page()
            try:
                self._goto_watch_comments(page, video_id)
                comments = self._parse_comment_threads(page, max_comments)
                logger.info("Comments read for %s: %d hilos", video_id, len(comments))
                return comments
            except Exception as exc:
                logger.error("list_video_comments failed for %s: %s", video_id, exc)
                return []
            finally:
                page.close()

    def post_comment_reply(self, video_id: str, comment_index: int, text: str,
                           expected_text: str = None) -> bool:
        """Responde a un comentario vía watch page (0 cuota).

        Abre el cajón de respuesta del hilo `comment_index`, escribe `text`
        carácter a carácter (typing humano) y pulsa Comentar.

        Si `expected_text` se pasa, verifica antes de publicar que el hilo
        objetivo sigue conteniendo ese texto (evita responder al comentario
        equivocado si el orden del DOM cambió entre la lectura y el envío).
        """
        with self._lock:
            self._ensure_browser()
            page = self._context.new_page()
            try:
                self._goto_watch_comments(page, video_id)
                return self._do_post_comment_reply(
                    page, comment_index, text, expected_text
                )
            except Exception as exc:
                logger.error("post_comment_reply failed for %s idx %s: %s",
                             video_id, comment_index, exc)
                return False
            finally:
                page.close()

    def _do_post_comment_reply(self, page, comment_index: int, text: str,
                               expected_text: str = None) -> bool:
        # ── 1. Localizar el hilo objetivo y abrir "Responder" ──
        try:
            state = page.evaluate("""(idx) => {
                const ts = document.querySelectorAll('ytd-comment-thread-renderer');
                const t = ts[idx];
                if (!t) return 'no-thread';
                const content = (t.querySelector('#content-text')?.textContent || '')
                    .trim().slice(0, 60);
                try { t.scrollIntoView({block: 'center'}); } catch (_) {}
                const btn = t.querySelector('#reply-button-end') ||
                    t.querySelector('#reply-button') ||
                    t.querySelector('[aria-label*="Responder"]') ||
                    t.querySelector('[aria-label*="Reply"]');
                if (!btn) return 'no-reply-btn';
                btn.click();
                return content;
            }""", comment_index)
        except Exception as exc:
            logger.error("Reply open error: %s", exc)
            return False

        if state == "no-thread" or state == "no-reply-btn":
            logger.warning("Cannot open reply for idx %s: %s", comment_index, state)
            return False

        # Verificación anti-comentario-equivocado
        if expected_text:
            norm_expected = expected_text.strip().lower()[:60]
            norm_state = (state or "").strip().lower()
            if not norm_state or norm_expected[:30] not in norm_state:
                logger.warning(
                    "Thread idx %s no coincide con el comentario esperado "
                    "(DOM cambió) — abortando sin publicar", comment_index
                )
                return False

        human_delay(2.0, 4.0, "reply simplebox")

        # ── 2. Abrir el diálogo de respuesta ──
        try:
            page.evaluate(
                "() => document.querySelector("
                "'ytd-comment-simplebox-renderer #placeholder-area')?.click()"
            )
        except Exception:
            pass
        human_delay(1.5, 3.0, "reply dialog")

        # ── 3. Localizar el input contenteditable ──
        ce = page.locator(
            "ytd-comment-simplebox-renderer #contenteditable-root"
        ).first
        if ce.count() == 0:
            ce = page.locator("#comment-dialog #contenteditable-root").first
        if ce.count() == 0:
            logger.warning("Reply contenteditable not found (idx %s)", comment_index)
            return False

        ce.click()
        human_delay(0.5, 1.2, "focus reply")

        # ── 4. Escribir carácter a carácter (typing humano) ──
        for ch in text:
            try:
                page.keyboard.type(ch)
            except Exception:
                pass
            time.sleep(random.uniform(0.04, 0.13) + (0.06 if ch in ".,!?¿" else 0))
        human_delay(1.0, 2.2, "typed")

        # ── 5. Pulsar "Comentar" (solo si quedó habilitado) ──
        try:
            result = page.evaluate("""() => {
                const cd = document.querySelector(
                    'ytd-comment-simplebox-renderer #comment-dialog, #comment-dialog');
                if (!cd) return 'no-dialog';
                const btn = Array.from(cd.querySelectorAll('button')).find(b =>
                    (b.textContent || '').trim() === 'Comentar' ||
                    (b.textContent || '').trim() === 'Comment' ||
                    b.getAttribute('aria-label') === 'Comentar' ||
                    b.getAttribute('aria-label') === 'Comment');
                if (!btn) return 'no-btn';
                if (btn.disabled) return 'disabled';
                btn.click();
                return 'posted';
            }""")
        except Exception as exc:
            logger.error("Reply button error: %s", exc)
            return False

        if result != "posted":
            logger.warning("Reply not posted for idx %s: %s", comment_index, result)
            return False

        human_delay(2.0, 4.0, "post settle")

        # ── 6. Verificación: el simplebox debe haberse cerrado ──
        try:
            gone = page.evaluate(
                "() => { const sb = document.querySelector("
                "'ytd-comment-simplebox-renderer'); "
                "return !sb || !sb.offsetParent; }"
            )
        except Exception:
            gone = True
        if not gone:
            logger.warning("Reply box still open after posting — revisar")
        logger.info("✅ Reply posted for %s (idx %s): %s...", video_id,
                    comment_index, text[:50])
        return True

    def close(self):
        with self._lock:
            try:
                if self._context:
                    self._context.close()
            except Exception:
                pass
            self._context = None
            self._owning_thread_id = None
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                _unregister_playwright(self._playwright)
                self._playwright = None

    def mark_altered_content(self, youtube_video_id: str) -> bool:
        with self._lock:
            try:
                self._ensure_browser()
                page = self._context.new_page()
                ok = self._do_mark(page, youtube_video_id)
            except Exception as e:
                logger.error("mark_altered_content failed for %s: %s", youtube_video_id, e)
                ok = False
        if not ok:
            self._alert_mark_failed(youtube_video_id)
        return ok

    def _alert_mark_failed(self, youtube_video_id: str) -> None:
        """Alerta (una vez por vídeo) cuando falla el auto-marcado de
        'contenido alterado/IA' en YouTube Studio.

        Fallar en silencio deja el canal expuesto al flag de spam/IA de
        YouTube (antiban, ago 2026): sin el marcado, el contenido generado
        por IA es el principal desencadenante de eliminaciones.
        """
        try:
            from database.db_extended import ExtendedDatabase
            from api.services.lifecycle_monitor import create_alert
            db = ExtendedDatabase()
            key = f"altered_mark_alert_{youtube_video_id}"
            if db.get_system_state(key):
                return
            db.set_system_state(key, "1")
            channel_id = None
            try:
                slug = getattr(self, "account", "")
                ch = db.get_channel_by_slug(slug) if slug else None
                if ch:
                    channel_id = ch.get("id")
            except Exception:
                channel_id = None
            create_alert(
                db,
                entity_type="video" if channel_id else "channel",
                entity_id=channel_id,
                channel_id=channel_id,
                alert_type="altered_content_mark_failed",
                severity="warning",
                title=f"Auto-marcado 'contenido alterado/IA' falló para {youtube_video_id}",
                message=(
                    f"No se pudo marcar el vídeo {youtube_video_id} como contenido "
                    f"alterado/IA en YouTube Studio (Playwright). El canal queda expuesto "
                    f"al flag de spam/IA de YouTube. Revisa el marcado manualmente o "
                    f"verifica las sesiones de navegador (python3 scripts/yt_browser_login.py)."
                ),
                metadata={"video_id": youtube_video_id, "action": "marcado manual requerido"},
            )
        except Exception as exc:
            logger.warning("mark-altered alert failed: %s", exc)

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

    def set_video_private_unschedule(self, youtube_video_id: str) -> bool:
        """Pone un vídeo en 'Privado' (desprogramado) vía YouTube Studio — 0 cuota API.

        Anti-ráfaga (ago 2026): cuando la cuota del Data API está agotada, un vídeo
        subido como privado con publishAt vencido puede salir en ráfaga (el patrón
        que alimentó los strikes de spam). Este método abre el vídeo en Studio y
        selecciona 'Privado', cancelando cualquier publishAt programado: el vídeo
        queda físicamente incapaz de publicarse hasta que se re-programe después
        (repack con cuota libre).

        Usa la MISMA sesión de navegador/lock que mark_altered_content. Fail-safe:
        devuelve False si no puede cambiar/confirmar la visibilidad; nunca deja el
        vídeo en un estado peor que 'privado'.
        """
        with self._lock:
            try:
                self._ensure_browser()
                page = self._context.new_page()
                ok = self._do_set_private(page, youtube_video_id)
            except Exception as e:
                logger.error("set_video_private_unschedule failed for %s: %s", youtube_video_id, e)
                ok = False
            if not ok:
                self._alert_hold_failed(youtube_video_id)
            return ok

    def _alert_hold_failed(self, youtube_video_id: str) -> None:
        """Alerta (una vez por vídeo) cuando falla el hold a 'Privado' en Studio.

        Antiban (ago 2026): si el hold falla y el vídeo tiene publishAt vencido,
        puede publicarse en ráfaga. La alerta pide revisión humana (Studio manual).
        """
        try:
            from database.db_extended import ExtendedDatabase
            from api.services.lifecycle_monitor import create_alert
            db = ExtendedDatabase()
            key = f"publish_hold_alert_{youtube_video_id}"
            if db.get_system_state(key):
                return
            db.set_system_state(key, "1")
            channel_id = None
            try:
                slug = getattr(self, "account", "")
                ch = db.get_channel_by_slug(slug) if slug else None
                if ch:
                    channel_id = ch.get("id")
            except Exception:
                channel_id = None
            create_alert(
                db,
                entity_type="video" if channel_id else "channel",
                entity_id=channel_id,
                channel_id=channel_id,
                alert_type="publish_hold_failed",
                severity="warning",
                title=f"Hold a Privado falló para {youtube_video_id}",
                message=(
                    f"No se pudo poner el vídeo {youtube_video_id} en 'Privado' vía "
                    f"YouTube Studio (Playwright). Si tiene publishAt vencido puede "
                    f"publicarse en ráfaga. Pásalo a Privado manualmente en Studio o "
                    f"verifica la sesión del navegador (python3 scripts/yt_browser_login.py)."
                ),
                metadata={"video_id": youtube_video_id, "action": "hold manual requerido"},
            )
        except Exception as exc:
            logger.warning("publish-hold alert failed: %s", exc)

    def _do_set_private(self, page, video_id: str) -> bool:
        """Internal: set a video to 'Privado' (unscheduled) on the edit page.

        Flujo validado contra Studio real (ago 2026): el control de visibilidad
        es `ytcp-video-metadata-visibility` (#visibility-text). Al abrirlo sale el
        diálogo `ytcp-video-visibility-select` con dos contenedores:
          - #first-container ("Guardar o publicar", colapsado): radios Público/
            Oculto/Privado — se expande con #first-container-expand-button.
          - #second-container ("Programar", activo cuando el vídeo tiene
            publishAt programado): fecha + "Programar como público" + "Hecho".
        Para desprogramar y dejar el vídeo en Privado: expandir el primer
        contenedor, seleccionar Público→Privado (fuerza la deselección de
        "Programar"), "Hecho", y Guardar. Fail-safe: cada paso verifica.
        """
        try:
            human_delay(1.0, 3.0, "hold: initial")
            edit_url = f"https://studio.youtube.com/video/{video_id}/edit"
            page.goto(edit_url, wait_until="domcontentloaded", timeout=60000)
            human_delay(4.0, 8.0, "hold: page load")
            if "/video/" not in page.url or "/edit" not in page.url:
                logger.error("[HOLD] Navigation failed: %s", page.url[:120])
                page.close()
                return False

            try:
                page.wait_for_selector("[id='title-textarea']", timeout=10000, state="visible")
            except PlaywrightTimeout:
                logger.warning("[HOLD] Title field not found, continuing")
            human_delay(2.0, 4.0, "hold: editor settle")

            # ── Current visibility state ──
            vis_el = page.query_selector("ytcp-video-metadata-visibility")
            if not vis_el:
                logger.error("[HOLD] Visibility control not found for %s — fail-safe", video_id)
                page.close()
                return False
            try:
                current = (vis_el.inner_text() or "").strip()
            except Exception:
                current = ""
            logger.info("[HOLD] %s current visibility: %r", video_id, current)

            if "privado" in current.lower() or "private" in current.lower():
                logger.info("[HOLD] %s already private — no-op", video_id)
                page.close()
                return True

            # ── Open the visibility dialog ──
            human_delay(0.5, 1.5, "hold: open dialog")
            try:
                vis_el.click()
                human_delay(1.5, 3.0, "hold: dialog open")
            except Exception as e:
                logger.error("[HOLD] Could not open visibility dialog: %s", e)
                page.close()
                return False

            # ── Expand first container (Guardar o publicar) if collapsed ──
            exp_btn = page.query_selector("ytcp-icon-button#first-container-expand-button")
            if exp_btn and exp_btn.is_visible():
                exp_btn.click()
                human_delay(1.5, 3.0, "hold: first container expanded")
            else:
                logger.info("[HOLD] First container already expanded (or no expand btn)")

            # ── Select Público → Privado (forces deselection of 'Programar') ──
            def _click_radio(text: str) -> bool:
                for el in page.query_selector_all("tp-yt-paper-radio-button"):
                    try:
                        t = (el.inner_text() or "").strip().split("\n")[0]
                        if t == text and el.is_visible():
                            el.click()
                            return True
                    except Exception:
                        continue
                return False

            if not _click_radio("Público"):
                logger.warning("[HOLD] 'Público' radio not found for %s — trying Privado directly", video_id)
            human_delay(0.5, 1.5, "hold: publico")
            if not _click_radio("Privado"):
                logger.error("[HOLD] 'Privado' radio not found for %s — aborting (fail-safe)", video_id)
                page.close()
                return False
            human_delay(0.8, 1.8, "hold: privado")

            # ── Click 'Hecho' (Done) to apply ──
            hecho = None
            for _ in range(10):
                hecho = page.query_selector("text=Hecho")
                if hecho and hecho.is_visible():
                    break
                time.sleep(1)
            if not hecho:
                logger.error("[HOLD] 'Hecho' not found for %s — aborting (fail-safe)", video_id)
                page.close()
                return False
            hecho.click()
            human_delay(1.5, 3.0, "hold: done clicked")

            # ── Verify dialog applied: visibility-text must show 'Privado' ──
            try:
                vis_after = (vis_el.inner_text() or "").strip()
            except Exception:
                vis_after = ""
            if "privado" not in vis_after.lower() and "private" not in vis_after.lower():
                logger.error(
                    "[HOLD] Visibility NOT 'Privado' after dialog for %s (still %r) — fail-safe",
                    video_id, vis_after,
                )
                page.close()
                return False
            logger.info("[HOLD] %s dialog applied → %r", video_id, vis_after)

            # ── Save ──
            save_el = None
            for _ in range(30):
                save_el = page.query_selector(SEL_GUARDAR_ENABLED)
                if save_el and save_el.is_enabled():
                    break
                time.sleep(1)
            if not save_el:
                logger.error("[HOLD] Save never enabled for %s", video_id)
                page.close()
                return False

            human_delay(0.8, 2.0, "hold: click save")
            save_el.click()
            human_delay(2.0, 4.0, "hold: save settling")

            try:
                page.wait_for_selector(SEL_SAVE_CONFIRM, timeout=5000, state="attached")
                logger.info("[HOLD] Save confirmed for %s", video_id)
            except PlaywrightTimeout:
                logger.info("[HOLD] No save toast for %s (clicked anyway)", video_id)

            logger.info("[HOLD] ✅ %s confirmado como 'Privado'", video_id)
            page.close()
            return True
        except PlaywrightTimeout as e:
            logger.error("[HOLD] Timeout for %s: %s", video_id, e)
            try: page.close()
            except Exception: pass
            return False
        except Exception as e:
            logger.error("[HOLD] Error for %s: %s", video_id, e)
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

    # ── Short → Long-form video linking ──────────────────────────────

    def link_longform_video(self, short_yt_id: str, longform_yt_id: str) -> bool:
        """Link a long-form video as the "Related video" on a YouTube Short.

        YouTube Data API v3 has NO endpoint for this. It must be done via
        YouTube Studio browser automation. Navigates to the Short's edit
        page, finds the "Video relacionado" section, searches for the
        long-form video, selects it, and saves.

        Args:
            short_yt_id: YouTube video ID of the Short.
            longform_yt_id: YouTube video ID of the long-form video to link.

        Returns:
            True if the link was set successfully, False otherwise.
        """
        with self._lock:
            try:
                self._ensure_browser()
                page = self._context.new_page()
                return self._do_link_longform(page, short_yt_id, longform_yt_id)
            except Exception as e:
                logger.error("link_longform_video failed for %s → %s: %s",
                             short_yt_id, longform_yt_id, e)
                return False

    def _do_link_longform(self, page, short_yt_id: str, longform_yt_id: str) -> bool:
        """Internal: execute the related-video linking flow on a fresh page.

        Includes a retry loop: if YouTube Studio shows a 'processing' indicator
        (video not yet ready for editing), the method waits with backoff and
        retries up to 3 times before giving up.
        """
        edit_url = f"https://studio.youtube.com/video/{short_yt_id}/edit"
        max_retries = 3
        retry_delays = [15, 30, 60]  # seconds between retries

        for attempt in range(max_retries):
            # ── Navigate ────────────────────────────────────────
            human_delay(1.0, 3.0, f"longform: initial (attempt {attempt + 1})")
            logger.info("Longform link: navigating to %s (attempt %d/%d)",
                        edit_url, attempt + 1, max_retries)
            page.goto(edit_url, wait_until="domcontentloaded", timeout=60000)
            human_delay(4.0, 8.0, "longform: page load")

            if "/video/" not in page.url or "/edit" not in page.url:
                logger.error("Longform link: navigation failed for %s: %s",
                             short_yt_id, page.url[:120])
                if attempt < max_retries - 1:
                    logger.info("Longform link: retrying after navigation failure...")
                    time.sleep(retry_delays[attempt])
                    continue
                page.close()
                return False

            # ── Check for "processing" state ────────────────────
            # YouTube may still be transcoding the Short; the edit page will
            # show a spinner or "Procesando" message and won't have the editor UI.
            if self._page_shows_processing(page, short_yt_id):
                if attempt < max_retries - 1:
                    wait = retry_delays[attempt]
                    logger.info("Longform link: video %s still processing — "
                                "waiting %ds before retry %d/%d",
                                short_yt_id, wait, attempt + 2, max_retries)
                    page.close()
                    time.sleep(wait)
                    page = self._context.new_page()
                    continue
                else:
                    logger.warning("Longform link: video %s still processing after %d attempts — giving up",
                                   short_yt_id, max_retries)
                    page.close()
                    return False

            # ── Editor UI detected — proceed ───────────────────
            try:
                page.wait_for_selector("[id='title-textarea']", timeout=10000, state="visible")
            except PlaywrightTimeout:
                logger.warning("Longform link: title field not found, continuing")
            human_delay(2.0, 4.0, "longform: editor settle")
            break  # Exit retry loop — page is ready

        # ── Page is ready ────────────────────────────────────────

        # Check if already linked (idempotent guard)
        if self._longform_already_linked(page):
            logger.info("Longform link: already linked for %s — skipping",
                        short_yt_id)
            page.close()
            return True

        # Scroll down to reveal the "Video relacionado" section
        self._scroll_page_to_bottom(page)
        human_delay(1.0, 2.0, "longform: post-scroll")

        # Try to find the "Mostrar más" button and expand sections
        found_mostrar = self._find_and_click_mostrar_mas(page)
        if found_mostrar:
            human_delay(1.5, 3.0, "longform: expanded sections")
            self._scroll_page_to_bottom(page)
            human_delay(0.5, 1.5, "longform: post-expand scroll")

        # ─ Strategy A: Find the "Related video" input field directly ──
        longform_url = f"https://www.youtube.com/watch?v={longform_yt_id}"
        success = self._paste_longform_url(page, longform_url)

        if success:
            # Save changes
            human_delay(1.0, 2.0, "longform: pre-save after link")
            saved = self._save_for_longform(page, short_yt_id)
            if saved:
                logger.info("✅ Longform video linked: %s → %s",
                            short_yt_id, longform_yt_id)
                page.close()
                return True

        # ─ Strategy B: Click "Añadir" button → search in dialog ──
        logger.info("Longform link: Strategy A failed, trying Strategy B")
        success = self._longform_via_add_button(page, longform_yt_id)
        if success:
            human_delay(1.0, 2.0, "longform: pre-save after link B")
            saved = self._save_for_longform(page, short_yt_id)
            if saved:
                logger.info("✅ Longform video linked (strategy B): %s → %s",
                            short_yt_id, longform_yt_id)
                page.close()
                return True

        logger.warning("Longform link: all strategies failed for %s", short_yt_id)
        page.close()
        return False

    def _page_shows_processing(self, page, video_id: str) -> bool:
        """Check if the YouTube Studio edit page shows a 'processing' indicator.

        When YouTube is still transcoding / processing a video, the edit page
        either redirects away from /edit or shows a spinner/message indicating
        the video isn't ready for editing. Returns True if the page is NOT yet
        ready for editing.
        """
        try:
            result = page.evaluate("""() => {
                // Check 1: did we get redirected away from /edit?
                if (!window.location.href.includes('/edit')) return true;

                // Check 2: is there a "processing" / "procesando" indicator?
                const pageText = (document.body?.textContent || '').toLowerCase();
                if (pageText.includes('procesando') || pageText.includes('processing video') ||
                    pageText.includes('still processing') || pageText.includes('transcoding')) {
                    return true;
                }

                // Check 3: is the title field (core editor UI) NOT yet visible?
                const titleField = document.querySelector('[id="title-textarea"]');
                if (!titleField || !titleField.offsetParent) {
                    // No editor UI visible — likely still loading / processing
                    const spinners = document.querySelectorAll(
                        'tp-yt-paper-spinner, [role="progressbar"], ytcp-video-thumbnail-spinner');
                    for (const s of spinners) {
                        if (s.offsetParent) return true;  // Spinner visible = still processing
                    }
                }

                return false;
            }""")
            if result:
                logger.debug("Longform link: page shows processing indicator for %s", video_id)
            return bool(result)
        except Exception:
            # If we can't evaluate, assume the page is not ready
            return True

    def _longform_already_linked(self, page) -> bool:
        """Check if a related video is already linked by looking for a
        linked video card / thumbnail in the 'Video relacionado' section."""
        try:
            result = page.evaluate("""() => {
                // Look for the "Video relacionado" section heading
                const headings = document.querySelectorAll(
                    'ytcp-form-label, ytcp-form-section-title, .form-section-title, ' +
                    '[id*="related"], [class*="related"]');
                for (const h of headings) {
                    const text = (h.textContent || '').toLowerCase();
                    if (text.includes('relacionado') || text.includes('related')) {
                        // Look for a linked video indicator nearby:
                        // thumbnail image or video title already filled
                        const parent = h.closest('[id*="related"], [class*="related"], ' +
                            'ytcp-form-group, .form-group');
                        const container = parent || h.parentElement?.parentElement;
                        if (!container) return false;

                        // Check for already-linked video: a thumbnail img or video title
                        const thumbs = container.querySelectorAll(
                            'img[src*="ytimg"], img[src*="ggpht"]');
                        const videoTitles = container.querySelectorAll(
                            'a[href*="watch?v="], ytcp-video-list-cell, ' +
                            '[id*="video-title"], [class*="video-title"]');

                        // If there's a clear button (X) or remove link, it's already linked
                        const removeBtns = container.querySelectorAll(
                            '[aria-label*="eliminar" i], [aria-label*="remove" i], ' +
                            '[aria-label*="quitar" i]');

                        if (removeBtns.length > 0) return true;
                        if (thumbs.length > 0 && videoTitles.length > 0) return true;
                    }
                }
                return false;
            }""")
            return bool(result)
        except Exception:
            return False

    def _paste_longform_url(self, page, longform_url: str) -> bool:
        """Strategy A: Find the 'Video relacionado' input field and paste the
        long-form video URL directly. YouTube Studio should auto-resolve it."""
        try:
            result = page.evaluate("""(url) => {
                // Find the "Video relacionado" section
                const allText = document.querySelectorAll('[id*="related"], ' +
                    '[class*="related"], ytcp-form-group');
                for (const el of allText) {
                    const text = (el.textContent || '').toLowerCase();
                    if (text.includes('relacionado') || text.includes('related')) {
                        // Find the input field within or near this section
                        const inputs = el.querySelectorAll(
                            'input[type="text"], input:not([type]), ' +
                            'tp-yt-paper-input input, ' +
                            'iron-autogrow-textarea textarea, ' +
                            'input[aria-label*="relacionado" i], ' +
                            'input[aria-label*="related" i]');
                        for (const input of inputs) {
                            if (input.offsetParent) {
                                // Clear & type the URL
                                input.focus();
                                input.value = '';
                                input.dispatchEvent(new Event('input', {bubbles: true}));
                                input.value = url;
                                input.dispatchEvent(new Event('input', {bubbles: true}));
                                input.dispatchEvent(new Event('change', {bubbles: true}));
                                return true;
                            }
                        }

                        // Fallback: find click target that opens the search
                        const addBtn = el.querySelector(
                            'button, ytcp-button, [role="button"], a.clickable');
                        if (addBtn && addBtn.offsetParent) {
                            // Try clicking the description/annotation next to
                            // "Video relacionado" to open the search input
                            const parent = el.parentElement?.parentElement;
                            if (!parent) return false;
                            const allInputs = parent.querySelectorAll(
                                'input[type="text"], input:not([type])');
                            for (const inp of allInputs) {
                                if (inp.offsetParent) {
                                    inp.focus();
                                    inp.value = url;
                                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                                    return true;
                                }
                            }
                        }
                    }
                }
                return false;
            }""", longform_url)

            if result:
                human_delay(2.0, 4.0, "longform: url pasted, waiting for resolve")
                # Wait for YouTube to auto-resolve the URL and show the video card
                time.sleep(3)
                # Try to press Enter or Tab to confirm selection
                try:
                    page.keyboard.press("Enter")
                    human_delay(1.0, 2.0, "longform: Enter pressed")
                except Exception:
                    pass
                return True

            return False
        except Exception as e:
            logger.debug("Longform link strategy A error: %s", e)
            return False

    def _longform_via_add_button(self, page, longform_yt_id: str) -> bool:
        """Strategy B: Click the 'Añadir' / 'Add' button next to 'Related video',
        then search for the long-form video in the dialog."""
        try:
            # Click "Añadir" / "Add" in the related video section
            clicked = page.evaluate("""() => {
                const headings = document.querySelectorAll(
                    'ytcp-form-label, ytcp-form-section-title, .form-section-title');
                for (const h of headings) {
                    const text = (h.textContent || '').toLowerCase();
                    if (text.includes('relacionado') || text.includes('related')) {
                        // Find parent container
                        const parent = h.closest('ytcp-form-group, .form-group, ' +
                            '[id*="related"], [class*="related"]');
                        const container = parent || h.parentElement?.parentElement;
                        if (!container) continue;

                        // Look for "Añadir" / "Add" button
                        const btns = container.querySelectorAll(
                            'button, ytcp-button, [role="button"]');
                        for (const btn of btns) {
                            const btnText = (btn.textContent || '').trim().toLowerCase();
                            if (btnText === 'añadir' || btnText === 'add' ||
                                btnText.includes('a\u00f1adir') || btnText.includes('agregar')) {
                                if (btn.offsetParent) {
                                    btn.click();
                                    return true;
                                }
                            }
                        }
                    }
                }
                return false;
            }""")

            if not clicked:
                logger.debug("Longform link: 'Añadir' button not found")
                return False

            human_delay(2.0, 4.0, "longform: dialog opened")
            longform_url = f"https://www.youtube.com/watch?v={longform_yt_id}"

            # In the dialog, find the search input and paste the URL
            url_pasted = page.evaluate("""(url) => {
                // Look for dialog with search input
                const dialogs = document.querySelectorAll(
                    '[role="dialog"], ytcp-dialog, ytcp-paper-dialog');
                for (const dlg of dialogs) {
                    if (!dlg.offsetParent) continue;
                    const inputs = dlg.querySelectorAll(
                        'input[type="text"], input:not([type]), ' +
                        'tp-yt-paper-input input');
                    for (const input of inputs) {
                        if (input.offsetParent) {
                            input.focus();
                            input.value = '';
                            input.dispatchEvent(new Event('input', {bubbles: true}));
                            input.value = url;
                            input.dispatchEvent(new Event('input', {bubbles: true}));
                            input.dispatchEvent(new Event('change', {bubbles: true}));
                            return true;
                        }
                    }
                }
                return false;
            }""", longform_url)

            if not url_pasted:
                logger.debug("Longform link: search input in dialog not found")
                return False

            human_delay(2.0, 4.0, "longform: search executed")
            time.sleep(2)

            # Press Enter to confirm the search
            try:
                page.keyboard.press("Enter")
                human_delay(2.0, 4.0, "longform: enter after search")
            except Exception:
                pass

            # Wait for results and select the first one
            selected = page.evaluate("""(vid) => {
                // Wait up to 5s for results to appear
                const dialogs = document.querySelectorAll('[role="dialog"]');
                for (const dlg of dialogs) {
                    if (!dlg.offsetParent) continue;
                    // Look for video list items
                    const items = dlg.querySelectorAll(
                        'ytcp-video-list-cell, [role="radio"], [role="option"], ' +
                        'tp-yt-paper-item, ytcp-video-row');
                    for (const item of items) {
                        if (!item.offsetParent) continue;
                        // Check if this item matches our video
                        const itemText = (item.textContent || '').toLowerCase();
                        const itemHtml = item.innerHTML || '';
                        if (itemText.includes(vid) || itemHtml.includes(vid)) {
                            item.click();
                            return true;
                        }
                    }
                    // If no match, select first visible item as fallback
                    for (const item of items) {
                        if (item.offsetParent &&
                            (item.textContent || '').trim().length > 3) {
                            item.click();
                            return true;
                        }
                    }
                }
                return false;
            }""", longform_yt_id)

            if not selected:
                logger.debug("Longform link: no result selected in dialog")
                # Try to dismiss dialog
                try: page.keyboard.press("Escape")
                except Exception: pass
                return False

            human_delay(1.0, 2.0, "longform: video selected")

            # Dismiss dialog if still open
            try:
                page.keyboard.press("Escape")
                human_delay(0.5, 1.0, "longform: dialog dismissed")
            except Exception:
                pass

            return True

        except Exception as e:
            logger.debug("Longform link strategy B error: %s", e)
            return False

    def _save_for_longform(self, page, video_id: str) -> bool:
        """Click save and confirm for long-form linking."""
        human_delay(1.0, 2.0, "longform: pre-guardar")
        guardar_el = None
        for _ in range(30):
            guardar_el = page.query_selector(SEL_GUARDAR_ENABLED)
            if guardar_el and guardar_el.is_enabled():
                break
            time.sleep(1)
        if not guardar_el:
            logger.error("Longform link: Guardar never enabled for %s", video_id)
            return False

        human_delay(0.8, 2.0, "longform: click guardar")
        guardar_el.click()
        human_delay(2.0, 4.0, "longform: save settling")

        try:
            page.wait_for_selector(SEL_SAVE_CONFIRM, timeout=5000, state="attached")
            logger.info("Longform link: Save confirmed for %s", video_id)
        except PlaywrightTimeout:
            logger.info("Longform link: No save toast for %s (clicked anyway)", video_id)

        return True


# ── Collaboration engine helpers (Fase cuota ago 2026) ──────────
# Descubrimiento de canales/videos vía web UI — 0 unidades de Data API.
# Reemplaza a search().list() (100 ud/call) que agotaba el presupuesto.

def _dismiss_yt_consent(page) -> bool:
    """Best-effort: aceptar el banner de consentimiento de YouTube si aparece."""
    try:
        if "consent.youtube.com" not in page.url:
            return True
        for sel in ("button[aria-label*='Aceptar']", "form button[type='submit']",
                    "button:has-text('Aceptar')", "button:has-text('Accept')"):
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                human_delay(1.0, 2.0, "consent accepted")
                return True
        return False
    except Exception:
        return False


def _parse_subs_text(renderer_text: str) -> int | None:
    """Parsear '12,3 mil suscriptores' / '1,2 M de suscriptores' → int aprox.

    Devuelve None si no se puede determinar.
    """
    import re
    m = re.search(
        r"([\d.,]+)\s*(mil|K|M)?\s*(?:de\s*)?suscriptores",
        renderer_text, re.IGNORECASE,
    )
    if not m:
        return None
    try:
        num = float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()
    if suffix in ("mil", "k"):
        num *= 1000
    elif suffix == "m":
        num *= 1_000_000
    return int(num)


# ── Browser-search methods (instalados en YouTubeBrowser vía monkey-patch
#    para no duplicar el manejo de locks/threads) ───────────────────

def _browser_search_channels(self, keyword: str, max_results: int = 5) -> list[dict]:
    """Buscar canales en YouTube vía web UI (0 cuota Data API).

    Returns [{"url": "/@handle", "name": "...", "subs": int|None}]
    """
    with self._lock:
        try:
            self._ensure_browser()
            page = self._context.new_page()
            return self._do_search_channels(page, keyword, max_results)
        except Exception as e:
            logger.error("search_channels failed for '%s': %s", keyword, e)
            return []


def _do_browser_search_channels(self, page, keyword: str, max_results: int) -> list[dict]:
    try:
        from urllib.parse import quote
        # sp=EgIQAg%3D%3D → filtro de resultados: solo canales
        url = ("https://www.youtube.com/results?search_query="
               f"{quote(keyword)}&sp=EgIQAg%3D%3D")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        human_delay(2.0, 4.0, "search load")
        _dismiss_yt_consent(page)
        channels: list[dict] = []
        seen: set[str] = set()
        for _ in range(8):  # scrolls
            renderers = page.query_selector_all("ytd-channel-renderer")
            for r in renderers:
                try:
                    anchor = r.query_selector(
                        "a#main-link, a.yt-simple-endpoint[href^='/@'], "
                        "a.yt-simple-endpoint[href^='/channel/']"
                    )
                    href = (anchor.get_attribute("href") or "").strip() if anchor else ""
                    text = r.inner_text() or ""
                    name = ""
                    try:
                        name_el = r.query_selector("yt-formatted-string#text")
                        name = name_el.inner_text().strip() if name_el else href
                    except Exception:
                        name = href
                except Exception:
                    continue
                if not href or href in seen:
                    continue
                if any(bad in href for bad in ("/results", "/hashtag", "/c/", "/playlist")):
                    continue
                seen.add(href)
                channels.append({"url": href, "name": name,
                                 "subs": _parse_subs_text(text)})
                if len(channels) >= max_results:
                    page.close()
                    return channels
            try:
                page.mouse.wheel(0, 3000)
                human_delay(0.8, 1.6, "scroll search")
            except Exception:
                break
        page.close()
        return channels[:max_results]
    except Exception as e:
        logger.error("Search scrape error: %s", e)
        try:
            page.close()
        except Exception:
            pass
        return []


def _browser_channel_videos(self, channel_url: str, limit: int = 3) -> list[dict]:
    """Videos recientes de un canal vía web UI (0 cuota Data API)."""
    with self._lock:
        try:
            self._ensure_browser()
            page = self._context.new_page()
            return self._do_channel_videos(page, channel_url, limit)
        except Exception as e:
            logger.error("get_channel_videos failed for %s: %s", channel_url, e)
            return []


def _do_browser_channel_videos(self, page, channel_url: str, limit: int) -> list[dict]:
    try:
        target = channel_url.rstrip("/")
        if not target.endswith("/videos"):
            target += "/videos"
        page.goto(target, wait_until="domcontentloaded", timeout=60000)
        human_delay(2.0, 4.0, "channel load")
        _dismiss_yt_consent(page)
        videos: list[dict] = []
        seen: set[str] = set()
        for _ in range(8):
            anchors = page.query_selector_all("a#video-title-link")
            for a in anchors:
                try:
                    href = (a.get_attribute("href") or "").strip()
                    title = (a.get_attribute("title") or "").strip()
                except Exception:
                    continue
                if href.startswith("/watch?v=") and href not in seen:
                    seen.add(href)
                    videos.append({"video_url": href, "title": title})
                    if len(videos) >= limit:
                        page.close()
                        return videos
            try:
                page.mouse.wheel(0, 3000)
                human_delay(0.8, 1.6, "scroll channel")
            except Exception:
                break
        page.close()
        return videos[:limit]
    except Exception as e:
        logger.error("Channel videos scrape error: %s", e)
        try:
            page.close()
        except Exception:
            pass
        return []


# Instalar métodos en la clase
YouTubeBrowser.search_channels = _browser_search_channels
YouTubeBrowser.get_channel_videos = _browser_channel_videos
YouTubeBrowser._do_search_channels = _do_browser_search_channels
YouTubeBrowser._do_channel_videos = _do_browser_channel_videos


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
    # Clean up ALL Playwright driver processes via the global registry.
    # This stops both the current thread's instance AND any instances
    # leaked by daemon threads that already exited.
    _cleanup_all_playwrights()


def cleanup_browser_thread():
    """Clean up the Playwright driver for the current thread only.
    
    Safe to call from daemon thread finally blocks — stops only the
    current thread's driver without affecting shared browser contexts
    used by other threads.
    """
    _cleanup_current_thread_playwright()


def get_account_for_channel(channel_slug: str) -> Optional[str]:
    """Return the Google account for a channel, from the DB channels table."""
    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        ch = db.get_channel_by_slug(channel_slug)
        if ch and ch.get("google_account"):
            return ch["google_account"]
    except Exception:
        pass
    return None


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
    
    # Get all channels with google_account set from DB
    try:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
        channels = db.get_channels(active_only=True)
        channel_accounts = [
            (ch["slug"], ch.get("google_account"))
            for ch in channels
            if ch.get("google_account")
        ]
    except Exception:
        channel_accounts = []
    
    for slug, account in channel_accounts:
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
