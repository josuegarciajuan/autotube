#!/usr/bin/env python3
"""
Detect videos affected by the black-screen bug (Jul 2026).

Downloads each video from YouTube via yt-dlp (lightweight 360p),
runs ffmpeg blackdetect, and classifies:

  confirmed  = black segment starting 30-70% through, lasting >20%,
               ending near the end (>90% of total duration).
  suspected  = yt-dlp failed (private) OR no video on YT/disk,
               but log evidence exists (ULTIMATE FALLBACK, gap, etc.).
  clean      = blackdetect found NO significant black segments.

Output: scripts/detection_report.json

Usage:  python3 scripts/detect_black_screen_videos.py
        python3 scripts/detect_black_screen_videos.py --ids 946,930,932
"""

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("detect")

# ── Config ───────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "autotube.db"
TEMP_DIR = Path(tempfile.mkdtemp(prefix="bsdetect_"))
YTDLP = "yt-dlp"
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
LOG_DIR = PROJECT_ROOT / "logs"

# Black detection thresholds
BLACK_PIX_THRESH = 0.10   # < this fraction of max brightness = "near-black"
BLACK_MIN_DUR = 2.0       # minimum consecutive black seconds to report
BLACK_START_MIN_PCT = 30  # segment must start AFTER 30% of video
BLACK_START_MAX_PCT = 70  # segment must start BEFORE 70% of video
BLACK_MIN_PCT_DUR = 20    # segment must be at least 20% of total duration
BLACK_END_PCT = 90        # segment must reach past 90% of total duration


def load_env():
    """Load .env if it exists."""
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())


def get_candidates(db_path: Path, specific_ids: list[int] | None = None) -> list[dict]:
    """Query the DB for candidate videos to analyze."""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    if specific_ids:
        placeholders = ",".join("?" * len(specific_ids))
        rows = conn.execute(
            f"SELECT v.id, v.canal, v.yt_video_id, v.yt_url, v.status, "
            f"v.privacy_status, v.created_at, c.slug "
            f"FROM videos v LEFT JOIN channels c ON v.channel_id = c.id "
            f"WHERE v.id IN ({placeholders})",
            specific_ids,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT v.id, v.canal, v.yt_video_id, v.yt_url, v.status, "
            "v.privacy_status, v.created_at, c.slug "
            "FROM videos v LEFT JOIN channels c ON v.channel_id = c.id "
            "WHERE v.yt_video_id IS NOT NULL AND v.yt_video_id != '' "
            "AND v.status NOT LIKE '%quality%' "
            "AND v.created_at > '2026-07-15' "
            "ORDER BY v.id DESC",
        ).fetchall()

    results = [dict(r) for r in rows]
    conn.close()
    return results


def get_log_evidence(video_id: int) -> list[str]:
    """Search worker logs for evidence of black-screen issues."""
    evidence = []
    if not LOG_DIR.exists():
        return evidence

    # Search for worker logs mentioning this video_id
    patterns = [
        (rf"video[_\s]*id[=:\s]*{video_id}\b.*ULTIMATE FALLBACK", "ULTIMATE_FALLBACK"),
        (rf"video[_\s]*id[=:\s]*{video_id}\b.*body/audio mismatch.*gap=(\d+\.\d+)", "AUDIO_GAP"),
        (rf"video[_\s]*id[=:\s]*{video_id}\b.*placeholder ratio", "PLACEHOLDER_RATIO"),
        (rf"video[_\s]*id[=:\s]*{video_id}\b.*tuple.*lstrip", "TUPLE_BUG"),
    ]

    for log_file in sorted(LOG_DIR.glob("worker_*.log")):
        try:
            text = log_file.read_text(errors="ignore")
            for pattern, label in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    gap = match.group(1) if label == "AUDIO_GAP" and match.lastindex else ""
                    detail = f"found in {log_file.name}"
                    if gap:
                        detail += f" gap={gap}s"
                    evidence.append(f"{label}: {detail}")
        except Exception:
            continue

    return evidence


