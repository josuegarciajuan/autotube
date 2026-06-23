#!/usr/bin/env python3
"""Test video generator — fast iteration for quality verification.

Generates a short test video (1-3 min) from scraped content to verify:
- Image relevance and quality
- Subtitle timing and readability  
- Script redaction quality (tone, narrative, structure)
- Video assembly correctness

Usage:
    # Full test (scrape + script + video)
    python test_video.py

    # With specific topic
    python test_video.py --topic "Milgram"

    # Skip scraping (reuse existing content in DB)
    python test_video.py --skip-scrape

    # Show detailed AI response
    python test_video.py --verbose

    # Switch to production mode (full 8-14 min)
    python test_video.py --prod

Requirements:
    - TEST_MODE must be True in config/canal1_config.py (or set via env)
    - OpenAI/DeepSeek API key must be configured
    - Unsplash/Pexels API keys for images
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import LOG_LEVEL, LOG_FORMAT, LOGS_DIR
from config import canal1_config as cfg

logger = logging.getLogger("test_video")


def setup_logging(verbose: bool = False):
    """Configure logging with verbosity control."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s" if verbose else "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOGS_DIR / "test_video.log", encoding="utf-8"),
        ],
    )
    for lib in ["urllib3", "googleapiclient", "google.auth", "apscheduler", "PIL", "moviepy", "openai", "httpx"]:
        logging.getLogger(lib).setLevel(logging.WARNING)


def report_script_quality(script: dict) -> None:
    """Print a quality summary of the generated script."""
    print("\n" + "=" * 60)
    print("📝 SCRIPT QUALITY REPORT")
    print("=" * 60)

    guion = script.get("guion", "")
    escenas = script.get("escenas", []) or []
    if isinstance(escenas, str):
        try:
            escenas = json.loads(escenas)
        except json.JSONDecodeError:
            escenas = []

    palabras = len(guion.split())
    scene_markers = guion.count("[ESCENA:")
    titulo = script.get("titulo_options", ["?"])
    if isinstance(titulo, str):
        try:
            titulo = json.loads(titulo)
        except json.JSONDecodeError:
            titulo = [titulo]
    titulo = titulo[0] if titulo else "?"

    print(f"  Title:          {titulo}")
    print(f"  Words:          {palabras}")
    print(f"  Scene markers:  {scene_markers}  (declared: {len(escenas)})")
    print(f"  Est. duration:  {script.get('duracion_estimada', '?')} min")
    print(f"  Keywords:       {len(script.get('keywords', []))}")

    # Quick redaction checks
    warnings = []
    if palabras < 100:
        warnings.append("⚠️  Script too short (<100 words)")
    if scene_markers == 0:
        warnings.append("⚠️  No [ESCENA:] markers found — images won't align")
    if scene_markers != len(escenas):
        warnings.append(f"⚠️  Scene marker count ({scene_markers}) ≠ declared scenes ({len(escenas)})")
    if "vosotros" in guion.lower() or "os " in guion.lower():
        warnings.append("⚠️  Iberian Spanish detected (vosotros/os) — should be LATAM neutral")
    if "hola" in guion[:100].lower():
        warnings.append("⚠️  Script starts with greeting instead of hook")

    if warnings:
        print("\n  ⚠️  WARNINGS:")
        for w in warnings:
            print(f"    {w}")
    else:
        print("\n  ✅ No redaction warnings")

    # Show first 200 chars of script
    print(f"\n  Preview: \"{guion[:200]}...\"")

    # SEO description
    seo = script.get("descripcion_seo", "")
    if seo:
        print(f"\n  SEO desc: {seo[:150]}...")


