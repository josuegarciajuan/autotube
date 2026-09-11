"""Title recovery for videos held at the packaging gate.

Videos can be held with ``validation_failed`` because:
  - the title exceeds ``TITLE_MAX_CHARS`` ("length"), or
  - the title lacks a concrete anchor ("specificity": no place/person).

``repair_title`` is a safe, deterministic sanitiser (no LLM). It strips
clickbait suffixes and trims to the configured maximum at a word boundary.

``retitle_validation_failed`` is the hybrid path: regenerate from the script
with the same ``MetadataGenerator`` used at creation, validated against the
exact upload gate, falling back to ``repair_title``. Videos without a script
cannot be safely regenerated (the title alone is not evidence) and are reported
for manual review.
"""

import json
import logging

logger = logging.getLogger("autotube.title_recovery")


def _title_bounds(cfg) -> tuple[int, int]:
    minimum = int(getattr(cfg, "TITLE_MIN_CHARS", 28) or 28)
    maximum = int(getattr(cfg, "TITLE_MAX_CHARS", 65) or 65)
    return minimum, maximum


def _truncate_at_word(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" .,;:·•–—-|¿?¡!")


def repair_title(title: str, cfg, max_chars: int | None = None) -> str | None:
    """Deterministic title repair. Returns None if bounds cannot be met.

    Only fixes length/format, never specificity (which needs content evidence).
    """
    from pipeline.metadata_generator import _strip_clickbait_suffix

    minimum, maximum = _title_bounds(cfg)
    if max_chars is not None:
        maximum = int(max_chars)
    text = _strip_clickbait_suffix(title or "")
    text = " ".join(text.split())
    text = _truncate_at_word(text, maximum)
    if len(text) < minimum:
        return None
    return text


def _script_context(db, script_id) -> tuple[str, list]:
    if not script_id:
        return "", []
    try:
        script = db.get_script(script_id)
    except Exception:
        script = None
    if not script:
        return "", []
    guion = script.get("guion") or ""
    raw = script.get("keywords_json") or script.get("keywords") or "[]"
    if isinstance(raw, str):
        try:
            keywords = json.loads(raw) if raw.strip() else []
        except (json.JSONDecodeError, TypeError):
            keywords = []
    else:
        keywords = raw or []
    return guion, keywords


def _validate(cfg, title: str, row: dict):
    from api.services.upload_scheduler import validate_upload_packaging

    return validate_upload_packaging(
        {
            "titulo_final": title,
            "thumbnail_path": row.get("thumbnail_path"),
            "thumbnail_text": row.get("thumbnail_text"),
        },
        cfg,
    )


def _candidate_from_llm(cfg, guion, keywords, row, attempts: int) -> tuple[str | None, list]:
    """Try the channel MetadataGenerator until a title passes the upload gate."""
    try:
        from pipeline.metadata_generator import MetadataGenerator
        generator = MetadataGenerator(cfg)
    except Exception as exc:
        logger.warning("title recovery: MetadataGenerator unavailable: %s", exc)
        return None, []

    for _ in range(max(1, attempts)):
        try:
            meta = generator.generate({"guion": guion, "keywords": keywords}) or {}
        except Exception as exc:
            logger.warning("title recovery: LLM generation failed: %s", exc)
            continue
        candidate = meta.get("selected_title") or ""
        if candidate and _validate(cfg, candidate, row).valid:
            return candidate, (meta.get("titles") or [candidate])
    return None, []


def retitle_validation_failed(
    db=None,
    dry_run: bool = False,
    channel_slug: str | None = None,
    use_llm: bool = True,
    max_llm_attempts: int = 2,
) -> dict:
    """Re-title ``validation_failed`` videos so they pass the packaging gate.

    Persists only ``titulo_final`` + ``title_options`` and returns the video to
    ``awaiting_upload``. Videos without a script (no safe evidence for a new
    title) are reported as ``manual`` and left untouched.
    """
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    from config.config_bridge import get_channel_config

    result = {"scanned": 0, "retitled": 0, "skipped": 0, "manual": 0,
              "errors": 0, "details": []}
    try:
        rows = db.get_videos(status="validation_failed", limit=500) or []
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("title recovery: could not list videos: %s", exc)
        return {**result, "errors": 1}

    for row in rows:
        vid = row.get("id")
        channel_id = row.get("channel_id")
        try:
            channel = db.get_channel(channel_id) or {}
        except Exception:
            channel = {}
        slug = channel.get("slug") or f"channel_{channel_id}"
        if channel_slug and slug != channel_slug:
            continue

        result["scanned"] += 1
        title = row.get("titulo_final") or ""
        try:
            cfg = get_channel_config(slug)
        except Exception as exc:
            result["errors"] += 1
            logger.warning("title recovery: config error for %s: %s", slug, exc)
            continue

        current = _validate(cfg, title, row)
        if current.valid:
            candidate, titles = title, [title]
        else:
            guion, keywords = _script_context(db, row.get("script_id"))
            candidate, titles = None, []
            if use_llm and guion:
                candidate, titles = _candidate_from_llm(
                    cfg, guion, keywords, row, max_llm_attempts,
                )
            if not candidate:
                repaired = repair_title(title, cfg)
                if repaired and _validate(cfg, repaired, row).valid:
                    candidate, titles = repaired, [repaired]

        if not candidate:
            guion_exists = bool(row.get("script_id"))
            action = "manual" if not guion_exists else "skipped"
            result[action] += 1
            result["details"].append({
                "video_id": vid, "slug": slug, "action": action,
                "reasons": list(current.reasons),
            })
            continue

        if dry_run:
            result["retitled"] += 1
            result["details"].append({
                "video_id": vid, "slug": slug, "action": "would_retitle",
                "new_title": candidate,
            })
            continue

        try:
            db.update_video(
                vid,
                titulo_final=candidate,
                title_options=json.dumps(titles, ensure_ascii=False),
                status="awaiting_upload",
                progress=5,
                progress_phase="upload",
                scheduled_upload_at=None,
                error_message="Retitled by title recovery",
            )
        except Exception as exc:
            result["errors"] += 1
            logger.error("title recovery: failed to update #%s: %s", vid, exc)
            continue

        result["retitled"] += 1
        result["details"].append({
            "video_id": vid, "slug": slug, "action": "retitled",
            "new_title": candidate,
        })
        logger.warning("[%s] title recovery: #%s re-titled → %s", slug, vid, candidate[:60])

    if result["retitled"]:
        logger.info(
            "Title recovery: %d re-titled, %d skipped, %d manual, %d errors",
            result["retitled"], result["skipped"], result["manual"], result["errors"],
        )
    return result
