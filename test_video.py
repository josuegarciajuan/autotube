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
    - TEST_MODE must be True in config/canal2_config.py (or set via env)
    - OpenAI/DeepSeek API key must be configured
    - Unsplash/Pexels API keys for images
"""

import argparse
import importlib
import json
import logging
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import LOG_LEVEL, LOG_FORMAT, LOGS_DIR

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

    n_video = sum(1 for a in media_assets if isinstance(a, dict) and a.get("type") == "video")
    n_image = sum(1 for a in media_assets if isinstance(a, dict) and a.get("type") == "image")
    n_placeholder = sum(1 for a in media_assets if isinstance(a, dict) and a.get("type") == "placeholder")

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
  python test_video.py --canal canal2         # Link to specific channel
  python test_video.py --skip-metadata        # Skip AI metadata generation
        """,
    )
    parser.add_argument("--topic", type=str, help="Specific experiment/topic to search for")
    parser.add_argument("--skip-scrape", action="store_true", help="Skip scraping — use existing DB content")
    parser.add_argument("--verbose", action="store_true", help="Show detailed AI responses and debug logs")
    parser.add_argument("--prod", action="store_true", help="Production mode: full-length 8-14 min video")
    parser.add_argument("--quick", action="store_true", help="Quick test mode: ultra-short ~30s video (fast render)")
    parser.add_argument("--quarter", action="store_true", 
                       help="Quarter-duration test: ~1/4 of normal (tests v2 outline+quality changes)")
    parser.add_argument("--fast-test", action="store_true",
                       help="Fast test mode: 480x270, no effects, no upload, canal4, ~5 min total")
