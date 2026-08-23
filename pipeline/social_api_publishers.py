"""API-based publishers for the EMBUDO (redirect) platforms: Bluesky + Mastodon.

These platforms do NOT host long horizontal video — the strategy here is a
teaser post (text + link card) that redirects traffic to YouTube. They have
free, open REST APIs that accept a simple token, so no Playwright is needed.

Credentials (from channel_social_accounts):
    bluesky:  username = handle (e.g. canal2.bsky.social)
              encrypted_password = app password
    mastodon: username = "user@instance" (e.g. canal2@mastodon.social)
              encrypted_password = OAuth access token
"""

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

BSKY_HOST = "https://bsky.social"


# ════════════════════════════════════════════════════════════════
# Bluesky (AT Protocol)
# ════════════════════════════════════════════════════════════════


def _bsky_session(handle: str, pw: str) -> dict | None:
    """Create an AT Protocol session → {accessJwt, did, handle}."""
    resp = requests.post(
        f"{BSKY_HOST}/xrpc/com.atproto.server.createSession",
        json={"identifier": handle, "password": pw},
        timeout=30,
    )
    if resp.status_code == 200:
        data = resp.json()
        return {
            "access_jwt": data.get("accessJwt", ""),
            "did": data.get("did", ""),
            "handle": data.get("handle", handle),
        }
    logger.warning("[Bluesky] createSession failed: HTTP %d — %s",
                   resp.status_code, resp.text[:300])
    return None


def _bsky_upload_thumb(access_jwt: str, thumb_path: str) -> str | None:
    """Upload a thumbnail blob (best-effort) → blob ref for the link card."""
    if not thumb_path or not os.path.exists(thumb_path):
        return None
    try:
        with open(thumb_path, "rb") as f:
            mime = "image/jpeg" if thumb_path.lower().endswith((".jpg", ".jpeg")) else "image/png"
            resp = requests.post(
                f"{BSKY_HOST}/xrpc/com.atproto.repo.uploadBlob",
                headers={
                    "Authorization": f"Bearer {access_jwt}",
                    "Content-Type": mime,
                },
                data=f.read(),
                timeout=60,
            )
        if resp.status_code == 200:
            blob = resp.json().get("blob", {})
            if blob.get("ref"):
                return {
                    "$type": "blob",
                    "ref": blob["ref"],
                    "mimeType": blob.get("mimeType", mime),
                    "size": blob.get("size", 0),
                }
    except Exception as exc:
        logger.warning("[Bluesky] Thumb upload skipped: %s", exc)
    return None


def _bsky_link_facet(text: str, url: str) -> list | None:
    """Build a clickable link facet for the URL inside text (byte offsets)."""
    idx = text.find(url)
    if idx < 0:
        return None
    start_byte = len(text[:idx].encode("utf-8"))
    end_byte = start_byte + len(url.encode("utf-8"))
    return [{
        "index": {"byteStart": start_byte, "byteEnd": end_byte},
        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
    }]


def publish_bluesky(content, creds: dict,
                    thumbnail_path: str = "") -> dict:
    """Publish a teaser post with link card to Bluesky.

    Returns {success: bool, post_url: str, post_id: str, error: str}.
    """
    handle = (creds.get("username") or "").strip()
    pw = (creds.get("password") or "").strip()
    if not handle or not pw:
        return {"success": False, "error": "Missing Bluesky handle or app password"}

    yt_url = (content.yt_url or "").strip()
    text = (content.text or "").strip()
    if not text and not yt_url:
        return {"success": False, "error": "Empty post text"}

    # Append the URL at the end so the link card + clickable facet work
    if yt_url and yt_url not in text:
        text = f"{text}\n\n{yt_url}".strip()

    session = _bsky_session(handle, pw)
    if not session:
        return {"success": False, "error": "Bluesky session failed (credenciales inválidas)"}

    record = {
        "text": text[:300],
        "createdAt": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    }
    if content.hashtags:
        hashtag_str = " ".join(
            h if h.startswith("#") else f"#{h}" for h in content.hashtags[:5]
        )
        record["text"] = (text[: 300 - len(hashtag_str) - 1] + "\n" + hashtag_str).strip()

    facets = _bsky_link_facet(record["text"], yt_url) if yt_url else None
    if facets:
        record["facets"] = facets

    # Link card embed (external)
    if yt_url:
        title = getattr(content, "video_title", "") or "Mira el vídeo completo"
        external = {
            "$type": "app.bsky.embed.external",
            "external": {
                "uri": yt_url,
                "title": (title or "Mira el vídeo completo")[:80],
                "description": (text[:160] if not title else ""),
            },
        }
        thumb = _bsky_upload_thumb(session["access_jwt"], thumbnail_path)
        if thumb:
            external["external"]["thumb"] = thumb
        record["embed"] = external

    try:
        resp = requests.post(
            f"{BSKY_HOST}/xrpc/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {session['access_jwt']}"},
            json={
                "repo": session["did"],
                "collection": "app.bsky.feed.post",
                "record": record,
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            rkey = data.get("uri", "").rsplit("/", 1)[-1]
            post_url = f"https://bsky.app/profile/{session['handle']}/post/{rkey}"
            return {"success": True, "post_url": post_url, "post_id": rkey}
        logger.warning("[Bluesky] createRecord failed: HTTP %d — %s",
                       resp.status_code, resp.text[:300])
        return {"success": False, "error": f"createRecord HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:
        logger.exception("[Bluesky] publish error")
        return {"success": False, "error": str(exc)}


# ════════════════════════════════════════════════════════════════
# Mastodon
# ════════════════════════════════════════════════════════════════


def _mastodon_split_username(username: str) -> tuple[str, str]:
    """'canal2@mastodon.social' → (instance, user)."""
    if "@" in username:
        user, _, instance = username.rpartition("@")
        return instance.strip(), user.strip()
    return "", username.strip()


def publish_mastodon(content, creds: dict) -> dict:
    """Publish a teaser post (text + link, auto-linkified) to Mastodon.

    Returns {success: bool, post_url: str, post_id: str, error: str}.
    """
    username = (creds.get("username") or "").strip()
    tok = (creds.get("password") or "").strip()
    instance, user = _mastodon_split_username(username)
    if not instance or not tok:
        return {"success": False, "error": "Missing Mastodon instance or access token"}

    yt_url = (content.yt_url or "").strip()
    text = (content.text or "").strip()
    if not text and not yt_url:
        return {"success": False, "error": "Empty post text"}

    if yt_url and yt_url not in text:
        text = f"{text}\n\n{yt_url}".strip()
    if content.hashtags:
        hashtag_str = " ".join(
            h if h.startswith("#") else f"#{h}" for h in content.hashtags[:6]
        )
        text = f"{text}\n\n{hashtag_str}".strip()

    base = f"https://{instance}"
    headers = {"Authorization": f"Bearer {tok}"}
    try:
        resp = requests.post(
            f"{base}/api/v1/statuses",
            headers=headers,
            data={
                "status": text[:500],
                "visibility": "public",
                "language": "es",
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            status_id = data.get("id", "")
            acct = data.get("account", {}).get("acct", user)
            post_url = data.get("url", "") or f"{base}/@{acct}/{status_id}"
            return {"success": True, "post_url": post_url, "post_id": status_id}
        logger.warning("[Mastodon] status create failed: HTTP %d — %s",
                       resp.status_code, resp.text[:300])
        return {"success": False, "error": f"statuses HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:
        logger.exception("[Mastodon] publish error")
        return {"success": False, "error": str(exc)}
