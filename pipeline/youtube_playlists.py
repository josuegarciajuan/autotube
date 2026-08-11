"""YouTube Playlist Manager — CRUD operations via YouTube Data API v3.
 
Creates, lists, and manages playlists per channel. Caches YouTube playlist IDs
in the local database for idempotent operations.

Also provides playlist selection for viral mirror discovery.
 
Quota costs (per operation):
  - playlists().list()     → 1 unit
  - playlists().insert()   → 50 units
  - playlistItems().insert() → 50 units
  - playlistItems().list()  → 1 unit
"""

import logging
import pickle
import random
from pathlib import Path
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config.settings import TOKENS_DIR

# ── Quota tracking (passive diagnostic — no behavioral change) ──
from api.services.quota_tracker import track_quota

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


# ── Playlist creation from channel config ─────────────────────────────

def create_playlists_for_channel(channel_slug: str, force: bool = False) -> dict:
    """Create 10 thematic playlists for a channel on YouTube.

    Uses the LLM-based playlist generator to produce subniche playlists,
    creates them on YouTube via the API, and caches IDs in the local DB.

    Idempotent: skips channels that already have playlists in DB unless
    ``force=True``.

    Args:
        channel_slug: Channel slug (e.g. "canal2")
        force: If True, regenerate even if playlists already exist in DB

    Returns:
        {"created_count": int, "existing_count": int, "errors": [...],
         "playlists": [config dicts]}
    """
    import json as _json
    from database.db_extended import ExtendedDatabase

    db = ExtendedDatabase()

    # Get channel DB record
    ch = db.get_channel_by_slug(channel_slug)
    if not ch:
        return {"error": f"Channel slug '{channel_slug}' not found in DB"}

    channel_id = ch["id"]
    channel_name = ch.get("name", channel_slug)

    # Check existing playlists (skip if already created, unless force)
    if not force:
        existing = db.get_channel_youtube_playlists(channel_id)
        if existing:
            logger.info("[%s] Channel already has %d playlists in DB — skipping. Use force=True to regenerate.",
                       channel_slug, len(existing))
            return {"created_count": 0, "existing_count": len(existing),
                    "skipped": True, "playlists": existing}

    # Load channel config for niche info
    try:
        from config.config_bridge import get_channel_config
        config = get_channel_config(channel_slug)
        niche_kw = getattr(config, "NICHE_KEYWORDS_ENG", []) or getattr(config, "CHANNEL_KEYWORDS", [])
        if isinstance(niche_kw, str):
            niche_kw = [niche_kw]
        niche_desc = getattr(config, "CANAL_TAGLINE", "") or ""
    except ImportError:
        logger.warning("[%s] Cannot load Python config — using channel name only", channel_slug)
        niche_kw = []
        niche_desc = ""

    # Generate playlist configs via LLM
    from pipeline.playlist_generator import generate_playlists_for_channel as _gen

    playlist_configs = _gen(
        channel_name=channel_name,
        niche_keywords=list(niche_kw)[:10] if niche_kw else None,
        niche_description=niche_desc,
        language="es",
        count=10,
    )

    if not playlist_configs:
        return {"error": "LLM playlist generation returned empty result"}

    # Store generated playlists in channel config_json
    try:
        config_json = _json.loads(ch.get("config_json", "{}")) if isinstance(ch.get("config_json"), str) else (ch.get("config_json") or {})
    except (_json.JSONDecodeError, TypeError):
        config_json = {}
    config_json["PLAYLISTS_GENERATED"] = playlist_configs
    db.update_channel(channel_id, config=config_json)

    # Create on YouTube + cache in DB
    mgr = YouTubePlaylistManager(channel_slug)
    if not mgr.authenticate():
        return {"error": f"Cannot authenticate channel '{channel_slug}' — check OAuth token"}

    created, errors = [], []
    for pl_cfg in playlist_configs:
        name = pl_cfg.get("name", "")
        slug_key = pl_cfg.get("slug", "")
        description = pl_cfg.get("description", "")
        pl_type = pl_cfg.get("type", "thematic")

        if not name or not slug_key:
            errors.append(f"Invalid playlist config: {pl_cfg}")
            continue

        try:
            found = mgr.find_playlist_by_title(name)
            if found:
                yt_id = found["yt_playlist_id"]
                logger.info("[%s] Playlist exists on YT: '%s' (%s)", channel_slug, name, yt_id)
            else:
                result = mgr.create_playlist(name, description)
                yt_id = result["yt_playlist_id"]

            # Cache in DB
            db.upsert_youtube_playlist(channel_id, slug_key, yt_id, name, pl_type)
            created.append({"slug": slug_key, "name": name, "yt_playlist_id": yt_id})

        except Exception as exc:
            errors.append(f"{name}: {exc}")
            logger.error("[%s] Error creating playlist '%s': %s", channel_slug, name, exc)

    logger.info("[%s] Playlists created: %d, errors: %d", channel_slug, len(created), len(errors))
    return {"created_count": len(created), "existing_count": 0, "errors": errors,
            "playlists": playlist_configs}


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

            # ── Track quota (diagnostic) ──────────────────────────
            track_quota(self.slug, "playlists.list", 1,
                        caller="list_playlists")

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

        # ── Track quota (diagnostic) ──────────────────────────────
        track_quota(self.slug, "playlists.insert", 50,
                    yt_id=resp.get("id", ""), caller="create_playlist")

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

            # ── Track quota (diagnostic) ──────────────────────────
            track_quota(self.slug, "playlistItems.insert", 50,
                        yt_id=yt_video_id, caller="add_video_to_playlist")

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

    def sync_playlists_from_config(self, playlist_configs: list[dict] = None,
                                     include_generated: bool = True) -> dict:
        """Ensure all playlists defined in channel config exist on YouTube.

        Creates missing playlists, caches IDs in DB. Uses:
          1. Static PLAYLISTS from channel config (canalX_config.py)
          2. LLM-generated PLAYLISTS_GENERATED from channels.config_json (if include_generated=True)

        Returns {created: [...], existing: [...], errors: [...]}.
        """
        all_configs = []

        # ── 1. Static playlists from Python config ──
        if playlist_configs is None:
            static_cfgs = getattr(self.config, "PLAYLISTS", [])
        else:
            static_cfgs = playlist_configs
        all_configs.extend(static_cfgs)

        # ── 2. LLM-generated playlists from DB config_json ──
        if include_generated:
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
            ch = db.get_channel_by_slug(self.slug)
            if ch:
                cj = ch.get("config_json", "{}")
                if isinstance(cj, str):
                    import json; cj = json.loads(cj) if cj else {}
                generated = cj.get("PLAYLISTS_GENERATED", [])
                if generated:
                    logger.info("[%s] Including %d LLM-generated playlists in sync",
                               self.slug, len(generated))
                    all_configs.extend(generated)

        if not all_configs:
            logger.info("[%s] No playlists defined in config", self.slug)
            return {"created": [], "existing": [], "errors": []}

        # ── Deduplicate by slug (static takes precedence) ──
        seen = set()
        deduped = []
        for cfg in all_configs:
            s = cfg.get("slug", "")
            if s and s not in seen:
                seen.add(s)
                deduped.append(cfg)

        created, existing, errors = [], [], []

        for pl_cfg in deduped:
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

        logger.info("[%s] Playlist sync: created=%d existing=%d errors=%d (total configs: %d)",
                     self.slug, len(created), len(existing), len(errors), len(deduped))
        return {"created": created, "existing": existing, "errors": errors}

    def sync_and_cache_all_playlists(self, channel_id: int) -> int:
        """Sync all playlists (static + generated) to YouTube and cache in DB.

        This is the comprehensive sync that covers both playlist sources.
        Returns the number of playlists in DB after sync.
        """
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

        sync_result = self.sync_playlists_from_config(include_generated=True)

        for pl in sync_result.get("created", []):
            db.upsert_youtube_playlist(
                channel_id, pl["slug"], pl["yt_playlist_id"], pl.get("name"),
            )
        for pl in sync_result.get("existing", []):
            db.upsert_youtube_playlist(
                channel_id, pl["slug"], pl["yt_playlist_id"], pl.get("name"),
            )

        all_pl = db.get_channel_youtube_playlists(channel_id)
        logger.info("[%s] Total playlists in DB after sync: %d (created=%d, existing=%d)",
                     self.slug, len(all_pl),
                     len(sync_result.get("created", [])),
                     len(sync_result.get("existing", [])))
        return len(all_pl)

    def add_video_to_all_playlists(self, yt_video_id: str,
                                    playlist_configs: list[dict] = None) -> dict:
        """Add a video to all configured playlists for the channel.

        Uses cached playlist IDs from DB when available.

        Returns {added_to: [...], already_in: [...], errors: [...]}.
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

        for pl_cfg in playlist_configs:
            slug_key = pl_cfg.get("slug", "")
            name = pl_cfg.get("name", "")

            if not slug_key:
                continue

            try:
                cached = db.get_playlist_by_slug(channel_id, slug_key)
                if not cached or not cached.get("yt_playlist_id"):
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

        return {"added_to": added_to, "already_in": already_in, "errors": errors}

    def add_video_to_playlist_by_slug(self, yt_video_id: str, playlist_slug: str,
                                       channel_id: int = None) -> dict:
        """Add a video to a specific playlist by its DB slug (not YouTube ID).

        Looks up the YouTube playlist ID from the local cache. If not found,
        triggers a full sync (static + generated playlists) and retries.

        Returns: same as ``add_video_to_playlist()``, plus ``error`` key on failure.
        """
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

        if channel_id is None:
            ch = db.get_channel_by_slug(self.slug)
            if not ch:
                logger.warning("[%s] add_video_to_playlist_by_slug: channel '%s' not found in DB",
                             self.slug, self.slug)
                return {"error": "Channel not found in DB"}
            channel_id = ch["id"]

        logger.info("[%s] add_video_to_playlist_by_slug: looking up slug='%s' for channel_id=%d",
                    self.slug, playlist_slug, channel_id)

        cached = db.get_playlist_by_slug(channel_id, playlist_slug)
        if not cached or not cached.get("yt_playlist_id"):
            # ── Fallback: sync all playlists and retry ──
            logger.warning(
                "[%s] Playlist slug '%s' NOT FOUND in DB (channel_id=%d). "
                "Triggering full sync and retrying...",
                self.slug, playlist_slug, channel_id,
            )
            # Log what IS in DB for diagnostics
            all_pl = db.get_channel_youtube_playlists(channel_id)
            logger.info("[%s] Available playlists in DB (%d): %s",
                       self.slug, len(all_pl),
                       [(p.get("slug"), p.get("name")) for p in all_pl])

            try:
                self.sync_and_cache_all_playlists(channel_id)
                # Retry lookup after sync
                cached = db.get_playlist_by_slug(channel_id, playlist_slug)
            except Exception as sync_exc:
                logger.error("[%s] Fallback sync failed: %s", self.slug, sync_exc)

        if not cached or not cached.get("yt_playlist_id"):
            # Still not found after sync
            logger.error(
                "[%s] CRITICAL: Playlist slug '%s' still not in DB after full sync. "
                "Video %s will NOT be added to any playlist.",
                self.slug, playlist_slug, yt_video_id,
            )
            return {"error": f"Playlist slug '{playlist_slug}' not cached in DB (after sync retry)"}

        logger.info("[%s] Found playlist '%s' → yt_playlist_id=%s",
                    self.slug, cached.get("name", playlist_slug), cached["yt_playlist_id"])

        result = self.add_video_to_playlist(cached["yt_playlist_id"], yt_video_id)
        if result.get("yt_playlist_item_id"):
            logger.info("[%s] ✅ SUCCESS: Video %s added to playlist '%s' (item: %s)",
                       self.slug, yt_video_id, cached.get("name", playlist_slug),
                       result["yt_playlist_item_id"])
        elif result.get("was_already_present"):
            logger.info("[%s] ⏭️ Video %s already in playlist '%s'",
                       self.slug, yt_video_id, cached.get("name", playlist_slug))
        return result
