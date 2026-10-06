#!/usr/bin/env python3
"""Resolve stale/obsolete alerts from ``pipeline_alerts`` by explicit id.

Uso puntual para limpiar ruido que el health monitor no cerró (o cerró tarde).
Es deliberadamente explícito: exige ``--ids`` (nunca barre por patrón), muestra
en dry-run lo que haría y sólo escribe con ``--apply``.

Protección: NUNCA toca alertas de revisión (``review_visibility_mismatch`` /
``review_exact_duplicate``); si se pide un id de ese tipo se salta sin resolver.

Usage:
    python3 scripts/resolve_stale_alerts.py --ids 1,2,3            # dry-run
    python3 scripts/resolve_stale_alerts.py --ids 1,2,3 --apply
    python3 scripts/resolve_stale_alerts.py --ids 1 --apply --reason "ruido aug 2026"
"""

import argparse
import logging
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("resolve_stale_alerts")

# Alertas que requieren acción humana: nunca se resuelven por script.
PROTECTED_ALERT_TYPES = {"review_visibility_mismatch", "review_exact_duplicate"}


def get_db():
    from database.db_extended import ExtendedDatabase, migrate_v2
    migrate_v2()
    return ExtendedDatabase()


def _parse_ids(raw: str) -> list[int]:
    ids = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            raise SystemExit(f"ID inválido: {chunk!r}")
    if not ids:
        raise SystemExit("--ids no contiene ningún id válido.")
    return ids


def run(ids: list[int], apply: bool, reason: str) -> int:
    db = get_db()
    db_path = os.getenv("DATABASE_PATH", str(_PROJECT_ROOT / "autotube.db"))
    print(f"\n=== RESOLVE STALE ALERTS ===\n  DB: {db_path}")

    with db._connect() as conn:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""SELECT id, alert_type, entity_type, entity_id, severity,
                       title, resolved
                FROM pipeline_alerts WHERE id IN ({placeholders})
                ORDER BY id""",
            ids,
        ).fetchall()
        found_ids = {row["id"] for row in rows}

        missing = [i for i in ids if i not in found_ids]
        eligible = []
        skipped_protected = []
        skipped_resolved = []

        print("\n  ID   RES  SEV       TIPO                         ENTIDAD        TÍTULO")
        for row in rows:
            protected = (
                row["alert_type"] in PROTECTED_ALERT_TYPES
                or str(row["alert_type"] or "").startswith("review_")
            )
            already = int(row["resolved"] or 0) == 1
            if protected:
                skipped_protected.append(row["id"])
                tag = "SKIP(REVIEW)"
            elif already:
                skipped_resolved.append(row["id"])
                tag = "SKIP(YA RES)"
            else:
                eligible.append(row["id"])
                tag = "RESOLVER"
            entity = f"{row['entity_type']}/{row['entity_id']}"
            title = (row["title"] or "")[:40]
            print(f"  {row['id']:<4} {int(row['resolved'] or 0):<3} "
                  f"{str(row['severity'] or ''):<9} {str(row['alert_type'] or ''):<28} "
                  f"{entity:<14} {tag:<12} {title}")

        for mid in missing:
            print(f"  {mid:<4} --  {'':<9} {'':<28} {'':<14} NO ENCONTRADA")

        print("\n  --- Resumen ---")
        print(f"  Solicitadas:        {len(ids)}")
        print(f"  A resolver:         {len(eligible)}  {eligible}")
        print(f"  Ya resueltas:       {len(skipped_resolved)}")
        print(f"  Protegidas (review):{len(skipped_protected)}  {skipped_protected}")
        print(f"  No encontradas:     {len(missing)}")

        if not apply:
            print(f"\n[dry-run] Se resolverían {len(eligible)} alertas. "
                  "Ejecuta con --apply para aplicarlo.")
            return 0

        if not eligible:
            print("\n[apply] Nada que resolver.")
            return 0

        ep = ",".join("?" for _ in eligible)
        suffix = f" [Resuelto manualmente: {reason}]"
        cur = conn.execute(
            f"""UPDATE pipeline_alerts
                   SET resolved = 1, resolved_at = datetime('now'), acknowledged = 1,
                       message = COALESCE(message, '') || ?
                 WHERE id IN ({ep}) AND resolved = 0""",
            [suffix, *eligible],
        )
        conn.commit()
        updated = cur.rowcount

        remaining = [r["id"] for r in conn.execute(
            f"SELECT id FROM pipeline_alerts WHERE id IN ({ep}) AND resolved = 0",
            eligible,
        ).fetchall()]

        print(f"\n[apply] Resueltas {updated} alertas.")
        if remaining:
            print(f"  ⚠ No resueltas tras el UPDATE: {remaining}")
        return updated


def main():
    parser = argparse.ArgumentParser(
        description="Resuelve alertas de pipeline_alerts por id (dry-run por defecto)."
    )
    parser.add_argument("--ids", required=True,
                        help="CSV de ids de pipeline_alerts, p.ej. 12,34,56")
    parser.add_argument("--apply", action="store_true",
                        help="Aplica los cambios (por defecto solo previsualiza).")
    parser.add_argument("--reason", default="limpieza manual",
                        help="Motivo que se añade al mensaje de la alerta.")
    args = parser.parse_args()

    ids = _parse_ids(args.ids)
    updated = run(ids, args.apply, args.reason)
    if args.apply:
        logger.info("Done. %d alertas resueltas.", updated)


if __name__ == "__main__":
    main()
