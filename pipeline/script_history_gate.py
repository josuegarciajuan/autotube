"""Gate de novedad de guion (T1.4 del experimento de recuperación de alcance).

Detecta guiones que reproducen casi literalmente el guion de un vídeo anterior
del mismo canal — el patrón de "plantilla" que YouTube penaliza como contenido
inauténtico. Se ejecuta en ``phase_pre_validate`` (antes de TTS/render), de modo
que un guion repetido no consume cómputo.

Métrica: Jaccard de *shingles* (n-gramas de palabras significativas). Es
determinista, sin coste de API y robusto a cambios menores de redacción. El
umbral por defecto es conservador (solo bloquea casi-duplicados).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from pipeline.topic_dedup import STOPWORDS, strip_accents

logger = logging.getLogger("autotube.script_history_gate")

DEFAULT_THRESHOLD = 0.55
DEFAULT_LOOKBACK = 80
SHINGLE_N = 4
MIN_SHINGLES = 15


@dataclass(frozen=True)
class NoveltyResult:
    novel: bool
    similar_to_id: int | None = None
    similarity: float = 0.0


def _shingles(text: str, n: int = SHINGLE_N) -> set[str]:
    """Conjunto de n-gramas de palabras significativas (sin stopwords)."""
    words = re.findall(r"[a-z0-9]+", strip_accents(text or "").lower())
    words = [w for w in words if len(w) >= 2 and w not in STOPWORDS]
    if len(words) < n:
        return set()
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _recent_scripts(db, slug: str, lookback: int,
                    exclude_id: int | None = None) -> list[tuple[int, str]]:
    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT id, guion FROM scripts WHERE canal = ? "
                "ORDER BY id DESC LIMIT ?",
                (slug, lookback),
            ).fetchall()
        out = [(int(r["id"]), r["guion"] or "") for r in rows]
        # El guion candidato ya está persistido en la tabla `scripts` (se inserta
        # en script_generator antes de la pre-validación), así que sin excluirlo
        # se compara consigo mismo y da similitud 1.00 → bloquea TODA generación.
        if exclude_id is not None:
            out = [(sid, g) for sid, g in out if sid != int(exclude_id)]
            # Solo guiones ANTERIORES: un id mayor se insertó después del
            # candidato (generación concurrente / reintento) y no es "historial".
            # Sin esto, comparar contra un guion posterior casi-idéntico
            # (mismo tema) daba sim=1.00 y bloqueaba el guion válido — causó la
            # sequía de canal3 (sep 2026).
            out = [(sid, g) for sid, g in out if sid < int(exclude_id)]
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("script-history lookup failed (%s): %s", slug, exc)
        return []


def check_script_novelty(
    guion: str,
    slug: str,
    db,
    threshold: float = DEFAULT_THRESHOLD,
    lookback: int = DEFAULT_LOOKBACK,
    exclude_id: int | None = None,
) -> NoveltyResult:
    """¿Es el guion suficientemente distinto de los previos del canal?

    ``exclude_id`` debe ser el ``id`` del propio guion candidato cuando ya está
    persistido en ``scripts`` (caso normal: ``script_generator`` lo inserta antes
    de la pre-validación). Sin excluirlo, el gate lo compara consigo mismo
    (similitud 1.00) y bloquea toda generación.

    Fail-open: ante cualquier error o guion demasiado corto, devuelve ``novel=True``
    (nunca bloquea por un fallo técnico).
    """
    try:
        candidate = _shingles(guion)
        if len(candidate) < MIN_SHINGLES:
            return NoveltyResult(True, None, 0.0)
        best_id, best = None, 0.0
        for sid, prior in _recent_scripts(db, slug, lookback,
                                          exclude_id=exclude_id):
            sim = jaccard(candidate, _shingles(prior))
            if sim > best:
                best, best_id = sim, sid
        return NoveltyResult(best < threshold, best_id, round(best, 3))
    except Exception as exc:  # noqa: BLE001
        logger.warning("script novelty gate failed (fail-open): %s", exc)
        return NoveltyResult(True, None, 0.0)


def get_novelty_settings(config=None) -> tuple[bool, float]:
    """``(enabled, threshold)`` desde la config del canal (fail-open)."""
    if config is None:
        return True, DEFAULT_THRESHOLD
    enabled = bool(getattr(config, "SCRIPT_HISTORY_GATE_ENABLED", True))
    try:
        threshold = float(getattr(config, "SCRIPT_HISTORY_GATE_THRESHOLD",
                                  DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        threshold = DEFAULT_THRESHOLD
    return enabled, max(0.0, min(1.0, threshold))
