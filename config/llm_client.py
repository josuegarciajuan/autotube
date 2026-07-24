"""Centralized LLM client factory with DeepSeek V4 thinking-mode management.

DeepSeek V4 models (deepseek-v4-flash, deepseek-v4-pro) have thinking mode
ENABLED by default since 2026-07-24. This module provides fine-grained control
via ``create_llm_client(enable_thinking=...)``.

Multi-model tiers (set in .env):
  - LLM_MODEL_SCRIPT   → script generation  (default: deepseek-v4-pro)
  - LLM_MODEL_CREATIVE → metadata, thumbnails, shorts, marketing
  - LLM_MODEL           → fallback / simple tasks (default: deepseek-v4-flash)

Usage:
    from config.llm_client import create_llm_client

    # Tier 3 — no thinking (theme extraction, comments, classification)
    client = create_llm_client()
    # → auto-injects extra_body={"thinking": {"type": "disabled"}}

    # Tier 2 — creative with thinking (metadata, thumbnails, shorts)
    client = create_llm_client(enable_thinking=True)
    # → auto-injects extra_body={"thinking": {"type": "enabled"}}
    #   + reasoning_effort="high"

    # Tier 1 — script generation (model override + thinking)
    client = create_llm_client(enable_thinking=True)
    # → use model=LLM_MODEL_SCRIPT in the .create() call

For non-DeepSeek base URLs (e.g., OpenAI vision), the client behaves as
a plain OpenAI instance with no modifications.

Ref: https://api-docs.deepseek.com/guides/thinking_mode
Ref: https://api-docs.deepseek.com/updates (deprecation 2026-07-24)
"""

import logging
from openai import OpenAI
from config.settings import LLM_API_KEY, LLM_BASE_URL

logger = logging.getLogger(__name__)


def create_llm_client(
    api_key=None,
    base_url=None,
    enable_thinking=False,
    reasoning_effort="high",
    **kwargs,
):
    """Create an OpenAI-compatible client with configurable thinking mode.

    Args:
        api_key: API key override (defaults to LLM_API_KEY from settings).
        base_url: Base URL override (defaults to LLM_BASE_URL from settings).
        enable_thinking: If True, inject thinking=enabled + reasoning_effort.
            If False (default), inject thinking=disabled.
        reasoning_effort: Effort level when thinking is enabled ("high" or "max").
        **kwargs: Additional kwargs forwarded to OpenAI (timeout, max_retries, etc.).

    Returns:
        OpenAI client instance. If base_url points to DeepSeek, the
        ``chat.completions.create`` method is patched to auto-inject
        the appropriate ``extra_body``.
    """
    effective_api_key = api_key or LLM_API_KEY
    effective_base_url = base_url or LLM_BASE_URL

    client = OpenAI(api_key=effective_api_key, base_url=effective_base_url, **kwargs)

    # Only patch DeepSeek endpoints to avoid interfering with OpenAI Vision etc.
    if "deepseek" in effective_base_url:
        _patch_chat_completions(client, enable_thinking, reasoning_effort)

    return client


def _patch_chat_completions(
    client: OpenAI,
    enable_thinking: bool,
    reasoning_effort: str,
) -> None:
    """Monkey-patch client.chat.completions.create with thinking mode control."""
    original_create = client.chat.completions.create

    def patched_create(**call_kwargs):
        call_kwargs.setdefault("extra_body", {})
        if enable_thinking:
            call_kwargs["extra_body"].setdefault("thinking", {"type": "enabled"})
            call_kwargs.setdefault("reasoning_effort", reasoning_effort)
        else:
            call_kwargs["extra_body"].setdefault("thinking", {"type": "disabled"})
        return original_create(**call_kwargs)

    client.chat.completions.create = patched_create
    mode = "enabled" if enable_thinking else "disabled"
    logger.debug(
        "DeepSeek thinking=%s (effort=%s) for client using %s",
        mode,
        reasoning_effort if enable_thinking else "n/a",
        client.base_url,
    )
