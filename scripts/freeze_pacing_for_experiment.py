#!/usr/bin/env python3
"""Congela la cadencia en `recovery` durante el experimento de recuperación.

Motivación: el experimento (`specs/experimento-recuperacion-alcance.md`) exige
un ritmo MENOR mientras dure (T+45). La transición automática de pacing puede
escalar a `normal` (más shorts/día) y romper el supuesto del experimento.

Uso:
    python3 scripts/freeze_pacing_for_experiment.py            # aplicar (recovery)
    python3 scripts/freeze_pacing_for_experiment.py --profile strike
    python3 scripts/freeze_pacing_for_experiment.py --off      # revertir

Deja el kill-switch `auto_pacing_transition=false` para que no se relaje solo.
Idempotente y sin efectos sobre contenido.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("freeze_pacing")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", default="recovery",
        choices=("strike", "recovery", "normal"),
        help="Perfil a fijar durante el experimento (default: recovery).",
    )
    parser.add_argument(
        "--off", action="store_true",
        help="Revierte: re-activa la auto-transición y limpia el override manual.",
    )
    args = parser.parse_args(argv)

    from database.db_extended import ExtendedDatabase
    from api.services.pacing_profile import recovery_experiment_days_left

    db = ExtendedDatabase()
    days_left = recovery_experiment_days_left(db)

    if args.off:
        db.set_system_state("auto_pacing_transition", "true")
        logger.info("Auto-transición de pacing RE-ACTIVADA")
        return 0

    if days_left is None:
        logger.warning(
            "El experimento no está activo (o ya venció T+45). No se congela la "
            "cadencia; usa --off si quieres revertir."
        )

    db.set_system_state("pacing_profile", args.profile)
    db.set_system_state("auto_pacing_transition", "false")
    logger.info(
        "Cadencia congelada en '%s' (auto_pacing_transition=false). "
        "Días restantes de experimento: %s",
        args.profile, days_left if days_left is not None else "n/a",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
