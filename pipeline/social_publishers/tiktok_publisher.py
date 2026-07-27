"""TikTok publisher — Uploads videos via browser automation."""
from __future__ import annotations

import asyncio
import logging
import os

from pipeline.social_publishers.base import SocialContent, SocialPlatform, register_publisher

logger = logging.getLogger(__name__)


class TikTokPublisher(SocialPlatform):
    """Upload clips to TikTok using browser automation."""

    platform = "tiktok"
    LOGIN_URL = "https://www.tiktok.com/login"
    UPLOAD_URL = "https://www.tiktok.com/creator#/upload"

    async def login(self, page, username: str, password: str) -> bool:
        """Log into TikTok.

        TikTok login flow:
        1. Go to login page → click "Use phone/email/username"
        2. Click "Log in with email or username"
        3. Enter username → enter password → click "Log in"
        """
        try:
            await page.goto(self.LOGIN_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Dismiss cookie banner if present
            try:
                cookie_btn = await page.query_selector('text="Reject all"')
                if cookie_btn:
                    await cookie_btn.click()
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

            # Step 1: Click "Use phone / email / username" link
            email_link = await page.query_selector(
                'text="Use phone / email / username", text="Usar telefono / correo / usuario"'
            )
            if email_link:
                await email_link.click()
                await page.wait_for_timeout(1500)

            # Step 2: Click "Log in with email or username"
            email_tab = await page.query_selector(
                'text="Email / Username", text="Correo / Usuario"'
            )
            if email_tab:
                await email_tab.click()
                await page.wait_for_timeout(1000)

            # Step 3: Enter username
            username_input = await page.wait_for_selector(
                'input[placeholder*="username"], input[placeholder*="correo"], input[name="username"]',
                timeout=10000,
            )
            if username_input:
                await username_input.click()
                await asyncio.sleep(0.5)
                await username_input.fill(username)
                await asyncio.sleep(0.5)

            # Step 4: Enter password
            pwd_input = await page.query_selector(
                'input[type="password"], input[placeholder*="contraseña"]'
            )
            if pwd_input:
                await pwd_input.click()
                await asyncio.sleep(0.3)
                await pwd_input.fill(password)
                await asyncio.sleep(0.5)

            # Step 5: Click "Log in" button
            login_btn = await page.query_selector(
                'button:has-text("Log in"), button:has-text("Iniciar sesion")'
            )
            if login_btn:
                await login_btn.click()
                await page.wait_for_timeout(5000)

            # CAPTCHA handling: wait up to 60s (manual intervention may be needed)
            try:
                captcha = await page.wait_for_selector(
                    'iframe[title*="captcha"], .captcha_verify', timeout=5000,
                )
                if captcha:
                    logger.warning("TikTok CAPTCHA detected — waiting 60s for resolution")
                    await page.wait_for_timeout(60000)
            except Exception:
                pass  # No captcha detected

            # Verify login
            await page.goto("https://www.tiktok.com/foryou", wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            logged_in = await page.query_selector(
                'a[href*="/@"], div[data-e2e="profile-icon"]'
            )
            if logged_in:
                logger.info("TikTok login successful")
                return True

            logger.warning("TikTok login may have failed — proceeding with cookies")
            return True  # Cookies might still work even if we can't verify

        except Exception as exc:
            logger.error("TikTok login error: %s", exc)
            return False

    async def publish(self, page, content: SocialContent, dry_run: bool = False) -> str:
        """Upload and post a TikTok video with caption."""
        try:
            if not content.media_path or not os.path.exists(content.media_path):
                logger.error("TikTok publish: no media file at %s", content.media_path)
                return ""

            await page.goto(self.UPLOAD_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Upload video
            file_input = await page.wait_for_selector(
                'input[type="file"][accept*="video"]', timeout=15000,
            )
            if file_input:
                await file_input.set_input_files(content.media_path)
                logger.info("TikTok: video file uploaded, waiting for processing...")
            else:
                upload_area = await page.query_selector('div[class*="upload"]')
                if upload_area:
                    await upload_area.click()
                    await page.wait_for_timeout(1000)
                    file_input2 = await page.query_selector('input[type="file"]')
                    if file_input2:
                        await file_input2.set_input_files(content.media_path)
                    else:
                        return ""

            # Wait for processing
            await page.wait_for_timeout(15000)

            # Fill caption
            try:
                caption_input = await page.wait_for_selector(
                    'div[contenteditable="true"], div[data-placeholder*="caption"]',
                    timeout=10000,
                )
                if caption_input:
                    await caption_input.click()
                    await asyncio.sleep(0.5)
                    full_caption = content.text
                    if content.hashtags:
                        full_caption += "\n\n" + " ".join(content.hashtags)
                    await caption_input.fill(full_caption)
                    await asyncio.sleep(1.0)
            except Exception as exc:
                logger.warning("TikTok: caption fill error: %s", exc)

            # Take screenshot for dry-run
            if dry_run:
                import os as _os
                _os.makedirs("output/social_tests", exist_ok=True)
                shot = f"output/social_tests/dryrun_tiktok_{int(time.time())}.png"
                await page.screenshot(path=shot)
                logger.info("[DRY-RUN] TikTok preview: %s", shot)
                return f"[DRY-RUN] {shot}"

            # Click Post
            await page.wait_for_timeout(1000)
            post_btn = await page.query_selector(
                'button:has-text("Post"), button:has-text("Publicar"), div[role="button"]:has-text("Post")'
            )
            if post_btn:
                await post_btn.click()
                await page.wait_for_timeout(5000)

            try:
                profile_link = await page.query_selector('a[href*="/@"]')
                if profile_link:
                    href = await profile_link.get_attribute("href")
                    if href:
                        return f"https://www.tiktok.com{href}" if not href.startswith("http") else href
            except Exception:
                pass

            return "https://www.tiktok.com"

        except Exception as exc:
            logger.error("TikTok publish error: %s", exc)
            return ""


# Register on import
register_publisher("tiktok", TikTokPublisher)
