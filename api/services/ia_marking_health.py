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
          AND COALESCE(v.uploaded_at, v.published_at) IS NOT NULL
          AND COALESCE(v.uploaded_at, v.published_at) >= datetime('now', ?)
    """, (since,))

    shorts = _rows(db, """
        SELECT s.id AS db_id, c.slug AS canal, s.youtube_id AS yt_id,
               COALESCE(s.manual_altered_content_done, 0) AS done
        FROM shorts s JOIN channels c ON c.id = s.channel_id
        WHERE s.youtube_id IS NOT NULL AND s.youtube_id != ''
          AND COALESCE(s.actual_published_at, s.published_at) IS NOT NULL
          AND COALESCE(s.actual_published_at, s.published_at) >= datetime('now', ?)
    """, (since,))

    all_items = videos + shorts
    unmarked = [i for i in all_items if not i["done"]]

    # Pendientes totales de backfill (histórico), informativo.
    backfill_pending = 0
    try:
        backfill_pending = int(db.get_system_state("ia_backfill_pending") or 0)
    except Exception:
        pass

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
    }


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
    else:
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
    return status
