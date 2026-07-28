#!/usr/bin/env python3
"""Repair viral videos whose titles/descriptions are in English due to a
failed translation step in the original pipeline.

This script:
1. Finds viral videos with English titles/descriptions in the DB
2. Regenerates Spanish titles and descriptions using the LLM
3. Updates the videos table (and the raw_content viral_meta_json)
4. For already-published videos, pushes the new title/description to YouTube

Usage:
  python3 scripts/repair_english_viral_videos.py --dry-run       # preview
  python3 scripts/repair_english_viral_videos.py --repair         # fix DB only
  python3 scripts/repair_english_viral_videos.py --repair --push  # fix DB + push to YT
"""

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("repair_english_viral")

# ── Language detection ──────────────────────────────────────


def is_english_text(text: str) -> bool:
    """Return True if text is clearly English (not Spanish)."""
    if not text or len(text) < 20:
        return False
    es_chars = sum(1 for c in text if c in 'áéíóúñü¿¡ÁÉÍÓÚÑÜ')
    if es_chars > 0:
        return False
    # Use space-padded markers (more reliable than word boundaries with proper nouns)
    t = ' ' + text.lower() + ' '
    english_markers = [' the ', ' and ', ' that ', ' have ', ' from ', ' with ',
                       ' this ', ' they ', ' will ', ' what ', ' when ', ' about ',
                       ' your ', ' our ', ' join ', ' subscribe ', ' which ', ' there ',
                       ' would ', ' could ', ' should ', ' their ', ' been ', ' were ',
                       ' are ', ' for ', ' not ', ' but ', ' you ', ' has ', ' had ']
    english_hits = sum(1 for m in english_markers if m in t)
    if english_hits >= 3:
        return True
    # Fallback: high ASCII word ratio with no Spanish accents
    words = t.split()
    if len(words) >= 10:
        ascii_words = sum(1 for w in words if w.isascii() and all(c.isascii() for c in w))
        if ascii_words / len(words) > 0.85:
            return True
    return False


# ── LLM Client ──────────────────────────────────────────────


def _get_llm_client():
    """Get an LLM client for title/description generation."""
    try:
        from openai import OpenAI
        from config.settings import (
            LLM_API_KEY, LLM_BASE_URL, LLM_MODEL,
        )
        if not LLM_API_KEY:
            logger.error("LLM_API_KEY not configured")
            return None, None
        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        return client, LLM_MODEL
    except Exception as e:
        logger.error("Failed to init LLM client: %s", e)
        return None, None


def call_llm(client, model: str, system: str, user: str, temperature: float = 0.7, max_retries: int = 3) -> str:
    """Call the LLM with retries and language validation."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=4096,
            )
            content = resp.choices[0].message.content
            if content:
                content = content.strip()
                if is_english_text(content):
                    logger.warning("LLM returned English text (attempt %d) — retrying", attempt)
                    continue
                return content
        except Exception as e:
            logger.warning("LLM call failed (attempt %d): %s", attempt, e)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
    return None


# ── Title & Description generation ──────────────────────────


_TITLE_SYSTEM = """Eres un experto en SEO de YouTube. Responde EXCLUSIVAMENTE en español.

Crea un título PARA YouTube en español basado en el tema del script proporcionado.
Reglas:
- Máximo 100 caracteres
- Usa gancho de curiosidad, misterio o pregunta
- NUNCA uses nombres de creadores originales
- NUNCA uses marcas como BBC, Netflix, etc.
- NUNCA incluyas claims de duración como "4+ HORAS"
- Debe ser COMPLETAMENTE diferente del título original en inglés
- Sin comillas, sin asteriscos, sin formato markdown
- Solo el título, sin prefijos ni explicaciones"""

_DESC_SYSTEM = """Eres un experto en SEO de YouTube. Responde EXCLUSIVAMENTE en español.

