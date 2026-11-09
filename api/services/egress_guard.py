"""Guard runtime de egress: bloquea conexiones directas a Google/YouTube.

Defensa en profundidad para procesos que trabajan con un canal gestionado
(principalmente ``full_pipeline_worker``). Si el código (o una librería) intenta
resolver un dominio de Google/YouTube sin pasar por el gateway, se lanza
``EgressBlockedError`` y se registra un crítico.

No sustituye al router único ni a la auditoría estática: es el cinturón que
evita fugas por rutas nuevas/olvidadas.
"""
from __future__ import annotations

import contextvars
import logging
import socket

logger = logging.getLogger("autotube.egress_guard")

_slug: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "egress_channel_slug", default=None
)
_installed = False

_DENY_DOMAINS = (
    "googleapis.com",
    "youtube.com",
    "youtu.be",
    "ytimg.com",
    "googlevideo.com",
    "google.com",
    "googleusercontent.com",
    "gstatic.com",
    "googlesyndication.com",
    "google-analytics.com",
    "googletagmanager.com",
)


class EgressBlockedError(RuntimeError):
    """Intento de conexión directa a Google/YouTube desde un canal gestionado."""


def current_channel() -> str | None:
    return _slug.get()


def set_channel(slug: str | None) -> None:
    _slug.set(slug)


def _is_managed(slug: str) -> bool:
    try:
        from api.services.egress_client import is_egress_managed
        return is_egress_managed(slug)
    except Exception:  # noqa: BLE001
        return False


def _is_denied(host: str) -> bool:
    h = (host or "").lower().strip(".")
    return any(h == d or h.endswith("." + d) for d in _DENY_DOMAINS)


def install() -> None:
    """Instala el hook de resolución de DNS (idempotente)."""
    global _installed
    if _installed:
        return
    original = socket.getaddrinfo

    def _guarded(host, port, *args, **kwargs):
        slug = _slug.get()
        if slug and _is_managed(slug) and _is_denied(str(host)):
            logger.critical(
                "[%s] EGRESS LOCAL BLOQUEADO: intento de conexión directa a %s",
                slug, host,
            )
            try:
                from api.services.lifecycle_monitor import create_alert
                from database.db_extended import ExtendedDatabase
                create_alert(
                    ExtendedDatabase(), entity_type="system",
                    entity_id=abs(__import__("zlib").crc32(slug.encode())) % (10 ** 6),
                    channel_id=None, alert_type="local_egress_blocked",
                    severity="critical",
                    title=f"Egress local bloqueado en {slug}",
                    message=f"Intento de conexión directa a {host} desde un canal gestionado.",
                    metadata={"slug": slug, "host": str(host)},
                )
            except Exception:  # noqa: BLE001
                pass
            raise EgressBlockedError(
                f"conexión directa a {host} bloqueada para el canal gestionado {slug}"
            )
        return original(host, port, *args, **kwargs)

    socket.getaddrinfo = _guarded
    _installed = True


def install_for_channel(slug: str | None) -> None:
    """Fija el canal y activa el guard si es gestionado."""
    set_channel(slug)
    if slug and _is_managed(slug):
        install()
