"""Facebook publisher — Posts to pages/groups via browser automation."""
from __future__ import annotations

import asyncio
import logging
import os

from pipeline.social_publishers.base import SocialContent, SocialPlatform, register_publisher

logger = logging.getLogger(__name__)


class FacebookPublisher(SocialPlatform):
    """Post text to Facebook via browser automation."""

    platform = "facebook"
    LOGIN_URL = "https://www.facebook.com/login"

    async def login(self, page, username: str, password: str) -> bool:
        try:
            await page.goto(self.LOGIN_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Accept cookies
            try:
                cookie_btn = await page.query_selector('text="Reject all"')
                if not cookie_btn:
                    cookie_btn = await page.query_selector('text="Rechazar todas"')
                if cookie_btn:
                    await cookie_btn.click()
            except Exception:
                pass

            # Fill email
            email_input = await page.wait_for_selector(
                'input[name="email"], input[id="email"]', timeout=10000,
            )
            if email_input:
                await email_input.fill(username)

            # Fill password
            pwd_input = await page.query_selector('input[name="pass"], input[id="pass"]')
            if pwd_input:
                await pwd_input.fill(password)

            # Click login
            login_btn = await page.query_selector(
                'button[name="login"], button:has-text("Log In"), button:has-text("Iniciar")'
            )
            if login_btn:
                await login_btn.click()
                await page.wait_for_timeout(5000)

            logger.info("Facebook login OK")
            return True
        except Exception as exc:
            logger.error("Facebook login error: %s", exc)
            return False

    async def publish(self, page, content: SocialContent, dry_run: bool = False) -> str:
        try:
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            create_btn = await page.query_selector(
                'div[role="button"]:has-text("What"), '
                'div[aria-label*="Create"], '
                'span:has-text("What")'
            )
            if create_btn:
                await create_btn.click()
                await page.wait_for_timeout(1000)

            text_field = await page.wait_for_selector(
                'div[contenteditable="true"], div[role="textbox"]', timeout=5000,
            )
            if text_field:
                await text_field.click()
                await text_field.fill(content.text)
                await asyncio.sleep(1.0)

            if dry_run:
                os.makedirs("output/social_tests", exist_ok=True)
                shot = f"output/social_tests/dryrun_facebook_{int(__import__('time').time())}.png"
                await page.screenshot(path=shot)
                logger.info("[DRY-RUN] Facebook preview: %s", shot)
                return f"[DRY-RUN] {shot}"

            post_btn = await page.query_selector(
                'div[role="button"]:has-text("Post"), div[role="button"]:has-text("Publicar")'
            )
            if post_btn:
                await post_btn.click()
                await page.wait_for_timeout(5000)
                return "https://www.facebook.com"

            return ""
        except Exception as exc:
            logger.error("Facebook publish error: %s", exc)
            return ""


register_publisher("facebook", FacebookPublisher)


class RedditPublisher(SocialPlatform):
    """Post text to Reddit via browser automation."""

    platform = "reddit"
    LOGIN_URL = "https://www.reddit.com/login"

    async def login(self, page, username: str, password: str) -> bool:
        try:
            await page.goto(self.LOGIN_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Fill username
            username_input = await page.wait_for_selector(
                'input[name="username"], input[id="loginUsername"]', timeout=10000,
            )
            if username_input:
                await username_input.fill(username)

            # Fill password
            pwd_input = await page.query_selector(
                'input[name="password"], input[id="loginPassword"]'
            )
            if pwd_input:
                await pwd_input.fill(password)

            # Click login
            login_btn = await page.query_selector(
                'button[type="submit"], button:has-text("Log In"), button:has-text("Iniciar")'
            )
            if login_btn:
                await login_btn.click()
                await page.wait_for_timeout(5000)

            logger.info("Reddit login OK")
            return True
        except Exception as exc:
            logger.error("Reddit login error: %s", exc)
            return False

    async def publish(self, page, content: SocialContent, dry_run: bool = False) -> str:
        try:
            await page.goto("https://www.reddit.com/submit", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            title_input = await page.wait_for_selector(
                'textarea[placeholder*="title"], textarea[id*="title"]', timeout=10000,
            )
            if title_input:
                title = content.text.split("\n")[0][:300] if content.text else "Check this out"
                await title_input.fill(title)

            editor_modes = await page.query_selector(
                'button:has-text("Markdown"), button:has-text("Switch to Markdown")'
            )
            if editor_modes:
                await editor_modes.click()
                await page.wait_for_timeout(500)

            body_field = await page.query_selector(
                'textarea[placeholder*="text"], div[contenteditable="true"]'
            )
            if body_field:
                await body_field.fill(content.text)
                await asyncio.sleep(1.0)

            if dry_run:
                os.makedirs("output/social_tests", exist_ok=True)
                shot = f"output/social_tests/dryrun_reddit_{int(__import__('time').time())}.png"
                await page.screenshot(path=shot)
                logger.info("[DRY-RUN] Reddit preview: %s", shot)
                return f"[DRY-RUN] {shot}"

            submit_btn = await page.query_selector(
                'button:has-text("Post"), button:has-text("Submit"), button:has-text("Publicar")'
            )
            if submit_btn:
                await submit_btn.click()
                await page.wait_for_timeout(5000)
                try:
                    post_link = await page.query_selector('a[data-click-id="body"]')
                    if post_link:
                        href = await post_link.get_attribute("href")
                        if href:
                            return f"https://www.reddit.com{href}" if not href.startswith("http") else href
                except Exception:
                    pass
                return "https://www.reddit.com"
            return ""
        except Exception as exc:
            logger.error("Reddit publish error: %s", exc)
            return ""


register_publisher("reddit", RedditPublisher)
