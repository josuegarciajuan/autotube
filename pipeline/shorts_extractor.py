"""Shorts clip extractor: uses LLM to find high-impact moments in video scripts.

Analyzes the full script with word-level timestamps and identifies 3-5 hook
moments suitable for short-form vertical content.
"""

import json
import logging
import re
from typing import Optional

from config.settings import LLM_PROVIDER, LLM_MODEL, LLM_API_KEY, LLM_BASE_URL

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """Analiza este guion de video documental y encuentra de {min_clips} a {max_clips} momentos 
de ALTO IMPACTO para extraer como YouTube Shorts (30-90 segundos cada uno).

Busca específicamente:
1. CLIFFHANGERS — momentos donde se plantea un misterio o pregunta sin resolver
2. REVELACIONES — datos sorprendentes, giros inesperados, hechos impactantes  
3. PICOS EMOCIONALES — momentos de máxima tensión, asombro o emoción
4. FRASES MEMORABLES — citas o afirmaciones que se quedarían en la mente del espectador

Para cada momento, devuelve:
- start_time: segundo de inicio aproximado (número)
- end_time: segundo de fin aproximado (número)  
- hook_title: título corto y viral para el Short (máximo 60 caracteres)
- hook_text: frase de enganche para quemar en pantalla (máximo 100 caracteres)
- ranking: 1-5 donde 1 = el más impactante/viralizable

REGLAS:
- Los clips deben durar entre 8 y 90 segundos (obligatorio, respétalo estrictamente)
- Prioriza momentos que funcionan sin contexto previo (autocontenidos)
- El hook_title debe generar curiosidad ("¿Sabías que...", "El misterio de...", "Nadie te contó que...")
- El hook_text debe ser la frase más potente del clip (30-60 palabras)
- Basa los timecodes en los timestamps proporcionados, no los inventes
- Escoge segmentos de al menos 8 segundos y máximo 90 segundos
- Devuelve EXACTAMENTE el siguiente JSON y nada más:

```json
{{
  "clips": [
    {{
      "start_time": 45.0,
      "end_time": 105.0,
      "hook_title": "¿Sabías que esta civilización desapareció sin dejar rastro?",
      "hook_text": "De un día para otro, una ciudad entera de 40,000 personas simplemente dejó de existir.",
      "ranking": 1
    }}
  ]
}}
```

IMPORTANTE: Los timecodes deben ser precisos, basados en la estructura del guion y los timestamps proporcionados.
No inventes timecodes — usa los datos reales del guion.

GUION:
---
{script_text}
---

TIMESTAMPS (word-level):
---
{timestamps_text}
---"""


class ShortsExtractor:
    """Analyzes video scripts with LLM to extract high-impact short clips."""

    def __init__(self):
        self._client = None

    def _get_client(self):
        """Lazy-init OpenAI-compatible client."""
        if self._client is not None:
            return self._client

        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "openai package required for shorts extraction. "
                "Install with: pip install openai"
            )

        self._client = OpenAI(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
        )
        return self._client

    def extract(
        self,
        script_text: str,
        timestamps: list[dict],
        max_clips: int = 5,
        min_clips: int = 3,
    ) -> list[dict]:
        """Extract high-impact clip specifications from a video script.

        Args:
            script_text: Full script text with block markers.
            timestamps: List of word-level timestamps [{word, start, end}, ...].
            max_clips: Maximum number of clips to extract.
            min_clips: Minimum number of clips to extract.

        Returns:
            List of clip specs: [{start_time, end_time, hook_title, hook_text, ranking}, ...]
        """
        # Convert timestamps to a condensed text format for the LLM
        ts_text = self._format_timestamps(timestamps)

        prompt = EXTRACTION_PROMPT.format(
            min_clips=min_clips,
            max_clips=max_clips,
            script_text=script_text[:12000],  # Truncate for token limits
            timestamps_text=ts_text[:8000],
        )

        client = self._get_client()

        try:
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "Eres un editor de video experto en YouTube Shorts virales. "
                                   "Identificas los momentos más impactantes de videos largos "
                                   "para crear clips cortos que generen millones de vistas.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                max_tokens=2000,
            )

            content = response.choices[0].message.content

            # Extract JSON from response (may be wrapped in ```json blocks)
            json_str = self._extract_json(content)
            data = json.loads(json_str)
            clips = data.get("clips", [])

            # Validate and sort by ranking
            valid_clips = []
            for clip in clips:
                if not all(k in clip for k in ("start_time", "end_time", "hook_title", "hook_text")):
                    continue
                # Ensure reasonable duration
                duration = clip["end_time"] - clip["start_time"]
                if duration < 8 or duration > 180:
                    logger.debug("Clip rejected: duration %.1fs outside 8-180 range", duration)
                    continue
                clip["ranking"] = clip.get("ranking", 5)
                valid_clips.append(clip)

            valid_clips.sort(key=lambda c: c["ranking"])
            logger.info(
                "Extracted %d valid clips from %d LLM suggestions",
                len(valid_clips), len(clips),
            )

            return valid_clips[:max_clips]

        except json.JSONDecodeError as e:
            logger.error("Failed to parse LLM clip extraction response: %s", e)
            logger.debug("Raw response: %s", content[:500] if 'content' in dir() else "N/A")
            return []
        except Exception as e:
            logger.error("Clip extraction failed: %s", e)
            return []

    def _format_timestamps(self, timestamps: list[dict]) -> str:
        """Format word timestamps into a readable text for the LLM."""
        if not timestamps:
            return "(no timestamps available)"

        # Group by every 10 words to save tokens
        lines = []
        current_second = 0
        current_text = []

        for ts in timestamps:
            word = ts.get("word", "")
            start = ts.get("start", 0)

            if start >= current_second + 5 and current_text:
                lines.append(f"[{current_second:.0f}s] {' '.join(current_text)}")
                current_text = []
                current_second = start

            current_text.append(word)

        if current_text:
            lines.append(f"[{current_second:.0f}s] {' '.join(current_text)}")

        return "\n".join(lines)

    def _extract_json(self, text: str) -> str:
        """Extract JSON from LLM response that may be wrapped in markdown blocks."""
        # Try to find JSON between ```json ... ``` blocks
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            return match.group(1)

        # Try to find JSON between { and }
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return match.group(0)

        return text
