"""Tests for script generation fault-tolerance features.

Covers: content-structure validation, emergency fallback structure,
and _classify_error for the retry wrapper.
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
        # _validate_content_structure is an instance method but
        # doesn't use self — call it directly as a static test helper.
        gen = ScriptGenerator.__new__(ScriptGenerator)
        return gen._validate_content_structure(script)

    def test_hook_missing_number_fails(self):
        script = _make_script([
            _make_block("En un lugar lejano ocurrió algo increíble.", "hook"),
            *[_make_block(f"Párrafo narrativo {i}.") for i in range(5)],
            _make_block(
                "En el próximo video exploramos otro caso. "
                "Activa la campana para no perdértelo.",
                "cierre",
            ),
        ])
        valid, issues = self._validate(script)
        assert not valid
        assert any("numeric" in i.lower() for i in issues)

    def test_hook_with_number_passes(self):
        script = _make_script([
            _make_block("En 1987, 347 personas desaparecieron.", "hook"),
            *[_make_block(f"Párrafo narrativo {i}.") for i in range(5)],
            _make_block(
                "En el próximo video exploramos otro caso. "
                "Activa la campana.",
                "cierre",
            ),
        ])
        valid, issues = self._validate(script)
        assert valid, f"Expected valid but got: {issues}"

    def test_insufficient_narrative_blocks_fails(self):
        script = _make_script([
            _make_block("En 1987 ocurrió algo increíble.", "hook"),
            _make_block("Solo dos bloques narrativos."),
            _make_block("En el próximo video veremos más.", "cierre"),
        ])
        valid, issues = self._validate(script)
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
                "Activa la campana y mira el próximo video.",
                "cierre",
            ),
        )
        script = _make_script(blocks)
        valid, _ = self._validate(script)
        assert valid

    def test_missing_end_hook_fails(self):
        script = _make_script([
            _make_block("En 1987 ocurrió algo increíble.", "hook"),
            *[_make_block(f"Párrafo narrativo {i}.") for i in range(5)],
            _make_block("Y así termina esta historia.", "cierre"),
        ])
        valid, issues = self._validate(script)
        assert not valid
        assert any("end hook" in i.lower() for i in issues)

    def test_end_hook_in_cta_passes(self):
        script = _make_script(
            [
                _make_block("En 1987 ocurrió algo increíble.", "hook"),
                *[_make_block(f"Párrafo narrativo {i}.")
                  for i in range(5)],
                _make_block("Y así termina esta historia.", "cierre"),
            ],
            cta_text="Activa la campana para el próximo video.",
        )
        valid, _ = self._validate(script)
        assert valid

    def test_próximo_video_pattern_matches(self):
        script = _make_script([
            _make_block("En 1987, 347 personas desaparecieron.", "hook"),
            *[_make_block(f"Narrativa {i}.") for i in range(5)],
            _make_block("En el PRÓXIMO VIDEO: más revelaciones.", "cierre"),
        ])
        valid, _ = self._validate(script)
        assert valid

    def test_activa_campana_pattern_matches(self):
        script = _make_script([
            _make_block("En 1987, 347 personas desaparecieron.", "hook"),
            *[_make_block(f"Narrativa {i}.") for i in range(5)],
            _make_block(
                "Gracias por ver. ACTIVA LA CAMPANA y suscríbete.",
                "cierre",
            ),
        ])
        valid, _ = self._validate(script)
        assert valid

    def test_empty_script_fails(self):
        valid, issues = self._validate({})
        assert not valid
        assert any("no blocks" in i.lower() for i in issues)

    def test_decimal_number_in_hook_passes(self):
        script = _make_script([
            _make_block("El 3.7% de los casos documentados...", "hook"),
            *[_make_block(f"Narrativa {i}.") for i in range(5)],
            _make_block("En el próximo video: más datos.", "cierre"),
        ])
        valid, _ = self._validate(script)
        assert valid


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
