#!/usr/bin/env python3
"""Corrige shorts mal clasificados en la tabla `videos`.

Problema: algunos Shorts de YouTube (duración < 60s) quedaron registrados en la
tabla `videos` (con video_path vacío y duracion_seg NULL) en lugar de `shorts`,
por lo que aparecían en la columna "Videos" del dashboard.

Este script los detecta, verifica la duración real vía yt-dlp (0 cuota) y los
mueve a la tabla `shorts` copiando su último snapshot de stats. Sin referencias
externas (source_video_id / planned_slots / content_schedules) que romper.

Uso:
    python3 scripts/fix_misclassified_shorts.py           # dry-run
    python3 scripts/fix_misclassified_shorts.py --apply   # aplica cambios
"""

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import DATABASE_PATH

SHORT_DURATION_THRESHOLD = 60  # segundos


def get_duration_ytdlp(youtube_id: str) -> int | None:
    """Devuelve la duración real en segundos vía yt-dlp (0 cuota). None si falla."""
    try:
        out = subprocess.run(
            [
                "yt-dlp", "--skip-download", "--no-warnings",
                "--print", "%(duration)s",
                f"https://youtube.com/watch?v={youtube_id}",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return None
        return int(float(out.stdout.strip().splitlines()[-1]))
    except Exception:
        return None


def find_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Videos publicados con video_path vacío y duracion_seg NULL (posibles shorts)."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """SELECT v.id, v.channel_id, v.titulo_final, v.yt_video_id, v.yt_url,
                  v.created_at, vsh.views, vsh.likes, vsh.comments,
                  vsh.estimated_minutes_watched, vsh.average_view_duration,
                  vsh.subscribers_gained, vsh.fetched_at
           FROM videos v
           JOIN video_stats_history vsh ON vsh.id = (
               SELECT MAX(id) FROM video_stats_history
               WHERE video_id = v.id AND views > 0
           )
           WHERE v.status = 'published'
             AND (v.video_path = '' OR v.video_path IS NULL)
             AND v.duracion_seg IS NULL"""
    ).fetchall()


def move_to_shorts(conn: sqlite3.Connection, row: sqlite3.Row, duration: int) -> None:
    """Mueve un video (short) a la tabla shorts copiando su último snapshot."""
    cur = conn.execute(
        """INSERT INTO shorts
             (channel_id, type, title, duration, status, published_at,
              youtube_id, youtube_url, file_path, longform_linked, created_at, updated_at)
           VALUES (?, 'native', ?, ?, 'published', ?, ?, ?, '', 0, ?, ?)""",
        (
            row["channel_id"],
            row["titulo_final"],
            duration,
            row["created_at"],
            row["yt_video_id"],
            row["yt_url"],
            row["created_at"],
            row["created_at"],
        ),
    )
    short_id = cur.lastrowid

    conn.execute(
        """INSERT INTO short_stats
             (short_id, yt_video_id, views, likes, comments,
              estimated_minutes_watched, average_view_duration,
              subscribers_gained, fetched_at, short_type, embeddable)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'native', 1)""",
        (
            short_id,
            row["yt_video_id"],
            row["views"],
            row["likes"],
            row["comments"],
            row["estimated_minutes_watched"],
            row["average_view_duration"],
            row["subscribers_gained"],
            row["fetched_at"],
        ),
    )

    # Borra el video y su historial de stats (ya migrado a shorts).
    conn.execute("DELETE FROM video_stats_history WHERE video_id = ?", (row["id"],))
    conn.execute("DELETE FROM videos WHERE id = ?", (row["id"],))


def main() -> int:
    parser = argparse.ArgumentParser(description="Corrige shorts mal clasificados.")
    parser.add_argument("--apply", action="store_true", help="Aplica los cambios (por defecto dry-run)")
    args = parser.parse_args()

    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row

    candidates = find_candidates(conn)
    print(f"Candidatos detectados (publicados, sin path, sin duración): {len(candidates)}")

    moved, skipped = 0, 0
    for row in candidates:
        duration = get_duration_ytdlp(row["yt_video_id"])
        if duration is None:
            print(f"  ⚠️  video #{row['id']} ({row['yt_video_id']}): yt-dlp falló — omitido")
            skipped += 1
            continue
        if duration >= SHORT_DURATION_THRESHOLD:
            print(f"  ·  video #{row['id']} ({row['yt_video_id']}): duración {duration}s ≥ 60s — NO es short, omitido")
            skipped += 1
            continue

        print(
            f"  ✅ short #{row['id']} ({row['yt_video_id']}) {duration}s "
            f"'{row['titulo_final']}' → mover a shorts"
        )
        if args.apply:
            move_to_shorts(conn, row, duration)
        moved += 1

    if args.apply:
        conn.commit()
        print(f"\n✅ Aplicado: {moved} shorts movidos a la tabla shorts, {skipped} omitidos.")
    else:
        print(f"\n🔍 DRY-RUN: {moved} movibles, {skipped} omitidos. Usa --apply para aplicar.")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
