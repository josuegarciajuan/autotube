"""Informe del experimento de recuperación de alcance (Fase 0).

Fuente única para responder "¿cómo va el experimento?" sin depender de la memoria.

KPIs **leading** (responden en días a packaging/contenido):
  - CTR long-form
  - Impresiones por vídeo (long-form)
  - Retención media (long-form)

KPIs **lagging** (tardan semanas):
  - Subs netos, vistas/día
  - Alcance Shorts a 7 días

Contrato: ``specs/experimento-recuperacion-alcance.md``. Solo lectura, 0 cuota.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("autotube.experiment_report")

STARTED_KEY = "experiment_started_at"
BASELINE_KEY = "experiment_baseline"
INTERVENTIONS_KEY = "experiment_interventions"


def _one(conn, sql: str, params: tuple = ()) -> dict:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else {}


def _scalar(conn, sql: str, params: tuple = (), default=None):
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return default
    return default if row[0] is None else row[0]


def _reach_metrics(conn, cid: int, start: str, end: str, typ: str) -> dict:
    """Impresiones/clics/CTR/vídeos/retención de un canal y tipo en [start,end).

    ``typ`` ∈ {"LONG","SHORT"}. Las fechas van en formato ``YYYYMMDD``.
    """
    if typ == "LONG":
        match = "EXISTS(SELECT 1 FROM videos v WHERE v.yt_video_id = r.yt_video_id)"
    else:
        match = "EXISTS(SELECT 1 FROM shorts s WHERE s.youtube_id = r.yt_video_id)"
    row = _one(
        conn,
        f"""SELECT COALESCE(SUM(r.impressions),0) imp,
                   COALESCE(SUM(CASE WHEN r.impressions>0
                                THEN r.impressions * r.impressions_ctr / 100.0
                                ELSE 0 END),0) clk,
                   COUNT(DISTINCT CASE WHEN r.impressions>0 THEN r.yt_video_id END) nvid
            FROM video_reach_daily r
            WHERE r.channel_id = ? AND r.date >= ? AND r.date < ? AND ({match})""",
        (cid, start, end),
    )
    imp = int(row.get("imp") or 0)
    clk = float(row.get("clk") or 0)
    nvid = int(row.get("nvid") or 0)
    retention = _scalar(
        conn,
        f"""SELECT AVG(r.retention_pct) FROM video_reach_daily r
            WHERE r.channel_id = ? AND r.date >= ? AND r.date < ?
              AND r.retention_pct > 0 AND r.retention_pct <= 100
              AND ({match})""",
        (cid, start, end),
    )
    return {
        "impressions": imp,
        "clicks": round(clk, 1),
        "ctr_pct": round(100.0 * clk / imp, 2) if imp else None,
        "videos_with_impressions": nvid,
        "impressions_per_video": round(imp / nvid, 1) if nvid else None,
        "retention_pct": round(float(retention), 1) if retention else None,
    }


def _shorts_reach7d(conn, cid: int) -> float | None:
    val = _scalar(
        conn,
        """SELECT AVG(v7) FROM (
             SELECT MAX(CASE WHEN julianday(ss.fetched_at) -
                      julianday(COALESCE(s.actual_published_at, s.published_at)) <= 7
                      THEN ss.views END) v7
             FROM shorts s JOIN short_stats ss ON ss.short_id = s.id
             WHERE s.channel_id = ? AND s.status = 'published'
               AND s.youtube_id IS NOT NULL
             GROUP BY s.id) WHERE v7 IS NOT NULL""",
        (cid,),
    )
    return round(float(val), 1) if val else None


def _subs_now_and_pre(conn, cid: int, pre_end: str) -> tuple[int, int]:
    now_subs = _scalar(
        conn,
        "SELECT subscribers FROM channel_stats_history WHERE channel_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (cid,),
        default=0,
    )
    pre_subs = _scalar(
        conn,
        "SELECT subscribers FROM channel_stats_history "
        "WHERE channel_id = ? AND date(fetched_at) <= ? AND subscribers > 0 "
        "ORDER BY id DESC LIMIT 1",
        (cid, pre_end),
        default=0,
    )
    return int(now_subs or 0), int(pre_subs or 0)


def _delta(now, pre):
    if now is None or pre is None:
        return None
    if isinstance(now, (int, float)) and isinstance(pre, (int, float)):
        return round(now - pre, 2)
    return None


def build_report(db=None, days_now: int = 14) -> dict:
    """Construye el informe del experimento (solo lectura)."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()

    started_raw = db.get_system_state(STARTED_KEY) or ""
    try:
        started = datetime.fromisoformat(str(started_raw)[:19])
    except (ValueError, TypeError):
        started = datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    # Ventanas de IGUAL longitud y sin solape: "ahora" = desde el inicio del
    # experimento (hasta `days_now`), "antes" = la misma cantidad de días justo
    # antes del inicio. Comparar manzanas con manzanas.
    since_start = (now.date() - started.date()).days
    win = max(1, min(int(days_now), since_start if since_start > 0 else int(days_now)))
    now_start = (now - timedelta(days=win)).strftime("%Y%m%d")
    pre_end = started.strftime("%Y%m%d")
    pre_start = (started - timedelta(days=win)).strftime("%Y%m%d")
    pre_end_date = started.strftime("%Y-%m-%d")

    interventions: list = []
    raw_int = db.get_system_state(INTERVENTIONS_KEY)
    if raw_int:
        try:
            interventions = json.loads(raw_int)
        except (ValueError, TypeError):
            interventions = []

    channels_out: list[dict] = []
    with db._connect() as conn:
        base_rows = db.get_channels(active_only=True) or []
        for ch in base_rows:
            if ch.get("slug") == "test":
                continue
            cid = int(ch.get("id") or 0)
            if not cid:
                continue
            long_now = _reach_metrics(conn, cid, now_start, "99999999", "LONG")
            long_pre = _reach_metrics(conn, cid, pre_start, pre_end, "LONG")
            short_now = _reach_metrics(conn, cid, now_start, "99999999", "SHORT")
            short_pre = _reach_metrics(conn, cid, pre_start, pre_end, "SHORT")
            subs_now, subs_pre = _subs_now_and_pre(conn, cid, pre_end_date)

            channels_out.append({
                "channel_id": cid,
                "slug": ch.get("slug"),
                "name": ch.get("name"),
                "leading": {
                    "longform_ctr_pct_now": long_now["ctr_pct"],
                    "longform_ctr_pct_pre": long_pre["ctr_pct"],
                    "longform_ctr_pct_delta": _delta(long_now["ctr_pct"], long_pre["ctr_pct"]),
                    "longform_impr_per_video_now": long_now["impressions_per_video"],
                    "longform_impr_per_video_pre": long_pre["impressions_per_video"],
                    "longform_impr_per_video_delta": _delta(
                        long_now["impressions_per_video"], long_pre["impressions_per_video"]
                    ),
                    "longform_retention_now": long_now["retention_pct"],
                    "longform_retention_pre": long_pre["retention_pct"],
                    "longform_retention_delta": _delta(
                        long_now["retention_pct"], long_pre["retention_pct"]
                    ),
                    "longform_impressions_now": long_now["impressions"],
                    "longform_impressions_pre": long_pre["impressions"],
                    "shorts_impressions_now": short_now["impressions"],
                    "shorts_ctr_pct_now": short_now["ctr_pct"],
                },
                "lagging": {
                    "subs_now": subs_now,
                    "subs_pre": subs_pre,
                    "subs_net_delta": (subs_now - subs_pre) if subs_pre else None,
                    "shorts_reach7d": _shorts_reach7d(conn, cid),
                },
            })

    return {
        "experiment_started_at": started_raw or None,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "windows": {
            "now": {"from": now_start, "days": win},
            "pre": {"from": pre_start, "to": pre_end, "days": win},
        },
        "interventions": interventions,
        "channels": channels_out,
        "note": (
            "KPIs leading = CTR/impresiones-por-vídeo/retención long-form (responden en días). "
            "El Reporting API tiene latencia de hasta 48 h: los últimos 1-2 días pueden faltar."
        ),
    }


def log_intervention(text: str, db=None, when: str | None = None) -> list:
    """Añade una intervención a la bitácora estructurada (system_state)."""
    if db is None:
        from database.db_extended import ExtendedDatabase
        db = ExtendedDatabase()
    when = when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    interventions: list = []
    raw = db.get_system_state(INTERVENTIONS_KEY)
    if raw:
        try:
            interventions = json.loads(raw)
        except (ValueError, TypeError):
            interventions = []
    interventions.append({"at": when, "text": text})
    db.set_system_state(INTERVENTIONS_KEY, json.dumps(interventions, ensure_ascii=False))
    return interventions
