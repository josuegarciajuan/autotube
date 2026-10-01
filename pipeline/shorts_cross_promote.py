"""Shorts cross-promotion engine.
 
Links shorts to long-form videos via descriptions, playlists, and comments.
Centralizes all cross-promotion logic so every publish point (API, scheduler,
standalone pipeline) behaves consistently.
 
Architecture:
  get_best_longform_link()       → Find the right long-form video to link
  get_source_video_info()        → Look up source video title + yt_id
  build_short_description()      → Format description with short copy-friendly URLs
  run_post_publish_promotion()   → Playlists + per-video playlist + first comment
"""
 
import logging
import re
from pathlib import Path
from typing import Optional
 
from config.settings import DATABASE_PATH
 
logger = logging.getLogger(__name__)
 
# ── Default playlist name per channel ─────────────────────────
 
DEFAULT_SHORTS_PLAYLIST = "Shorts"
PER_VIDEO_PLAYLIST_SUFFIX = "| Shorts"


# ═══════════════════════════════════════════════════════════════
# Link resolution
# ═══════════════════════════════════════════════════════════════

def get_best_longform_link(
    channel_id: int,
    source_video_id: int | None = None,
    topic: str = "",
) -> str | None:
    """Find the best long-form video URL to link from a short.

    Priority:
      1. If source_video_id is set (clip short) AND is publicly available, link
         to that video.
      2. Otherwise link to the most thematically relevant public long-form
         (token overlap with ``topic``); si no hay tema, el más reciente.

    F8: nunca enlaza a un vídeo privado/programado (evita un link muerto).
    Returns a full YouTube watch URL or None.
    """
    import sqlite3

    conn = sqlite3.connect(str(DATABASE_PATH), timeout=60)

    try:
        # Priority 1: explicit source video, solo si es público
        if source_video_id:
            row = conn.execute(
                """SELECT yt_video_id FROM videos
                   WHERE id = ? AND yt_video_id IS NOT NULL AND yt_video_id != ''
                     AND status IN ('published', 'uploaded')
                     AND (privacy_status IS NULL OR privacy_status = 'public')""",
                (source_video_id,),
            ).fetchone()
            if row and row[0]:
                return f"https://www.youtube.com/watch?v={row[0]}"

        # Priority 2: públicos recientes del canal
        rows = conn.execute(
            """SELECT yt_video_id, titulo_final FROM videos
               WHERE channel_id = ?
                 AND yt_video_id IS NOT NULL AND yt_video_id != ''
                 AND status IN ('published', 'uploaded')
                 AND (privacy_status IS NULL OR privacy_status = 'public')
               ORDER BY created_at DESC
               LIMIT 30""",
            (channel_id,),
        ).fetchall()
        if not rows:
            return None

        # Relevancia temática (F8): mejor solapamiento; si no hay, el reciente.
        if topic:
            try:
                from pipeline.topic_dedup import topic_tokens
                seed = topic_tokens(topic)
                best_score, best_id = -1, None
                for r in rows:
                    score = len(seed & topic_tokens(r[1] or "")) if seed else 0
                    if score > best_score:
                        best_score, best_id = score, r[0]
                if best_id and best_score > 0:
                    return f"https://www.youtube.com/watch?v={best_id}"
            except Exception as exc:  # noqa: BLE001
                logger.debug("longform relevance ranking failed: %s", exc)

        return f"https://www.youtube.com/watch?v={rows[0][0]}"
    finally:
        conn.close()


def attributable_conversion(db, short_id: int) -> dict:
    """Conversión atribuible de un short, o ``unknown`` si no hay dato fiable.

    YouTube no expone clics en enlaces de descripciones de Shorts, así que no se
    inventa una tasa: se devuelve ``unknown`` salvo que existan stats del short.
    """
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT subscribers_gained, views FROM short_stats "
                "WHERE short_id=? ORDER BY id DESC LIMIT 1",
                (short_id,),
            ).fetchone()
        if not row:
            return {"status": "unknown", "reason": "sin stats"}
        subs, views = int(row[0] or 0), int(row[1] or 0)
        return {
            "status": "known",
            "subs_gained": subs,
            "views": views,
            "conversion_pct": round(subs / views * 100, 3) if views else None,
            "note": "subs ganados atribuidos al short; no aislables de otros vídeos",
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "reason": str(exc)[:120]}


# ═══════════════════════════════════════════════════════════════
# Description builder
# ═══════════════════════════════════════════════════════════════

