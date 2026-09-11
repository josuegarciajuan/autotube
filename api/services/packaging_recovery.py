"""Packaging recovery: requeue videos held at the final packaging gate.

Videos rejected by the final upload choke point are marked
``validation_failed``. Glass Box only scans ``error`` and exhausted
``awaiting_upload`` rows, so a ``validation_failed`` video stays stuck forever
even after the packaging policy/config is relaxed or the title/thumbnail is
fixed (bug ago 2026: canal2 accumulated ~15 rows).

This sweep re-runs the exact same validator and, if the video now passes,
returns it to ``awaiting_upload`` for a normal dispatch. Videos that still fail
are left untouched (only a diagnostic detail is returned).
"""

import logging

logger = logging.getLogger("autotube.packaging_recovery")

# Cadence of the supervised loop (api/main.py).
PACKAGING_RECOVERY_INTERVAL_MIN = 30
# Safety cap per channel per cycle so a large backlog cannot flood the queue.
MAX_RECOVERIES_PER_CHANNEL_PER_CYCLE = 5
# Hard cap per invocation.
MAX_RECOVERIES_PER_CYCLE = 25


def recover_packaging_held_videos(
    db=None,
    dry_run: bool = False,
    channel_slug: str | None = None,
) -> dict:
    """Re-validate ``validation_failed`` videos and requeue the recoverable ones.

    Args:
        db: ExtendedDatabase (injected in tests).
        dry_run: if True, only report what would be requeued.
        channel_slug: optional filter to a single channel.

    Returns:
        dict with {scanned, recovered, skipped, errors, details}.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    from api.services.upload_scheduler import validate_upload_packaging
    from config.config_bridge import get_channel_config

    scanned = recovered = skipped = errors = 0
    per_channel: dict[int, int] = {}
    details: list[dict] = []
    channel_cache: dict[int, dict | None] = {}

    try:
        rows = db.get_videos(status="validation_failed", limit=500) or []
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("packaging recovery: could not list videos: %s", exc)
        return {"scanned": 0, "recovered": 0, "skipped": 0, "errors": 1, "details": []}

    for row in rows:
        if recovered >= MAX_RECOVERIES_PER_CYCLE:
            break
        vid = row.get("id")
        channel_id = row.get("channel_id")
        path = row.get("video_path")
        scanned += 1

        if channel_id not in channel_cache:
            try:
                channel_cache[channel_id] = db.get_channel(channel_id)
            except Exception:
                channel_cache[channel_id] = None
        channel = channel_cache.get(channel_id) or {}
        slug = channel.get("slug") or f"channel_{channel_id}"

        if channel_slug and slug != channel_slug:
            continue

        # The file must exist on disk, otherwise the upload cannot proceed.
        import os
        if not path or not os.path.exists(path):
            skipped += 1
            details.append({"video_id": vid, "slug": slug, "action": "missing_file"})
            continue

        if per_channel.get(channel_id, 0) >= MAX_RECOVERIES_PER_CHANNEL_PER_CYCLE:
            skipped += 1
            continue

        try:
            cfg = get_channel_config(slug)
        except Exception as exc:
            errors += 1
            logger.warning("packaging recovery: config error for %s: %s", slug, exc)
            continue

        video = {
            "titulo_final": row.get("titulo_final"),
            "thumbnail_path": row.get("thumbnail_path"),
            "thumbnail_text": row.get("thumbnail_text"),
        }
        try:
            result = validate_upload_packaging(video, cfg)
        except Exception as exc:
            errors += 1
            logger.warning("packaging recovery: validation error for video %s: %s", vid, exc)
            continue

        new_title = None
        if not result.valid:
            # Deterministic self-heal for length-only failures: trim the title
            # and re-validate. Specificity needs content evidence (LLM/manual)
            # and is intentionally NOT guessed here.
            if "length" in result.reasons:
                try:
                    from api.services.title_recovery import repair_title
                    repaired = repair_title(row.get("titulo_final"), cfg)
                except Exception as exc:
                    logger.debug("packaging recovery: repair_title failed for #%s: %s", vid, exc)
                    repaired = None
                if repaired:
                    retry_video = dict(video)
                    retry_video["titulo_final"] = repaired
                    retry = validate_upload_packaging(retry_video, cfg)
                    if retry.valid:
                        new_title = repaired
                        result = retry
            if not result.valid:
                skipped += 1
                details.append({
                    "video_id": vid, "slug": slug, "action": "still_invalid",
                    "reasons": list(result.reasons),
                })
                continue

        if dry_run:
            recovered += 1
            per_channel[channel_id] = per_channel.get(channel_id, 0) + 1
            detail = {"video_id": vid, "slug": slug, "action": "would_requeue"}
            if new_title:
                detail["new_title"] = new_title
            details.append(detail)
            logger.info("[%s] [DRY-RUN] packaging recovery would requeue video #%s", slug, vid)
            continue

        try:
            update_kwargs = {
                "status": "awaiting_upload",
                "progress": 5,
                "progress_phase": "upload",
                "scheduled_upload_at": None,
                "error_message": "Requeued by packaging recovery",
            }
            if new_title:
                import json as _json
                update_kwargs["titulo_final"] = new_title
                update_kwargs["title_options"] = _json.dumps([new_title], ensure_ascii=False)
            db.update_video(vid, **update_kwargs)
        except Exception as exc:
            errors += 1
            logger.error("packaging recovery: failed to requeue video %s: %s", vid, exc)
            continue

        recovered += 1
        per_channel[channel_id] = per_channel.get(channel_id, 0) + 1
        detail = {"video_id": vid, "slug": slug, "action": "requeued"}
        if new_title:
            detail["new_title"] = new_title
        details.append(detail)
        logger.warning("[%s] packaging recovery: requeued video #%s → awaiting_upload", slug, vid)

    if recovered:
        logger.info(
            "Packaging recovery: %d requeued, %d skipped, %d errors (scanned=%d)",
            recovered, skipped, errors, scanned,
        )
    return {"scanned": scanned, "recovered": recovered, "skipped": skipped,
            "errors": errors, "details": details}
