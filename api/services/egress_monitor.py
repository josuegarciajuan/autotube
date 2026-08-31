"""Monitor de liveness de la IP residencial de los agentes egress.

Comprueba periódicamente que cada canal gestionado sigue saliendo por su IP
residencial esperada (``/egress-check`` del agente). Si el agente no responde o
devuelve otra IP (proxy caducado/caído), marca ``egress_down_<slug>=1`` y crea
una alerta crítica ``egress_ip_down``.

La capa de delegación (``egress_client``) consulta ese flag antes de cualquier
operación → FAIL-CLOSED: no intenta nada con la IP muerta (ni cae a la del
server). Al recuperarse, la alerta se resuelve y se reanuda solo.
"""
from __future__ import annotations

import logging
import zlib
from datetime import datetime, timezone

logger = logging.getLogger("autotube.egress_monitor")

EGRESS_DOWN_PREFIX = "egress_down_"
EGRESS_ALERT = "egress_ip_down"
CHECK_INTERVAL_SEC = 300  # cada 5 min


def _entity_id(slug: str) -> int:
    try:
        return abs(zlib.crc32(str(slug).encode("utf-8"))) % (10 ** 6)
    except Exception:
        return 0


def _load_agents() -> dict:
    from api.services.egress_client import _load_agents as _la
    return _la()


def _resolve_alert(db, slug: str) -> None:
    try:
        with db._connect() as conn:
            conn.execute(
                """UPDATE pipeline_alerts SET resolved = 1, resolved_at = datetime('now')
                   WHERE entity_type = 'system' AND entity_id = ?
                     AND alert_type = ? AND resolved = 0""",
                (_entity_id(slug), EGRESS_ALERT),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[%s] resolve egress alert failed: %s", slug, exc)


def _raise_alert(db, slug: str, ip: str, expected: str) -> None:
    try:
        from api.services.lifecycle_monitor import create_alert
        create_alert(
            db, entity_type="system", entity_id=_entity_id(slug), channel_id=None,
            alert_type=EGRESS_ALERT, severity="critical",
            title=f"IP residencial del canal {slug} caída/inactiva",
            message=(
                f"El agente egress de {slug} no sale por su IP residencial esperada. "
                f"IP detectada: {ip or 'desconocida'}; esperada: {expected or 'no configurada'}. "
                f"Posible caducidad del proxy (Geonix). Las operaciones de este canal están "
                f"BLOQUEADAS (fail-closed) hasta que se renueve/active la IP. "
                f"Verificar: panel → Verificar egress, o renovar en el proveedor."
            ),
            metadata={"slug": slug, "ip": ip, "expected": expected, "checked_at": datetime.now(timezone.utc).isoformat()},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] egress alert create failed: %s", slug, exc)


def check_all_egress(db=None) -> dict:
    """Comprueba la salud de la IP residencial de todos los canales gestionados."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    from api.services.egress_delegation import egress_client_for
    agents = _load_agents()
    results = {}
    for slug, cfg in agents.items():
        expected = str(cfg.get("expected_ip", "") or "")
        client = egress_client_for(slug)
        ok = False
        ip = ""
        if client is not None:
            try:
                data = client.egress_check()
                ip = str(data.get("result", {}).get("ip", ""))
                ok = bool(ip) and (not expected or ip == expected)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] egress-check unreachable: %s", slug, exc)
                ok = False
        key = EGRESS_DOWN_PREFIX + slug
        if ok:
            db.set_system_state(key, "0")
            _resolve_alert(db, slug)
            logger.info("[%s] egress IP OK (%s)", slug, ip)
        else:
            db.set_system_state(key, "1")
            _raise_alert(db, slug, ip, expected)
            logger.warning("[%s] egress IP DOWN (ip=%r expected=%r) — fail-closed", slug, ip, expected)
        results[slug] = {"ok": ok, "ip": ip}
    return results


def is_egress_down(db, slug: str) -> bool:
    try:
        return db.get_system_state(EGRESS_DOWN_PREFIX + slug) == "1"
    except Exception:
        return False


def egress_monitor_loop():
    """Loop síncrono (correr en asyncio.to_thread): comprueba la salud cada 5 min."""
    import time as _time
    _time.sleep(120)  # deja estabilizar a la API
    while True:
        try:
            from api.services.lifecycle_monitor import touch_task_heartbeat as _tth
            _tth("egress_monitor")
            from database.db_extended import ExtendedDatabase
            check_all_egress(ExtendedDatabase())
        except Exception as exc:  # noqa: BLE001
            logger.warning("egress monitor error: %s", exc)
        _time.sleep(CHECK_INTERVAL_SEC)
