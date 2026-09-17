"""Tests de T1.6: endurecimiento de ``content_safety`` (consejo médico / AI doctor).

Se prueba la capa determinista pura (``_deterministic_check``) para no tocar la
DB ni llamar al LLM.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.content_safety import _deterministic_check  # noqa: E402


def test_medical_advice_first_person_blocked():
    v = _deterministic_check(["Te recomiendo tomar este remedio para dormir mejor"])
    assert v is not None and v.safe is False
    assert "consejo_medico" in v.categories


def test_ai_doctor_persona_blocked():
    v = _deterministic_check(["Hola, soy médico y te voy a explicar qué tratamiento seguir"])
    assert v is not None and v.safe is False
    assert "consejo_medico" in v.categories


def test_medical_documentary_not_blocked():
    # Documental médico legítimo: no debe caer por "consejo_medico".
    v = _deterministic_check([
        "El síndrome de la piel de piedra: esclerodermia sistémica progresiva",
        "Una enfermedad rara documentada por la ciencia desde 1752.",
    ])
    if v is not None:  # si otro tier lo bloquea, no debe ser por consejo_medico
        assert "consejo_medico" not in v.categories


def test_existing_claim_patterns_still_work():
    v = _deterministic_check(["Esta cura milagrosa elimina el cáncer sin quimioterapia"])
    assert v is not None and v.safe is False
    assert "claims_medicos" in v.categories
