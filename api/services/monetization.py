"""Estado de monetización (F2) — horas Analytics vs horas YPP confirmadas.

Las horas de Analytics de largos públicos son una **aproximación operativa**,
no las "horas de visualización válidas" que YouTube exige para el YPP (1.000
subs + 4.000 h públicas válidas en 12 meses). Presentarlas como progreso real de
monetización sería engañoso, así que se mantienen separadas:

* ``longform_public_hours_analytics``: suma de watch-minutes de largos desde el
  embudo (Reporting API), solo como tendencia interna.
* ``ypp_confirmed``: cifra confirmada por el operador desde YouTube Studio, con
  fecha y procedencia; es la única que cuenta como progreso hacia monetización.

Solo lectura salvo ``set_ypp_confirmed`` (acción explícita del operador).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("autotube.monetization")

YPP_HOURS_KEY = "ypp_confirmed_hours"
YPP_AT_KEY = "ypp_confirmed_at"
YPP_SOURCE_KEY = "ypp_confirmed_source"
YPP_NOTE_KEY = "ypp_confirmed_note"

YPP_TARGET_HOURS = 4000.0
YPP_TARGET_SUBS = 1000


def get_ypp_confirmed(db) -> dict:
    """Cifra de horas YPP confirmada por el operador (o ``None``)."""
    try:
        raw = db.get_system_state(YPP_HOURS_KEY)
        hours = float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        hours = None
    return {
        "hours": hours,
        "confirmed_at": db.get_system_state(YPP_AT_KEY) or None,
        "source": db.get_system_state(YPP_SOURCE_KEY) or None,
        "note": db.get_system_state(YPP_NOTE_KEY) or None,
        "target_hours": YPP_TARGET_HOURS,
        "target_subs": YPP_TARGET_SUBS,
        "progress_pct": round(hours / YPP_TARGET_HOURS * 100, 2) if hours else None,
    }


def set_ypp_confirmed(db, hours: float, source: str = "studio", note: str = "") -> dict:
    """Registra la cifra confirmada en Studio (acción explícita del operador)."""
    hours = max(0.0, float(hours))
    db.set_system_state(YPP_HOURS_KEY, str(round(hours, 1)))
    db.set_system_state(YPP_AT_KEY, datetime.now(timezone.utc).isoformat())
    db.set_system_state(YPP_SOURCE_KEY, source[:120])
    if note:
        db.set_system_state(YPP_NOTE_KEY, note[:400])
    logger.info("YPP confirmado: %.1f h (fuente=%s)", hours, source)
    return get_ypp_confirmed(db)


def longform_public_hours_analytics(db, days: int = 365) -> dict:
    """Horas de largos públicos (aproximación) por canal y agregado.

    Filtra por ``videos`` (long-form) para no mezclar Shorts; el feed de Shorts
    no cuenta para el umbral de 4.000 h.
    """
    out: dict[str, float] = {}
    with db._connect() as conn:
        rows = conn.execute(
            """SELECT c.slug AS slug,
                      COALESCE(SUM(r.watch_minutes), 0) AS minutes
               FROM video_reach_daily r
               JOIN videos v ON v.yt_video_id = r.yt_video_id
               JOIN channels c ON c.id = r.channel_id
               WHERE r.date >= date('now', ?)
               GROUP BY c.slug""",
            (f"-{int(days)} days",),
        ).fetchall()
    for r in rows:
        out[r["slug"]] = round(float(r["minutes"] or 0) / 60.0, 1)
    return {"days": int(days), "by_channel_hours": out,
            "total_hours": round(sum(out.values()), 1)}


def monetization_status(db, days: int = 365) -> dict:
    """Bloque de monetización para el informe: Analytics aprox. + YPP confirmado."""
    return {
        "longform_public_hours_analytics": longform_public_hours_analytics(db, days=days),
        "ypp_confirmed": get_ypp_confirmed(db),
        "note": (
            "Las horas de Analytics son una aproximación operativa de largos "
            "públicos; NO equivalen a las horas válidas del YPP. Solo "
            "'ypp_confirmed' (Studio) cuenta como progreso de monetización."
        ),
    }
