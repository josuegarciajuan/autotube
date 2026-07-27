#!/usr/bin/env python3
"""Interactive social media login tool.

Opens a VISIBLE browser window for each social platform so a human can:
- Complete the login flow (including CAPTCHA, 2FA, email verification)
- The browser session is then saved and reused by the automation.

Usage:
    # Login to a single platform for a channel
    python3 scripts/social_login.py --canal canal2 --platform twitter

    # Login to all configured platforms
    python3 scripts/social_login.py --canal canal2 --all

    # Login with explicit credentials (skip DB)
    python3 scripts/social_login.py --canal canal2 --platform twitter \
        --username "@mychannel" --password "mypassword"

    # Test mode: login + validate only (no save)
    python3 scripts/social_login.py --canal canal2 --platform twitter --validate-only

Credentials are encrypted and stored in the DB. Browser cookies are
persisted for future automated logins.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("social_login")


async def login_platform(
    channel_id: int,
    channel_slug: str,
    platform: str,
    username: str,
    password: str,
    save_to_db: bool = True,
) -> dict:
    """Login to a social media platform interactively.

    Opens a visible browser so the user can handle CAPTCHA/2FA.
    """
    from pipeline.social_browser import BrowserSessionManager
    from pipeline.social_publishers.base import get_publisher
    from pipeline.social_encryption import get_encryption

    logger.info("🚀 Opening browser for %s login (%s)...", platform, username)
    logger.info("   Complete the login flow manually if needed (CAPTCHA, 2FA, etc.)")
    logger.info("   The browser will stay open for 120 seconds after login.")

    result = {"success": False, "cookies_json": "", "error": ""}
    publisher = get_publisher(platform)

    async with BrowserSessionManager(headless=False) as bsm:
        page = await bsm.new_page()

        try:
            login_ok = await publisher.login(page, username, password)
            if login_ok:
                logger.info("✅ Login to %s successful!", platform)
                result["success"] = True
            else:
                logger.warning("⚠️  Login to %s may have failed — check the browser", platform)
                logger.info("   You have 60 seconds to complete login manually...")
                await asyncio.sleep(60)
                # Check again
                login_ok = await publisher.validate_login(page, username)
                if login_ok:
                    result["success"] = True
        except Exception as exc:
            result["error"] = str(exc)
            logger.error("❌ Login error: %s", exc)
            logger.info("   You have 60 seconds to complete login manually...")
            await asyncio.sleep(60)

        if result["success"]:
            cookies = await bsm.save_cookies(page)
            result["cookies_json"] = cookies

            if save_to_db and cookies:
                from database.db_extended import ExtendedDatabase
                db = ExtendedDatabase()
                enc = get_encryption()
                encrypted_pw = enc.encrypt(password)

                # Save/update credentials
                db.upsert_social_account(
                    channel_id=channel_id,
                    platform=platform,
                    username=username,
                    encrypted_password=encrypted_pw,
                    enabled=True,
                )

                # Save cookies
                acct = db.get_social_account(channel_id, platform)
                if acct:
                    db.update_social_cookies(acct["id"], cookies)
                    logger.info("💾 Credentials + cookies saved to DB for %s/%s", channel_slug, platform)
                else:
                    logger.warning("⚠️  Could not find account in DB after upsert")
            elif save_to_db and not cookies:
                logger.warning("⚠️  No cookies captured — login may need manual completion")

            logger.info("   Browser will close in 5 seconds...")
            await asyncio.sleep(5)
        else:
            logger.warning("   Press Enter to close browser...")
            input()

    return result


async def login_all_platforms(channel_id: int, channel_slug: str) -> list[dict]:
    """Login to all platforms that have credentials configured."""
    from database.db_extended import ExtendedDatabase
    from pipeline.social_encryption import get_encryption

    db = ExtendedDatabase()
    accounts = db.get_channel_social_accounts(channel_id)

    if not accounts:
        logger.error("No social accounts configured for channel %s", channel_slug)
        logger.info("Configure them via the web panel or API first.")
        return []

    enc = get_encryption()
    results = []

    for acct in accounts:
        platform = acct["platform"]
        username = acct["username"]
        password = enc.decrypt(acct["encrypted_password"])

        if not password:
            logger.warning("Skipping %s — cannot decrypt password", platform)
            continue

        result = await login_platform(
            channel_id, channel_slug, platform, username, password, save_to_db=False,
        )
        results.append(result)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Interactive social media login",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scripts/social_login.py --canal canal2 --platform twitter
  python3 scripts/social_login.py --canal canal2 --all
  python3 scripts/social_login.py --canal canal2 --platform twitter \\
      --username "@mychannel" --password "mypassword"
        """,
    )
    parser.add_argument("--canal", required=True, help="Channel slug (e.g., canal2)")
    parser.add_argument("--platform", help="Platform: twitter, tiktok, instagram, facebook, reddit")
    parser.add_argument("--all", action="store_true", help="Login to all configured platforms")
    parser.add_argument("--username", help="Override username from DB")
    parser.add_argument("--password", help="Override password from DB")
    parser.add_argument("--validate-only", action="store_true", help="Validate session without saving")
    parser.add_argument("--verbose", action="store_true", help="Show debug logs")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Get channel
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    ch = db.get_channel_by_slug(args.canal)
    if not ch:
        logger.error("Channel not found: %s", args.canal)
        sys.exit(1)

    channel_id = ch["id"]
    channel_slug = ch["slug"]
    logger.info("Channel: %s (id=%d)", channel_slug, channel_id)

    if args.all:
        results = asyncio.run(login_all_platforms(channel_id, channel_slug))
        ok = sum(1 for r in results if r["success"])
        logger.info("✅ %d/%d platforms logged in successfully", ok, len(results))
        return

    if not args.platform:
        parser.error("--platform or --all required")

    # Resolve credentials
    username = args.username
    password = args.password

    if not username or not password:
        from pipeline.social_encryption import get_encryption
        acct = db.get_social_account(channel_id, args.platform.lower())
        if not acct:
            logger.error("No credentials found for %s on %s. Use --username and --password.", args.canal, args.platform)
            sys.exit(1)
        username = username or acct["username"]
        password = password or get_encryption().decrypt(acct["encrypted_password"])

    if not password:
        logger.error("Cannot decrypt password for %s. Re-save credentials via the panel.", args.platform)
        sys.exit(1)

    result = asyncio.run(login_platform(
        channel_id, channel_slug, args.platform.lower(),
        username, password, save_to_db=not args.validate_only,
    ))

    if result["success"]:
        logger.info("✅ Login completo!")
        sys.exit(0)
    else:
        logger.error("❌ Login fallido: %s", result["error"])
        sys.exit(1)


if __name__ == "__main__":
    main()
