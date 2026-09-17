"""Tests de ``pipeline/topic_semantic.py`` (dedup semántico del experimento).

Cubren: similitud coseno, detección por embeddings (paráfrasis sin tokens
compartidos), degradación a tokens cuando no hay embeddings y settings.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline import topic_semantic as ts  # noqa: E402


def test_cosine_basics():
    assert ts.cosine([1, 0], [1, 0]) == 1.0
    assert ts.cosine([1, 0], [0, 1]) == 0.0
    assert ts.cosine([], [1, 0]) == 0.0
    assert ts.cosine([1, 0], [1, 0, 0]) == 0.0  # dimensión distinta


def test_detect_duplicate_tokens():
    entries = [{"topic_label": "El secreto enterrado de Göbekli Tepe"}]
    is_dup, matched, method = ts.detect_duplicate(
        "El secreto de Göbekli Tepe", entries, use_semantic=False,
    )
    assert is_dup is True
    assert method == "tokens"
    assert "Tepe" in matched


def test_detect_duplicate_embeddings_paraphrase(monkeypatch):
    candidate = "El hombre que nunca conciliaba el sueño"
    entries = [
        {"topic_label": "Historias de naufragios en el Ártico"},
        {"topic_label": "Insomnio familiar fatal"},
    ]

    def fake_embed(texts):
        # candidate y "Insomnio familiar fatal" comparten vector; el resto ortogonal.
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]]

    monkeypatch.setattr(ts, "embed_texts", fake_embed)

    is_dup, matched, method = ts.detect_duplicate(
        candidate, entries, token_threshold=0.5, semantic_threshold=0.86,
    )
    assert method == "embeddings", "los tokens no bastan; debe resolver el embedding"
    assert is_dup is True
    assert matched == "Insomnio familiar fatal"


def test_detect_duplicate_semantic_unavailable_no_false_positive(monkeypatch):
    monkeypatch.setattr(ts, "embed_texts", lambda texts: None)
    is_dup, matched, method = ts.detect_duplicate(
        "Un tema totalmente nuevo sobre volcanes", 
        [{"topic_label": "Insomnio familiar fatal"}],
    )
    assert is_dup is False
    assert matched is None
    assert method is None


def test_semantic_threshold_rejects_far_vectors(monkeypatch):
    monkeypatch.setattr(
        ts, "embed_texts",
        lambda texts: [[1.0, 0.0]] + [[0.0, 1.0] for _ in texts[1:]],
    )
    matched, sim = ts.semantic_best_match(
        "candidato", [{"topic_label": "otro"}], threshold=0.86,
    )
    assert matched is None
    assert sim == 0.0


class _Cfg:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_semantic_settings_defaults_and_override():
    enabled, thr = ts.get_semantic_settings(None)
    assert enabled is True
    assert 0.0 < thr <= 1.0

    enabled, thr = ts.get_semantic_settings(
        _Cfg(TOPIC_DEDUP_SEMANTIC_ENABLED=False,
             TOPIC_DEDUP_SEMANTIC_THRESHOLD=0.9)
    )
    assert enabled is False
    assert thr == 0.9

    # umbral inválido → default seguro
    _, thr_bad = ts.get_semantic_settings(
        _Cfg(TOPIC_DEDUP_SEMANTIC_THRESHOLD="no-es-numero")
    )
    assert 0.0 < thr_bad <= 1.0
