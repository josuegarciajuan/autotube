"""AI script generator for the Autotube pipeline.

Supports OpenAI (GPT-4o-mini) and DeepSeek (v3/v4) via OpenAI-compatible SDK.
Transforms scraped raw content into structured YouTube scripts
with scene markers, title options, and emotion annotations.
"""

import importlib
import json
import logging
import time
from typing import Optional

from openai import OpenAI

from config.settings import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
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
    "emociones",
    "keywords",
    "duracion_estimada",
    "descripcion_seo",
    "hashtags",
    "fuentes_citadas",
    "chapters",
}


class ScriptGenerator:
    """Generate YouTube narration scripts from raw content using AI (DeepSeek/OpenAI)."""

    def __init__(self, db: Database, canal_config):
        """Initialize the script generator.

        Args:
            db: Database instance for persistence.
            canal_config: Canal-specific config module (e.g. canal1_config).
        """
        self.db = db
        self.canal_config = canal_config
        self.canal = canal_config.CANAL_NAME
        self.client = OpenAI(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
        )

        # Dynamic prompt import: canal1 → prompts.canal1_prompts, etc.
        try:
            prompts_module = importlib.import_module(f"prompts.{self.canal}_prompts")
            self._build_system_prompt = prompts_module.build_system_prompt
            self._format_user_prompt = prompts_module.format_user_prompt
        except ImportError:
            logger.warning(
                "No prompts module for %s, falling back to canal1_prompts",
                self.canal,
            )
            from prompts.canal1_prompts import build_system_prompt, format_user_prompt
            self._build_system_prompt = build_system_prompt
            self._format_user_prompt = format_user_prompt

        logger.info(
            "ScriptGenerator initialized: provider=%s model=%s canal=%s",
            LLM_PROVIDER,
            LLM_MODEL,
            self.canal,
        )

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
        if not isinstance(data.get("descripcion_seo"), str) or len(data.get("descripcion_seo", "")) < 20:
            raise ValueError("descripcion_seo must be a string with at least 20 characters")
        if not isinstance(data.get("hashtags"), list) or len(data.get("hashtags", [])) < 1:
            raise ValueError("hashtags must be a non-empty list")
        if not isinstance(data.get("fuentes_citadas"), list) or len(data.get("fuentes_citadas", [])) < 1:
            raise ValueError("fuentes_citadas must be a non-empty list")
        if not isinstance(data.get("chapters"), list) or len(data.get("chapters", [])) < 1:
            raise ValueError("chapters must be a non-empty list")

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

    def generate(self, content_item: dict) -> Optional[dict]:
        """Generate a script from a single raw_content row.

        Args:
            content_item: Dict from raw_content table with keys:
                id, title, source, subreddit, score, text, etc.

        Returns:
            Dict with script fields (id, titulo_options, guion, escenas, etc.)
            or None if generation fails.
        """
        content_id = content_item.get("id")
        user_prompt = self._format_user_prompt(content_item)
        system_prompt = self._build_system_prompt(self.canal_config)
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
                model=LLM_MODEL,
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
            data.setdefault("bloques", [])
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

        try:
            script_id = self.db.insert_script(
                raw_content_id=content_id,
                canal=self.canal,
                titulo_options=data["titulo_options"],
                guion=data["guion"],
                escenas=data["escenas"],
                bloques=data.get("bloques"),
                emociones=data.get("emociones"),
                keywords=data.get("keywords"),
                duracion_estimada=data.get("duracion_estimada"),
                token_count=total_tokens,
                cost_estimate=cost,
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

        logger.info(
            "Script saved: id=%s content_id=%s titles=%s tokens=%d",
            script_id,
            content_id,
            len(data["titulo_options"]),
            total_tokens,
        )

        result = {
            "id": script_id,
            "raw_content_id": content_id,
            "canal": self.canal,
            "titulo_options": data["titulo_options"],
            "guion": data["guion"],
            "escenas": data["escenas"],
            "bloques": data.get("bloques", []),
            "bloques_json": json.dumps(data.get("bloques", [])),
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
        }
        return result

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
