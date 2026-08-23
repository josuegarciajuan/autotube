"""Social media accounts management API."""
import json
import logging
from fastapi import APIRouter, HTTPException
from api.deps import get_db
from api.schemas.models import (
    SocialAccountCreate,
    SocialAccountUpdate,
    SocialAccountResponse,
    SocialTimingUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _row_to_response(row: dict) -> SocialAccountResponse:
    """Convert DB row → SocialAccountResponse."""
    return SocialAccountResponse(
        id=row["id"],
        channel_id=row["channel_id"],
        platform=row["platform"],
        username=row["username"],
        enabled=bool(row.get("enabled", True)),
        has_cookies=bool(row.get("cookies_json")),
        last_login_at=str(row.get("last_login_at", "")),
        last_error=row.get("last_error"),
        created_at=str(row.get("created_at", "")),
        updated_at=str(row.get("updated_at", "")),
    )


# ── CRUD ──────────────────────────────────────────────────────

@router.get("/{channel_id}/social-accounts")
def list_social_accounts(channel_id: int):
    """List all social media accounts configured for a channel.
    Passwords are never returned — only usernames and status."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    try:
        from pipeline.social_encryption import get_encryption
        accounts = db.get_channel_social_accounts(channel_id)
        # Mask passwords
        for acct in accounts:
            acct["encrypted_password"] = "****"
        return [_row_to_response(a) for a in accounts]
    except Exception as exc:
        raise HTTPException(500, f"Error listing accounts: {exc}")


@router.put("/{channel_id}/social-accounts/{platform}")
def upsert_social_account(channel_id: int, platform: str, data: SocialAccountCreate):
    """Create or update social media credentials for a channel.

    The password is encrypted with Fernet before storage."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    valid_platforms = {"tiktok", "twitter", "instagram", "facebook", "reddit", "rumble",
                       "dailymotion", "bluesky", "mastodon"}
    platform_lower = platform.lower()
    if platform_lower not in valid_platforms:
        raise HTTPException(400, f"Invalid platform '{platform}'. Valid: {valid_platforms}")

    try:
        from pipeline.social_encryption import get_encryption
        enc = get_encryption()
        encrypted_pw = enc.encrypt(data.password)

        db.upsert_social_account(
            channel_id=channel_id,
            platform=platform_lower,
            username=data.username,
            encrypted_password=encrypted_pw,
            enabled=data.enabled,
        )

        accounts = db.get_channel_social_accounts(channel_id)
        for acct in accounts:
            if acct["platform"] == platform_lower:
                return _row_to_response(acct)

        raise HTTPException(500, "Account saved but not found in response")
    except Exception as exc:
        raise HTTPException(500, f"Error saving account: {exc}")


@router.delete("/{channel_id}/social-accounts/{platform}")
def delete_social_account(channel_id: int, platform: str):
    """Delete a social media account configuration."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    try:
        deleted = db.delete_social_account(channel_id, platform.lower())
        if not deleted:
            raise HTTPException(404, f"No account found for platform '{platform}'")
        return {"ok": True, "message": f"Account for {platform} deleted"}
    except Exception as exc:
        raise HTTPException(500, f"Error deleting account: {exc}")


@router.patch("/{channel_id}/social-accounts/{platform}")
def update_social_account(channel_id: int, platform: str, data: SocialAccountUpdate):
    """Update individual fields of a social account (username, password, enabled)."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    accounts = db.get_channel_social_accounts(channel_id)
    existing = next((a for a in accounts if a["platform"] == platform.lower()), None)
    if not existing:
        raise HTTPException(404, f"No account found for platform '{platform}'")

    try:
        from pipeline.social_encryption import get_encryption
        enc = get_encryption()

        if data.username is not None:
            existing["username"] = data.username
        if data.password is not None:
            existing["encrypted_password"] = enc.encrypt(data.password)
        if data.enabled is not None:
            existing["enabled"] = data.enabled

        db.upsert_social_account(
            channel_id=channel_id,
            platform=platform.lower(),
            username=existing["username"],
            encrypted_password=existing["encrypted_password"],
            enabled=existing["enabled"],
        )

        accounts = db.get_channel_social_accounts(channel_id)
        for acct in accounts:
            if acct["platform"] == platform.lower():
                return _row_to_response(acct)
    except Exception as exc:
        raise HTTPException(500, f"Error updating account: {exc}")


