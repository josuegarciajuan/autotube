#!/usr/bin/env python3
"""Diagnóstico completo de todos los canales — cruza DB local vs YouTube API.

Detecta 6 tipos de anomalías:
  1. duplicates         — mismo título subido 2+ veces al mismo canal
  2. orphaned_on_yt     — video en YouTube sin registro en DB local
  3. deleted_manually    — yt_video_id en DB que ya no existe en YouTube
  4. missing_thumbnail   — video publicado sin thumbnail personalizado
  5. stuck_processing    — processingStatus='processing' hace >24h
  6. publish_failed      — privacyStatus='private' con publishAt ya pasado

Uso:
    python3 scripts/diagnose_all_channels.py              # report completo
    python3 scripts/diagnose_all_channels.py --json-only  # solo JSON sin log
    python3 scripts/diagnose_all_channels.py --channel canal2  # un solo canal
"""

import argparse
import json
import logging
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ── Project root ─────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
PROJECT_ROOT = settings.PROJECT_ROOT

from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from database.db_extended import ExtendedDatabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("diagnose_all")

# ── Config ────────────────────────────────────────────────────
TOKENS_DIR = settings.TOKENS_DIR
OUTPUT_DIR = settings.OUTPUT_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

# Cuota: 1 unit por videos().list (50 ids) + 1 por channels().list + 1 por playlistItems
QUOTA_ESTIMATE_PER_CHANNEL = 10  # generoso

# ── Helpers ────────────────────────────────────────────────────


def _authenticate(slug: str) -> Any:
    """Load OAuth2 token for a channel slug. Returns googleapiclient service."""
    token_path = TOKENS_DIR / f"{slug}.pickle"
    if not token_path.exists():
        logger.warning("Token no encontrado: %s — saltando %s", token_path, slug)
        return None

    try:
        with open(token_path, "rb") as f:
            creds = pickle.load(f)
    except (pickle.UnpicklingError, EOFError, TypeError) as exc:
        logger.error("Token corrupto para %s: %s", slug, exc)
        return None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(GoogleRequest())
                with open(token_path, "wb") as f:
                    pickle.dump(creds, f)
                logger.info("Token refrescado para %s", slug)
            except Exception as exc:
                logger.error("No se pudo refrescar token para %s: %s", slug, exc)
                return None
        else:
            logger.error("Token inválido para %s — requiere re-autenticación", slug)
            return None

    return build("youtube", "v3", credentials=creds)


