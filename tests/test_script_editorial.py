"""Tests for Fase 2 editorial quality of scripts.

Covers the new read-only validator checks (hook promise, novelty, rhetorical
questions, claim honesty) — which are WARNINGS ONLY and must never mark a
script as grave — and the bounded editorial repair in ``generate_v2``
(``_maybe_editorial_repair``), which is a no-op unless the per-channel flag
``SCRIPT_EDITORIAL_REVIEW_ENABLED`` is True.
"""

from types import SimpleNamespace

import pytest

from pipeline.script_validator import ScriptValidator
from pipeline.script_generator import ScriptGenerator


# ── Helpers ──────────────────────────────────────────────────────────

def _block(texto: str, tipo: str = "desarrollo") -> dict:
    return {"texto": texto, "tipo": tipo}


def _script(bloques: list[dict]) -> dict:
    return {
        "guion": "\n\n".join(b["texto"] for b in bloques),
        "bloques": bloques,
        "titulo_options": ["Título de prueba"],
    }


def _good_script() -> dict:
    return _script([
        _block(
            "En 1987, el vuelo 123 de la Fuerza Aérea desapareció del radar "
            "a las 14:32. Los registros indican que la tripulación reportó una "
            "luz anómala antes del silencio, y la verdad sobre su destino sigue "
            "siendo un enigma documentado.",
            "hook",
        ),
        _block(
            "Los investigadores peinaron 400 kilómetros de cordillera con "
            "helicópteros y perros. Encontraron restos metálicos, pero ninguna "
            "señal humana apareció en la nieve durante veinte semanas.",
        ),
        _block(
            "Un técnico de la base de Mendoza revisó las cintas magnetofónicas "
            "del control aéreo. Escuchó treinta segundos de estática y luego una "
            "voz que repetía coordenadas en inglés.",
        ),
        _block(
            "Décadas después, un periodista chileno publicó documentos "
            "desclasificados. Los papeles mencionan un segundo avión, jamás "
            "listado en los registros oficiales del escuadrón.",
        ),
        _block(
            "La teoría más aceptada apunta a un fallo eléctrico, aunque los "
            "peritos no hallaron una causa concluyente. El expediente queda "
            "abierto y las familias todavía esperan respuestas.",
            "cierre",
        ),
    ])


def _generic_repetitive_script() -> dict:
    repeated = (
        "En 1999 ocurrió un suceso extraño que muchas personas recuerdan. "
        "El episodio fue investigado por un equipo local que recogió "
        "testimonios durante varias semanas sin llegar a una conclusión clara."
    )
    return _script([
        _block(
            "En este video vamos a hablar de un misterio fascinante que nadie "
            "entiende del todo y que dejó muchas dudas abiertas.",
            "hook",
        ),
        _block(repeated),
        _block(repeated),
        _block(repeated),
        _block(repeated, "cierre"),
    ])


# ── Validator: new editorial checks are warnings, never grave ────────

