"""Gobernador de la fábrica continua (Fase 4).

Pausa la GENERACIÓN (nunca la planificación ni la publicación) cuando los
recursos del sistema están comprometidos:

  1. **Disco libre** por debajo de ``MIN_FREE_DISK_MB`` → pausar la fábrica.
     La cola sin límite acumula vídeos renderizados; sin tope de disco el
     sistema se llena y mata el propio render en curso.
  2. **Créditos LLM** agotados/error → pausar la fábrica (red de seguridad,
     no tope presupuestario: el kill-switch evita generar sin crédito y
     acumular errores en cadena).

   Sobre los créditos LLM: el pool de modelos tiene failover real
   (p.ej. ``deepseek:…→openai:…``), de modo que basta con que UN proveedor
   tenga crédito para seguir generando. La fábrica se pausa SOLO cuando
   TODOS los proveedores registrados están no-usables, no cuando cae un
   mero fallback que el sistema no está usando.

Ambas son compuertas FAIL-OPEN (ante error de medición se permite generar)
y SOLO afectan al dispatch automático (fábrica), no a las acciones manuales
del panel.
"""

from __future__ import annotations

import logging
import shutil

logger = logging.getLogger("autotube.factory_governor")


# Estados que significan "este proveedor no puede usarse para generar".
# "low" (saldo bajo pero > 0) NO bloquea: el pool sigue pudiendo generar
# con ese proveedor y la alerta es solo informativa.
_BLOCKING_CREDIT_STATUSES = {"exhausted", "error"}


def _default_min_free_disk_mb() -> int:
    try:
        from config.settings import MIN_FREE_DISK_MB
        return int(MIN_FREE_DISK_MB or 5000)
    except Exception:
        return 5000


def free_disk_mb() -> int:
    """Espacio libre en el filesystem del proyecto (MB)."""
    try:
        from config.settings import PROJECT_ROOT
        usage = shutil.disk_usage(str(PROJECT_ROOT))
        return int(usage.free / (1024 * 1024))
    except Exception:
        return 0


def disk_ok(min_free_mb: int | None = None) -> bool:
    """True si hay disco suficiente. Fail-open ante errores de medición."""
    try:
        if min_free_mb is None:
            min_free_mb = _default_min_free_disk_mb()
        free = free_disk_mb()
        if free <= 0:
            return True  # no se pudo medir → no bloquear
        ok = free >= min_free_mb
        if not ok:
            logger.warning(
                "Factory governor: disco bajo (%d MB < %d MB) — pausando generación",
                free, min_free_mb,
            )
        return ok
    except Exception:
        return True


def credits_ok(db=None) -> bool:
    """True si hay al menos un proveedor LLM usable. Fail-open sin registro.

    El pool de modelos tiene failover real (p.ej. ``deepseek:…→openai:…``),
    por lo que basta con que UN proveedor tenga crédito para seguir generando.
    Si el fallback (p.ej. OpenAI) se agota pero el primario (DeepSeek) sigue
    sano, la fábrica NO debe pausarse: se sigue generando con el primario.

    Se pausa SOLO cuando TODOS los proveedores registrados están no-usables
    (status ``exhausted`` o ``error``). Fail-open: sin registros o ante error
    de medición se permite generar.
    """
    try:
        if db is None:
            from database.db_extended import ExtendedDatabase
            db = ExtendedDatabase()
        from api.services.llm_credit_checker import get_llm_credit_status
        status = get_llm_credit_status(db) or {}
        # Solo proveedores con registro de estado presente (ausente → ignorado).
        providers = {
            name: status[name]
            for name in ("deepseek", "openai")
            if status.get(name)
        }
        if not providers:
            return True  # fail-open: sin registros no bloqueamos
        blocked = {
            name: rec.get("status")
            for name, rec in providers.items()
            if rec.get("status") in _BLOCKING_CREDIT_STATUSES
        }
        if len(blocked) >= len(providers):
            logger.warning(
                "Factory governor: TODOS los proveedores LLM no-usables (%s) — pausando generación",
                ", ".join(f"{k}={v}" for k, v in blocked.items()),
            )
            return False
        return True
    except Exception:
        return True


def factory_ok(db=None) -> bool:
    """Compuerta combinada de la fábrica: disco + créditos LLM."""
    return disk_ok() and credits_ok(db)