def download_video(yt_video_id: str) -> Path | None:
    """Download a lightweight version of a video from YouTube.

    Uses yt-dlp with minimal quality (360p) for fast analysis.
    """
    output_path = TEMP_DIR / f"{yt_video_id}.mp4"
    if output_path.exists():
        output_path.unlink()

    cmd = [
        YTDLP,
        "-f", "worst[height<=360][ext=mp4]",
        "-o", str(output_path),
        "--no-playlist",
        "--geo-bypass",
        "--socket-timeout", "15",
        "--retries", "1",
        f"https://www.youtube.com/watch?v={yt_video_id}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 1000:
            return output_path
        # Check if it's a private video error
        stderr = (result.stderr or "") + (result.stdout or "")
        if "private" in stderr.lower() or "unavailable" in stderr.lower():
            logger.debug("Video %s appears to be private/unavailable", yt_video_id)
            return None
        logger.debug("yt-dlp failed for %s: rc=%d stderr=%s",
                     yt_video_id, result.returncode, stderr[:200])
        return None
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp timeout for %s", yt_video_id)
        return None
    except Exception as e:
        logger.warning("yt-dlp error for %s: %s", yt_video_id, e)
        return None


def analyze_black_detect(video_path: Path) -> dict | None:
    """Run ffmpeg blackdetect on a video file.

    Returns parseable info about the worst black segment found,
    or None if no significant black segments detected.
    """
    cmd = [
        FFMPEG, "-i", str(video_path),
        "-vf", f"blackdetect=d={BLACK_MIN_DUR}:pix_th={BLACK_PIX_THRESH}",
        "-an", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        # blackdetect writes to stderr
        output = result.stderr

        # Get total duration
        dur_cmd = [
            FFPROBE, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        dur_result = subprocess.run(dur_cmd, capture_output=True, text=True, timeout=15)
        total_dur = float(dur_result.stdout.strip()) if dur_result.stdout.strip() else 0

        if total_dur <= 0:
            return None

        # Parse blackdetect lines: black_start:X black_end:Y black_duration:Z
        black_segments = []
        for line in output.split("\n"):
            match = re.search(
                r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)",
                line,
            )
            if match:
                black_segments.append({
                    "start": float(match.group(1)),
                    "end": float(match.group(2)),
                    "duration": float(match.group(3)),
                })

        if not black_segments:
            return {"total_dur": total_dur, "segments": [], "worst_pct": 0}

        worst = max(black_segments, key=lambda s: s["duration"])
        worst_pct = (worst["duration"] / total_dur) * 100

        return {
            "total_dur": total_dur,
            "segments": black_segments,
            "worst_start": worst["start"],
            "worst_start_pct": (worst["start"] / total_dur) * 100,
            "worst_end": worst["end"],
            "worst_end_pct": (worst["end"] / total_dur) * 100,
            "worst_dur": worst["duration"],
            "worst_pct": worst_pct,
        }

    except Exception as e:
        logger.warning("blackdetect error for %s: %s", video_path.name, e)
        return None


def classify(analysis: dict | None, log_evidence: list[str], yt_video_id: str) -> str:
    """Classify a video based on blackdetect results + log evidence.

    Returns: 'confirmed', 'suspected', or 'clean'
    """
    if analysis is None:
        # Could not analyze (yt-dlp failed or no file)
        if log_evidence:
            return "suspected"
        return "clean"

    if not analysis.get("segments"):
        return "clean"

    worst = analysis
    start_pct = worst.get("worst_start_pct", 0)
    worst_pct = worst.get("worst_pct", 0)
    end_pct = worst.get("worst_end_pct", 0)

    if (start_pct >= BLACK_START_MIN_PCT
            and start_pct <= BLACK_START_MAX_PCT
            and worst_pct >= BLACK_MIN_PCT_DUR
            and end_pct >= BLACK_END_PCT):
        return "confirmed"

    # If there's significant black but doesn't match the exact pattern,
    # check with log evidence
    if worst_pct > 15 and log_evidence:
        return "suspected"

    return "clean"


def main():
    load_env()

    # Allow targeting specific IDs
    specific_ids = None
    for arg in sys.argv[1:]:
        if arg.startswith("--ids="):
            specific_ids = [int(x.strip()) for x in arg.split("=", 1)[1].split(",")]
            break

    candidates = get_candidates(DB_PATH, specific_ids)
    logger.info("Found %d candidate videos to analyze", len(candidates))

    confirmed = []
    suspected = []
    clean = []
    total = len(candidates)

    for idx, vid in enumerate(candidates):
        vid_id = vid["id"]
        yt_id = vid["yt_video_id"]
        canal = vid.get("slug") or vid.get("canal", "?")
        logger.info("[%d/%d] Analyzing video %d (%s) yt=%s...",
                     idx + 1, total, vid_id, canal, yt_id)

        # Get log evidence
        log_evidence = get_log_evidence(vid_id)

        # Download from YouTube
        video_path = None
        if yt_id:
            video_path = download_video(yt_id)

        # Analyze
        analysis = None
        if video_path and video_path.exists():
            analysis = analyze_black_detect(video_path)
            # Clean up temp file
            try:
                video_path.unlink()
            except Exception:
                pass

        # Classify
        verdict = classify(analysis, log_evidence, yt_id)

        result = {
            "video_id": vid_id,
            "canal": canal,
            "yt_video_id": yt_id,
            "yt_url": f"https://youtube.com/watch?v={yt_id}" if yt_id else "",
            "status": vid.get("status", ""),
            "privacy_status": vid.get("privacy_status", ""),
            "created_at": vid.get("created_at", ""),
            "verdict": verdict,
        }

        if analysis:
            result["analysis"] = {
                "total_dur": analysis["total_dur"],
                "worst_black_pct": analysis.get("worst_pct", 0),
                "worst_start_pct": analysis.get("worst_start_pct", 0),
                "worst_end_pct": analysis.get("worst_end_pct", 0),
            }
            pct_str = f"black={analysis.get('worst_pct', 0):.0f}%"
        else:
            result["analysis"] = None
            pct_str = "N/A"

        if log_evidence:
            result["log_evidence"] = log_evidence

        if verdict == "confirmed":
            confirmed.append(result)
            logger.warning("  ❌ CONFIRMED black-screen | %s | evidence=%s",
                          pct_str, log_evidence[:3] if log_evidence else "none")
        elif verdict == "suspected":
            suspected.append(result)
            logger.warning("  ⚠️ SUSPECTED | %s | %s",
                          pct_str, log_evidence[:2] if log_evidence else "yt-dlp failed")
        else:
            clean.append(result)
            logger.info("  ✅ CLEAN | %s", pct_str)

        # Brief pause to avoid rate limiting
        time.sleep(0.5)

    # ── Report ────────────────────────────────────────────────
    report = {
        "scan_date": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_analyzed": total,
        "confirmed_count": len(confirmed),
        "suspected_count": len(suspected),
        "clean_count": len(clean),
        "confirmed": confirmed,
        "suspected": suspected,
        "clean": clean,
    }

    report_path = PROJECT_ROOT / "scripts" / "detection_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    # ── Summary ───────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("DETECTION COMPLETE")
    logger.info("  Confirmed:  %d", len(confirmed))
    logger.info("  Suspected:  %d", len(suspected))
    logger.info("  Clean:      %d", len(clean))
    logger.info("  Total:      %d", total)
    logger.info("  Report:     %s", report_path)

    if confirmed:
        logger.info("--- CONFIRMED (need cleanup) ---")
        for v in confirmed:
            logger.info("  ID=%d %s %s black=%.0f%%",
                       v["video_id"], v["canal"], v["yt_url"],
                       (v.get("analysis") or {}).get("worst_black_pct", 0))

    if suspected:
        logger.info("--- SUSPECTED (needs review) ---")
        for v in suspected:
            reasons = v.get("log_evidence", ["yt-dlp unavailable"])
            logger.info("  ID=%d %s (%s)", v["video_id"], v["canal"], reasons[0][:60])

    # Cleanup temp dir
    try:
        import shutil
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
