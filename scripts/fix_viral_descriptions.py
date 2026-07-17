#!/usr/bin/env python3
"""Regenerate and push corrected Spanish descriptions for viral-mode videos
whose current description is a copy of the original English description
(containing promo links, Discord invites, Patreon URLs, etc.).

Workflow:
  1. Detect affected videos (description matches original or contains promo leaks)
  2. Regenerate a Spanish description via LLM using the video title + script
  3. Sanitize the result (strip any residual URLs/handles/promos)
  4. Update the local DB (videos.description)
  5. Push the corrected description to YouTube via API (videos().update)

Usage:
    python3 scripts/fix_viral_descriptions.py --dry-run     # preview only
    python3 scripts/fix_viral_descriptions.py --execute     # regenerate + push

Quota cost: ~50 units per video push.
"""

import argparse
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fix_viral_descriptions")

# ── Leak indicators (same as viral_cloner.py) ────────────────────
_LEAK_PATTERNS = [
    (r'https?://[^\s]+', 'URL'),
    (r'discord\.gg/', 'Discord invite'),
    (r'(?:patreon|paypal|buymeacoffee)\.', 'payment link'),
    (r'bit\.ly/', 'short link'),
    (r'@[a-zA-Z0-9_]{3,}', '@handle'),
    (r'(?:subscribe|suscribete|suscríbete)', 'subscribe CTA', re.IGNORECASE),
    (r'(?:join|follow|find)\s+(?:me|us|my|our)\s+(?:on|at)', 'follow CTA', re.IGNORECASE),
    (r'(?:activate|hit|press)\s+(?:the\s+)?(?:bell|notification)', 'bell CTA', re.IGNORECASE),
    (r'(?:business|contact)\s*(?:inquir|mail|email)', 'business contact', re.IGNORECASE),
    (r'(?:directed by|produced by)\s+@', 'directed by @', re.IGNORECASE),
]

_LEAK_LINE_PATTERNS = [
    re.compile(r'^.*(?:subscribe|suscribete|suscríbete).*(?:$|\.|\!|\?)', re.IGNORECASE),
    re.compile(r'^.*(?:join.+discord|discord.+join).*$', re.IGNORECASE),
    re.compile(r'^.*(?:follow|follow me|sígueme|sigueme).*(?:instagram|twitter|facebook|tiktok|social).*$', re.IGNORECASE),
    re.compile(r'^.*(?:support|patreon|paypal|donate).*$', re.IGNORECASE),
    re.compile(r'^.*(?:activate|hit|press).*(?:bell|notification).*$', re.IGNORECASE),
    re.compile(r'^.*(?:business|contact)\s*(?:inquir|mail|email).*$', re.IGNORECASE),
    re.compile(r'^.*(?:check out|watch)\s+(?:my|our|the)\s+(?:channel|video|other).*$', re.IGNORECASE),
    re.compile(r'^.*(?:like|share|comment).*(?:below|this video).*$', re.IGNORECASE),
    re.compile(r'^.*(?:directed by|produced by|edited by)\s+@.*$', re.IGNORECASE),
    re.compile(r'^.*@[a-zA-Z0-9_]{3,}.*$', re.IGNORECASE),
]


def is_description_leaked(desc: str) -> bool:
    """Check if a description contains promo content leaks."""
    if not desc:
        return False
    if desc.startswith("Join my Discord") or desc.startswith("WATCH AD-FREE"):
        return True
    if len(desc) < 30:
        return False
    for pattern_data in _LEAK_PATTERNS:
        if isinstance(pattern_data, tuple):
            pattern, label, *rest = pattern_data
            flags = rest[0] if rest else 0
            if re.search(pattern, desc, flags=flags):
                logger.debug("  Leak: %s", label)
                return True
    return False


