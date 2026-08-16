"""AI script generator for the Autotube pipeline.

Supports OpenAI (GPT-4o-mini) and DeepSeek (v3/v4) via OpenAI-compatible SDK.
Transforms scraped raw content into structured YouTube scripts
with scene markers, title options, and emotion annotations.
"""

import importlib
import json
import logging
import random
import re
import time
from difflib import SequenceMatcher
from typing import Optional

from config.llm_helpers import _extract_reasoning_content
from config.model_pool import ModelPool

from config.settings import (
    LLM_MODEL,
    LLM_MODEL_SCRIPT,
    LLM_POOL_RETRIES_PER_MODEL,
    LLM_PROVIDER,
    OPENAI_MAX_TOKENS,
    OPENAI_TEMPERATURE,
)
from database.db import Database

logger = logging.getLogger(__name__)

# Pricing per 1M tokens (USD) — adjust as providers update
PRICING = {
    "deepseek": {"input": 0.14, "output": 0.28},
    "openai": {"input": 0.15, "output": 0.60},
}
PRICE_INPUT_PER_M = PRICING.get(LLM_PROVIDER, PRICING["openai"])["input"]
PRICE_OUTPUT_PER_M = PRICING.get(LLM_PROVIDER, PRICING["openai"])["output"]

REQUIRED_JSON_KEYS = {
    "titulo_options",
    "guion",
    "escenas",
    "keywords",
    "duracion_estimada",
    # ── Campos regenerados por metadata_generator — NO bloquean ──
    # descripcion_seo, hashtags, fuentes_citadas, chapters, emociones
    # son regenerados desde cero por MetadataGenerator.generate().
    # Exigirlos aquí causaba descarte de guiones perfectos por campos
    # que nadie consume downstream. Ver OPTIONAL_METADATA_KEYS abajo.
}

OPTIONAL_METADATA_KEYS = {
    "descripcion_seo": "",
    "hashtags": [],
    "fuentes_citadas": [],
    "chapters": [],
    "emociones": [],
}

# Word count: voice-aware via config/voice_timing.py (single source of truth).
# Uses the channel's actual TTS voice rate, not a hardcoded WPM assumption.
from config.voice_timing import duration_for_words as _duration_for_words
WORD_COUNT_TOLERANCE = 0.15

# Multi-chunk: split if estimated output tokens > this fraction of max_tokens
MULTI_CHUNK_TOKEN_RATIO = 0.35

# Force multi-chunk for videos longer than this (minutes) regardless of token estimate.
# One-shot generations for >10 min scripts consistently produce too few words,
# so we split into chapters to keep each LLM call manageable.
FORCE_MULTI_CHUNK_MIN_DURATION = 8.0

# Retry: max attempts when word count is below target
MAX_WORD_COUNT_RETRIES = 3

# Safety margin for token estimation (chars → tokens is roughly chars/3 for Spanish)
TOKEN_CHAR_RATIO = 2.8

# Expansion loop: max rounds to grow a short script until it reaches words_min.
# Prevents infinite cost if the model repeatedly fails to expand.
# Set to 0 to disable expansion entirely.
MAX_EXPANSION_ROUNDS = 3
# Stale threshold: stop expansion if no growth for this many consecutive rounds
EXPANSION_STALE_ROUNDS = 2

# ── Narrative quality check thresholds ────────────────────
# Sentence similarity above this is considered repetitive
REPETITION_SIMILARITY_THRESHOLD = 0.60
# Max fraction of block pairs that can be flagged as repetitive
MAX_REPETITION_PAIR_RATIO = 0.15
# Max blocks overall that can be involved in repetition
MAX_REPETITION_BLOCK_RATIO = 0.30
# Minimum number of blocks for coherence check to apply
MIN_BLOCKS_FOR_COHERENCE_CHECK = 5
# Banned opening phrases that indicate weak hooks
BANNED_OPENING_PATTERNS = [
    r"en\s+este\s+video\s+(vamos|hablaremos|exploraremos|veremos)",
    r"hoy\s+(vamos|hablaremos|exploraremos|conoceremos|veremos)",
    r"bienvenidos?\s+a",
    r"en\s+el\s+video\s+de\s+hoy",
    r"te\s+(voy|vamos)\s+a\s+(contar|hablar|explicar)",
]

# ── Fault-tolerance: retry & content-structure validation ────
MAX_GENERATION_ATTEMPTS = 3
GENERATION_BACKOFF_SECONDS = [2.0, 4.0, 8.0]
API_CONNECTIVITY_CHECK_TIMEOUT = 5.0
MIN_NARRATIVE_BLOCKS = 5
SERIALIZED_ENDING_PATTERNS = [
    r"\b(?:el\s+)?pr[oó]xim[oa]\s+(?:video|episodio|cap[ií]tulo|caso|tema|historia|entrega)\b",
    r"\b(?:el|la)\s+siguiente\s+(?:video|episodio|cap[ií]tulo|caso|tema|historia|entrega)\b",
    r"\bno\s+te\s+(?:pierdas|olvides)\s+(?:el|la)\s+(?:pr[oó]xim[oa]|siguiente)\b",
]

# ── Lightweight language detection (no external deps) ──────────
ES_STOPWORDS = {'de', 'la', 'que', 'el', 'en', 'los', 'las', 'con', 'por',
                'para', 'una', 'como', 'más', 'pero', 'sus', 'fue', 'han',
                'muy', 'sin', 'del', 'entre', 'había', 'era', 'sido', 'este',
                'esta', 'cada', 'todo', 'historia', 'caso', 'años'}
EN_STOPWORDS = {'the', 'of', 'and', 'to', 'in', 'that', 'was', 'for', 'with',
                'had', 'this', 'from', 'but', 'they', 'his', 'her', 'were',
                'been', 'have', 'are', 'their', 'not', 'would', 'been'}


def _detect_text_language(text: str) -> str:
    """Heuristic language detection via stopword frequency ratio.
    
    Returns 'es', 'en', or 'unknown'. Handles texts with 0 stopwords
    gracefully. Not intended to be perfect — just catch obvious
    mismatches (e.g. English Wikipedia fed to Spanish channel).
    """
    if not text or len(text) < 50:
        return 'unknown'
    text_lower = text.lower()
    words = set(re.findall(r'\b\w+\b', text_lower))
    es_hits = len(words & ES_STOPWORDS)
    en_hits = len(words & EN_STOPWORDS)
    if es_hits > en_hits * 2:
        return 'es'
    elif en_hits > es_hits * 2:
        return 'en'
    elif es_hits > en_hits:
        return 'es'
    elif en_hits > es_hits:
        return 'en'
    return 'unknown'


def _dedup_blocks(bloques: list[dict]) -> list[dict]:
    """Remove near-duplicate narrative blocks using character-level similarity.

    Uses difflib.SequenceMatcher (gestalt pattern matching) to detect
    semantically identical or near-identical blocks across a sliding
    window. Threshold tuned for Spanish narrative text (75% match).
    Critical for marathon-scale generation (90+ blocks, 150+ batches)
    where the LLM may inadvertently rephrase previously covered content.

    Args:
        bloques: List of block dicts with 'texto' field.

    Returns:
        Deduplicated list preserving original order.
    """
    if len(bloques) < 3:
        return bloques

    kept = [bloques[0]]
    # Larger window for marathons, smaller for normal videos
    window_size = min(20, len(bloques))

    for blk in bloques[1:]:
        txt = blk.get("texto", "").strip().lower()
        if not txt or len(txt) < 30:
            # Skip very short blocks (transitions, one-liners)
            kept.append(blk)
            continue

        is_dup = False
        # Check against nearby blocks (sliding window)
        for prev in kept[-window_size:]:
            prev_txt = prev.get("texto", "").strip().lower()
            if not prev_txt or len(prev_txt) < 30:
                continue
            # Length ratio guard: skip if lengths differ by >3x
            if max(len(txt), len(prev_txt)) / min(len(txt), len(prev_txt)) > 3.0:
                continue
            sim = SequenceMatcher(None, txt, prev_txt).ratio()
            if sim > 0.75:
                is_dup = True
                logger.debug(
                    "_dedup_blocks: removed near-duplicate block "
                    "(similarity=%.2f): \"%s...\"",
                    sim, txt[:80],
                )
                break
        if not is_dup:
            kept.append(blk)

    return kept


