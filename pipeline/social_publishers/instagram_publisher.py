"""Instagram publisher — Posts Reels via browser automation."""
from __future__ import annotations

import asyncio
import logging
import os

from pipeline.social_publishers.base import SocialContent, SocialPlatform, register_publisher

logger = logging.getLogger(__name__)


class InstagramPublisher(SocialPlatform):
    """Upload Reels to Instagram using browser automation."""

    platform = "instagram"
    LOGIN_URL = "https://www.instagram.com/accounts/login/"
    UPLOAD_URL = "https://www.instagram.com/reels/create/"

    async def login(self, page, username: str, password: str) -> bool:
        try:
            await page.goto(self.LOGIN_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Dismiss cookie
            try:
                cookie_btn = await page.query_selector('text="Reject all"')
                if not cookie_btn:
                    cookie_btn = await page.query_selector('text="Rechazar todas"')
                if cookie_btn:
                    await cookie_btn.click()
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

            # Fill username
            username_input = await page.wait_for_selector(
                'input[name="username"]', timeout=10000,
            )
            if username_input:
                await username_input.fill(username)
                await asyncio.sleep(0.5)

            # Fill password
            pwd_input = await page.query_selector('input[name="password"]')
            if pwd_input:
                await pwd_input.fill(password)
                await asyncio.sleep(0.5)

            # Click login
            login_btn = await page.query_selector(
                'button[type="submit"], div[role="button"]:has-text("Log In")'
            )
            if login_btn:
                await login_btn.click()
                await page.wait_for_timeout(5000)

            # Dismiss "Save info" dialog
            try:
                not_now = await page.wait_for_selector(
                    'text="Not now", text="Ahora no"', timeout=5000,
                )
                if not_now:
                    await not_now.click()
                    await page.wait_for_timeout(2000)
            except Exception:
                pass

            # Dismiss notifications prompt
            try:
                not_now2 = await page.wait_for_selector(
                    'text="Not Now", text="Ahora no"', timeout=5000,
                )
                if not_now2:
                    await not_now2.click()
            except Exception:
                pass

            logger.info("Instagram login OK")
            return True

        except Exception as exc:
            logger.error("Instagram login error: %s", exc)
            return False

    async def publish(self, page, content: SocialContent) -> str:
        try:
            await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Click "Create" button (+) on navbar
            create_btn = await page.query_selector(
                'svg[aria-label="New post"], svg[aria-label="Nueva publicacion"], '
                'a[href="/reels/create/"]'
            )
            if not create_btn:
                create_btn = await page.query_selector(
                    'div[role="menuitem"]:has-text("Create"), div[role="menuitem"]:has-text("Crear")'
                )

            if create_btn:
                await create_btn.click()
                await page.wait_for_timeout(2000)

            # Upload video
            if content.media_path and os.path.exists(content.media_path):
                file_input = await page.wait_for_selector(
                    'input[type="file"]', timeout=10000,
                )
                if file_input:
                    await file_input.set_input_files(content.media_path)
                    await page.wait_for_timeout(10000)  # Wait for upload + processing
                    logger.info("Instagram: video uploaded")
                else:
                    logger.error("Instagram: file input not found")
                    return ""

            # Click "Next" through edit screens (usually 2-3 screens)
            for _ in range(3):
                await asyncio.sleep(2.0)
                next_btn = await page.query_selector(
                    'div[role="button"]:has-text("Next"), div[role="button"]:has-text("Siguiente")'
                )
                if next_btn:
                    await next_btn.click()
                else:
                    break

            # Fill caption
            try:
                caption_input = await page.wait_for_selector(
                    'textarea[aria-label*="caption"], div[contenteditable="true"]',
                    timeout=5000,
                )
                if caption_input:
                    full_caption = content.text
                    if content.hashtags:
                        full_caption += "\n.\n.\n.\n" + " ".join(content.hashtags)
                    await caption_input.fill(full_caption)
                    await asyncio.sleep(1.0)
            except Exception:
                pass

            # Click "Share" / "Compartir"
            share_btn = await page.query_selector(
                'div[role="button"]:has-text("Share"), div[role="button"]:has-text("Compartir")'
            )
            if share_btn:
                await share_btn.click()
                await page.wait_for_timeout(8000)
                logger.info("Instagram: posted")
                return "https://www.instagram.com"

            return ""

        except Exception as exc:
            logger.error("Instagram publish error: %s", exc)
            return ""


register_publisher("instagram", InstagramPublisher)
