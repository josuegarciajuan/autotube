"""Playwright-based browser automation for social media publishing.

Manages browser sessions, login persistence (cookies), and anti-detection
behaviors for each social media platform.

Usage:
    from pipeline.social_browser import BrowserSessionManager

    async with BrowserSessionManager() as bsm:
        page = await bsm.get_session(channel_id, "twitter")
        # ... interact with page ...
        await bsm.save_session(channel_id, "twitter")
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Anti-detection settings ───────────────────────────────

_CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--disable-setuid-sandbox",
    "--window-size=1280,900",
]

_LAUNCH_OPTIONS = {
    "args": _CHROMIUM_ARGS,
    "headless": True,
}

# ── BrowserSessionManager ──────────────────────────────────


class BrowserSessionManager:
    """Manages Playwright browser sessions for social media platforms.

    Handles browser lifecycle, login persistence via cookies, and
    per-channel/platform session isolation.
    """

    def __init__(self, user_data_dir: str = None):
        self._browser = None
        self._context = None
        self._playwright = None
        self._user_data_dir = user_data_dir or str(
            Path(__file__).resolve().parent.parent / "browser_data"
        )
        os.makedirs(self._user_data_dir, exist_ok=True)

    # ── lifecycle ──────────────────────────────────────────

    async def start(self):
        """Launch browser and create a context."""
        if self._browser is not None:
            return

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install playwright && "
                "playwright install chromium"
            )

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(**_LAUNCH_OPTIONS)
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        # Anti-detection script
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['es-ES', 'es', 'en-US', 'en']});
        """)
        logger.debug("Browser session started")

    async def stop(self):
        """Close browser and cleanup."""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._context = None
        self._playwright = None
        logger.debug("Browser session stopped")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    # ── session management ─────────────────────────────────

    async def new_page(self):
        """Create a new page in the current context."""
        if self._context is None:
            await self.start()
        page = await self._context.new_page()
        # Randomize viewport slightly
        w = 1280 + random.randint(-20, 20)
        h = 900 + random.randint(-10, 10)
        await page.set_viewport_size({"width": w, "height": h})
        return page

    async def load_cookies(self, page, cookies_json: str):
        """Load cookies from stored JSON into a page."""
        if not cookies_json:
            return
        try:
            cookies = json.loads(cookies_json)
            await page.context.add_cookies(cookies)
            logger.debug("Loaded %d cookies", len(cookies))
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("Failed to load cookies: %s", exc)

    async def save_cookies(self, page) -> str:
        """Save current page cookies as JSON string."""
        try:
            cookies = await page.context.cookies()
            return json.dumps(cookies)
        except Exception as exc:
            logger.error("Failed to save cookies: %s", exc)
            return ""

    # ── human-like interactions ────────────────────────────

    @staticmethod
    async def human_type(page, selector: str, text: str, delay_ms: int = None):
        """Type text with human-like random delays between keystrokes."""
        await page.click(selector)
        for char in text:
            await page.keyboard.type(char)
            d = delay_ms or random.randint(30, 120)
            await asyncio.sleep(d / 1000.0)

    @staticmethod
    async def human_scroll(page, count: int = 3):
        """Perform human-like scrolling."""
        for _ in range(count):
            delta = random.randint(100, 400)
            await page.evaluate(f"window.scrollBy(0, {delta})")
            await asyncio.sleep(random.uniform(0.3, 1.0))

    @staticmethod
    async def random_delay(min_ms: int = 200, max_ms: int = 1500):
        """Random delay to simulate human reading/thinking time."""
        await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000.0)

    # ── login flow ─────────────────────────────────────────

    async def login_and_save(
        self, channel_id: int, platform: str, username: str, password: str,
    ) -> dict:
        """Log in to a platform, save cookies to DB, return result dict.

        Returns:
            {"success": bool, "cookies_json": str, "error": str}
        """
        from pipeline.social_publishers.base import get_publisher

        result = {"success": False, "cookies_json": "", "error": ""}
        page = None

        try:
            page = await self.new_page()
            publisher = get_publisher(platform)

            # Try loading existing cookies first
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
            acct = db.get_social_account(channel_id, platform)
            if acct and acct.get("cookies_json"):
                await self.load_cookies(page, acct["cookies_json"])

            # Attempt login
            login_success = await publisher.login(page, username, password)
            if login_success:
                cookies = await self.save_cookies(page)
                result["success"] = True
                result["cookies_json"] = cookies
                logger.info("Login OK for %s on %s", username, platform)
            else:
                result["error"] = "Login failed — check credentials or platform UI changed"
                logger.warning("Login failed for %s on %s", username, platform)

        except Exception as exc:
            result["error"] = str(exc)
            logger.error("Login error for %s on %s: %s", username, platform, exc)
        finally:
            if page:
                await page.close()

        return result
