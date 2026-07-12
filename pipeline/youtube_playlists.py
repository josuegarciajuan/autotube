"""YouTube Playlist Manager — CRUD operations via YouTube Data API v3.

Creates, lists, and manages playlists per channel. Caches YouTube playlist IDs
in the local database for idempotent operations.

Quota costs (per operation):
  - playlists().list()     → 1 unit
  - playlists().insert()   → 50 units
  - playlistItems().insert() → 50 units
  - playlistItems().list()  → 1 unit
"""

import logging
import pickle
from pathlib import Path
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import TOKENS_DIR

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# Auth helpers (shared pattern with other YouTube modules)
# ═══════════════════════════════════════════════════════════════════

def _load_credentials(token_path: Path) -> Optional[Credentials]:
    """Load and refresh OAuth2 credentials from a pickle file."""
    if not token_path.exists():
        return None
    try:
        with open(token_path, "rb") as f:
            creds = pickle.load(f)
    except Exception as exc:
        logger.warning("Cannot load token %s: %s", token_path, exc)
        return None

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_path, "wb") as f:
                pickle.dump(creds, f)
            logger.info("Token refreshed: %s", token_path)
        except Exception as exc:
            logger.error("Token refresh failed %s: %s", token_path, exc)
            return None
    elif not creds.valid:
        logger.error("Invalid token: %s", token_path)
        return None

    return creds


# ═══════════════════════════════════════════════════════════════════
# YouTubePlaylistManager
# ═══════════════════════════════════════════════════════════════════

