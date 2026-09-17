"""Dedup semántico de temáticas — capa sobre ``pipeline/topic_dedup.py``.

Objetivo del experimento de recuperación de alcance: evitar que un canal vuelva
a publicar el mismo tema con un título cosméticamente distinto (causa directa de
la penalización por "contenido inauténtico/repetitivo").

Tres niveles, de más barato a más caro:

1. **Tokens** (``topic_dedup``): determinista, coste 0. Atrapa near-duplicados.
2. **Embeddings**: se piden al proveedor IA ya integrado
   (``config.llm_client``). Atrapa paráfrasis que no comparten apenas tokens.
3. **LLM** (adjudicación): reservado para zona gris; ver ``llm_same_topic``.

Si los embeddings no están disponibles (el proveedor no expone ``/embeddings``,
sin key, offline), **degrada a tokens sin romper** — el dedup nunca falla abierto
a publicar repetidos por un error técnico.
"""

from __future__ import annotations

import logging
import math
import os

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = os.getenv("TOPIC_EMBEDDING_MODEL", "text-embedding-3-small")
DEFAULT_SEMANTIC_THRESHOLD = 0.86
# Máximo de temas consumidos contra los que comparar (acota coste/tiempo).
MAX_COMPARE = 300


def cosine(a: list[float], b: list[float]) -> float:
    """Similitud coseno en [0,1] (vectores de embeddings)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embeddings del proveedor IA configurado, o ``None`` si no está disponible."""
    texts = [t for t in (texts or []) if t]
    if not texts:
        return None
    try:
        from config.llm_client import create_llm_client
        client = create_llm_client()
        resp = client.embeddings.create(
            model=DEFAULT_EMBEDDING_MODEL, input=texts,
        )
        vectors = [list(d.embedding) for d in resp.data]
        if len(vectors) != len(texts):
            return None
        return vectors
    except Exception as exc:  # noqa: BLE001
        logger.debug("embeddings unavailable (%s): %s", type(exc).__name__, str(exc)[:120])
        return None


def _entry_label(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("topic_label") or entry.get("label") or "")
    if isinstance(entry, (tuple, list)) and entry:
        return str(entry[0] or "")
    return ""


def semantic_best_match(
    candidate_label: str,
    entries: list,
    threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
) -> tuple[str | None, float]:
    """Mejor coincidencia semántica por embeddings. ``(label|None, similitud)``."""
    labels = [_entry_label(e) for e in entries[:MAX_COMPARE]]
    pairs = [(lbl, e) for lbl, e in zip(labels, entries[:MAX_COMPARE]) if lbl]
    if not candidate_label or not pairs:
        return None, 0.0
    vectors = embed_texts([candidate_label] + [lbl for lbl, _ in pairs])
    if not vectors:
        return None, 0.0
    cvec, ovecs = vectors[0], vectors[1:]
    best_label, best_sim = None, 0.0
    for (lbl, _e), vec in zip(pairs, ovecs):
        sim = cosine(cvec, vec)
        if sim > best_sim:
            best_sim, best_label = sim, lbl
    if best_sim >= threshold:
        return best_label, best_sim
    return None, best_sim


def llm_same_topic(a: str, b: str, timeout: float = 30.0) -> bool | None:
    """Adjudicación zona gris: ¿son el mismo tema? ``None`` si el LLM no responde.

    Reservado: no se usa por defecto (coste por par). Disponible para un futuro
    gate de zona gris.
    """
    try:
        from config.llm_client import create_llm_client
        client = create_llm_client()
        resp = client.chat.completions.create(
            model=os.getenv("LLM_MODEL", "deepseek-v4-flash"),
            messages=[
                {"role": "system", "content": (
                    "Decide si dos titulares tratan EL MISMO tema/noticia concreta. "
                    "Responde solo SI o NO."
                )},
                {"role": "user", "content": f"1) {a}\n2) {b}"},
            ],
            timeout=timeout,
        )
        answer = (resp.choices[0].message.content or "").strip().upper()
        return answer.startswith("SI") or answer.startswith("SÍ") or answer.startswith("YES")
    except Exception as exc:  # noqa: BLE001
        logger.debug("llm_same_topic failed: %s", str(exc)[:120])
        return None


def detect_duplicate(
    candidate_label: str,
    entries: list,
    *,
    token_threshold: float = 0.5,
    min_shared: int = 2,
    semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    use_semantic: bool = True,
) -> tuple[bool, str | None, str | None]:
    """¿Es un tema ya consumido? ``(is_dup, matched_label, method)``.

    ``method`` ∈ ``{"tokens", "embeddings", None}``.

    Acepta entradas heterogéneas (dicts de DB con ``tokens_json``, tuplas
    ``(label, tokens)``, o dicts sin tokens) y las normaliza: si faltan tokens,
    se derivan del label. Así el dedup no falla abierto por una entrada parcial.
    """
    from pipeline.topic_dedup import (
        find_best_duplicate, topic_tokens, tokens_from_json,
    )

    if not candidate_label or not entries:
        return False, None, None

    normalized: list[dict] = []
    for e in entries:
        label = _entry_label(e)
        if not label:
            continue
        if isinstance(e, dict):
            toks = e.get("tokens")
            if not toks:
                toks = tokens_from_json(e.get("tokens_json"))
        elif isinstance(e, (tuple, list)) and len(e) >= 2:
            toks = e[1]
        else:
            toks = None
        if not toks:
            toks = topic_tokens(label)
        normalized.append({"topic_label": label, "tokens": toks})
    if not normalized:
        return False, None, None

    toks = topic_tokens(candidate_label)
    if toks:
        matched, _sim = find_best_duplicate(
            toks, normalized, threshold=token_threshold, min_shared=min_shared,
        )
        if matched:
            return True, matched, "tokens"

    if use_semantic:
        try:
            matched, _sim = semantic_best_match(
                candidate_label, normalized, threshold=semantic_threshold,
            )
            if matched:
                return True, matched, "embeddings"
        except Exception as exc:  # noqa: BLE001
            logger.debug("semantic dedup failed (fail-open to tokens): %s", exc)

    return False, None, None


def get_semantic_settings(config=None) -> tuple[bool, float]:
    """``(enabled, threshold)`` desde la config del canal (fail-open a True)."""
    if config is None:
        return True, DEFAULT_SEMANTIC_THRESHOLD
    enabled = bool(getattr(config, "TOPIC_DEDUP_SEMANTIC_ENABLED", True))
    try:
        threshold = float(getattr(config, "TOPIC_DEDUP_SEMANTIC_THRESHOLD",
                                  DEFAULT_SEMANTIC_THRESHOLD))
    except (TypeError, ValueError):
        threshold = DEFAULT_SEMANTIC_THRESHOLD
    return enabled, max(0.0, min(1.0, threshold))