parser.add_argument("--canal", type=str, required=True,
                   help="Channel slug to link video to")
    parser.add_argument("--skip-metadata", action="store_true",
                       help="Skip AI metadata generation (titles, description, tags)")
    args = parser.parse_args()

    # Dynamically load the correct per-channel config module
    cfg = importlib.import_module(f"config.{args.canal}_config")

    setup_logging(args.verbose)

    # ── Unified test profile (single source of truth for CLI + API) ──
    from config.test_profile import apply_test_profile, get_test_word_targets

    skip_upload = False
    if args.fast_test:
        args.canal = "test"
        cfg = importlib.import_module(f"config.test_config")
        apply_test_profile(cfg, mode="fast")
        _, _, _, _, quarter_dur = get_test_word_targets(cfg, mode="quarter")
        words_min, words_max, blocks_min, blocks_max, quarter_dur = get_test_word_targets(cfg, mode="quarter")
        cfg.TEST_SCRIPT_WORDS_MIN = words_min
        cfg.TEST_SCRIPT_WORDS_MAX = words_max
        cfg.TEST_SCRIPT_BLOCKS_MIN = blocks_min
        cfg.TEST_SCRIPT_BLOCKS_MAX = blocks_max
        cfg.TEST_VIDEO_DURATION_TARGET = quarter_dur
        skip_upload = True
        args.skip_scrape = False
        args.skip_metadata = True
        logger.info("⚡ FAST TEST: %dx%d ultrafast canal=%s ~%.1fmin %d-%dw unified-profile",
                     cfg.VIDEO_RESOLUTION[0], cfg.VIDEO_RESOLUTION[1],
                     args.canal, quarter_dur,
                     cfg.TEST_SCRIPT_WORDS_MIN, cfg.TEST_SCRIPT_WORDS_MAX)

    elif args.prod:
        apply_test_profile(cfg, mode="prod")
        if hasattr(cfg, '_quick_images_override'):
            delattr(cfg, '_quick_images_override')
        logger.info("🚀 PRODUCTION MODE: full-length video (8-14 min)")

    elif args.quarter:
        apply_test_profile(cfg, mode="quarter")
        words_min, words_max, blocks_min, blocks_max, quarter_dur = get_test_word_targets(cfg, mode="quarter")
        quarter_scenes_min = max(4, cfg.PROD_SCRIPT_SCENES_MIN // 3)
        quarter_scenes_max = max(6, cfg.PROD_SCRIPT_SCENES_MAX // 3)
        cfg.TEST_SCRIPT_WORDS_MIN = words_min
        cfg.TEST_SCRIPT_WORDS_MAX = words_max
        cfg.TEST_SCRIPT_SCENES_MIN = quarter_scenes_min
        cfg.TEST_SCRIPT_SCENES_MAX = quarter_scenes_max
        cfg.TEST_SCRIPT_BLOCKS_MIN = blocks_min
        cfg.TEST_SCRIPT_BLOCKS_MAX = blocks_max
        cfg.TEST_VIDEO_DURATION_TARGET = quarter_dur
        logger.info("¼ QUARTER TEST MODE: ~%.1f min (%d-%d words, %d-%d blocks) unified-profile",
                     quarter_dur, words_min, words_max, blocks_min, blocks_max)

    elif args.quick:
        apply_test_profile(cfg, mode="quick")
        words_min, words_max, blocks_min, blocks_max, dur_target = get_test_word_targets(cfg, mode="quick")
        cfg.TEST_SCRIPT_WORDS_MIN = words_min
        cfg.TEST_SCRIPT_WORDS_MAX = words_max
        cfg.TEST_SCRIPT_BLOCKS_MIN = blocks_min
        cfg.TEST_SCRIPT_BLOCKS_MAX = blocks_max
        cfg.TEST_VIDEO_DURATION_TARGET = dur_target
        cfg.IMAGES_PER_SCENE = getattr(cfg, 'QUICK_TEST_IMAGES_PER_SCENE', 3)
        logger.info("⚡ QUICK TEST MODE: ultra-short (%d-%d words) unified-profile",
                     words_min, words_max)

    else:
        apply_test_profile(cfg, mode="default")
        logger.info("🧪 TEST MODE: short video (%d-%d words) unified-profile",
                     cfg.TEST_SCRIPT_WORDS_MIN, cfg.TEST_SCRIPT_WORDS_MAX)

    full_start = time.time()

    from orchestrator import PipelineOrchestrator
    from database.db import Database, init_db
    from database.db_extended import migrate_v2, ExtendedDatabase

    db = Database()
    ext_db = ExtendedDatabase()
    init_db()
    migrate_v2()

    # ── Register generation job for dashboard feedback ──────────────
    # This creates a job in the DB so the frontend panel shows progress.
    channel_id = None
    video_id = None
    try:
        ch = ext_db.get_channel_by_slug(args.canal)
        channel_id = ch["id"] if ch else None
    except Exception:
        pass
    job_id = None
    if channel_id:
        try:
            video_id_for_job = None  # will be assigned after video creation
            job_id = ext_db.create_job(channel_id, "generate_and_upload", video_id_for_job)
            logger.info("📊 Dashboard job #%d registered for channel '%s'", job_id, args.canal)
        except Exception as exc:
            logger.warning("Could not register dashboard job: %s", exc)

    def _update_panel(progress: int, phase: str, msg: str, **kwargs):
        """Update generation_jobs table for dashboard visibility.
        
        The orchestrator creates its own videos entry via insert_video().
        We only update generation_jobs here to avoid duplicates.
        """
        nonlocal job_id, video_id, ext_db
        if not job_id:
            return
        try:
            ext_db.update_job(job_id, progress=progress, phase=phase, status="running")
        except Exception:
            pass

    orch = PipelineOrchestrator(
        canal=args.canal,
        progress_callback=_update_panel,
    )

    # Bridge unified test profile to the orchestrator's SimpleNamespace.
    # The orchestrator loads config via config_bridge which creates a NEW object,
    # so module-level monkey-patching doesn't propagate. We re-apply the same
    # profile so orchestrator and cfg module are always in sync.
    orch_config = orch.config
    if hasattr(orch_config, '__dict__'):
        # Determine which mode was applied to cfg
        if args.fast_test:
            mode = "fast"
        elif args.prod:
            mode = "prod"
        elif args.quarter:
            mode = "quarter"
        elif args.quick:
            mode = "quick"
        else:
            mode = "default"
        from config.test_profile import apply_test_profile as _atp2, get_test_word_targets as _gtwt2
        _atp2(orch_config, mode=mode)
        # Also bridge word/block/duration targets
        orch_config.TEST_SCRIPT_WORDS_MIN = cfg.TEST_SCRIPT_WORDS_MIN
        orch_config.TEST_SCRIPT_WORDS_MAX = cfg.TEST_SCRIPT_WORDS_MAX
        orch_config.TEST_VIDEO_DURATION_TARGET = cfg.TEST_VIDEO_DURATION_TARGET
        orch_config.TEST_SCRIPT_BLOCKS_MIN = getattr(cfg, "TEST_SCRIPT_BLOCKS_MIN", 3)
        orch_config.TEST_SCRIPT_BLOCKS_MAX = getattr(cfg, "TEST_SCRIPT_BLOCKS_MAX", 6)

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
        if job_id:
            ext_db.update_job(job_id, status="failed", progress=0, phase="script",
                             error_msg="Script generation failed")
        if video_id:
            with ext_db._connect() as conn:
                conn.execute("UPDATE videos SET status='error', progress_phase='script' WHERE id=?", (video_id,))
                conn.commit()
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
        if job_id:
            ext_db.update_job(job_id, status="failed", progress=30, phase="tts",
                             error_msg="TTS generation failed")
        return 1
    audio_gen_time = time.time() - t0
    audio_dur = (audio_data.get("timestamps", [{}])[-1].get("end_ms", 0) / 1000
                 if audio_data.get("timestamps") else 0)
    logger.info("   Audio: %s (gen %.1fs, dur %.1fs)", Path(audio_data["audio_path"]).name,
                audio_gen_time, audio_dur)

    # ── Phase 4: Media (video + image hybrid) ───────────────────
    t0 = time.time()
    logger.info("🖼️  Phase 4: Fetching media assets (video + image)...")
    media_assets_raw = orch.phase_media(script, audio_data)
    if not media_assets_raw:
        logger.error("❌ Media fetch failed")
        if job_id:
            ext_db.update_job(job_id, status="failed", progress=50, phase="media",
                             error_msg="Media fetch failed")
        if video_id:
            with ext_db._connect() as conn:
                conn.execute("UPDATE videos SET status='error', progress_phase='media' WHERE id=?", (video_id,))
                conn.commit()
        return 1

    _update_panel(55, "media", "Media listo")
    t_media = time.time() - t0
    logger.info("   Media fetched in %.1fs", t_media)

    # ── Phase 5: Video assembly ──────────────────────────────────
    t0 = time.time()
    logger.info("🎬 Phase 5: Assembling video...")
    video_data = orch.phase_video(script, audio_data, media_assets_raw, job_id=job_id)
    t1 = time.time()
    if not video_data:
        logger.error("❌ Video assembly failed")
        if job_id:
            ext_db.update_job(job_id, status="failed", progress=100, phase="error",
                             error_msg="Video assembly failed")
        if video_id:
            with ext_db._connect() as conn:
                conn.execute("UPDATE videos SET status='error', progress_phase='video' WHERE id=?", (video_id,))
                conn.commit()
        return 1
    _update_panel(85, "video", "Video renderizado")
    logger.info("   Video rendered in %.1fs", t1 - t0)

    # Link the orchestrator-created video entry for dashboard progress
    db_video_id = video_data.get("video_id") if video_data else None
    if db_video_id and not video_id:
        video_id = db_video_id
        logger.info("📊 Dashboard linked to video #%d", video_id)

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
            if job_id:
                ext_db.update_job(job_id, progress=92, phase="metadata",
                                 status="running")
        else:
            logger.warning("   Metadata generation failed — video is still valid")
    else:
        logger.info("📊 Phase 6: Metadata generation SKIPPED (--skip-metadata)")

    # ── Phase 7: Upload to YouTube (skip in fast-test mode) ───────
    if skip_upload:
        logger.info("📤 Phase 7: Upload SKIPPED (fast-test mode)")
        if job_id:
            ext_db.update_job(job_id, progress=100, phase="video_ready", status="completed")
        if video_id:
            with ext_db._connect() as conn:
                conn.execute("UPDATE videos SET status='ready', progress=100, progress_phase='video_ready' WHERE id=?", (video_id,))
                conn.commit()
    else:
        t0 = time.time()
        logger.info("📤 Phase 7: Uploading to YouTube...")
        try:
            upload_result = orch.phase_upload(script, video_data)
            if upload_result:
                logger.info("   ✅ Uploaded: %s (%.1fs)",
                             upload_result, time.time() - t0)
                if job_id:
                    ext_db.update_job(job_id, progress=100, phase="uploaded", status="completed")
                if video_id:
                    with ext_db._connect() as conn:
                        conn.execute("UPDATE videos SET status='ready', progress=100, progress_phase='uploaded' WHERE id=?", (video_id,))
                        conn.commit()
            else:
                logger.warning("   ⚠️ Upload returned empty — video exists locally")
                if job_id:
                    ext_db.update_job(job_id, progress=100, phase="video_ready", status="completed")
        except Exception as e:
            logger.error("   ❌ Upload failed: %s", e)
            if job_id:
                ext_db.update_job(job_id, progress=100, phase="video_ready", status="completed",
                                 error_msg=f"Upload failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