class TestEditorialValidatorChecks:

    def test_generic_repetitive_script_is_not_grave(self):
        result = ScriptValidator().validate(_generic_repetitive_script())
        assert result.passes is True
        assert result.details["severe_issues"] == set()
        # Hook promise + novelty + rhetorical/claim checks add warnings only.
        assert result.warnings, "expected editorial warnings"
        assert "hook_promise_score" in result.details
        assert result.details["hook_promise_score"] < 1.0
        assert result.details.get("novelty_score", 1.0) < 1.0

    def test_good_script_passes(self):
        result = ScriptValidator().validate(_good_script())
        assert result.passes is True
        assert result.details["severe_issues"] == set()

    def test_generic_hook_is_flagged_as_warning(self):
        result = ScriptValidator().validate(_generic_repetitive_script())
        assert any("Hook promise" in w for w in result.warnings)
        assert result.hook_score < 1.0  # existing banned-pattern check also fires

    def test_novelty_detects_repetitive_blocks(self):
        result = ScriptValidator().validate(_generic_repetitive_script())
        assert any("Novelty" in w for w in result.warnings)

    def test_novelty_not_flagged_for_diverse_blocks(self):
        result = ScriptValidator().validate(_good_script())
        assert not any("Novelty" in w for w in result.warnings)
        assert result.details["novelty_score"] == pytest.approx(1.0, abs=0.05) or \
            result.details["novelty_score"] > 0.5

    def test_rhetorical_questions_flagged(self):
        bloques = [
            _block("¿Qué ocurrió en 1999 con el radar?", "hook"),
            _block("¿Quién ordenó silenciar el informe?"),
            _block("¿Por qué borraron las cintas?"),
            _block("¿Cómo escapó el testigo?"),
            _block("Los documentos de 1999 siguen clasificados.", "cierre"),
        ]
        result = ScriptValidator().validate(_script(bloques))
        assert result.passes is True
        assert any("Rhetorical questions" in w for w in result.warnings)
        assert result.details["rhetorical_score"] < 1.0

    def test_claim_honesty_flagged(self):
        bloques = [
            _block("Siempre supieron la verdad del caso de 1999.", "hook"),
            _block("Nunca dudaron de la versión oficial de 1999."),
            _block("Sin duda los 12 testigos colaboraron."),
            _block("La evidencia es un hecho probado en 1999."),
            _block("Nadie cuestionó el informe final de 1999.", "cierre"),
        ]
        result = ScriptValidator().validate(_script(bloques))
        assert result.passes is True
        assert any("Claim honesty" in w for w in result.warnings)
        assert result.details["claim_honesty_score"] < 1.0

    def test_hedged_claims_not_flagged(self):
        bloques = [
            _block("Según los registros, en 1999 el radar detectó una señal.", "hook"),
            _block("Los 3 peritos sugieren que podría tratarse de un fallo."),
            _block("La teoría más aceptada indica un error de calibración del 2 %."),
            _block("Quizá la evidencia de 1999 sea incompleta, admiten las fuentes."),
            _block("El expediente de 1999 sigue abierto, sin conclusiones firmes.", "cierre"),
        ]
        result = ScriptValidator().validate(_script(bloques))
        assert result.passes is True
        assert not any("Claim honesty" in w for w in result.warnings)

    def test_new_scores_never_enter_severe_issues(self):
        for script in (_good_script(), _generic_repetitive_script()):
            result = ScriptValidator().validate(script)
            severe = result.details.get("severe_issues", set())
            assert not (severe & {
                "hook_promise", "novelty", "rhetorical", "claim_honesty",
            })


# ── Bounded editorial repair in generate_v2 ──────────────────────────

class _EditorialConfig:
    def __init__(self, enabled: bool, max_calls: int = 1):
        self.SCRIPT_EDITORIAL_REVIEW_ENABLED = enabled
        self.SCRIPT_EDITORIAL_REVIEW_MAX_CALLS = max_calls


class _StubValidator:
    def __init__(self, score: float, passes: bool):
        self._score = score
        self._passes = passes
        self.calls = 0

    def validate(self, *args, **kwargs):
        self.calls += 1
        return SimpleNamespace(score=self._score, passes=self._passes)


def _make_generator(enabled: bool, max_calls: int = 1,
                    check_result: dict | None = None,
                    regen=None, regen_raises: bool = False):
    gen = ScriptGenerator.__new__(ScriptGenerator)
    gen.canal_config = _EditorialConfig(enabled, max_calls)

    calls = {"check": 0, "regen": 0}

    def _check(_enriched):
        calls["check"] += 1
        return check_result if check_result is not None else {
            "problem_paragraphs": [0], "avoid_themes": ["tema"],
        }

    def _regenerate(_current, _check, _content):
        calls["regen"] += 1
        if regen_raises:
            raise RuntimeError("boom")
        if regen is not None:
            return regen
        return {"bloques": [{"texto": "reparado"}], "repaired": True}

    gen._check_narrative_quality = _check
    gen._regenerate_problematic_paragraphs = _regenerate
    return gen, calls