def sanitize_promo_content(text: str) -> str:
    """Remove promotional leaks from a description."""
    if not text or len(text) < 10:
        return text
    lines = text.split("\n")
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            kept.append("")
            continue
        is_leak = any(p.match(stripped) for p in _LEAK_LINE_PATTERNS)
        if not is_leak:
            kept.append(stripped)
    cleaned = "\n".join(kept)
    cleaned = re.sub(r'https?://[^\s\)\]>]+', '', cleaned)
    cleaned = re.sub(r'[\w.-]+@[\w.-]+\.\w+', '', cleaned)
    cleaned = re.sub(r'@[a-zA-Z0-9_]{3,}', '', cleaned)
    cleaned = re.sub(r'(?:discord|patreon|paypal|buymeacoffee|bit\.ly)[^\s]*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    cleaned = re.sub(r' +\n', '\n', cleaned)
    cleaned = re.sub(r'\n +', '\n', cleaned)
    cleaned = re.sub(r'  +', ' ', cleaned)
    stripped = cleaned.strip()
    if len(stripped) < 30:
        return ""
    return stripped


def get_llm_client():
    """Get an OpenAI-compatible client for description generation."""
    try:
        import openai
        from config.config_bridge import get_channel_config
        cfg = get_channel_config("canal2")
        api_key = getattr(cfg, "LLM_API_KEY", None) or getattr(cfg, "API_KEY", None)
        base_url = getattr(cfg, "LLM_BASE_URL", "https://api.deepseek.com")
        model = getattr(cfg, "LLM_MODEL", "deepseek-chat")
        if not api_key:
            logger.error("No LLM API key found in config")
            return None, None
        client = openai.OpenAI(api_key=api_key, base_url=base_url)
        return client, model
    except Exception as e:
        logger.error("Failed to init LLM client: %s", e)
        return None, None


def generate_description(title: str, script_text: str, channel_slug: str) -> str:
    """Generate a fresh Spanish description via LLM."""
    client, model = get_llm_client()
    if not client:
        return ""

    system = """You are a YouTube SEO expert. Write an original Spanish video description.

CRITICAL: Do NOT include URLs, @handles, Discord, Patreon, "subscribe", "follow me",
"activate the bell", or ANY external platform calls. This must be 100% original text.

Write:
1. A 1-2 sentence hook about the video topic.
2. A 3-5 sentence summary of what the viewer will learn.
3. 3-5 topic keywords or phrases (no hashtags).
4. A generic closing note (e.g., "Basado en investigación y documentación histórica.").

400-800 characters total. Short paragraphs. Output ONLY the description."""

    user = (
        f"Write a Spanish YouTube description for a video titled:\n"
        f"\"{title}\"\n\n"
        f"The video script discusses these topics:\n{script_text[:3000]}\n\n"
        f"Channel context: {channel_slug}"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.6,
            max_tokens=500,
        )
        result = response.choices[0].message.content
        if result:
            return result.strip()
    except Exception as e:
        logger.error("LLM generation failed: %s", e)
    return ""


def update_youtube_description(yt_video_id: str, desc: str, channel_slug: str) -> bool:
    """Push a new description to YouTube via the videos().update API."""
    try:
        from config.config_bridge import get_channel_config
        from pipeline.youtube_uploader import YouTubeUploader

        cfg = get_channel_config(channel_slug)
        uploader = YouTubeUploader(cfg)
        if not uploader.authenticate():
            logger.warning("[%s] Auth failed — skipping YT push for %s", channel_slug, yt_video_id)
            return False

        # Get current snippet to preserve title/category
        service = uploader._get_service()
        current = service.videos().list(
            part="snippet",
            id=yt_video_id,
        ).execute()

        if not current.get("items"):
            logger.warning("[%s] Video %s not found on YouTube", channel_slug, yt_video_id)
            return False

        snippet = current["items"][0]["snippet"]
        body = {
            "id": yt_video_id,
            "snippet": {
                "title": snippet["title"],
                "categoryId": snippet["categoryId"],
                "description": desc[:5000],
            },
        }
        service.videos().update(part="snippet", body=body).execute()
        logger.info("[%s] ✅ YouTube description updated for %s", channel_slug, yt_video_id)
        return True
    except Exception as e:
        logger.error("[%s] YouTube update failed for %s: %s", channel_slug, yt_video_id, e)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Fix viral video descriptions leaked from original English source"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview only, no changes")
    group.add_argument("--execute", action="store_true", help="Regenerate + update DB + push to YT")
    parser.add_argument("--skip-youtube", action="store_true", help="Skip YouTube API push (DB only)")
    args = parser.parse_args()

    db_path = PROJECT_ROOT / "autotube.db"
    if not db_path.exists():
        logger.error("Database not found: %s", db_path)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # ── Find affected videos ──────────────────────────────────
    # Videos in viral mode whose description matches the original English
    # or contains promo leak indicators.
    affected_rows = conn.execute("""
        SELECT v.id, v.canal, v.titulo_final, v.description, v.yt_video_id,
               v.status, v.source_mode, s.guion as script_text,
               rc.viral_original_description,
               s.id as script_id, v.script_id
        FROM videos v
        JOIN scripts s ON s.id = v.script_id
        JOIN raw_content rc ON rc.id = s.raw_content_id
        WHERE v.source_mode = 'viral'
          AND v.description IS NOT NULL
          AND s.guion IS NOT NULL
        ORDER BY v.id
    """).fetchall()

    affected = []
    for row in affected_rows:
        vid = dict(row)
        desc = vid.get("description", "")
        original = vid.get("viral_original_description", "")

        # Check if description = original (copy) or contains leaks
        is_copy = (original and desc and desc[:40] == original[:40])
        is_leaked = is_copy or is_description_leaked(desc)

        if is_leaked:
            affected.append(vid)

    if not affected:
        print("No affected videos found.")
        return

    print(f"\nFound {len(affected)} affected videos:\n")
    for i, vid in enumerate(affected):
        desc_preview = (vid["description"] or "")[:80].replace("\n", " ")
        print(f"  {i+1}. video #{vid['id']} [{vid['canal']}] {vid['status']}")
        print(f"     Title: {vid['titulo_final'][:70]}")
        print(f"     Desc:  {desc_preview}...")
        print(f"     YT ID: {vid.get('yt_video_id', 'N/A')}")
        print()

    if args.dry_run:
        print("── DRY RUN — no changes made. Use --execute to apply. ──")
        return

    # ── Execute: regenerate descriptions ──────────────────────
    print("═ Regenerating descriptions ═\n")
    fixed_count = 0
    yt_pushed_count = 0

    for vid in affected:
        vid_id = vid["id"]
        canal = vid["canal"]
        title = vid["titulo_final"] or "Video sin título"
        script_text = vid["script_text"] or ""
        yt_video_id = vid.get("yt_video_id", "")

        print(f"  [{canal}] video #{vid_id} — generating...")

        # Generate fresh description
        new_desc = generate_description(title, script_text, canal)
        if not new_desc:
            logger.warning("  ✗ Generation failed — skipping")
            continue

        # Sanitize
        new_desc = sanitize_promo_content(new_desc)
        if not new_desc:
            logger.warning("  ✗ Sanitization produced empty result — skipping")
            continue

        # Update DB
        conn.execute("UPDATE videos SET description = ? WHERE id = ?", (new_desc, vid_id))
        logger.info("  ✓ DB updated (%d chars)", len(new_desc))

        # Push to YouTube (if applicable)
        if yt_video_id and not args.skip_youtube:
            ok = update_youtube_description(yt_video_id, new_desc, canal)
            if ok:
                yt_pushed_count += 1

        fixed_count += 1
        print()

    conn.commit()
    conn.close()

    print(f"═ Done ═")
    print(f"  DB updates:   {fixed_count}")
    print(f"  YT pushes:    {yt_pushed_count}")
    print(f"  YT skipped:   {fixed_count - yt_pushed_count}")


if __name__ == "__main__":
    main()