def _slugify_title(title: str) -> str:
    """Normalize title for duplicate detection (conservative).

    Only removes case, spaced separators (" — ", " | "), trailing
    parenthetical/bracket decorations ((REAL), [LIMITADO], etc.), and
    punctuation. Does NOT remove content words — over-grouping distinct
    videos (e.g. "dormir" vs "morir", "Göbekli Tepe" vs "Karahan Tepe")
    is far more dangerous than missing a duplicate.
    """
    import re
    t = title.lower().strip()
    # Remove series suffix after a SPACED separator only (so "K-141" is not
    # truncated to "K"). Handles "Título — El Secreto X" and "Título | Serie".
    t = re.split(r'\s+[—–|]\s+', t, maxsplit=1)[0]
    # Strip trailing bracketed/parenthetical decorations repeatedly:
    # (REAL), (Documental), [LIMITADO], [CLASIFICADO], (DOCUMENTAL), etc.
    while True:
        t2 = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]\s*$', '', t).strip()
        if t2 == t:
            break
        t = t2
    # Remove remaining punctuation and collapse whitespace
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _compute_title_similarity(a: str, b: str) -> float:
    """Simple word-level Jaccard similarity between two normalized titles."""
    words_a = set(_slugify_title(a).split())
    words_b = set(_slugify_title(b).split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _find_duplicates_by_title(yt_videos: list[dict]) -> list[dict]:
    """Group YT videos by EXACT normalized title (dict keyed by slug).

    v24.1 fix: replaced fuzzy Jaccard similarity (>0.70) with exact-title
    grouping. The fuzzy matching grouped DISTINCT videos that differed by a
    single content word (e.g. 'dormir' vs 'morir' vs 'soñar', 'dolor' vs
    'hambre', 'Göbekli Tepe' vs 'Karahan Tepe'), causing cleanup to DELETE
    legitimate distinct videos with thousands of views. Exact matching only
    groups true re-uploads (identical titles after normalization).
    """
    buckets: dict[str, list[dict]] = {}
    for v in yt_videos:
        key = _slugify_title(v["snippet"]["title"])
        if not key:
            continue
        buckets.setdefault(key, []).append(v)
    return [grp for grp in buckets.values() if len(grp) >= 2]


def _check_thumbnail_custom(thumbnails: dict) -> bool:
    """Check if video has a custom thumbnail (not just auto-generated default)."""
    if not thumbnails:
        return False
    # Custom thumbnails appear as 'standard' or 'maxres', not just 'default'
    has_custom = "maxres" in thumbnails or "standard" in thumbnails
    if has_custom:
        return True
    # If only 'default' exists and it was auto-generated (URL contains 'hqdefault' or similar)
    default = thumbnails.get("default", {})
    default_url = default.get("url", "")
    if default_url and "hqdefault" in default_url:
        return False  # auto-generated
    return has_custom

# ── Main diagnostic per channel ─────────────────────────────────


def diagnose_channel(slug: str, yt_service, db: ExtendedDatabase) -> dict:
    """Run full diagnostic for one channel. Returns anomaly report dict."""
    result = {
        "channel": slug,
        "duplicates": [],
        "orphaned_on_yt": [],
        "deleted_manually": [],
        "missing_thumbnail": [],
        "stuck_processing": [],
        "publish_failed": [],
        "stats": {"db_videos": 0, "yt_videos": 0, "matched": 0, "quota_used": 0},
    }

    # ── 1. Get channel's upload playlist ─────────────────────
    try:
        ch_resp = yt_service.channels().list(
            part="contentDetails,snippet",
            mine=True,
            maxResults=1,
        ).execute()
        result["stats"]["quota_used"] += 1

        ch_items = ch_resp.get("items", [])
        if not ch_items:
            logger.warning("[%s] Canal sin items — ¿token inválido?", slug)
            return result

        uploads_playlist_id = ch_items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        channel_title = ch_items[0]["snippet"]["title"]
        logger.info("[%s] Canal: %s | playlist: %s", slug, channel_title, uploads_playlist_id)
    except HttpError as exc:
        logger.error("[%s] Error accediendo al canal: %s", slug, exc)
        return result

    # ── 2. List all video IDs from uploads playlist ──────────
    yt_video_ids: list[str] = []
    next_page_token = None
    page_count = 0

    while True:
        try:
            pl_resp = yt_service.playlistItems().list(
                part="contentDetails",
                playlistId=uploads_playlist_id,
                maxResults=50,
                pageToken=next_page_token,
            ).execute()
            result["stats"]["quota_used"] += 1
            page_count += 1

            for item in pl_resp.get("items", []):
                vid = item["contentDetails"].get("videoId")
                if vid:
                    yt_video_ids.append(vid)

            next_page_token = pl_resp.get("nextPageToken")
            if not next_page_token:
                break

            logger.debug("[%s] Página %d: %d IDs (total: %d)",
                         slug, page_count, len(yt_video_ids), len(yt_video_ids))

        except HttpError as exc:
            logger.error("[%s] Error listando playlist (página %d): %s", slug, page_count, exc)
            break

    # ── v24.1 fix: dedupe video IDs ─────────────────────────────
    # YouTube's uploads playlist occasionally returns the same videoId
    # across page boundaries (pagination overlap). Duplicate IDs here
    # propagate into `all_yt_videos`, making `_find_duplicates_by_title`
    # group a video WITH ITSELF (title similarity 1.0). That in turn made
    # cleanup_yt_duplicates.py delete the canonical video (same ID listed
    # as both KEEP and DEL). Dedupe here, preserving order.
    _raw_count = len(yt_video_ids)
    _seen: set[str] = set()
    yt_video_ids = [v for v in yt_video_ids if not (v in _seen or _seen.add(v))]
    if len(yt_video_ids) != _raw_count:
        logger.warning(
            "[%s] Deduplicados %d video IDs repetidos (paginación) — %d únicos",
            slug, _raw_count - len(yt_video_ids), len(yt_video_ids),
        )

    result["stats"]["yt_videos"] = len(yt_video_ids)
    logger.info("[%s] Total videos en YT: %d (en %d páginas)", slug, len(yt_video_ids), page_count)

    if not yt_video_ids:
        return result

    # ── 3. Batch-fetch video details (snippet + status + statistics) ──
    all_yt_videos: list[dict] = []
    batch_size = 50

    for i in range(0, len(yt_video_ids), batch_size):
        batch = yt_video_ids[i:i + batch_size]
        try:
            vid_resp = yt_service.videos().list(
                part="snippet,status,contentDetails,statistics",
                id=",".join(batch),
                maxResults=50,
            ).execute()
            result["stats"]["quota_used"] += 1
            all_yt_videos.extend(vid_resp.get("items", []))
        except HttpError as exc:
            logger.error("[%s] Error batch %d-%d: %s", slug, i, i + batch_size, exc)
            continue

    logger.info("[%s] Metadatos obtenidos para %d/%d videos",
                slug, len(all_yt_videos), len(yt_video_ids))

    # ── 4. Build YT-side index ──────────────────────────────
    yt_index: dict[str, dict] = {}
    for v in all_yt_videos:
        yt_id = v.get("id", "")
        if yt_id:
            yt_index[yt_id] = v

    # ── 5. Get DB-side videos for this channel ───────────────
    db_videos = db.get_videos(status=None, limit=9999, offset=0)
    db_for_channel = [
        v for v in db_videos
        if v.get("canal") == slug
        and v.get("yt_video_id")
        and str(v.get("yt_video_id", "")).strip()
    ]
    result["stats"]["db_videos"] = len(db_for_channel)

    db_index: dict[str, dict] = {}
    for v in db_for_channel:
        yt_id = str(v.get("yt_video_id", "")).strip()
        if yt_id:
            db_index[yt_id] = v

    # ── 5b. Also index SHORTS (tracked in the `shorts` table) ──
    # v24.1 fix: shorts live in a separate table but still appear in the
    # uploads playlist. Without this, every short was misreported as an
    # 'orphaned_on_yt' (932 → 764 were actually shorts). Mark them matched
    # so they don't pollute the orphan list.
    try:
        with db._connect() as _conn:
            _shorts_rows = _conn.execute(
                """SELECT s.youtube_id, c.slug
                   FROM shorts s JOIN channels c ON c.id = s.channel_id
                   WHERE c.slug = ? AND s.youtube_id IS NOT NULL AND s.youtube_id != ''
                """,
                (slug,),
            ).fetchall()
        for _sr in _shorts_rows:
            _sid = str(_sr["youtube_id"]).strip()
            if _sid and _sid not in db_index:
                db_index[_sid] = {"id": None, "yt_video_id": _sid, "is_short": True}
    except Exception as _e:
        logger.debug("[%s] Shorts indexing skipped: %s", slug, _e)

    # ── 6. Cross-reference ──────────────────────────────────
    matched = 0

    # 6a. Videos en YT que NO están en DB → orphaned_on_yt
    for yt_id, yt_v in yt_index.items():
        if yt_id in db_index:
            matched += 1
        else:
            result["orphaned_on_yt"].append({
                "yt_video_id": yt_id,
                "title": yt_v["snippet"]["title"],
                "published_at": yt_v["snippet"].get("publishedAt", ""),
                "privacy": yt_v["status"].get("privacyStatus", ""),
                "views": int(yt_v.get("statistics", {}).get("viewCount", 0)),
            })

    # 6b. Videos en DB que NO están en YT → deleted_manually
    for yt_id, db_v in db_index.items():
        if db_v.get("is_short"):
            continue  # skip shorts — they're not in the videos table
        if yt_id not in yt_index:
            result["deleted_manually"].append({
                "db_video_id": db_v["id"],
                "yt_video_id": yt_id,
                "title": db_v.get("titulo_final", ""),
                "status": db_v.get("status", ""),
            })

    result["stats"]["matched"] = matched

    # ── 7. Detect duplicates (same channel, similar title) ──
    dup_groups = _find_duplicates_by_title(all_yt_videos)
    for group in dup_groups:
        entries = []
        for v in group:
            yt_id = v.get("id", "")
            entries.append({
                "yt_video_id": yt_id,
                "title": v["snippet"]["title"],
                "published_at": v["snippet"].get("publishedAt", ""),
                "views": int(v.get("statistics", {}).get("viewCount", 0)),
                "in_db": yt_id in db_index,
                "db_video_id": db_index[yt_id]["id"] if yt_id in db_index else None,
                "db_status": db_index[yt_id].get("status", "") if yt_id in db_index else "",
            })
        result["duplicates"].append(entries)

    # ── 8. Detect missing thumbnails ────────────────────────
    for v in all_yt_videos:
        yt_id = v.get("id", "")
        thumbnails = v.get("snippet", {}).get("thumbnails", {})
        if not _check_thumbnail_custom(thumbnails):
            result["missing_thumbnail"].append({
                "yt_video_id": yt_id,
                "title": v["snippet"]["title"],
                "in_db": yt_id in db_index,
                "db_video_id": db_index[yt_id]["id"] if yt_id in db_index else None,
                "db_thumbnail_path": db_index[yt_id].get("thumbnail_path", "") if yt_id in db_index else "",
            })

    # ── 9. Detect stuck processing ──────────────────────────
    now_utc = datetime.now(timezone.utc)
    for v in all_yt_videos:
        yt_id = v.get("id", "")
        status = v.get("status", {})
        processing = status.get("processingStatus", "")
        privacy = status.get("privacyStatus", "")
        pub_at_raw = status.get("publishAt") or v["snippet"].get("publishedAt", "")

        if processing == "processing":
            # Check if it's been stuck > 24h
            try:
                if pub_at_raw:
                    pub_at = datetime.fromisoformat(pub_at_raw.replace("Z", "+00:00"))
                    hours_stuck = (now_utc - pub_at).total_seconds() / 3600
                else:
                    # Use publishedAt as fallback
                    raw = v["snippet"].get("publishedAt", "")
                    if raw:
                        pub_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        hours_stuck = (now_utc - pub_at).total_seconds() / 3600
                    else:
                        hours_stuck = None

                if hours_stuck is not None and hours_stuck > 24:
                    result["stuck_processing"].append({
                        "yt_video_id": yt_id,
                        "title": v["snippet"]["title"],
                        "hours_stuck": round(hours_stuck, 1),
                        "processing_status": processing,
                        "privacy_status": privacy,
                        "in_db": yt_id in db_index,
                        "db_video_id": db_index[yt_id]["id"] if yt_id in db_index else None,
                        "db_status": db_index[yt_id].get("status", "") if yt_id in db_index else "",
                    })
            except (ValueError, TypeError):
                pass

        # 9b. Publish failed — private with publishAt passed
        if privacy == "private":
            publish_at_raw = status.get("publishAt", "")
            if publish_at_raw:
                try:
                    publish_at = datetime.fromisoformat(publish_at_raw.replace("Z", "+00:00"))
                    if publish_at < now_utc:
                        hours_past = (now_utc - publish_at).total_seconds() / 3600
                        result["publish_failed"].append({
                            "yt_video_id": yt_id,
                            "title": v["snippet"]["title"],
                            "privacy": privacy,
                            "publish_at": publish_at_raw,
                            "hours_past": round(hours_past, 1),
                            "processing_status": processing,
                            "in_db": yt_id in db_index,
                            "db_video_id": db_index[yt_id]["id"] if yt_id in db_index else None,
                            "db_status": db_index[yt_id].get("status", "") if yt_id in db_index else "",
                        })
                except (ValueError, TypeError):
                    pass

    # ── 10. Log summary ─────────────────────────────────────
    dup_count = len(result["duplicates"])
    orphan_count = len(result["orphaned_on_yt"])
    ghost_count = len(result["deleted_manually"])
    no_thumb_count = len(result["missing_thumbnail"])
    stuck_count = len(result["stuck_processing"])
    fail_count = len(result["publish_failed"])

    if dup_count or orphan_count or ghost_count or no_thumb_count or stuck_count or fail_count:
        logger.warning(
            "[%s] ⚠️  Anomalías: %d dups, %d huérfanos YT, %d fantasmas DB, "
            "%d sin thumbnail, %d processing stuck, %d publish failed",
            slug, dup_count, orphan_count, ghost_count, no_thumb_count,
            stuck_count, fail_count,
        )
    else:
        logger.info("[%s] ✅ Sin anomalías detectadas", slug)

    return result


# ── Global report generation ────────────────────────────────────


def run_diagnostic(channel_slugs: list[str], json_only: bool = False) -> dict:
    """Run diagnostic on all (or one) channels. Returns full report dict."""
    db = ExtendedDatabase()
    channels_to_check = channel_slugs

    now_iso = datetime.now(timezone.utc).isoformat()
    report = {
        "generated_at": now_iso,
        "channels_checked": [],
        "total_anomalies": 0,
        "channels": {},
        "summary": {
            "duplicates": 0,
            "orphaned_on_yt": 0,
            "deleted_manually": 0,
            "missing_thumbnail": 0,
            "stuck_processing": 0,
            "publish_failed": 0,
            "total_quota_used": 0,
        },
    }

    for slug in channels_to_check:
        if not json_only:
            logger.info("━━━ [%s] Iniciando diagnóstico ━━━", slug)

        yt_service = _authenticate(slug)
        if yt_service is None:
            report["channels"][slug] = {"error": "auth_failed", "token_missing": True}
            continue

        ch_report = diagnose_channel(slug, yt_service, db)

        # Accumulate summary
        for key in ["duplicates", "orphaned_on_yt", "deleted_manually",
                     "missing_thumbnail", "stuck_processing", "publish_failed"]:
            report["summary"][key] += len(ch_report[key])

        report["summary"]["total_quota_used"] += ch_report["stats"]["quota_used"]
        report["channels_checked"].append(slug)
        report["channels"][slug] = ch_report

        # Small delay between channels to avoid rate limits
        time.sleep(1)

    # Total anomalies
    report["total_anomalies"] = (
        report["summary"]["duplicates"]
        + report["summary"]["orphaned_on_yt"]
        + report["summary"]["deleted_manually"]
        + report["summary"]["missing_thumbnail"]
        + report["summary"]["stuck_processing"]
        + report["summary"]["publish_failed"]
    )

    return report


def save_report(report: dict) -> Path:
    """Save report to JSON file. Returns path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = OUTPUT_DIR / f"diagnose_report_{ts}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return output_path


def print_summary(report: dict):
    """Print a human-readable summary to stdout."""
    s = report["summary"]
    print("\n" + "=" * 60)
    print("  DIAGNÓSTICO COMPLETO DE CANALES")
    print(f"  Generado: {report['generated_at']}")
    print(f"  Canales analizados: {', '.join(report['channels_checked'])}")
    print("=" * 60)
    print(f"  Quota total usada: ~{s['total_quota_used']} unidades")
    print(f"  Total anomalías:   {report['total_anomalies']}")
    print("-" * 60)
    print(f"  🔴 Duplicados (mismo título):      {s['duplicates']}")
    print(f"  🟡 Huérfanos YT (no en DB):        {s['orphaned_on_yt']}")
    print(f"  ⚪ Fantasmas DB (borrados manual):  {s['deleted_manually']}")
    print(f"  🟠 Sin thumbnail personalizado:     {s['missing_thumbnail']}")
    print(f"  🔵 Processing stuck (>24h):         {s['stuck_processing']}")
    print(f"  🟣 Publish fallido (private vencido):{s['publish_failed']}")
    print("=" * 60)

    # Per-channel details
    for slug, ch in report["channels"].items():
        if ch.get("error"):
            print(f"\n  ❌ {slug}: ERROR — {ch.get('error')}")
            continue
        stats = ch.get("stats", {})
        print(f"\n  📺 {slug}: {stats.get('yt_videos','?')} videos YT, "
              f"{stats.get('db_videos','?')} en DB, {stats.get('matched','?')} matched")
        for anomaly_type, entries in ch.items():
            if anomaly_type in ("stats", "channel", "error"):
                continue
            if entries:
                print(f"     {anomaly_type}: {len(entries)}")
                # Show first 3 examples
                for idx, entry in enumerate(entries[:3]):
                    if isinstance(entry, list):  # duplicates group
                        titles = [e.get("title", e.get("yt_video_id", "?"))[:60] for e in entry]
                        print(f"       → {len(entry)} copias: {', '.join(titles)}")
                        break  # one example per dup group
                    else:
                        title = entry.get("title", entry.get("yt_video_id", "?"))
                        print(f"       → {entry.get('yt_video_id','?')}: {str(title)[:70]}")
                if len(entries) > 3:
                    print(f"       ... y {len(entries)-3} más")


# ── Entry point ─────────────────────────────────────────────────


if __name__ == "__main__":
    from scripts.runtime_context import add_channel_selector_arguments, resolve_channels, SelectorError
    parser = argparse.ArgumentParser(description="Diagnóstico completo de canales YouTube")
    add_channel_selector_arguments(parser)
    parser.add_argument("--json-only", action="store_true",
                        help="Solo generar JSON de salida sin log verboso")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validar selector y mostrar alcance sin consumir cuota")
    parser.add_argument("--no-summary", action="store_true",
                        help="No imprimir resumen en pantalla")
    args = parser.parse_args()

    try:
        contexts = resolve_channels(
            channel_id=args.channel_id, slug=args.slug, project=args.project,
            all_channels=args.all_channels, yes=args.yes,
        )
    except SelectorError as exc:
        parser.error(str(exc))

    if args.dry_run:
        print(json.dumps({"channels": [c.slug for c in contexts]}, ensure_ascii=False))
        raise SystemExit(0)

    if args.json_only:
        logging.getLogger().setLevel(logging.WARNING)

    logger.info("Iniciando diagnóstico de canales...")
    report = run_diagnostic([c.slug for c in contexts], json_only=args.json_only)

    output_path = save_report(report)
    logger.info("Reporte guardado en: %s", output_path)

    if not args.no_summary:
        print_summary(report)

    if report["total_anomalies"] > 0:
        logger.warning("⚠️  Se encontraron %d anomalías. Revisa el reporte JSON para detalles.",
                       report["total_anomalies"])
        sys.exit(1)
    else:
        logger.info("✅ Sin anomalías — todos los canales limpios.")
        sys.exit(0)