def build_short_description(
    hook_text: str = "",
    hashtags: list[str] | None = None,
    longform_url: str | None = None,
    channel_url: str = "",
    subscribe_reason: str = "",
) -> str:
    """Build a YouTube short description with cross-promotion links.

    Description links in Shorts are NOT clickable (YouTube platform limitation),
    so we use the shortest possible URL format (youtu.be/XXXXX) and an explicit
    "copy & paste" call-to-action so mobile users can act on it.

    Template:
      {hook_text}

      #hashtag1 #hashtag2 ...

      📺 Video completo → {short_url}
         (copia y pega en tu navegador)

      🔔 Suscríbete: {channel_short}
    """
    parts = []

    if hook_text:
        parts.append(hook_text.strip())
        parts.append("")  # blank line

    if hashtags:
        clean_tags = [
            f"#{t.strip('#')}" for t in hashtags[:10] if t.strip()
        ]
        parts.append(" ".join(clean_tags))
        parts.append("")

    if longform_url:
        short_url = _to_youtu_be(longform_url)
        parts.append(f"📺 Video completo → {short_url}")
        parts.append("   (copia y pega en tu navegador)")
        parts.append("")

    if channel_url:
        short_channel = channel_url.replace("https://www.youtube.com/", "")
        # F8: una razón concreta convierte más que un "suscríbete" genérico.
        if subscribe_reason:
            parts.append(f"🔔 Suscríbete para más: {subscribe_reason}")
            parts.append(f"   {short_channel}")
        else:
            parts.append(f"🔔 Suscríbete: {short_channel}")

    return "\n".join(parts).strip()


# ═══════════════════════════════════════════════════════════════
# URL helpers
# ═══════════════════════════════════════════════════════════════

def _to_youtu_be(long_url: str) -> str:
    """Convert a YouTube watch URL to the shorter 'youtu.be/XXXXX' format.

    The youtu.be format is easier to copy/paste on mobile and more
    compact in Shorts descriptions where URLs are not clickable.
    """
    match = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', long_url)
    if match:
        return f"youtu.be/{match[1]}"
    return long_url


# ═══════════════════════════════════════════════════════════════
# Source video info (for per-video playlists)
# ═══════════════════════════════════════════════════════════════

def get_source_video_info(source_video_id: int) -> dict | None:
    """Look up the source (long-form) video's YouTube ID and title.

    Used to build per-video playlist names like 'Titulo del video | Shorts'.

    Returns dict with {yt_video_id, title} or None.
    """
    import sqlite3

    conn = sqlite3.connect(str(DATABASE_PATH), timeout=60)
    try:
        row = conn.execute(
            """SELECT yt_video_id, titulo_final FROM videos
               WHERE id = ?
                 AND yt_video_id IS NOT NULL
                 AND yt_video_id != ''""",
            (source_video_id,),
        ).fetchone()
        if row and row[0]:
            return {"yt_video_id": row[0], "title": row[1] or "Video"}
        return None
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# Post-publish promotion
# ═══════════════════════════════════════════════════════════════