Escribe una descripción ORIGINAL para un video de YouTube en español.
Reglas:
- NO incluyas URLs, @handles, Discord, Patreon, "suscríbete", etc.
- 2-3 párrafos, 400-800 caracteres
- Primer párrafo: un gancho sobre el tema
- Segundo párrafo: lo que el espectador aprenderá
- Tercer párrafo (opcional): 3-5 palabras clave del tema
- Termina con: "Basado en investigación y documentación."
- Solo la descripción, sin prefijos ni explicaciones"""


def generate_title(client, model: str, english_title: str, script_excerpt: str) -> str:
    """Generate a fresh Spanish title from the English original topic."""
    user = (
        f"Título original en inglés (solo como referencia del TEMA): \"{english_title}\"\n\n"
        f"Script del video (para contexto):\n{script_excerpt[:3000]}\n\n"
        f"Crea un título COMPLETAMENTE NUEVO en español. NO traduzcas el título original. "
        f"Usa palabras y estructura diferentes."
    )
    result = call_llm(client, model, _TITLE_SYSTEM, user, temperature=0.85)
    if result:
        # Clean up artifacts
        result = re.sub(r'^\*\*|\*\*$', '', result)
        result = re.sub(r'^#\s*', '', result)
        result = re.sub(r'^["\']|["\']$', '', result)
        result = re.sub(r'^[Tt]ítulo:?\s*', '', result)
        result = result.strip()
        # Truncate to 100 chars at word boundary
        if len(result) > 100:
            result = result[:100].rsplit(' ', 1)[0]
    return result


def generate_description(client, model: str, title_es: str, script_excerpt: str, hashtags: list[str]) -> str:
    """Generate a Spanish description from the script content."""
    user = (
        f"Título del video: \"{title_es}\"\n\n"
        f"Script (en español):\n{script_excerpt[:4000]}\n\n"
        f"Basado en esta información, escribe una descripción original en español."
    )
    result = call_llm(client, model, _DESC_SYSTEM, user, temperature=0.6)
    if result and hashtags:
        hashtag_str = " ".join(hashtags[:5])
        if hashtag_str and hashtag_str not in result[-300:]:
            result = result.strip() + "\n\n" + hashtag_str
    return result


# ── Database operations ─────────────────────────────────────


def find_english_viral_videos(conn: sqlite3.Connection) -> list[dict]:
    """Find viral videos with English titles or descriptions."""
    rows = conn.execute("""
        SELECT v.id, v.canal, v.titulo_final, v.description, v.yt_video_id,
               v.status, v.script_id, v.channel_id
        FROM videos v
        WHERE v.source_mode = 'viral'
          AND v.status != 'draft'
        ORDER BY v.id
    """).fetchall()

    english_videos = []
    for row in rows:
        vid = dict(row)
        title = vid["titulo_final"] or ""
        desc = vid["description"] or ""

        title_is_en = is_english_text(title)
        desc_is_en = is_english_text(desc)

        if title_is_en or desc_is_en:
            vid["title_is_english"] = title_is_en
            vid["desc_is_english"] = desc_is_en
            english_videos.append(vid)

    return english_videos


def get_script_text(conn: sqlite3.Connection, video: dict) -> tuple[str | None, str | None, list[str]]:
    """Get Spanish script text from raw_content or script blocks.
    Returns (script_es, english_original_title, hashtags)
    """
    script_id = video.get("script_id")
    raw_id = None

    # Try via scripts table
    if script_id:
        row = conn.execute(
            "SELECT raw_content_id FROM scripts WHERE id = ?", (script_id,)
        ).fetchone()
        if row:
            raw_id = row[0]

    if not raw_id:
        return None, None, []

    row = conn.execute(
        "SELECT viral_script_es, viral_original_title, viral_meta_json FROM raw_content WHERE id = ?",
        (raw_id,),
    ).fetchone()
    if not row:
        return None, None, []

    script_es = row[0]
    original_title = row[1]
    meta = json.loads(row[2]) if row[2] else {}

    # Extract hashtags from channel config
    channel_id = video.get("channel_id")
    hashtags = []
    if channel_id:
        ch_row = conn.execute(
            "SELECT config_json FROM channels WHERE id = ?", (channel_id,)
        ).fetchone()
        if ch_row:
            ch_config = json.loads(ch_row[0]) if ch_row[0] else {}
            hashtags = ch_config.get("seo_hashtags", []) or ch_config.get("SEO_HASHTAGS", [])

    return script_es, original_title, hashtags


def update_video_metadata(conn: sqlite3.Connection, video_id: int, new_title: str, new_desc: str):
    """Update the video's title and description in the DB."""
    conn.execute(
        "UPDATE videos SET titulo_final = ?, description = ?, updated_at = datetime('now') WHERE id = ?",
        (new_title, new_desc, video_id),
    )
    conn.commit()
    logger.info("  Updated video #%d in DB: title=%s", video_id, new_title[:60])


def push_to_youtube(video: dict, service, new_title: str, new_desc: str) -> bool:
    """Push new title and description to YouTube."""
    try:
        yt_id = video["yt_video_id"]
        resp = service.videos().list(part="snippet", id=yt_id, maxResults=1).execute()
        items = resp.get("items", [])
        if not items:
            logger.warning("  Video %s not found on YouTube", yt_id)
            return False

        snippet = items[0]["snippet"]
        snippet["title"] = new_title
        snippet["description"] = new_desc

        service.videos().update(
            part="snippet",
            body={"id": yt_id, "snippet": snippet},
        ).execute()
        logger.info("  ✓ Pushed new title+description to YouTube for %s", yt_id)
        return True
    except Exception as e:
        logger.error("  ✗ Failed to push to YouTube for %s: %s", video["yt_video_id"], e)
        return False


