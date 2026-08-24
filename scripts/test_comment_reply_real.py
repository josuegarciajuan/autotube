#!/usr/bin/env python3
"""Prueba REAL end-to-end del mecanismo de comentarios (0 cuota Data API).

1. Publica un primer comentario como el canal en un video propio.
2. Le responde con el flujo de respuesta por navegador (typing humano).
3. Verifica que la respuesta quedó publicada y lo registra en comment_log.

Uso:
  python3 scripts/test_comment_reply_real.py --account tracatrack \
      --video-id qSKamnFXnAs --canal canal3 --db-video-id 2096 \
      --channel-name "Civilizaciones Olvidadas"
"""

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("test_comment_reply_real")

from pipeline.youtube_browser import YouTubeBrowser, human_delay
from pipeline.youtube_comments import (
    YouTubeCommentManager,
    _REPLY_SYSTEM,
    _FIRST_COMMENT_SYSTEM,
)


def wait_rotate(browser, page):
    if not browser._wait_rotate_cookies(page):
        logger.warning("Rotación de cookies sin terminar, continúo igualmente")


def goto_comments(browser, page, video_id):
    page.goto(f"https://www.youtube.com/watch?v={video_id}",
              wait_until="domcontentloaded", timeout=60000)
    wait_rotate(browser, page)
    human_delay(4.0, 6.0, "load")
    page.evaluate('() => document.getElementById("comments")?.scrollIntoView()')
    human_delay(2.0, 3.0, "scroll")


