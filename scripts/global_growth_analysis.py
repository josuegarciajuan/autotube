#!/usr/bin/env python3
"""Reproducible global growth analysis for the active channels.

Read-only. Compares two complete 28-day windows from ``video_reach_daily``
(Reporting API), splits long-form vs shorts, and emits a Markdown + JSON
report under ``reports/``. No conclusions are inferred here — only numbers,
with a data-quality section so the reader can see what is and is not
supportable.

Usage:
    python3 scripts/global_growth_analysis.py
    python3 scripts/global_growth_analysis.py --end 2026-11-06 --days 28
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def db_path() -> str:
    return os.getenv("DATABASE_PATH", str(ROOT / "autotube.db"))


def _iso(d: date) -> str:
    return d.isoformat()


def _window(conn: sqlite3.Connection, channel_id: int, start: date, end: date) -> dict:
    row = conn.execute(
        """SELECT COUNT(DISTINCT date) days,
                  COUNT(DISTINCT yt_video_id) videos,
                  COALESCE(SUM(views), 0) views,
                  COALESCE(SUM(impressions), 0) impressions,
                  COALESCE(SUM(watch_minutes), 0) watch_minutes,
                  COALESCE(SUM(subs_gained), 0) subs_gained,
                  COALESCE(SUM(subs_lost), 0) subs_lost,
                  SUM(CASE WHEN impressions > 0 THEN impressions * impressions_ctr ELSE 0 END) ctr_num
           FROM video_reach_daily
           WHERE channel_id = ? AND date BETWEEN ? AND ?""",
        (channel_id, _iso(start), _iso(end)),
    ).fetchone()
    impressions = row["impressions"] or 0
    ctr_num = row["ctr_num"] or 0
    return {
        "days": row["days"],
        "videos": row["videos"],
        "views": row["views"],
        "impressions": impressions,
        "weighted_ctr_pct": round(ctr_num / impressions, 3) if impressions else None,
        "watch_minutes": round(row["watch_minutes"], 1),
        "subs_gained": row["subs_gained"],
        "subs_lost": row["subs_lost"],
        "subs_net": row["subs_gained"] - row["subs_lost"],
    }


def _format_split(conn: sqlite3.Connection, channel_id: int, start: date, end: date) -> dict:
    rows = conn.execute(
        """SELECT CASE
                     WHEN r.yt_video_id IN (SELECT youtube_id FROM shorts WHERE channel_id = :cid) THEN 'short'
                     WHEN r.yt_video_id IN (SELECT yt_video_id FROM videos WHERE channel_id = :cid) THEN 'long'
                     ELSE 'other'
                  END AS fmt,
                  COALESCE(SUM(r.views), 0) views,
                  COALESCE(SUM(r.impressions), 0) impressions,
                  COALESCE(SUM(r.watch_minutes), 0) watch_minutes,
                  COALESCE(SUM(r.subs_gained), 0) subs_gained,
                  COALESCE(SUM(CASE WHEN r.impressions > 0 THEN r.impressions * r.impressions_ctr ELSE 0 END), 0) ctr_num,
                  COUNT(DISTINCT r.yt_video_id) videos
           FROM video_reach_daily r
           WHERE r.channel_id = :cid AND r.date BETWEEN :start AND :end
           GROUP BY fmt""",
        {"cid": channel_id, "start": _iso(start), "end": _iso(end)},
    ).fetchall()
    out = {}
    for r in rows:
        impressions = r["impressions"] or 0
        out[r["fmt"]] = {
            "videos": r["videos"],
            "views": r["views"],
            "impressions": impressions,
            "weighted_ctr_pct": (
                round((r["ctr_num"] or 0) / impressions, 3) if impressions else None
            ),
            "watch_minutes": round(r["watch_minutes"], 1),
            "subs_gained": r["subs_gained"],
        }
    return out


def _weekly(conn: sqlite3.Connection, channel_id: int, start: date, end: date) -> list[dict]:
    rows = conn.execute(
        """SELECT substr(date, 1, 7) month, date,
                  SUM(views) views, SUM(impressions) impressions,
                  SUM(watch_minutes) watch_minutes, SUM(subs_gained) subs_gained
           FROM video_reach_daily
           WHERE channel_id = ? AND date BETWEEN ? AND ?
           GROUP BY date ORDER BY date""",
        (channel_id, _iso(start), _iso(end)),
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = date.fromisoformat(r["date"])
        week_start = d - timedelta(days=d.weekday())
        key = _iso(week_start)
        bucket = out.setdefault(key, {
            "week_start": key, "views": 0, "impressions": 0,
            "watch_minutes": 0.0, "subs_gained": 0,
        })
        bucket["views"] += r["views"] or 0
        bucket["impressions"] += r["impressions"] or 0
        bucket["watch_minutes"] += r["watch_minutes"] or 0.0
        bucket["subs_gained"] += r["subs_gained"] or 0
    return sorted(out.values(), key=lambda x: x["week_start"])


def _top_videos(conn: sqlite3.Connection, channel_id: int, start: date, end: date, limit: int = 8) -> list[dict]:
    rows = conn.execute(
        """SELECT r.yt_video_id,
                  COALESCE(MAX(v.titulo_final), s.title, r.yt_video_id) title,
                  SUM(r.views) views,
                  SUM(r.impressions) impressions,
                  SUM(r.watch_minutes) watch_minutes
           FROM video_reach_daily r
           LEFT JOIN videos v ON v.channel_id = r.channel_id AND v.yt_video_id = r.yt_video_id
           LEFT JOIN shorts s ON s.channel_id = r.channel_id AND s.youtube_id = r.yt_video_id
           WHERE r.channel_id = ? AND r.date BETWEEN ? AND ?
           GROUP BY r.yt_video_id
           ORDER BY views DESC LIMIT ?""",
        (channel_id, _iso(start), _iso(end), limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _data_quality(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """SELECT MIN(date) first, MAX(date) last,
                  COUNT(*) rows, SUM(CASE WHEN impressions = 0 THEN 1 ELSE 0 END) zero_impressions,
                  SUM(CASE WHEN impressions_ctr < 0 OR impressions_ctr > 100 THEN 1 ELSE 0 END) bad_ctr,
                  COUNT(DISTINCT date) distinct_dates
           FROM video_reach_daily"""
    ).fetchone()
    return {
        "first_date": row["first"],
        "last_date": row["last"],
        "rows": row["rows"],
        "distinct_dates": row["distinct_dates"],
        "rows_without_impressions": row["zero_impressions"],
        "rows_with_invalid_ctr": row["bad_ctr"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default=None, help="Last day of the recent window (YYYY-MM-DD)")
    ap.add_argument("--days", type=int, default=28)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    quality = _data_quality(conn)
    if args.end:
        end = date.fromisoformat(args.end)
    elif quality["last_date"]:
        end = date.fromisoformat(quality["last_date"])
    else:
        raise SystemExit("No reach data available")
    recent_start = end - timedelta(days=args.days - 1)
    prev_end = recent_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=args.days - 1)

    channels = conn.execute(
        "SELECT id, slug, name FROM channels WHERE active = 1 ORDER BY id"
    ).fetchall()

    report = {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "windows": {
            "recent": [_iso(recent_start), _iso(end)],
            "previous": [_iso(prev_start), _iso(prev_end)],
            "days": args.days,
        },
        "data_quality": quality,
        "channels": [],
        "notes": [
            "Ventanas completas de días naturales; no se comparan vídeos de distinta edad.",
            "Vistas a nivel de vídeo pueden solaparse entre días: se suman por día (no snapshots acumulativos).",
            "CTR ponderado por impresiones; ausencia de impresiones no equivale a CTR 0.",
            "Métricas de shorts y largos se separan por tabla de origen.",
            "En shorts, vistas/impresiones puede superar 100%: las impresiones del feed no son equivalentes. Usar impressions_ctr reportado, nunca un ratio derivado.",
        ],
    }

    for ch in channels:
        recent = _window(conn, ch["id"], recent_start, end)
        previous = _window(conn, ch["id"], prev_start, prev_end)
        delta = {}
        for key in ("views", "impressions", "watch_minutes", "subs_gained", "subs_net"):
            before = previous[key] or 0
            after = recent[key] or 0
            delta[key] = round(((after - before) / before) * 100, 1) if before else None
        report["channels"].append({
            "slug": ch["slug"],
            "name": ch["name"],
            "recent": recent,
            "previous": previous,
            "delta_pct": delta,
            "format_split": _format_split(conn, ch["id"], recent_start, end),
            "weekly": _weekly(conn, ch["id"], prev_start, end),
            "top_videos": _top_videos(conn, ch["id"], recent_start, end),
        })

    conn.close()

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    stamp = date.today().isoformat()
    json_path = out_dir / f"growth-{stamp}.json"
    md_path = out_dir / f"growth-{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    lines = [
        f"# Análisis global de crecimiento — {stamp}",
        "",
        f"- Ventana reciente: **{recent_start} → {end}** ({args.days} días)",
        f"- Ventana anterior: **{prev_start} → {prev_end}**",
        f"- Datos de alcance: **{quality['first_date']} → {quality['last_date']}**",
        f"- Filas con impresiones 0: **{quality['rows_without_impressions']}** "
        f"de {quality['rows']}",
        "",
        "## Comparación por canal",
        "",
        "| Canal | Vistas (ant.) | Vistas (rec.) | Δ | Impresiones (rec.) | CTR pond. | "
        "Min. vistos (rec.) | Subs netos (rec.) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for c in report["channels"]:
        r, p = c["recent"], c["previous"]
        d = c["delta_pct"]["views"]
        ctr = r["weighted_ctr_pct"]
        ctr_txt = "—" if ctr is None else f"{ctr:.2f}%"
        lines.append(
            f"| {c['name']} | {p['views']:,} | {r['views']:,} | "
            f"{'—' if d is None else f'{d:+.1f}%'} | {r['impressions']:,} | "
            f"{ctr_txt} | "
            f"{r['watch_minutes']:,} | {r['subs_net']:+,} |"
        )

    lines += ["", "## Detalle por canal", ""]
    for c in report["channels"]:
        lines += [f"### {c['name']} (`{c['slug']}`)", ""]
        fs = c["format_split"]
        for fmt in ("long", "short", "other"):
            if fmt in fs:
                v = fs[fmt]
                ctr = v.get("weighted_ctr_pct")
                lines.append(
                    f"- **{fmt}**: {v['videos']} vídeos, {v['views']:,} vistas, "
                    f"{v['impressions']:,} impresiones, "
                    f"CTR pond. {'—' if ctr is None else f'{ctr:.2f}%'}, "
                    f"{v['watch_minutes']:,} min vistos, "
                    f"{v['subs_gained']:+,} subs"
                )
        lines.append("")
        lines.append("| Semana | Vistas | Impresiones | Min. vistos | Subs |")
        lines.append("|---|---:|---:|---:|---:|")
        for w in c["weekly"]:
            lines.append(
                f"| {w['week_start']} | {w['views']:,} | {w['impressions']:,} | "
                f"{w['watch_minutes']:,.0f} | {w['subs_gained']:+,} |"
            )
        lines.append("")
        lines.append("| Vídeo | Vistas | Impresiones | Min. vistos |")
        lines.append("|---|---:|---:|---:|")
        for t in c["top_videos"]:
            title = (t["title"] or t["yt_video_id"]).replace("|", "\\|")[:70]
            lines.append(
                f"| {title} | {t['views'] or 0:,} | {t['impressions'] or 0:,} | "
                f"{(t['watch_minutes'] or 0):,.0f} |"
            )
        lines.append("")

    lines += [
        "## Límites del análisis",
        "",
        *[f"- {n}" for n in report["notes"]],
        "",
        "Este informe es descriptivo. Cualquier experimento debe registrarse con "
        "hipótesis, métrica principal, guardarraíl y plazo antes de aplicarse.",
        "",
    ]
    md_path.write_text("\n".join(lines))

    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
