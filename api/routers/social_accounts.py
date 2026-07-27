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

    valid_platforms = {"tiktok", "twitter", "instagram", "facebook", "reddit"}
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
