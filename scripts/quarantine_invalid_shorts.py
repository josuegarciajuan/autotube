#!/usr/bin/env python3
"""Quarantine queued Shorts whose rendered MP4 is not a valid 9:16 short.

Oct 2026 incident: four generated shorts were 1080x1920 but had a video
stream of 991-1657 s with 36-42 s of audio, and `shorts.duration` was NULL.
This script re-probes every queued artifact (never trusts the DB duration),
marks the invalid ones as `failed`, and releases their planned slots so the
planner can render a replacement.

Safe by default: dry-run. Use ``--apply`` to mutate the database.

Usage:
    python3 scripts/quarantine_invalid_shorts.py            # dry-run
    python3 scripts/quarantine_invalid_shorts.py --apply    # apply
    python3 scripts/quarantine_invalid_shorts.py --ids 2148,2149 --apply
    # Re-render immediate replacement only for the recent ones:
    python3 scripts/quarantine_invalid_shorts.py --ids 2148,2149 --release-slots --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import DATABASE_PATH  # noqa: E402

QUEUED_STATUSES = ("generated", "ready", "pending", "rendering")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _candidates(conn: sqlite3.Connection, ids: list[int] | None) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in QUEUED_STATUSES)
    params: list = list(QUEUED_STATUSES)
    id_clause = ""
    if ids:
        id_clause = "AND s.id IN (%s)" % ",".join("?" for _ in ids)
        params.extend(ids)
    return conn.execute(
        f"""SELECT s.id, s.channel_id, s.status, s.file_path, s.duration,
                   c.slug
              FROM shorts s
              JOIN channels c ON c.id = s.channel_id
             WHERE s.status IN ({placeholders})
               AND s.file_path IS NOT NULL AND s.file_path != ''
               {id_clause}
             ORDER BY s.id""",
        params,
    ).fetchall()


def _max_duration(channel_slug: str) -> float:
    try:
        from config.config_bridge import get_channel_config
        cfg = get_channel_config(channel_slug)
        return float(getattr(cfg, "SHORTS_MAX_DURATION_SEC", 58.0))
    except Exception as exc:  # noqa: BLE001 — fall back to the global budget
        print(f"  ! config de {channel_slug} no disponible ({exc}); uso 58.0s")
        return 58.0


def _quarantine(conn: sqlite3.Connection, row: sqlite3.Row, reason: str,
                *, release_slots: bool) -> None:
    """Mark the short failed and free its slot.

    ``release_slots=False`` (default): the slot is **cancelled**. It stops
    occupying the plan (a `generated` slot is "protected" and blocks new
    planning) without triggering an immediate render of dozens of historical
    artifacts. `reconcile_shorts_schedule` creates fresh slots normally.

    ``release_slots=True``: the slot returns to `pending` so the dispatcher
    renders a replacement now (useful for a handful of recent shorts).
    """
    conn.execute(
        """UPDATE shorts
              SET status='failed', error_message=?, updated_at=datetime('now')
            WHERE id=?""",
        (f"cuarentena render inválido: {reason}"[:300], row["id"]),
    )
    if release_slots:
        conn.execute(
            """UPDATE shorts_planned_slots
                  SET status='pending', short_id=NULL, job_id=NULL,
                      retry_count=0, scheduled_at=datetime('now'),
                      error_message='reemplazo tras cuarentena de render inválido',
                      updated_at=CURRENT_TIMESTAMP
                WHERE short_id=?
                  AND status IN ('generated','pending','running','failed','completed')""",
            (row["id"],),
        )
    else:
        conn.execute(
            """UPDATE shorts_planned_slots
                  SET status='cancelled', short_id=NULL, job_id=NULL,
                      error_message='slot liberado: render en cuarentena',
                      updated_at=CURRENT_TIMESTAMP
                WHERE short_id=?
                  AND status IN ('generated','pending','running','failed','completed')""",
            (row["id"],),
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Cuarentena de shorts inválidos")
    ap.add_argument("--apply", action="store_true", help="Aplica cambios (por defecto dry-run)")
    ap.add_argument("--ids", default="", help="IDs separados por coma (opcional)")
    ap.add_argument("--release-slots", action="store_true",
                    help="Devuelve los slots a pending (re-render inmediato). "
                         "Por defecto se cancelan y el planificador crea reemplazos.")
    ap.add_argument("--include-missing", action="store_true",
                    help="Incluye shorts cuyo MP4 ya no existe (orphan cleanup).")
    args = ap.parse_args()

    ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()] or None

    from pipeline.shorts_media import validate_short_render

    conn = _connect()
    rows = _candidates(conn, ids)
    print(f"Candidatos en cola: {len(rows)}")

    invalid: list[tuple[sqlite3.Row, str]] = []
    for row in rows:
        path = Path(row["file_path"])
        if not path.exists():
            if not args.include_missing:
                continue
            reason = f"archivo no existe: {path}"
        else:
            try:
                validate_short_render(
                    path, max_duration_sec=_max_duration(row["slug"]),
                )
                continue
            except Exception as exc:  # noqa: BLE001 — invalid render
                reason = str(exc)
        invalid.append((row, reason))
        print(
            f"  ✗ short #{row['id']} ({row['slug']}, status={row['status']}): "
            f"{reason}"
        )

    if not invalid:
        print("✅ Ningún short en cola es inválido.")
        conn.close()
        return 0

    if not args.apply:
        print(f"\n🔍 DRY-RUN: {len(invalid)} inválidos. Usa --apply para cuarentena.")
        conn.close()
        return 0

    try:
        for row, reason in invalid:
            _quarantine(conn, row, reason, release_slots=args.release_slots)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(
        f"\n✅ Aplicado: {len(invalid)} shorts marcados failed y sus slots "
        f"liberados para re-render. Los MP4 NO se borran (purga manual tras validar)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
