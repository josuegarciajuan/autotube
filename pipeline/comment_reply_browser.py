"""Respuestas a comentarios vía navegador (watch page de YouTube) — 0 cuota de API.

Orquesta la lectura de comentarios públicos (watch page), la generación de la
respuesta humanizada (LLM, prompt _REPLY_SYSTEM) y la publicación vía el
cajón de respuesta de YouTube, con escritura carácter a carácter para parecer
humano. Sin cuota del Data API y sin necesidad de scope force-ssl.

Límites anti-spam:
  - Máx N respuestas por video/ronda (COMMENT_REPLY_MAX_PER_VIDEO).
  - Máx M respuestas/día por cuenta Google (COMMENT_REPLY_DAILY_CAP_PER_ACCOUNT),
    contadas desde comment_log (unión videos → channels.google_account).
Idempotencia: se registra cada respuesta en comment_log con un fingerprint del
comentario padre; nunca se responde dos veces al mismo comentario.
"""

import hashlib
import json
import logging
import random
import sqlite3
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from config.settings import DATABASE_PATH
from pipeline.youtube_comments import YouTubeCommentManager, _REPLY_SYSTEM


def _comment_fingerprint(text: str) -> str:
    """Fingerprint estable del texto de un comentario (para idempotencia)."""
    return hashlib.md5(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def count_replies_today_for_account(db, account: str) -> int:
    """Respuestas de hoy (comment_log type='reply') para una cuenta Google."""
    try:
        conn = sqlite3.connect(str(DATABASE_PATH))
        conn.row_factory = sqlite3.Row
        today = datetime.now(timezone.utc).date().isoformat()
        row = conn.execute(
            """SELECT COUNT(*) AS n
               FROM comment_log cl
               JOIN videos v ON cl.video_id = v.id
               JOIN channels c ON v.channel_id = c.id
               WHERE cl.comment_type = 'reply'
                 AND date(cl.posted_at) = ?
                 AND c.google_account = ?""",
            (today, account),
        ).fetchone()
        conn.close()
        return row["n"] if row else 0
    except Exception as exc:
        logger.warning("count_replies_today_for_account(%s) failed: %s", account, exc)
        return 0


def _already_replied_fingerprints(db, db_video_id: int) -> set:
    """Fingerprints de comentarios ya respondidos para este video (local)."""
    out = set()
    try:
        for row in db.get_video_comments_log(db_video_id):
            if row.get("comment_type") == "reply" and row.get("parent_comment_id"):
                out.add(row["parent_comment_id"])
    except Exception as exc:
        logger.warning("already_replied read failed: %s", exc)
    return out


def reply_to_video_comments(channel_slug: str, yt_video_id: str,
                            db_video_id: int, max_replies: int = None) -> dict:
    """Responde a comentarios de espectadores vía navegador (0 cuota).

    Args:
        channel_slug: slug del canal (ej. "canal2").
        yt_video_id: ID del video en YouTube.
        db_video_id: ID local del video (para comment_log).
        max_replies: override del cap por video (default config).

    Returns:
        dict con {posted, skipped, failed, reasons...}.
    """
    from config.settings import (
        COMMENT_REPLY_DAILY_CAP_PER_ACCOUNT,
        COMMENT_REPLY_MAX_PER_VIDEO,
    )
    from database.db_extended import ExtendedDatabase
    from pipeline.youtube_browser import get_browser, get_account_for_channel

    db = ExtendedDatabase()
    ch = db.get_channel_by_slug(channel_slug)
    if not ch:
        return {"error": f"canal {channel_slug} no encontrado", "posted": 0}

    account = get_account_for_channel(channel_slug) or channel_slug
    channel_name = (ch.get("name") or "").strip()

    # ── Delegación al agente egress (canal gestionado) ──
    from api.services.egress_delegation import egress_client_for
    _egress = egress_client_for(channel_slug)

    # ── Cap diario por cuenta ──
    used_today = count_replies_today_for_account(db, account)
    remaining_daily = max(0, COMMENT_REPLY_DAILY_CAP_PER_ACCOUNT - used_today)
    if remaining_daily <= 0:
        logger.info("[%s] Cap diario alcanzado (%d/%d) — skip",
                    channel_slug, used_today, COMMENT_REPLY_DAILY_CAP_PER_ACCOUNT)
        return {"posted": 0, "skipped": 0, "failed": 0, "daily_cap_hit": True}

    cap = max_replies or COMMENT_REPLY_MAX_PER_VIDEO
    cap = max(1, min(int(cap), remaining_daily))

    # ── Leer comentarios ──
    if _egress is not None:
        _r = _egress.browser_action("comments", account=account,
                                    params={"video_id": yt_video_id, "max_comments": 60})
        comments = _r.get("result", []) if _r.get("ok") else []
    else:
        browser = get_browser(account)
        comments = browser.list_video_comments(yt_video_id, max_comments=60)
    if not comments:
        logger.info("[%s] Sin comentarios visibles en %s", channel_slug, yt_video_id)
        return {"posted": 0, "skipped": 0, "failed": 0, "no_comments": True}

    already = _already_replied_fingerprints(db, db_video_id)

    # ── Filtrar elegibles ──
    eligible = []
    for c in comments:
        fp = _comment_fingerprint(c["text"])
        if fp in already:
            continue
        if channel_name and any(
            a.strip().lower() == channel_name.lower()
            for a in c.get("reply_authors", [])
        ):
            continue
        text = (c.get("text") or "").strip()
        if len(text) < 10:
            continue
        if "http://" in text or "https://" in text:
            continue
        if not c.get("has_reply_button"):
            continue
        eligible.append(c)

    if not eligible:
        logger.info("[%s] %s: sin comentarios elegibles (%d leídos)",
                    channel_slug, yt_video_id, len(comments))
        return {"posted": 0, "skipped": 0, "failed": 0, "nothing_eligible": True}

    # ── Selección aleatoria (natural, no predecible) ──
    random.shuffle(eligible)
    n_target = random.randint(1, min(cap, len(eligible)))
    targets = eligible[:n_target]
    logger.info("[%s] %s: %d comentarios leídos, %d elegibles, respondiendo a %d",
                channel_slug, yt_video_id, len(comments), len(eligible), n_target)

    # ── Generador LLM (reutiliza prompt humanizado, sin auth) ──
    llm = YouTubeCommentManager(channel_slug)
    system_prompt = _REPLY_SYSTEM.format(channel_tone=llm._get_channel_tone())

    posted = skipped = failed = 0
    for c in targets:
        reply_text = llm._generate_comment_text(
            system_prompt,
            f"COMENTARIO DEL ESPECTADOR: {c['text']}\n\nEscribe tu respuesta:",
            temperature=0.85,
        )
        if not reply_text:
            failed += 1
            continue

        if _egress is not None:
            _r2 = _egress.browser_action(
                "reply_comment", account=account,
                params={"video_id": yt_video_id, "comment_index": c["index"],
                        "text": reply_text, "expected_text": c["text"]},
            )
            ok = bool(_r2.get("ok"))
        else:
            ok = browser.post_comment_reply(
                yt_video_id, c["index"], reply_text, expected_text=c["text"]
            )
        if ok:
            try:
                db.log_comment(
                    video_id=db_video_id,
                    yt_video_id=yt_video_id,
                    yt_comment_id="browser",
                    comment_type="reply",
                    parent_comment_id=_comment_fingerprint(c["text"]),
                    comment_text=reply_text,
                )
            except Exception as exc:
                logger.warning("comment_log insert failed: %s", exc)
            posted += 1
        else:
            failed += 1

        # Pausa natural entre respuestas
        time.sleep(random.uniform(4.0, 10.0))

    logger.info("[%s] Comentarios: %d publicados, %d fallos (%s)",
                channel_slug, posted, failed, yt_video_id)
    return {
        "posted": posted,
        "skipped": skipped,
        "failed": failed,
        "read": len(comments),
        "eligible": len(eligible),
        "account": account,
        "remaining_daily": remaining_daily - posted,
    }