class ScriptGenerator:
    """Generate YouTube narration scripts from raw content using AI (DeepSeek/OpenAI)."""

    def __init__(self, db: Database, canal_config):
        """Initialize the script generator.

        Args:
            db: Database instance for persistence.
            canal_config: Canal-specific config module (e.g. canal2_config).
        """
        self.db = db
        self.canal_config = canal_config
        self.canal = canal_config.CANAL_NAME

        # Multi-model pool with automatic failover (v22)
        self.model_pool = ModelPool.from_env()
        self._llm_retries = max(1, min(10, int(LLM_POOL_RETRIES_PER_MODEL)))
        self._llm_retry_delay = 2.0  # initial backoff seconds (doubles each retry)

        # Test harness: inject a mock client to bypass the pool in unit tests
        self._test_client = None
        self._test_client_no_thinking = None

        # Legacy clients kept for backward compat with non-pool callers (ThemeExtractor etc.)
        # These are used by _llm_json_call when no explicit client is passed.
        self._legacy_client = None
        self._legacy_client_no_thinking = None
        try:
            # Create a thinking client from the first pool entry as fallback
            first_entry = next(self.model_pool.iter_models())
            self._legacy_client = first_entry[1]  # client
        except (StopIteration, Exception):
            pass
        try:
            # Create a no-thinking client from the last pool entry as fallback
            # (use last entry since it's likely a fast/non-thinking model)
            all_entries = list(self.model_pool.iter_models())
            if all_entries:
                self._legacy_client_no_thinking = all_entries[-1][1]
        except Exception:
            pass

        # P2/P3: multi-chunk, theme context, word count emphasis
        self._theme_context = None
        self._word_count_emphasis = 1.0
        self._chunk_context = None

        # Unified prompts — parameterized by channel config
        # Replaces per-channel prompts/canal*_prompts.py imports
        from prompts.base_prompts import build_system_prompt, format_user_prompt
        self._build_system_prompt = build_system_prompt
        self._format_user_prompt = format_user_prompt

        logger.info(
            "ScriptGenerator initialized: provider=%s model=%s canal=%s",
            LLM_PROVIDER,
            LLM_MODEL,
            self.canal,
        )

    def _llm_json_call(self, thinking: bool = True, client=None, model_name: str = None, **call_kwargs):
        """Call LLM chat.completions.create and parse JSON with retry.

        Handles empty/invalid JSON responses from the API by retrying up
        to ``self._llm_retries`` times with exponential backoff.  Returns
        the parsed dict, or raises the last exception on total failure.

        Args:
            thinking: If True (default), prefer thinking-enabled client.
                If False, prefer no-thinking client.
            client: Explicit OpenAI client (from ModelPool). If None, uses
                legacy self.client / self._client_no_thinking.
            model_name: Name of the model being called (for logging).
        """
        if client is not None:
            effective_client = client
        elif thinking:
            effective_client = self._legacy_client
        else:
            effective_client = self._legacy_client_no_thinking

        if effective_client is None:
            raise RuntimeError("No LLM client available for JSON call")

        last_exc = None
        for attempt in range(self._llm_retries):
            try:
                response = effective_client.chat.completions.create(**call_kwargs)
                content = response.choices[0].message.content
                # DeepSeek thinking-mode fallback
                if not content:
                    reasoning = _extract_reasoning_content(response)
                    if reasoning:
                        content = reasoning
                        logger.debug(
                            "LLM fallback: using reasoning_content "
                            "(thinking mode, %d chars)", len(content)
                        )
                if content is None or not content.strip():
                    raise ValueError(
                        "LLM returned empty content (attempt %d/%d)" % (
                            attempt + 1, self._llm_retries,
                        )
                    )
                # raw_decode tolerates trailing text after valid JSON
                # (LLMs sometimes append explanations after closing brace)
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(content.strip())
                return obj
            except json.JSONDecodeError as exc:
                last_exc = exc
                if attempt < self._llm_retries - 1:
                    delay = self._llm_retry_delay * (2 ** attempt)
                    logger.warning(
                        "LLM JSON parse failed (%s, attempt %d/%d): %s — retrying in %.1fs",
                        model_name or "unknown", attempt + 1, self._llm_retries, exc, delay,
                    )
                    time.sleep(delay)
            except ValueError as exc:
                last_exc = exc
                if attempt < self._llm_retries - 1:
                    delay = self._llm_retry_delay * (2 ** attempt)
                    logger.warning(
                        "%s (%s) — retrying in %.1fs",
                        exc, model_name or "unknown", delay,
                    )
                    time.sleep(delay)
            except Exception as exc:
                last_exc = exc
                if attempt < self._llm_retries - 1:
                    delay = self._llm_retry_delay * (2 ** attempt)
                    logger.warning(
                        "LLM call failed (%s, attempt %d/%d): %s — retrying in %.1fs",
                        model_name or "unknown", attempt + 1, self._llm_retries, exc, delay,
                    )
                    time.sleep(delay)
        raise last_exc

    # ── Model pool failover (v22) ───────────────────────────────────

    @staticmethod
    @staticmethod
    def _classify_error(exc: Exception) -> str:
        """Classify an exception into a standard error_type for logging."""
        exc_name = type(exc).__name__
        msg = str(exc).lower()

        if isinstance(exc, json.JSONDecodeError):
            return "json_parse"
        if "empty content" in msg:
            return "empty_content"
        if exc_name in ("APITimeoutError", "Timeout", "ReadTimeout"):
            return "timeout"
        if exc_name in ("RateLimitError", "RateLimit"):
            return "rate_limit"
        if "validation" in msg:
            return "validation_failed"
        if exc_name in ("ConnectionError", "APIConnectionError"):
            return "connection_error"
        if exc_name == "ValidationError" or "schema" in msg:
            return "schema_error"
        return "exception"

    def _check_api_connectivity(self) -> tuple:
        """Verify LLM API reachability before attempting generation.

        Probes the base URL of each model in the pool with a short timeout.
        Returns (reachable: bool, detail: str).
        """
        import requests
        for entry, _ in self.model_pool.iter_models():
            try:
                url = entry.base_url.rstrip('/') + '/models'
                resp = requests.get(
                    url,
                    timeout=API_CONNECTIVITY_CHECK_TIMEOUT,
                    headers={'Authorization': f'Bearer {entry.api_key[:8]}***'},
                )
                if resp.status_code < 500:
                    logger.debug(
                        "API connectivity OK for %s (HTTP %d)",
                        entry.display_name, resp.status_code,
                    )
                    return True, entry.display_name
            except requests.Timeout:
                logger.warning(
                    "API connectivity TIMEOUT for %s", entry.display_name,
                )
            except requests.ConnectionError:
                logger.warning(
                    "API connectivity REFUSED for %s", entry.display_name,
                )
            except Exception as exc:
                logger.warning(
                    "API connectivity error for %s: %s",
                    entry.display_name, exc,
                )
        return False, "all models unreachable"

    def _validate_content_structure(self, script: dict) -> tuple:
        """Validate script content structure for minimum quality requirements.

        Checks:
          1. Hook block contains numeric / quantitative data (NON-BLOCKING).
          2. At least MIN_NARRATIVE_BLOCKS narrative / development blocks.
          3. Ending does not serialize the current video into a next-content teaser.

        Returns (valid: bool, issues: list[str], warnings: list[str]).
        """
        if not script or not script.get('bloques'):
            return False, ["no blocks in script"], []

        bloques = script['bloques']
        issues = []
        warnings = []

        # 1. Hook should contain numeric/quantitative data (non-blocking)
        first_text = bloques[0].get('texto', '') if bloques else ''
        if not re.search(r'\d+', first_text):
            warnings.append("hook block missing numeric / quantitative data (non-blocking)")

        # 2. At least MIN_NARRATIVE_BLOCKS development blocks
        narrative_types = {
            'desarrollo', 'suceso', 'climax', 'explicacion', 'reflexion',
            'contexto', 'protagonistas', 'hechos', 'momento_cumbre',
            'gancho', 'hook',
        }
        narrative_blocks = [
            b for b in bloques
            if b.get('tipo', 'desarrollo').lower() in narrative_types
        ]
        if len(narrative_blocks) < MIN_NARRATIVE_BLOCKS:
            issues.append(
                f"only {len(narrative_blocks)} narrative blocks "
                f"(need >= {MIN_NARRATIVE_BLOCKS})"
            )

        # 3. A reflective closure is sufficient. Reject serialized teasers
        # so retries can produce an ending that resolves this video's arc.
        ending_text = ' '.join(
            block.get('texto', '') for block in bloques[-2:]
            if isinstance(block, dict)
        )
        cta_text = ''
        cta = script.get('cta')
        if isinstance(cta, dict):
            cta_text = cta.get('texto', '')
        combined_end = (ending_text + ' ' + cta_text).lower()
        if any(re.search(pattern, combined_end) for pattern in SERIALIZED_ENDING_PATTERNS):
            issues.append("serialized next-content teaser in ending")

        # 4. Language check: the generated script must be in the
        #    channel language. If the LLM outputs English (because the
        #    source was English and the prompt wasn't explicit enough),
        #    catch it here so the retry loop can correct it.
        channel_lang = getattr(self.canal_config, 'LANGUAGE', 'es')
        if channel_lang:
            guion_text = script.get('guion', '')
            if not guion_text:
                guion_text = ' '.join(
                    b.get('texto', '') for b in bloques
                )
            script_lang = _detect_text_language(guion_text)
            if script_lang and script_lang != channel_lang:
                issues.append(
                    f"script language is '{script_lang}' "
                    f"(expected '{channel_lang}')"
                )

        return len(issues) == 0, issues, warnings

    def _record_phase_metric(
        self, phase: str, success: bool,
        error_type: str = None, duration_ms: int = 0,
        details: dict = None,
    ):
        """Record per-phase metrics for error-rate tracking and monitoring.

        Accumulates counters in ``self._phase_metrics`` and persists
        individual attempts to ``script_generation_attempts`` for
        historical analysis via the Monitor panel.
        """
        if not hasattr(self, '_phase_metrics'):
            self._phase_metrics = {}
        m = self._phase_metrics.setdefault(
            phase, {'attempts': 0, 'failures': 0, 'errors': {}},
        )
        m['attempts'] += 1
        if not success:
            m['failures'] += 1
            if error_type:
                m['errors'][error_type] = \
                    m['errors'].get(error_type, 0) + 1

        try:
            self.db.log_generation_attempt(
                canal=self.canal,
                model_name="retry_wrapper",
                attempt_number=1,
                pool_position=0,
                success=success,
                error_type=error_type or ("ok" if success else "unknown"),
                error_message=str(details) if details else "",
                phase=f"retry_{phase}",
                duration_ms=duration_ms,
            )
        except Exception:
            pass

    def _call_with_failover(
        self,
        phase: str = "blocks",
        thinking: bool = True,
        video_id: int = None,
        content_id: int = None,
        **call_kwargs,
    ) -> dict:
        """Call LLM with automatic model-pool failover and structured logging.

        Iterates over the model pool. For each model, retries up to
        ``self._llm_retries`` times. On failure, logs the attempt and
        moves to the next model. If all models fail, raises the last
        exception.

        Test mode: if ``self._test_client`` is set, bypasses the pool
        entirely and uses the injected mock client.
        """
        # ── Test harness bypass ──────────────────────────────
        test_client = self._test_client or getattr(self, 'client', None)
        test_client_no_thinking = self._test_client_no_thinking or getattr(self, '_client_no_thinking', None)
        if test_client is not None or test_client_no_thinking is not None:
            client = test_client if thinking else (test_client_no_thinking or test_client)
            return self._llm_json_call(
                thinking=thinking,
                client=client,
                model_name="test-mock",
                **call_kwargs,
            )

        pool_position = 0
        last_error = None

        for entry, client in self.model_pool.iter_models():
            model_name = entry.display_name
            logger.info(
                "Failover: trying model %s (position %d, phase=%s)",
                model_name, pool_position, phase,
            )

            # Inject the correct model_id for this pool entry so that
            # OpenAI-compatible clients receive their actual model name
            # rather than the hardcoded LLM_MODEL_SCRIPT from call sites.
            call_kwargs["model"] = entry.model_id

            for attempt_num in range(1, self._llm_retries + 1):
                t0 = time.time()
                try:
                    result = self._llm_json_call(
                        thinking=thinking,
                        client=client,
                        model_name=model_name,
                        **call_kwargs,
                    )
                    duration_ms = int((time.time() - t0) * 1000)

                    # Log success
                    try:
                        self.db.log_generation_attempt(
                            canal=self.canal,
                            model_name=entry.model_id,
                            attempt_number=attempt_num,
                            pool_position=pool_position,
                            success=True,
                            phase=phase,
                            video_id=video_id,
                            content_id=content_id,
                            duration_ms=duration_ms,
                        )
                    except Exception:
                        pass

                    logger.info(
                        "Failover: %s succeeded (attempt %d, %.1fs)",
                        model_name, attempt_num, duration_ms / 1000.0,
                    )
                    return result

                except Exception as exc:
                    duration_ms = int((time.time() - t0) * 1000)
                    error_type = self._classify_error(exc)
                    error_msg = str(exc)[:500]
                    last_error = exc

                    # Log failure
                    try:
                        self.db.log_generation_attempt(
                            canal=self.canal,
                            model_name=entry.model_id,
                            attempt_number=attempt_num,
                            pool_position=pool_position,
                            success=False,
                            error_type=error_type,
                            error_message=error_msg,
                            phase=phase,
                            video_id=video_id,
                            content_id=content_id,
                            duration_ms=duration_ms,
                        )
                    except Exception:
                        pass

                    logger.warning(
                        "Failover: %s attempt %d/%d FAILED (%s): %s",
                        model_name, attempt_num, self._llm_retries,
                        error_type, error_msg[:120],
                    )

            # Model exhausted — move to next
            logger.warning(
                "Failover: %s exhausted (%d attempts) — model FAILED",
                model_name, self._llm_retries,
            )
            pool_position += 1

        raise RuntimeError(
            f"All {len(self.model_pool)} model(s) in pool failed for phase '{phase}'. "
            f"Last error: {last_error}"
        )

    # ── Top-level retry wrapper with content validation ─────────────

    def _generate_with_retry(
        self, content_item: dict, palabras_objetivo: int = None,
        marathon_overrides: dict = None,
    ) -> Optional[dict]:
        """Generate a script with pre-flight checks, retry, and content validation.

        Up to ``MAX_GENERATION_ATTEMPTS`` attempts with exponential backoff
        (2s / 4s / 8s). Each attempt:
          1. Verifies API connectivity.
          2. Calls ``generate_v2()`` (which internally uses model-pool failover).
          3. Validates script content structure (hook with data, narrative
             blocks, end hook).

        Falls back to ``_generate_emergency_script`` if all attempts fail.
        Returns the enriched script dict or None.
        """
        content_id = content_item.get('id')

        for attempt in range(MAX_GENERATION_ATTEMPTS):
            # ── Pre-check: API connectivity ──────────────────
            reachable, detail = self._check_api_connectivity()
            if not reachable:
                logger.warning(
                    "generate_with_retry: API unreachable "
                    "(attempt %d/%d): %s",
                    attempt + 1, MAX_GENERATION_ATTEMPTS, detail,
                )
                self._record_phase_metric(
                    "precheck", False, "api_unreachable",
                )
                if attempt < MAX_GENERATION_ATTEMPTS - 1:
                    time.sleep(GENERATION_BACKOFF_SECONDS[attempt])
                    continue
                break

            # ── Generate ─────────────────────────────────────
            gen_start = time.time()
            try:
                script = self.generate_v2(
                    content_item, palabras_objetivo=palabras_objetivo,
                    marathon_overrides=marathon_overrides,
                )
            except Exception as exc:
                gen_ms = int((time.time() - gen_start) * 1000)
                logger.error(
                    "generate_with_retry: generation crashed "
                    "(attempt %d/%d): %s",
                    attempt + 1, MAX_GENERATION_ATTEMPTS, exc,
                )
                self._record_phase_metric(
                    "generation", False,
                    self._classify_error(exc), gen_ms,
                )
                if attempt < MAX_GENERATION_ATTEMPTS - 1:
                    time.sleep(GENERATION_BACKOFF_SECONDS[attempt])
                    continue
                break

            if script is None:
                logger.warning(
                    "generate_with_retry: generation returned None "
                    "(attempt %d/%d)",
                    attempt + 1, MAX_GENERATION_ATTEMPTS,
                )
                if attempt < MAX_GENERATION_ATTEMPTS - 1:
                    time.sleep(GENERATION_BACKOFF_SECONDS[attempt])
                    continue
                break

            # ── Content-structure validation ─────────────────
            valid, issues, warnings = self._validate_content_structure(script)
            if warnings:
                logger.info(
                    "generate_with_retry: content validation warnings: %s",
                    "; ".join(warnings),
                )
            if valid:
                logger.info(
                    "generate_with_retry: PASSED on attempt %d/%d",
                    attempt + 1, MAX_GENERATION_ATTEMPTS,
                )
                return script

            logger.warning(
                "generate_with_retry: content validation FAILED "
                "(attempt %d/%d): %s",
                attempt + 1, MAX_GENERATION_ATTEMPTS,
                "; ".join(issues),
            )
            self._record_phase_metric(
                "validation", False, "content_structure",
                details={"issues": issues, "warnings": warnings},
            )

            if attempt < MAX_GENERATION_ATTEMPTS - 1:
                time.sleep(GENERATION_BACKOFF_SECONDS[attempt])

        # ── All attempts exhausted ───────────────────────────
        logger.warning(
            "generate_with_retry: ALL %d attempts failed for "
            "content_id=%s — using emergency fallback",
            MAX_GENERATION_ATTEMPTS, content_id,
        )

        # Emit phase-metrics summary for monitoring
        if hasattr(self, '_phase_metrics') and self._phase_metrics:
            parts = []
            for ph, m in self._phase_metrics.items():
                ok = m['attempts'] - m['failures']
                parts.append(
                    f"{ph}={ok}/{m['attempts']} ok"
                )
                if m['errors']:
                    err_detail = ", ".join(
                        f"{k}={v}" for k, v in m['errors'].items()
                    )
                    parts[-1] += f" ({err_detail})"
            logger.info("Phase metrics: %s", "; ".join(parts))

        # ── Use marathon word_target if available, otherwise channel default ──
        if marathon_overrides and marathon_overrides.get("word_target_override"):
            emergency_wt = marathon_overrides["word_target_override"]
        else:
            emergency_wt = self._get_word_target()

        return self._generate_emergency_script(
            content_item, emergency_wt,
        )

    # ── Single-model call (bypasses pool for targeted retries) ──────

    def _call_llm_single_model(
        self, provider: str, model_id: str, phase: str = "emergency",
        thinking: bool = False, video_id: int = None,
        content_id: int = None, **call_kwargs,
    ) -> dict:
        """Call a single specific model, bypassing the pool entirely.

        Used by the emergency language retry chain to target specific
        models (GPT → DeepSeek → GPT → DeepSeek) instead of relying
        on the default pool ordering.
        """
        from config.model_pool import ModelPool
        entry = ModelPool._build_entry(provider, model_id)
        if not entry:
            raise ValueError(
                f"Cannot build model entry for {provider}:{model_id}"
            )
        client = ModelPool(entries=[]).create_client(entry)
        model_name = entry.display_name

        t0 = time.time()
        result = self._llm_json_call(
            thinking=thinking,
            client=client,
            model_name=model_name,
            **call_kwargs,
        )
        duration_ms = int((time.time() - t0) * 1000)

        # Log the success
        try:
            self.db.log_generation_attempt(
                canal=self.canal,
                model_name=model_id,
                attempt_number=1,
                pool_position=0,
                success=True,
                phase=phase,
                video_id=video_id,
                content_id=content_id,
                duration_ms=duration_ms,
            )
        except Exception:
            pass

        logger.info(
            "Single-model call: %s succeeded (%.1fs, phase=%s)",
            model_name, duration_ms / 1000.0, phase,
        )
        return result

    # ── Emergency script generation ─────────────────────────────────

    def _generate_emergency_script(
        self, content_item: dict, word_target: dict
    ) -> Optional[dict]:
        """Emergency fallback: LLM-based generation first, raw chunking last.

        When all normal LLM attempts fail validation, this makes a final
        attempt with a simplified emergency prompt.  If the LLM produces
        English (wrong language), up to 4 additional retries are made
        with alternating models (GPT → DeepSeek → GPT → DeepSeek) and
        progressively stronger language enforcement.

        Only if ALL LLM attempts fail (including language retries) does
        raw text chunking kick in as absolute last resort.

        Marked with ``emergency_mode=True`` in the scripts table.
        """
        logger.warning(
            "EMERGENCY MODE: generating fallback script for content_id=%s",
            content_item.get("id"),
        )

        title = content_item.get("title", "Historia Increíble")
        text = content_item.get("text", "")
        target_words = (
            word_target.get("palabras_objetivo", 1500) if word_target else 1500
        )

        if not title and not text:
            logger.error(
                "EMERGENCY: no title or text in content — cannot generate"
            )
            return None

        # ── Attempt 1: LLM-based emergency generation (pool) ──────
        try:
            script = self._emergency_llm_generate(content_item, target_words)
            if script:
                script["emergency_mode"] = True
                logger.info(
                    "EMERGENCY: LLM fallback succeeded — %d blocks",
                    len(script.get('bloques', [])),
                )
                return script
        except Exception as exc:
            logger.error("EMERGENCY: LLM fallback crashed: %s", exc)
            if hasattr(self, '_phase_metrics'):
                m = self._phase_metrics.setdefault(
                    'emergency', {'attempts': 0, 'failures': 0, 'errors': {}}
                )
                m['attempts'] += 1
                m['failures'] += 1
                m['errors']['llm_crash'] = \
                    m['errors'].get('llm_crash', 0) + 1

        # ── Language retry chain: 4 attempts with model alternation ──
        # If the first attempt failed because of language (English output),
        # retry with specific models one at a time.  Each model gets a
        # stronger language enforcement preamble.
        LANGUAGE_RETRY_CHAIN = [
            ("openai", "gpt-4o-mini"),     # Retry 1: GPT
            ("deepseek", "deepseek-chat"), # Retry 2: DeepSeek
            ("openai", "gpt-4o-mini"),     # Retry 3: GPT (alt temp)
            ("deepseek", "deepseek-chat"), # Retry 4: DeepSeek (alt temp)
        ]

        for retry_idx, (provider, model_id) in enumerate(LANGUAGE_RETRY_CHAIN):
            full_model_id = f"{provider}:{model_id}"
            logger.warning(
                "EMERGENCY: language retry %d/4 — trying %s",
                retry_idx + 1, full_model_id,
            )

            try:
                script = self._emergency_llm_generate(
                    content_item, target_words, model_id=full_model_id,
                )
                if script:
                    script["emergency_mode"] = True
                    logger.info(
                        "EMERGENCY: language retry %d SUCCEEDED (%s) — "
                        "%d blocks",
                        retry_idx + 1, full_model_id,
                        len(script.get('bloques', [])),
                    )
                    return script
                else:
                    logger.warning(
                        "EMERGENCY: language retry %d FAILED (%s) — "
                        "wrong language or empty output",
                        retry_idx + 1, full_model_id,
                    )
            except Exception as exc:
                logger.error(
                    "EMERGENCY: language retry %d crashed (%s): %s",
                    retry_idx + 1, full_model_id, exc,
                )
                if hasattr(self, '_phase_metrics'):
                    m = self._phase_metrics.setdefault(
                        'emergency', {'attempts': 0, 'failures': 0, 'errors': {}}
                    )
                    m['attempts'] += 1
                    m['failures'] += 1
                    m['errors']['lang_retry_crash'] = \
                        m['errors'].get('lang_retry_crash', 0) + 1

        # ── All language retries exhausted ─────────────────────────
        logger.error(
            "EMERGENCY: ALL %d language retries failed — "
            "falling through to raw chunking as absolute last resort",
            len(LANGUAGE_RETRY_CHAIN),
        )

        # ── Attempt 2: raw text chunking (absolute last resort) ────
        # This will go through its own language guard before returning.
        return self._emergency_raw_chunk(content_item, target_words)

    def _emergency_llm_generate(
        self, content_item: dict, target_words: int,
        model_id: str = None,
    ) -> Optional[dict]:
        """Generate an emergency script via LLM with a simplified prompt.

        Unlike the normal generation flow, this uses a single-shot prompt
        that asks the LLM for the complete script in one response.  Less
        sophisticated than the batched V2 approach, but much more likely
        to produce a valid script when the normal flow is stuck.

        If *model_id* is provided (e.g. ``"openai:gpt-4o-mini"``), the
        call bypasses the pool entirely and targets that specific model.
        This is used by the language retry chain.
        """
        title = content_item.get("title", "")
        text = content_item.get("text", "")

        # ── Language preamble: stronger when this is a retry ───────
        if model_id:
            lang_preamble = (
                "⚠️ CRÍTICO — LEE ESTO PRIMERO:\n"
                "TODO el guion DEBE estar EXCLUSIVAMENTE en español "
                "latinoamericano neutro. PROHIBIDO escribir NINGUNA "
                "frase en inglés. PROHIBIDO usar texto de la fuente "
                "sin traducir. Si la fuente está en inglés, TRADÚCELA "
                "completamente al español. Repito: SOLO ESPAÑOL.\n\n"
            )
        else:
            lang_preamble = (
                "Eres un guionista de documentales en español "
                "latinoamericano neutro. "
            )

        emergency_prompt = (
            f"{lang_preamble}"
            "Genera un guion documental COMPLETO basado en esta "
            "fuente:\n\n"
            f"TÍTULO: {title}\n"
            f"FUENTE: {text[:3000]}\n\n"
            "REQUISITOS OBLIGATORIOS:\n"
            "1. Hook inicial intrigante con un dato numérico concreto.\n"
            "2. Al menos 5 bloques narrativos de desarrollo con hechos.\n"
            "3. Un cierre reflexivo que resuelva el arco de este video. "
            "No anuncies ni anticipes el próximo/siguiente video, episodio o entrega.\n"
            "4. Un CTA final invitando a suscribirse al canal.\n"
            "5. IDIOMA: TODO el guion en español latinoamericano neutro. "
            "Nada de vosotros, os, conjugaciones ibéricas.\n"
            f"6. Extensión objetivo: ~{target_words} palabras.\n\n"
            "Responde ÚNICAMENTE con un JSON con este formato:\n"
            '{"guion": "texto completo del guion", '
            '"bloques": [{"texto": "bloque 1"}, {"texto": "bloque 2"}, ...]}'
        )

        # ── Call: single-model or pool ───────────────────────────
        try:
            if model_id:
                provider, mid = model_id.split(":", 1)
                result = self._call_llm_single_model(
                    provider=provider,
                    model_id=mid,
                    phase="emergency",
                    thinking=False,
                    messages=[
                        {"role": "user", "content": emergency_prompt},
                    ],
                    temperature=0.8,
                    max_tokens=min(4000, OPENAI_MAX_TOKENS),
                )
            else:
                result = self._call_with_failover(
                    phase="emergency",
                    thinking=False,
                    messages=[
                        {"role": "user", "content": emergency_prompt},
                    ],
                    temperature=0.8,
                    max_tokens=min(4000, OPENAI_MAX_TOKENS),
                )
        except Exception as exc:
            logger.error("EMERGENCY: LLM call failed: %s", exc)
            return None

        if not result or not result.get("bloques"):
            logger.warning("EMERGENCY: LLM returned no blocks")
            return None

        # ── Language validation ──────────────────────────────────
        guion_text = result.get("guion", "")
        if not guion_text:
            guion_text = " ".join(
                b.get("texto", "") for b in result.get("bloques", [])
            )
        script_lang = _detect_text_language(guion_text)
        channel_lang = getattr(self.canal_config, "LANGUAGE", "es")
        if script_lang and script_lang != channel_lang:
            logger.warning(
                "EMERGENCY: script language is '%s' (expected '%s') — "
                "rejecting, will retry with next model",
                script_lang, channel_lang,
            )
            return None  # caller will retry with another model

        # Enrich blocks (with failover — fall back to raw if enrichment fails)
        script = self._enrich_blocks(result["bloques"], content_item, {"palabras_objetivo": target_words})
        if script:
            script["emergency_mode"] = True
            return script

        return self._build_raw_script(result["bloques"], content_item, title, {"palabras_objetivo": target_words})

    def _emergency_raw_chunk(
        self, content_item: dict, target_words: int
    ) -> Optional[dict]:
        """Absolute last resort: build a script by chunking raw source text.

        This is the legacy emergency generator preserved as a safety net.
        It builds blocks by splitting the source text into word chunks and
        wrapping them with hardcoded Spanish scaffolding (hook, reflection,
        CTA).

        Includes a language gate: if the body blocks are predominantly in
        English (i.e. raw Wikipedia text), the function refuses to return
        the script — an unintelligible English narration read by a Spanish
        TTS voice is worse than no video at all.
        """
        import re as _re

        title = content_item.get("title", "Historia Increíble")
        text = content_item.get("text", "")

        # ── Scale body extraction to target word count ────────
        sentences = _re.split(r"(?<=[.!?])\s+", text.strip())
        if target_words <= 2000:
            max_body_sentences = 12
            chunk_size = max(60, min(120, target_words // 7))
        elif target_words <= 5000:
            max_body_sentences = 30
            chunk_size = max(90, min(180, target_words // 15))
        else:
            max_body_sentences = 60
            chunk_size = max(120, min(250, target_words // 25))

        intro = (
            " ".join(sentences[:2])
            if len(sentences) >= 2 else text[:300]
        )
        body = (
            " ".join(sentences[2:min(max_body_sentences, len(sentences))])
            if len(sentences) > 2 else ""
        )
        if not body:
            body = text[:3000] if len(text) > 300 else ""

        bloques = []

        # ── 1. Hook with numeric data ────────────────────────
        numbers = _re.findall(r'\b\d+\b', intro + " " + title)
        if numbers:
            num = numbers[0]
            hook_text = (
                f"Exactamente {num}. Esa es la cifra que cambió "
                f"para siempre la forma en que entendemos este caso. "
                f"{intro[:200]}"
            )
        else:
            hook_text = (
                f"Hay historias que desafían toda explicación lógica. "
                f"Esta es una de ellas. {intro[:220]}"
            )
            if len(hook_text) < 80:
                hook_text = (
                    f"Los datos hablan por sí solos. Cada fuente "
                    f"confirma lo que parecía imposible. "
                    f"{intro[:200]}"
                )
        bloques.append({"texto": hook_text})

        # ── 2. Body blocks (at least MIN_NARRATIVE_BLOCKS) ───
        body_words = body.split()
        chunks = []
        for i in range(0, len(body_words), chunk_size):
            chunk = " ".join(body_words[i:i + chunk_size])
            if chunk.strip():
                chunks.append({"texto": chunk})

        min_blocks = max(MIN_NARRATIVE_BLOCKS, target_words // 250)
        if len(chunks) < min_blocks:
            fallback_padding = [
                "Lo que hace único este caso es la combinación de factores que lo rodean.",
                "Cada elemento, por separado, podría tener una explicación convencional.",
                "Pero juntos forman un patrón que desafía las probabilidades.",
                "Las fuentes documentales confirman los hechos principales sin margen de error.",
                "Aunque algunos detalles siguen siendo motivo de debate entre expertos.",
                "Los investigadores han dedicado años a desentrañar cada pieza de este rompecabezas.",
                "Nuevos datos siguen apareciendo, añadiendo capas de complejidad al caso.",
                "Lo que comenzó como una simple observación se convirtió en un fenómeno documentado.",
                "La comunidad científica sigue dividida sobre la interpretación de estos hallazgos.",
                "Cada nueva investigación aporta un ángulo distinto que enriquece el debate.",
            ]
            needed = min_blocks - len(chunks)
            for i in range(min(needed, len(fallback_padding))):
                chunks.append({"texto": fallback_padding[i]})

        bloques.extend(chunks)

        # ── 3. Reflective closure for this video ──────────────
        bloques.append({
            "texto": (
                "Este caso recuerda que incluso los hechos mejor documentados "
                "pueden dejar preguntas abiertas."
            ),
        })

        # ── 4. CTA ───────────────────────────────────────────
        outro = getattr(
            self.canal_config, "CANAL_OUTRO_TAGLINE",
            "Suscríbete para más historias increíbles.",
        )
        bloques.append({"texto": outro})

        # ── Language gate: refuse to return raw English chunks ──
        # The body blocks contain raw source text.  If that source is
        # English (e.g. Wikipedia), a Spanish TTS voice reading English
        # produces an unintelligible video.  Detect the body language
        # and bail out if it's predominantly English.
        body_text = " ".join(
            b.get("texto", "") for b in chunks if b.get("texto", "")
        )
        if len(body_text) > 200:
            body_lang = _detect_text_language(body_text)
            channel_lang = getattr(self.canal_config, "LANGUAGE", "es")
            if body_lang and body_lang != channel_lang:
                logger.error(
                    "EMERGENCY (raw): body language is '%s' (expected '%s') "
                    "— refusing to produce raw English script for '%s'",
                    body_lang, channel_lang, title[:60],
                )
                return None  # discard — partial Spanish scaffolding + English body is worse than nothing

        total_words = sum(
            len(b.get("texto", "").split()) for b in bloques
        )
        logger.info(
            "EMERGENCY (raw): generated %d blocks, %d words for '%s'",
            len(bloques), total_words, title[:60],
        )

        # Enrich blocks (with failover — fall back to raw if enrichment fails)
        word_target_dict = {"palabras_objetivo": target_words}
        script = self._enrich_blocks(bloques, content_item, word_target_dict)
        if script:
            script["emergency_mode"] = True
            return script

        return self._build_raw_script(bloques, content_item, title, word_target_dict)

    def _build_raw_script(
        self, bloques: list[dict], content_item: dict, title: str, word_target: dict
    ) -> dict:
        """Build a minimal script dict from raw blocks (no enrichment)."""
        guion = "\n\n".join(b.get("texto", "") for b in bloques)
        duration = max(2, round(len(guion.split()) / 150))
        return {
            "titulo_options": [title[:100]],
            "titulo_selected": title[:100],
            "guion": guion,
            "bloques": bloques,
            "bloques_json": bloques,
            "escenas": [],
            "escenas_json": [],
            "emociones": [],
            "keywords": [],
            "hashtags": [],
            "duracion_estimada": duration,
            "chapters": [],
            "fuentes_citadas": [],
            "palabras_reales": len(guion.split()),
            "emergency_mode": True,
        }

    def set_theme_context(self, ctx):
        """Set visual theme context for the next generation."""
        self._theme_context = ctx

    def set_stop_event(self, event):
        """Attach a threading.Event for cooperative cancellation."""
        self._stop_event = event

    def set_progress_callback(self, cb: callable):
        """Attach a progress callback: cb(percent: int, phase: str, message: str)."""
        self._progress_cb = cb

    def _compute_word_target(self, duration_target: float) -> dict:
        """Compute word/block targets from a duration using real voice speed.

        Single source of truth — replaces _get_word_target() and
        _get_word_target_scaled().  Uses config/voice_timing.py for
        accurate words-per-minute based on the channel's configured TTS rate.
        """
        from config.voice_timing import words_for_duration

        cfg = self.canal_config
        test_mode = getattr(cfg, "TEST_MODE", False)

        if test_mode:
            words_obj = words_for_duration(self.canal_config, duration_target)
            words_min = getattr(cfg, "TEST_SCRIPT_WORDS_MIN", 200)
            words_max = getattr(cfg, "TEST_SCRIPT_WORDS_MAX", 600)
            blocks_min = getattr(cfg, "TEST_SCRIPT_BLOCKS_MIN", 3)
            blocks_max = getattr(cfg, "TEST_SCRIPT_BLOCKS_MAX", 6)
        else:
            words_obj = words_for_duration(self.canal_config, duration_target)
            words_min = max(100, int(words_obj * 0.85))
            words_max = int(words_obj * 1.3)
            blocks_min = max(3, int(duration_target * 1.2))
            blocks_max = max(5, int(duration_target * 2.0))

        return {
            "words_min": words_min,
            "words_max": words_max,
            "duration_target": duration_target,
            "blocks_min": blocks_min,
            "blocks_max": blocks_max,
            "palabras_objetivo": words_obj,
        }

    def _generate_outline(
        self, content_item: dict = None, word_target: dict = None,
        content_text: str = None, duration_min: float = None,
        word_target_int: int = None, canal_config=None,
        marathon_mode: bool = False, marathon_params: dict = None,
    ) -> Optional[dict]:
        """Generate a structured outline BEFORE writing blocks.

        One LLM call produces 4-6 chapters with titles, central ideas,
        concrete facts, visual keywords, and emotional arcs. This outline
        is then injected into every batch of block generation to maintain
        narrative coherence and factual substance.

        Args:
            content_item: Raw content dict (for normal mode).
            word_target: Word target dict (for normal mode).
            content_text: Direct content text (for marathon mode).
            duration_min: Target duration (for marathon mode).
            word_target_int: Target word count (for marathon mode).
            canal_config: Channel config (for marathon mode).
            marathon_mode: Whether this is a marathon generation.
            marathon_params: {num_sections, narrative_format, outline_chapters}.

        Returns:
            Dict with ``chapters`` list and ``summary``, or None on failure.
        """
        if marathon_mode:
            # ── Marathon mode: use special marathon outline prompt ──
            raw_text = content_text or ""
            dm = duration_min or 60
            wt = word_target_int or 8500
            cfg = canal_config or self.canal_config
            mp = marathon_params or {}

            from prompts.base_prompts import build_marathon_outline_prompt as bmop_fn
            system_prompt = bmop_fn(
                config=cfg, duration_min=dm,
                num_sections=mp.get("num_sections", 12),
                narrative_format=mp.get("narrative_format", "top_cases"),
                word_target=wt,
            )

            user_prompt = (
                f"Contenido fuente (investigación):\n\n{raw_text[:8000]}\n\n"
                f"Genera el outline estructurado en formato JSON."
            )

            try:
                data = self._call_with_failover(
                    phase="marathon_outline",
                    thinking=True,
                    model=LLM_MODEL_SCRIPT,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.7,
                    max_tokens=min(4096, OPENAI_MAX_TOKENS),
                    response_format={"type": "json_object"},
                )
                chapters = data.get("chapters", [])
                if not chapters or not isinstance(chapters, list):
                    logger.warning("[MARATHON] _generate_outline: empty chapters")
                    return None
                logger.info("[MARATHON] _generate_outline: %d chapters", len(chapters))
                return data
            except Exception as exc:
                logger.warning("[MARATHON] _generate_outline failed: %s", exc)
                return None

        # ── Normal mode ──
        content_text_item = content_item.get("text", "")[:4000]
        content_title = content_item.get("title", "")
        duration_min_val = word_target.get("duration_target", 15)
        palabras_objetivo = word_target.get("palabras_objetivo", 2500)

        try:
            prompts_module = importlib.import_module(
                f"prompts.{self.canal}_prompts"
            )
            system_prompt = prompts_module.build_outline_prompt(
                config=self.canal_config,
                duration_min=duration_min_val,
                word_target=palabras_objetivo,
            )
        except (ImportError, AttributeError):
            system_prompt = (
                "Eres un editor de documentales. Genera un outline "
                "estructurado del video a partir del contenido fuente.\n\n"
                "El outline debe tener 4-6 capítulos. Cada capítulo debe "
                "incluir: título, idea central, 2-3 hechos concretos, "
                "keywords visuales en inglés, y la emoción objetivo.\n\n"
                "El contenido debe ser FACTUAL y CONCRETO. Nada de "
                "metáforas vacías o lenguaje poético sin sustancia.\n\n"
                'Responde en JSON: {"chapters": [...], "summary": "..."}'
            )

        user_prompt = (
            f"Fuente: {content_title}\n\n"
            f"Contenido:\n{content_text}\n\n"
            f"Duración objetivo: {duration_min} min (~{palabras_objetivo} palabras).\n"
            f"Genera 4-6 capítulos con hechos CONCRETOS. NADA de relleno metafórico."
        )

        try:
            data = self._call_with_failover(
                phase="outline",
                thinking=True,
                model=LLM_MODEL_SCRIPT,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.7,
                max_tokens=min(3000, OPENAI_MAX_TOKENS),
                response_format={"type": "json_object"},
            )
            chapters = data.get("chapters", [])
            if not chapters or not isinstance(chapters, list):
                logger.warning("_generate_outline: empty or invalid chapters")
                return None
            logger.info(
                "_generate_outline: %d chapters generated (summary: %s)",
                len(chapters),
                data.get("summary", "")[:80],
            )
            return data
        except Exception as exc:
            logger.warning("_generate_outline failed: %s", exc)
            return None

    def _generate_blocks_batch(
        self, content_item: dict, previous_blocks: list = None,
        word_guidance: int = 250, source_text: str = None,
        outline: dict = None, batch_num: int = 0,
    ) -> list[dict]:
        """Generate 2-4 narrative blocks with the lightweight content prompt.

        Returns a list of block dicts with at least 'texto' field,
        or empty list on failure.
        """
        content_id = content_item.get("id")
        content_title = content_item.get("title", "")

        # Use the unified base prompt
        from prompts.base_prompts import build_content_only_prompt
        system_prompt = build_content_only_prompt(
            config=self.canal_config,
            previous_blocks=previous_blocks,
            word_guidance=word_guidance,
            source_text=source_text,
            outline=outline,
            batch_num=batch_num,
        )

        user_prompt = f"Fuente: {content_title}\n\nContinúa la narración documental."

        # ── Language enforcement: when source text is in a different
        #     language than the channel, explicitly instruct the LLM
        #     to write in the channel language. Without this, the LLM
        #     may follow the source language instead.
        if source_text:
            source_lang = _detect_text_language(source_text)
            channel_lang = getattr(self.canal_config, 'LANGUAGE', 'es')
            if source_lang and source_lang != channel_lang:
                lang_names = {
                    'en': 'inglés', 'es': 'español',
                    'fr': 'francés', 'pt': 'portugués',
                }
                src_name = lang_names.get(source_lang, source_lang)
                ch_name = lang_names.get(channel_lang, channel_lang)
                user_prompt += (
                    f"\n\n⚠️ IMPORTANTE: La fuente está en {src_name}. "
                    f"DEBES escribir TODO el guion en {ch_name}. "
                    "Usa los hechos como inspiración, NO traduzcas "
                    "literalmente."
                )

        try:
            data = self._call_with_failover(
                phase="blocks",
                thinking=True,
                model=LLM_MODEL_SCRIPT,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.85,
                max_tokens=min(2000, OPENAI_MAX_TOKENS),
                response_format={"type": "json_object"},
            )

            bloques = data.get("bloques", [])

            if not isinstance(bloques, list):
                return []

            # Normalize: ensure each block has 'texto'
            valid = []
            for b in bloques:
                if isinstance(b, dict) and b.get("texto", "").strip():
                    valid.append({"texto": b["texto"].strip()})
            return valid

        except Exception as exc:
            logger.warning("Block batch generation failed: %s", exc)
            return []

    def _build_minimal_prompt(
        self, previous_blocks: list = None, word_guidance: int = 250, source_text: str = None,
    ) -> str:
        """Fallback minimal content prompt (when channel prompt module is unavailable)."""
        cfg = self.canal_config
        tone = getattr(cfg, "CANAL_TONE", "Narración documental.")
        style = getattr(cfg, "CANAL_NARRATIVE_STYLE", "documental")

        context = ""
        if previous_blocks:
            last_texts = [b.get("texto", "") for b in previous_blocks[-4:] if isinstance(b, dict)]
            if last_texts:
                context = "\nCONTINUACIÓN:\n" + " ".join(last_texts)[-400:] + "\n"

        source = ""
        if source_text:
            source = f"\nFUENTE:\n{source_text[:1500]}\n"

        return f"""Eres un guionista documental para YouTube. Escribe en español latinoamericano neutro.
TONO: {tone}
ESTILO: {style}
Genera 2-4 bloques narrativos (~{word_guidance} palabras). Cada bloque solo necesita "texto".
Responde JSON: {{"bloques": [{{"texto": "..."}}]}}{source}{context}"""

    def _enrich_blocks(
        self, bloques: list[dict], content_item: dict, word_target: dict,
    ) -> dict:
        """Enrich raw text-only bloques in two iterative phases.

        Phase 1 — _enrich_block_fields_iterative:
          Batches of 5 blocks → adds tipo, emocion, search_query_en,
          escena_descripcion, media_tipo, media_duracion.
          Lightweight calls, each handling a small window of text.

        Phase 2 — _enrich_document_metadata:
          Single call with block summaries → produces titulo_options,
          keywords, hashtags, descripcion_seo, chapters, cta, parrafos.

        This replaces the old monolithic approach that truncated the
        full guion to 3000 chars, silently dropping most blocks.
        """
        if not bloques:
            return None

        from config.voice_timing import duration_for_words
        full_guion = "\n\n".join(b["texto"] for b in bloques)
        total_words = len(full_guion.split())
        duration_min = duration_for_words(self.canal_config, total_words)

        # ── Phase 1: iterative per-block field enrichment ────────
        enriched_bloques = self._enrich_block_fields_iterative(bloques, content_item)

        # ── Phase 2: document-level metadata ──────────────────────
        doc_meta = self._enrich_document_metadata(
            enriched_bloques, content_item, word_target, full_guion,
        )

        # ── Assemble final data ──────────────────────────────────
        escenas = [
            {"descripcion": b.get("escena_descripcion", "")}
            for b in enriched_bloques
        ]
        emociones = [b.get("emocion", "") for b in enriched_bloques if b.get("emocion")]

        titulo_options = doc_meta.get("titulo_options", [content_item.get("title", "Sin título")])
        data = {
            "titulo_options": titulo_options,
            "titulo_selected": titulo_options[0] if titulo_options else content_item.get("title", "Sin título"),
            "descripcion_seo": doc_meta.get("descripcion_seo", ""),
            "guion": full_guion,
            "parrafos": doc_meta.get("parrafos", [{"idea_central": "", "bloques": enriched_bloques}]),
            "cta": doc_meta.get("cta", {"tipo": "cta", "texto": "Suscríbete para más."}),
            "bloques": enriched_bloques,
            "escenas": escenas,
            "emociones": emociones,
            "keywords": doc_meta.get("keywords", []),
            "hashtags": doc_meta.get("hashtags", []),
            "duracion_estimada": duration_min,
            "chapters": doc_meta.get("chapters", []),
            "fuentes_citadas": doc_meta.get("fuentes_citadas", []),
            "palabras_reales": total_words,
        }
        return data

    def _extract_onscreen_text(self, data: dict) -> dict:
        """Extract [TEXTO_PANTALLA: "..."] tags from block text into onscreen_text field.

        Scans all bloques for embedded onscreen text directives, extracts them,
        removes the tag from the narration text, and stores them in a dedicated
        'onscreen_text' field on each block.
        """
        import re
        pattern = re.compile(r'\[TEXTO_PANTALLA:\s*"([^"]+)"\s*\]')

        bloques = data.get("bloques", [])
        if not bloques:
            return data

        for block in bloques:
            texto = block.get("texto", "")
            match = pattern.search(texto)
            if match:
                block["onscreen_text"] = match.group(1).strip()
                # Remove the tag from narration text
                block["texto"] = pattern.sub("", texto).strip()
                # Clean up double spaces / leading punctuation
                block["texto"] = re.sub(r'\s{2,}', ' ', block["texto"]).strip()

        # Also remove tags from the full guion
        guion = data.get("guion", "")
        if guion:
            data["guion"] = pattern.sub("", guion)
            data["guion"] = re.sub(r'\s{2,}', ' ', data["guion"]).strip()

        # Count how many onscreen texts were extracted
        onscreen_count = sum(1 for b in bloques if b.get("onscreen_text"))
        if onscreen_count > 0:
            logger.info("_extract_onscreen_text: extracted %d onscreen texts", onscreen_count)

        return data

    # ────────────────────────────────────────────────────────────
    # Phase 1: iterative block field enrichment
    # ────────────────────────────────────────────────────────────

    ENRICH_BATCH_SIZE = 5  # blocks per batch — keeps each call small

    def _enrich_block_fields_iterative(
        self, bloques: list[dict], content_item: dict,
    ) -> list[dict]:
        """Enrich block-level fields in batches so no truncation occurs.

        For each batch of ENRICH_BATCH_SIZE blocks we send the full
        block texts + lightweight instructions.  The LLM returns
        enriched blocks with: tipo, emocion, search_query_en,
        escena_descripcion, media_tipo, media_duracion.

        Context from the previous batch (last block's tipo) is
        threaded through for narrative arc coherence.
        """
        if not bloques:
            return []

        total = len(bloques)
        enriched: list[dict] = []
        previous_tipo = "hook"  # starter arc

        for batch_start in range(0, total, self.ENRICH_BATCH_SIZE):
            batch_end = min(batch_start + self.ENRICH_BATCH_SIZE, total)
            batch = bloques[batch_start:batch_end]
            batch_num = (batch_start // self.ENRICH_BATCH_SIZE) + 1
            num_batches = (total + self.ENRICH_BATCH_SIZE - 1) // self.ENRICH_BATCH_SIZE

            logger.info(
                "Enrich fields: batch %d/%d (blocks %d–%d of %d)",
                batch_num, num_batches, batch_start + 1, batch_end, total,
            )

            enriched_batch = self._enrich_block_fields_batch(
                batch, previous_tipo, batch_num, num_batches, content_item,
            )

            if enriched_batch:
                enriched.extend(enriched_batch)
                previous_tipo = enriched_batch[-1].get("tipo", "desarrollo")
            else:
                # Fallback: keep raw blocks with default fields
                logger.warning(
                    "Enrich fields batch %d returned empty — using raw blocks", batch_num,
                )
                for b in batch:
                    b_with_defaults = dict(b)
                    b_with_defaults.setdefault("tipo", "desarrollo")
                    b_with_defaults.setdefault("emocion", "neutral")
                    b_with_defaults.setdefault("search_query_en", content_item.get("title", ""))
                    b_with_defaults.setdefault("escena_descripcion", b.get("texto", "")[:80])
                    b_with_defaults.setdefault("media_tipo", "imagen")
                    b_with_defaults.setdefault("media_duracion", 6.0)
                    enriched.append(b_with_defaults)

        logger.info(
            "Enrich fields done: %d/%d blocks enriched", len(enriched), total,
        )
        return enriched

    def _enrich_block_fields_batch(
        self, batch: list[dict], previous_tipo: str,
        batch_num: int, num_batches: int, content_item: dict,
    ) -> list[dict]:
        """Enrich one batch of blocks with per-block metadata fields.

        Lightweight call: only ~500-800 input tokens, ~500 output tokens.
        Returns list of enriched block dicts or empty list on failure.
        """
        # Number the blocks for precise alignment
        numbered_blocks = []
        for i, b in enumerate(batch):
            text = b.get("texto", "")
            numbered_blocks.append(f"[BLOQUE {i + 1}]\n{text}")

        blocks_text = "\n\n".join(numbered_blocks)

        # Continuity hint from previous batch
        arc_hint = ""
        if batch_num == 1:
            arc_hint = (
                "ARCOS PERMITIDOS: hook, desarrollo, climax, reflexion, cierre.\n"
                f"El primer bloque de este lote DEBE ser 'hook' (apertura).\n"
            )
        elif batch_num == num_batches:
            arc_hint = (
                f"El lote anterior terminó con tipo '{previous_tipo}'.\n"
                "ARCOS PERMITIDOS: desarrollo, climax, reflexion, cierre.\n"
                "El ÚLTIMO bloque de este lote DEBE ser 'cierre' (conclusión final).\n"
            )
        else:
            arc_hint = (
                f"El lote anterior terminó con tipo '{previous_tipo}'.\n"
                "ARCOS PERMITIDOS: desarrollo, climax, reflexion.\n"
                "Continúa el arco narrativo de forma natural.\n"
            )

        # ── Build theme context block for the prompt ──────────────────
        theme_block = ""
        tc = self._theme_context
        if tc:
            theme_lines = ["\nCONTEXTO TEMÁTICO DEL VIDEO (anclaje visual secundario):"]
            if tc.genre and tc.genre != "documental":
                theme_lines.append(f"- Género/ambientación: {tc.genre}")
            if tc.era_decade and tc.era_decade not in ("atemporal", "presente", ""):
                theme_lines.append(f"- Época/década: {tc.era_decade}")
            elif tc.era and tc.era != "atemporal":
                theme_lines.append(f"- Época: {tc.era}")
            if tc.primary_subject:
                theme_lines.append(f"- Sujeto visual principal: {tc.primary_subject}")
            if tc.key_motifs:
                theme_lines.append(f"- Motivos visuales icónicos: {', '.join(tc.key_motifs[:4])}")
            if tc.forbidden_elements:
                theme_lines.append(f"- ⛔ ELEMENTOS PROHIBIDOS (NUNCA incluir en search_query_en): {', '.join(tc.forbidden_elements)}")
            theme_lines.append(
                "\nINSTRUCCIÓN CLAVE: La temática es el CONTEXTO, NO el protagonista de la query.\n"
                "- El SUJETO NARRATIVO (lo que se narra en este bloque concreto) SIEMPRE domina la query (~60-70%).\n"
                "- La ambientación temática (época, motivos, género) se usa SOLO como anclaje secundario (~30-40%):\n"
                "  máximo 1-2 keywords temáticas, y solo si son relevantes para lo narrado en este bloque.\n"
                "- Ejemplo: si el canal es de Egipto antiguo y este bloque narra 'los mercaderes usaban\n"
                "  balanzas de precisión para pesar el oro' → 'merchant weighing gold precision scale ancient Egyptian'\n"
                "  (keywords narrativos dominan: merchant, weighing, gold, precision, scale + 1 anclaje: ancient Egyptian).\n"
                "  NUNCA generes solo 'ancient Egypt trade economy' (eso es temático genérico, no refleja lo narrado).\n"
                "- Si el bloque habla de un concepto atemporal sin conexión visual con la época → usa SOLO\n"
                "  keywords narrativos + como máximo 1 keyword de ambientación.\n"
                "- NUNCA incluyas elementos prohibidos en search_query_en."
            )
            theme_block = "\n".join(theme_lines)

        system_prompt = (
            "Eres un asistente editorial. Tu tarea es enriquecer bloques narrativos "
            "de un guion documental en español latinoamericano.\n\n"
            "REGLAS ESTRICTAS:\n"
            "1. NO cambies, resumas ni acortes el texto original de los bloques.\n"
            "2. Solo añades los campos de metadatos indicados.\n"
            "3. Mantén el número EXACTO de bloques del lote.\n"
            "4. Responde ÚNICAMENTE con JSON.\n\n"
            f"{arc_hint}\n"
            + (theme_block + "\n\n" if theme_block else "")
            + "CAMPOS POR BLOQUE:\n"
            "- tipo: (hook|desarrollo|climax|reflexion|cierre)\n"
            "- emocion: sentimiento predominante en español (misterio, asombro, tensión, reflexión...)\n"
            "- search_query_en: frase de búsqueda en INGLÉS para encontrar "
            "video/imagen en bancos de stock (Pexels, Pixabay, Unsplash).\n"
            "  REGLAS OBLIGATORIAS:\n"
            "  * FUSIÓN NARRATIVA + TEMÁTICA (DOS PARTES, AMBAS OBLIGATORIAS):\n"
            "    (1) SUJETO NARRATIVO (VA PRIMERO, ~60-70% de la query): 3-5 keywords\n"
            "        que describen EXACTAMENTE lo que se narra en este bloque — persona,\n"
            "        acción, lugar, objeto CONCRETO mencionado en la narración.\n"
            "        Usa palabras del propio texto narrado como keywords.\n"
            "    (2) AMBIENTACIÓN TEMÁTICA (VA DESPUÉS, ~30-40% de la query): máximo 1-2\n"
            "        keywords del contexto temático como ANCLAJE VISUAL secundario.\n"
            "        Usa preferentemente key_motifs, primary_subject, genre o era_decade.\n"
            "  * LO QUE VES = LO QUE OYES: la query debe reflejar VISUALMENTE lo narrado.\n"
            "    Si el bloque narra 'los mercaderes usaban balanzas de precisión para\n"
            "    pesar el oro', la query debe ser 'merchant weighing gold precision scale\n"
            "    ancient Egyptian', NO 'ancient Egypt trade economy' (eso es temático genérico).\n"
            "  * Varía el encuadre/ángulo entre bloques consecutivos:\n"
            "    alterna 'wide shot' ↔ 'close-up detail' ↔ 'distant view'.\n"
            "  * Usa términos visuales concretos: 'aerial shot', 'wide angle', "
            "'close up detail', 'drone footage', 'golden hour'\n"
            "  * Equilibra especificidad con disponibilidad en stock: "
            "'18th century French revolution' (OK) vs 'Robespierre guillotining "
            "Danton 5 April 1794' (DEMASIADO específico, no existe en stock)\n"
            "  * Traduce conceptos abstractos a escenas visuales "
            "(ej: 'creencia en la muerte' → 'ancient tomb burial chamber dark')\n"
            "  * BIEN: 'merchant weighing gold scale ancient Egyptian marketplace'\n"
            "  * BIEN: 'physician examining patient medieval instruments torchlight'\n"
            "  * BIEN: 'ancient Egyptian gold mask museum exhibit close up'\n"
            "  * MAL: 'Funeral Mask Egypt Art Institute Chicago' (nombre de museo)\n"
            "  * MAL: 'ancient Egypt history documentary' (solo temático, sin lo narrado)\n"
            "  * MAL: 'medieval history atmosphere dramatic lighting' (solo términos de\n"
            "    época, sin keywords de la narración)\n"
            "- escena_descripcion: descripción visual cinematográfica en español (1 frase)\n"
            "- media_tipo: 'video' o 'imagen' según:\n"
            "  * VIDEO: paisajes, naturaleza, ciudades, cielo, agua, movimiento, time-lapses, drones\n"
            "  * IMAGEN: objetos estáticos, documentos, mapas, reliquias, retratos, conceptos abstractos\n"
            "  * En caso de duda: IMAGEN\n"
            "- media_duracion: duración en segundos (imagen=5-7, video=8-12)"
        )

        user_prompt = (
            f"Tema: {content_item.get('title', 'Documental')}\n"
            f"Lote {batch_num}/{num_batches} ({len(batch)} bloques):\n\n"
            f"{blocks_text}\n\n"
            f"Devuelve los {len(batch)} bloques enriquecidos en este formato:\n"
            '{"bloques": [{"texto": "...", "tipo": "...", "emocion": "...", '
            '"search_query_en": "...", "escena_descripcion": "...", '
            '"media_tipo": "...", "media_duracion": N}]}'
        )

        try:
            data = self._call_with_failover(
                phase="enrich",
                thinking=False,
                model=LLM_MODEL_SCRIPT,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.5,
                max_tokens=2048,
                response_format={"type": "json_object"},
            )

            enriched_batch = data.get("bloques", [])

            if not isinstance(enriched_batch, list) or len(enriched_batch) != len(batch):
                logger.warning(
                    "Enrich batch %d: got %d blocks, expected %d — using raw fallback",
                    batch_num, len(enriched_batch) if isinstance(enriched_batch, list) else 0,
                    len(batch),
                )
                return []

            # Validate: each enriched block must preserve the original text
            valid = []
            for i, eb in enumerate(enriched_batch):
                if isinstance(eb, dict) and eb.get("texto", "").strip():
                    orig_text = batch[i].get("texto", "")
                    enriched_text = eb["texto"].strip()
                    # Allow minor whitespace diffs but reject major changes
                    if len(enriched_text) >= len(orig_text) * 0.85:
                        valid.append(eb)
                    else:
                        # Text was truncated — use original with default fields
                        logger.warning(
                            "Block %d text was altered (%d→%d chars) — restoring original",
                            i + 1, len(orig_text), len(enriched_text),
                        )
                        b = dict(batch[i])
                        b["tipo"] = eb.get("tipo", "desarrollo")
                        b["emocion"] = eb.get("emocion", "neutral")
                        b["search_query_en"] = eb.get("search_query_en", "")
                        b["escena_descripcion"] = eb.get("escena_descripcion", b.get("texto", "")[:80])
                        b["media_tipo"] = eb.get("media_tipo", "imagen")
                        b["media_duracion"] = eb.get("media_duracion", 6.0)
                        valid.append(b)

            if len(valid) != len(batch):
                logger.warning(
                    "Enrich batch %d: only %d/%d valid blocks after validation",
                    batch_num, len(valid), len(batch),
                )

            return valid

        except Exception as exc:
            logger.warning("Enrich batch %d LLM call failed: %s", batch_num, exc)
            return []

    # ────────────────────────────────────────────────────────────
    # Phase 2: document-level metadata
    # ────────────────────────────────────────────────────────────

    def _enrich_document_metadata(
        self, bloques: list[dict], content_item: dict,
        word_target: dict, full_guion: str,
    ) -> dict:
        """Generate document-level metadata from already-enriched blocks.

        Because each block is now small (fields already enriched), we send
        a summary of every block instead of full text. The LLM produces:
        titulo_options, keywords, hashtags, descripcion_seo, chapters, cta,
        and organizes blocks into parrafos with idea_central.

        This is a single call since metadata is inherently global.
        """
        if not bloques:
            return {}

        # Build compact block summaries (field names + first 80 chars of text)
        summaries = []
        for i, b in enumerate(bloques):
            texto_preview = b.get("texto", "")[:80].replace("\n", " ").strip()
            summaries.append(
                f"  [{i + 1}] tipo={b.get('tipo', '?')} | emoción={b.get('emocion', '?')} | "
                f"media={b.get('media_tipo', '?')} | \"{texto_preview}...\""
            )

        summaries_text = "\n".join(summaries)

        # Build a representative guion excerpt for titling/SEO context
        # Take first 2 + last 2 blocks for intro/conclusion flavor
        intro_blocks = bloques[:2]
        outro_blocks = bloques[-2:] if len(bloques) > 4 else bloques[2:]

        intro_text = " ".join(b.get("texto", "")[:200] for b in intro_blocks)
        outro_text = " ".join(b.get("texto", "")[:200] for b in outro_blocks)
        excerpt = f"INICIO: {intro_text}\n...\nFINAL: {outro_text}"

        n_blocks = len(bloques)
        total_words = len(full_guion.split())

        system_prompt = (
            "Eres un editor de documentales para YouTube. Genera metadatos "
            "profesionales para un guion narrativo ya escrito.\n\n"
            "Recibirás un resumen de TODOS los bloques del guion.\n"
            "Tu trabajo es generar exclusivamente metadatos editoriales.\n"
            "NO generes contenido narrativo nuevo.\n"
            "Responde ÚNICAMENTE con JSON válido."
        )

        user_prompt = (
            f"Tema: {content_item.get('title', 'Documental')}\n"
            f"Total: {n_blocks} bloques, {total_words} palabras\n\n"
            f"--- RESUMEN DE BLOQUES ---\n"
            f"{summaries_text}\n\n"
            f"--- EXCERTO DEL GUION ---\n"
            f"{excerpt}\n\n"
            f"Genera los siguientes metadatos:\n\n"
            f"1. titulo_options: 3 opciones de título en español (8-12 palabras c/u).\n"
            f"   Estilo: intrigante, emocional, que genere curiosidad.\n\n"
            f"2. keywords: 10-15 palabras clave relevantes (español).\n\n"
            f"3. hashtags: 8-12 hashtags (sin #, solo texto, mezcla español e inglés).\n\n"
            f"4. descripcion_seo: párrafo SEO de 80-150 palabras en español.\n"
            f"   Incluye keywords naturales y llamada a la acción.\n\n"
            f"5. chapters: timestamp chapters (minuto aprox basado en orden de bloques).\n"
            f"   Formato: [{{\"time\": \"0:00\", \"title\": \"...\"}}].\n"
            f"   4-6 chapters distribuidos uniformemente en ~{word_target.get('duration_target', 15)} min.\n\n"
            f"6. parrafos: agrupa los bloques en 3-5 párrafos temáticos.\n"
            f"   Cada párrafo con idea_central y la lista de índices de bloques que lo componen.\n"
            f"   Formato: [{{\"idea_central\": \"...\", \"bloque_indices\": [1,2,3]}}].\n"
            f"   Usa los números de bloque [1] a [{n_blocks}] del resumen.\n"
            f"   ¡TODOS los {n_blocks} bloques deben estar asignados a algún párrafo!\n\n"
            f"7. cta: llamada a la acción final.\n"
            f"   Formato: {{\"tipo\": \"cta\", \"texto\": \"...\"}}.\n"
            f"   15-25 palabras, invitando a suscribirse y comentar.\n\n"
            f"8. fuentes_citadas: 3-5 fuentes ficticias pero verosímiles.\n"
            f"   Formato: [\"Autor (año). Título. Editorial.\"].\n\n"
            f"Responde: {{\"titulo_options\": [...], \"keywords\": [...], \"hashtags\": [...], "
            f"\"descripcion_seo\": \"...\", \"chapters\": [...], \"parrafos\": [...], "
            f"\"cta\": {{...}}, \"fuentes_citadas\": [...]}}"
        )

        try:
            data = self._call_with_failover(
                phase="metadata",
                thinking=False,
                model=LLM_MODEL_SCRIPT,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.7,
                max_tokens=min(4096, OPENAI_MAX_TOKENS),
                response_format={"type": "json_object"},
            )

            # Post-process parrafos: convert indices to actual bloques
            raw_parrafos = data.get("parrafos", [])
            final_parrafos = []
            assigned_indices: set = set()

            for rp in raw_parrafos:
                indices = rp.get("bloque_indices", [])
                parrafo_bloques = []
                for idx in indices:
                    if 1 <= idx <= n_blocks:
                        parrafo_bloques.append(bloques[idx - 1])
                        assigned_indices.add(idx)
                if parrafo_bloques:
                    final_parrafos.append({
                        "idea_central": rp.get("idea_central", ""),
                        "bloques": parrafo_bloques,
                    })

            # Any unassigned blocks → create a catch-all parrafo
            all_indices = set(range(1, n_blocks + 1))
            missing = sorted(all_indices - assigned_indices)
            if missing:
                logger.warning(
                    "Document metadata: %d blocks not assigned to any parrafo — "
                    "creating catch-all parrafo for indices %s",
                    len(missing), missing,
                )
                catch_all_bloques = [bloques[idx - 1] for idx in missing if 1 <= idx <= n_blocks]
                final_parrafos.append({
                    "idea_central": "Continuación narrativa",
                    "bloques": catch_all_bloques,
                })

            # Build enriched result
            result = {
                "titulo_options": data.get("titulo_options", []),
                "descripcion_seo": data.get("descripcion_seo", ""),
                "parrafos": final_parrafos or [{"idea_central": "", "bloques": bloques}],
                "cta": data.get("cta"),
                "keywords": data.get("keywords", []),
                "hashtags": data.get("hashtags", []),
                "chapters": data.get("chapters", []),
                "fuentes_citadas": data.get("fuentes_citadas", []),
            }

            logger.info(
                "Document metadata done: %d titles, %d keywords, %d hashtags, "
                "%d chapters, %d parrafos",
                len(result["titulo_options"]), len(result["keywords"]),
                len(result["hashtags"]), len(result["chapters"]),
                len(result.get("parrafos", [])),
            )

            return result

        except Exception as exc:
            logger.warning("Document metadata generation failed: %s — using defaults", exc)
            return {
                "titulo_options": [content_item.get("title", "Sin título")],
                "descripcion_seo": "",
                "parrafos": [{"idea_central": "", "bloques": bloques}],
                "cta": {"tipo": "cta", "texto": "Suscríbete para más."},
                "keywords": [],
                "hashtags": [],
                "chapters": [],
                "fuentes_citadas": [],
            }

    def generate_v2(self, content_item: dict, palabras_objetivo: int = None,
                     marathon_overrides: dict = None) -> Optional[dict]:
        """Generate a script using sequential block-by-block generation.

        Content from the database is used as thematic inspiration only —
        the LLM is free to expand any topic to the requested word count.

        Args:
            content_item: Raw content dict (title, text, source, etc.)
            palabras_objetivo: Exact word target (computed from duration × voice speed).
                               If None, derived from channel average duration.
            marathon_overrides: Optional dict with marathon-specific overrides:
                - max_blocks: Override PROD_SCRIPT_BLOCKS_MAX * 1.5 cap
                - max_batches: Override max_batches (default 50)
                - max_empty_strikes: Override max_empty_strikes (default 10)
                - outline: Pre-generated marathon outline to use directly
                - word_target_override: Complete word target dict to use instead

        Returns:
            Enriched script dict or None on failure.
        """
        content_text = content_item.get("text", "")
        content_id = content_item.get("id")

        if not content_text:
            logger.warning("Empty content text for item %s", content_id)
            return None

        # ── Marathon override: use pre-computed word target ──
        if marathon_overrides and marathon_overrides.get("word_target_override"):
            word_target = marathon_overrides["word_target_override"]
            duration_target = word_target["duration_target"]
        else:
            # Step 1: Word target
            if palabras_objetivo is not None:
                cfg = self.canal_config
                duration_target = _duration_for_words(cfg, palabras_objetivo)
                word_target = {
                    "words_min": max(100, int(palabras_objetivo * 0.85)),
                    "words_max": int(palabras_objetivo * 1.5),
                    "duration_target": duration_target,
                    "blocks_min": max(3, int(duration_target * 1.2)),
                    "blocks_max": max(5, int(duration_target * 2.0)),
                    "palabras_objetivo": palabras_objetivo,
                }
            else:
                word_target = self._get_word_target()
                palabras_objetivo = word_target["palabras_objetivo"]

        words_min = word_target["words_min"]
        logger.info(
            "generate_v2: content_id=%s chars=%d target=%d words (~%s min, voice-factor)",
            content_id, len(content_text), palabras_objetivo,
            word_target["duration_target"],
        )

        # Step 2: Generate structured outline (NEW — outline-first approach)
        # This gives the LLM a coherent chapter structure BEFORE writing blocks,
        # preventing rambling, repetitive, or factually empty narration.
        outline = None
        if marathon_overrides and marathon_overrides.get("outline"):
            outline = marathon_overrides["outline"]
            logger.info(
                "generate_v2: using marathon outline — %d chapters",
                len(outline.get("chapters", [])),
            )
        else:
            try:
                outline = self._generate_outline(content_item, word_target)
                if outline:
                    n_chapters = len(outline.get("chapters", []))
                    logger.info(
                        "generate_v2: outline generated — %d chapters",
                        n_chapters,
                    )
            except Exception as exc:
                logger.warning("generate_v2: outline generation failed (continuing without): %s", exc)

        # Step 3: Sequential block generation
        all_bloques: list[dict] = []
        empty_strikes = 0
        total_chars = len(content_text)
        # ── Marathon overrides: allow longer generation ──
        max_empty_strikes = marathon_overrides.get("max_empty_strikes", 10) if marathon_overrides else 10
        max_batches = marathon_overrides.get("max_batches", 50) if marathon_overrides else 50

        for batch_num in range(max_batches):
            # Cooperative stop check
            if hasattr(self, '_stop_event') and self._stop_event and self._stop_event.is_set():
                logger.info("generate_v2: stop requested at batch %d", batch_num + 1)
                break

            total_words = sum(len(b.get("texto", "").split()) for b in all_bloques)

            if total_words >= palabras_objetivo * 0.98:
                logger.info(
                    "generate_v2: target reached at batch %d (%d words ≥ %d)",
                    batch_num + 1, total_words, int(palabras_objetivo * 0.98),
                )
                break

            # ── Rotating source text window (v24: anti-repetition) ──
            # Slides the source material window proportionally with
            # narrative progress so the LLM always sees fresh content.
            # This prevents the LLM from re-reading the same 3000 chars
            # across 150+ batch calls in marathon mode.
            window_size = 3000
            if total_chars > window_size:
                progress = min(0.95, total_words / max(1, palabras_objetivo))
                start = max(0, int(progress * (total_chars - window_size)))
                source_text = content_text[start:start + window_size]
            else:
                source_text = content_text[:window_size]

            # Calculate word guidance for this batch
            remaining = max(100, palabras_objetivo - total_words)
            word_guidance = min(400, max(100, int(remaining * 0.5)))
            context = all_bloques if all_bloques else None

            try:
                new_bloques = self._generate_blocks_batch(
                    content_item, context, word_guidance, source_text,
                    outline=outline, batch_num=batch_num,
                )
            except Exception as exc:
                logger.warning("generate_v2: batch %d LLM call failed: %s", batch_num + 1, exc)
                # Rate-limit / transient error: backoff and retry
                empty_strikes += 1
                if empty_strikes >= max_empty_strikes:
                    logger.error("generate_v2: %d consecutive failures — giving up", empty_strikes)
                    break
                import time as _time
                _time.sleep(min(30, 2 ** empty_strikes))
                continue

            if not new_bloques:
                empty_strikes += 1
                logger.warning(
                    "generate_v2: batch %d returned no blocks (strike %d/%d)",
                    batch_num + 1, empty_strikes, max_empty_strikes,
                )
                if empty_strikes >= max_empty_strikes:
                    logger.warning("generate_v2: %d empty strikes — content exhausted", empty_strikes)
                    break
                continue

            all_bloques.extend(new_bloques)
            new_wc = sum(len(b.get("texto", "").split()) for b in new_bloques)
            total_words = sum(len(b.get("texto", "").split()) for b in all_bloques)
            empty_strikes = 0  # reset on success

            logger.info(
                "generate_v2: batch %d → +%d blocks, +%d words, total=%d/%d",
                batch_num + 1, len(new_bloques), new_wc, total_words, palabras_objetivo,
            )

            # Emit progress callback (map word completion to 15-23% range)
            if hasattr(self, '_progress_cb') and self._progress_cb:
                pct = min(23, 15 + int(8 * total_words / max(1, palabras_objetivo)))
                try:
                    self._progress_cb(
                        pct, "script",
                        f"Generando guion: {total_words}/{palabras_objetivo} palabras "
                        f"(batch {batch_num + 1})",
                        current=total_words, total=palabras_objetivo,
                    )
                except Exception:
                    pass

        if not all_bloques:
            logger.error("generate_v2: no blocks generated after %d batches", max_batches)
            return None

        total_words = sum(len(b.get("texto", "").split()) for b in all_bloques)
        initial_blocks = len(all_bloques)

        # ── Dedup pass (v24: anti-repetition) ──
        # Detect and remove near-duplicate blocks before enrichment.
        # Critical for marathons where 150+ batch calls can produce
        # semantically overlapping content across distant blocks.
        all_bloques = _dedup_blocks(all_bloques)
        if len(all_bloques) < initial_blocks:
            removed = initial_blocks - len(all_bloques)
            logger.warning(
                "generate_v2: dedup removed %d/%d near-duplicate blocks",
                removed, initial_blocks,
            )

        logger.info(
            "generate_v2: content done — %d blocks, %d words",
            len(all_bloques), sum(len(b.get("texto", "").split()) for b in all_bloques),
        )

        # Safety net: if the LLM massively overshoots the word target
        # (e.g. thinking mode or stale config), cap gracefully by word count
        # instead of raw block count. Block count doesn't reliably correlate
        # with duration — a 50-word hook block and a 150-word narrative block
        # are very different lengths.
        #
        # Cap only triggers when total_words > 2× the target, and it keeps
        # blocks sequentially up to ~1× the target words.
        # ── Marathon: use marathon max_blocks as extra guard ──
        if marathon_overrides and marathon_overrides.get("max_blocks"):
            marathon_max = marathon_overrides["max_blocks"]
            if len(all_bloques) > marathon_max * 3:  # marathon: very high bar (e.g. 270 blocks)
                capped = marathon_max
                logger.warning(
                    "generate_v2 [MARATHON]: bloques generados (%d, %d words) exceden 3x"
                    " el máximo (%d). Posible word_target inflado. Usando los primeros"
                    " %d bloques.",
                    len(all_bloques), total_words, marathon_max, capped,
                )
                all_bloques = all_bloques[:capped]
        else:
            # Non-marathon: cap by word count, not block count
            if total_words > palabras_objetivo * 2.0:
                target_word_cap = int(palabras_objetivo * 1.0)
                kept_bloques: list[dict] = []
                kept_words = 0
                for b in all_bloques:
                    wc = len(b.get("texto", "").split())
                    if kept_words + wc > target_word_cap and kept_bloques:
                        break
                    kept_bloques.append(b)
                    kept_words += wc
                logger.warning(
                    "generate_v2: total_words (%d) > 2× target (%d) — "
                    "capped to %d words (%d blocks, was %d)",
                    total_words, palabras_objetivo, kept_words,
                    len(kept_bloques), len(all_bloques),
                )
                all_bloques = kept_bloques

        # Step 3: Enrich blocks with structural fields
        if hasattr(self, '_progress_cb') and self._progress_cb:
            try:
                self._progress_cb(24, "script", "Enriqueciendo guion con metadatos (SEO, emociones, media)...")
            except Exception:
                pass

        enriched = self._enrich_blocks(all_bloques, content_item, word_target)

        # Step 3.5: Script validation (v22 — dedicated validator)
        if enriched and enriched.get("bloques"):
            try:
                from pipeline.script_validator import ScriptValidator
                validator = ScriptValidator(self.canal_config)
                val_result = validator.validate(enriched, word_target, content_item)

                if val_result.passes:
                    logger.info(
                        "ScriptValidator: PASS (score=%.2f) — rep=%.2f coh=%.2f hook=%.2f",
                        val_result.score,
                        val_result.repetition_score,
                        val_result.coherence_score,
                        val_result.hook_score,
                    )
                    if val_result.warnings:
                        logger.info("ScriptValidator warnings: %s", "; ".join(val_result.warnings[:3]))
                elif not val_result.is_grave:
                    # Minor issues — try regeneration without failover
                    logger.warning(
                        "ScriptValidator: minor issues (score=%.2f): %s",
                        val_result.score,
                        "; ".join(val_result.issues[:3]),
                    )
                    try:
                        regenerated = self._regenerate_problematic_paragraphs(
                            enriched, {"issues": val_result.issues}, content_item
                        )
                        if regenerated:
                            val2 = validator.validate(regenerated, word_target, content_item)
                            if val2.passes:
                                logger.info("ScriptValidator: PASS after regeneration (score=%.2f)", val2.score)
                                enriched = regenerated
                    except Exception:
                        pass  # keep original enriched
                else:
                    # Grave issues — would trigger model failover, but we're past generation
                    logger.warning(
                        "ScriptValidator: GRAVE issues (score=%.2f): %s",
                        val_result.score,
                        "; ".join(val_result.issues[:5]),
                    )
                    # Still try to salvage with regeneration
                    try:
                        regenerated = self._regenerate_problematic_paragraphs(
                            enriched, {"issues": val_result.issues}, content_item
                        )
                        if regenerated:
                            val2 = validator.validate(regenerated, word_target, content_item)
                            if val2.passes:
                                enriched = regenerated
                            else:
                                logger.warning("ScriptValidator: still failing after regeneration — keeping original")
                    except Exception:
                        pass

            except Exception as exc:
                logger.warning(
                    "ScriptValidator: exception during validation — proceeding: %s", exc
                )

        # Step 3.6: Extract onscreen text tags from blocks
        if enriched and enriched.get("bloques"):
            enriched = self._extract_onscreen_text(enriched)

        # Step 4: Save to DB (or emergency mode)
        if hasattr(self, '_progress_cb') and self._progress_cb:
            try:
                self._progress_cb(25, "script", f"Guion completo: {total_words} palabras")
            except Exception:
                pass

        full_guion = enriched.get("guion", "")
        result = self._save_and_return(
            content_id=content_item.get("id"),
            data=enriched,
            total_tokens=0,
            cost=0.0,
            elapsed_ms=0,
            word_target=word_target,
        )

        if result:
            logger.info(
                "generate_v2: saved script id=%s, %d words, %d blocks%s",
                result.get("id") if result else "FAILED",
                total_words,
                len(all_bloques),
                " [EMERGENCY]" if result.get("emergency_mode") else "",
            )
            return result

        # ── Emergency mode: all LLM generation + enrichment failed ──
        logger.warning(
            "generate_v2: enrichment/DB save failed — activating emergency mode "
            "for content_id=%s",
            content_id,
        )
        emergency = self._generate_emergency_script(content_item, word_target)
        if emergency:
            result = self._save_and_return(
                content_id=content_item.get("id"),
                data=emergency,
                total_tokens=0,
                cost=0.0,
                elapsed_ms=0,
                word_target=word_target,
            )
            if result:
                logger.warning(
                    "generate_v2: EMERGENCY script saved (id=%s, %d words)",
                    result.get("id"),
                    len(emergency.get("guion", "").split()),
                )
                return result

        logger.error("generate_v2: ALL modes failed — returning None")
        return None

    def _get_word_target(self) -> dict:
        """Return word/block target using the channel's duration objective.

        Reads VIDEO_AVERAGE_DURATION_MIN ± VIDEO_DURATION_DISCREPANCY_MIN
        from the channel config (DB-authoritative via config bridge, set via
        the panel "Duración — Objetivo"). Uses voice_timing.py for accurate
        word count based on the configured TTS voice rate.
        """
        cfg = self.canal_config
        test_mode = getattr(cfg, "TEST_MODE", False)

        if test_mode:
            duration_target = getattr(cfg, "TEST_VIDEO_DURATION_TARGET", 2)
        else:
            mean = getattr(cfg, "VIDEO_AVERAGE_DURATION_MIN", 15)
            disc = getattr(cfg, "VIDEO_DURATION_DISCREPANCY_MIN", 3)
            duration_target = round(
                random.uniform(max(0.5, mean - disc), mean + disc), 1
            )
            # Guard: never exceed PROD_VIDEO_DURATION_MAX regardless of
            # what the panel "Duración media" is set to. Prevents runaway
            # durations (e.g. panel set to 40 min) from producing 7h pipelines.
            max_dur = getattr(cfg, "PROD_VIDEO_DURATION_MAX", None)
            if max_dur is not None:
                duration_target = min(duration_target, float(max_dur))

        return self._compute_word_target(duration_target)

    def _estimate_output_tokens(self, target_words: int) -> int:
        """Estimate output tokens from target word count (Spanish text)."""
        return int(target_words * TOKEN_CHAR_RATIO)

    def _validate_script_json(self, data: dict) -> dict:
        """Validate that parsed JSON has all required keys.

        Args:
            data: Parsed JSON dict from GPT response.

        Returns:
            The same dict if valid.

        Raises:
            ValueError: If required keys are missing or types are wrong.
        """
        missing = REQUIRED_JSON_KEYS - set(data.keys())
        if missing:
            raise ValueError(f"Missing required JSON keys: {missing}")

        if not isinstance(data["titulo_options"], list) or len(data["titulo_options"]) < 1:
            raise ValueError("titulo_options must be a non-empty list")
        if not isinstance(data["guion"], str) or len(data["guion"]) < 100:
            raise ValueError("guion must be a string with at least 100 characters")
        if not isinstance(data["escenas"], list):
            raise ValueError("escenas must be a list")
        if not isinstance(data["emociones"], list):
            raise ValueError("emociones must be a list")
        if not isinstance(data["keywords"], list):
            raise ValueError("keywords must be a list")
        if not isinstance(data["duracion_estimada"], (int, float)):
            raise ValueError("duracion_estimada must be a number")
        # ── Optional metadata fields — regenerated by metadata_gen ──
        # These fields are regenerated from scratch by MetadataGenerator.generate().
        # Missing or empty values are NOT blocking — metadata_gen will fill them.
        for key, default in OPTIONAL_METADATA_KEYS.items():
            val = data.get(key)
            if val is None or (isinstance(val, (str, list)) and len(val) == 0):
                logger.warning(
                    "Script JSON missing optional key '%s' — will be regenerated by metadata_gen", key
                )
                data.setdefault(key, default)

        if not isinstance(data.get("emociones"), list):
            data.setdefault("emociones", [])

        # fuentes_citadas and chapters: optional, metadata_gen regenerates
        if "fuentes_citadas" not in data:
            data["fuentes_citadas"] = []
        if "chapters" not in data:
            data["chapters"] = []

        return data

    def _calculate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Calculate USD cost based on token usage.

        Args:
            prompt_tokens: Number of input/prompt tokens.
            completion_tokens: Number of output/completion tokens.

        Returns:
            Estimated cost in USD.
        """
        input_cost = (prompt_tokens / 1_000_000) * PRICE_INPUT_PER_M
        output_cost = (completion_tokens / 1_000_000) * PRICE_OUTPUT_PER_M
        return round(input_cost + output_cost, 6)

    def _generate_multi_chunk(self, content_item: dict, content_text: str, word_target: dict) -> Optional[dict]:
        """Generate script in multiple LLM calls when content is too large for single call.

        1. Outline: generate chapter structure with word targets per chapter.
        2. Per chapter: generate bloques with chunk context.
        3. Merge all chapters into final result.
        """
        import copy

        duration_target = word_target["duration_target"]
        # Number of chapters ≈ duration / 3 (each chapter ~3 min).
        # Smaller chapters are easier for the LLM to fill correctly.
        # Cap at 8 chapters max to keep overhead manageable.
        n_chapters = max(2, min(8, int(duration_target / 3)))
        logger.info(
            "Multi-chunk mode: splitting into %d chapters for %d-min video",
            n_chapters, duration_target,
        )

        # ── Step 1: Outline generation ─────────────────────────
        min_chapter_words = max(300, int(word_target["words_min"] / n_chapters * 0.95))
        outline_system = (
            f"Eres un guionista. Divide el siguiente contenido en EXACTAMENTE {n_chapters} capítulos "
            f"coherentes para un video documental de {duration_target} minutos. "
            f"Cada capítulo debe ser una unidad narrativa completa. "
            f"Asigna un número de palabras objetivo a cada capítulo. "
            f"¡OBLIGATORIO! Cada capítulo debe tener AL MENOS {min_chapter_words} palabras. "
            f"El total de TODOS los capítulos debe sumar entre {word_target['words_min']} "
            f"y {word_target['words_max']} palabras. "
            f"Si algún capítulo tiene menos de {min_chapter_words} palabras, la respuesta será RECHAZADA. "
            f"Responde SOLO con JSON."
        )
        outline_prompt = (
            f"CONTENIDO:\n{content_text[:5000]}\n\n"
            f"Genera {n_chapters} capítulos. Responde:\n"
            f'{{"chapters": [{{"title": "...", "word_target": N, "order": 1}}, ...]}}'
        )
        try:
            outline_data = self._call_with_failover(
                phase="outline",
                thinking=True,
                model=LLM_MODEL_SCRIPT,
                messages=[
                    {"role": "system", "content": outline_system},
                    {"role": "user", "content": outline_prompt},
                ],
                temperature=OPENAI_TEMPERATURE,
                max_tokens=min(OPENAI_MAX_TOKENS, 1500),
                response_format={"type": "json_object"},
            )
            chapters = outline_data.get("chapters", [])
            if not chapters:
                logger.warning("Outline returned no chapters — falling back to single-chunk")
                return self._single_generate(content_item, word_target)
        except Exception as exc:
            logger.warning("Outline generation failed: %s — falling back to single-chunk", exc)
            return self._single_generate(content_item, word_target)

        logger.info("Outline: %d chapters generated", len(chapters))

        # ── Step 2: Generate each chapter ──────────────────────
        all_bloques: list[dict] = []
        all_guion_parts: list[str] = []
        prev_context = None

        for ci, chapter in enumerate(chapters):
            ch_order = chapter.get("order", ci + 1)
            ch_title = chapter.get("title", f"Capítulo {ch_order}")
            ch_word_target = chapter.get("word_target", word_target["words_min"] // n_chapters)

            logger.info("Generating chapter %d/%d: %s (%d words target)",
                         ch_order, len(chapters), ch_title, ch_word_target)

            # Build chunk context from previous chapter
            self._chunk_context = None
            if prev_context and all_bloques:
                last_bloques = all_bloques[-2:] if len(all_bloques) >= 2 else all_bloques
                last_text = " ".join(b.get("texto", "") for b in last_bloques)
                self._chunk_context = {
                    "order": ch_order,
                    "total": len(chapters),
                    "last_paragraph": last_text[-300:],
                    "title": ch_title,
                }

            # Build chapter-specific word target (narrower range = harder to ignore)
            ch_target = {
                "words_min": max(100, int(ch_word_target * 0.8)),
                "words_max": int(ch_word_target * 1.3),
                "duration_target": max(2, int(duration_target / n_chapters)),
                "blocks_min": max(2, word_target["blocks_min"] // n_chapters),
                "blocks_max": max(4, word_target["blocks_max"] // n_chapters + 1),
            }

            system_prompt = self._build_system_prompt(
                self.canal_config,
                word_count_emphasis=self._word_count_emphasis,
                chunk_context=self._chunk_context,
                theme_context=self._theme_context,
                word_target=ch_target,
            )

            chapter_prompt = (
                f"CAPÍTULO {ch_order}/{len(chapters)}: {ch_title}\n"
                f"Genera SOLO este capítulo del guion ({ch_target['words_min']}-{ch_target['words_max']} palabras, "
                f"{ch_target['blocks_min']}-{ch_target['blocks_max']} bloques).\n\n"
                f"CONTENIDO:\n{content_text[:3000]}"
            )

            # Generate chapter with model-pool failover
            ch_data = None
            try:
                ch_data = self._call_with_failover(
                    phase="blocks",
                    thinking=True,
                    model=LLM_MODEL_SCRIPT,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": chapter_prompt},
                    ],
                    temperature=OPENAI_TEMPERATURE,
                    max_tokens=OPENAI_MAX_TOKENS,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                logger.error(
                    "Chapter %d generation failed after retries: %s — skipping", ch_order, exc,
                )
            if ch_data is None:
                continue  # both attempts failed; expansion will compensate

            ch_bloques = ch_data.get("bloques", [])
            ch_guion = ch_data.get("guion", "")
            if ch_bloques:
                all_bloques.extend(ch_bloques)
            if ch_guion:
                all_guion_parts.append(ch_guion)

            prev_context = {"order": ch_order, "bloques": ch_bloques, "guion": ch_guion}
            if ci < len(chapters) - 1:
                time.sleep(0.5)  # polite rate limiting

        if not all_bloques:
            logger.warning("Multi-chunk generated no bloques — falling back to single-chunk")
            return self._single_generate(content_item, word_target)

        # ── Step 3: Merge all chapters ─────────────────────────
        merged_guion = "\n\n[PAUSA: 1.5 segundos]\n\n".join(all_guion_parts)
        merged_chapters_data = chapters  # use outline chapters as video chapters

        # Use first chapter's titulo_options, combine keywords/hashtags
        merged_data = {
            "titulo_options": [f"{chapters[0].get('title', '')} — Documental"],
            "guion": merged_guion,
            "parrafos": [],              # multi-chunk doesn't generate paragraph-level grouping
            "bloques": all_bloques,
            "cta": None,                 # multi-chunk doesn't generate CTA at chapter level
            "escenas": [b.get("escena_descripcion", "") for b in all_bloques],
            "emociones": [{"segmento": b.get("tipo", "desarrollo"), "emocion": b.get("emocion", "")}
                          for b in all_bloques],
            "keywords": [],
            "hashtags": [],
            "duracion_estimada": word_target["duration_target"],
            "descripcion_seo": chapters[0].get("title", "Documental"),
            "chapters": chapters,
            "fuentes_citadas": [content_item.get("source", "desconocida")],
        }

        # Collect all keywords/hashtags from individual chapters
        all_kw = set()
        all_ht = set()
        for ch in chapters:
            if "keywords" in ch:
                all_kw.update(ch.get("keywords", []))
            if "hashtags" in ch:
                all_ht.update(ch.get("hashtags", []))
        merged_data["keywords"] = list(all_kw)[:25]
        merged_data["hashtags"] = list(all_ht)[:15]

        logger.info("Multi-chunk complete: %d bloques, %d words",
                     len(all_bloques), len(merged_guion.split()))
        return merged_data

    def _generate_raw(self, content_item: dict, word_target: dict = None) -> Optional[tuple]:
        """Generate script data from LLM WITHOUT saving to DB.

        Extracted from _single_generate so that the caller can run
        post-generation steps (expansion, validation) before persisting.

        Returns:
            (data, total_tokens, cost, elapsed_ms, word_target) or None on failure.
        """
        if word_target is None:
            word_target = self._get_word_target()

        content_id = content_item.get("id")
        user_prompt = self._format_user_prompt(content_item)
        system_prompt = self._build_system_prompt(
            self.canal_config,
            word_count_emphasis=self._word_count_emphasis,
            chunk_context=self._chunk_context,
            theme_context=self._theme_context,
            word_target=word_target,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        logger.info(
            "Generating script for content_id=%s, title=%s, source=%s",
            content_id,
            content_item.get("title", "")[:80],
            content_item.get("source"),
        )

        start_time = time.time()
        try:
            response = self.client.chat.completions.create(
                model=LLM_MODEL_SCRIPT,
                messages=messages,
                temperature=OPENAI_TEMPERATURE,
                max_tokens=OPENAI_MAX_TOKENS,
                response_format={"type": "json_object"},
            )
            elapsed_ms = int((time.time() - start_time) * 1000)
        except Exception as exc:
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.error(
                "AI API error (%s) for content_id=%s: %s",
                LLM_PROVIDER,
                content_id,
                exc,
            )
            self.db.log_pipeline(
                self.canal, "script", "error",
                message=f"AI API error ({LLM_PROVIDER}): {exc}",
                content_id=content_id,
                duration_ms=elapsed_ms,
            )
            return None

        prompt_tokens = response.usage.prompt_tokens if response.usage else 0
        completion_tokens = response.usage.completion_tokens if response.usage else 0
        total_tokens = prompt_tokens + completion_tokens
        cost = self._calculate_cost(prompt_tokens, completion_tokens)

        raw_text = response.choices[0].message.content.strip()
        logger.info(
            "AI response (%s) received: tokens_in=%d tokens_out=%d cost=$%.6f time=%dms",
            LLM_PROVIDER,
            prompt_tokens,
            completion_tokens,
            cost,
            elapsed_ms,
        )

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            logger.error(
                "Failed to parse GPT JSON for content_id=%s: %s",
                content_id,
                exc,
            )
            self.db.log_pipeline(
                self.canal, "script", "error",
                message=f"JSON parse error: {exc}",
                content_id=content_id,
                duration_ms=elapsed_ms,
            )
            return None

        try:
            self._validate_script_json(data)
        except ValueError as exc:
            logger.warning(
                "Script validation failed for content_id=%s: %s — attempting to fix",
                content_id,
                exc,
            )
            # Ensure minimum valid structure
            data.setdefault("titulo_options", [content_item.get("title", "Sin título")])
            data.setdefault("escenas", [])
            data.setdefault("parrafos", [])
            data.setdefault("bloques", [])
            data.setdefault("cta", None)
            data.setdefault("emociones", [])
            data.setdefault("keywords", [])
            data.setdefault("duracion_estimada", 8)
            data.setdefault("descripcion_seo", content_item.get("title", "Sin título"))
            data.setdefault("hashtags", ["#Historias"])
            data.setdefault("fuentes_citadas", [content_item.get("source", "desconocida")])
            data.setdefault("chapters", [{"time": "0:00", "title": "Introducción"}])
            if not isinstance(data.get("guion"), str) or len(data.get("guion", "")) < 100:
                self.db.log_pipeline(
                    self.canal, "script", "error",
                    message=f"Script validation failed: {exc}",
                    content_id=content_id,
                    duration_ms=elapsed_ms,
                )
                return None

        return (data, total_tokens, cost, elapsed_ms, word_target)

    def _single_generate(self, content_item: dict, word_target: dict = None) -> Optional[dict]:
        """Core single-call generate WITH persistence (backward-compat wrapper).

        Calls _generate_raw and immediately saves to DB.
        Used by multi-chunk fallback paths that need an immediate saved result.
        """
        result = self._generate_raw(content_item, word_target)
        if result is None:
            return None
        data, total_tokens, cost, elapsed_ms, word_target = result
        return self._save_and_return(
            content_item.get("id"), data, total_tokens, cost, elapsed_ms, word_target,
        )

    def _save_and_return(self, content_id, data, total_tokens, cost, elapsed_ms, word_target):
        """Save script to DB and build result dict.
        
        Supports both the new parrafos+cta format (v3) and the legacy flat bloques format (v2).
        """
        # ── Extract parrafos + CTA (new v3 format) ─────────────
        parrafos = data.get("parrafos", [])
        cta_block = data.get("cta")
        
        # Flatten bloques from parrafos (new format) with paragraph metadata
        all_bloques = []
        paragraph_boundaries = []
        for pi, p in enumerate(parrafos):
            if isinstance(p, dict):
                bloques = p.get("bloques", [])
                for bi, b in enumerate(bloques):
                    if isinstance(b, dict):
                        b["paragraph_idx"] = pi
                        b["is_last_in_paragraph"] = (bi == len(bloques) - 1)
                all_bloques.extend(bloques)
                
                # Build transition metadata for paragraph boundaries (all except last)
                if pi < len(parrafos) - 1:
                    next_p = parrafos[pi + 1] if isinstance(parrafos[pi + 1], dict) else {}
                    paragraph_boundaries.append({
                        "paragraph_idx": pi,
                        "idea_central": next_p.get("idea_central", ""),
                        "cambio_tematico": next_p.get("cambio_tematico", 5),
                    })
        
        # Backward compat: if LLM returned old flat "bloques" field
        if not all_bloques:
            all_bloques = data.get("bloques", [])
        
        # NOTE: Do NOT append CTA to all_bloques. The CTA has its own dedicated
        # video/audio section built separately by VideoEditor._build_cta().
        # Appending it here would cause the CTA text to be TTS'd twice:
        #   (1) as part of the body narration (generate_segmented),
        #   (2) as a separate cta_audio_path (orchestrator.py line 329-336).
        # Keeping it only in the separate cta_audio_path ensures proper timing:
        #   BODY (narrative) → CTA (dedicated section) → OUTRO (subscribe screen).
        #
        # if cta_block and isinstance(cta_block, dict):
        #     all_bloques.append(cta_block)
        
        # Normalize: content_id=0 (e.g. marathon synthetic content) fails FK constraint
        if not content_id:
            content_id = None
        
        try:
            script_id = self.db.insert_script(
                raw_content_id=content_id,
                canal=self.canal,
                titulo_options=data["titulo_options"],
                guion=data["guion"],
                escenas=data["escenas"],
                bloques=all_bloques,
                emociones=data.get("emociones"),
                keywords=data.get("keywords"),
                duracion_estimada=data.get("duracion_estimada"),
                token_count=total_tokens,
                cost_estimate=cost,
                emergency_mode=data.get("emergency_mode", False),
            )
        except Exception as exc:
            logger.error(
                "Failed to insert script for content_id=%s: %s",
                content_id,
                exc,
            )
            self.db.log_pipeline(
                self.canal, "script", "error",
                message=f"DB insert error: {exc}",
                content_id=content_id,
                duration_ms=elapsed_ms,
            )
            return None

        self.db.mark_content_used(content_id)
        self.db.log_pipeline(
            self.canal, "script", "success",
            message=(
                f"Script {script_id} generated. "
                f"Tokens: {total_tokens}, Cost: ${cost:.6f}, "
                f"Time: {elapsed_ms}ms"
            ),
            content_id=content_id,
            duration_ms=elapsed_ms,
        )

        guion_text = data.get("guion", "")
        actual_words = len(guion_text.split()) if guion_text else 0

        logger.info(
            "Script saved: id=%s content_id=%s titles=%s tokens=%d words=%s parrafos=%d bloques=%d",
            script_id,
            content_id,
            len(data["titulo_options"]),
            total_tokens,
            actual_words,
            len(parrafos),
            len(all_bloques),
        )

        result = {
            "id": script_id,
            "raw_content_id": content_id,
            "canal": self.canal,
            "titulo_options": data["titulo_options"],
            "guion": data["guion"],
            "escenas": data["escenas"],
            "parrafos": parrafos,
            "bloques": all_bloques,
            "bloques_json": json.dumps(all_bloques),
            "paragraph_boundaries": paragraph_boundaries,
            "cta": cta_block,
            "escenas_json": json.dumps(data["escenas"]),
            "emociones": data.get("emociones", []),
            "keywords": data.get("keywords", []),
            "duracion_estimada": data.get("duracion_estimada"),
            "descripcion_seo": data.get("descripcion_seo", ""),
            "hashtags": data.get("hashtags", []),
            "fuentes_citadas": data.get("fuentes_citadas", []),
            "chapters": data.get("chapters", []),
            "token_count": total_tokens,
            "cost_estimate": cost,
            "actual_word_count": actual_words,
            "target_word_count": word_target["words_min"] if word_target else 0,
        }
        return result

    def _expand_to_target(
        self, content_item: dict, data: dict, word_target: dict,
    ) -> dict:
        """Expand a short script iteratively until it meets the word_count target.

        Each round sends the current guion + bloques back to the model with
        instructions to add depth, detail, and citations. Continues until
        actual_words >= words_min or MAX_EXPANSION_ROUNDS is exhausted.

        Args:
            content_item: Raw content item dict (source text used for expansion).
            data: Script data dict (guion, bloques, escenas, etc.).
            word_target: Target dict from _get_word_target().

        Returns:
            The (possibly expanded) data dict with updated guion/bloques/escenas.
        """
        import copy

        guion = data.get("guion", "")
        actual_words = len(guion.split()) if guion else 0
        words_min = word_target["words_min"]
        words_max = word_target["words_max"]
        duration_target = word_target["duration_target"]
        content_text = content_item.get("text", "")

        if actual_words >= words_min or MAX_EXPANSION_ROUNDS < 1:
            return data  # already meets target or expansion disabled

        logger.info(
            "Expansion loop: %d words (need %d-%d for %.1f min). Up to %d rounds.",
            actual_words, words_min, words_max, duration_target, MAX_EXPANSION_ROUNDS,
        )

        best_data = copy.deepcopy(data)
        best_words = actual_words
        stale_rounds = 0

        for round_num in range(1, MAX_EXPANSION_ROUNDS + 1):
            bloques = best_data.get("bloques", [])
            # Serialize current bloques for the prompt (compact)
            bloques_preview = []
            for b in bloques[:20]:  # cap to avoid huge prompts
                if isinstance(b, dict):
                    bloques_preview.append(
                        f"[{b.get('tipo', '')}] {b.get('texto', '')[:120]}"
                    )
            bloques_text = "\n".join(bloques_preview) if bloques_preview else "Sin bloques"

            # Build expansion system prompt with escalating emphasis
            expansion_emphasis = 1.0 + round_num * 0.5
            expansion_target = {
                "words_min": words_min,
                "words_max": words_max,
                "duration_target": duration_target,
                "blocks_min": max(word_target["blocks_min"], len(bloques) + 2),
                "blocks_max": max(word_target["blocks_max"], len(bloques) + 8),
            }

            system_prompt = self._build_system_prompt(
                self.canal_config,
                word_count_emphasis=expansion_emphasis,
                chunk_context=None,
                theme_context=self._theme_context,
                word_target=expansion_target,
            )

            # NOTE: we inline a correction marker to avoid mutating _format_user_prompt
            user_prompt = (
                f"🔴 CORRECCIÓN DE AMPLIACIÓN (ronda {round_num}/{MAX_EXPANSION_ROUNDS}):\n\n"
                f"Tu guion tiene SOLAMENTE {best_words} palabras. "
                f"El objetivo es AL MENOS {words_min} palabras "
                f"({duration_target} minutos de video).\n\n"
                f"GUION ACTUAL (íntegro):\n{guion}\n\n"
                f"BLOQUES ACTUALES:\n{bloques_text}\n\n"
                f"CONTENIDO FUENTE (apóyate en él para expandir):\n"
                f"{content_text[:3000]}\n\n"
                f"⚠️ INSTRUCCIONES ESTRICTAS:\n"
                f"1. AMPLÍA el guion EXISTENTE. Mantén estructura, tono y formato JSON.\n"
                f"2. AÑADE {expansion_target['blocks_max'] - len(bloques)} bloques NUEVOS con "
                f"detalles sensoriales, contexto histórico, citas de las fuentes y reflexiones.\n"
                f"3. PROFUNDIZA los bloques existentes que sean cortos (<40 palabras).\n"
                f"4. El guion completo debe tener AL MENOS {words_min} palabras. "
                f"CUENTA las palabras ANTES de entregar.\n"
                f"5. NO repitas frases ni uses relleno. Cada bloque nuevo debe aportar "
                f"contenido original basado en las fuentes."
            )

            try:
                expanded = self._call_with_failover(
                    phase="expand",
                    thinking=True,
                    model=LLM_MODEL_SCRIPT,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=OPENAI_TEMPERATURE,
                    max_tokens=OPENAI_MAX_TOKENS,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                logger.warning("Expansion round %d API/parse error: %s", round_num, exc)
                stale_rounds += 1
                if stale_rounds >= EXPANSION_STALE_ROUNDS:
                    logger.warning(
                        "Expansion stuck after %d stale rounds. Stopping at %d words.",
                        stale_rounds, best_words,
                    )
                    break
                continue

            new_guion = expanded.get("guion", "")
            new_words = len(new_guion.split()) if new_guion else 0

            # Normalize bloques (handle v2 flat / v3 parrafos-based)
            new_bloques_raw = expanded.get("bloques", [])
            new_bloques = []
            if new_bloques_raw and isinstance(new_bloques_raw[0], dict) and "bloques" in new_bloques_raw[0]:
                for p in new_bloques_raw:
                    new_bloques.extend(p.get("bloques", []))
            else:
                new_bloques = new_bloques_raw

            if new_words > best_words and len(new_guion) > len(guion):
                # Accept the expanded version
                best_data["guion"] = new_guion
                best_data["bloques"] = new_bloques
                best_data["escenas"] = expanded.get("escenas", best_data.get("escenas", []))
                best_data["emociones"] = expanded.get("emociones", best_data.get("emociones", []))
                best_data["keywords"] = expanded.get("keywords", best_data.get("keywords", []))
                best_data["titulo_options"] = expanded.get("titulo_options", best_data.get("titulo_options", []))
                best_data["parrafos"] = expanded.get("parrafos", best_data.get("parrafos", []))
                best_data["duracion_estimada"] = _duration_for_words(self.canal_config, new_words)
                best_data["descripcion_seo"] = expanded.get("descripcion_seo", best_data.get("descripcion_seo", ""))
                best_data["hashtags"] = expanded.get("hashtags", best_data.get("hashtags", []))
                best_data["fuentes_citadas"] = expanded.get("fuentes_citadas", best_data.get("fuentes_citadas", []))
                best_data["chapters"] = expanded.get("chapters", best_data.get("chapters", []))
                best_words = new_words
                guion = new_guion
                stale_rounds = 0

                logger.info(
                    "Expansion round %d: %d words (+%d)",
                    round_num, best_words, new_words - actual_words if round_num == 1 else best_words,
                )
            else:
                stale_rounds += 1
                logger.warning(
                    "Expansion round %d: no growth (%d words ≤ %d best)",
                    round_num, new_words, best_words,
                )

            if best_words >= words_min:
                logger.info(
                    "Expansion SUCCESS after %d rounds: %d words ≥ %d",
                    round_num, best_words, words_min,
                )
                break

            if stale_rounds >= EXPANSION_STALE_ROUNDS:
                logger.warning(
                    "Expansion stalled for %d rounds. Stopping at %d words.",
                    stale_rounds, best_words,
                )
                break

        # Ensure duracion_estimada reflects actual word count
        if best_data.get("duracion_estimada", 0) < 1:
            best_data["duracion_estimada"] = _duration_for_words(self.canal_config, best_words)

        if best_words < words_min:
            logger.warning(
                "Expansion exhausted after %d rounds. Best: %d words (target=%d). "
                "Proceeding with best available.",
                MAX_EXPANSION_ROUNDS, best_words, words_min,
            )
        else:
            logger.info(
                "Expansion final: %d words (target %d-%d)",
                best_words, words_min, words_max,
            )

        return best_data

    # ═══════════════════════════════════════════════════════════════
    #  Narrative Quality Checks (anti-repetition, coherence, hook)
    # ═══════════════════════════════════════════════════════════════

    def _check_narrative_quality(self, enriched: dict) -> dict:
        """Post-generation quality check for repetition, coherence and hook.

        Analyzes the enriched script to detect:
        1. Thematic repetition: sentences/blocks that say the same thing
        2. Narrative coherence: story has clear beginning and end
        3. Hook quality: opening blocks are engaging (no weak intros)

        Args:
            enriched: Dict from _enrich_blocks() with parrafos, bloques, guion.

        Returns:
            dict with:
                passes: bool — True if all checks pass
                repetition_score: float 0-1 — higher means more repetition
                coherence_score: float 0-1 — 1 = perfect structure
                hook_score: float 0-1 — 1 = strong hook
                issues: list of descriptive strings
                problem_paragraphs: list[int] — paragraph indices to regenerate
                avoid_themes: list[str] — themes the LLM should avoid
        """
        parrafos = enriched.get("parrafos", [])
        if not parrafos:
            return self._empty_check_result("No paragraphs found")

        all_bloques = enriched.get("bloques", [])
        if len(all_bloques) < MIN_BLOCKS_FOR_COHERENCE_CHECK:
            return {
                "passes": True,
                "repetition_score": 0.0,
                "coherence_score": 1.0,
                "hook_score": 1.0,
                "issues": [],
                "problem_paragraphs": [],
                "avoid_themes": [],
                "notes": "Script too short for quality checks",
            }

        issues = []
        problem_paragraphs = set()
        avoid_themes = set()

        # ── 1. Repetition check ──────────────────────────
        rep_result = self._check_repetition(all_bloques, parrafos)
        issues.extend(rep_result["issues"])
        problem_paragraphs.update(rep_result["problem_paragraphs"])
        avoid_themes.update(rep_result["avoid_themes"])

        # ── 2. Coherence check ───────────────────────────
        coh_result = self._check_coherence(all_bloques, parrafos)
        issues.extend(coh_result["issues"])
        if coh_result.get("missing_hook_para") is not None:
            problem_paragraphs.add(coh_result["missing_hook_para"])

        # ── 3. Hook quality check ────────────────────────
        hook_result = self._check_hook_quality(all_bloques)
        issues.extend(hook_result["issues"])
        if hook_result.get("weak_hook_para") is not None:
            problem_paragraphs.add(hook_result["weak_hook_para"])

        # ── Aggregate scores ─────────────────────────────
        n_paragraphs = len(parrafos)
        n_repetition_issues = len(rep_result["issues"])
        n_coherence_issues = len(coh_result["issues"])
        n_hook_issues = len(hook_result["issues"])

        repetition_score = min(1.0, n_repetition_issues / max(1, n_paragraphs))
        coherence_score = max(0.0, 1.0 - n_coherence_issues * 0.5)
        hook_score = max(0.0, 1.0 - n_hook_issues * 0.5)
        total_issues = n_repetition_issues + n_coherence_issues + n_hook_issues

        # Determine if regeneration is needed
        needs_regeneration = (
            len(problem_paragraphs) > 0
            and total_issues > 0
        )

        return {
            "passes": not needs_regeneration,
            "repetition_score": round(repetition_score, 3),
            "coherence_score": round(coherence_score, 3),
            "hook_score": round(hook_score, 3),
            "issues": issues,
            "problem_paragraphs": sorted(problem_paragraphs),
            "avoid_themes": sorted(avoid_themes),
            "total_issues": total_issues,
        }

    @staticmethod
    def _empty_check_result(reason: str) -> dict:
        return {
            "passes": True,
            "repetition_score": 0.0,
            "coherence_score": 1.0,
            "hook_score": 1.0,
            "issues": [reason],
            "problem_paragraphs": [],
            "avoid_themes": [],
        }

    def _check_repetition(self, all_bloques: list, parrafos: list) -> dict:
        """Check for thematic repetition across paragraphs.

        Uses sentence-level similarity analysis and keyword overlap
        to detect blocks that rephrase the same ideas.
        """
        issues = []
        problem_paragraphs = set()
        avoid_themes = set()

        if len(all_bloques) < 3:
            return {"issues": issues, "problem_paragraphs": problem_paragraphs, "avoid_themes": avoid_themes}

        # Build paragraph→blocks mapping
        para_blocks: dict[int, list[dict]] = {}
        for b in all_bloques:
            pi = b.get("paragraph_idx", 0)
            if pi not in para_blocks:
                para_blocks[pi] = []
            para_blocks[pi].append(b)

        # Extract full text per paragraph and per block
        para_texts = {}
        for pi, blocks in para_blocks.items():
            para_texts[pi] = " ".join(b.get("texto", "") for b in blocks)

        block_texts = [b.get("texto", "") for b in all_bloques]

        # ── Sentence-level similarity across paragraphs ────
        # Extract all sentences with their paragraph index
        all_sentences = []
        for b in all_bloques:
            text = b.get("texto", "")
            pi = b.get("paragraph_idx", 0)
            sents = re.split(r'(?<=[.!?])\s+', text)
            for s in sents:
                s = s.strip()
                if len(s) > 25:  # meaningful sentences only
                    all_sentences.append((s, pi))

        # Compare sentences from DIFFERENT paragraphs only
        flagged_pairs = []
        flagged_block_indices = set()
        for i in range(len(all_sentences)):
            for j in range(i + 1, len(all_sentences)):
                si, pi = all_sentences[i]
                sj, pj = all_sentences[j]
                if pi == pj:
                    continue  # same paragraph is fine
                ratio = SequenceMatcher(None, si.lower(), sj.lower()).ratio()
                if ratio > REPETITION_SIMILARITY_THRESHOLD:
                    flagged_pairs.append((i, j, ratio, si[:80], sj[:80]))
                    flagged_block_indices.add(i)
                    flagged_block_indices.add(j)
                    problem_paragraphs.add(pj)  # later paragraph is the "repeater"

        # Report if too many pairs
        total_pairs = max(1, len(all_sentences) * (len(all_sentences) - 1) // 2)
        actual_pairs = len(flagged_pairs)
        if actual_pairs > 0:
            ratio = actual_pairs / max(1, total_pairs)
            if ratio > MAX_REPETITION_PAIR_RATIO or len(flagged_block_indices) / max(1, len(all_sentences)) > MAX_REPETITION_BLOCK_RATIO:
                issues.append(
                    f"Repeticion tematica: {actual_pairs} pares de oraciones similares "
                    f"(>{REPETITION_SIMILARITY_THRESHOLD:.0%} similitud) entre parrafos distintos"
                )
                # Log examples for debugging
                for a, b, r, s1, s2 in flagged_pairs[:5]:
                    logger.debug(f"  Repeticion: [{a}] vs [{b}] ({r:.0%}): {s1} ≈ {s2}")

        # ── Conceptual keyword overlap across paragraphs ──
        # Extract key nouns/phrases (words > 5 chars) and check overuse
        concept_keywords = [
            "proporción áurea", "número áureo", "proporcione",
            "escala musical", "escalas pentatónicas", "escalas heptatónicas",
            "intervalos armónicos", "músico compone", "armonía matemática",
            "templo", "calendario", "360 día", "ciclos",
            "sistema operativo", "código", "ADN cultural",
            "no necesitaron", "no construyeron imperio", "no arrasaron",
            "campo de batalla", "guerra no terminó", "no fue militar",
            "linaje", "sacrificio", "pruebas de sangre", "hermetismo",
            "élite que custodiaba", "rituales", "registro akáshico",
            "núcleo de dato", "conocimiento akáshico",
            "cada vez que un arquitecto", "cada vez que un músico",
            "cada vez que medimos",
        ]
        concept_usage = {kw: set() for kw in concept_keywords}
        for b in all_bloques:
            text = b.get("texto", "").lower()
            pi = b.get("paragraph_idx", 0)
            for kw in concept_keywords:
                if kw in text:
                    concept_usage[kw].add(pi)

        # Flag concepts used in 3+ different paragraphs
        overused = []
        for kw, paras in concept_usage.items():
            if len(paras) >= 3:
                overused.append(kw)
                for p in paras:
                    problem_paragraphs.add(p)
                avoid_themes.add(kw)

        if overused:
            # Only flag as issue if there are multiple overused concepts
            if len(overused) >= 3:
                issues.append(
                    f"Conceptos repetidos en ≥3 parrafos: {', '.join(overused[:6])}"
                    + ("..." if len(overused) > 6 else "")
                )

        return {
            "issues": issues,
            "problem_paragraphs": problem_paragraphs,
            "avoid_themes": avoid_themes,
        }

    def _check_coherence(self, all_bloques: list, parrafos: list) -> dict:
        """Check that the script has a coherent narrative arc.

        Verifies:
        - First paragraph starts with hook-type blocks
        - Last paragraph ends with closure-type blocks
        - Story has both intro and conclusion
        """
        issues = []

        if not all_bloques or not parrafos:
            return {"issues": issues}

        # Check for hook at the beginning
        first_blocks = all_bloques[:3]
        has_hook = any(b.get("tipo") == "hook" for b in first_blocks if isinstance(b, dict))
        if not has_hook:
            first_para = 0
            issues.append("Falta bloque de tipo 'hook' en los primeros 3 bloques (enganche inicial debil)")
            return {"issues": issues, "missing_hook_para": first_para}

        # Check for closure at the end
        last_blocks = all_bloques[-3:]
        has_cierre = any(b.get("tipo") == "cierre" for b in last_blocks if isinstance(b, dict))
        if not has_cierre:
            last_para = all_bloques[-1].get("paragraph_idx", len(parrafos) - 1) if all_bloques else 0
            issues.append("Falta bloque de tipo 'cierre' al final del guion")

        # Check that last paragraph has is_last_in_paragraph markers working
        last_para_blocks = [b for b in all_bloques if b.get("paragraph_idx") == len(parrafos) - 1]
        if last_para_blocks and not any(b.get("is_last_in_paragraph") for b in last_para_blocks):
            pass  # minor, not critical

        return {"issues": issues}

    def _check_hook_quality(self, all_bloques: list) -> dict:
        """Check the quality of the hook / opening blocks.

        Flags:
        - Weak opening phrases ("En este video vamos a...")
        - First sentence not impactful enough
        - Missing retention phrase in introduction
        """
        issues = []
        result = {"issues": issues}
        if not all_bloques:
            return result

        first_block_text = all_bloques[0].get("texto", "") if all_bloques else ""

        # Check for banned weak opening patterns
        first_text_lower = first_block_text.lower()
        for pattern in BANNED_OPENING_PATTERNS:
            if re.search(pattern, first_text_lower):
                issues.append(f"Gancho debil detectado: '{first_block_text[:80]}...' usa frase prohibida")
                return {
                    "issues": issues,
                    "weak_hook_para": all_bloques[0].get("paragraph_idx", 0),
                }

        # Check first sentence length — too short is weak
        first_sentences = re.split(r'(?<=[.!?])\s+', first_block_text)
        if first_sentences:
            first_sentence = first_sentences[0].strip()
            if len(first_sentence.split()) < 8:
                issues.append(f"Primera oracion demasiado corta ({len(first_sentence.split())} palabras): '{first_sentence[:80]}'")

        # Check early blocks for engagement markers
        early_texts = " ".join(b.get("texto", "") for b in all_bloques[:3]).lower()
        if "?" not in early_texts and "!" not in early_texts:
            issues.append("Primeros bloques no contienen preguntas ni exclamaciones — posible falta de gancho emocional")

        # Check for retention phrase in introduction blocks (first ~5 blocks)
        intro_text = " ".join(b.get("texto", "") for b in all_bloques[:5]).lower()
        retention_patterns = [
            r"qu[eé]date",           # "quédate", "quedate"
            r"no\s+te\s+vayas",      # "no te vayas"
            r"sigue\s+conmigo",      # "sigue conmigo"
            r"no\s+te\s+pierdas",    # "no te pierdas"
            r"espera\s+a\s+ver",     # "espera a ver"
            r"acomp[aá][ñn]ame",     # "acompañame", "acompaname"
            r"no\s+te\s+lo\s+pierdas", # "no te lo pierdas"
            r"descubre\s+(c[oó]mo|qu[eé]|por\s+qu[eé])", # "descubre cómo/que/por qué"
            r"vas\s+a\s+(querer|necesitar)\s+(ver|saber)", # "vas a querer/necesitar ver/saber"
            r"te\s+lo\s+vas\s+a\s+perder", # "te lo vas a perder"
        ]
        has_retention = any(re.search(p, intro_text) for p in retention_patterns)
        if not has_retention:
            issues.append(
                "No se detecta frase de retencion explicita en los primeros bloques "
                "— el gancho podria ser debil. Debe incluir una frase que invite al "
                "espectador a quedarse (ej: 'quedate hasta el final', 'no te vayas', "
                "'sigue conmigo', etc.)"
            )
            # Mark the first paragraph as weak to trigger regeneration
            result["weak_hook_para"] = all_bloques[0].get("paragraph_idx", 0)

        return result

    def _regenerate_problematic_paragraphs(
        self, enriched: dict, check_result: dict, content_item: dict,
    ) -> Optional[dict]:
        """Re-generate problematic paragraphs via LLM while keeping good ones.

        Sends the LLM the full script context plus explicit instructions about
        which themes to avoid and what to replace. Only the problematic
        paragraphs are rewritten; good ones are preserved.

        Args:
            enriched: Full enriched script dict from _enrich_blocks().
            check_result: Result from _check_narrative_quality().
            content_item: Raw content dict for context.

        Returns:
            Updated enriched dict with regenerated paragraphs, or None on failure.
        """
        if not enriched or not check_result:
            return None

        parrafos = enriched.get("parrafos", [])
        problem_indices = set(check_result.get("problem_paragraphs", []))
        avoid_themes = check_result.get("avoid_themes", [])

        if not problem_indices:
            logger.info("_regenerate_problematic_paragraphs: nothing to regenerate")
            return enriched

        # Separate good vs problematic paragraphs
        good_paras = [p for i, p in enumerate(parrafos) if i not in problem_indices]
        bad_paras = [(i, parrafos[i]) for i in sorted(problem_indices) if i < len(parrafos)]

        if not bad_paras:
            return enriched

        logger.info(
            "_regenerate_problematic_paragraphs: regenerating %d/%d paragraphs (indices: %s, avoid: %s)",
            len(bad_paras), len(parrafos),
            sorted(problem_indices),
            avoid_themes[:5],
        )

        # Build context from good paragraphs
        good_context = ""
        for p in good_paras:
            if isinstance(p, dict):
                idea = p.get("idea_central", "")
                blocks_text = " ".join(
                    b.get("texto", "") for b in p.get("bloques", [])
                    if isinstance(b, dict)
                )[:300]
                good_context += f"\n[PÁRRAFO BUENO — CONSERVAR]: {idea}\n{blocks_text}\n"

        # Build description of what to regenerate and what to avoid
        bad_descriptions = []
        for idx, p in bad_paras:
            blocks_text = " ".join(
                b.get("texto", "") for b in p.get("bloques", [])
                if isinstance(b, dict)
            )[:300]
            n_blocks = len(p.get("bloques", []))
            bad_descriptions.append(
                f"Párrafo {idx} ({n_blocks} bloques) — A REGENERAR:\n{blocks_text}"
            )

        avoid_text = ""
        if avoid_themes:
            avoid_text = (
                f"\n⛔ TEMAS/CONCEPTOS PROHIBIDOS (ya aparecen en otros párrafos):\n"
                + "\n".join(f"  - {t}" for t in avoid_themes[:10])
                + "\n\nNO repitas ninguno de estos conceptos en los nuevos bloques."
            )

        # Build regeneration prompt
        system_prompt = (
            "Eres un editor de guiones documentales. Tu tarea es REGENERAR "
            "párrafos específicos de un guion que tienen repeticiones temáticas.\n\n"
            "Recibirás:\n"
            "1. Los párrafos BUENOS (que debes CONSERVAR como están)\n"
            "2. Los párrafos PROBLEMÁTICOS (que debes REESCRIBIR)\n"
            "3. Una lista de temas PROHIBIDOS (no debes mencionarlos)\n\n"
            "REGLAS:\n"
            "- Reescribe SOLO los párrafos problemáticos.\n"
            "- Introduce ideas GENUINAMENTE NUEVAS, no reformules lo mismo.\n"
            "- Respeta el tono y estilo del canal.\n"
            "- Cada bloque nuevo debe tener tipo, emocion, texto, escena_descripcion, "
            "search_query_en, media_tipo, media_duracion.\n"
            "- search_query_en SIEMPRE en inglés.\n"
            "- Mantén el MISMO número de bloques por párrafo que el original.\n"
            "- La historia debe fluir naturalmente entre los párrafos buenos y los regenerados.\n\n"
            f"{avoid_text}\n\n"
            "Responde ÚNICAMENTE con JSON: {\"regenerated_parrafos\": [...]} "
            "donde cada elemento tiene la misma estructura que los parrafos originales "
            "(idea_central, cambio_tematico, bloques con todos sus campos)."
        )

        user_prompt = (
            f"Tema del video: {content_item.get('title', 'Documental')}\n\n"
            f"=== PÁRRAFOS BUENOS (CONSERVAR) ==={good_context}\n\n"
            f"=== PÁRRAFOS A REGENERAR ===\n"
            + "\n---\n".join(bad_descriptions)
            + f"\n\n{avoid_text}\n\n"
            f"Regenera los párrafos problemáticos. Mantén el mismo número de bloques "
            f"por párrafo. Cada bloque debe tener TODOS los campos completos."
        )

        try:
            data = self._call_with_failover(
                phase="quality_check",
                thinking=False,
                model=LLM_MODEL_SCRIPT,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.7,
                max_tokens=min(4000, OPENAI_MAX_TOKENS),
                response_format={"type": "json_object"},
            )
            regenerated_paras = data.get("regenerated_parrafos", [])

            if not isinstance(regenerated_paras, list) or not regenerated_paras:
                logger.warning(
                    "_regenerate_problematic_paragraphs: LLM returned no regenerated paragraphs"
                )
                return enriched  # return original, don't break the pipeline

            # Validate paragraph structure
            for p in regenerated_paras:
                if not isinstance(p, dict) or "bloques" not in p or not p.get("bloques"):
                    logger.warning(
                        "_regenerate_problematic_paragraphs: invalid paragraph in response, "
                        "keeping original"
                    )
                    return enriched

            # Merge: replace problematic paragraphs with regenerated ones
            new_parrafos = []
            regen_idx = 0
            for i in range(len(parrafos)):
                if i in problem_indices and regen_idx < len(regenerated_paras):
                    new_parrafos.append(regenerated_paras[regen_idx])
                    regen_idx += 1
                    logger.info(
                        "_regenerate_problematic_paragraphs: replaced paragraph %d", i
                    )
                else:
                    new_parrafos.append(parrafos[i])

            # Rebuild flat bloques list and guion
            new_bloques = []
            new_guion_parts = []
            for pi, p in enumerate(new_parrafos):
                if isinstance(p, dict):
                    bloques = p.get("bloques", [])
                    for bi, b in enumerate(bloques):
                        if isinstance(b, dict):
                            b["paragraph_idx"] = pi
                            b["is_last_in_paragraph"] = (bi == len(bloques) - 1)
                            new_bloques.append(b)
                            new_guion_parts.append(b.get("texto", ""))

            # Update enriched dict
            new_enriched = dict(enriched)
            new_enriched["parrafos"] = new_parrafos
            new_enriched["bloques"] = new_bloques
            new_enriched["guion"] = "\n\n".join(new_guion_parts)
            new_enriched["escenas"] = [
                {"descripcion": b.get("escena_descripcion", "")}
                for b in new_bloques
            ]
            new_enriched["emociones"] = [
                b.get("emocion", "") for b in new_bloques if b.get("emocion")
            ]

            logger.info(
                "_regenerate_problematic_paragraphs: SUCCESS — %d paragraphs regenerated, "
                "new total: %d paragraphs, %d blocks",
                len(bad_paras), len(new_parrafos), len(new_bloques),
            )

            return new_enriched

        except json.JSONDecodeError as exc:
            logger.warning(
                "_regenerate_problematic_paragraphs: JSON parse error: %s", exc
            )
            return enriched
        except Exception as exc:
            logger.error(
                "_regenerate_problematic_paragraphs: LLM call failed: %s", exc
            )
            return enriched

    def generate(self, content_item: dict) -> Optional[dict]:
        """Generate a script from a single raw_content row.

        Uses the fault-tolerant retry wrapper (v22.1) which performs
        pre-flight API connectivity checks, up to 3 generation attempts
        with exponential backoff, and content-structure validation before
        returning. Falls back to emergency mode if all retries fail.
        """
        palabras_obj = content_item.get("_palabras_objetivo", None)
        return self._generate_with_retry(
            content_item, palabras_objetivo=palabras_obj,
        )

    def generate_batch(self, count: int = 1) -> list[dict]:
        """Generate scripts for multiple unused content items.

        Args:
            count: Number of scripts to generate.

        Returns:
            List of script dicts that were successfully generated.
        """
        items = self.db.get_unused_content(canal=self.canal, limit=count)
        if not items:
            logger.info("No unused content available for canal=%s", self.canal)
            return []

        logger.info("Generating batch of %d scripts for canal=%s", len(items), self.canal)
        results = []
        for item in items:
            script = self.generate(item)
            if script is not None:
                results.append(script)

        logger.info(
            "Batch complete: %d/%d scripts generated successfully",
            len(results),
            len(items),
        )
        return results

    # ── Marathon mode: ~1h deep-dive video generation ─────────────

    def generate_marathon(
        self,
        content_items: list,
        canal_config,
        duration_target: int = 60,
        num_sections: int = 12,
        narrative_format: str = "top_cases",
        outline_chapters: int = 15,
        words_min: int = 8000,
        words_max: int = 12000,
        blocks_min: int = 50,
        blocks_max: int = 90,
        llm_max_batches: int = 150,
        llm_max_empty_strikes: int = 20,
        title_format: str = "",
    ) -> Optional[dict]:
        """Generate a long-form marathon script (~1h video).

        This is a specialized entry point that:
        - Uses a marathon-specific outline prompt (build_marathon_outline_prompt)
        - Scales word/block targets for 60-minute content
        - Removes the hard caps on max_blocks, duration, and batch count
        - Uses rich source material (deep-scraped Wikipedia articles)

        Args:
            content_items: List of deeply scraped content items.
            canal_config: The channel's config module.
            duration_target: Target video duration in minutes.
            num_sections: Number of independent sections/cases.
            narrative_format: "top_cases" | "deep_story" | "historical_collapse"
            outline_chapters: Number of chapters in the outline.
            words_min/max: Target word count range.
            blocks_min/max: Target block count range.
            llm_max_batches: Max batch iterations.
            llm_max_empty_strikes: Tolerance for empty generations.
            title_format: Optional title format string.

        Returns:
            Script dict or None on failure.
        """
        logger.info(
            "[MARATHON][%s] generate_marathon: %dmin, %d sections, format=%s",
            self.canal, duration_target, num_sections, narrative_format,
        )

        # ── 1. Build the marathon outline prompt ──────────────
        from prompts.base_prompts import build_marathon_outline_prompt as build_marathon_fn

        # Combine all source text
        combined_text = "\n\n---\n\n".join(
            f"FUENTE {i+1}: {item.get('title', 'Sin título')}\n{item.get('text', '')}"
            for i, item in enumerate(content_items)
        )

        # Compute word target for marathon scale
        from config.voice_timing import words_for_duration
        words_obj = words_for_duration(canal_config, duration_minutes=duration_target)
        words_target_for_prompt = words_obj

        # ── 2. Generate marathon outline ──────────────────────
        logger.info("[MARATHON][%s] Generating marathon outline (%d chapters)...", self.canal, outline_chapters)
        outline = self._generate_outline(
            content_item=None,
            content_text=combined_text[:8000],  # trim for prompt limit
            duration_min=duration_target,
            word_target=words_target_for_prompt,
            canal_config=canal_config,
            marathon_mode=True,
            marathon_params={
                "num_sections": num_sections,
                "narrative_format": narrative_format,
                "outline_chapters": outline_chapters,
            },
        )

        if not outline or not outline.get("chapters"):
            logger.warning("[MARATHON][%s] Outline generation failed", self.canal)
            return None

        logger.info(
            "[MARATHON][%s] Outline: %d chapters generated",
            self.canal, len(outline.get("chapters", [])),
        )

        # ── 3. Generate blocks with marathon scale ────────────
        # Override the guards for marathon mode
        total_scenes_min = max(30, num_sections * 3)
        total_scenes_max = max(50, num_sections * 5)

        # Use generate_v2 with marathon-scale parameters
        # Build a synthetic content item that wraps all sources
        marathon_content = {
            "title": content_items[0].get("title", "Documental") if content_items else "Documental",
            "text": combined_text[:12000],  # rich source material
            "source": "wikipedia_deep",
            "score": 100,
            "canal": self.canal,
            "id": (content_items[0].get("id") or None) if content_items else None,
            "_marathon": True,
            "_marathon_outline": outline,
            "_marathon_params": {
                "duration_target": duration_target,
                "num_sections": num_sections,
                "narrative_format": narrative_format,
                "words_min": words_min,
                "words_max": words_max,
                "blocks_min": blocks_min,
                "blocks_max": blocks_max,
                "llm_max_batches": llm_max_batches,
                "llm_max_empty_strikes": llm_max_empty_strikes,
            },
        }

        # ── 4. Override generate_v2's internal caps ───────────
        # Store original values to restore later
        original_max_blocks = getattr(canal_config, "PROD_SCRIPT_BLOCKS_MAX", 18)
        original_duration_max = getattr(canal_config, "PROD_VIDEO_DURATION_MAX", 14)

        # Temporarily override for marathon scale
        if hasattr(canal_config, "__dict__"):
            canal_config.__dict__["PROD_SCRIPT_BLOCKS_MAX"] = blocks_max
            canal_config.__dict__["PROD_VIDEO_DURATION_MAX"] = duration_target + 10
        else:
            # config is a SimpleNamespace, use setattr
            import types
            if isinstance(canal_config, types.SimpleNamespace):
                canal_config.PROD_SCRIPT_BLOCKS_MAX = blocks_max
                canal_config.PROD_VIDEO_DURATION_MAX = duration_target + 10

        try:
            # Use the standard generate path with marathon scale
            # We override the config temporarily so the guards in generate_v2
            # allow the large word/block counts
            script = self._generate_with_retry(
                marathon_content,
                palabras_objetivo=words_obj,
                marathon_overrides={
                    "max_blocks": blocks_max,
                    "max_batches": llm_max_batches,
                    "max_empty_strikes": llm_max_empty_strikes,
                    "outline": outline,
                    "word_target_override": {
                        "words_min": words_min,
                        "words_max": words_max,
                        "duration_target": duration_target,
                        "blocks_min": blocks_min,
                        "blocks_max": blocks_max,
                        "palabras_objetivo": words_obj,
                    },
                },
            )
        finally:
            # Restore original config values
            if hasattr(canal_config, "__dict__"):
                canal_config.__dict__["PROD_SCRIPT_BLOCKS_MAX"] = original_max_blocks
                canal_config.__dict__["PROD_VIDEO_DURATION_MAX"] = original_duration_max
            else:
                import types
                if isinstance(canal_config, types.SimpleNamespace):
                    canal_config.PROD_SCRIPT_BLOCKS_MAX = original_max_blocks
                    canal_config.PROD_VIDEO_DURATION_MAX = original_duration_max

        if script:
            words = len(script.get("guion", "").split()) if script.get("guion") else 0
            logger.info(
                "[MARATHON][%s] Script generated: %d words, %s",
                self.canal, words, script.get("id"),
            )

            # Override title with marathon format if provided
            if title_format and script:
                formatted_title = title_format.replace("{N}", str(num_sections))
                # The title will be set by the LLM, but we add the marathon tag
                script["_marathon_title_format"] = formatted_title

        return script
