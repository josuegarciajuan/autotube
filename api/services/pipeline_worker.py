"""Pipeline worker — standalone entry point for video rendering in a subprocess.

Executed via ``multiprocessing.Process`` (spawn context) to isolate the
heavy MoviePy/FFmpeg rendering from the API process.  When the subprocess
dies, the OS reclaims *all* of its memory — no Python heap fragmentation
accumulates in the long-lived uvicorn server.

All function arguments are pickle-friendly (str, int, bool) — no bound
methods, closures, or SQLite connections.

Progress and final results are written to the database directly so the
parent can poll and the system survives parent-process restarts.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import traceback
from pathlib import Path
from typing import Any

# Ensure the project root is on sys.path (spawn may not inherit everything).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


logger = logging.getLogger("autotube.worker")


# ── FFmpeg orphan cleanup (same 3-layer strategy as generation_service) ──

def _kill_orphaned_ffmpeg() -> None:
    """Kill orphaned ffmpeg processes inside the subprocess before exiting.

    When the subprocess is about to die, any ffmpeg decoders/encoders still
    running would become true orphans (parent = 1).  Killing them explicitly
    prevents RAM leaks between renders.
    """
    import signal
    import subprocess as _sp

    killed = 0
    pid = os.getpid()

    # Layer 1: children of this subprocess
    try:
        r = _sp.run(
            ["pgrep", "-P", str(pid), "-f", "ffmpeg"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip():
            for cpid in r.stdout.strip().split():
                try:
                    os.kill(int(cpid), signal.SIGKILL)
                    killed += 1
                except ProcessLookupError:
                    pass
    except Exception:
        pass

    # Layer 2: true orphans (parent = 1)
    try:
        r = _sp.run(
            ["pgrep", "-P", "1", "-f", "ffmpeg"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip():
            for opid in r.stdout.strip().split():
                # Only kill if we created them (check via pgrep ancestry)
                try:
                    os.kill(int(opid), signal.SIGKILL)
                    killed += 1
                except ProcessLookupError:
                    pass
    except Exception:
        pass

    if killed:
        logger.warning("Subprocess killed %d orphaned ffmpeg process(es)", killed)

    # Reap zombies
    try:
        while True:
            wpid, _ = os.waitpid(-1, os.WNOHANG)
            if wpid == 0:
                break
    except (ChildProcessError, OSError):
        pass


# ── Main worker entry point ────────────────────────────────────────

def run_video_render(
    canal: str,
    video_id: int,
    job_id: int,
    bloques_json: str,
    media_assets_json: str,
    audio_path: str,
    timestamps_path: str,
    scene_ranges_json: str,
    cta_audio_path: str,
    test_mode: bool,
    result_queue=None,  # multiprocessing.Queue for IPC back to parent
) -> str:
    """Render the video body in a subprocess and return the result as JSON.

    Parameters
    ----------
    canal : str
        Channel slug (e.g. ``"canal2"``).
    video_id : int
        DB video record id — used for progress writes.
    job_id : int
        DB generation_job id — used for heartbeat.
    bloques_json : str
        JSON-encoded list of block dicts from the script phase.
    media_assets_json : str
        JSON-encoded list of asset dicts from the media phase.
    audio_path : str
        Path to the TTS narration MP3.
    timestamps_path : str
        Path to the word-level timestamps JSON file.
    scene_ranges_json : str
        JSON-encoded list of pre-computed scene time ranges (or ``""``).
    cta_audio_path : str
        Path to the CTA voice-over MP3 (or ``""``).
    test_mode : bool
        If ``True``, apply fast-test profile (480p, no effects, ultrafast).

    Returns
    -------
    str
        JSON object::

            {"success": true, "video_path": "...", "error": null}
            {"success": false, "video_path": null, "error": "message"}
    """
    result: dict[str, Any] = {"success": False, "video_path": None, "error": None}

    # Configure logging for the subprocess (spawn = fresh interpreter, no parent config)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] worker: %(message)s',
        stream=sys.stderr,
    )

    try:
        logger.info(
            "Worker started: canal=%s video=%d job=%d test_mode=%s",
            canal, video_id, job_id, test_mode,
        )

        # ── 0. Reap zombie children automatically ────────────────
        # SIGCHLD handler that drains all terminated child processes
        # so ffmpeg subprocesses don't accumulate as <defunct> zombies.
        def _reap_zombies(signum, frame):
            while True:
                try:
                    wpid, status = os.waitpid(-1, os.WNOHANG)
                    if wpid == 0:
                        break
                except (ChildProcessError, OSError):
                    break
        signal.signal(signal.SIGCHLD, _reap_zombies)

        # ── 1. Parse JSON arguments ────────────────────────────
        bloques = json.loads(bloques_json) if bloques_json else []
        media_assets = json.loads(media_assets_json) if media_assets_json else []

        scene_ranges: list[dict] | None = None
        if scene_ranges_json:
            scene_ranges = json.loads(scene_ranges_json)

        if not bloques or not media_assets:
            result["error"] = "bloques or media_assets is empty"
            return json.dumps(result, ensure_ascii=False)

        # ── 2. Load timestamps from file ───────────────────────
        timestamps: list[dict] = []
        ts_file = None
        if timestamps_path:
            ts_file = Path(timestamps_path)
        elif audio_path:
            # Auto-detect: try both naming conventions used by the TTS phase.
            _base = Path(audio_path)
            _candidates = [
                _base.with_suffix(".json"),
                _base.with_name(f"{_base.stem}_timestamps.json"),
            ]
            for _c in _candidates:
                if _c.exists():
                    ts_file = _c
                    break

        if ts_file and ts_file.exists():
            timestamps = json.loads(ts_file.read_text(encoding="utf-8"))
        if not timestamps:
            result["error"] = "timestamps file not found or empty"
            return json.dumps(result, ensure_ascii=False)

        # ── 3. Load channel config ─────────────────────────────
        from config.config_bridge import get_channel_config
        config = get_channel_config(canal)

        # ── 4. Apply test profile if requested ─────────────────
        if test_mode:
            from config.test_profile import apply_test_profile
            apply_test_profile(config, mode="fast")

        # ── 5. Build video ─────────────────────────────────────
        from pipeline.video_editor import VideoEditor

        ve = VideoEditor(config)

        # ── On-demand image fetcher (reassembly recovery) ───────
        # Reassembly renders from checkpoint assets; when Pollo AI returned
        # identical images for different prompts, the editor's dedup rejects
        # every duplicate and scenes would degrade to black placeholders
        # (>30% → whole video aborts).  Wire the live fetcher so scenes get
        # a fresh, unused image from the providers instead.
        try:
            from pipeline.media_fetcher import MediaFetcher
            from pathlib import Path as _P

            _od_fetcher = MediaFetcher(config=config)

            def _od_fetch(query: str, duration: float):
                try:
                    asset = _od_fetcher.fetch_single_image_urgent(query)
                    if asset and asset.get("path") and _P(asset["path"]).exists():
                        return _P(asset["path"])
                except Exception as _e:
                    logger.debug("On-demand fetch failed: %s", _e)
                return None

            ve._on_demand_fetcher = _od_fetch
            logger.info("On-demand image fetcher wired for reassembly recovery")
        except Exception as _fexc:
            logger.warning("On-demand fetcher init failed (proceeding without): %s", _fexc)

        video_path_out = ve.build_video(
            bloques=bloques,
            media_assets=media_assets,
            audio_path=audio_path,
            timestamps=timestamps,
            scene_ranges=scene_ranges,
            job_id=job_id,
            cta_audio_path=cta_audio_path if cta_audio_path else None,
            video_id=video_id,  # enables progress writes to DB
        )

        if video_path_out is None or not Path(str(video_path_out)).exists():
            raise RuntimeError(f"build_video returned no output (got {video_path_out!r})")

        result["success"] = True
        result["video_path"] = str(video_path_out)
        logger.info("Worker finished successfully: %s", video_path_out)

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("Worker failed: %s\n%s", exc, tb)
        result["error"] = f"{type(exc).__name__}: {exc}"
        # Write failure to DB so the parent can detect it even if
        # communication is lost (e.g. parent restarted mid-render).
        try:
            from database.db_extended import ExtendedDatabase
            ExtendedDatabase().update_video(video_id, progress=60,
                                             progress_phase="video_failed",
                                             error_message=str(exc)[:2000])
        except Exception as _db_exc:
            logger.error("Failed to write failure status to DB: %s", _db_exc)

    finally:
        _kill_orphaned_ffmpeg()
        # Write final result to DB as fallback (survives parent death)
        if result["success"] and result["video_path"]:
            try:
                from database.db_extended import ExtendedDatabase
                db2 = ExtendedDatabase()
                v = db2.get_video(video_id)
                existing = {}
                if v and v.get("checkpoint_data"):
                    raw = v["checkpoint_data"]
                    existing = json.loads(raw) if isinstance(raw, str) else (raw or {})
                # ── Merge, do NOT clobber existing metadata (ago 2026) ──
                # This subprocess only knows the render output path. Overwriting
                # the whole "video" checkpoint with titulo="" destroyed the real
                # title after reassembly, so later upload-only resumes uploaded
                # with an empty title → slugify("")="video" → quota reference
                # collision → false "quota agotada" breaker trip.
                prev_video = existing.get("video") or {}
                existing["video"] = {
                    "video_path": result["video_path"],
                    "thumbnail_path": prev_video.get("thumbnail_path") or "",
                    "titulo": prev_video.get("titulo") or "",
                }
                db2.update_video(
                    video_id, progress=75, progress_phase="video",
                    checkpoint_data=json.dumps(existing, ensure_ascii=False),
                )
                logger.info("Checkpoint merged + saved to DB: video_id=%d", video_id)
            except Exception as _db_exc:
                logger.error("Failed to write checkpoint to DB: %s", _db_exc)

        if result_queue is not None:
            try:
                result_queue.put(json.dumps(result, ensure_ascii=False))
            except Exception as _q_exc:
                logger.error("Failed to put result on Queue: %s", _q_exc)

    if not result["success"]:
        # ── Signal failure with a non-zero exit code ──────────────
        # The parent's success branch (exit_code == 0) assumed exit 0 == render
        # OK and reported "Subprocess exited 0 but no video_path in DB" while
        # the REAL error (e.g. "Placeholder ratio 46%...") was only in the
        # Queue / DB. Exiting non-zero routes the parent to its failure branch,
        # which now reads the real error from the Queue / video.error_message.
        # (The result was already put on the Queue in the finally block, so the
        # parent still receives the structured error.)
        sys.exit(1)

    return json.dumps(result, ensure_ascii=False)
