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

from config.llm_client import create_llm_client

from config.settings import (
    LLM_MODEL,
    LLM_MODEL_SCRIPT,
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
        self.client = create_llm_client(
            enable_thinking=True,
            timeout=120.0,   # 2 min for LLM calls
            max_retries=2,
        )
        self._llm_retries = 3        # max retries for empty/broken JSON responses
        self._llm_retry_delay = 2.0  # initial backoff seconds (doubles each retry)

        # P2/P3: multi-chunk, theme context, word count emphasis
        self._theme_context = None
        self._word_count_emphasis = 1.0
        self._chunk_context = None

        # Dynamic prompt import: canal2 → prompts.canal2_prompts, etc.
        try:
            prompts_module = importlib.import_module(f"prompts.{self.canal}_prompts")
            self._build_system_prompt = prompts_module.build_system_prompt
            self._format_user_prompt = prompts_module.format_user_prompt
        except ImportError:
            logger.warning(
                "No prompts module for %s, falling back to canal2_prompts",
                self.canal,
            )
            from prompts.canal2_prompts import build_system_prompt, format_user_prompt
            self._build_system_prompt = build_system_prompt
            self._format_user_prompt = format_user_prompt

        logger.info(
            "ScriptGenerator initialized: provider=%s model=%s canal=%s",
            LLM_PROVIDER,
            LLM_MODEL,
            self.canal,
        )

    def _llm_json_call(self, **call_kwargs):
        """Call LLM chat.completions.create and parse JSON with retry.

        Handles empty/invalid JSON responses from the API by retrying up
        to ``self._llm_retries`` times with exponential backoff.  Returns
        the parsed dict, or raises the last exception on total failure.
        """
        last_exc = None
        for attempt in range(self._llm_retries):
            try:
                response = self.client.chat.completions.create(**call_kwargs)
                content = response.choices[0].message.content
                if content is None or not content.strip():
                    raise ValueError(
                        "LLM returned empty content (attempt %d/%d)" % (
                            attempt + 1, self._llm_retries,
                        )
                    )
                return json.loads(content.strip())
            except json.JSONDecodeError as exc:
                last_exc = exc
                if attempt < self._llm_retries - 1:
                    delay = self._llm_retry_delay * (2 ** attempt)
                    logger.warning(
                        "LLM JSON parse failed (attempt %d/%d): %s — retrying in %.1fs",
                        attempt + 1, self._llm_retries, exc, delay,
                    )
                    time.sleep(delay)
            except ValueError as exc:
                last_exc = exc
                if attempt < self._llm_retries - 1:
                    delay = self._llm_retry_delay * (2 ** attempt)
                    logger.warning(
                        "%s — retrying in %.1fs", exc, delay,
                    )
                    time.sleep(delay)
            except Exception as exc:
                last_exc = exc
                if attempt < self._llm_retries - 1:
                    delay = self._llm_retry_delay * (2 ** attempt)
                    logger.warning(
                        "LLM call failed (attempt %d/%d): %s — retrying in %.1fs",
                        attempt + 1, self._llm_retries, exc, delay,
                    )
                    time.sleep(delay)
        raise last_exc

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
        self, content_item: dict, word_target: dict,
    ) -> Optional[dict]:
        """Generate a structured outline BEFORE writing blocks.

        One LLM call produces 4-6 chapters with titles, central ideas,
        concrete facts, visual keywords, and emotional arcs. This outline
        is then injected into every batch of block generation to maintain
        narrative coherence and factual substance.

        Returns:
            Dict with ``chapters`` list and ``summary``, or None on failure.
        """
        content_text = content_item.get("text", "")[:4000]
        content_title = content_item.get("title", "")
        duration_min = word_target.get("duration_target", 15)
        palabras_objetivo = word_target.get("palabras_objetivo", 2500)

        try:
            prompts_module = importlib.import_module(
                f"prompts.{self.canal}_prompts"
            )
            system_prompt = prompts_module.build_outline_prompt(
                config=self.canal_config,
                duration_min=duration_min,
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
            data = self._llm_json_call(
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

        # Use the lightweight prompt
        try:
            prompts_module = importlib.import_module(f"prompts.{self.canal}_prompts")
            system_prompt = prompts_module.build_content_only_prompt(
                config=self.canal_config,
                previous_blocks=previous_blocks,
                word_guidance=word_guidance,
                source_text=source_text,
                outline=outline,
                batch_num=batch_num,
            )
        except (ImportError, AttributeError):
            # Fallback: build minimal prompt inline
            system_prompt = self._build_minimal_prompt(previous_blocks, word_guidance, source_text)

        user_prompt = f"Fuente: {content_title}\n\nContinúa la narración documental."

        try:
            data = self._llm_json_call(
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

        data = {
            "titulo_options": doc_meta.get("titulo_options", [content_item.get("title", "Sin título")]),
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

        system_prompt = (
            "Eres un asistente editorial. Tu tarea es enriquecer bloques narrativos "
            "de un guion documental en español latinoamericano.\n\n"
            "REGLAS ESTRICTAS:\n"
            "1. NO cambies, resumas ni acortes el texto original de los bloques.\n"
            "2. Solo añades los campos de metadatos indicados.\n"
            "3. Mantén el número EXACTO de bloques del lote.\n"
            "4. Responde ÚNICAMENTE con JSON.\n\n"
            f"{arc_hint}\n"
            "CAMPOS POR BLOQUE:\n"
            "- tipo: (hook|desarrollo|climax|reflexion|cierre)\n"
            "- emocion: sentimiento predominante en español (misterio, asombro, tensión, reflexión...)\n"
            "- search_query_en: frase de búsqueda en INGLÉS para encontrar "
            "video/imagen en bancos de stock (Pexels, Pixabay, Unsplash).\n"
            "  REGLAS OBLIGATORIAS:\n"
            "  * FUSIÓN NARRATIVA + TEMÁTICA (DOS PARTES, AMBAS OBLIGATORIAS):\n"
            "    (1) SUJETO NARRATIVO (VA PRIMERO): 2-4 keywords que describen\n"
            "        EXACTAMENTE lo que se narra en este bloque — persona, acción,\n"
            "        lugar, objeto mencionado en la narración.\n"
            "    (2) AMBIENTACIÓN TEMÁTICA (VA DESPUÉS): 1-2 keywords de\n"
            "        época/estilo que anclan la escena en el mundo del video.\n"
            "  * LO QUE VES = LO QUE OYES: si el bloque narra 'el médico examinó\n"
            "    al paciente con instrumentos rudimentarios', la query debe ser\n"
            "    sobre 'physician examining patient medieval instruments',\n"
            "    NO sobre 'medieval medicine history'.\n"
            "  * PROGRESIÓN VISUAL: como procesas TODOS los bloques del lote a la\n"
            "    vez, asegura que las escenas consecutivas compartan al menos UN\n"
            "    elemento visual (misma luz, material, tipo de locación) para\n"
            "    crear un HILO VISUAL que una el video. NO uses la misma keyword\n"
            "    de anclaje temático en dos bloques seguidos.\n"
            "  * Usa términos visuales concretos: 'aerial shot', 'wide angle', "
            "'close up detail', 'drone footage', 'golden hour'\n"
            "  * Equilibra especificidad con disponibilidad en stock: "
            "'18th century French revolution' (OK) vs 'Robespierre guillotining "
            "Danton 5 April 1794' (DEMASIADO específico, no existe en stock)\n"
            "  * Traduce conceptos abstractos a escenas visuales "
            "(ej: 'creencia en la muerte' → 'ancient tomb burial chamber dark')\n"
            "  * BIEN: 'physician examining patient medieval instruments torchlight'\n"
            "  * BIEN: 'ancient Egyptian gold mask museum exhibit close up'\n"
            "  * MAL: 'Funeral Mask Egypt Art Institute Chicago' (nombre de museo)\n"
            "  * MAL: 'medieval history atmosphere dramatic lighting' (sin keywords "
            "de la narración, solo términos genéricos de época)\n"
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
            data = self._llm_json_call(
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
            data = self._llm_json_call(
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

    def generate_v2(self, content_item: dict, palabras_objetivo: int = None) -> Optional[dict]:
        """Generate a script using sequential block-by-block generation.

        Content from the database is used as thematic inspiration only —
        the LLM is free to expand any topic to the requested word count.

        Args:
            content_item: Raw content dict (title, text, source, etc.)
            palabras_objetivo: Exact word target (computed from duration × voice speed).
                               If None, derived from channel average duration.

        Returns:
            Enriched script dict or None on failure.
        """
        content_text = content_item.get("text", "")
        content_id = content_item.get("id")

        if not content_text:
            logger.warning("Empty content text for item %s", content_id)
            return None

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
        max_empty_strikes = 10          # ↑ from 3 — don't give up easily
        max_batches = 50                # ↑ from 30
        source_text = content_text[:3000]

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
        logger.info(
            "generate_v2: content done — %d blocks, %d words",
            len(all_bloques), total_words,
        )

        # Step 3: Enrich blocks with structural fields
        if hasattr(self, '_progress_cb') and self._progress_cb:
            try:
                self._progress_cb(24, "script", "Enriqueciendo guion con metadatos (SEO, emociones, media)...")
            except Exception:
                pass

        enriched = self._enrich_blocks(all_bloques, content_item, word_target)

        # Step 3.5: Narrative quality check (anti-repetition + coherence + hook)
        if enriched and enriched.get("bloques"):
            try:
                check = self._check_narrative_quality(enriched)
                if not check.get("passes", True):
                    logger.warning(
                        "Narrative quality check found issues: %s (score: rep=%.2f coh=%.2f hook=%.2f)",
                        check.get("issues", []),
                        check.get("repetition_score", 0),
                        check.get("coherence_score", 0),
                        check.get("hook_score", 0),
                    )
                    # Attempt regeneration
                    regenerated = self._regenerate_problematic_paragraphs(
                        enriched, check, content_item
                    )
                    if regenerated:
                        # Re-check regenerated script (single retry)
                        check2 = self._check_narrative_quality(regenerated)
                        if check2.get("passes", True):
                            logger.info(
                                "Narrative quality check PASSED after regeneration "
                                "(was: rep=%.2f coh=%.2f hook=%.2f → rep=%.2f coh=%.2f hook=%.2f)",
                                check.get("repetition_score", 0),
                                check.get("coherence_score", 0),
                                check.get("hook_score", 0),
                                check2.get("repetition_score", 0),
                                check2.get("coherence_score", 0),
                                check2.get("hook_score", 0),
                            )
                        enriched = regenerated
                    else:
                        logger.warning(
                            "Regeneration failed or returned no changes — "
                            "proceeding with original script"
                        )
                else:
                    logger.info(
                        "Narrative quality check PASSED (rep=%.2f coh=%.2f hook=%.2f)",
                        check.get("repetition_score", 0),
                        check.get("coherence_score", 0),
                        check.get("hook_score", 0),
                    )
            except Exception as exc:
                logger.warning(
                    "Narrative quality check failed with exception: %s — "
                    "proceeding with original script",
                    exc,
                )

        # Step 3.6: Extract onscreen text tags from blocks
        if enriched and enriched.get("bloques"):
            enriched = self._extract_onscreen_text(enriched)

        # Step 4: Save to DB
        if hasattr(self, '_progress_cb') and self._progress_cb:
            try:
                self._progress_cb(25, "script", f"Guion completo: {total_words} palabras")
            except Exception:
                pass

        full_guion = enriched.get("guion", "")
        result = self._save_and_return(
            content_id=content_item.get("id"),
            data=enriched,
            total_tokens=0,  # not tracked per-batch in v2
            cost=0.0,
            elapsed_ms=0,
            word_target=word_target,
        )

        logger.info(
            "generate_v2: saved script id=%s, %d words, %d blocks",
            result.get("id") if result else "FAILED",
            total_words,
            len(all_bloques),
        )
        return result

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
            outline_data = self._llm_json_call(
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

            # Generate chapter with retry (via _llm_json_call)
            ch_data = None
            try:
                ch_data = self._llm_json_call(
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
                expanded = self._llm_json_call(
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
        """
        issues = []
        if not all_bloques:
            return {"issues": issues}

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

        return {"issues": issues}

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
            data = self._llm_json_call(
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

        Routes to the new sequential block-by-block generator (v2) for
        production mode, keeping the old multi-chunk/single-chunk approach
        as fallback for test mode.

        Args:
            content_item: Dict from raw_content table.

        Returns:
            Dict with script fields or None if generation fails.
        """
        cfg = self.canal_config

        # Always use v2 sequential block-by-block generation (including outline-first).
        # Test mode now uses v2 too — the word targets are already reduced via
        # TEST_SCRIPT_WORDS_MIN/MAX and _compute_word_target().
        palabras_obj = content_item.get("_palabras_objetivo", None)
        return self.generate_v2(content_item, palabras_objetivo=palabras_obj)

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
