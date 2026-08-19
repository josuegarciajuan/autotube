#!/usr/bin/env python3
"""Re-programar el publishAt de vídeos 'uploaded_private' a horas pico.

Contexto (ago 2026): varios vídeos se subieron como private con un
target_public_at heredado de la heurística de nicho (madrugada) o a días
vista (horizonte de planning). Quedan "calentando" en YouTube y publican a
horas malas (3-5 AM Madrid) o días después.

Este script:
1. Localiza vídeos status='uploaded_private' con yt_video_id y sin publicar.
2. Para cada canal, toma las horas óptimas (optimal_publish_slots) DIURNAS
   (hora local >= 9, descartando madrugada) ordenadas por slot_rank.
3. Asigna a cada vídeo la siguiente ocurrencia futura de esas horas, con
   >=3h de separación entre vídeos del mismo canal.
4. Llama a YouTubeUploader.set_publish_at() para reprogramar el publishAt
   real en YouTube (vídeo sigue private; YouTube publica a la nueva hora).
5. Actualiza videos.target_public_at en la DB.

Quota: ~50 ud por vídeo (videos.update). 22 vídeos ≈ 1.100 ud (marginal).

Usage:
    python3 scripts/reschedule_uploaded_private.py           # dry-run
    python3 scripts/reschedule_uploaded_private.py --apply   # ejecutar
"""

import logging
import sys
from datetime import datetime, timedelta, timezone

import pytz

_PROJECT_ROOT = __file__.rsplit("/scripts/", 1)[0]
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("reschedule_uploaded_private")

DAYTIME_MIN_HOUR = 9          # descartar horas de madrugada (local)
SAME_CHANNEL_GAP_HOURS = 3    # separación mínima entre vídeos del mismo canal
MARGIN_MINUTES = 45           # publishAt mínimo en el futuro (margen YouTube)
FALLBACK_HOURS = [(13, 0), (18, 0), (20, 0)]  # si no hay optimal slots diurnos


def get_db():
    from database.db_extended import ExtendedDatabase, migrate_v2
    migrate_v2()
    return ExtendedDatabase()


def load_videos(db):
    """Devuelve [{video_id, channel_id, slug, yt_video_id, target_public_at, titulo}]."""
    import sqlite3 as _sql
    with db._connect() as conn:
        conn.row_factory = _sql.Row
        rows = conn.execute(
            """SELECT v.id AS video_id, v.channel_id, v.yt_video_id,
                      v.target_public_at, v.titulo_final, c.slug
               FROM videos v
               JOIN channels c ON c.id = v.channel_id
               WHERE v.status = 'uploaded_private'
                 AND v.published_at IS NULL
                 AND v.yt_video_id IS NOT NULL
                 AND v.yt_video_id != ''
               ORDER BY v.channel_id, v.target_public_at"""
        ).fetchall()
    return [dict(r) for r in rows]


def channel_hours(db, channel_id):
    """Horas diurnas (local) del canal desde optimal_publish_slots, ordenadas por rank."""
    slots = db.get_optimal_slots(channel_id, "long") or []
    hours = []
    for s in slots:
        h = int(s.get("target_hour", 0) or 0)
        m = int(s.get("target_minute", 0) or 0)
        if h >= DAYTIME_MIN_HOUR:
            hours.append((h, m))
    if not hours:
        hours = list(FALLBACK_HOURS)
    # dedupe preservando orden
    seen, out = set(), []
    for hm in hours:
        if hm not in seen:
            seen.add(hm)
            out.append(hm)
    return out


def build_candidates(hours, start_utc, tz, days_ahead=12):
    """Todas las ocurrencias futuras de las horas (local) en los próximos días."""
    now_local = start_utc.astimezone(tz)
    candidates = []
    for day_offset in range(days_ahead + 1):
        day = (now_local + timedelta(days=day_offset)).date()
        for (h, m) in hours:
            dt = tz.localize(datetime(day.year, day.month, day.day, h, m))
            candidates.append(dt.astimezone(timezone.utc))
    candidates.sort()
    return candidates


def assign_times(videos, hours, tz, now_utc):
    """Asigna a cada vídeo una hora pico futura con gap >=3h."""
    candidates = build_candidates(hours, now_utc, tz)
    min_start = now_utc + timedelta(minutes=MARGIN_MINUTES)
    assignments = []
    last = None
    for v in videos:
        for cand in candidates:
            if cand < min_start:
                continue
            if last is not None and cand < last + timedelta(hours=SAME_CHANNEL_GAP_HOURS):
                continue
            assignments.append((v, cand))
            last = cand
            min_start = cand
            break
        else:
            logger.warning("  (no candidate for video #%s)", v["video_id"])
    return assignments


def main():
    apply = "--apply" in sys.argv
    db = get_db()
    videos = load_videos(db)
    if not videos:
        logger.info("No uploaded_private videos to reschedule.")
        return

    logger.info("%d uploaded_private videos. Modo %s.", len(videos), "APPLY" if apply else "DRY-RUN")
    tz = pytz.timezone("Europe/Madrid")
    now_utc = datetime.now(timezone.utc)

    by_channel = {}
    for v in videos:
        by_channel.setdefault(v["channel_id"], []).append(v)

    plan = []  # (video_dict, new_utc_dt, slug)
    for ch_id, ch_videos in by_channel.items():
        slug = ch_videos[0]["slug"]
        hours = channel_hours(db, ch_id)
        logger.info("[%s] %d videos, horas diurnas %s", slug, len(ch_videos), hours)
        assignments = assign_times(ch_videos, hours, tz, now_utc)
        for v, new_dt in assignments:
            plan.append((v, new_dt, slug))

    # ordenar el plan globalmente por nueva hora para un reporte claro
    plan.sort(key=lambda x: x[1])

    print("\n=== PLAN DE REPROGRAMACIÓN ===")
    changed = 0
    for v, new_dt, slug in plan:
        old = v["target_public_at"]
        new_iso = new_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        new_local = new_dt.astimezone(tz).strftime("%d/%m %H:%M")
        title = (v["titulo_final"] or "?")[:45]
        print(f"  [{slug}] #{v['video_id']} {old or '?'} -> {new_iso} ({new_local}) | {title}")
        changed += 1
    print(f"\nTotal a reprogramar: {changed}")

    if not apply:
        print("DRY-RUN — usa --apply para ejecutar.")
        return

    # ── Aplicar ──
    from pipeline.youtube_uploader import YouTubeUploader
    uploaders = {}
    ok = 0
    fail = 0
    for v, new_dt, slug in plan:
        new_iso = new_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        try:
            up = uploaders.get(slug)
            if up is None:
                up = YouTubeUploader(account_name=slug, channel_slug=slug, db=db)
                if not up.authenticate():
                    raise RuntimeError("auth failed")
                uploaders[slug] = up
            up.set_publish_at(v["yt_video_id"], new_iso)
            with db._connect() as conn:
                conn.execute(
                    "UPDATE videos SET target_public_at = ? WHERE id = ?",
                    (new_iso, v["video_id"]),
                )
                conn.commit()
            logger.info("[%s] ✅ video #%s (%s) -> %s", slug, v["video_id"], v["yt_video_id"], new_iso)
            ok += 1
        except Exception as exc:
            logger.error("[%s] ❌ video #%s (%s): %s", slug, v["video_id"], v.get("yt_video_id"), exc)
            fail += 1

    print(f"\nResultado: {ok} reprogramados, {fail} fallos.")


if __name__ == "__main__":
    main()