class YouTubePlaylistManager:
    """Manage YouTube playlists for a specific channel."""

    def __init__(self, channel_slug: str):
        self.slug = channel_slug
        self._token_path = TOKENS_DIR / f"{channel_slug}.pickle"
        self._service: Any = None
        self._config: Any = None

    # ── Config ────────────────────────────────────────────────────

    @property
    def config(self):
        if self._config is None:
            from config.config_bridge import get_channel_config
            self._config = get_channel_config(self.slug)
        return self._config

    # ── Auth ───────────────────────────────────────────────────────

    def authenticate(self) -> bool:
        """Load and refresh channel token. Returns True if authenticated."""
        creds = _load_credentials(self._token_path)
        if creds is None:
            logger.error("No valid credentials for %s", self.slug)
            return False
        self._service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        return True

    def _ensure_auth(self):
        """Ensure service is authenticated; raise if not."""
        if self._service is None and not self.authenticate():
            raise RuntimeError(f"Cannot authenticate channel {self.slug}")

    # ── Playlist CRUD ─────────────────────────────────────────────

    def list_playlists(self) -> list[dict]:
        """List all playlists for the channel. Returns [{id, title, description, item_count}, ...].

        Quota: 1 unit.
        """
        self._ensure_auth()
        playlists: list[dict] = []
        page_token = None

        while True:
            resp = self._service.playlists().list(
                part="snippet,contentDetails",
                mine=True,
                maxResults=50,
                pageToken=page_token,
            ).execute()

            for item in resp.get("items", []):
                playlists.append({
                    "yt_playlist_id": item["id"],
                    "title": item["snippet"]["title"],
                    "description": item["snippet"].get("description", ""),
                    "item_count": item["contentDetails"]["itemCount"],
                    "published_at": item["snippet"].get("publishedAt", ""),
                })

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return playlists

    def find_playlist_by_title(self, title: str) -> Optional[dict]:
        """Search existing playlists by exact title match. Returns None if not found.

        Quota: 1 unit.
        """
        all_pl = self.list_playlists()
        for pl in all_pl:
            if pl["title"].strip().lower() == title.strip().lower():
                return pl
        return None

    def create_playlist(self, title: str, description: str = "",
                         privacy: str = "public") -> dict:
        """Create a new YouTube playlist.

        Quota: 50 units.

        Returns {yt_playlist_id, title, url}.
        """
        self._ensure_auth()

        body = {
            "snippet": {
                "title": title[:150],
                "description": description[:5000],
            },
            "status": {
                "privacyStatus": privacy,
            },
        }

        resp = self._service.playlists().insert(
            part="snippet,status",
            body=body,
        ).execute()

        yt_id = resp["id"]
        logger.info("[%s] Created playlist: %s (%s)", self.slug, title, yt_id)
        return {
            "yt_playlist_id": yt_id,
            "title": title,
            "url": f"https://www.youtube.com/playlist?list={yt_id}",
        }

    # ── Playlist Items ────────────────────────────────────────────

    def add_video_to_playlist(self, yt_playlist_id: str,
                               yt_video_id: str) -> dict:
        """Add a video to a playlist. Idempotent — skips if already present.

        Quota: 50 units (+ 1 if checking existence).

        Returns {yt_playlist_item_id, was_already_present}.
        """
        self._ensure_auth()

        # Check if already in playlist
        if self.is_video_in_playlist(yt_playlist_id, yt_video_id):
            logger.debug("[%s] Video %s already in playlist %s", self.slug, yt_video_id, yt_playlist_id)
            return {"was_already_present": True}

        body = {
            "snippet": {
                "playlistId": yt_playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": yt_video_id,
                },
            },
        }

        try:
            resp = self._service.playlistItems().insert(
                part="snippet",
                body=body,
            ).execute()
            item_id = resp["id"]
            logger.info("[%s] Added video %s to playlist %s (item: %s)",
                         self.slug, yt_video_id, yt_playlist_id, item_id)
            return {"yt_playlist_item_id": item_id, "was_already_present": False}
        except HttpError as exc:
            # Already exists → 409 conflict
            if exc.resp.status == 409:
                logger.debug("[%s] Video %s already in playlist %s (409)", self.slug, yt_video_id, yt_playlist_id)
                return {"was_already_present": True}
            raise

    def is_video_in_playlist(self, yt_playlist_id: str,
                              yt_video_id: str) -> bool:
        """Check if a video is already in a playlist.

        Quota: 1 unit.
        """
        self._ensure_auth()
        try:
            resp = self._service.playlistItems().list(
                part="snippet",
                playlistId=yt_playlist_id,
                videoId=yt_video_id,
                maxResults=1,
            ).execute()
            return len(resp.get("items", [])) > 0
        except HttpError:
            return False

    # ── High-level operations ─────────────────────────────────────

    def sync_playlists_from_config(self, playlist_configs: list[dict] = None) -> dict:
        """Ensure all playlists defined in channel config exist on YouTube.

        Creates missing playlists, caches IDs in DB. Uses the PLAYLISTS config
        list from the channel config.

        Returns {created: [...], existing: [...], errors: [...]}.
        """
        if playlist_configs is None:
            playlist_configs = getattr(self.config, "PLAYLISTS", [])

        if not playlist_configs:
            logger.info("[%s] No playlists defined in config", self.slug)
            return {"created": [], "existing": [], "errors": []}

        created, existing, errors = [], [], []

        for pl_cfg in playlist_configs:
            name = pl_cfg.get("name", "")
            slug_key = pl_cfg.get("slug", "")
            description = pl_cfg.get("description", "")
            pl_type = pl_cfg.get("type", "thematic")

            if not name or not slug_key:
                errors.append(f"Invalid playlist config (missing name or slug): {pl_cfg}")
                continue

            try:
                found = self.find_playlist_by_title(name)
                if found:
                    existing.append({"slug": slug_key, "name": name, "yt_playlist_id": found["yt_playlist_id"]})
                    logger.debug("[%s] Playlist exists: %s", self.slug, name)
                else:
                    result = self.create_playlist(name, description)
                    created.append({"slug": slug_key, "name": name, "yt_playlist_id": result["yt_playlist_id"]})
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                logger.error("[%s] Error syncing playlist %s: %s", self.slug, name, exc)

        logger.info("[%s] Playlist sync: created=%d existing=%d errors=%d",
                     self.slug, len(created), len(existing), len(errors))
        return {"created": created, "existing": existing, "errors": errors}

    def add_video_to_all_playlists(self, yt_video_id: str,
                                    playlist_configs: list[dict] = None,
                                    auto_classify: bool = False,
                                    title: str = None,
                                    description: str = None) -> dict:
        """Add a video to all configured playlists for the channel.

        Uses cached playlist IDs from DB when available.

        Args:
            yt_video_id: YouTube video ID
            playlist_configs: Optional override playlist configs
            auto_classify: If True and multiple playlists, use AI to pick the
                          best matching playlist and add ONLY to that one.
            title: Video title (needed for auto_classify)
            description: Video description (needed for auto_classify)

        Returns {added_to: [...], already_in: [...], errors: [...],
                 auto_selected: slug or None}.
        """
        if playlist_configs is None:
            playlist_configs = getattr(self.config, "PLAYLISTS", [])

        if not playlist_configs:
            return {"added_to": [], "already_in": [], "errors": []}

        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

        # Get channel DB id
        ch = db.get_channel_by_slug(self.slug)
        if not ch:
            return {"added_to": [], "already_in": [], "errors": ["Channel not found in DB"]}

        channel_id = ch["id"]
        added_to, already_in, errors = [], [], []
        auto_selected = None

        # ── Auto-classify: pick the single best matching playlist ──
        target_playlists = list(playlist_configs)  # copy
        if auto_classify and len(target_playlists) > 1 and title:
            try:
                best_slug = self._classify_playlist(title, description or "", target_playlists)
                if best_slug:
                    target_playlists = [pl for pl in target_playlists if pl.get("slug") == best_slug]
                    auto_selected = best_slug
                    logger.info("[%s] Auto-selected playlist '%s' for video '%s'",
                                self.slug, best_slug, title[:50])
            except Exception as e:
                logger.warning("[%s] Playlist classification failed: %s", self.slug, e)

        for pl_cfg in target_playlists:
            slug_key = pl_cfg.get("slug", "")
            name = pl_cfg.get("name", "")

            if not slug_key:
                continue

            try:
                # Look up cached playlist ID
                cached = db.get_playlist_by_slug(channel_id, slug_key)
                if not cached or not cached.get("yt_playlist_id"):
                    # Try to find or create it
                    found = self.find_playlist_by_title(name)
                    if found:
                        db.upsert_youtube_playlist(channel_id, slug_key, found["yt_playlist_id"],
                                                    name, pl_cfg.get("type", "thematic"))
                        yt_playlist_id = found["yt_playlist_id"]
                    else:
                        errors.append(f"{name}: playlist not found on YouTube")
                        continue
                else:
                    yt_playlist_id = cached["yt_playlist_id"]

                result = self.add_video_to_playlist(yt_playlist_id, yt_video_id)
                if result.get("was_already_present"):
                    already_in.append(slug_key)
                else:
                    added_to.append(slug_key)

            except Exception as exc:
                errors.append(f"{name}: {exc}")
                logger.error("[%s] Error adding video to playlist %s: %s", self.slug, name, exc)

        return {"added_to": added_to, "already_in": already_in, "errors": errors,
                "auto_selected": auto_selected}

    def _classify_playlist(self, title: str, description: str,
                            playlists: list[dict]) -> str | None:
        """Use AI to classify which playlist best matches a video's content.

        Args:
            title: Video title
            description: Video description
            playlists: List of playlist configs [{slug, name, description, type}]

        Returns the slug of the best matching playlist, or None.
        """
        if not playlists or len(playlists) <= 1:
            return playlists[0]["slug"] if playlists else None

        import json as _json
        from openai import OpenAI
        from config.settings import OPENAI_API_KEY, LLM_MODEL

        client = OpenAI(api_key=OPENAI_API_KEY)

        # Build playlist descriptions for the prompt
        playlist_desc = "\n".join(
            f"- {pl['slug']}: {pl.get('description', pl.get('name', ''))}"
            for pl in playlists
        )

        system_prompt = (
            "Eres un clasificador experto en contenido de YouTube. "
            "Tu tarea es leer el título y la descripción de un vídeo, "
            "y elegir la lista de reproducción que mejor encaje con el contenido. "
            "Responde SOLO con el slug de la playlist elegida, sin comillas ni explicaciones."
        )

        user_prompt = (
            f"TÍTULO DEL VÍDEO: {title[:200]}\n\n"
            f"DESCRIPCIÓN: {description[:500]}\n\n"
            f"LISTAS DE REPRODUCCIÓN DISPONIBLES:\n{playlist_desc}\n\n"
            f"Elige el slug de la lista que mejor encaje."
        )

        try:
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=50,
            )
            result = response.choices[0].message.content.strip().lower()
            # Validate that the result matches one of the slugs
            valid_slugs = {pl["slug"].lower() for pl in playlists}
            if result in valid_slugs:
                # Return the original casing
                for pl in playlists:
                    if pl["slug"].lower() == result:
                        return pl["slug"]
            logger.warning("[%s] AI returned invalid playlist slug: %s", self.slug, result)
            return None
        except Exception as e:
            logger.warning("[%s] Playlist classification LLM error: %s", self.slug, e)
            return None
