"""Monitor de liveness de la IP de los gateways egress por canal.

Comprueba periódicamente que cada canal gestionado (flag durable en DB **o**
registro secreto) sigue saliendo por su IP esperada. Si el gateway no responde,
la IP no coincide o el perfil está incompleto, marca ``egress_down_<slug>=1`` y
crea una alerta crítica ``egress_ip_down``.

La capa de delegación (``egress_client``) consulta ese flag antes de cualquier
operación → FAIL-CLOSED: no intenta nada con la IP muerta (ni cae a la del
server). Al recuperarse, la alerta se resuelve y se reanuda solo.

Regla estricta nov 2026: un perfil sin ``expected_ip`` NO se considera sano.
Antes, si faltaba ``expected_ip``, cualquier IP no vacía pasaba el check.
"""
from __future__ import annotations

import logging
import zlib
from datetime import datetime, timezone

logger = logging.getLogger("autotube.egress_monitor")

EGRESS_DOWN_PREFIX = "egress_down_"
EGRESS_ALERT = "egress_ip_down"
CHECK_INTERVAL_SEC = 60  # cada 60 s (cinturón adicional a la guardia por-op del agente)


def _entity_id(slug: str) -> int:
    try:
        return abs(zlib.crc32(str(slug).encode("utf-8"))) % (10 ** 6)
    except Exception:
        return 0


def _load_agents() -> dict:
    """Compatibilidad: delega en el registro durable."""
    from api.services.egress_registry import _load_agents as _la
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


def _raise_alert(db, slug: str, ip: str, expected: str, error: str = "") -> None:
    try:
        from api.services.lifecycle_monitor import create_alert
        detail = f" Detalle: {error}" if error else ""
        create_alert(
            db, entity_type="system", entity_id=_entity_id(slug), channel_id=None,
            alert_type=EGRESS_ALERT, severity="critical",
            title=f"Gateway/IP del canal {slug} caído o mal configurado",
            message=(
                f"El gateway egress de {slug} no sale por su IP esperada. "
                f"IP detectada: {ip or 'desconocida'}; esperada: {expected or 'no configurada'}."
                f"{detail} Las operaciones de este canal están BLOQUEADAS "
                f"(fail-closed) hasta corregirlo. Verificar: panel → Verificar egress, "
                f"o revisar config/egress_agents.json / la DB del canal."
            ),
            metadata={
                "slug": slug, "ip": ip, "expected": expected,
                "error": error[:400],
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] egress alert create failed: %s", slug, exc)


def _known_channel_slugs(db) -> set[str]:
    """Slugs de canales registrados en la DB (para descartar egress obsoletos)."""
    try:
        with db._connect() as conn:
            rows = conn.execute("SELECT slug FROM channels").fetchall()
            return {r["slug"] for r in rows if r and r["slug"]}
    except Exception as exc:  # noqa: BLE001
        logger.debug("egress monitor: no se pudo leer channels (%s) — se comprueban todos", exc)
        return set()


def _update_channel_status(db, slug: str, status: str, ip: str = "") -> None:
    """Persiste el estado del gateway en ``channels`` (best-effort)."""
    try:
        setter = getattr(db, "set_channel_egress_status", None)
        if callable(setter):
            setter(slug, status, last_ip=ip or None)
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        with db._connect() as conn:
            conn.execute(
                "UPDATE channels SET egress_status = ?, "
                "egress_last_ip = COALESCE(?, egress_last_ip), "
                "egress_last_verified_at = CASE WHEN ? = 'up' "
                "THEN CURRENT_TIMESTAMP ELSE egress_last_verified_at END, "
                "updated_at = CURRENT_TIMESTAMP WHERE slug = ?",
                (status, ip or None, status, slug),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[%s] no se pudo persistir egress_status: %s", slug, exc)


def check_all_egress(db=None) -> dict:
    """Comprueba la salud de la IP de todos los canales gestionados."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    from api.services.egress_client import EgressAgent
    from api.services.egress_registry import (
        EgressConfigurationError,
        get_managed_slugs,
        get_profile,
    )

    managed = sorted(get_managed_slugs(db))
    known = _known_channel_slugs(db)
    results: dict[str, dict] = {}

    for slug in managed:
        # Egress huérfano (canal retirado/renombrado): no alertar. Resolvemos
        # cualquier alerta previa para que no quede colgada en el panel.
        if known and slug not in known:
            logger.debug("[%s] egress sin canal en DB — omitido (config obsoleta)", slug)
            _resolve_alert(db, slug)
            continue

        try:
            row = None
            getter = getattr(db, "get_channel_by_slug", None)
            if callable(getter):
                row = getter(slug)
            profile = get_profile(slug, db_row=row)
        except EgressConfigurationError as exc:
            key = EGRESS_DOWN_PREFIX + slug
            db.set_system_state(key, "1")
            _raise_alert(db, slug, "", "", str(exc))
            _update_channel_status(db, slug, "error")
            results[slug] = {"ok": False, "ip": "", "expected": "", "error": str(exc)}
            logger.warning("[%s] perfil incompleto — fail-closed: %s", slug, exc)
            continue

        if profile is None:
            continue

        expected = profile.expected_ip
        ip = ""
        ok = False
        error = ""
        try:
            client = EgressAgent(slug, profile.url, profile.token)
            hz = client.healthz()
            ip = str(hz.get("egress_ip") or "")
            agent_expected = str(hz.get("expected_ip") or expected).strip()
            # La IP esperada del agente y la del registro deben coincidir.
            expected = agent_expected or expected
            ok = (
                bool(hz.get("ok"))
                and bool(hz.get("egress_ok"))
                and bool(ip)
                and bool(expected)
                and ip == expected
                and expected == profile.expected_ip
            )
            if not ok:
                error = (
                    f"healthz ok={hz.get('ok')} egress_ok={hz.get('egress_ok')} "
                    f"ip={ip or '?'} expected={expected or '?'} "
                    f"registry_expected={profile.expected_ip}"
                )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)

        key = EGRESS_DOWN_PREFIX + slug
        if ok:
            db.set_system_state(key, "0")
            _resolve_alert(db, slug)
            _update_channel_status(db, slug, "up", ip)
            logger.info("[%s] egress IP OK (%s)", slug, ip)
        else:
            db.set_system_state(key, "1")
            _raise_alert(db, slug, ip, expected, error)
            _update_channel_status(db, slug, "down", ip)
            logger.warning("[%s] egress DOWN (ip=%r expected=%r) — fail-closed",
                           slug, ip, expected)
        results[slug] = {"ok": ok, "ip": ip, "expected": expected}

    return results


def is_egress_down(db, slug: str) -> bool:
    try:
        return db.get_system_state(EGRESS_DOWN_PREFIX + slug) == "1"
    except Exception:
        return False


def egress_monitor_loop():
    """Loop síncrono (correr en asyncio.to_thread): comprueba cada 60 s."""
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
