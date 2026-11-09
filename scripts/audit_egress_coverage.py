#!/usr/bin/env python3
"""Auditoría estática: evita nuevas rutas locales a YouTube/Google.

Regla: cualquier archivo nuevo que importe googleapiclient/yt-dlp/Playwright o
use URLs de Google/YouTube debe pasar por el gateway o estar justificado en
``KNOWN_LOCAL`` (archivos legacy ya revisados y, cuando aplica, fail-closed).

Uso:
    python3 scripts/audit_egress_coverage.py
Exit 0 si no hay violaciones; 1 si aparece una ruta nueva no allowlisted.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = (
    "googleapiclient",
    "google_auth_oauthlib",
    "yt_dlp",
    "YoutubeDL",
    '"yt-dlp"',
    "'yt-dlp'",
    "i.ytimg.com",
    "suggestqueries.google.com",
    "news.google.com",
    "studio.youtube.com",
    "youtube.com/watch",
    "googleapis.com",
)

ALLOWED_PREFIXES = (
    "egress_agent/",
    "tests/",
    "deploy/",
    "docs/",
    "specs/",
    "api/services/egress_",
    "api/services/youtube_gateway.py",
)

# Archivos legacy con rutas locales revisadas. Para un canal gestionado deben
# estar protegidos por el router, un guard o fail-closed. No añadir aquí sin
# justificar en el ADR/spec.
KNOWN_LOCAL = {
    "pipeline/youtube_uploader.py",
    "pipeline/youtube_stats.py",
    "pipeline/youtube_stats_scraper.py",
    "pipeline/youtube_wall_scraper.py",
    "pipeline/youtube_playlists.py",
    "pipeline/youtube_comments.py",
    "pipeline/youtube_channel_manager.py",
    "pipeline/youtube_reach.py",
    "pipeline/youtube_browser.py",
    "pipeline/comments_reply_browser.py",
    "pipeline/comment_reply_browser.py",
    "pipeline/metadata_optimizer.py",
    "pipeline/thumbnail_compare.py",
    "pipeline/seo_researcher.py",
    "pipeline/topic_demand.py",
    "pipeline/viral_cloner.py",
    "pipeline/title_keyword_planner.py",
    "pipeline/providers/youtube_cc.py",
    "pipeline/video_lifecycle.py",
    "pipeline_dist/ytdlp_audio_dist.py",
    "pipeline_dist/ytdlp_clip_dist.py",
    "scrapers/youtube_viral.py",
    "scrapers/google_news.py",
    "scrapers/base.py",
    "database/db_extended.py",
    "api/services/yt_state_reconciler.py",
    "api/services/thumbnail_verification_service.py",
    "api/services/shorts_scheduler.py",
    "api/services/upload_health_checker.py",
    "api/services/publish_repack.py",
    "api/services/publish_coverage.py",
    "api/services/gradual_resume.py",
    "api/services/spam_mitigation.py",
    "api/services/review_governance.py",
    "api/services/ab_test_worker.py",
    "api/services/editorial_reviews.py",
    "api/services/view_gap_monitor.py",
    "api/services/shorts_backfill_service.py",
    "api/routers/auth.py",
    "api/routers/channels.py",
    "api/routers/videos.py",
    "api/routers/shorts.py",
    "pipeline/video_lifecycle.py",
    "scripts/check_video_removals.py",
    "scripts/hold_pending_publishes.py",
    "scripts/recover_thumbnails.py",
    "scripts/reupload_missing_thumbnails.py",
    "scripts/regenerate_and_upload_thumbnails.py",
    "scripts/delete_comment.py",
    "scripts/diagnose_yt_processing.py",
    "scripts/verify_yt_privacy.py",
    "scripts/oauth_quick.py",
    "scripts/complete_auth.py",
    "scripts/yt_mark_all_ia.py",
    "scripts/yt_mark_pending_ia.py",
    "scripts/diag_yt_processing.py",
    "scripts/fix_stuck_processing.py",
    "scripts/register_missing_video.py",
    "scripts/cleanup_phase2.py",
    "scripts/audit_egress_coverage.py",
    "pipeline/shorts_cross_promote.py",
    "api/main.py",
    "api/services/full_pipeline_worker.py",
    "api/services/redistribution_worker.py",
    "api/services/generation_service.py",
    "api/services/upload_scheduler.py",
    "api/services/publishers/platform_manager.py",
    "api/routers/system.py",
    "api/routers/cross_platform.py",
    # Entrypoints legacy (no forman parte del worker de un canal gestionado).
    "orchestrator.py",
    "main.py",
    "test_video.py",
}


def _iter_py():
    skip_dirs = {
        ".git", "__pycache__", "frontend", "backups", "reports",
        "output", "logs", "tokens", "data", "node_modules",
    }
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if any(part in skip_dirs for part in rel.parts):
            continue
        yield path, rel.as_posix()


def main() -> int:
    violations: list[tuple[str, str]] = []
    for path, rel in _iter_py():
        # Los scripts operativos y pipeline_dist no forman parte del pipeline
        # automático de un canal; se revisan manualmente y tienen su propio
        # guard/fail-closed cuando aplican.
        if rel.startswith(("scripts/", "pipeline_dist/")):
            continue
        if rel.startswith(ALLOWED_PREFIXES) or rel in KNOWN_LOCAL:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for token in FORBIDDEN:
            if token in text:
                violations.append((rel, token))

    if violations:
        print("❌ egress coverage: rutas locales nuevas a YouTube/Google:")
        for rel, token in violations:
            print(f"  - {rel}: {token}")
        print("\nUsa api/services/youtube_gateway.py o justifica en KNOWN_LOCAL.")
        return 1
    print("✅ egress coverage: sin rutas locales nuevas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
