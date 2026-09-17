#!/usr/bin/env python3
"""Auditoría del catálogo: near-duplicados, títulos plantilla y disclosure IA.

**Solo lectura.** No publica, no oculta ni borra nada: genera un informe para
que el operador decida qué `unlisted` merece la pena (el borrado masivo está
desaconsejado). Alimenta el diagnóstico del experimento
(``specs/experimento-recuperacion-alcance.md``).

Uso:
    python3 scripts/audit_catalog_duplicates.py
    python3 scripts/audit_catalog_duplicates.py --channel canal5 --threshold 0.5
    python3 scripts/audit_catalog_duplicates.py --json /tmp/audit.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.topic_dedup import topic_similarity, topic_tokens  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("audit_catalog")

# Artefactos de plantilla/IA en títulos (heurística conservadora).
SUSPICIOUS_SUFFIXES = ("al fin", "impactante", "siniestro", "impresionante")


def _title(row: dict) -> str:
    return (row.get("label") or "").strip()


def _suspicious(title: str) -> bool:
    t = (title or "").lower()
    if "_" in t:  # p. ej. "miracles_and_coincidences"
        return True
    return any(t.endswith(s) or f" — {s}" in t for s in SUSPICIOUS_SUFFIXES)


def _clusters(rows: list[dict], threshold: float, min_tokens: int = 2) -> list[dict]:
    """Agrupa filas cuyo título se parece por encima del umbral (O(n²) acotado)."""
    token_sets = {r["id"]: topic_tokens(_title(r)) for r in rows}
    used: set[int] = set()
    clusters: list[dict] = []
    for i, a in enumerate(rows):
        if a["id"] in used:
            continue
        ta = token_sets[a["id"]]
        if len(ta) < min_tokens:
            continue
        group = [a]
        for b in rows[i + 1:]:
            if b["id"] in used:
                continue
            tb = token_sets[b["id"]]
            if len(tb) < min_tokens:
                continue
            if len(ta & tb) < min_tokens:
                continue
            if topic_similarity(ta, tb) >= threshold:
                group.append(b)
                used.add(b["id"])
        if len(group) > 1:
            used.add(a["id"])
            clusters.append({
                "n": len(group),
                "items": [
                    {
                        "id": g["id"],
                        "yt": g.get("yt"),
                        "title": _title(g),
                        "views": g.get("views") or 0,
                        "published_at": g.get("published_at"),
                    }
                    for g in group
                ],
            })
    clusters.sort(key=lambda c: c["n"], reverse=True)
    return clusters


def audit_channel(db, channel: dict, threshold: float) -> dict:
    cid, slug = channel["id"], channel["slug"]
    with db._connect() as conn:
        shorts = [dict(r) for r in conn.execute(
            "SELECT s.id, s.youtube_id AS yt, "
            "       COALESCE(s.hook_title, s.title) AS label, "
            "       COALESCE(s.actual_published_at, s.published_at) AS published_at, "
            "       COALESCE((SELECT MAX(x.views) FROM short_stats x "
            "                 WHERE x.short_id = s.id AND x.views > 0), 0) AS views, "
            "       COALESCE(s.manual_altered_content_done, 0) AS disclosure "
            "FROM shorts s WHERE s.channel_id = ? AND s.youtube_id IS NOT NULL "
            "  AND s.status = 'published'",
            (cid,),
        ).fetchall()]
        videos = [dict(r) for r in conn.execute(
            "SELECT v.id, v.yt_video_id AS yt, v.titulo_final AS label, "
            "       COALESCE(v.actual_published_at, v.published_at, v.uploaded_at) "
            "         AS published_at, "
            "       COALESCE((SELECT MAX(x.views) FROM video_stats_history x "
            "                 WHERE x.video_id = v.id AND x.views > 0), 0) AS views, "
            "       COALESCE(v.manual_altered_content_done, 0) AS disclosure "
            "FROM videos v WHERE v.channel_id = ? AND v.yt_video_id IS NOT NULL "
            "  AND v.status IN ('published', 'uploaded_private')",
            (cid,),
        ).fetchall()]

    shorts_clusters = _clusters(shorts, threshold)
    video_clusters = _clusters(videos, threshold)

    no_disclosure_shorts = sum(1 for s in shorts if not s["disclosure"])
    no_disclosure_videos = sum(1 for v in videos if not v["disclosure"])
    suspicious = [s for s in shorts + videos if _suspicious(s["label"])]

    return {
        "channel_id": cid,
        "slug": slug,
        "shorts": {
            "total": len(shorts),
            "duplicate_clusters": shorts_clusters,
            "duplicate_items": sum(c["n"] for c in shorts_clusters),
            "without_disclosure": no_disclosure_shorts,
        },
        "longform": {
            "total": len(videos),
            "duplicate_clusters": video_clusters,
            "duplicate_items": sum(c["n"] for c in video_clusters),
            "without_disclosure": no_disclosure_videos,
        },
        "suspicious_titles": [
            {"id": s["id"], "title": s["label"], "views": s["views"]}
            for s in suspicious
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=None, help="slug concreto (default: todos)")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="similitud mínima para near-duplicado (0-1)")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="ruta donde volcar el informe JSON")
    args = parser.parse_args(argv)

    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()

    channels = db.get_channels(active_only=True)
    if args.channel:
        channels = [c for c in channels if c["slug"] == args.channel]

    report = {"threshold": args.threshold, "channels": []}
    for ch in channels:
        if ch["slug"] == "test":
            continue
        res = audit_channel(db, ch, args.threshold)
        report["channels"].append(res)
        s, lf = res["shorts"], res["longform"]
        print(f"\n=== {res['slug']} ===")
        print(f"  shorts: {s['total']} · clusters dup: {len(s['duplicate_clusters'])} "
              f"({s['duplicate_items']} vídeos) · sin disclosure: {s['without_disclosure']}")
        print(f"  longform: {lf['total']} · clusters dup: {len(lf['duplicate_clusters'])} "
              f"({lf['duplicate_items']} vídeos) · sin disclosure: {lf['without_disclosure']}")
        print(f"  títulos sospechosos (plantilla/IA): {len(res['suspicious_titles'])}")
        for c in s["duplicate_clusters"][:3]:
            print(f"    [dup x{c['n']}] {c['items'][0]['title'][:70]}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Informe escrito en %s", args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
