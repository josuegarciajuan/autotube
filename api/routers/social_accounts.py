"""Social media accounts management API."""
import json
import logging
from fastapi import APIRouter, HTTPException
from api.deps import get_db
from api.schemas.models import (
    SocialAccountCreate,
    SocialAccountUpdate,
    SocialAccountResponse,
    SocialRevealRequest,
    SocialTimingUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _set_existing(existing: dict, col: str, value: str) -> None:
    """Store an already-encrypted value in a social account row dict."""
    existing[col] = value


def _row_to_response(row: dict) -> SocialAccountResponse:
    """Convert DB row → SocialAccountResponse.

    Secrets are NEVER returned: only presence flags (has_*). The encrypted
    password itself never leaves the server through this endpoint.
    """
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
        account_email=row.get("account_email") or None,
        notes=row.get("notes") or None,
        has_email_password=bool(row.get("account_email_password")),
        has_account_password=bool(row.get("account_password")),
        has_api_key=bool(row.get("encrypted_password")),
    )


# ── CRUD ──────────────────────────────────────────────────────

@router.get("/{channel_id}/social-accounts")
def list_social_accounts(channel_id: int):
    """List all social media accounts configured for a channel.
    Passwords are never returned — only usernames, identity fields
    (email/notes) and presence flags (has_api_key, has_*_password)."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    try:
        accounts = db.get_channel_social_accounts(channel_id)
        return [_row_to_response(a) for a in accounts]
    except Exception as exc:
        raise HTTPException(500, f"Error listing accounts: {exc}")


@router.put("/{channel_id}/social-accounts/{platform}")
def upsert_social_account(channel_id: int, platform: str, data: SocialAccountCreate):
    """Create or update social media credentials for a channel.

    The API token (password) and the identity secrets (account_email_password,
    account_password) are encrypted with Fernet before storage."""
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
        encrypted_pw = enc.encrypt(data.password) if data.password else ""

        db.upsert_social_account(
            channel_id, platform_lower, data.username, encrypted_pw, data.enabled,
            data.account_email or None,
            enc.encrypt(data.account_email_password) if data.account_email_password else None,
            enc.encrypt(data.account_password) if data.account_password else None,
            data.notes or None,
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
            _set_existing(existing, "encrypted_password", enc.encrypt(data.password))
        if data.enabled is not None:
            existing["enabled"] = data.enabled
        # Identity fields (v45): None preserva el valor existente.
        if data.account_email is not None:
            existing["account_email"] = data.account_email or None
        if data.account_email_password is not None:
            _set_existing(existing, "account_email_password",
                          enc.encrypt(data.account_email_password))
        if data.account_password is not None:
            _set_existing(existing, "account_password", enc.encrypt(data.account_password))
        if data.notes is not None:
            existing["notes"] = data.notes or None

        db.upsert_social_account(
            channel_id, platform.lower(), existing["username"],
            existing["encrypted_password"], existing["enabled"],
            existing.get("account_email"), existing.get("account_email_password"),
            existing.get("account_password"), existing.get("notes"),
        )

        accounts = db.get_channel_social_accounts(channel_id)
        for acct in accounts:
            if acct["platform"] == platform.lower():
                return _row_to_response(acct)
    except Exception as exc:
        raise HTTPException(500, f"Error updating account: {exc}")


# ── Reveal credential (under demand) ─────────────────────────

@router.post("/{channel_id}/social-accounts/{platform}/reveal")
def reveal_social_credential(channel_id: int, platform: str, data: SocialRevealRequest):
    """Reveal a single stored credential value (decrypted) on demand.

    Allowed fields: 'api_key' (encrypted_password), 'email_password'
    (account_email_password), 'account_password' (account_password).
    Used by the UI to show the credential "a mano" with an explicit click.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    platform_lower = platform.lower()
    acct = db.get_social_account(channel_id, platform_lower)
    if not acct:
        raise HTTPException(404, f"No credentials configured for {platform}")

    field = data.field
    if field == "api_key":
        column = "encrypted_password"
    elif field == "email_password":
        column = "account_email_password"
    elif field == "account_password":
        column = "account_password"
    else:
        raise HTTPException(400, f"Invalid field '{field}'. Valid: api_key, email_password, account_password")

    encrypted = acct.get(column)
    if not encrypted:
        raise HTTPException(404, f"No {field} stored for {platform}")

    try:
        from pipeline.social_encryption import get_encryption
        enc = get_encryption()
        value = enc.decrypt(encrypted)
    except Exception as exc:
        raise HTTPException(500, f"Failed to decrypt {field}: {exc}")

    return {"ok": True, "field": field, "value": value}


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

    API-based platforms (rumble, dailymotion, facebook, bluesky, mastodon)
    are validated with a direct API call — no browser needed.
    Playwright platforms (tiktok, twitter, instagram, reddit) open a VISIBLE
    browser, attempt login, take a screenshot, and save cookies on success.

    Returns {ok, message, screenshot_path?, cookies_saved?}.
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
    pw = enc.decrypt(acct["encrypted_password"])
    if not pw:
        raise HTTPException(400, f"Failed to decrypt password for {platform}")

    # ── API-based platforms: validate directly, no browser ──
    API_PLATFORMS = {"rumble", "dailymotion", "facebook", "bluesky", "mastodon"}
    if platform_lower in API_PLATFORMS:
        try:
            if platform_lower in ("bluesky", "mastodon"):
                from pipeline.social_api_publishers import validate_bluesky, validate_mastodon
                creds = {"username": acct["username"], "password": pw}
                result = (validate_bluesky if platform_lower == "bluesky"
                          else validate_mastodon)(creds)
            else:
                from api.services.publishers.base import get_publisher
                pub = get_publisher(platform_lower)
                result = await pub.validate(channel_id)
        except Exception as exc:
            db.update_social_error(acct["id"], str(exc)[:1000])
            raise HTTPException(500, f"Test failed: {exc}")

        ok = bool(result.get("ok"))
        if ok:
            db.upsert_social_account(
                channel_id=channel_id, platform=platform_lower,
                username=acct["username"], encrypted_password=acct["encrypted_password"],
                enabled=acct.get("enabled", True),
            )
            db.update_social_cookies(acct["id"], acct.get("cookies_json") or "")
        else:
            db.update_social_error(acct["id"], result.get("message", "Credenciales inválidas")[:1000])
        return {
            "ok": ok,
            "message": result.get("message", "OK" if ok else "Falló"),
            "screenshot_path": None,
            "cookies_saved": False,
        }

    # ── Playwright platforms: browser login (existing behavior) ──
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
                password=pw,
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
