"""Parches de estabilidad del generador de guion (aditivos, en runtime).

Problema detectado: el enriquecido pedía 5 bloques completos en un JSON con
`max_tokens=2048` (`pipeline/script_generator.py:1619,1894`). La salida real
ronda los ~7.000-7.700 caracteres, así que el modelo se corta y el JSON queda
truncado (`Unterminated string`) de forma crónica; al agotar el pool cae al
fallback silencioso de "bloques crudos", que usa el título del contenido como
query visual para TODAS las escenas → riesgo de assets repetidos/placeholders.

Estos parches se aplican SOLO en el proceso del pipeline distribuido y no
modifican los ficheros del pipeline clásico:
  1. `ENRICH_BATCH_SIZE = 2` (configurable por env) para que cada salida quepa.
  2. Reintento por bloque individual si un lote vuelve vacío, con log de error
     (deja de degradar en silencio).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def apply_llm_stability_patches() -> bool:
    """Aplica los parches sobre `ScriptGenerator`. Idempotente.

    Devuelve True si quedaron aplicados (o ya lo estaban).
    """
    try:
        from pipeline import script_generator as sg
    except Exception as exc:  # pragma: no cover - import pesado
        logger.warning("No se pudo importar script_generator para parchear: %s", exc)
        return False

    SG = sg.ScriptGenerator
    if getattr(SG, "_dist_stability_patched", False):
        return True

    batch_size = max(1, int(os.environ.get("AUTOTUBE_ENRICH_BATCH_SIZE", "2") or 2))
    SG.ENRICH_BATCH_SIZE = batch_size

    original = SG._enrich_block_fields_batch

    def _patched_enrich(self, batch, previous_tipo, batch_num, num_batches, content_item):
        result = original(self, batch, previous_tipo, batch_num, num_batches, content_item)
        if result:
            return result
        logger.error(
            "Enrich batch %d vacío (posible truncación JSON); reintento por bloques de 1",
            batch_num,
        )
        out: list[dict] = []
        prev = previous_tipo
        for block in batch:
            one = original(self, [block], prev, batch_num, num_batches, content_item)
            if not one:
                logger.error(
                    "Enrich bloque individual también falló; se usará fallback crudo "
                    "(media degradada)"
                )
                return []
            out.extend(one)
            prev = one[-1].get("tipo", prev)
        return out

    _patched_enrich.__name__ = getattr(original, "__name__", "_enrich_block_fields_batch")
    SG._enrich_block_fields_batch = _patched_enrich
    SG._dist_stability_patched = True
    logger.info(
        "Parches LLM aplicados: ENRICH_BATCH_SIZE=%d + reintento por bloque", batch_size
    )
    return True
