"""Post-generation script validator.

Evaluates a generated script for structural integrity, word count,
repetition, hook quality, and factual grounding. Produces a
ValidatorResult that determines whether the script passes, needs
minor autofix, or should be rejected (triggering model failover).

Usage:
    validator = ScriptValidator(canal_config)
    result = validator.validate(script_dict, word_target, content_item)
    if result.passes:
        ...  # script is good
    elif result.can_autofix:
        fixed = validator.autofix(script_dict, result)
    else:
        ...  # grave issues — discard and try next model
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)

# ── Thresholds ─────────────────────────────────────────────────────
WORD_COUNT_OK_RATIO = 0.85       # ≥85% of target = OK
WORD_COUNT_GRAVE_RATIO = 0.50    # <50% = grave (failover)
REPETITION_SIMILARITY_THRESHOLD = 0.65
MAX_REPETITION_PAIR_RATIO = 0.15  # max fraction of block pairs that can be repetitive
MAX_REPETITION_BLOCK_RATIO = 0.30  # max blocks involved in repetition
MIN_BLOCKS_FOR_COHERENCE = 5
BANNED_OPENING_PATTERNS = [
    r"en\s+este\s+video\s+(vamos|hablaremos|exploraremos|veremos)",
    r"hoy\s+(vamos|hablaremos|exploraremos|conoceremos|veremos)",
    r"bienvenidos?\s+a",
    r"en\s+el\s+video\s+de\s+hoy",
    r"te\s+(voy|vamos)\s+a\s+(contar|hablar|explicar)",
]
MIN_FACTS_FOR_PASS = 0  # minimum concrete facts from source (0 = skip this check)


@dataclass
class ValidatorResult:
    """Result of script validation."""

    passes: bool = True
    score: float = 1.0                # 0.0 (worst) to 1.0 (perfect)
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    # Sub-scores
    word_count_score: float = 1.0
    structural_score: float = 1.0
    repetition_score: float = 1.0
    hook_score: float = 1.0
    coherence_score: float = 1.0

    @property
    def is_grave(self) -> bool:
        """True if issues are severe enough to require failover."""
        return not self.passes and not self.can_autofix

    @property
    def can_autofix(self) -> bool:
        """True if issues can be fixed without changing model."""
        severe = {"word_count_grave", "no_blocks", "empty_guion"}
        return not severe.intersection(self.details.get("severe_issues", set()))


class ScriptValidator:
    """Validate a generated script dict for quality and structural integrity."""

    def __init__(self, canal_config=None):
        self.canal_config = canal_config

    def validate(
        self,
        script: dict,
        word_target: dict = None,
        content_item: dict = None,
    ) -> ValidatorResult:
        """Run all validation checks on a script dict.

        Args:
            script: Script dict with keys like 'guion', 'bloques', 'titulo_options', etc.
            word_target: Dict with words_min, words_max, duration_target (from ScriptGenerator).
            content_item: Raw content dict (title, text, source) for factual grounding check.

        Returns:
            ValidatorResult with scores and issue lists.
        """
        result = ValidatorResult()
        severe_issues: set[str] = set()

        # 1. Structural check
        structural = self._check_structure(script)
        result.structural_score = structural["score"]
        if structural["issues"]:
            result.issues.extend(structural["issues"])
            severe_issues.update(structural.get("severe", []))

        # 2. Word count check
        if word_target:
            wc = self._check_word_count(script, word_target)
            result.word_count_score = wc["score"]
            if wc["issues"]:
                result.issues.extend(wc["issues"])
                severe_issues.update(wc.get("severe", []))
            result.details["word_count"] = wc.get("actual", 0)
        else:
            result.word_count_score = 1.0

        # 3. Repetition check
        bloques = script.get("bloques", [])
        if bloques and len(bloques) >= 2:
            rep = self._check_repetition(bloques)
            result.repetition_score = rep["score"]
            if rep["issues"]:
                result.issues.extend(rep["issues"])
        else:
            result.repetition_score = 1.0

        # 4. Hook quality check
        hook = self._check_hook(script)
        result.hook_score = hook["score"]
        if hook["issues"]:
            result.warnings.extend(hook["issues"])  # hook issues are warnings, not failover

        # 5. Coherence check
        if bloques and len(bloques) >= MIN_BLOCKS_FOR_COHERENCE:
            coh = self._check_coherence(bloques)
            result.coherence_score = coh["score"]
            if coh["issues"]:
                result.issues.extend(coh["issues"])
        else:
            result.coherence_score = 1.0

        # Overall score (weighted average)
        weights = {"structural": 0.30, "word_count": 0.25, "repetition": 0.25,
                    "hook": 0.10, "coherence": 0.10}
        result.score = (
            result.structural_score * weights["structural"]
            + result.word_count_score * weights["word_count"]
            + result.repetition_score * weights["repetition"]
            + result.hook_score * weights["hook"]
            + result.coherence_score * weights["coherence"]
        )

        result.details["severe_issues"] = severe_issues
        result.passes = len(severe_issues) == 0

        if result.passes and result.warnings:
            logger.info(
                "Validator: PASS with %d warning(s): %s",
                len(result.warnings),
                "; ".join(result.warnings[:3]),
            )
        elif not result.passes:
            logger.warning(
                "Validator: FAIL — score=%.2f, severe=%s, issues=%s",
                result.score,
                severe_issues,
                "; ".join(result.issues[:5]),
            )

        return result

    # ── Individual checks ─────────────────────────────────────────

    def _check_structure(self, script: dict) -> dict:
        """Check required keys and non-empty fields."""
        issues = []
        severe = set()
        score = 1.0

        required = ["guion", "bloques", "titulo_options"]
        for key in required:
            if key not in script or not script[key]:
                msg = f"Missing or empty '{key}'"
                issues.append(msg)
                severe.add("empty_guion" if key == "guion" else "structural_missing")

        bloques = script.get("bloques", [])
        if not isinstance(bloques, list):
            issues.append("'bloques' is not a list")
            severe.add("structural_missing")
            score = 0.0
        elif len(bloques) == 0:
            issues.append("'bloques' is empty")
            severe.add("no_blocks")
            score = 0.0

        # Check block fields
        empty_texts = 0
        for i, b in enumerate(bloques):
            if not b.get("texto", "").strip():
                empty_texts += 1
        if empty_texts > len(bloques) * 0.3:
            issues.append(f"{empty_texts}/{len(bloques)} bloques have empty text")
            severe.add("too_many_empty_blocks")
            score = max(0, score - 0.5)

        return {"score": score, "issues": issues, "severe": severe}

    def _check_word_count(self, script: dict, word_target: dict) -> dict:
        """Check script word count against target."""
        issues = []
        severe = set()
        score = 1.0

        words_min = word_target.get("words_min", 500)
        guion = script.get("guion", "")
        actual = len(guion.split()) if guion else 0

        ratio = actual / max(1, words_min)

        if ratio < WORD_COUNT_GRAVE_RATIO:
            issues.append(
                f"Word count {actual} is {ratio:.0%} of minimum {words_min} "
                f"(grave: <{WORD_COUNT_GRAVE_RATIO:.0%})"
            )
            severe.add("word_count_grave")
            score = ratio / WORD_COUNT_GRAVE_RATIO  # proportional score
        elif ratio < WORD_COUNT_OK_RATIO:
            issues.append(
                f"Word count {actual} is {ratio:.0%} of minimum {words_min}"
            )
            score = ratio / WORD_COUNT_OK_RATIO
        else:
            score = min(1.0, ratio / 1.0)

        return {"score": score, "issues": issues, "severe": severe, "actual": actual}

    def _check_repetition(self, bloques: list[dict]) -> dict:
        """Check for repetitive content between consecutive blocks."""
        issues = []
        score = 1.0

        texts = [b.get("texto", "") for b in bloques]
        n = len(texts)
        flagged_pairs = 0
        flagged_blocks: set[int] = set()

        for i in range(n - 1):
            a, b = texts[i], texts[i + 1]
            if len(a) < 20 or len(b) < 20:
                continue
            similarity = SequenceMatcher(None, a.lower(), b.lower()).ratio()
            if similarity >= REPETITION_SIMILARITY_THRESHOLD:
                flagged_pairs += 1
                flagged_blocks.add(i)
                flagged_blocks.add(i + 1)

        total_pairs = max(1, n - 1)
        pair_ratio = flagged_pairs / total_pairs
        block_ratio = len(flagged_blocks) / max(1, n)

        if pair_ratio > MAX_REPETITION_PAIR_RATIO:
            issues.append(
                f"Repetition: {flagged_pairs}/{total_pairs} pairs flagged "
                f"({pair_ratio:.0%} > {MAX_REPETITION_PAIR_RATIO:.0%})"
            )
            score = max(0, 1.0 - pair_ratio * 2)
        elif block_ratio > MAX_REPETITION_BLOCK_RATIO:
            issues.append(
                f"Repetition: {len(flagged_blocks)}/{n} blocks involved"
            )
            score = max(0, 1.0 - block_ratio * 2)
        elif flagged_pairs > 0:
            # Minor repetition — warning level
            score = max(0.7, 1.0 - pair_ratio)

        return {"score": score, "issues": issues}

    def _check_hook(self, script: dict) -> dict:
        """Check if the opening hook uses banned patterns (warnings only)."""
        issues = []
        score = 1.0

        guion = script.get("guion", "")
        if not guion:
            return {"score": score, "issues": issues}

        # Get first meaningful sentence
        first_block_text = ""
        bloques = script.get("bloques", [])
        if bloques and bloques[0].get("texto"):
            first_block_text = bloques[0]["texto"]
        else:
            sentences = re.split(r"(?<=[.!?])\s+", guion.strip())
            first_block_text = sentences[0] if sentences else ""

        first_sentence = first_block_text.strip().lower()

        for pattern in BANNED_OPENING_PATTERNS:
            if re.search(pattern, first_sentence):
                issues.append(f"Weak hook: opening matches '{pattern}'")
                score = 0.5
                break

        return {"score": score, "issues": issues}

    def _check_coherence(self, bloques: list[dict]) -> dict:
        """Check narrative coherence between consecutive blocks."""
        issues = []
        score = 1.0

        texts = [b.get("texto", "") for b in bloques]
        gaps = 0
        for i in range(len(texts) - 1):
            a, b = texts[i], texts[i + 1]
            if len(a) < 10 or len(b) < 10:
                continue
            # Very low similarity + no shared keywords → possible coherence gap
            sim = SequenceMatcher(None, a.lower().split(), b.lower().split()).ratio()
            if sim < 0.03:
                gaps += 1

        if gaps > len(texts) * 0.3:
            issues.append(f"Coherence: {gaps} abrupt transitions detected")
            score = 0.7

        return {"score": score, "issues": issues}
