"""YouTube channel manager — branding, metadata, profile management via Data API v3.

What CAN be updated via API:
    - snippet.description
    - brandingSettings.channel.keywords
    - brandingSettings.channel.country
    - brandingSettings.channel.defaultLanguage

What CANNOT be updated via API (requires YouTube Studio):
    - snippet.title (tied to Google account name)
    - Banner image (channelBanners API deprecated for non-partnered channels)
    - Avatar / profile picture (tied to Google account picture)
"""

import logging
import pickle
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import TOKENS_DIR
from config.config_bridge import get_channel_config

logger = logging.getLogger(__name__)


class YouTubeChannelManager:
    """Manipulate YouTube channel branding settings via the Data API."""

    def __init__(self, channel_slug: str):
        self.slug = channel_slug
        self._token_path = TOKENS_DIR / f"{channel_slug}.pickle"
        self._service = None

    # ── Authentication ─────────────────────────────────────────

    def authenticate(self) -> bool:
        """Load and refresh channel token. Returns True if authenticated."""
        if not self._token_path.exists():
            logger.error("Token not found: %s. Authenticate first.", self._token_path)
            return False

        try:
            with open(self._token_path, "rb") as f:
                creds = pickle.load(f)
        except Exception as exc:
            logger.error("Cannot load token: %s", exc)
            return False

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(self._token_path, "wb") as f:
                    pickle.dump(creds, f)
            except Exception as exc:
                logger.error("Token refresh failed: %s", exc)
                return False

        self._service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        return True

    # ── Channel metadata update ────────────────────────────────

    def update_channel_metadata(
        self,
        description: str = None,
        keywords: list[str] = None,
        country: str = "ES",
        language: str = "es",
    ) -> dict:
        """Update channel brandingSettings via API (keywords, country, language).

        Note: snippet.description cannot be updated via API for most channels.
        Use YouTube Studio for description changes.

        Returns a dict with {updated_fields: list, ok: bool, ...}
        """
        if not self._service:
            if not self.authenticate():
                return {"error": "Authentication failed"}

        # First, get the current channel data (needed for the update)
        resp = self._service.channels().list(
            part="snippet,brandingSettings",
            mine=True,
        ).execute()

        items = resp.get("items", [])
        if not items:
            return {"error": "No channel found for this account"}

        channel = items[0]
        yt_channel_id = channel["id"]
        snippet = channel.get("snippet", {})
        branding = channel.get("brandingSettings", {}).get("channel", {})

        updated = []

        # Note: snippet.description cannot be updated via API for non-verified channels.
        # Documented in manual_setup_required.

        # Update branding settings
        branding_update = {}
        if keywords is not None:
            branding_update["keywords"] = " ".join(keywords)[:500]
            updated.append("keywords")
        if country is not None:
            branding_update["country"] = country
            updated.append("country")
        if language is not None:
            branding_update["defaultLanguage"] = language
            updated.append("defaultLanguage")

        if not updated:
            return {"message": "Nothing to update", "updated_fields": []}

        try:
            self._service.channels().update(
                part="brandingSettings",
                body={
                    "id": yt_channel_id,
                    "brandingSettings": {"channel": branding_update},
                },
            ).execute()
            logger.info("Channel %s branding updated: %s", self.slug, updated)
            return {
                "ok": True,
                "updated_fields": updated,
                "yt_channel_id": yt_channel_id,
            }
        except HttpError as exc:
            logger.error("Channel update failed: %s", exc)
            return {"error": str(exc), "updated_fields": updated}

    # ── Manual setup report ────────────────────────────────────

    def get_unuploadable_report(self) -> dict:
        """Generate a report of what must be configured manually in YouTube Studio.

        Returns a dict with instructions and file paths for manual setup.
        """
        from config.settings import OUTPUT_DIR
        import json

        cfg = get_channel_config(self.slug)

        banner_path = OUTPUT_DIR / "thumbnails" / self.slug / "banner.jpg"
        avatar_path = OUTPUT_DIR / "thumbnails" / self.slug / "avatar.jpg"

        return {
            "channel_name_suggested": getattr(cfg, "CANAL_DISPLAY_NAME", self.slug),
            "manual_fields": [
                {
                    "field": "channel_name",
                    "reason": "Tied to Google account — change in YouTube Studio",
                    "suggested_value": getattr(cfg, "CANAL_DISPLAY_NAME", ""),
                },
                {
                    "field": "banner_image",
                    "reason": "Banner upload requires YouTube Studio (not available via API)",
                    "file": f"/api/static/thumbnails/{self.slug}/banner.jpg",
                    "dimensions": "2560 x 1440 px",
                    "ready": banner_path.exists(),
                },
                {
                    "field": "avatar_image",
                    "reason": "Profile picture tied to Google account",
                    "file": f"/api/static/thumbnails/{self.slug}/avatar.jpg",
                    "dimensions": "800 x 800 px",
                    "ready": avatar_path.exists(),
                },
            ],
            "instructions": [
                "1. Ve a YouTube Studio → Personalización → Marca",
                "2. Sube la imagen de banner (2560x1440)",
                "3. Sube la imagen de avatar/perfil (800x800)",
                "4. En 'Información básica' cambia el nombre del canal",
                "5. Pega la descripción y keywords que se sincronizaron vía API",
            ],
            "copy_paste_data": {
                "description": getattr(cfg, "CHANNEL_ABOUT_SECTION", ""),
                "keywords": ", ".join(getattr(cfg, "CHANNEL_KEYWORDS", [])),
            },
        }
