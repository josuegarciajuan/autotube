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
exec >> "$LOG" 2>&1

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

# ── 1. Regenerar diagnóstico (info in_db fresca) ──────────────────
echo "── Diagnóstico ──"
python3 scripts/diagnose_all_channels.py
DIAG_RC=$?
if [ $DIAG_RC -ne 0 ]; then
    echo "❌ Diagnóstico falló (rc=$DIAG_RC) — abortando"
    exit 1
fi

# ── 2. Limpieza de duplicados (auto-confirm, cap 30/canal) ─────────
echo "── Limpieza ──"
python3 scripts/cleanup_yt_duplicates.py --execute --yes --max-delete 30
CLEAN_RC=$?
if [ $CLEAN_RC -ne 0 ]; then
    echo "❌ Limpieza falló (rc=$CLEAN_RC)"
    exit 1
fi

echo "=== Limpieza programada terminada: $(date) ==="
