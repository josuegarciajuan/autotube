"""Multi-model pool with automatic failover for script generation.

Parses LLM_POOL_MODELS from .env and provides model iteration with
per-model retries. When one model exhausts all retries, the next
model in the pool is tried automatically.

Pool format (in .env):
    LLM_POOL_MODELS = "deepseek:deepseek-v4-pro,openai:gpt-4o-mini"

Each entry is ``provider:model_id``. The provider determines which
API key and base URL to use (from existing settings).

Usage:
    from config.model_pool import ModelPool
    pool = ModelPool.from_env()
    for entry, client in pool.iter_models():
        response = client.chat.completions.create(...)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterator

from openai import OpenAI

from config.settings import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL_SCRIPT,
    LLM_POOL_MODELS,
    LLM_POOL_RETRIES_PER_MODEL,
    OPENAI_API_KEY,
)

logger = logging.getLogger(__name__)


@dataclass
class ModelEntry:
    """A single model in the pool with its provider metadata."""

    provider: str          # "deepseek" or "openai"
    model_id: str          # e.g. "deepseek-v4-pro", "gpt-4o-mini"
    api_key: str
    base_url: str
    enable_thinking: bool = False
    reasoning_effort: str = "high"
    timeout: float = 120.0
    max_retries: int = 2

    @property
    def display_name(self) -> str:
        return f"{self.provider}:{self.model_id}"


@dataclass
class ModelPool:
    """Ordered pool of LLM models with client creation and failover iteration."""

    entries: list[ModelEntry] = field(default_factory=list)
    retries_per_model: int = 3

    @classmethod
    def from_env(cls) -> ModelPool:
        """Build a ModelPool from environment variables.

        Reads LLM_POOL_MODELS (comma-separated ``provider:model_id`` entries),
        assigns API keys / base URLs by provider, and configures thinking
        mode per model.

        Falls back to a single-model pool using LLM_MODEL_SCRIPT if
        LLM_POOL_MODELS is not set.
        """
        raw = LLM_POOL_MODELS.strip()
        if not raw:
            logger.info(
                "LLM_POOL_MODELS not set — falling back to single-model pool: %s",
                LLM_MODEL_SCRIPT,
            )
            return cls._single_model_pool()

        retries = max(1, min(10, int(LLM_POOL_RETRIES_PER_MODEL)))
        entries = []

        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue

            parts = item.split(":", 1)
            if len(parts) != 2:
                logger.warning("Invalid pool entry '%s' — expected provider:model_id, skipping", item)
                continue

            provider, model_id = parts[0].strip().lower(), parts[1].strip()
            if not model_id:
                logger.warning("Empty model_id in pool entry '%s' — skipping", item)
                continue

            entry = cls._build_entry(provider, model_id)
            if entry:
                entries.append(entry)
                logger.info(
                    "ModelPool: registered %s (thinking=%s, timeout=%.0fs)",
                    entry.display_name,
                    entry.enable_thinking,
                    entry.timeout,
                )

        if not entries:
            logger.warning("No valid pool entries parsed — falling back to single-model pool")
            return cls._single_model_pool()

        return cls(entries=entries, retries_per_model=retries)

    @classmethod
    def _single_model_pool(cls) -> ModelPool:
        """Create a pool with just the default script model (LLM_MODEL_SCRIPT)."""
        provider = "deepseek" if "deepseek" in LLM_BASE_URL else "openai"
        entry = cls._build_entry(provider, LLM_MODEL_SCRIPT)
        if entry:
            return cls(entries=[entry], retries_per_model=3)
        return cls(entries=[], retries_per_model=3)

    @staticmethod
    def _build_entry(provider: str, model_id: str) -> ModelEntry | None:
        """Build a ModelEntry with the correct API key and base URL per provider."""
        if provider == "deepseek":
            api_key = LLM_API_KEY
            base_url = LLM_BASE_URL if "deepseek" in LLM_BASE_URL else "https://api.deepseek.com/v1"
            # Only v4 reasoning models need thinking mode; standard chat models don't
            enable_thinking = "v4" in model_id.lower()
            timeout = 120.0
        elif provider == "openai":
            api_key = OPENAI_API_KEY
            base_url = "https://api.openai.com/v1"
            enable_thinking = False
            timeout = 90.0
        else:
            logger.warning("Unknown provider '%s' — skipping", provider)
            return None

        if not api_key:
            logger.warning("No API key available for provider '%s' — skipping", provider)
            return None

        return ModelEntry(
            provider=provider,
            model_id=model_id,
            api_key=api_key,
            base_url=base_url,
            enable_thinking=enable_thinking,
            timeout=timeout,
        )

    def create_client(self, entry: ModelEntry) -> OpenAI:
        """Create an OpenAI-compatible client for a model entry.

        For DeepSeek providers, the client is monkey-patched to auto-inject
        thinking mode control into extra_body.
        """
        client = OpenAI(
            api_key=entry.api_key,
            base_url=entry.base_url,
            timeout=entry.timeout,
            max_retries=entry.max_retries,
        )

        if entry.provider == "deepseek":
            _patch_thinking(client, entry.enable_thinking, entry.reasoning_effort)

        return client

    def iter_models(self) -> Iterator[tuple[ModelEntry, OpenAI]]:
        """Iterate over pool entries, yielding (ModelEntry, OpenAI client) pairs.

        Each iteration step represents a model failover boundary. The caller
        should attempt up to ``retries_per_model`` calls on each client before
        advancing to the next model.
        """
        for entry in self.entries:
            client = self.create_client(entry)
            yield entry, client

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return len(self.entries) > 0


def _patch_thinking(
    client: OpenAI,
    enable_thinking: bool,
    reasoning_effort: str,
) -> None:
    """Monkey-patch client.chat.completions.create with thinking mode control.

    Only applied to DeepSeek clients. Inject extra_body with thinking=enabled
    or thinking=disabled depending on the enable_thinking flag.
    """
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
