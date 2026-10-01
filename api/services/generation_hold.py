"""Pausa de generación (hold) — protección a través de reinicios.

La pausa *generation-only* de autotube se representa con **dos filas centinela**
en ``generation_jobs`` (``phase='hold'``) que ocupan los guards de concurrencia:

  * ``action='generate_only'``         → bloquea long-form
    (``count_active_longform_jobs`` lo cuenta como job activo).
  * ``action='generate_native_short'`` → bloquea shorts
    (``count_active_shorts_jobs`` lo cuenta como job activo).

Su vigencia se controla por ``last_heartbeat_at`` (TTL en días). Un refrescador
externo puede empujar ese latido, pero **la intención del operador** se persiste
en ``system_state['generation_hold']`` para que el hold sobreviva a reinicios sin
depender de un único mecanismo.

Reglas duras:
  * Los centinelas **NUNCA** son huérfanos: los recuperadores de arranque y el
    reaper deben saltarlos (ver ``is_hold_row`` y los parches en
    ``generation_service`` / ``db_extended``).
  * Crear o refrescar el hold **no enciende nada**: solo mantiene la pausa.
  * Liberar el hold exige una confirmación explícita.
  * Fail-open en lectura: si la tabla no existe, se degrada sin romper el
    arranque (tests, esquemas antiguos).

Contrato padre: ``specs/experimento-recuperacion-alcance.md`` (ámbito de pausa).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("autotube.generation_hold")

# ── Constantes del hold ─────────────────────────────────────────
HOLD_PHASE = "hold"
HOLD_ACTIONS = ("generate_only", "generate_native_short")
INTENT_KEY = "generation_hold"
STATE_KEY = "generation_hold_state"
DEFAULT_TTL_DAYS = 2
RELEASE_CONFIRM = "REANUDAR_GENERACION"

_TRUTHY = {"1", "true", "yes", "on"}
REQUIRED_ACTIONS = set(HOLD_ACTIONS)


# ── Utilidades internas (solo dependen de `db._connect`) ────────

def _connect(db):
    return db._connect()


def _get_state(db, key: str) -> str:
    try:
        with _connect(db) as conn:
            row = conn.execute(
                "SELECT value FROM system_state WHERE key = ?", (key,)
            ).fetchone()
        return str(row[0]) if row and row[0] is not None else ""
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("generation_hold: read state %s failed: %s", key, exc)
        return ""


def _set_state(db, key: str, value: str) -> None:
    with _connect(db) as conn:
        conn.execute(
            "INSERT INTO system_state(key, value, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=datetime('now')",
            (key, value),
        )
        conn.commit()


def hold_intent(db) -> bool:
    """True si el operador declaró la intención de mantener la generación pausada."""
    return _get_state(db, INTENT_KEY).strip().lower() in _TRUTHY


def set_hold_intent(db, enabled: bool, reason: str = "") -> None:
    """Persiste la intención del operador (fuente de verdad del hold)."""
    _set_state(db, INTENT_KEY, "true" if enabled else "false")
    if enabled and reason:
        _set_state(db, "generation_hold_reason", reason[:400])


def _hold_channel_id(db, fallback_from_rows=None) -> int | None:
    """Canal neutro para los centinelas (no afecta a los contadores).

    Prefiere el canal de los centinelas existentes; si no, el canal de pruebas;
    si no existe, el primer canal. ``None`` si no hay canales.
    """
    for row in (fallback_from_rows or []):
        cid = row.get("channel_id")
        if cid:
            return int(cid)
    try:
        with _connect(db) as conn:
            row = conn.execute(
                "SELECT id FROM channels WHERE slug = 'test' LIMIT 1"
            ).fetchone()
            if row and row[0]:
                return int(row[0])
            row = conn.execute("SELECT MIN(id) FROM channels").fetchone()
            if row and row[0]:
                return int(row[0])
    except Exception as exc:  # noqa: BLE001
        logger.debug("generation_hold: cannot resolve channel id: %s", exc)
    return None


def is_hold_row(row: dict) -> bool:
    """True si una fila de ``generation_jobs`` es un centinela de hold."""
    try:
        return str(row.get("phase") or "").strip().lower() == HOLD_PHASE
    except Exception:  # noqa: BLE001
        return False


def active_hold_rows(db, ttl_grace: bool = True) -> list[dict]:
    """Centinelas de hold vigentes (``status='running'`` y latido no vencido).

    ``ttl_grace=True`` incluye filas con latido futuro o muy reciente. Una fila
    con latido vencido se considera no vigente (el TTL del hold expiró).
    """
    try:
        with _connect(db) as conn:
            conn.row_factory = _row_factory(conn)
            rows = conn.execute(
                "SELECT id, action, status, phase, channel_id, started_at, "
                "       last_heartbeat_at, worker_pid, video_id "
                "FROM generation_jobs "
                "WHERE phase = ? AND status = 'running'",
                (HOLD_PHASE,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            hb = d.get("last_heartbeat_at")
            if ttl_grace and hb:
                try:
                    ts = datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < datetime.now(timezone.utc):
                        continue  # TTL vencido
                except (ValueError, TypeError):
                    pass
            out.append(d)
        return out
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("generation_hold: active_hold_rows failed: %s", exc)
        return []


def _hold_rows_any_status(db) -> list[dict]:
    """Centinelas de hold existentes sin filtrar por estado.

    Necesario para RESTAURAR un centinela que un reinicio marcó como ``failed``
    en lugar de crear un duplicado.
    """
    try:
        with _connect(db) as conn:
            rows = conn.execute(
                "SELECT id, action, status, phase, channel_id, last_heartbeat_at "
                "FROM generation_jobs WHERE phase = ?",
                (HOLD_PHASE,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("generation_hold: _hold_rows_any_status failed: %s", exc)
        return []


def is_generation_hold_active(db) -> bool:
    """True si la pausa de generación está vigente por intención o centinelas."""
    if hold_intent(db):
        return True
    return bool(active_hold_rows(db))


def _row_factory(conn):
    import sqlite3
    return sqlite3.Row


def ensure_hold(db, reason: str = "", ttl_days: int = DEFAULT_TTL_DAYS) -> dict:
    """Idempotente: garantiza que los centinelas existen, ``running`` y frescos.

    No toca ninguna otra fila de ``generation_jobs`` y no enciende generación.
    Devuelve un resumen ``{created, refreshed, actions, channel_id}``.
    """
    ttl = max(1, int(ttl_days or DEFAULT_TTL_DAYS))
    # Mirar TODOS los centinelas (cualquier estado) para restaurar los que un
    # reinicio marcó como 'failed' en vez de crear duplicados.
    existing = _hold_rows_any_status(db)
    by_action = {r.get("action"): r for r in existing if r.get("action") in HOLD_ACTIONS}
    channel_id = _hold_channel_id(db, existing or None)

    created = 0
    refreshed = 0
    try:
        with _connect(db) as conn:
            # Refresca latido de los centinelas existentes (aunque su status no
            # sea 'running', los devolvemos a running para restaurar la pausa).
            for action in HOLD_ACTIONS:
                row = by_action.get(action)
                if row:
                    conn.execute(
                        "UPDATE generation_jobs "
                        "SET status='running', finished_at=NULL, "
                        "    last_heartbeat_at=datetime('now', ?) "
                        "WHERE id=? AND phase=?",
                        (f"+{ttl} days", row["id"], HOLD_PHASE),
                    )
                    refreshed += 1
                else:
                    if channel_id is None:
                        continue
                    conn.execute(
                        "INSERT INTO generation_jobs "
                        "(channel_id, action, status, phase, progress, "
                        " started_at, last_heartbeat_at) "
                        "VALUES (?, ?, 'running', ?, 0, "
                        "        datetime('now'), datetime('now', ?))",
                        (channel_id, action, HOLD_PHASE, f"+{ttl} days"),
                    )
                    created += 1
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("generation_hold: ensure_hold failed: %s", exc)
        return {"created": created, "refreshed": refreshed, "error": str(exc)}

    if reason:
        _set_state(db, "generation_hold_reason", reason[:400])
    _publish_state(db, reason=reason)
    logger.info(
        "generation_hold: pausa asegurada (creados=%d, refrescados=%d)",
        created, refreshed,
    )
    return {
        "created": created,
        "refreshed": refreshed,
        "actions": list(HOLD_ACTIONS),
        "channel_id": channel_id,
    }


def refresh_hold(db, ttl_days: int = DEFAULT_TTL_DAYS) -> int:
    """Solo empuja el latido de los centinelas existentes (no crea filas)."""
    ttl = max(1, int(ttl_days or DEFAULT_TTL_DAYS))
    try:
        with _connect(db) as conn:
            cur = conn.execute(
                "UPDATE generation_jobs SET last_heartbeat_at=datetime('now', ?) "
                "WHERE phase=? AND action IN (?, ?)",
                (f"+{ttl} days", HOLD_PHASE, *HOLD_ACTIONS),
            )
            conn.commit()
            n = cur.rowcount
        _publish_state(db)
        return n
    except Exception as exc:  # noqa: BLE001
        logger.debug("generation_hold: refresh_hold failed: %s", exc)
        return 0


def release_hold(db, confirm: str, actor: str = "operator") -> dict:
    """Libera la pausa. Exige ``confirm == RELEASE_CONFIRM``.

    Marca los centinelas como ``cancelled`` y limpia la intención. **No** inicia
    ninguna generación: solo deja de retenerla.
    """
    if confirm != RELEASE_CONFIRM:
        raise ValueError("Confirmación explícita requerida para reanudar la generación")
    released = 0
    try:
        with _connect(db) as conn:
            cur = conn.execute(
                "UPDATE generation_jobs SET status='cancelled', "
                "finished_at=CURRENT_TIMESTAMP, error_msg='hold released by operator' "
                "WHERE phase=? AND status='running'",
                (HOLD_PHASE,),
            )
            conn.commit()
            released = cur.rowcount
    except Exception as exc:  # noqa: BLE001
        logger.error("generation_hold: release_hold failed: %s", exc)
    set_hold_intent(db, False)
    _set_state(db, "generation_hold_released_by", actor)
    _set_state(db, "generation_hold_released_at", datetime.now(timezone.utc).isoformat())
    _publish_state(db)
    logger.warning("generation_hold: pausa liberada por %s (centinelas=%d)", actor, released)
    return {"released": released}


def reconcile_on_startup(db) -> dict:
    """Auto-sanación en el arranque de la API.

    Si el operador mantiene la intención de pausa, re-asegura los centinelas
    **después** de que la recuperación de arranque los haya podido tocar. Nunca
    crea el hold si no hay intención previa (no se inventa una pausa).
    """
    # Adopción: si no hay intención registrada pero existen centinelas vigentes
    # (p. ej. los mantiene un refrescador externo), se adopta como intención para
    # que el hold sobreviva a los siguientes reinicios sin escritura manual.
    raw_intent = _get_state(db, INTENT_KEY).strip().lower()
    if raw_intent == "" and active_hold_rows(db):
        set_hold_intent(db, True, reason="adoptado de centinelas existentes")
        logger.info("generation_hold: intención adoptada de centinelas vigentes")

    intent = hold_intent(db)
    result = {"intent": intent, "ensured": None, "active": None}
    if intent:
        result["ensured"] = ensure_hold(db, reason="self-heal on startup")
    result["active"] = is_generation_hold_active(db)
    if intent and not result["active"]:
        _emit_hold_alert(
            db, severity="critical",
            title="Pausa de generación no vigente tras el arranque",
            message="La intención de pausa está activa pero no hay centinelas vigentes.",
        )
    return result


# ── Feedback (alerta de sistema + aviso de estado) ──────────────

def _emit_hold_alert(db, severity: str, title: str, message: str, metadata=None) -> None:
    try:
        from api.services.lifecycle_monitor import emit_alert
        emit_alert(
            db=db, entity_type="system", entity_id=0,
            alert_type="generation_hold",
            severity=severity, title=title, message=message,
            metadata=metadata or {},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("generation_hold: emit alert failed: %s", exc)


def _publish_state(db, reason: str = "") -> None:
    """Publica el aviso de estado que el panel consume (F9)."""
    try:
        rows = active_hold_rows(db)
        payload = {
            "active": bool(rows) or hold_intent(db),
            "intent": hold_intent(db),
            "reason": reason or _get_state(db, "generation_hold_reason"),
            "sentinels": [
                {"action": r.get("action"), "id": r.get("id"),
                 "heartbeat": r.get("last_heartbeat_at")}
                for r in rows
            ],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _set_state(db, STATE_KEY, json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.debug("generation_hold: publish state failed: %s", exc)
