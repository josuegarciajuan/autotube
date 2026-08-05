"""Collaboration engine — find similar niche channels and leave value comments.

Runs daily, discovers small channels (<5K subs) in the same niche via YouTube
Search API, and posts genuine, non-spam comments on their recent videos. This
builds channel authority and attracts curious viewers naturally.

Strategy:
  1. Search for channels with niche keywords, filter by subscriber count
  2. Get each candidate's 3 most recent videos
  3. LLM generates a genuine comment (question, observation, or data point)
  4. Post via YouTube Data API commentThreads().insert()
  5. Max 3-5 comments/channel/day to avoid spam flags

The engine NEVER mentions our channel, NEVER asks for subs, and NEVER uses
spam patterns. Comments are designed to add value to the video's discussion.
"""

import logging
import random
import time
from typing import Optional

logger = logging.getLogger("autotube.collab")


# ── Tuning ────────────────────────────────────────────────
MAX_COMMENTS_PER_CHANNEL_PER_DAY = 3       # Avoid spam flags
MAX_TARGET_CHANNEL_SUBS = 5000              # Only target small channels
MIN_RECENT_VIDEOS = 3                       # How many videos to check per candidate
MAX_SEARCH_CANDIDATES = 10                  # Max channels to discover per run
COMMENT_MAX_LENGTH = 300                    # Max comment length (chars)


def discover_niche_channels(
    channel_slug: str,
    max_subs: int = MAX_TARGET_CHANNEL_SUBS,
    max_candidates: int = MAX_SEARCH_CANDIDATES,
) -> list[dict]:
    """Discover small YouTube channels in the same niche.

    Uses YouTube Data API search with channel keywords to find channels
    with similar content and small subscriber counts.

    Returns list of {"channel_id": str, "title": str, "subs": int, "description": str}
    """
    from config.config_bridge import get_channel_config
    from pipeline.youtube_uploader import YouTubeUploader

    ch_config = get_channel_config(channel_slug)
    keywords = getattr(ch_config, "CHANNEL_KEYWORDS", [])
    if not keywords:
        logger.warning("[collab] No keywords for %s", channel_slug)
        return []

    uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
    if not uploader.authenticate():
        logger.warning("[collab] Auth failed for %s", channel_slug)
        return []

    youtube = uploader.get_authenticated_service()
    candidates = []
    seen_channels = set()

    # Search with different keyword combinations to find varied results
    random.shuffle(keywords)
    for kw in keywords[:8]:  # Try up to 8 keywords
        if len(candidates) >= max_candidates:
            break

        try:
            search_response = youtube.search().list(
                part="snippet",
                q=f"{kw} documental español",
                type="channel",
                maxResults=5,
                order="relevance",
            ).execute()

            for item in search_response.get("items", []):
                channel_id = item["snippet"]["channelId"]
                if channel_id in seen_channels:
                    continue
                seen_channels.add(channel_id)

                title = item["snippet"]["title"]

                # Get subscriber count
                try:
                    ch_response = youtube.channels().list(
                        part="statistics,snippet",
                        id=channel_id,
                    ).execute()
                    if not ch_response.get("items"):
                        continue

                    ch_info = ch_response["items"][0]
                    subs = int(ch_info["statistics"].get("subscriberCount", 0))

                    # Skip our own channel (by handle or name)
                    our_handle = getattr(ch_config, "YOUTUBE_HANDLE", "")
                    if our_handle and our_handle.lower() in title.lower():
                        continue

                    # Only target small channels (< max_subs)
                    if subs > max_subs or subs < 10:
                        continue

                    candidates.append({
                        "channel_id": channel_id,
                        "title": title,
                        "subs": subs,
                        "description": ch_info["snippet"].get("description", ""),
                    })

                    if len(candidates) >= max_candidates:
                        break

                except Exception:
                    continue

        except Exception as e:
            logger.debug("[collab] Search error for kw='%s': %s", kw, e)
            continue

    logger.info("[collab] %s: found %d candidate channels", channel_slug, len(candidates))
    return candidates[:max_candidates]


def get_candidate_videos(
    channel_slug: str,
    candidate_channel_id: str,
    limit: int = MIN_RECENT_VIDEOS,
) -> list[dict]:
    """Get the most recent videos from a candidate channel."""
    from pipeline.youtube_uploader import YouTubeUploader

    uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
    if not uploader.authenticate():
        return []

    youtube = uploader.get_authenticated_service()

    try:
        # Get upload playlist
        ch_response = youtube.channels().list(
            part="contentDetails",
            id=candidate_channel_id,
        ).execute()
        if not ch_response.get("items"):
            return []

        uploads_playlist = ch_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

        # Get recent videos
        videos = []
        playlist_response = youtube.playlistItems().list(
            part="snippet",
            playlistId=uploads_playlist,
            maxResults=limit,
        ).execute()

        for item in playlist_response.get("items", []):
            videos.append({
                "video_id": item["snippet"]["resourceId"]["videoId"],
                "title": item["snippet"]["title"],
                "description": item["snippet"].get("description", ""),
                "channel_title": item["snippet"]["channelTitle"],
            })

        return videos

    except Exception as e:
        logger.debug("[collab] Error getting videos for channel %s: %s", candidate_channel_id, e)
        return []