# ── Timing configuration ─────────────────────────────────────

@router.get("/{channel_id}/social-timing")
def get_social_timing(channel_id: int):
    """Get per-platform timing delays (minutes after go_public)."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    config = ch.get("config_json", {})
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            config = {}

    social_timing = config.get("SOCIAL_TIMING", {})
    return {
        "tiktok": social_timing.get("tiktok", 30),
        "twitter": social_timing.get("twitter", 60),
        "instagram": social_timing.get("instagram", 120),
        "facebook": social_timing.get("facebook", 180),
        "reddit": social_timing.get("reddit", 240),
    }


@router.put("/{channel_id}/social-timing")
def update_social_timing(channel_id: int, data: SocialTimingUpdate):
    """Update per-platform timing delays (minutes after go_public)."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    config = ch.get("config_json", {})
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            config = {}

    social_timing = config.get("SOCIAL_TIMING", {})
    updates = data.model_dump(exclude_none=True)
    for platform, delay in updates.items():
        if delay is not None:
            social_timing[platform] = max(0, delay)  # No negative delays

    config["SOCIAL_TIMING"] = social_timing
    db.update_channel(channel_id, config=config)
    return {"ok": True, "social_timing": social_timing}


# ── Test connection ─────────────────────────────────────────

@router.post("/{channel_id}/social-accounts/{platform}/test")
async def test_social_account(channel_id: int, platform: str):
    """Test login to a social media platform with current credentials.

    Opens a VISIBLE browser (headless=false), attempts login with stored
    credentials, takes a screenshot, and saves cookies on success.

    Returns {ok, message, screenshot_path, cookies_saved}.
    """
    import asyncio
    import base64
    import os
    from pathlib import Path

    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    platform_lower = platform.lower()
    acct = db.get_social_account(channel_id, platform_lower)
    if not acct:
        raise HTTPException(404, f"No credentials configured for {platform}")

    # Decrypt password
    from pipeline.social_encryption import get_encryption
    enc = get_encryption()
    password = enc.decrypt(acct["encrypted_password"])
    if not password:
        raise HTTPException(400, f"Failed to decrypt password for {platform}")

    # Screenshot dir
    screenshot_dir = Path(__file__).resolve().parent.parent.parent / "output" / "social_tests"
    os.makedirs(screenshot_dir, exist_ok=True)
    screenshot_path = str(screenshot_dir / f"login_{platform_lower}_{channel_id}_{int(__import__('time').time())}.png")

    try:
        from pipeline.social_browser import BrowserSessionManager

        # Use headless=False so user can see what happens (for debugging)
        async with BrowserSessionManager(headless=False) as bsm:
            page = await bsm.new_page()

            # Try login
            result = await bsm.login_and_save(
                channel_id=channel_id,
                platform=platform_lower,
                username=acct["username"],
                password=password,
            )

            # Take screenshot regardless of result
            try:
                await page.screenshot(path=screenshot_path, full_page=False)
            except Exception:
                pass  # Screenshot is best-effort

            if result["success"]:
                # Save cookies to DB
                if result.get("cookies_json"):
                    db.update_social_cookies(acct["id"], result["cookies_json"])
                    logger.info("Test login OK for %s on %s (cookies saved)", acct["username"], platform_lower)
                else:
                    logger.info("Test login OK for %s on %s (no cookies captured)", acct["username"], platform_lower)

                return {
                    "ok": True,
                    "message": f"Login exitoso en {platform_lower} como {acct['username']}",
                    "screenshot_path": screenshot_path,
                    "cookies_saved": bool(result.get("cookies_json")),
                }
            else:
                db.update_social_error(acct["id"], result.get("error", "Login failed"))
                return {
                    "ok": False,
                    "message": f"Login fallido en {platform_lower}: {result.get('error', 'Unknown error')}",
                    "screenshot_path": screenshot_path,
                    "cookies_saved": False,
                }

    except Exception as exc:
        db.update_social_error(acct["id"], str(exc)[:1000])
        raise HTTPException(500, f"Test failed: {exc}")
