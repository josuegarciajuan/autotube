#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
# Limpieza programada de duplicados de YouTube (unattended).
#
# Ejecuta: diagnóstico fresco + limpieza de duplicados con auto-confirm.
# Programado vía systemd timer (autotube-dup-cleanup.timer) a diario.
#
# Guards de seguridad (se cancela silenciosamente si se cumple alguno):
#   - scheduler pausado
#   - cuota de YouTube agotada
#   - subidas en curso (evita la carrera que borró el canónico recién subido)
# ─────────────────────────────────────────────────────────────────────
set -uo pipefail
cd /root/autotube

mkdir -p logs
LOG="logs/dup_cleanup_$(date +%Y%m%d_%H%M%S).log"
# Log a fichero Y al journal de systemd (stderr), para que `systemctl status`
# muestre la causa real de un fallo. Antes solo se redirigía al fichero, que
# además logrotate truncan cada hora, dejando el journal vacío.
exec > >(tee -a "$LOG") 2>&1

echo "=== Limpieza programada de duplicados: $(date) ==="

# ── Guard pre-vuelo ────────────────────────────────────────────────
GUARD=$(python3 - <<'PY' 2>/dev/null
from database.db_extended import ExtendedDatabase
db = ExtendedDatabase()
if db.get_system_state("scheduler_paused") == "true":
    print("scheduler_paused")
elif db.is_quota_exhausted():
    print("quota_exhausted")
elif db.count_active_upload_jobs() > 0:
    print("upload_active")
else:
    print("ok")
PY
)
GUARD="${GUARD:-ok}"

if [ "$GUARD" != "ok" ]; then
    echo "⏸️  Guard activo ($GUARD) — se cancela la limpieza. Se reintentará en la próxima ejecución."
    exit 0
fi

# ── Helper de alertas (visibilidad sin vigilancia manual) ──────────
# Un fallo real deja una alerta crítica `dup_cleanup_failed` en el panel; al
# terminar bien se resuelve sola. Así no hace falta mirar systemd a mano.
alert_fail() {
    REASON="$1" python3 - <<'PY' 2>/dev/null || true
import os
try:
    from api.services.lifecycle_monitor import emit_alert
    emit_alert(
        entity_type="system",
        alert_type="dup_cleanup_failed",
        severity="critical",
        title="Limpieza de duplicados falló",
        message=(
            "La limpieza programada de duplicados de YouTube falló.\n\n"
            "Motivo: " + os.environ.get("REASON", "desconocido") + "\n\n"
            "Revisar: journalctl -u autotube-dup-cleanup.service"
        ),
    )
except Exception:
    pass
PY
}

alert_ok() {
    python3 - <<'PY' 2>/dev/null || true
try:
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    with db._connect() as conn:
        conn.execute(
            "UPDATE pipeline_alerts SET resolved=1, resolved_at=datetime('now'), "
            "acknowledged=1 WHERE alert_type='dup_cleanup_failed' AND resolved=0"
        )
        conn.commit()
except Exception:
    pass
PY
}

# ── 1. Regenerar diagnóstico (info in_db fresca) ──────────────────
# `diagnose_all_channels.py` exige un selector explícito (--channel-id/--slug/
# --project/--all). Sin él argparse aborta con rc=2 y el script entero fallaba
# antes de limpiar (regresión desde ~sep-2026). `--all` requiere `--yes`.
#
# ⚠️  Semántica de salida de diagnose_all_channels.py:
#   rc=0 → sin anomalías
#   rc=1 → SÍ hay anomalías (informativo: duplicados, orphaned_on_yt,
#          missing_thumbnail...). NO es un fallo: la limpieza DEBE continuar
#          (encontrar duplicados es justo el propósito del servicio).
#   rc>=2 → error real (CLI cambiada, excepción, DB inaccesible) → abortar.
echo "── Diagnóstico ──"
python3 scripts/diagnose_all_channels.py --all --yes
DIAG_RC=$?
if [ $DIAG_RC -gt 1 ]; then
    echo "❌ Diagnóstico falló (rc=$DIAG_RC) — abortando"
    alert_fail "diagnóstico rc=$DIAG_RC (¿cambió la CLI de diagnose_all_channels.py?)"
    exit 1
fi
if [ $DIAG_RC -eq 1 ]; then
    echo "ℹ️  Diagnóstico: anomalías detectadas (rc=1) — se procede con la limpieza."
fi

# ── 2. Limpieza de duplicados (auto-confirm, cap 30/canal) ─────────
# Exit != 0 solo por fallos REALES; la cuota agotada es transitoria y no
# marca el servicio como fallido (se reintenta en la próxima ejecución).
echo "── Limpieza ──"
python3 scripts/cleanup_yt_duplicates.py --execute --yes --max-delete 30
CLEAN_RC=$?
if [ $CLEAN_RC -ne 0 ]; then
    echo "❌ Limpieza falló (rc=$CLEAN_RC)"
    alert_fail "limpieza rc=$CLEAN_RC (fallos reales de borrado — revisar log)"
    exit 1
fi

alert_ok
echo "=== Limpieza programada terminada: $(date) ==="
