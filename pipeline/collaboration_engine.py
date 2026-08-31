"""Collaboration engine — find similar niche channels and leave value comments.

v2 (ago 2026, Fase cuota): el descubrimiento de canales y vídeos usa el
NAVEGADOR (web UI de YouTube, 0 unidades de Data API). La versión v1 usaba
search().list() (100 ud/call × 8 keywords × canal) y agotaba el presupuesto
del proyecto compartido justo en la ventana crítica de las 3-4 AM.

Los comentarios se publican vía Data API (commentThreads.insert, 50 ud) solo
si el proyecto GCP del canal tiene >= COLLAB_MIN_FREE_PCT% de cuota libre.
Flag global: COLLAB_ENABLED (default false).

Strategy:
  1. Search canales del nicho vía navegador, filtrar por suscriptores
  2. Obtener los vídeos recientes del candidato vía navegador
  3. LLM genera un comentario genuino (pregunta, observación o dato)
  4. Publicar vía Data API (50 ud) con gate de presupuesto
  5. Max 3-5 comentarios/canal/día para evitar flags de spam

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
MAX_SEARCH_KEYWORDS = 5                     # Keywords per discovery round (v2: reducido)
COMMENT_MAX_LENGTH = 300                    # Max comment length (chars)


def _collab_enabled() -> bool:
    """Flag global del engine (COLLAB_ENABLED, default false)."""
    try:
        from config.settings import COLLAB_ENABLED
        return bool(COLLAB_ENABLED)
    except Exception:
        return False


def _project_has_free_capacity(channel_slug: str) -> bool:
    """True si el proyecto del canal tiene >= COLLAB_MIN_FREE_PCT% libre."""
    try:
        from config.settings import COLLAB_MIN_FREE_PCT
        from api.services.quota_tracker import (
            get_channel_project, project_has_free_capacity,
        )
        return project_has_free_capacity(
            get_channel_project(channel_slug), COLLAB_MIN_FREE_PCT
        )
    except Exception:
        return False


def _get_browser_for_channel(channel_slug: str):
    """Instancia de navegador para el account del canal (0 cuota API).

    Para canales gestionados por agente egress devuelve un adaptador que
    delega search/get_channel_videos al agente (la IP de salida es la del
    agente, no la del server).
    """
    from pipeline.youtube_browser import get_account_for_channel
    account = get_account_for_channel(channel_slug)
    if not account:
        return None
    from api.services.egress_delegation import egress_client_for
    _egress = egress_client_for(channel_slug)
    if _egress is not None:
        return _EgressBrowserAdapter(_egress, account)
    from pipeline.youtube_browser import get_browser
    return get_browser(account)


class _EgressBrowserAdapter:
    """Adaptador que delega operaciones de navegador al agente egress."""

    def __init__(self, egress, account: str):
        self._egress = egress
        self._account = account

    def search_channels(self, keyword: str, max_results: int = 5) -> list[dict]:
        _r = self._egress.browser_action(
            "collab_search", account=self._account,
            params={"keyword": keyword, "max_results": max_results},
        )
        return _r.get("result", []) if _r.get("ok") else []

    def get_channel_videos(self, channel_url: str, limit: int = 3) -> list[dict]:
        _r = self._egress.browser_action(
            "collab_channel_videos", account=self._account,
            params={"channel_url": channel_url, "limit": limit},
        )
        return _r.get("result", []) if _r.get("ok") else []


def discover_niche_channels(
    channel_slug: str,
    max_subs: int = MAX_TARGET_CHANNEL_SUBS,
    max_candidates: int = MAX_SEARCH_CANDIDATES,
) -> list[dict]:
    """Discover small YouTube channels in the same niche.

    v2: usa la web UI de YouTube vía navegador (0 cuota Data API).
    Los sub-counts se parsean del texto del renderer ("12,3 mil suscriptores").

    Returns list of {"channel_url": str, "title": str, "subs": int|None}
    """
    from config.config_bridge import get_channel_config

    ch_config = get_channel_config(channel_slug)
    keywords = getattr(ch_config, "CHANNEL_KEYWORDS", [])
    if not keywords:
        logger.warning("[collab] No keywords for %s", channel_slug)
        return []

    browser = _get_browser_for_channel(channel_slug)
    if browser is None:
        logger.warning("[collab] No browser account for %s", channel_slug)
        return []

    candidates = []
    seen = set()

    random.shuffle(keywords)
    for kw in keywords[:MAX_SEARCH_KEYWORDS]:
        if len(candidates) >= max_candidates:
            break
        try:
            results = browser.search_channels(
                f"{kw} documental español", max_results=5
            )
            for item in results:
                url = item.get("url", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                subs = item.get("subs")
                # Skip channels que claramente superan el tope
                if subs is not None and (subs > max_subs or subs < 1):
                    continue
                candidates.append({
                    "channel_url": url,
                    "title": item.get("name", url.strip("/@")),
                    "subs": subs,
                })
                if len(candidates) >= max_candidates:
                    break
        except Exception as e:
            logger.debug("[collab] Browser search error for kw='%s': %s", kw, e)
            continue

    logger.info("[collab] %s: found %d candidate channels (browser, 0 quota)",
                channel_slug, len(candidates))
    return candidates[:max_candidates]


def get_candidate_videos(
    channel_slug: str,
    candidate_channel_url: str,
    limit: int = MIN_RECENT_VIDEOS,
) -> list[dict]:
    """Get the most recent videos from a candidate channel (browser, 0 quota)."""
    browser = _get_browser_for_channel(channel_slug)
    if browser is None:
        return []

    try:
        raw = browser.get_channel_videos(candidate_channel_url, limit=limit)
        videos = []
        for item in raw:
            video_id = (item.get("video_url") or "").replace("/watch?v=", "")
            video_id = video_id.split("&")[0]
            if video_id:
                videos.append({
                    "video_id": video_id,
                    "title": item.get("title", ""),
                    "description": "",
                    "channel_title": "",
                })
        return videos
    except Exception as e:
        logger.debug("[collab] Browser error getting videos for %s: %s",
                     candidate_channel_url, e)
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
    from config.llm_client import create_llm_client
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
    """Post a comment on a YouTube video via Data API (50 ud, tracked).

    Gate: solo publica si el proyecto del canal tiene >= COLLAB_MIN_FREE_PCT%
    de cuota libre. Un comentario jamás debe tumbar el presupuesto de subidas.
    """
    if not _project_has_free_capacity(channel_slug):
        logger.debug("[collab] %s: project quota too tight — comment skipped", channel_slug)
        return False

    try:
        from pipeline.youtube_comments import YouTubeCommentManager
        mgr = YouTubeCommentManager(channel_slug)
        result = mgr.post_comment(video_id, comment_text)
        return bool(result and result.get("yt_comment_id"))
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

    result = {"candidates": 0, "videos_checked": 0, "comments_posted": 0, "errors": 0}

    # ── Gate de presupuesto ANTES de gastar nada ──────────────
    if not _project_has_free_capacity(channel_slug):
        logger.debug("[collab] %s: project quota too tight — round skipped", channel_slug)
        return result

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

    # Discover candidate channels (browser — 0 quota)
    candidates = discover_niche_channels(channel_slug)
    result["candidates"] = len(candidates)

    for candidate in candidates:
        if result["comments_posted"] >= remaining:
            break

        videos = get_candidate_videos(channel_slug, candidate.get("channel_url", ""))
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
                            channel_slug, candidate.get("title", "")[:30],
                            comment[:80])
            else:
                result["errors"] += 1

    return result


def run_all_channels_collab() -> dict:
    """Run collaboration rounds for all active channels.

    Gate global COLLAB_ENABLED (default false). Con true, cada ronda valida
    su propio presupuesto de proyecto antes de gastar.

    Returns: {"channels_processed": N, "total_comments": N, "total_errors": N}
    """
    if not _collab_enabled():
        return {"channels_processed": 0, "total_comments": 0, "total_errors": 0,
                "disabled": True}

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