def report_video_quality(video_data: dict, audio_data: dict, media_assets: list, elapsed: float) -> None:
    """Print video assembly quality report."""
    print("\n" + "=" * 60)
    print("🎬 VIDEO QUALITY REPORT")
    print("=" * 60)

    video_path = Path(video_data["video_path"])
    thumbnail_path = Path(video_data.get("thumbnail_path", ""))

    n_video = sum(1 for a in media_assets if a.get("type") == "video")
    n_image = sum(1 for a in media_assets if a.get("type") == "image")
    n_placeholder = sum(1 for a in media_assets if a.get("type") == "placeholder")

    print(f"  Video:        {video_path.name}")
    print(f"  Size:         {video_path.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"  Thumbnail:    {'✅' if thumbnail_path.exists() else '❌'} {thumbnail_path.name if thumbnail_path.exists() else 'missing'}")
    print(f"  Audio:        {Path(audio_data['audio_path']).name}")
    print(f"  Media assets: {len(media_assets)} total ({n_video} video, {n_image} image, {n_placeholder} placeholder)")

    # Timing breakdown
    minutes = int(elapsed / 60)
    seconds = int(elapsed % 60)
    print(f"\n  ⏱️  Total time: {minutes}m {seconds}s")

    print(f"\n  ▶️  Play: {video_path}")


def report_metadata_quality(metadata: dict) -> None:
    """Print SEO metadata quality report."""
    if not metadata:
        return
    
    print("\n" + "=" * 60)
    print("📊 SEO METADATA REPORT")
    print("=" * 60)
    
    titles = metadata.get("titles", [])
    print(f"\n  🏆 Selected title: {metadata.get('selected_title', 'N/A')}")
    print(f"\n  📝 Title options ({len(titles)}):")
    for i, t in enumerate(titles, 1):
        print(f"     {i}. {t} ({len(t)} chars)")
    
    desc = metadata.get("description", "")
    print(f"\n  📄 Description ({len(desc.encode('utf-8'))} bytes):")
    # Show first 125 chars (the hook) separately
    print(f"     Hook: \"{desc[:125]}...\"")
    
    tags = metadata.get("tags", [])
    tags_str = ", ".join(tags)
    print(f"\n  🏷️  Tags ({len(tags)} total, {len(tags_str)} chars):")
    print(f"     {tags_str}")
    
    thumb_text = metadata.get("thumbnail_text", "")
    print(f"\n  🖼️  Thumbnail text: \"{thumb_text}\" ({len(thumb_text)} chars)")
    
    cost = metadata.get("cost_estimate", 0)
    tokens = metadata.get("token_count", 0)
    if tokens:
        print(f"\n  💰 Tokens: {tokens} | Cost: ${cost:.6f}")


def main():
    parser = argparse.ArgumentParser(
        description="Autotube Test Video — fast quality verification",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_video.py                        # Full test cycle
  python test_video.py --topic Milgram        # Specific experiment
  python test_video.py --skip-scrape          # Reuse DB content
  python test_video.py --verbose              # Show AI response details
  python test_video.py --prod                 # Production mode (8-14 min)
  python test_video.py --quick                # Quick test mode (~30s, fast render)
  python test_video.py --canal canal1         # Link to specific channel
  python test_video.py --skip-metadata        # Skip AI metadata generation
        """,
    )
    parser.add_argument("--topic", type=str, help="Specific experiment/topic to search for")
    parser.add_argument("--skip-scrape", action="store_true", help="Skip scraping — use existing DB content")
    parser.add_argument("--verbose", action="store_true", help="Show detailed AI responses and debug logs")
    parser.add_argument("--prod", action="store_true", help="Production mode: full-length 8-14 min video")
    parser.add_argument("--quick", action="store_true", help="Quick test mode: ultra-short ~30s video (fast render)")
    parser.add_argument("--canal", type=str, default="canal1", 
                       help="Channel slug to link video to (default: canal1)")
    parser.add_argument("--skip-metadata", action="store_true",
                       help="Skip AI metadata generation (titles, description, tags)")
    args = parser.parse_args()

    setup_logging(args.verbose)

    # ── Toggle test/prod/quick mode ───────────────────────────────
    if args.prod:
        cfg.TEST_MODE = False
        # Monkey-patch IMAGES_PER_SCENE for quick mode (used by image_fetcher)
        if hasattr(cfg, 'QUICK_TEST_IMAGES_PER_SCENE'):
            delattr(cfg, '_quick_images_override')
        logger.info("🚀 PRODUCTION MODE: full-length video (8-14 min)")
    elif args.quick:
        cfg.TEST_MODE = True
        cfg.TEST_SCRIPT_WORDS_MIN = cfg.QUICK_TEST_SCRIPT_WORDS_MIN
        cfg.TEST_SCRIPT_WORDS_MAX = cfg.QUICK_TEST_SCRIPT_WORDS_MAX
        cfg.TEST_SCRIPT_SCENES_MIN = cfg.QUICK_TEST_SCRIPT_SCENES_MIN
        cfg.TEST_SCRIPT_SCENES_MAX = cfg.QUICK_TEST_SCRIPT_SCENES_MAX
        cfg.TEST_VIDEO_DURATION_TARGET = cfg.QUICK_TEST_VIDEO_DURATION_TARGET
        cfg.IMAGES_PER_SCENE = cfg.QUICK_TEST_IMAGES_PER_SCENE
        logger.info("⚡ QUICK TEST MODE: ultra-short video (%d-%d words, %d-%d scenes, %d images/scene)",
                     cfg.TEST_SCRIPT_WORDS_MIN, cfg.TEST_SCRIPT_WORDS_MAX,
                     cfg.TEST_SCRIPT_SCENES_MIN, cfg.TEST_SCRIPT_SCENES_MAX,
                     cfg.IMAGES_PER_SCENE)
    else:
        cfg.TEST_MODE = True
        logger.info("🧪 TEST MODE: short video (%d-%d words, %d-%d scenes)",
                     cfg.TEST_SCRIPT_WORDS_MIN, cfg.TEST_SCRIPT_WORDS_MAX,
                     cfg.TEST_SCRIPT_SCENES_MIN, cfg.TEST_SCRIPT_SCENES_MAX)

    full_start = time.time()

    from orchestrator import PipelineOrchestrator
    from database.db import Database, init_db

    db = Database()
    init_db()

    orch = PipelineOrchestrator(canal=args.canal)

    # ── Phase 1: Scrape ──────────────────────────────────────────
    if not args.skip_scrape:
        t0 = time.time()
        logger.info("📥 Phase 1: Scraping content...")
        count = orch.phase_scrape()
        t1 = time.time()
        logger.info("   Scraped %d items in %.1fs", count, t1 - t0)
    else:
        unused = db.get_unused_count(args.canal)
        logger.info("📥 Phase 1: SKIPPED (--skip-scrape). %d unused items in DB.", unused)

    # ── Phase 2: Generate script ─────────────────────────────────
    t0 = time.time()
    logger.info("📝 Phase 2: Generating script...")

    # Fetch one unused content item (optionally by topic)
    items = db.get_unused_content(canal=args.canal, limit=10)
    if not items:
        logger.error("❌ No unused content in DB. Run with --skip-scrape or scrape first.")
        return 1

    # If topic specified, prefer matching items
    selected = items[0]
    if args.topic:
        topic_lower = args.topic.lower()
        for item in items:
            if topic_lower in str(item.get("title", "")).lower() or topic_lower in str(item.get("text", "")).lower():
                selected = item
                break
        else:
            logger.warning("No exact match for '%s' — using first available item.", args.topic)

    script = orch.script_gen.generate(selected)
    t1 = time.time()

    if not script:
        logger.error("❌ Script generation failed")
        return 1

    logger.info("   Script generated in %.1fs", t1 - t0)
    report_script_quality(script)

    if args.verbose:
        print("\n" + "-" * 40)
        print("FULL SCRIPT:")
        print(script.get("guion", ""))
        print("-" * 40)

    # ── Phase 3: TTS ─────────────────────────────────────────────
    t0 = time.time()
    logger.info("🔊 Phase 3: TTS audio...")
    audio_data = orch.phase_tts(script)
    if not audio_data:
        logger.error("❌ TTS failed")
        return 1
    logger.info("   Audio: %s (%.1fs)", Path(audio_data["audio_path"]).name, time.time() - t0)

    # ── Phase 4: Media (video + image hybrid) ───────────────────
    t0 = time.time()
    logger.info("🖼️  Phase 4: Fetching media assets (video + image)...")
    media_assets = orch.phase_media(script)
    if not media_assets:
        logger.error("❌ Media fetch failed")
        return 1
    n_video = sum(1 for a in media_assets if a.get("type") == "video")
    n_image = sum(1 for a in media_assets if a.get("type") == "image")
    logger.info("   %d assets (%d video, %d image) in %.1fs",
                len(media_assets), n_video, n_image, time.time() - t0)

    # ── Phase 5: Video assembly ──────────────────────────────────
    t0 = time.time()
    logger.info("🎬 Phase 5: Assembling video...")
    video_data = orch.phase_video(script, audio_data, media_assets)
    t1 = time.time()
    if not video_data:
        logger.error("❌ Video assembly failed")
        return 1

    full_elapsed = time.time() - full_start
    logger.info("   Video rendered in %.1fs", t1 - t0)

    report_video_quality(video_data, audio_data, media_assets, full_elapsed)

    # ── Phase 6: SEO Metadata (optional) ─────────────────────────
    metadata = None
    if not args.skip_metadata:
        t0 = time.time()
        logger.info("📊 Phase 6: Generating SEO metadata & optimized thumbnail...")
        metadata = orch.phase_metadata(script, video_data)
        t1 = time.time()
        if metadata:
            logger.info("   Metadata generated in %.1fs", t1 - t0)
            report_metadata_quality(metadata)
        else:
            logger.warning("   Metadata generation failed — video is still valid")
    else:
        logger.info("📊 Phase 6: Metadata generation SKIPPED (--skip-metadata)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
