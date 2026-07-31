"""Pipeline validation — mandatory quality gates before upload.

Two-phase validation for the Autotube production pipeline:

  Pre-validation (after script, before TTS): Cheap sanity checks. Abort if
  critically broken — saves hours of TTS/media/render for doomed content.

  Post-validation (after metadata, before upload): Quality gate. Never
  discards videos. Blocking only for unrecoverable file issues. Duration
  out of range → warning (monitor investigates later). Metadata issues →
  auto-fix via LLM re-generation.

Architecture:
  - Built on top of the existing title_enricher.enforce_power_words()
    guarantee (deterministic, always succeeds).
  - Does NOT duplicate the enricher — it verifies the guarantee held
    (belt+suspenders) and re-calls it if a bug slipped through.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pipeline.title_enricher import enforce_power_words

logger = logging.getLogger(__name__)

# ── Pre-validation ───────────────────────────────────────────────────


@dataclass
class PreValidationResult:
    """Result of pre-validation (after script, before TTS)."""

    passed: bool
    checks: list[ValidationCheck] = field(default_factory=list)
    blocking_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ── Post-validation ──────────────────────────────────────────────────


@dataclass
class PostValidationResult:
    """Result of post-validation (after metadata, before upload)."""

    passed: bool
    checks: list[ValidationCheck] = field(default_factory=list)
    blocking_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    auto_fixes_applied: list[str] = field(default_factory=list)

    duration_seconds: Optional[float] = None
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)

    # Updated metadata after auto-fixes (caller should use these)
    updated_title: Optional[str] = None
    updated_description: Optional[str] = None
    updated_tags: Optional[list[str]] = None


# ── Individual check ─────────────────────────────────────────────────


@dataclass
class ValidationCheck:
    """Single validation check result."""

    name: str  # e.g. "file_exists", "duration_range", "title_power_word"
    passed: bool
    message: str
    severity: str = "blocking"  # "blocking" | "warning" | "autofix"
    auto_fixed: bool = False


# ── Main validator ───────────────────────────────────────────────────


class VideoValidator:
    """Validates pipeline output at two stages: pre (after script) and
    post (after metadata, before upload).

    Args:
        channel_config: Channel SimpleNamespace with at least:
            TITLE_POWER_WORDS (list[str])
            PROD_VIDEO_DURATION_MIN (int, minutes)
            PROD_VIDEO_DURATION_MAX (int, minutes)
    """

    def __init__(self, channel_config, *, dry_run: bool = False):
        self.config = channel_config
        self.dry_run = dry_run

        self.power_words: list[str] = [
            w.lower()
            for w in getattr(channel_config, "TITLE_POWER_WORDS", [])
        ]
        self.duration_min_sec: int = (
            getattr(channel_config, "PROD_VIDEO_DURATION_MIN", 10) * 60
        )
        self.duration_max_sec: int = (
            getattr(channel_config, "PROD_VIDEO_DURATION_MAX", 14) * 60
        )
        self.title_max_chars: int = getattr(channel_config, "TITLE_MAX_CHARS", 100)

    # ── Pre-validation (after script, before TTS) ──────────────────

    def pre_validate(self, script: dict) -> PreValidationResult:
        """Sanity checks before investing compute in TTS/media/render.

        BLOCKING: empty title, empty script body.
        WARNING: estimated duration outside config range.
        """
        checks: list[ValidationCheck] = []
        blocking: list[str] = []
        warnings: list[str] = []

        titulo = (script.get("titulo_selected") or script.get("titulo") or "").strip()

        # 1. Title not empty
        if not titulo:
            msg = "Script has no title — aborting before TTS/render"
            checks.append(
                ValidationCheck("title_not_empty", False, msg, "blocking")
            )
            blocking.append(msg)
        else:
            checks.append(
                ValidationCheck(
                    "title_not_empty", True,
                    f"Title present: '{titulo[:60]}'", "blocking",
                )
            )

        # 2. Script body has content
        guion = (script.get("guion") or "").strip()
        if not guion:
            msg = "Script body (guion) is empty — aborting before TTS/render"
            checks.append(
                ValidationCheck("guion_has_content", False, msg, "blocking")
            )
            blocking.append(msg)
        else:
            checks.append(
                ValidationCheck(
                    "guion_has_content", True,
                    f"Script body: {len(guion.split())} words", "blocking",
                )
            )

        # 3. Duration estimate in range (WARNING only — real duration depends on TTS)
        dur_est_min = script.get("duracion_estimada", 0)
        if isinstance(dur_est_min, (int, float)) and dur_est_min > 0:
            dur_est_sec = dur_est_min * 60
            if dur_est_sec < self.duration_min_sec:
                msg = (
                    f"Script duration estimate {dur_est_min:.0f} min below "
                    f"minimum {self.duration_min_sec // 60} min (warning — "
                    f"real duration depends on TTS)"
                )
                checks.append(
                    ValidationCheck(
                        "duration_estimate_range", False, msg, "warning",
                    )
                )
                warnings.append(msg)
            elif dur_est_sec > self.duration_max_sec:
                msg = (
                    f"Script duration estimate {dur_est_min:.0f} min above "
                    f"maximum {self.duration_max_sec // 60} min (warning — "
                    f"real duration depends on TTS)"
                )
                checks.append(
                    ValidationCheck(
                        "duration_estimate_range", False, msg, "warning",
                    )
                )
                warnings.append(msg)
            else:
                checks.append(
                    ValidationCheck(
                        "duration_estimate_range", True,
                        f"Duration estimate {dur_est_min:.0f} min in range "
                        f"[{self.duration_min_sec // 60}-{self.duration_max_sec // 60}]",
                        "warning",
                    )
                )
        else:
            msg = "Script has no duration estimate — cannot validate"
            checks.append(
                ValidationCheck(
                    "duration_estimate_range", False, msg, "warning",
                )
            )
            warnings.append(msg)

        passed = len(blocking) == 0

        return PreValidationResult(
            passed=passed,
            checks=checks,
            blocking_errors=blocking,
            warnings=warnings,
        )

    # ── Post-validation (after metadata, before upload) ────────────

    def post_validate(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: list[str],
        *,
        metadata_gen=None,
        script: dict = None,
    ) -> PostValidationResult:
        """Quality gate before upload. Never discards the video.

        BLOCKING: file missing, file corrupt/unreadable.
        WARNING: duration out of range (monitor investigates later).
        AUTO-FIX: missing description/tags (LLM regenerate).
        BELT+SUSPENDERS: verify enforce_power_words() guarantee.

        Args:
            video_path: Path to the rendered MP4 file.
            title: Final title (should already have power word via enricher).
            description: YouTube description text.
            tags: List of tag strings.
            metadata_gen: MetadataGenerator instance for auto-fixes.
            script: Original script dict for context if regen needed.

        Returns:
            PostValidationResult with checks, auto-fixes applied,
            and possibly updated title/description/tags.
        """
        checks: list[ValidationCheck] = []
        blocking: list[str] = []
        warnings: list[str] = []
        auto_fixes: list[str] = []

        updated_title = title
        updated_description = description
        updated_tags = list(tags)

        # ── 1. File exists ────────────────────────────────────────
        if not video_path or not os.path.isfile(video_path):
            msg = f"Video file not found on disk: {video_path or '(empty path)'}"
            checks.append(
                ValidationCheck("file_exists", False, msg, "blocking")
            )
            blocking.append(msg)
            return PostValidationResult(
                passed=False,
                checks=checks,
                blocking_errors=blocking,
                warnings=warnings,
                duration_seconds=None,
                title=updated_title,
                description=updated_description,
                tags=updated_tags,
            )

        file_size = os.path.getsize(video_path)
        if file_size == 0:
            msg = f"Video file is zero bytes: {video_path}"
            checks.append(
                ValidationCheck("file_exists", False, msg, "blocking")
            )
            blocking.append(msg)
        else:
            checks.append(
                ValidationCheck(
                    "file_exists", True,
                    f"Video file: {video_path} ({file_size / 1024 / 1024:.1f} MB)",
                    "blocking",
                )
            )

        # ── 2. File valid (ffprobe) ───────────────────────────────
        duration_sec = self._get_duration_ffprobe(video_path)
        has_video_stream = self._has_video_stream(video_path)

        if duration_sec is None or not has_video_stream:
            msg = (
                f"Video file is corrupt or unreadable (ffprobe): {video_path}"
            )
            checks.append(
                ValidationCheck("file_valid", False, msg, "blocking")
            )
            blocking.append(msg)
            return PostValidationResult(
                passed=False,
                checks=checks,
                blocking_errors=blocking,
                warnings=warnings,
                duration_seconds=None,
                title=updated_title,
                description=updated_description,
                tags=updated_tags,
            )

        checks.append(
            ValidationCheck(
                "file_valid", True,
                f"Valid video: {duration_sec / 60:.1f} min, "
                f"{file_size / 1024 / 1024:.1f} MB",
                "blocking",
            )
        )

        # ── 3. Duration range (WARNING only — NEVER blocks) ───────
        if duration_sec < self.duration_min_sec:
            msg = (
                f"Video duration {duration_sec / 60:.1f} min below "
                f"minimum {self.duration_min_sec / 60:.0f} min — "
                f"uploading anyway. Monitor should investigate cause."
            )
            checks.append(
                ValidationCheck("duration_range", False, msg, "warning")
            )
            warnings.append(msg)
        elif duration_sec > self.duration_max_sec:
            msg = (
                f"Video duration {duration_sec / 60:.1f} min above "
                f"maximum {self.duration_max_sec / 60:.0f} min — "
                f"uploading anyway. Monitor should investigate cause."
            )
            checks.append(
                ValidationCheck("duration_range", False, msg, "warning")
            )
            warnings.append(msg)
        else:
            checks.append(
                ValidationCheck(
                    "duration_range", True,
                    f"Duration {duration_sec / 60:.1f} min in range "
                    f"[{self.duration_min_sec // 60}-{self.duration_max_sec // 60}]",
                    "warning",
                )
            )

        # ── 4. Title present ──────────────────────────────────────
        if not updated_title or not updated_title.strip():
            msg = "Title is empty — attempting LLM auto-fix"
            checks.append(
                ValidationCheck(
                    "title_present", False, msg, "autofix",
                )
            )
            fixed = self._auto_fix_title(
                updated_title, script, metadata_gen,
            )
            if fixed:
                updated_title = fixed
                auto_fixes.append("title_regenerated")
                checks.append(
                    ValidationCheck(
                        "title_present", True,
                        f"Title regenerated: '{updated_title[:60]}'",
                        "autofix", auto_fixed=True,
                    )
                )
            else:
                blocking.append(msg)
        else:
            checks.append(
                ValidationCheck(
                    "title_present", True,
                    f"Title: '{updated_title[:60]}'", "blocking",
                )
            )

        # ── 5. Title power word (belt+suspenders) ─────────────────
        # enforce_power_words() already ran in metadata_gen.
        # This verifies the guarantee held. If not, re-call it.
        if updated_title and not self._any_power_word_in_title(
            updated_title
        ):
            logger.warning(
                "Power word guarantee violated for title '%s' — "
                "re-calling enforce_power_words()",
                updated_title[:80],
            )
            fixed = enforce_power_words(
                updated_title,
                self.power_words,
                max_chars=self.title_max_chars,
            )
            if fixed != updated_title:
                updated_title = fixed
                auto_fixes.append("title_power_word_enforced")
                checks.append(
                    ValidationCheck(
                        "title_power_word", True,
                        f"Power word enforced: '{updated_title[:60]}'",
                        "autofix", auto_fixed=True,
                    )
                )
            else:
                # enforce returned same title — no power word was injectable
                # (shouldn't happen due to brute-force fallback, but be safe)
                msg = (
                    f"Title missing power word and enricher could not fix: "
                    f"'{updated_title[:60]}'"
                )
                checks.append(
                    ValidationCheck(
                        "title_power_word", False, msg, "warning",
                    )
                )
                warnings.append(msg)
        elif updated_title:
            checks.append(
                ValidationCheck(
                    "title_power_word", True,
                    "Power word present in title", "autofix",
                )
            )
        else:
            checks.append(
                ValidationCheck(
                    "title_power_word", False,
                    "Cannot check — title is empty", "autofix",
                )
            )

        # ── 6. Description present ────────────────────────────────
        desc = (updated_description or "").strip()
        if not desc:
            msg = "Description is empty — attempting LLM auto-fix"
            checks.append(
                ValidationCheck(
                    "description_present", False, msg, "autofix",
                )
            )
            fixed_desc = self._auto_fix_description(
                script, metadata_gen,
            )
            if fixed_desc:
                updated_description = fixed_desc
                auto_fixes.append("description_regenerated")
                checks.append(
                    ValidationCheck(
                        "description_present", True,
                        f"Description regenerated: {len(fixed_desc)} chars",
                        "autofix", auto_fixed=True,
                    )
                )
            else:
                blocking.append(msg)
        elif len(desc) < 100:
            msg = f"Description too short ({len(desc)} chars) — attempting LLM auto-fix"
            checks.append(
                ValidationCheck(
                    "description_present", False, msg, "autofix",
                )
            )
            fixed_desc = self._auto_fix_description(
                script, metadata_gen,
            )
            if fixed_desc and len(fixed_desc) >= 100:
                updated_description = fixed_desc
                auto_fixes.append("description_regenerated")
                checks.append(
                    ValidationCheck(
                        "description_present", True,
                        f"Description regenerated: {len(fixed_desc)} chars",
                        "autofix", auto_fixed=True,
                    )
                )
            elif fixed_desc:
                updated_description = fixed_desc
                checks.append(
                    ValidationCheck(
                        "description_present", False,
                        f"Regenerated description still short: {len(fixed_desc)} chars",
                        "warning",
                    )
                )
                warnings.append(msg)
            else:
                blocking.append(msg)
        else:
            checks.append(
                ValidationCheck(
                    "description_present", True,
                    f"Description: {len(desc)} chars", "blocking",
                )
            )

        # ── 7. Tags present ───────────────────────────────────────
        if not updated_tags:
            msg = "No tags — attempting LLM auto-fix"
            checks.append(
                ValidationCheck(
                    "tags_present", False, msg, "autofix",
                )
            )
            fixed_tags = self._auto_fix_tags(script, metadata_gen)
            if fixed_tags and len(fixed_tags) >= 3:
                updated_tags = fixed_tags
                auto_fixes.append("tags_regenerated")
                checks.append(
                    ValidationCheck(
                        "tags_present", True,
                        f"Tags regenerated: {len(fixed_tags)} tags",
                        "autofix", auto_fixed=True,
                    )
                )
            elif fixed_tags:
                updated_tags = fixed_tags
                auto_fixes.append("tags_regenerated_partial")
                checks.append(
                    ValidationCheck(
                        "tags_present", False,
                        f"Regenerated tags: only {len(fixed_tags)} (minimum 3)",
                        "warning",
                    )
                )
                warnings.append(msg)
            else:
                blocking.append(msg)
        elif len(updated_tags) < 3:
            msg = f"Only {len(updated_tags)} tags (minimum 3) — attempting LLM auto-fix"
            checks.append(
                ValidationCheck(
                    "tags_present", False, msg, "autofix",
                )
            )
            fixed_tags = self._auto_fix_tags(script, metadata_gen)
            if fixed_tags and len(fixed_tags) >= 3:
                updated_tags = fixed_tags
                auto_fixes.append("tags_regenerated")
                checks.append(
                    ValidationCheck(
                        "tags_present", True,
                        f"Tags regenerated: {len(fixed_tags)} tags",
                        "autofix", auto_fixed=True,
                    )
                )
            else:
                updated_tags = fixed_tags or updated_tags
                warnings.append(msg)
        else:
            checks.append(
                ValidationCheck(
                    "tags_present", True,
                    f"{len(updated_tags)} tags", "blocking",
                )
            )

        passed = len(blocking) == 0

        return PostValidationResult(
            passed=passed,
            checks=checks,
            blocking_errors=blocking,
            warnings=warnings,
            auto_fixes_applied=auto_fixes,
            duration_seconds=duration_sec,
            title=updated_title,
            description=updated_description,
            tags=updated_tags,
            updated_title=updated_title if updated_title != title else None,
            updated_description=(
                updated_description
                if updated_description != description
                else None
            ),
            updated_tags=(
                updated_tags if updated_tags != tags else None
            ),
        )

    # ── ffprobe helpers ───────────────────────────────────────────

    @staticmethod
    def _get_duration_ffprobe(video_path: str) -> Optional[float]:
        """Get video duration in seconds via ffprobe. None on failure."""
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    video_path,
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            logger.warning("ffprobe duration failed for %s: %s", video_path, exc)
        return None

    @staticmethod
    def _has_video_stream(video_path: str) -> bool:
        """Check that the file has at least one video stream."""
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=codec_type",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    video_path,
                ],
                capture_output=True, text=True, timeout=30,
            )
            return "video" in (result.stdout or "").lower()
        except Exception:
            return False

    # ── Power word check ──────────────────────────────────────────

    def _any_power_word_in_title(self, title: str) -> bool:
        """Check if any power word appears in title (case-insensitive,
        word-boundary match)."""
        if not title or not self.power_words:
            return True  # nothing to enforce → not broken
        import re

        title_lower = title.lower()
        for word in self.power_words:
            pattern = (
                r"(?<![a-záéíóúüñA-ZÁÉÍÓÚÜÑ])"
                + re.escape(word)
                + r"(?![a-záéíóúüñA-ZÁÉÍÓÚÜÑ])"
            )
            if re.search(pattern, title_lower):
                return True
        return False

    # ── Auto-fix helpers ──────────────────────────────────────────

    def _auto_fix_title(
        self,
        title: str,
        script: dict | None,
        metadata_gen,
    ) -> Optional[str]:
        """Regenerate title via metadata_gen. Returns fixed title or None."""
        if script is None:
            logger.warning("Cannot auto-fix title — no script context")
            return None

        try:
            if metadata_gen is None:
                logger.error("Cannot auto-fix title — no metadata_gen")
                return None

            logger.info("Auto-fixing title via metadata_gen.generate()...")
            result = metadata_gen.generate(script)

            if result and result.get("selected_title"):
                new_title = result["selected_title"]
                # Enforce power words on the regenerated title
                new_title = enforce_power_words(
                    new_title,
                    self.power_words,
                    max_chars=self.title_max_chars,
                )
                logger.info("Auto-fixed title: '%s'", new_title[:60])
                return new_title
        except Exception as exc:
            logger.error("Auto-fix title failed: %s", exc)

        return None

    def _auto_fix_description(
        self,
        script: dict | None,
        metadata_gen,
    ) -> Optional[str]:
        """Regenerate description via metadata_gen. Returns fixed desc or None."""
        if script is None:
            logger.warning("Cannot auto-fix description — no script context")
            return None

        try:
            if metadata_gen is None:
                logger.error("Cannot auto-fix description — no metadata_gen")
                return None

            logger.info("Auto-fixing description via metadata_gen.generate()...")
            result = metadata_gen.generate(script)

            if result and result.get("description"):
                desc = result["description"]
                logger.info(
                    "Auto-fixed description: %d chars", len(desc),
                )
                return desc
        except Exception as exc:
            logger.error("Auto-fix description failed: %s", exc)

        return None

    def _auto_fix_tags(
        self,
        script: dict | None,
        metadata_gen,
    ) -> Optional[list[str]]:
        """Regenerate tags via metadata_gen. Returns fixed tags or None."""
        if script is None:
            logger.warning("Cannot auto-fix tags — no script context")
            return None

        try:
            if metadata_gen is None:
                logger.error("Cannot auto-fix tags — no metadata_gen")
                return None

            logger.info("Auto-fixing tags via metadata_gen.generate()...")
            result = metadata_gen.generate(script)

            if result and result.get("tags"):
                tags = result["tags"]
                if isinstance(tags, list) and len(tags) > 0:
                    logger.info("Auto-fixed tags: %d tags", len(tags))
                    return tags
        except Exception as exc:
            logger.error("Auto-fix tags failed: %s", exc)

        return None
