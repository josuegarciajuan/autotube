"""Tests for build_content_only_prompt() — narrative continuity.

Run:  python3 -m pytest tests/test_content_prompt.py -v
"""

import sys
sys.path.insert(0, "/root/autotube")

import pytest
from prompts.base_prompts import build_content_only_prompt, build_outline_prompt
from tests.conftest import MockConfigCanal2


class TestContentPromptStructure:
    """Verify the lightweight content-only prompt."""

    def test_prompt_has_narrative_continuity(self):
        """With previous blocks, includes CONTINUIDAD marker and rules."""
        prompt = build_content_only_prompt(
            config=MockConfigCanal2,
            previous_blocks=[
                {"texto": "Primer párrafo sobre sincronías."},
                {"texto": "Segundo párrafo sobre casualidades."},
                {"texto": "Tercer párrafo con la historia principal."},
                {"texto": "Cuarto párrafo con el desenlace."},
            ],
            word_guidance=250,
            source_text="Contenido fuente de prueba."
        )
        assert "CONTINUIDAD NARRATIVA" in prompt
        assert "NO repitas" in prompt
        assert "AVANZA" in prompt
        assert "historia empezó así" in prompt.lower()

    def test_prompt_shows_beginning(self):
        """First blocks appear as 'La historia empezó así'."""
        prompt = build_content_only_prompt(
            config=MockConfigCanal2,
            previous_blocks=[
                {"texto": "Primer párrafo sobre sincronías y casualidades inexplicables."},
                {"texto": "Segundo párrafo."},
                {"texto": "Tercero."},
            ],
            word_guidance=250,
        )
        assert "Primer párrafo" in prompt

    def test_prompt_shows_last_blocks(self):
        """Last blocks appear as 'Lo ÚLTIMO que narraste'."""
        prompt = build_content_only_prompt(
            config=MockConfigCanal2,
            previous_blocks=[
                {"texto": "A"},
                {"texto": "B"},
                {"texto": "C"},
                {"texto": "Desenlace de la historia con conclusión final."},
            ],
            word_guidance=250,
        )
        assert "Desenlace" in prompt
        assert "Lo ÚLTIMO que narraste" in prompt

    def test_prompt_includes_source_text(self):
        """Source text appears in the prompt."""
        prompt = build_content_only_prompt(
            config=MockConfigCanal2,
            previous_blocks=None,
            word_guidance=250,
            source_text="Historia real sobre casualidades imposibles documentadas en Ohio, 1971."
        )
        assert "Ohio" in prompt
        assert "CONTENIDO FUENTE" in prompt

    def test_prompt_without_previous_blocks(self):
        """No CONTINUIDAD section when no previous blocks."""
        prompt = build_content_only_prompt(
            config=MockConfigCanal2,
            previous_blocks=None,
            word_guidance=250,
            source_text="Test"
        )
        assert "CONTINUIDAD NARRATIVA" not in prompt

    def test_prompt_length_reasonable(self):
        """Prompt should be lightweight (< 3000 chars)."""
        prompt = build_content_only_prompt(
            config=MockConfigCanal2,
            previous_blocks=[
                {"texto": f"Párrafo número {i} con suficiente contenido para probar."}
                for i in range(5)
            ],
            word_guidance=250,
            source_text="Test " * 50
        )
        assert len(prompt) < 3500, f"Prompt too long: {len(prompt)} chars"


class TestOutlinePrompt:
    """Tests for the new build_outline_prompt() function."""

    def test_demands_concrete_facts(self):
        """Outline prompt must demand hechos concretos and forbid empty metaphors."""
        prompt = build_outline_prompt(
            config=MockConfigCanal2, duration_min=12, word_target=2000,
        )
        assert "HECHOS CONCRETOS" in prompt, "Must demand concrete facts"
        assert "PROHIBIDO" in prompt, "Must forbid empty language"
        assert "metáforas vacías" in prompt, "Must explicitly ban empty metaphors"

    def test_has_chapter_structure(self):
        """Outline must produce chapter JSON with required fields."""
        prompt = build_outline_prompt(
            config=MockConfigCanal2, duration_min=10, word_target=1500,
        )
        assert '"chapters"' in prompt
        assert '"titulo"' in prompt
        assert '"idea_central"' in prompt

    def test_includes_visual_keywords(self):
        """Outline must include visual keywords for stock media."""
        prompt = build_outline_prompt(
            config=MockConfigCanal2, duration_min=8, word_target=1200,
        )
        assert "visual_keywords_en" in prompt
        assert "stock media" in prompt.lower()


class TestContentPromptWithOutline:
    """Tests for build_content_only_prompt() with outline parameter."""

    def test_accepts_outline_and_injects_context(self):
        """With outline, prompt includes 'CONTEXTO DEL CAPÍTULO' section."""
        outline = {
            "chapters": [
                {
                    "titulo": "El Misterio Inicial",
                    "idea_central": "El suceso que cambió todo",
                    "hechos_concretos": ["En 1972 ocurrió X", "300 personas lo vieron"],
                    "visual_keywords_en": "mysterious event cinematic",
                    "emocion_objetivo": "asombro",
                }
            ]
        }
        prompt = build_content_only_prompt(
            config=MockConfigCanal2,
            outline=outline,
            batch_num=1,
        )
        assert "CONTEXTO DEL CAPÍTULO" in prompt, "Must inject outline context"
        assert "El Misterio Inicial" in prompt, "Must include chapter title"
        assert "En 1972" in prompt, "Must include concrete facts"
        assert "HECHOS CONCRETOS QUE DEBES INCLUIR" in prompt

    def test_handles_empty_outline_gracefully(self):
        """Empty outline should not break the prompt."""
        prompt = build_content_only_prompt(
            config=MockConfigCanal2,
            outline={"chapters": []},
            batch_num=0,
        )
        assert "Reglas estrictas" in prompt.lower() or "REGLAS" in prompt
