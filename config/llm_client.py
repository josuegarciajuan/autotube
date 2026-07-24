"""Centralized LLM client factory with DeepSeek V4 thinking-mode management.

DeepSeek V4 models (deepseek-v4-flash, deepseek-v4-pro) have thinking mode
ENABLED by default since 2026-07-24, which:
- Silently ignores temperature/top_p/presence_penalty/frequency_penalty
- Adds reasoning_content output tokens (extra cost, not used by the pipeline)
- May change response behavior vs. the old deepseek-chat non-thinking default

This module provides `create_llm_client()` — a drop-in replacement for
`OpenAI()` that auto-disables thinking mode when connecting to DeepSeek APIs
by injecting `extra_body={"thinking": {"type": "disabled"}}` into every
`chat.completions.create()` call.

Usage:
    from config.llm_client import create_llm_client

    client = create_llm_client(timeout=120.0, max_retries=2)
    response = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[...],
        temperature=0.7,
    )
    # extra_body={"thinking": {"type": "disabled"}} is auto-injected

For non-DeepSeek base URLs (e.g., OpenAI vision), the client behaves as
a plain OpenAI instance with no modifications.

Ref: https://api-docs.deepseek.com/guides/thinking_mode
Ref: https://api-docs.deepseek.com/updates (deprecation 2026-07-24)
"""

import logging
from openai import OpenAI
from config.settings import LLM_API_KEY, LLM_BASE_URL

logger = logging.getLogger(__name__)


def create_llm_client(api_key=None, base_url=None, **kwargs):
    """Create an OpenAI-compatible client with thinking disabled for DeepSeek.

    Args:
        api_key: API key override (defaults to LLM_API_KEY from settings).
        base_url: Base URL override (defaults to LLM_BASE_URL from settings).
        **kwargs: Additional kwargs forwarded to OpenAI() (timeout, max_retries, etc.).

    Returns:
        OpenAI client instance. If base_url points to a DeepSeek API, the
        ``chat.completions.create`` method is patched to auto-inject
        ``extra_body={"thinking": {"type": "disabled"}}``.
    """
    effective_api_key = api_key or LLM_API_KEY
    effective_base_url = base_url or LLM_BASE_URL

    client = OpenAI(api_key=effective_api_key, base_url=effective_base_url, **kwargs)

    # Only patch DeepSeek endpoints to avoid interfering with OpenAI Vision etc.
    if "deepseek" in effective_base_url:
        _patch_chat_completions(client)

    return client


def _patch_chat_completions(client: OpenAI) -> None:
    """Monkey-patch client.chat.completions.create to disable thinking mode."""
    original_create = client.chat.completions.create

    def patched_create(**call_kwargs):
        call_kwargs.setdefault("extra_body", {})
        # Only add if not already explicitly set by the caller
        call_kwargs["extra_body"].setdefault("thinking", {"type": "disabled"})
        return original_create(**call_kwargs)

    client.chat.completions.create = patched_create
    logger.debug(
        "DeepSeek thinking-mode auto-disabled for client using %s",
        client.base_url,
    )