def generate_value_comment(
    video_title: str,
    video_description: str,
    channel_niche: str,
    our_channel_name: str = "",
) -> Optional[str]:
    """Generate a genuine, non-spam comment using LLM.

    The comment must:
    - Be specific to the video content (reference something in the title/description)
    - Add value: a question, an observation, or an interesting data point
    - NEVER mention our channel
    - NEVER ask for subscriptions
    - NEVER contain links
    - Sound like a real human wrote it (imperfect Spanish is OK)
    """
    from config.llm_helpers import create_llm_client
    from config.settings import LLM_MODEL

    client = create_llm_client(enable_thinking=False, timeout=30.0, max_retries=1)

    prompt = f"""Eres un espectador real de YouTube que acaba de ver este video:

Título: {video_title[:200]}
Descripción: {video_description[:300]}

El canal trata sobre: {channel_niche}

Escribe UN comentario GENUINO como si fueras un espectador real. Reglas:
1. Debe ser específico al contenido del video (menciona algo concreto del título)
2. Añade valor: una pregunta interesante, una observación, o un dato complementario
3. Entre 40 y 150 caracteres
4. Suena 100% humano, nada de "gran video" o "excelente contenido"
5. NO menciones ningún canal propio
6. NO pidas suscripción ni likes
7. NO incluyas enlaces
8. Escribe en español natural (LATAM o España, imperfecto está bien)

Responde SOLO con el texto del comentario, sin comillas ni markdown."""

    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Escribe el comentario."},
            ],
            temperature=0.9,
            max_tokens=150,
        )
        comment = response.choices[0].message.content.strip()

        # Clean up common LLM artifacts
        if comment.startswith('"') and comment.endswith('"'):
            comment = comment[1:-1]
        comment = comment.replace("```", "").replace("Comentario:", "").strip()

        if len(comment) > COMMENT_MAX_LENGTH:
            comment = comment[:COMMENT_MAX_LENGTH - 3] + "..."

        if len(comment) < 15:
            return None

        return comment

    except Exception as e:
        logger.debug("[collab] Comment generation failed: %s", e)
        return None


def post_youtube_comment(
    channel_slug: str,
    video_id: str,
    comment_text: str,
) -> bool:
    """Post a comment on a YouTube video."""
    from pipeline.youtube_uploader import YouTubeUploader

    uploader = YouTubeUploader(account_name=channel_slug, channel_slug=channel_slug)
    if not uploader.authenticate():
        return False

    youtube = uploader.get_authenticated_service()

    try:
        youtube.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {
                        "snippet": {
                            "textOriginal": comment_text,
                        }
                    },
                }
            },
        ).execute()
        return True
    except Exception as e:
        logger.debug("[collab] Comment post failed: %s", str(e)[:100])
        return False


def run_collaboration_round(channel_slug: str) -> dict:
    """Run one daily collaboration round for a single channel.

    Returns: {"candidates": N, "videos_checked": N, "comments_posted": N, "errors": N}
    """
    from config.config_bridge import get_channel_config
    from database.db_extended import ExtendedDatabase

    db = ExtendedDatabase()
    ch_config = get_channel_config(channel_slug)
    niche_text = ", ".join(getattr(ch_config, "CHANNEL_KEYWORDS", [])[:5])
    our_name = getattr(ch_config, "CANAL_DISPLAY_NAME", channel_slug)

    result = {"candidates": 0, "videos_checked": 0, "comments_posted": 0, "errors": 0}

    # Track today's comments to avoid exceeding limit
    today = time.strftime("%Y-%m-%d")
    conn = __import__("sqlite3").connect(
        str(__import__("config.settings", fromlist=["DATABASE_PATH"]).DATABASE_PATH), timeout=10
    )
    today_count = conn.execute(
        """SELECT COUNT(*) FROM comment_log
           WHERE video_id IN (SELECT yt_video_id FROM videos WHERE canal = ?)
             AND posted_at >= ?""",
        (channel_slug, today),
    ).fetchone()[0]
    conn.close()

    if today_count >= MAX_COMMENTS_PER_CHANNEL_PER_DAY:
        logger.debug("[collab] %s: daily limit reached (%d)", channel_slug, today_count)
        return result

    remaining = MAX_COMMENTS_PER_CHANNEL_PER_DAY - today_count

    # Discover candidate channels
    candidates = discover_niche_channels(channel_slug)
    result["candidates"] = len(candidates)

    for candidate in candidates:
        if result["comments_posted"] >= remaining:
            break

        videos = get_candidate_videos(channel_slug, candidate["channel_id"])
        result["videos_checked"] += len(videos)

        for video in videos:
            if result["comments_posted"] >= remaining:
                break

            # Small delay between comments to avoid rate limiting
            time.sleep(random.uniform(2.0, 5.0))

            comment = generate_value_comment(
                video_title=video["title"],
                video_description=video.get("description", ""),
                channel_niche=niche_text,
            )

            if not comment:
                continue

            success = post_youtube_comment(channel_slug, video["video_id"], comment)
            if success:
                result["comments_posted"] += 1

                logger.info("[collab] %s → %s | '%s'",
                            channel_slug, candidate["title"][:30],
                            comment[:80])
            else:
                result["errors"] += 1

    return result


def run_all_channels_collab() -> dict:
    """Run collaboration rounds for all active channels.

    Returns: {"channels_processed": N, "total_comments": N, "total_errors": N}
    """
    from database.db_extended import ExtendedDatabase

    db = ExtendedDatabase()
    channels = db.get_channels(active_only=True)

    total = {"channels_processed": 0, "total_comments": 0, "total_errors": 0}

    for ch in channels:
        slug = ch["slug"]
        try:
            result = run_collaboration_round(slug)
            total["channels_processed"] += 1
            total["total_comments"] += result["comments_posted"]
            total["total_errors"] += result["errors"]
        except Exception as e:
            logger.warning("[collab] Error for %s: %s", slug, e)
            total["total_errors"] += 1

    if total["total_comments"] > 0:
        logger.info("[collab] Daily round: %d channels, %d comments, %d errors",
                    total["channels_processed"], total["total_comments"], total["total_errors"])

    return total
