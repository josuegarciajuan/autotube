"""Tests for script generation fault-tolerance features.

Covers: content-structure validation, natural endings, emergency fallback
structure, and _classify_error for the retry wrapper.
"""

import pytest
import json

from pipeline.script_generator import (
    ScriptGenerator,
    MIN_NARRATIVE_BLOCKS,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _make_block(texto: str, tipo: str = "desarrollo") -> dict:
    return {"texto": texto, "tipo": tipo}


def _make_script(bloques: list[dict], cta_text: str = "") -> dict:
    script = {"bloques": bloques}
    if cta_text:
        script["cta"] = {"texto": cta_text, "tipo": "cta"}
    return script


# ── Content-structure validation ────────────────────────────────────

class TestValidateContentStructure:

    def _validate(self, script: dict) -> tuple:
        """Call the (instance) method via a neutral instance."""
        gen = ScriptGenerator.__new__(ScriptGenerator)
        gen.canal_config = type("Config", (), {"LANGUAGE": "es"})()
        return gen._validate_content_structure(script)

    def test_hook_missing_number_fails(self):
        script = _make_script([
            _make_block("En un lugar lejano ocurrió algo increíble.", "hook"),
            *[_make_block(f"Párrafo narrativo {i}.") for i in range(5)],
            _make_block(
                "La pregunta que deja este caso sigue abierta.",
                "cierre",
            ),
        ])
        valid, issues, warnings = self._validate(script)
        assert valid, issues
        assert any("numeric" in warning.lower() for warning in warnings)

    def test_hook_with_number_passes(self):
        script = _make_script([
            _make_block("En 1987, 347 personas desaparecieron.", "hook"),
            *[_make_block(f"Párrafo narrativo {i}.") for i in range(5)],
            _make_block(
                "Quizá la lección más importante sea aceptar lo que aún no sabemos.",
                "cierre",
            ),
        ])
        valid, issues, _ = self._validate(script)
        assert valid, f"Expected valid but got: {issues}"

    def test_insufficient_narrative_blocks_fails(self):
        script = _make_script([
            _make_block("En 1987 ocurrió algo increíble.", "hook"),
            _make_block("Solo dos bloques narrativos."),
            _make_block("El misterio sigue invitándonos a investigar.", "cierre"),
        ])
        valid, issues, _ = self._validate(script)
        assert not valid
        assert any("narrative blocks" in i.lower() for i in issues)

    def test_exactly_min_narrative_blocks_passes(self):
        blocks = [
            _make_block("En 1987, datos asombrosos.", "hook"),
        ]
        blocks += [
            _make_block(f"Narrativa {i}.") for i in range(MIN_NARRATIVE_BLOCKS)
        ]
        blocks.append(
            _make_block(
                "Esta historia nos recuerda los límites de lo que creemos saber.",
                "cierre",
            ),
        )
        script = _make_script(blocks)
        valid, _, _ = self._validate(script)
        assert valid

    def test_reflective_ending_passes_without_teaser(self):
        script = _make_script([
            _make_block("En 1987 ocurrió algo increíble.", "hook"),
            *[_make_block(f"Párrafo narrativo {i}.") for i in range(5)],
            _make_block("Y así termina esta historia.", "cierre"),
        ])
        valid, issues, _ = self._validate(script)
        assert valid, f"Expected reflective closure to pass: {issues}"

    def test_reflective_cta_passes(self):
        script = _make_script(
            [
                _make_block("En 1987 ocurrió algo increíble.", "hook"),
                *[_make_block(f"Párrafo narrativo {i}.")
                  for i in range(5)],
                _make_block("Y así termina esta historia.", "cierre"),
            ],
            cta_text="Gracias por acompañarnos en esta reflexión.",
        )
        valid, _, _ = self._validate(script)
        assert valid

    @pytest.mark.parametrize("ending", [
        "En el próximo video conoceremos otra historia.",
        "En el siguiente episodio veremos más detalles.",
        "No te pierdas la siguiente entrega.",
    ])
    def test_serialized_next_content_ending_is_rejected(self, ending):
        script = _make_script([
            _make_block("En 1987, 347 personas desaparecieron.", "hook"),
            *[_make_block(f"Narrativa {i}.") for i in range(5)],
            _make_block(ending, "cierre"),
        ])
        valid, issues, _ = self._validate(script)
        assert not valid
        assert any("serialized" in issue.lower() for issue in issues)

    def test_serialized_teaser_before_final_cta_is_rejected(self):
        script = _make_script([
            _make_block("En 1987, 347 personas desaparecieron.", "hook"),
            *[_make_block(f"Narrativa {i}.") for i in range(5)],
            _make_block("En la siguiente entrega conoceremos otro caso.", "cierre"),
            _make_block("Gracias por acompañarnos en esta reflexión.", "cierre"),
        ])
        valid, issues, _ = self._validate(script)
        assert not valid
        assert any("serialized" in issue.lower() for issue in issues)

    def test_subscribe_cta_without_next_content_passes(self):
        script = _make_script([
            _make_block("En 1987, 347 personas desaparecieron.", "hook"),
            *[_make_block(f"Narrativa {i}.") for i in range(5)],
            _make_block(
                "Gracias por ver. Suscríbete si quieres seguir acompañándonos.",
                "cierre",
            ),
        ])
        valid, _, _ = self._validate(script)
        assert valid

    def test_empty_script_fails(self):
        valid, issues, _ = self._validate({})
        assert not valid
        assert any("no blocks" in i.lower() for i in issues)

    def test_decimal_number_in_hook_passes(self):
        script = _make_script([
            _make_block("El 3.7% de los casos documentados...", "hook"),
            *[_make_block(f"Narrativa {i}.") for i in range(5)],
            _make_block("Los datos disponibles no resuelven todas las preguntas.", "cierre"),
        ])
        valid, _, _ = self._validate(script)
        assert valid


class TestEmergencyFallbackEndings:

    def test_raw_emergency_fallback_uses_reflective_closure(self):
        class Config:
            LANGUAGE = "es"
            CANAL_OUTRO_TAGLINE = "Gracias por acompañarnos en esta reflexión."

        gen = ScriptGenerator.__new__(ScriptGenerator)
        gen.canal_config = Config()
        gen._enrich_blocks = lambda *_args, **_kwargs: None
        source = " ".join([
            "La investigación documentó el caso con fechas, fuentes y testimonios verificables."
        ] * 40)

        script = gen._emergency_raw_chunk(
            {"title": "Caso documentado", "text": source}, target_words=500,
        )

        assert script is not None
        ending = " ".join(block["texto"] for block in script["bloques"][-2:]).lower()
        assert "próximo video" not in ending
        assert "proximo video" not in ending
        assert "siguiente episodio" not in ending
        assert "siguiente entrega" not in ending


# ── Error classification ────────────────────────────────────────────

class TestClassifyError:

    def test_json_decode(self):
        err = json.JSONDecodeError("msg", "doc", 0)
        assert ScriptGenerator._classify_error(err) == "json_parse"

    def test_empty_content(self):
        err = ValueError("LLM returned empty content (attempt 1/3)")
        assert ScriptGenerator._classify_error(err) == "empty_content"

    def test_timeout(self):
        for name in ("APITimeoutError", "Timeout", "ReadTimeout"):
            err = type(name, (Exception,), {})("timeout!")
            assert ScriptGenerator._classify_error(err) == "timeout"

    def test_rate_limit(self):
        for name in ("RateLimitError", "RateLimit"):
            err = type(name, (Exception,), {})("too many requests")
            assert ScriptGenerator._classify_error(err) == "rate_limit"

    def test_connection_error(self):
        for name in ("ConnectionError", "APIConnectionError"):
            err = type(name, (Exception,), {})("connection refused")
            assert ScriptGenerator._classify_error(err) == "connection_error"

    def test_validation_error(self):
        # "validation" substring matches before "schema", so "validation_failed"
        err = Exception("schema validation failed")
        assert ScriptGenerator._classify_error(err) == "validation_failed"

    def test_pure_schema_error(self):
        # Schema check only triggers on ValidationError type or "schema" in
        # msg without "validation"
        class ValidationError(Exception):
            pass
        err = ValidationError("bad schema")
        assert ScriptGenerator._classify_error(err) == "schema_error"

    def test_generic_exception(self):
        assert ScriptGenerator._classify_error(Exception("random")) == "exception"