def run_post_publish_promotion(
    channel_slug: str,
    short_yt_id: str,
    channel_id: int,
    source_yt_id: str | None = None,
    source_video_id: int | None = None,
    first_comment_text: str | None = None,
    playlist_name: str = None,
    channel_config = None,
) -> dict:
    """Execute all post-publish cross-promotion actions for a short.

    Actions (non-critical — failures are logged, never raised):
      1. Add short to the generic "Shorts" playlist (creates it if missing).
      2. Add the source long-form video to the "Shorts" playlist so both
         appear together in the same browsable collection.
      3. If this is a clip short AND per-video playlists are enabled,
         create a dedicated "{Source Title} | Shorts" playlist and add
         both the long-form video and the short to it.
      4. Post a first comment linking back to the source video (if enabled).

    Returns a dict with action results.
    """
    # ── Master switch: apagado por canal (SHORTS_LONGFORM_LINK_ENABLED=False) ──
    # Quota pruning (ago 2026): los shorts se suben SIN playlist ni comentario.
    # Esta guard cubre las 5 rutas de llamada (shorts_native, api/routers/shorts.py
    # ×4, backfill). Sin ella, el paso 1 (playlist "Shorts") seguiría ejecutándose
    # aunque el link long-form estuviera desactivado.
    if channel_config is not None and not getattr(channel_config, "SHORTS_LONGFORM_LINK_ENABLED", True):
        return {
            "skipped": True,
            "reason": "cross_promote_disabled",
            "playlist_added": False,
            "per_video_playlist_added": False,
            "source_in_shorts_playlist": False,
            "comment_posted": False,
            "errors": [],
        }

    # ── Quota-aware gating: 3 tiers ─────────────────────────────
    # Tier CRITICAL (>85%): skip EVERYTHING — just upload the short.
    # Tier TIGHT (>70%): skip comments + per-video playlists (expensive).
    # Below 70%: full cross-promotion.
    from api.services.quota_tracker import (
        should_skip_all_cross_promote,
        should_skip_short_comments,
        should_skip_per_video_playlist,
    )
    if should_skip_all_cross_promote(channel_slug):
        return {
            "skipped": True,
            "reason": "quota_critical",
            "playlist_added": False,
            "per_video_playlist_added": False,
            "source_in_shorts_playlist": False,
            "comment_posted": False,
            "errors": [],
        }
    quota_tight = should_skip_short_comments(channel_slug)  # >70%

    playlist_name = playlist_name or DEFAULT_SHORTS_PLAYLIST
    per_video_pl = (
        getattr(channel_config, "SHORTS_PER_VIDEO_PLAYLIST", True)
        if channel_config is not None
        else True
    )

    result = {
        "playlist_added": False,
        "per_video_playlist_added": False,
        "source_in_shorts_playlist": False,
        "comment_posted": False,
        "errors": [],
    }

    pm = None  # lazy-init playlist manager

    def _get_pm():
        nonlocal pm
        if pm is None:
            from pipeline.youtube_playlists import YouTubePlaylistManager
            pm = YouTubePlaylistManager(channel_slug)
            pm.authenticate()
        return pm

    # ── 1. Add short to generic Shorts playlist ─────────────────
    try:
        pmgr = _get_pm()
        yt_playlist_id = _ensure_playlist(pmgr, playlist_name)
        if yt_playlist_id:
            pmgr.add_video_to_playlist(yt_playlist_id, short_yt_id)
            result["playlist_added"] = True
            logger.info(
                "[%s] Short %s → playlist '%s'",
                channel_slug, short_yt_id, playlist_name,
            )
    except Exception as exc:
        msg = f"Shorts playlist add failed: {exc}"
        result["errors"].append(msg)
        logger.warning("[%s] %s", channel_slug, msg)

    # ── 2. Add source long-form video to Shorts playlist ─────────
    #    So viewers browsing the Shorts playlist see both formats.
    if source_yt_id:
        try:
            pmgr = _get_pm()
            yt_playlist_id = _ensure_playlist(pmgr, playlist_name)
            if yt_playlist_id:
                pmgr.add_video_to_playlist(yt_playlist_id, source_yt_id)
                result["source_in_shorts_playlist"] = True
                logger.info(
                    "[%s] Long-form %s → playlist '%s'",
                    channel_slug, source_yt_id, playlist_name,
                )
        except Exception as exc:
            msg = f"Adding source video to Shorts playlist failed: {exc}"
            result["errors"].append(msg)
            logger.warning("[%s] %s", channel_slug, msg)

    # ── 3. Per-video playlist: "{Title} | Shorts" ─────────────────
    #    Only for clip shorts that have an explicit source_video_id.
    #    Skip when quota is tight (>70%) to save 101+ units per short.
    if not quota_tight and per_video_pl and source_yt_id and source_video_id:
        try:
            source_info = get_source_video_info(source_video_id)
            if source_info and source_info.get("title"):
                per_video_name = f"{source_info['title'][:120]} {PER_VIDEO_PLAYLIST_SUFFIX}"
                pmgr = _get_pm()
                per_video_pl_id = _ensure_playlist(pmgr, per_video_name)
                if per_video_pl_id:
                    # Add both the long-form source and this short
                    pmgr.add_video_to_playlist(per_video_pl_id, source_yt_id)
                    pmgr.add_video_to_playlist(per_video_pl_id, short_yt_id)
                    result["per_video_playlist_added"] = True
                    logger.info(
                        "[%s] Per-video playlist '%s': added source %s + short %s",
                        channel_slug, per_video_name, source_yt_id, short_yt_id,
                    )
        except Exception as exc:
            msg = f"Per-video playlist failed: {exc}"
            result["errors"].append(msg)
            logger.warning("[%s] %s", channel_slug, msg)

    # ── 4. First comment with long-form link ────────────────────
    #    Skip when quota is tight (>70%) to save 50 units per short.
    if not quota_tight and (source_yt_id or first_comment_text):
        try:
            from pipeline.youtube_comments import YouTubeCommentManager

            cm = YouTubeCommentManager(channel_slug)
            if cm.authenticate():
                text = first_comment_text
                if not text and source_yt_id:
                    short_url = _to_youtu_be(
                        f"https://www.youtube.com/watch?v={source_yt_id}"
                    )
                    text = (
                        f"🎬 ¿Quieres ver la historia completa? "
                        f"Mírala aquí {short_url}"
                        f"  (copia y pega en tu navegador)"
                    )
                if text:
                    cm.post_comment(short_yt_id, text)
                    result["comment_posted"] = True
                    logger.info(
                        "[%s] First comment posted on short %s",
                        channel_slug, short_yt_id,
                    )
        except Exception as exc:
            msg = f"First comment failed: {exc}"
            result["errors"].append(msg)
            logger.warning("[%s] %s", channel_slug, msg)

    return result


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _ensure_playlist(pm, playlist_name: str) -> str | None:
    """Get or create a playlist by name. Returns yt_playlist_id or None."""
    try:
        existing = pm.find_playlist_by_title(playlist_name)
        if existing:
            return existing["yt_playlist_id"]

        created = pm.create_playlist(
            title=playlist_name,
            description=f"Shorts del canal",
            privacy="public",
        )
        return created.get("yt_playlist_id")
    except Exception as exc:
        logger.error("Cannot ensure playlist '%s': %s", playlist_name, exc)
        return None


def should_cross_promote(channel_config) -> bool:
    """Check if cross-promotion is enabled for this channel config."""
    return getattr(channel_config, "SHORTS_LONGFORM_LINK_ENABLED", True)


def should_auto_comment(channel_config) -> bool:
    """Check if auto-comment on shorts is enabled."""
    return getattr(channel_config, "SHORTS_FIRST_COMMENT_LINK", True)
