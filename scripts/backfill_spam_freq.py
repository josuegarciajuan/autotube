#!/usr/bin/env python3
"""Backfill retroactivo de la rebaja de frecuencia por strike de spam.

Contexto: los strikes de spam de canal4 (id 5) y canal5 (id 7) se registraron
el 2026-08-19/20, ANTES de desplegar `reduce_publication_frequency_after_strike`
(commit 930f39e, 2026-08-20). Por eso la rebaja de frecuencia nunca se aplicó:
`videos_per_day` seguía en 2 y no existían las claves `spam_freq_restore_*`.

Este script aplica la rebaja retroactivamente a los canales bloqueados. Es
IDEMPOTENTE a nivel de los valores originales (el restore key solo se guarda la
primera vez), pero cada llamada vuelve a recalcular la rebaja sobre el valor
actual, así que ejecutarlo dos veces sobre el mismo canal solo recalcula sobre
valores ya rebajados (sin efecto adicional en long/nativos).

Uso:
    python3 scripts/backfill_spam_freq.py

Efecto esperado (por canal y hermanos del mismo proyecto GCP):
    - videos_per_day 2 → 1
    - shorts_native_per_day 2 → 1
    - shorts_clips_per_long → floor(valor/2)
    - Se crea spam_freq_restore_{id} con los valores originales para
      restauración manual vía panel ("Restaurar frecuencia").
"""
from __future__ import annotations

import sys


def main() -> int:
    from api.services.spam_mitigation import reduce_publication_frequency_after_strike

    # Canales bloqueados por strike de spam (AGENTS.md: canal4 id=5, canal5 id=7).
    targets = [(5, "canal4"), (7, "canal5")]

    all_affected: list[int] = []
    for cid, slug in targets:
        affected = reduce_publication_frequency_after_strike(cid, slug)
        all_affected.extend(affected)
        print(f"✓ {slug} (id={cid}): frecuencia rebajada — afectados: {affected}")

    print(f"\nTotal canales afectados: {sorted(set(all_affected))}")
    print("Restauración manual vía panel (o POST /api/system/spam-blocks/{id}/restore-frequency).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
