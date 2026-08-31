#!/usr/bin/env python3
"""Backfill retroactivo de la rebaja de frecuencia por strike de spam.

Contexto: algunos strikes históricos se registraron antes de desplegar
`reduce_publication_frequency_after_strike`. Este script permite aplicar la
rebaja a canales seleccionados explícitamente.

Este script aplica la rebaja retroactivamente a los canales bloqueados. Es
IDEMPOTENTE a nivel de los valores originales (el restore key solo se guarda la
primera vez), pero cada llamada vuelve a recalcular la rebaja sobre el valor
actual, así que ejecutarlo dos veces sobre el mismo canal solo recalcula sobre
valores ya rebajados (sin efecto adicional en long/nativos).

Uso:
    python3 scripts/backfill_spam_freq.py --slug canal4 --dry-run

Efecto esperado (por canal y hermanos del mismo proyecto GCP):
    - videos_per_day 2 → 1
    - shorts_native_per_day 2 → 1
    - shorts_clips_per_long → floor(valor/2)
    - Se crea spam_freq_restore_{id} con los valores originales para
      restauración manual vía panel ("Restaurar frecuencia").
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.runtime_context import add_channel_selector_arguments, resolve_channels, SelectorError


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    add_channel_selector_arguments(parser)
    parser.add_argument("--dry-run", action="store_true", help="mostrar canales sin modificar frecuencia")
    args = parser.parse_args()
    try:
        channels = resolve_channels(
            channel_id=args.channel_id, slug=args.slug, project=args.project,
            all_channels=args.all_channels, yes=args.yes,
        )
    except SelectorError as exc:
        parser.error(str(exc))

    if args.dry_run:
        for channel in channels:
            print(f"DRY RUN: {channel.slug} (id={channel.id}, project={channel.project})")
        return 0

    from api.services.spam_mitigation import reduce_publication_frequency_after_strike

    all_affected: list[int] = []
    for channel in channels:
        affected = reduce_publication_frequency_after_strike(channel.id, channel.slug)
        all_affected.extend(affected)
        print(f"✓ {channel.slug} (id={channel.id}): frecuencia rebajada — afectados: {affected}")

    print(f"\nTotal canales afectados: {sorted(set(all_affected))}")
    print("Restauración manual vía panel (o POST /api/system/spam-blocks/{id}/restore-frequency).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
