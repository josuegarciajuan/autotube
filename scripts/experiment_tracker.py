#!/usr/bin/env python3
"""Instrumentación del experimento de recuperación de alcance.

Sella el baseline del experimento en ``system_state`` y programa los
recordatorios de checkpoint (T+7 / T+21 / T+45) como ``scheduled_reminders``.
Al vencer, el loop `reminders` de ``api/main.py`` emite una alerta de sistema
(el operador analiza y decide — este script NUNCA publica ni cambia contenido).

Uso:
    python3 scripts/experiment_tracker.py baseline
    python3 scripts/experiment_tracker.py schedule --start 2026-09-17
    python3 scripts/experiment_tracker.py status

Contrato funcional: ``specs/experimento-recuperacion-alcance.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("experiment_tracker")

EXPERIMENT_ID = "recuperacion-alcance"
BASELINE_KEY = "experiment_baseline"
STARTED_KEY = "experiment_started_at"
CHECKPOINTS_KEY = "experiment_checkpoints"

CHECKPOINT_DAYS = (7, 14, 21, 45)
CHECKPOINT_HOUR_UTC = 9  # 09:00 UTC

# Los checkpoints salen como ALERTA CRÍTICA en el panel (no pasan desapercibidos).
CHECKPOINT_SEVERITY = "critical"

# Qué mirar en cada checkpoint (va en el mensaje de la alerta).
CHECKPOINT_BRIEFS = {
    7: (
        "Señal temprana: ¿hay avisos de política nuevos? ¿el alcance Shorts a 7 d "
        "del formato nuevo supera el baseline? ¿algún long-form con distribución "
        "(browse/suggested) y retención > baseline?"
    ),
    14: (
        "Revisión intermedia: ¿mejoran los KPIs leading (CTR e impresiones por "
        "vídeo long-form) tras la Fase 1 de packaging? Ejecuta "
        "'python3 scripts/experiment_report.py' y decide continuar, refinar o "
        "revertir según la matriz del spec §6."
    ),
    21: (
        "Análisis principal: retención long-form (objetivo >40 %), subs NETOS por "
        "canal, watch-hours/día y ratio de temas rechazados por dedup/novedad. "
        "Refinar temas, hooks y packaging según las reglas de decisión del spec."
    ),
    45: (
        "Decisión de rumbo: escalar los formatos con retención probada o reajustar "
        "la ruta a YPP (1.000 subs + 4.000 h). Aplicar la matriz de decisión del "
        "spec y anotar el resultado en specs/experimento-recuperacion-alcance.md."
    ),
}


# ── Helpers SQL (funcionan con cualquier objeto que exponga _connect()) ──

def _one(conn, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else {}


def _scalar(conn, sql: str, params: tuple = (), default=0):
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


def compute_baseline(db) -> dict:
    """Calcula el baseline por canal y agregado. Solo lectura."""
    channels: list[dict] = []
    with db._connect() as conn:
        ch_rows = conn.execute(
            "SELECT id, slug, name FROM channels "
            "WHERE active = 1 AND slug != 'test' ORDER BY id"
        ).fetchall()

        for ch in ch_rows:
            cid = ch["id"]

            latest_ch = _one(
                conn,
                "SELECT subscribers, total_views, video_count, "
                "       estimated_minutes_watched "
                "FROM channel_stats_history WHERE channel_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (cid,),
            )
            prev7_ch = _one(
                conn,
                "SELECT subscribers FROM channel_stats_history "
                "WHERE channel_id = ? AND fetched_at <= datetime('now','-7 days') "
                "  AND subscribers > 0 ORDER BY id DESC LIMIT 1",
                (cid,),
            )

            lf = _one(
                conn,
                "SELECT COUNT(*) n, COALESCE(SUM(vsh.views),0) views, "
                "       COALESCE(AVG(vsh.views),0) avg_views "
                "FROM videos v JOIN video_stats_history vsh ON vsh.id = ("
                "  SELECT MAX(x.id) FROM video_stats_history x "
                "  WHERE x.video_id = v.id AND x.views > 0) "
                "WHERE v.channel_id = ? AND v.yt_video_id IS NOT NULL",
                (cid,),
            )
            retention = _scalar(
                conn,
                "SELECT AVG(vad.metric_value) FROM video_analytics_detailed vad "
                "JOIN videos v ON v.id = vad.video_id "
                "WHERE v.channel_id = ? AND vad.report_type = 'retention_pct' "
                "  AND vad.metric_value > 0",
                (cid,),
                default=None,
            )

            sh = _one(
                conn,
                "SELECT COUNT(*) n, COALESCE(SUM(ss.views),0) views, "
                "       COALESCE(AVG(ss.views),0) avg_views "
                "FROM shorts s JOIN short_stats ss ON ss.id = ("
                "  SELECT MAX(x.id) FROM short_stats x "
                "  WHERE x.short_id = s.id AND x.views > 0) "
                "WHERE s.channel_id = ? AND s.status = 'published' "
                "  AND s.youtube_id IS NOT NULL",
                (cid,),
            )
            reach7 = _scalar(
                conn,
                "SELECT AVG(v7) FROM ("
                "  SELECT MAX(CASE WHEN julianday(ss.fetched_at) - "
                "       julianday(COALESCE(s.actual_published_at, s.published_at)) <= 7 "
                "       THEN ss.views END) v7 "
                "  FROM shorts s JOIN short_stats ss ON ss.short_id = s.id "
                "  WHERE s.channel_id = ? AND s.status = 'published' "
                "    AND s.youtube_id IS NOT NULL GROUP BY s.id) "
                "WHERE v7 IS NOT NULL",
                (cid,),
                default=None,
            )
            repeat_titles = _scalar(
                conn,
                "SELECT COUNT(*) FROM ("
                "  SELECT lower(trim(COALESCE(hook_title, title))) t, COUNT(*) n "
                "  FROM shorts WHERE channel_id = ? AND youtube_id IS NOT NULL "
                "  GROUP BY t HAVING n > 1)",
                (cid,),
            )

            latest_subs = int(latest_ch.get("subscribers") or 0)
            prev_subs = int(prev7_ch.get("subscribers") or 0)
            minutes = float(latest_ch.get("estimated_minutes_watched") or 0)

            channels.append({
                "channel_id": cid,
                "slug": ch["slug"],
                "name": ch["name"],
                "subscribers": latest_subs,
                "net_subs_7d": (latest_subs - prev_subs) if prev_subs else None,
                "total_views": int(latest_ch.get("total_views") or 0),
                "watch_hours": round(minutes / 60.0, 1),
                "longform": {
                    "n": int(lf.get("n") or 0),
                    "total_views": int(lf.get("views") or 0),
                    "avg_views": round(float(lf.get("avg_views") or 0), 1),
                    "retention_pct": round(float(retention), 1) if retention else None,
                },
                "shorts": {
                    "n": int(sh.get("n") or 0),
                    "total_views": int(sh.get("views") or 0),
                    "avg_views": round(float(sh.get("avg_views") or 0), 1),
                    "reach7d_avg": round(float(reach7), 1) if reach7 else None,
                },
                "repeated_titles": int(repeat_titles or 0),
            })

    return {
        "experiment": EXPERIMENT_ID,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "channels": channels,
    }


def store_baseline(db) -> dict:
    """Sella experiment_started_at (si falta) y experiment_baseline."""
    baseline = compute_baseline(db)
    started = db.get_system_state(STARTED_KEY)
    if not started:
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        db.set_system_state(STARTED_KEY, started)
        logger.info("experiment_started_at = %s", started)
    db.set_system_state(BASELINE_KEY, json.dumps(baseline, ensure_ascii=False))
    logger.info("Baseline sellado para %d canales", len(baseline["channels"]))
    return baseline


def _checkpoint_due_at(start_iso: str, days: int) -> str:
    """Fecha de vencimiento = start (fecha) + `days` días a las 09:00 UTC."""
    start_date = start_iso[:10]
    base = datetime.strptime(start_date, "%Y-%m-%d").replace(
        hour=CHECKPOINT_HOUR_UTC, minute=0, second=0
    )
    return (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _checkpoint_title(days: int) -> str:
    return f"Checkpoint experimento alcance — T+{days}"


def schedule_checkpoints(db, start_iso: str) -> list[dict]:
    """Crea/actualiza los 3 recordatorios de checkpoint (idempotente).

    - Si ya existe un recordatorio pendiente del mismo checkpoint, actualiza su
      ``due_at`` (permite reprogramar cambiando ``--start``).
    - Si no existe, lo crea.
    """
    # entity_type/entity_id propios del experimento: `scheduled_reminders` tiene
    # un índice único (entity_type, entity_id) y los 3 checkpoints deben coexistir.
    # entity_id = días del checkpoint (7/21/45) garantiza unicidad.
    scheduled: list[dict] = []
    with db._connect() as conn:
        existing_rows = conn.execute(
            "SELECT id, title, due_at, status, metadata_json "
            "FROM scheduled_reminders "
            "WHERE alert_type = 'experiment_checkpoint' AND status = 'pending'"
        ).fetchall()
    existing = {r["title"]: dict(r) for r in existing_rows}

    for days in CHECKPOINT_DAYS:
        title = _checkpoint_title(days)
        due_at = _checkpoint_due_at(start_iso, days)
        message = CHECKPOINT_BRIEFS[days]
        meta = {
            "experiment": EXPERIMENT_ID,
            "checkpoint_days": days,
            "start": start_iso,
            "review": "specs/experimento-recuperacion-alcance.md",
            # El loop de reminders lee esto para emitir la alerta como crítica.
            "severity": CHECKPOINT_SEVERITY,
        }

        cur = existing.get(title)
        if cur:
            with db._connect() as conn:
                conn.execute(
                    "UPDATE scheduled_reminders SET due_at = ?, message = ?, "
                    "metadata_json = ?, entity_type = 'experiment', entity_id = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (due_at, message, json.dumps(meta, ensure_ascii=False),
                     days, cur["id"]),
                )
                conn.commit()
            reminder_id = cur["id"]
            logger.info("Checkpoint T+%d reprogramado → %s (id=%s)", days, due_at, reminder_id)
        else:
            reminder_id = db.create_scheduled_reminder(
                title=title,
                message=message,
                due_at=due_at,
                entity_id=days,
                entity_type="experiment",
                alert_type="experiment_checkpoint",
                metadata=meta,
            )
            if reminder_id is None:
                logger.error(
                    "Checkpoint T+%d NO se pudo programar (conflicto de entidad). "
                    "Revisar scheduled_reminders.", days,
                )
            else:
                logger.info("Checkpoint T+%d programado → %s (id=%s)", days, due_at, reminder_id)

        scheduled.append({
            "checkpoint_days": days,
            "title": title,
            "due_at": due_at,
            "reminder_id": reminder_id,
        })

    db.set_system_state(
        CHECKPOINTS_KEY,
        json.dumps({"start": start_iso, "reminders": scheduled}, ensure_ascii=False),
    )
    return scheduled


def print_status(db) -> None:
    started = db.get_system_state(STARTED_KEY)
    raw = db.get_system_state(BASELINE_KEY)
    print("Experimento:", EXPERIMENT_ID)
    print("Inicio:", started or "(sin sellar)")
    if raw:
        try:
            baseline = json.loads(raw)
        except (TypeError, ValueError):
            baseline = {}
        print(f"Baseline capturado: {baseline.get('captured_at')}")
        for ch in baseline.get("channels", []):
            lf, sh = ch.get("longform", {}), ch.get("shorts", {})
            print(
                f"  {ch['slug']:<7} subs={ch['subscribers']:<5} net7d={ch.get('net_subs_7d')} "
                f"lf_avg={lf.get('avg_views')} ret={lf.get('retention_pct')} "
                f"sh_avg={sh.get('avg_views')} reach7d={sh.get('reach7d_avg')} "
                f"rep_titles={ch.get('repeated_titles')}"
            )
    else:
        print("Baseline: (sin sellar)")
    print("Checkpoints programados:")
    for r in db.list_scheduled_reminders(status=None, limit=20):
        if r.get("alert_type") == "experiment_checkpoint":
            print(f"  [{r.get('status')}] {r.get('title')} → {r.get('due_at')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("baseline", help="Sella baseline + experiment_started_at")

    p_sched = sub.add_parser("schedule", help="Programa/actualiza los checkpoints")
    p_sched.add_argument("--start", default=None, help="YYYY-MM-DD (default: hoy UTC)")

    sub.add_parser("status", help="Muestra baseline y checkpoints")

    args = parser.parse_args(argv)

    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()

    if args.cmd == "baseline":
        store_baseline(db)
        return 0
    if args.cmd == "schedule":
        start = args.start
        if not start:
            start = db.get_system_state(STARTED_KEY) or datetime.now(
                timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S")
        schedule_checkpoints(db, start)
        return 0
    if args.cmd == "status":
        print_status(db)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