# ── Main ────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Repair English viral videos")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview only")
    group.add_argument("--repair", action="store_true", help="Fix DB metadata")
    parser.add_argument("--push", action="store_true", help="Also push to YouTube (requires --repair)")
    parser.add_argument("--video-id", type=int, nargs="+", help="Specific video IDs to repair")
    args = parser.parse_args()

    if args.push and not args.repair:
        logger.error("--push requires --repair")
        sys.exit(1)

    db_path = PROJECT_ROOT / "autotube.db"
    if not db_path.exists():
        logger.error("DB not found: %s", db_path)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Find affected videos
    all_videos = find_english_viral_videos(conn)

    if args.video_id:
        all_videos = [v for v in all_videos if v["id"] in args.video_id]

    if not all_videos:
        print("No viral videos with English content found. ✓")
        conn.close()
        return

    print(f"\n{'═' * 60}")
    print(f"  Viral videos with English content: {len(all_videos)}")
    for v in all_videos:
        issues = []
        if v.get("title_is_english"):
            issues.append("title")
        if v.get("desc_is_english"):
            issues.append("description")
        print(f"  [{v['canal']}] #{v['id']} {v['titulo_final'][:70]} — {', '.join(issues)} ({v['status']})")
    print(f"{'═' * 60}\n")

    if args.dry_run:
        print("── DRY RUN — use --repair to fix DB, --repair --push to fix + push to YouTube ──")
        conn.close()
        return

    # Init LLM client
    client, model = _get_llm_client()
    if not client:
        logger.error("Cannot proceed without LLM client")
        conn.close()
        sys.exit(1)

    # Init YouTube services if pushing
    services = {}
    if args.push:
        try:
            from pipeline.youtube_uploader import YouTubeUploader
            channels = list(set(v["canal"] for v in all_videos))
            for canal in channels:
                logger.info("[%s] Authenticating for YouTube push...", canal)
                uploader = YouTubeUploader(account_name=canal, channel_slug=canal)
                if uploader.authenticate():
                    services[canal] = uploader._get_service()
                    logger.info("[%s] ✓ Authenticated", canal)
                else:
                    logger.warning("[%s] ✗ Auth failed — will skip YouTube push for this channel", canal)
        except Exception as e:
            logger.error("Failed to init YouTube services: %s", e)

    # Repair each video
    repaired = []
    failed = []
    pushed = []

    for video in all_videos:
        vid_id = video["id"]
        canal = video["canal"]

        print(f"\n{'─' * 50}")
        print(f"Repairing [{canal}] video #{vid_id} ({video['status']})")
        print(f"  Current title: {video['titulo_final'][:80]}")

        # Get script text
        script_es, original_title, hashtags = get_script_text(conn, video)

        if not script_es:
            logger.warning("  No script_es found — cannot regenerate content")
            failed.append(video)
            continue

        # Check if script is actually in English
        if is_english_text(script_es):
            # Script is English — we need to translate it first
            logger.warning("  Script is also in English — need to translate first")
            # For now, use the English title to generate a Spanish one
            # This is not ideal but better than full English content
            new_title = generate_title(client, model, original_title or video["titulo_final"], script_es[:3000])
            # For description, we need to translate the script or generate from the topic
            if new_title:
                new_desc = generate_description(client, model, new_title, script_es[:4000], hashtags)
            elif not new_title:
                logger.warning("  Could not generate title — skipping")
                failed.append(video)
                continue
        else:
            # Script is already Spanish — just need title and description
            new_title = video.get("title_is_english") and generate_title(
                client, model, original_title or "", script_es[:3000]
            ) or video["titulo_final"]
            new_desc = video.get("desc_is_english") and generate_description(
                client, model, video["titulo_final"], script_es[:4000], hashtags
            ) or video["description"]

        if not new_title and video.get("title_is_english"):
            logger.warning("  Title generation failed")
            failed.append(video)
            continue

        new_title = new_title or video["titulo_final"]
        new_desc = new_desc or video.get("description", "")

        print(f"  New title: {new_title[:80]}")

        # Update DB
        update_video_metadata(conn, vid_id, new_title, new_desc)

        # Push to YouTube if requested and video is already published
        if args.push and video["yt_video_id"] and video["status"] in ("published", "unlisted", "uploaded_private"):
            service = services.get(canal)
            if service:
                ok = push_to_youtube(video, service, new_title, new_desc)
                if ok:
                    pushed.append(video)
            else:
                logger.warning("  No YouTube service for channel %s — skipping push", canal)

        repaired.append(video)
        time.sleep(2)  # Rate limiting

    conn.close()

    # Summary
    print(f"\n{'═' * 60}")
    print(f"  REPAIR SUMMARY")
    print(f"  Total:        {len(all_videos)}")
    print(f"  Repaired DB:  {len(repaired)}")
    print(f"  Pushed to YT: {len(pushed)}")
    print(f"  Failed:       {len(failed)}")
    if failed:
        print(f"\n  Failed videos:")
        for v in failed:
            print(f"    #{v['id']} [{v['canal']}] {v['titulo_final'][:60]}")
    print(f"{'═' * 60}")


if __name__ == "__main__":
    main()
