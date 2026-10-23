#!/usr/bin/env python3
"""Baseline / seguimiento del experimento «curiosidad oscura» (solo lectura).

Calcula medianas por canal de vídeos long-form y shorts sobre una ventana
reciente, usando únicamente las estadísticas ya recolectadas en la base de
datos (0 cuota de YouTube API, sin llamadas de red).

Uso:
    python3 scripts/baseline_curiosidad.py [--days 90] [--db autotube.db]

Salida: JSON por stdout con:
  - longform_by_channel: videos, medianas de views 7d/28d, CTR 28d, retención.
  - shorts_by_channel:   shorts, mediana de views.
  - top/bottom longform por views_28d (para inspección cualitativa).

Fase 0/6/7 del plan «curiosidad oscura»: la misma medición se repite al
evaluar el piloto, de modo que línea base y resultado sean comparables.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta


def median(values):
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return None
    n = len(vs)
    mid = n // 2
    return vs[mid] if n % 2 else (vs[mid - 1] + vs[mid]) / 2


def parse_date(s):
    if not s:
        return None
    s = str(s)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def compute(db_path: str, window_days: int) -> dict:
    today = date.today()
    min_pub_date = (today - timedelta(days=window_days)).isoformat()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # ── Longform: alcance diario por vídeo, ventanas 7d/28d desde publicación ──
    rows = conn.execute(
        """
        SELECT v.id, v.canal, v.titulo_final,
               COALESCE(v.actual_published_at, v.published_at) AS pub
        FROM videos v
        WHERE v.yt_video_id IS NOT NULL
          AND COALESCE(v.actual_published_at, v.published_at) IS NOT NULL
          AND date(COALESCE(v.actual_published_at, v.published_at)) >= ?
        """,
        (min_pub_date,),
    ).fetchall()

    reach = {}
    for r in conn.execute(
        """
        SELECT v.id AS vid, r.date, r.views, r.impressions, r.impressions_ctr,
               r.retention_pct
        FROM video_reach_daily r JOIN videos v ON v.yt_video_id = r.yt_video_id
        """
    ):
        reach.setdefault(r["vid"], []).append(r)

    retention_latest = {}
    for r in conn.execute(
        """
        SELECT h.video_id, h.retention_pct, h.average_view_duration
        FROM video_stats_history h
        WHERE h.id = (SELECT h2.id FROM video_stats_history h2
                      WHERE h2.video_id = h.video_id
                      ORDER BY h2.fetched_at DESC, h2.id DESC LIMIT 1)
        """
    ):
        retention_latest[r["video_id"]] = (r["retention_pct"], r["average_view_duration"])

    per_channel = {}
    videos_detail = []
    for r in rows:
        pub = parse_date(r["pub"])
        if pub is None:
            continue
        v7 = v28 = imp28 = 0
        ctr_num = 0.0
        for d in reach.get(r["id"], []):
            dd = parse_date(d["date"])
            if dd is None:
                continue
            delta = (dd - pub).days
            if 0 <= delta < 7:
                v7 += d["views"] or 0
            if 0 <= delta < 28:
                v28 += d["views"] or 0
                imp28 += d["impressions"] or 0
                ctr_num += (d["impressions"] or 0) * (d["impressions_ctr"] or 0.0)
        ctr28 = round(ctr_num / imp28, 2) if imp28 else None
        ret, avd = retention_latest.get(r["id"], (None, None))
        ch = per_channel.setdefault(
            r["canal"],
            {"videos": 0, "v7": [], "v28": [], "ctr28": [], "ret": [], "avd": []},
        )
        ch["videos"] += 1
        ch["v7"].append(v7)
        ch["v28"].append(v28)
        if ctr28 is not None:
            ch["ctr28"].append(ctr28)
        if ret:
            ch["ret"].append(ret)
        if avd:
            ch["avd"].append(avd)
        videos_detail.append(
            {
                "id": r["id"],
                "canal": r["canal"],
                "titulo": r["titulo_final"],
                "pub": pub.isoformat(),
                "views_7d": v7,
                "views_28d": v28,
                "impressions_28d": imp28,
                "ctr_28d": ctr28,
                "retention_pct": ret,
                "avg_view_duration_s": avd,
            }
        )

    longform_summary = {}
    for canal, ch in sorted(per_channel.items()):
        longform_summary[canal] = {
            "videos": ch["videos"],
            "median_views_7d": median(ch["v7"]),
            "median_views_28d": median(ch["v28"]),
            "median_ctr_28d_pct": median(ch["ctr28"]),
            "median_retention_pct": median(ch["ret"]),
            "median_avg_view_duration_s": median(ch["avd"]),
            "videos_with_ctr": len(ch["ctr28"]),
        }

    # ── Shorts: últimas views por short en la ventana ──
    short_rows = conn.execute(
        """
        SELECT s.id, c.slug AS canal, s.title,
               COALESCE(s.actual_published_at, s.published_at) AS pub,
               (SELECT st.views FROM short_stats st WHERE st.short_id = s.id
                ORDER BY st.fetched_at DESC, st.id DESC LIMIT 1) AS views,
               (SELECT st.average_view_duration FROM short_stats st WHERE st.short_id = s.id
                ORDER BY st.fetched_at DESC, st.id DESC LIMIT 1) AS avd
        FROM shorts s JOIN channels c ON c.id = s.channel_id
        WHERE s.youtube_id IS NOT NULL
          AND COALESCE(s.actual_published_at, s.published_at) IS NOT NULL
          AND date(COALESCE(s.actual_published_at, s.published_at)) >= ?
        """,
        (min_pub_date,),
    ).fetchall()

    shorts_by_channel = {}
    for r in short_rows:
        ch = shorts_by_channel.setdefault(r["canal"], {"shorts": 0, "views": [], "avd": []})
        ch["shorts"] += 1
        if r["views"] is not None:
            ch["views"].append(r["views"])
        if r["avd"]:
            ch["avd"].append(r["avd"])

    shorts_summary = {}
    for canal, ch in sorted(shorts_by_channel.items()):
        shorts_summary[canal] = {
            "shorts": ch["shorts"],
            "median_views": median(ch["views"]),
            "median_avg_view_duration_s": median(ch["avd"]),
        }

    videos_detail.sort(key=lambda v: v["views_28d"], reverse=True)
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "window_days": window_days,
        "min_publish_date": min_pub_date,
        "longform_by_channel": longform_summary,
        "shorts_by_channel": shorts_summary,
        "top15_longform_28d": videos_detail[:15],
        "bottom5_longform_28d": [v for v in videos_detail if v["views_28d"] is not None][-5:],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="autotube.db", help="Ruta a la base de datos SQLite")
    ap.add_argument("--days", type=int, default=90, help="Ventana hacia atrás en días")
    args = ap.parse_args()
    print(json.dumps(compute(args.db, args.days), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
