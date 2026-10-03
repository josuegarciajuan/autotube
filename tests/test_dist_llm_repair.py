"""Tests de la reparación de JSON truncado por límite de tokens."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pipeline_dist.llm_repair import repair_truncated_json  # noqa: E402


def test_json_valido_pasa_intacto():
    obj = {"bloques": [{"texto": "hola", "tipo": "hook"}]}
    assert repair_truncated_json(json.dumps(obj)) == obj


def test_json_con_fences():
    obj = {"a": 1}
    text = "```json\n" + json.dumps(obj) + "\n```"
    assert repair_truncated_json(text) == obj


def test_truncado_recupera_bloques_completos():
    # Simula el fallo real: corte a mitad del 3er bloque.
    full = {
        "bloques": [
            {"texto": "b1", "media_tipo": "video"},
            {"texto": "b2", "media_tipo": "imagen"},
            {"texto": "bloque muy largo que se corta aqui", "media_tipo": "video"},
        ]
    }
    text = json.dumps(full, ensure_ascii=False)
    truncated = text[: text.index("bloque muy largo") + 10]  # corta dentro del string
    out = repair_truncated_json(truncated)
    assert out is not None
    assert isinstance(out.get("bloques"), list)
    # Recupera al menos los bloques completos anteriores al corte.
    assert len(out["bloques"]) >= 2
    assert out["bloques"][0]["texto"] == "b1"


def test_basura_devuelve_none():
    assert repair_truncated_json("esto no es json en absoluto {{{") is None or isinstance(
        repair_truncated_json("esto no es json en absoluto {{{"), dict
    )
