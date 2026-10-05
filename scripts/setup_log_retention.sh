#!/usr/bin/env bash
# setup_log_retention.sh — instala la rotación de logs de Autotube.
#
# Qué hace (idempotente, seguro de re-ejecutar):
#   1. Copia deploy/logrotate/autotube        -> /etc/logrotate.d/autotube (0644 root:root)
#   2. Copia deploy/systemd/autotube-logrotate.{service,timer} -> /etc/systemd/system/
#   3. systemctl daemon-reload + enable --now autotube-logrotate.timer
#   4. Valida la config con `logrotate -d` (dry-run) y aborta si hay errores.
#
# No rota api.log ni logs/obs/** (los rota el propio proceso): ver la cabecera de
# deploy/logrotate/autotube.
#
# Uso:
#   sudo bash scripts/setup_log_retention.sh              # instala/actualiza
#   sudo bash scripts/setup_log_retention.sh --force-now  # además, rota ya una vez
#   bash scripts/setup_log_retention.sh --help
#
# Variables de entorno:
#   LOGROTATE_BIN   ruta al binario de logrotate (def. /usr/sbin/logrotate)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGROTATE_BIN="${LOGROTATE_BIN:-/usr/sbin/logrotate}"

CONFIG_SRC="${ROOT}/deploy/logrotate/autotube"
SERVICE_SRC="${ROOT}/deploy/systemd/autotube-logrotate.service"
TIMER_SRC="${ROOT}/deploy/systemd/autotube-logrotate.timer"

CONFIG_DST="/etc/logrotate.d/autotube"
UNIT_DIR="/etc/systemd/system"
STATE_FILE="/var/lib/logrotate/autotube.status"
STATE_DIR="$(dirname "${STATE_FILE}")"
TIMER_UNIT="autotube-logrotate.timer"
SERVICE_UNIT="autotube-logrotate.service"

FORCE_NOW=0

usage() {
    cat <<'EOF'
Uso: setup_log_retention.sh [--force-now]

  --force-now   Tras instalar, ejecuta una rotación real inmediata
                (logrotate --force) con el state file propio.
  -h, --help    Muestra esta ayuda.

Instala /etc/logrotate.d/autotube y autotube-logrotate.{service,timer}.
Requiere root y systemd. Es idempotente: se puede re-ejecutar.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --force-now) FORCE_NOW=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: opción desconocida: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

log()  { printf '==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- 0) Comprobaciones previas ------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "debe ejecutarse como root (sudo)."

# Localiza logrotate sin asumir la ruta.
if [ ! -x "${LOGROTATE_BIN}" ]; then
    LOGROTATE_BIN="$(command -v logrotate 2>/dev/null || true)"
fi
[ -n "${LOGROTATE_BIN}" ] && [ -x "${LOGROTATE_BIN}" ] || die \
    "no se encontró logrotate (esperado en /usr/sbin/logrotate). Instálalo con: apt-get install -y logrotate"

command -v systemctl >/dev/null 2>&1 || die "systemctl no está disponible; se requiere systemd."

[ -f "${CONFIG_SRC}" ]  || die "falta ${CONFIG_SRC}"
[ -f "${SERVICE_SRC}" ] || die "falta ${SERVICE_SRC}"
[ -f "${TIMER_SRC}" ]   || die "falta ${TIMER_SRC}"

log "logrotate: ${LOGROTATE_BIN}"

# --- 1) Instalar la configuración --------------------------------------------
install -d -m 0755 "${STATE_DIR}"
log "instalando ${CONFIG_DST}"
install -m 0644 -o root -g root "${CONFIG_SRC}" "${CONFIG_DST}"

# --- 2) Validar la config ANTES de habilitar el timer ------------------------
log "validando ${CONFIG_DST} (logrotate -d)"
if ! validate_out="$("${LOGROTATE_BIN}" -d "${CONFIG_DST}" 2>&1)"; then
    printf '%s\n' "${validate_out}" >&2
    die "la configuración ${CONFIG_DST} es inválida; NO se habilita el timer. Corrige deploy/logrotate/autotube y reintenta."
fi

# --- 3) Instalar unidades systemd --------------------------------------------
log "instalando unidades systemd en ${UNIT_DIR}"
install -m 0644 -o root -g root "${SERVICE_SRC}" "${UNIT_DIR}/${SERVICE_UNIT}"
install -m 0644 -o root -g root "${TIMER_SRC}"   "${UNIT_DIR}/${TIMER_UNIT}"

log "systemctl daemon-reload"
systemctl daemon-reload

log "systemctl enable --now ${TIMER_UNIT}"
systemctl enable --now "${TIMER_UNIT}"

# --- 4) Rotación inmediata opcional ------------------------------------------
if [ "${FORCE_NOW}" -eq 1 ]; then
    log "rotación forzada (logrotate --force)"
    "${LOGROTATE_BIN}" --force --state "${STATE_FILE}" "${CONFIG_DST}"
fi

log "hecho."
echo
echo "Verificación:"
echo "  systemctl status ${TIMER_UNIT} --no-pager"
echo "  systemctl list-timers ${TIMER_UNIT} --no-pager"
echo "  journalctl -u ${SERVICE_UNIT} -n 50 --no-pager"
echo "  /usr/sbin/logrotate -d ${CONFIG_DST}"
echo "  ls -lah /root/autotube/logs/ | grep -E '\\.(gz|1)\$'"
