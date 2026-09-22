"""A4 — Salud del marcado "contenido alterado/IA".

Dos señales:

1. **Reconciliación** (`altered_mark_missing`, critical): si hay vídeos/shorts
   publicados en las últimas N horas (ventana de gracia) cuyo flag
   ``manual_altered_content_done`` sigue a 0, es que el marcado automático no
   está funcionando (el bug del thread daemon mató 163 long-forms en silencio).

2. **Heartbeat** (`ia_mark_health_ok`, info): confirmación diaria de que todo
   lo publicado recientemente quedó marcado, con el progreso del backfill.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("autotube.ia_marking_health")

DEFAULT_GRACE_HOURS = 72


def _rows(db, sql: str, params: tuple = ()) -> list[dict]:
    with db._connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def collect_ia_marking_status(db, grace_hours: int = DEFAULT_GRACE_HOURS) -> dict:
    """Estado de marcado IA de lo publicado en la ventana de gracia. Solo lectura."""
    since = f"-{int(grace_hours)} hours"

    videos = _rows(db, """
        SELECT v.id AS db_id, c.slug AS canal, v.yt_video_id AS yt_id,
               COALESCE(v.manual_altered_content_done, 0) AS done
        FROM videos v JOIN channels c ON c.id = v.channel_id
        WHERE v.yt_video_id IS NOT NULL AND v.yt_video_id != ''
          AND COALESCE(v.manual_altered_content_skip, 0) = 0
          AND COALESCE(v.uploaded_at, v.published_at) IS NOT NULL
          AND COALESCE(v.uploaded_at, v.published_at) >= datetime('now', ?)
    """, (since,))

    shorts = _rows(db, """
        SELECT s.id AS db_id, c.slug AS canal, s.youtube_id AS yt_id,
               COALESCE(s.manual_altered_content_done, 0) AS done
        FROM shorts s JOIN channels c ON c.id = s.channel_id
        WHERE s.youtube_id IS NOT NULL AND s.youtube_id != ''
          AND COALESCE(s.manual_altered_content_skip, 0) = 0
          AND COALESCE(s.actual_published_at, s.published_at) IS NOT NULL
          AND COALESCE(s.actual_published_at, s.published_at) >= datetime('now', ?)
    """, (since,))

    all_items = videos + shorts
    unmarked = [i for i in all_items if not i["done"]]

    # Pendientes totales de backfill (histórico), calculados en vivo para no
    # depender de un contador que puede quedar obsoleto si la sesión aborta.
    backfill_pending = 0
    try:
        vp = _rows(db, """
            SELECT COUNT(*) AS n FROM videos
            WHERE yt_video_id IS NOT NULL AND yt_video_id != ''
              AND COALESCE(manual_altered_content_done, 0) = 0
              AND COALESCE(manual_altered_content_skip, 0) = 0
              AND COALESCE(status, '') NOT IN ('deleted_on_yt', 'removed')
              AND COALESCE(yt_visibility, '') NOT IN ('removed', 'unavailable')
        """)
        sp = _rows(db, """
            SELECT COUNT(*) AS n FROM shorts
            WHERE youtube_id IS NOT NULL AND youtube_id != ''
              AND COALESCE(manual_altered_content_done, 0) = 0
              AND COALESCE(manual_altered_content_skip, 0) = 0
              AND COALESCE(yt_visibility, '') NOT IN ('removed', 'unavailable')
        """)
        backfill_pending = int(vp[0]["n"]) + int(sp[0]["n"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("live backfill_pending failed: %s", exc)
        try:
            backfill_pending = int(db.get_system_state("ia_backfill_pending") or 0)
        except Exception:
            backfill_pending = 0

    # Estancamiento: quedan pendientes pero no hay progreso desde hace N días.
    last_progress = None
    backfill_stalled = False
    try:
        last_progress = db.get_system_state("ia_backfill_last_progress")
    except Exception:
        last_progress = None
    if backfill_pending > 0 and backfill_stalled_check(last_progress):
        backfill_stalled = True

    by_channel: dict[str, int] = {}
    for i in unmarked:
        by_channel[i["canal"]] = by_channel.get(i["canal"], 0) + 1

    return {
        "grace_hours": grace_hours,
        "recent_total": len(all_items),
        "recent_unmarked": len(unmarked),
        "unmarked_ids": [i["yt_id"] for i in unmarked[:20]],
        "unmarked_by_channel": by_channel,
        "backfill_pending": backfill_pending,
        "backfill_last_progress": last_progress,
        "backfill_stalled": backfill_stalled,
    }


def _mark_done(db, kind: str, db_id: int) -> None:
    """Marca en BD el ítem como 'contenido alterado/IA' hecho."""
    table = "videos" if kind == "video" else "shorts"
    with db._connect() as conn:
        conn.execute(
            f"UPDATE {table} SET manual_altered_content_done=1 WHERE id=?",
            (db_id,),
        )
        conn.commit()


def reconcile_recent_ia_marks(db, grace_hours: int = DEFAULT_GRACE_HOURS,
                              limit: int = 10) -> dict:
    """Red de seguridad del marcado IA: reintenta marcar subidas recientes sin marcar.

    Si el hilo daemon que marca tras subir falló (o no llegó a ejecutarse), este
    loop lo recupera sin esperar al backfill histórico. Fail-open: nunca lanza.
    Para canales gestionados por agente egress delega; para el resto usa el
    navegador local (con lock de cuenta, sin interferir con el worker).
    """
    result = {"attempted": 0, "marked": 0, "failed": 0, "session_expired": False}
    since = f"-{int(grace_hours)} hours"
    try:
        rows = _rows(db, """
            SELECT v.id AS db_id, c.slug AS canal, v.yt_video_id AS yt_id, 'video' AS kind
            FROM videos v JOIN channels c ON c.id = v.channel_id
            WHERE v.yt_video_id IS NOT NULL AND v.yt_video_id != ''
              AND COALESCE(v.manual_altered_content_done,0)=0
              AND COALESCE(v.manual_altered_content_skip,0)=0
              AND COALESCE(v.status,'') NOT IN ('deleted_on_yt','removed')
              AND COALESCE(v.yt_visibility,'') NOT IN ('removed','unavailable','age_restricted')
              AND COALESCE(v.uploaded_at, v.published_at) >= datetime('now', ?)
            UNION ALL
            SELECT s.id, c.slug, s.youtube_id, 'short'
            FROM shorts s JOIN channels c ON c.id = s.channel_id
            WHERE s.youtube_id IS NOT NULL AND s.youtube_id != ''
              AND COALESCE(s.manual_altered_content_done,0)=0
              AND COALESCE(s.manual_altered_content_skip,0)=0
              AND COALESCE(s.yt_visibility,'') NOT IN ('removed','unavailable','age_restricted')
              AND COALESCE(s.actual_published_at, s.published_at) >= datetime('now', ?)
            LIMIT ?
        """, (since, since, limit))
    except Exception as exc:  # noqa: BLE001
        logger.debug("reconcile recent ia marks query failed: %s", exc)
        return result
    if not rows:
        return result

    try:
        from pipeline.youtube_browser import (
            get_browser, get_account_for_channel, mark_altered_content_robust,
        )
        from api.services.egress_delegation import egress_client_for
    except Exception as exc:  # noqa: BLE001
        logger.debug("reconcile imports failed: %s", exc)
        return result

    browsers: dict = {}
    for r in rows:
        result["attempted"] += 1
        try:
            canal = r["canal"]
            account = get_account_for_channel(canal)
            _egress = egress_client_for(canal)
            if _egress is not None:
                resp = _egress.browser_action(
                    "mark_altered", account=account or "",
                    params={"video_id": r["yt_id"]})
                ok, reason = bool(resp.get("ok")), "egress"
            else:
                if not account:
                    result["failed"] += 1
                    continue
                browser = browsers.get(account)
                if browser is None:
                    browser = get_browser(account)
                    browsers[account] = browser
                ok = mark_altered_content_robust(browser, r["yt_id"], attempts=3)
                reason = getattr(browser, "last_mark_reason", "") or ""
            if ok:
                _mark_done(db, r["kind"], r["db_id"])
                result["marked"] += 1
                logger.info("Reconciliación IA: %s %s (%s) marcado",
                            r["kind"], r["yt_id"], canal)
            else:
                result["failed"] += 1
                if reason == "session_expired":
                    result["session_expired"] = True
        except Exception as exc:  # noqa: BLE001
            result["failed"] += 1
            logger.warning("Reconciliación IA falló para %s: %s", r.get("yt_id"), exc)
    return result


def backfill_stalled_check(last_progress: str | None,
                           stale_days: int = 2) -> bool:
    """True si no hay progreso del backfill registrado en ``stale_days`` días.

    Sin marca de tiempo (nunca progresó) también cuenta como estancado, para no
    quedarnos ciegos cuando la sesión aborta siempre antes de marcar nada.
    """
    if not last_progress:
        return True
    try:
        from datetime import datetime, timedelta
        ts = datetime.fromisoformat(str(last_progress))
        # ``ia_backfill_last_progress`` se guarda con tz (Europe/Madrid);
        # comparar aware vs naive lanzaba TypeError y marcaba estancado siempre.
        now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
        return now - ts > timedelta(days=stale_days)
    except Exception:
        return True



def check_ia_marking_health(db, grace_hours: int = DEFAULT_GRACE_HOURS) -> dict:
    """Emite alerta crítica si hay recientes sin marcar; si no, heartbeat info."""
    status = collect_ia_marking_status(db, grace_hours=grace_hours)
    try:
        from api.services.lifecycle_monitor import emit_alert
    except Exception as exc:  # noqa: BLE001
        logger.warning("emit_alert no disponible: %s", exc)
        return status

    if status["recent_unmarked"] > 0:
        emit_alert(
            db, entity_type="system", entity_id=0,
            alert_type="altered_mark_missing", severity="critical",
            title=f"⚠️ {status['recent_unmarked']} subida(s) reciente(s) SIN marcado IA",
            message=(
                f"En las últimas {grace_hours} h hay {status['recent_unmarked']} de "
                f"{status['recent_total']} publicaciones sin marcar como contenido "
                f"alterado/IA ({status['unmarked_by_channel']}). El marcado automático "
                f"no está funcionando o hay pendientes de backfill."
            ),
            metadata={
                "unmarked_ids": status["unmarked_ids"],
                "by_channel": status["unmarked_by_channel"],
                "grace_hours": grace_hours,
            },
        )

    # Estancamiento del backfill: pendientes históricos sin progreso. No lo
    # cubre la ventana de gracia (los pendientes son antiguos) y sin esta señal
    # el sistema decía "OK" mientras la sesión diaria abortaba sin avanzar.
    if status["backfill_stalled"]:
        emit_alert(
            db, entity_type="system", entity_id=0,
            alert_type="ia_backfill_stalled", severity="critical",
            title=f"🐢 Backfill IA estancado ({status['backfill_pending']} pendientes)",
            message=(
                f"Quedan {status['backfill_pending']} vídeos/shorts sin marcar y no hay "
                f"progreso del backfill desde hace >2 días "
                f"(último progreso: {status.get('backfill_last_progress') or 'nunca'}). "
                f"Revisar la sesión diaria (journalctl -u autotube-ia-backfill.service)."
            ),
            metadata={
                "backfill_pending": status["backfill_pending"],
                "last_progress": status.get("backfill_last_progress"),
            },
        )

    if status["recent_unmarked"] == 0 and not status["backfill_stalled"]:
        emit_alert(
            db, entity_type="system", entity_id=0,
            alert_type="ia_mark_health_ok", severity="info",
            title="✅ Marcado IA OK",
            message=(
                f"{status['recent_total']} publicación(es) recientes, todas marcadas. "
                f"Backfill pendiente histórico: {status['backfill_pending']}."
            ),
            metadata=status,
        )
        # La condición que originó las alertas críticas ya no se cumple: ciérralas,
        # en vez de dejarlas abiertas para siempre tras desaparecer de la ventana
        # (o tras corregirse un falso positivo de "estancado").
        try:
            with db._connect() as conn:
                conn.execute(
                    """UPDATE pipeline_alerts
                       SET resolved=1, resolved_at=datetime('now'), acknowledged=1,
                           message=COALESCE(message, '') ||
                           ' [Auto-resuelto: publicaciones recientes marcadas]'
                       WHERE alert_type='altered_mark_missing' AND resolved=0"""
                )
                conn.execute(
                    """UPDATE pipeline_alerts
                       SET resolved=1, resolved_at=datetime('now'), acknowledged=1,
                           message=COALESCE(message, '') ||
                           ' [Auto-resuelto: backfill IA con progreso reciente]'
                       WHERE alert_type='ia_backfill_stalled' AND resolved=0"""
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("resolve ia marking alerts failed: %s", exc)
    return status
