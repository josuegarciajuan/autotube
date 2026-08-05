#!/usr/bin/env python3
"""Verify YouTube privacy status for all recent videos.
Cross-references DB status with actual YouTube API privacyStatus.
"""

import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.youtube_uploader import YouTubeUploader


def main():
    db = sqlite3.connect("autotube.db")
    db.row_factory = sqlite3.Row

    # Get all unique yt_video_ids from last 3 days
    recent = db.execute("""
        SELECT DISTINCT v.yt_video_id, v.canal, v.status as db_status,
               v.privacy_status as db_privacy, v.publish_mode, v.published_at,
               v.target_public_at, v.uploaded_at,
               (SELECT max(v2.id) FROM videos v2 WHERE v2.yt_video_id = v.yt_video_id) as max_id,
               (SELECT v2.titulo_final FROM videos v2 WHERE v2.id = (SELECT max(v3.id) FROM videos v3 WHERE v3.yt_video_id = v.yt_video_id)) as title
        FROM videos v
        WHERE v.yt_video_id IS NOT NULL
          AND date(v.created_at) >= date('now', '-3 days')
        ORDER BY v.created_at DESC
    """).fetchall()

    print(f"\n{'='*80}")
    print(f"VERIFICACIÓN DE PRIVACIDAD EN YOUTUBE")
    print(f"Fecha/Hora: {datetime.now(timezone.utc).isoformat()}")
    print(f"Videos a verificar: {len(recent)}")
    print(f"{'='*80}\n")

    # Cache uploaders per channel
    uploaders = {}
    results = []

    for r in recent:
        yt_id = r["yt_video_id"]
        canal = r["canal"] or ""
        db_status = r["db_status"]
        db_privacy = r["db_privacy"]
        title = r["title"] or "?"
        publish_mode = r["publish_mode"]
        target = r["target_public_at"]
        published_db = r["published_at"]
        uploaded = r["uploaded_at"]

        # Get or create uploader for this channel
        if canal not in uploaders:
            try:
                uploader = YouTubeUploader(account_name=canal, channel_slug=canal)
                if not uploader.authenticate():
                    print(f"[{canal}] ❌ Auth failed — skipping channel")
                    uploaders[canal] = None
                else:
                    uploaders[canal] = uploader
                    print(f"[{canal}] ✓ Authenticated")
            except Exception as e:
                print(f"[{canal}] ❌ Auth error: {e}")
                uploaders[canal] = None

        uploader = uploaders.get(canal)
        if not uploader:
            print(f"  [{canal}] {yt_id} — SKIPPED (no auth)")
            results.append((r, None, "no_auth"))
            continue

        # Query YouTube API
        try:
            service = uploader._get_service()
            resp = service.videos().list(part="status,snippet", id=yt_id).execute()
            items = resp.get("items", [])

            if not items:
                yt_privacy = "NOT_FOUND"
                yt_status = "NOT_FOUND"
                yt_title = "N/A"
                yt_public_stats = False
            else:
                item = items[0]
                yt_privacy = item["status"]["privacyStatus"]
                yt_status = item["status"]["uploadStatus"]
                yt_title = item["snippet"]["title"][:80]
                yt_public_stats = item["status"].get("publicStatsViewable", True)

            match = yt_privacy == db_privacy

            # Determine expected vs actual
            discrepancy = ""
            if not match:
                discrepancy = f"⚠️  DISCREPANCY: DB={db_privacy} vs YT={yt_privacy}"

            # Special checks
            special = ""
            if db_status == "uploaded_private" and yt_privacy == "public":
                special = "🔴 DB says uploaded_private but YT is PUBLIC! Already made public without DB update."
            elif db_status == "published" and yt_privacy != "public":
                special = "🔴 DB says published but YT is NOT public!"
            elif db_privacy == "private" and yt_privacy == "private":
                if target and published_db:
                    # Should have been published
                    special = f"🟡 STILL PRIVATE — target was {target}, should be public"
                elif target and not published_db:
                    special = f"🟢 PENDING — scheduled for {target} (not due yet?)"

            status_icon = "✓" if match else "✗"
            print(f"  [{canal}] {status_icon} {yt_id}")
            print(f"        YT: privacy={yt_privacy} upload_status={yt_status}")
            print(f"        DB: privacy={db_privacy} status={db_status} mode={publish_mode}")
            print(f"        Title: {yt_title[:70]}")
            if target:
                print(f"        Scheduled: {target}")
            if published_db:
                print(f"        DB published_at: {published_db}")
            if uploaded:
                print(f"        Uploaded: {uploaded}")
            if discrepancy:
                print(f"        {discrepancy}")
            if special:
                print(f"        {special}")
            print()

            results.append((r, yt_privacy, "ok"))

            # Rate limit: 1 req/sec per channel
            time.sleep(0.3)

        except Exception as e:
            print(f"  [{canal}] {yt_id} — ❌ API Error: {e}")
            results.append((r, None, str(e)))

    # Summary
    print(f"{'='*80}")
    print("RESUMEN")
    print(f"{'='*80}")

    total = len(results)
    ok = sum(1 for _, _, s in results if s == "ok")
    errors = sum(1 for _, _, s in results if s != "ok" and s != "no_auth")
    no_auth = sum(1 for _, _, s in results if s == "no_auth")

    discrepancies = []
    for (r, yt_privacy, status) in results:
        if status == "ok" and yt_privacy != r["db_privacy"]:
            discrepancies.append(r)

    print(f"  Total: {total}")
    print(f"  Verificados OK: {ok}")
    print(f"  Con errores: {errors}")
    print(f"  Sin auth: {no_auth}")

    if discrepancies:
        print(f"\n  ⚠️  DISCREPANCIAS ({len(discrepancies)}):")
        for d in discrepancies:
            # Find matching result
            for (r, yt_p, _) in results:
                if r["yt_video_id"] == d["yt_video_id"]:
                    print(f"    [{d['canal']}] {d['yt_video_id']}: DB={d['db_privacy']} vs YT={yt_p} "
                          f"(DB status={d['db_status']})")
                    break
    else:
        print(f"\n  ✓ Sin discrepancias entre DB y YouTube")

    db.close()


if __name__ == "__main__":
    main()