def post_first_comment(browser, page, text: str) -> bool:
    """Publica un comentario top-level como el canal (composer de la watch page)."""
    # Abrir el composer
    opened = page.evaluate(
        "() => document.querySelector('ytd-comments #placeholder-area')?.click()"
    )
    human_delay(1.5, 2.5, "composer open")
    ce = page.locator("ytd-comments #contenteditable-root, "
                      "ytd-commentbox #contenteditable-root, "
                      "#contenteditable-root").first
    if ce.count() == 0:
        logger.error("Contenteditable del composer no encontrado")
        return False
    ce.click()
    human_delay(0.5, 1.0, "focus")
    for ch in text:
        page.keyboard.type(ch)
        time.sleep(0.04 + (0.05 if ch in ".,!?¿" else 0))
    human_delay(1.0, 2.0, "typed")

    res = page.evaluate("""() => {
        const cd = document.querySelector('ytd-comments #comment-dialog, #comment-dialog');
        if (!cd) return 'no-dialog';
        const btn = Array.from(cd.querySelectorAll('button')).find(b =>
            (b.textContent||'').trim() === 'Comentar' ||
            (b.textContent||'').trim() === 'Comment' ||
            b.getAttribute('aria-label') === 'Comentar' ||
            b.getAttribute('aria-label') === 'Comment');
        if (!btn) return 'no-btn';
        if (btn.disabled) return 'disabled';
        btn.click();
        return 'posted';
    }""")
    if res != "posted":
        logger.error("Primer comentario no publicado: %s", res)
        return False
    human_delay(3.0, 5.0, "first comment posted")
    logger.info("✅ Primer comentario publicado")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--video-id", required=True)
    ap.add_argument("--canal", required=True)
    ap.add_argument("--db-video-id", type=int, required=True)
    ap.add_argument("--channel-name", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--post-first", action="store_true",
                    help="Publicar el primer comentario (si no, solo responder al primero existente)")
    ap.add_argument("--target-prefix", default="",
                    help="Prefijo de texto para elegir el comentario a responder")
    args = ap.parse_args()

    browser = YouTubeBrowser(args.account)
    browser._ensure_browser()
    page = browser._context.new_page()
    goto_comments(browser, page, args.video_id)

    llm = YouTubeCommentManager(args.canal)

    first_text = None
    if args.post_first:
        # Generar primer comentario con el prompt de first_comment (contexto = título)
        user_p = f"CONTENIDO DEL VIDEO:\nTítulo: {args.title or 'video'}\n\nEscribe el comentario."
        first_text = llm._generate_comment_text(_FIRST_COMMENT_SYSTEM, user_p, temperature=0.9)
        if not first_text:
            first_text = ("¿Qué opináis de este tema? ¿Conocíais esta teoría? "
                          "Cuéntalo en los comentarios 👇")
        logger.info("Primer comentario generado: %s", first_text[:90])
        ok = post_first_comment(browser, page, first_text)
        if not ok:
            page.close(); browser.close(); sys.exit(1)

    # ── Leer comentarios y localizar el primero (nuestro o el de un espectador) ──
    human_delay(1.0, 2.0, "pre-read")
    comments = browser._parse_comment_threads(page, max_comments=20)
    if not comments:
        # recargar para asegurar estado fresco
        goto_comments(browser, page, args.video_id)
        comments = browser._parse_comment_threads(page, max_comments=20)
    logger.info("Comentarios leídos: %d", len(comments))
    for c in comments[:5]:
        logger.info("  [%d] @%s: %s", c["index"], c["author"], c["text"][:60])

    if not comments:
        logger.error("No hay comentarios para responder")
        page.close(); browser.close(); sys.exit(1)

    # Elegir objetivo: si publicamos el primero, nuestro comentario (autor = canal)
    target = None
    if args.target_prefix:
        pre = args.target_prefix.strip().lower()[:30]
        for c in comments:
            if pre and pre in c["text"].strip().lower():
                target = c
                break
    if not target and args.post_first and first_text:
        norm = first_text.strip().lower()[:40]
        for c in comments:
            if norm[:25] and norm[:25] in c["text"].strip().lower():
                target = c
                break
    if not target:
        target = comments[0]
    logger.info("➡️  Objetivo: [%d] @%s: %s", target["index"], target["author"],
                target["text"][:70])

    # ── Generar respuesta humanizada vía LLM ──
    system_prompt = _REPLY_SYSTEM.format(channel_tone=llm._get_channel_tone())
    reply_text = llm._generate_comment_text(
        system_prompt,
        f"COMENTARIO DEL ESPECTADOR: {target['text']}\n\nEscribe tu respuesta:",
        temperature=0.85,
    )
    if not reply_text:
        reply_text = "Que bueno que te haya gustado! Me alegra mucho que lo hayas visto entero"
    logger.info("💬 Respuesta generada: %s", reply_text[:100])

    # ── Publicar la respuesta con el mecanismo del pipeline ──
    page.close()  # cerrar sesión de página, post_comment_reply abre la suya
    ok_reply = browser.post_comment_reply(
        args.video_id, target["index"], reply_text, expected_text=target["text"]
    )
    if not ok_reply:
        logger.error("❌ La respuesta NO se publicó")
        browser.close(); sys.exit(1)

    # ── Verificación: releer y confirmar que el hilo tiene respuesta del canal ──
    human_delay(3.0, 5.0, "pre-verify")
    verify = browser.list_video_comments(args.video_id, max_comments=20)
    vtarget = next((c for c in verify if c["index"] == target["index"]), None)
    matched = False
    if vtarget:
        has_our_reply = any(
            a.strip().lower() == args.channel_name.lower()
            for a in vtarget.get("reply_authors", [])
        )
        logger.info("🔎 Verificación hilo [%d]: reply_authors=%s",
                    vtarget["index"], vtarget.get("reply_authors"))
        matched = has_our_reply
    if not matched:
        # buscar el texto de la respuesta en el hilo
        for c in verify:
            if reply_text.strip().lower()[:30] in c["text"].strip().lower():
                matched = True
                break

    # ── Registrar en comment_log ──
    from database.db_extended import ExtendedDatabase
    db = ExtendedDatabase()
    log_id = None
    if matched:
        try:
            log_id = db.log_comment(
                video_id=args.db_video_id,
                yt_video_id=args.video_id,
                yt_comment_id="browser",
                comment_type="reply",
                parent_comment_id="test:" + args.video_id,
                comment_text=reply_text,
            )
        except Exception as exc:
            logger.warning("comment_log insert falló: %s", exc)

    print("\n" + "=" * 60)
    print("RESULTADO PRUEBA REAL:")
    print(f"  Video:        {args.video_id}")
    print(f"  Comentario objetivo: @{target['author']}: {target['text'][:50]!r}")
    print(f"  Respuesta:    {reply_text!r}")
    print(f"  Publicada:    {'SÍ' if ok_reply else 'NO'}")
    print(f"  Verificada en la página: {'SÍ' if matched else 'NO'}")
    print(f"  comment_log id: {log_id}")
    print("=" * 60)

    browser.close()
    return 0 if (ok_reply and matched) else 1


if __name__ == "__main__":
    sys.exit(main())