def test_repair_disabled_is_noop_and_fires_no_llm_call():
    original = {"bloques": [{"texto": "original"}]}
    gen, calls = _make_generator(enabled=False)
    validator = _StubValidator(score=0.9, passes=True)

    out = gen._maybe_editorial_repair(
        original, SimpleNamespace(score=0.4, passes=False), validator, None, {},
    )
    assert out is original
    assert calls["regen"] == 0
    assert calls["check"] == 0
    assert validator.calls == 0


def test_repair_enabled_accepts_improvement_with_one_call():
    original = {"bloques": [{"texto": "original"}]}
    repaired = {"bloques": [{"texto": "reparado"}]}
    gen, calls = _make_generator(enabled=True, max_calls=1, regen=repaired)
    validator = _StubValidator(score=0.95, passes=True)

    out = gen._maybe_editorial_repair(
        original, SimpleNamespace(score=0.5, passes=False), validator, None, {},
    )
    assert out is repaired
    assert calls["regen"] == 1
    assert validator.calls == 1


def test_repair_enabled_keeps_original_when_not_improved():
    original = {"bloques": [{"texto": "original"}]}
    repaired = {"bloques": [{"texto": "reparado"}]}
    gen, calls = _make_generator(enabled=True, max_calls=1, regen=repaired)
    # passes but lower score than baseline → keep original
    validator = _StubValidator(score=0.4, passes=True)

    out = gen._maybe_editorial_repair(
        original, SimpleNamespace(score=0.5, passes=False), validator, None, {},
    )
    assert out is original
    assert calls["regen"] == 1
    assert validator.calls == 1


def test_repair_enabled_keeps_original_when_second_validation_fails():
    original = {"bloques": [{"texto": "original"}]}
    repaired = {"bloques": [{"texto": "reparado"}]}
    gen, calls = _make_generator(enabled=True, max_calls=1, regen=repaired)
    validator = _StubValidator(score=0.99, passes=False)

    out = gen._maybe_editorial_repair(
        original, SimpleNamespace(score=0.5, passes=False), validator, None, {},
    )
    assert out is original
    assert calls["regen"] == 1


def test_repair_max_calls_zero_is_noop():
    original = {"bloques": [{"texto": "original"}]}
    gen, calls = _make_generator(enabled=True, max_calls=0)
    validator = _StubValidator(score=0.9, passes=True)

    out = gen._maybe_editorial_repair(
        original, SimpleNamespace(score=0.4, passes=False), validator, None, {},
    )
    assert out is original
    assert calls["regen"] == 0


def test_repair_is_fail_open_on_regeneration_error():
    original = {"bloques": [{"texto": "original"}]}
    gen, calls = _make_generator(enabled=True, max_calls=1, regen_raises=True)
    validator = _StubValidator(score=0.9, passes=True)

    out = gen._maybe_editorial_repair(
        original, SimpleNamespace(score=0.4, passes=False), validator, None, {},
    )
    assert out is original
    assert calls["regen"] == 1


def test_repair_skips_when_no_problem_paragraphs():
    original = {"bloques": [{"texto": "original"}]}
    gen, calls = _make_generator(
        enabled=True, max_calls=1, check_result={"problem_paragraphs": []},
    )
    validator = _StubValidator(score=0.9, passes=True)

    out = gen._maybe_editorial_repair(
        original, SimpleNamespace(score=0.4, passes=False), validator, None, {},
    )
    assert out is original
    assert calls["regen"] == 0


def test_ensure_paragraph_indices_backfills():
    block_a = {"texto": "a"}
    block_b = {"texto": "b"}
    block_c = {"texto": "c"}
    enriched = {
        "parrafos": [
            {"bloques": [block_a, block_b]},
            {"bloques": [block_c]},
        ],
        "bloques": [block_a, block_b, block_c],
    }
    ScriptGenerator._ensure_paragraph_indices(enriched)
    assert block_a["paragraph_idx"] == 0
    assert block_a["is_last_in_paragraph"] is False
    assert block_b["paragraph_idx"] == 0
    assert block_b["is_last_in_paragraph"] is True
    assert block_c["paragraph_idx"] == 1
    assert block_c["is_last_in_paragraph"] is True
