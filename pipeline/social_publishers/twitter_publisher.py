"""Twitter/X publisher — Posts threads via browser automation."""
from __future__ import annotations

import asyncio
import logging
import os
import time

from pipeline.social_publishers.base import SocialContent, SocialPlatform, register_publisher

logger = logging.getLogger(__name__)


class TwitterPublisher(SocialPlatform):
    """Publish threads to Twitter/X using browser automation."""

    platform = "twitter"
    LOGIN_URL = "https://x.com/login"
    COMPOSE_URL = "https://x.com/compose/post"

    async def login(self, page, username: str, password: str) -> bool:
        """Log into Twitter/X."""
        try:
            await page.goto(self.LOGIN_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Fill username
            username_input = await page.wait_for_selector(
                'input[autocomplete="username"], input[name="text"]', timeout=15000,
            )
            if username_input:
                await username_input.click()
                await asyncio.sleep(0.5)
                await username_input.fill(username)
                await asyncio.sleep(1.0)

                # Click "Next" button
                next_btn = await page.query_selector(
                    'div[role="button"]:has-text("Next"), div[role="button"]:has-text("Siguiente")'
                )
                if next_btn:
                    await next_btn.click()
                    await page.wait_for_timeout(2000)

                # Sometimes Twitter asks for email/phone verification — skip with username again
                unusual_activity = await page.query_selector('text="Enter your phone"')
                if unusual_activity:
                    # Type the username again
                    verify_input = await page.wait_for_selector(
                        'input[type="text"], input[name="text"]', timeout=5000,
                    )
                    if verify_input:
                        await verify_input.fill(username)
                        next_btn2 = await page.query_selector(
                            'div[role="button"]:has-text("Next"), div[role="button"]:has-text("Siguiente")'
                        )
                        if next_btn2:
                            await next_btn2.click()
                            await page.wait_for_timeout(2000)

                # Fill password
                await page.wait_for_timeout(1000)
                pwd_input = await page.wait_for_selector(
                    'input[type="password"], input[name="password"]', timeout=10000,
                )
                if pwd_input:
                    await pwd_input.click()
                    await asyncio.sleep(0.3)
                    await pwd_input.fill(password)
                    await asyncio.sleep(0.7)

                    # Click "Log in" button
                    login_btn = await page.query_selector(
                        'div[role="button"]:has-text("Log in"), div[role="button"]:has-text("Iniciar")'
                    )
                    if login_btn:
                        await login_btn.click()
                        await page.wait_for_timeout(5000)

            # Verify login
            await page.goto("https://x.com/home", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Check for logged-in indicator
            logged_in = await page.query_selector('a[data-testid="AppTabBar_Profile_Link"]')
            if logged_in:
                logger.info("Twitter login successful")
                return True

            logger.warning("Twitter login failed — could not verify logged-in state")
            return False

        except Exception as exc:
            logger.error("Twitter login error: %s", exc)
            return False

    async def publish(self, page, content: SocialContent, dry_run: bool = False) -> str:
        """Post a tweet thread to Twitter/X."""
        try:
            parts = content.thread_parts or [content.text]
            if not parts:
                logger.error("No content to tweet")
                return ""

            # Navigate to compose
            await page.goto(self.COMPOSE_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)

            post_url = ""
            for i, tweet_text in enumerate(parts):
                if i > 0:
                    await page.goto(self.COMPOSE_URL, wait_until="domcontentloaded")
                    await page.wait_for_timeout(1000)

                editor = await page.wait_for_selector(
                    'div[data-contents="true"], div[role="textbox"]', timeout=10000,
                )
                if not editor:
                    logger.error("Twitter compose box not found")
                    break

                await editor.click()
                await asyncio.sleep(0.5)
                await editor.fill("")
                await editor.type(tweet_text, delay=30)
                await asyncio.sleep(1.0)

                # Add media if first tweet
                if i == 0 and content.media_path:
                    try:
                        file_input = await page.query_selector('input[type="file"]')
                        if file_input:
                            await file_input.set_input_files(content.media_path)
                            await page.wait_for_timeout(3000)
                    except Exception as exc:
                        logger.warning("Media upload failed: %s", exc)

                if dry_run:
                    logger.info("[DRY-RUN] Would tweet: %s...", tweet_text[:60])
                    if i == 0:
                        import os
                        screenshot_dir = "output/social_tests"
                        os.makedirs(screenshot_dir, exist_ok=True)
                        shot = f"{screenshot_dir}/dryrun_twitter_{int(time.time())}.png"
                        await page.screenshot(path=shot)
                        return f"[DRY-RUN] {shot}"
                    continue

                # Click Post
                post_btn = await page.query_selector(
                    'button[data-testid="tweetButton"], div[role="button"]:has-text("Post")'
                )
                if post_btn:
                    await post_btn.click()
                    await page.wait_for_timeout(3000)

                    if i == 0:
                        try:
                            tweet_link = await page.query_selector(
                                'a[href*="/status/"] time, article a[href*="/status/"]'
                            )
                            if tweet_link:
                                href = await tweet_link.get_attribute("href")
                                if href:
                                    post_url = f"https://x.com{href}" if not href.startswith("http") else href
                        except Exception:
                            pass

                await asyncio.sleep(1.0)

            if dry_run:
                return f"[DRY-RUN] {len(parts)} tweets previewed"

            if post_url:
                logger.info("Twitter thread posted: %s", post_url)
            return post_url

        except Exception as exc:
            logger.error("Twitter publish error: %s", exc)
            return ""

    async def validate_login(self, page, username: str) -> bool:
        """Check if Twitter session is still valid."""
        try:
            await page.goto("https://x.com/home", wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            return await page.query_selector('a[data-testid="AppTabBar_Profile_Link"]') is not None
        except Exception:
            return False


# Register on import
register_publisher("twitter", TwitterPublisher)
