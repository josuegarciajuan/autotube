"""YouTube OAuth authentication endpoints.

Provides headless-safe OAuth for multi-channel setups.
"""

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from api.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


class AuthStartResponse(BaseModel):
    auth_url: str
    message: str


class AuthCodeRequest(BaseModel):
    code: str


@router.post("/channels/{channel_id}/auth-start")
def auth_start(channel_id: int):
    """Initiate OAuth flow — returns the authorization URL.

    User opens this URL in their browser, authorizes,
    copies the 'code' from the redirect URL, and calls auth-code.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    slug = ch["slug"]

    # ── Delegación al agente egress (canal gestionado) ──
    from api.services.egress_delegation import egress_client_for
    _client = egress_client_for(slug)
    if _client is not None:
        try:
            auth_url = _client.oauth_url()
        except Exception as exc:
            raise HTTPException(503, f"Agente egress no accesible para {slug}: {exc}")
        if not auth_url:
            raise HTTPException(500, "Could not generate auth URL — check client_secret")
        return {
            "auth_url": auth_url,
            "message": (
                "Canal gestionado por agente egress: abre esta URL en un navegador "
                "que salga por la IP del canal, autoriza, y pega el 'code'. "
                "El intercambio del código lo hace el agente (IP aislada)."
            ),
        }

    from pipeline.youtube_uploader import YouTubeUploader

    uploader = YouTubeUploader(account_name=slug, channel_slug=slug)
    auth_url = uploader.get_auth_url()

    if not auth_url:
        raise HTTPException(500, "Could not generate auth URL — check client_secret")

    return {
        "auth_url": auth_url,
        "message": (
            "Abre esta URL en tu navegador, autoriza con la cuenta "
            "Google del canal, y copia el parámetro 'code' de la URL "
            "de redirección. Luego llama a POST /auth-code con el código."
        ),
    }


@router.post("/channels/{channel_id}/auth-code")
def auth_code(channel_id: int, data: AuthCodeRequest):
    """Complete OAuth flow with the authorization code.

    Exchanges the code for credentials and saves the token.
    """
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    slug = ch["slug"]

    # ── Delegación al agente egress (canal gestionado) ──
    from api.services.egress_delegation import egress_client_for
    _client = egress_client_for(slug)
    if _client is not None:
        try:
            ok = _client.auth_exchange(data.code)
        except Exception as exc:
            raise HTTPException(503, f"Agente egress no accesible para {slug}: {exc}")
        if not ok:
            raise HTTPException(400, "Auth failed. Check that the code is valid and not expired.")
        return {"ok": True, "message": "✅ Autorizado vía agente egress (IP aislada)"}

    from pipeline.youtube_uploader import YouTubeUploader

    uploader = YouTubeUploader(account_name=slug, channel_slug=slug)
    ok = uploader.complete_auth_with_code(data.code)

    if not ok:
        raise HTTPException(400, "Auth failed. Check that the code is valid and not expired.")

    # Verify and get channel info from YouTube
    try:
        service = uploader._get_service()
        resp = service.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items", [])
        if items:
            yt_channel_id = items[0]["id"]
            yt_title = items[0]["snippet"]["title"]
            db.update_channel_profile_fields(
                channel_id,
                yt_channel_id=yt_channel_id,
                yt_channel_url=f"https://www.youtube.com/channel/{yt_channel_id}",
            )
            return {
                "ok": True,
                "channel_title": yt_title,
                "yt_channel_id": yt_channel_id,
                "message": f"✅ Conectado a: {yt_title}",
            }
    except Exception as exc:
        logger.warning("Auth succeeded but couldn't fetch channel info: %s", exc)

    return {"ok": True, "message": "✅ Authorized but channel info not fetched"}


@router.get("/channels/{channel_id}/auth-status")
def auth_status(channel_id: int):
    """Check current OAuth status for a channel."""
    db = get_db()
    ch = db.get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    slug = ch["slug"]

    from pipeline.youtube_uploader import YouTubeUploader

    uploader = YouTubeUploader(account_name=slug, channel_slug=slug)
    return uploader.check_auth_status()


# ── Browser session status ─────────────────────────────────────

@router.get("/browser-sessions/status")
async def browser_session_status():
    """Check if Playwright browser sessions are valid for all accounts.
    
    Returns per-account status with status field:
        "valid" — session is authenticated
        "expired" — session needs re-login
        "in_use" — profile is currently in use by another process (NOT expired)
        "error" — couldn't verify (transient)
        "missing_profile" — profile doesn't exist (never logged in)
    """
    try:
        from pipeline.youtube_browser import get_all_browser_session_status
        accounts = await get_all_browser_session_status()
        return {
            "accounts": accounts,
            "all_valid": all(a["status"] == "valid" for a in accounts),
            "any_invalid": any(a["status"] not in ("valid", "in_use") for a in accounts),
        }
    except Exception as e:
        logger.error("Failed to check browser session status: %s", e)
        return {
            "accounts": [],
            "all_valid": False,
            "any_invalid": True,
            "error": str(e),
        }
