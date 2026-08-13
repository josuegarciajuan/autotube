#!/usr/bin/env python3
"""Informe diario de salud + alertas — Autotube.

Chequea métricas clave de la recuperación y crea alertas (pipeline_alerts)
visibles en el dashboard para problemas críticos.

Se ejecuta vía systemd timer: autotube-health-report.timer (diario ~08:00).

Escribe: logs/health_report_YYYYMMDD.log
Alertas (dashboard) para:
  - quota_warning:   algún proyecto GCP > 90% de la cuota diaria
  - upload_stalled:  0 subidas hoy y backlog >= 5 en awaiting_upload
  - upload_failures: >= 5 upload_only fallidos en las últimas 24h
  - shorts_deleted:  >= 10 shorts eliminados por YT en las últimas 24h
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, date

# ── Añadir raíz del proyecto al path (scripts/ se ejecutan desde la raíz) ──
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _line(msg: str = "") -> None:
    print(msg)


def main() -> int:
    # ── Setup ──────────────────────────────────────────────────
    from database.db import init_db
    from database.db_extended import migrate_v2, ExtendedDatabase

    init_db()
    migrate_v2()
    db = ExtendedDatabase()

    import os
    os.makedirs("logs", exist_ok=True)
    log_path = f"logs/health_report_{date.today().isoformat()}.log"
    fh = open(log_path, "w")
    _orig_stdout = sys.stdout
    sys.stdout = _Tee(_orig_stdout, fh)  # consola + fichero

    _line("=" * 70)
    _line(f"INFORME DE SALUD AUTOTUBE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    _line("=" * 70)

    alerts_created = 0

    # ── 1. Subidas de hoy ──────────────────────────────────────
    try:
        today_uploads = db.count_today_uploads()
        _line(f"\n[1] Subidas hoy (long-form + shorts): {today_uploads}")
    except Exception as e:
        today_uploads = -1
        _line(f"\n[1] ERROR contando subidas: {e}")

    # ── 2. Backlog awaiting_upload ─────────────────────────────
    try:
        with db._connect() as conn:
            backlog = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE status='awaiting_upload'"
            ).fetchone()[0]
            generating = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE status IN ('generating','uploading','reassembling')"
            ).fetchone()[0]
            ready = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE status='ready'"
            ).fetchone()[0]
        _line(f"[2] Backlog: awaiting_upload={backlog}, generating/uploading={generating}, ready={ready}")
    except Exception as e:
        backlog = 0
        _line(f"[2] ERROR: {e}")

    # ── 3. Uploads fallidos en 24h ─────────────────────────────
    try:
        with db._connect() as conn:
            failed_uploads = conn.execute(
                """SELECT COUNT(*) FROM generation_jobs
                   WHERE action='upload_only' AND status='failed'
                     AND created_at >= datetime('now','-24 hours')"""
            ).fetchone()[0]
            failed_shorts = conn.execute(
                """SELECT COUNT(*) FROM generation_jobs
                   WHERE action IN ('generate_native_short','generate_clip_short')
                     AND status='failed'
                     AND created_at >= datetime('now','-24 hours')"""
            ).fetchone()[0]
        _line(f"[3] Fallos 24h: uploads={failed_uploads}, shorts={failed_shorts}")
    except Exception as e:
        failed_uploads = 0
        _line(f"[3] ERROR: {e}")

    # ── 4. Cuota por proyecto GCP ──────────────────────────────
    quota_warn_projects = []
    try:
        import os
        # Límite real configurable (los canales consumen 30-55k/día → no es 10k)
        _limit = int(os.getenv("YT_DAILY_QUOTA_LIMIT", "100000"))
        from api.services.quota_tracker import get_daily_usage, get_channel_project
        usage = get_daily_usage(db=db)
        by_channel = usage.get("by_channel", {})
        project_units: dict[str, int] = {}
        for slug, units in by_channel.items():
            proj = get_channel_project(slug)
            project_units[proj] = project_units.get(proj, 0) + units
        _line(f"\n[4] Cuota de hoy (límite por proyecto = {_limit} ud):")
        for proj, units in sorted(project_units.items()):
            pct = units / max(_limit, 1) * 100
            _line(f"    {proj}: {units} ud ({pct:.0f}%)")
            if pct >= 90:
                quota_warn_projects.append(f"{proj} ({pct:.0f}%)")
    except Exception as e:
        _line(f"\n[4] ERROR cuota: {e}")

    # ── 5. Shorts eliminados por YT (24h) ──────────────────────
    try:
        with db._connect() as conn:
            shorts_deleted = conn.execute(
                """SELECT COUNT(*) FROM generation_jobs
                   WHERE action='generate_native_short'
                     AND error_msg LIKE '%no aparece en YouTube%'
                     AND created_at >= datetime('now','-24 hours')"""
            ).fetchone()[0]
        _line(f"\n[5] Shorts eliminados por YT (24h): {shorts_deleted}")
    except Exception as e:
        shorts_deleted = 0
        _line(f"\n[5] ERROR: {e}")

    # ── 6. Crear alertas críticas ──────────────────────────────
    _line("\n" + "-" * 70)
    _line("ALERTAS")
    _line("-" * 70)
    try:
        from api.services.lifecycle_monitor import create_alert

        def _alert(alert_type, severity, title, message):
            nonlocal alerts_created
            aid = create_alert(
                db, entity_type="system", entity_id=0,
                alert_type=alert_type, severity=severity,
                title=title, message=message,
            )
            if aid:
                alerts_created += 1
                _line(f"  ⚠️  [{severity}] {title}: {message}")
            else:
                _line(f"  ✓ ok (sin alerta): {title}")

        # Quota warning por proyecto
        if quota_warn_projects:
            _alert("quota_warning", "critical",
                   "Cuota de YouTube al 90%+",
                   "Proyecto(s): " + ", ".join(quota_warn_projects))
        else:
            _line("  ✓ cuota por proyecto bajo control")

        # Upload stalled
        if today_uploads == 0 and backlog >= 5:
            _alert("upload_stalled", "critical",
                   "Subidas estancadas",
                   f"0 subidas hoy con {backlog} vídeos en awaiting_upload")
        elif backlog >= 15:
            _alert("upload_backlog", "warning",
                   "Backlog de subidas alto",
                   f"{backlog} vídeos esperando subir")
        else:
            _line("  ✓ subidas/backlog OK")

        # Fallos de upload
        if failed_uploads >= 5:
            _alert("upload_failures", "critical",
                   "Muchos fallos de subida",
                   f"{failed_uploads} upload_only fallidos en 24h")
        else:
            _line("  ✓ fallos de subida OK")

        # Shorts eliminados
        if shorts_deleted >= 10:
            _alert("shorts_deleted", "warning",
                   "Shorts eliminados por YouTube",
                   f"{shorts_deleted} shorts 'no aparece en YouTube' en 24h")
        else:
            _line("  ✓ shorts eliminados OK")

    except Exception as e:
        _line(f"  ERROR creando alertas: {e}")

    _line("\n" + "=" * 70)
    _line(f"Informe completado. Alertas nuevas: {alerts_created}")
    _line("=" * 70)

    sys.stdout = _orig_stdout
    fh.close()
    return 0


class _Tee:
    """Escribe a stdout y a un fichero a la vez."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)

    def flush(self):
        for st in self.streams:
            st.flush()


if __name__ == "__main__":
    sys.exit(main())
