#!/usr/bin/env python3
"""Elimina un comentario propio vía watch page usando locators de Playwright
(que atraviesan shadow DOM, a diferencia de page.evaluate/querySelectorAll).

Uso:
  python3 scripts/delete_comment.py --account tracatrack --video-id qSKamnFXnAs \
      --prefix "Uy, con este me has dado"
"""

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("delete_comment")

from pipeline.youtube_browser import YouTubeBrowser, human_delay
from playwright.sync_api import TimeoutError as PlaywrightTimeout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--video-id", required=True)
    ap.add_argument("--prefix", required=True)
    args = ap.parse_args()

    b = YouTubeBrowser(args.account)
    b._ensure_browser()
    page = b._context.new_page()
    page.goto(f"https://www.youtube.com/watch?v={args.video_id}",
              wait_until="domcontentloaded", timeout=60000)
    b._wait_rotate_cookies(page)
    human_delay(4.0, 6.0, "load")
    page.evaluate('() => document.getElementById("comments")?.scrollIntoView()')
    human_delay(2.0, 3.0, "scroll")

    # 1. Localizar el hilo por prefijo de texto
    threads = page.locator("ytd-comment-thread-renderer")
    n = threads.count()
    target = None
    for i in range(n):
        txt = ""
        try:
            txt = threads.nth(i).locator("#content-text").first.inner_text() or ""
        except Exception:
            pass
        if txt.strip().startswith(args.prefix):
            target = threads.nth(i)
            logger.info("Hilo encontrado en índice %d", i)
            break
    if target is None:
        logger.error("No se encontró comentario con prefijo %r", args.prefix)
        page.close(); b.close(); sys.exit(1)

    # 2. Abrir menú de acciones
    try:
        target.locator(
            "yt-icon-button[aria-label*='acciones'], "
            "yt-icon-button[aria-label*='actions'], "
            "ytd-menu-renderer #button"
        ).first.click(timeout=8000)
    except Exception as e:
        logger.error("No se pudo abrir el menú de acciones: %s", e)
        page.close(); b.close(); sys.exit(1)
    human_delay(2.0, 3.0, "menu open")

    # 3. Click en 'Eliminar' (text locator atraviesa shadow DOM)
    try:
        page.locator("text=Eliminar").first.click(timeout=8000)
        logger.info("Click en Eliminar OK")
    except PlaywrightTimeout:
        try:
            page.locator("text=Delete").first.click(timeout=5000)
            logger.info("Click en Delete OK")
        except PlaywrightTimeout:
            logger.error("Item Eliminar/Delete no encontrado en el menú")
            page.close(); b.close(); sys.exit(1)
    human_delay(3.0, 4.0, "confirm dialog")

    # 4. Confirmar en el diálogo ("Eliminar comentario | ... | Cancelar | Eliminar")
    confirmed = False
    try:
        dlg = page.locator("tp-yt-paper-dialog:visible, [role='dialog']:visible").first
        if dlg.count() > 0:
            logger.info("Diálogo detectado: %s",
                        (dlg.inner_text() or "").replace("\n", " | ")[:80])
            # El botón de confirmar es el 'Eliminar' EXACTO (el título es 'Eliminar comentario')
            confirm_btn = dlg.get_by_text("Eliminar", exact=True)
            if confirm_btn.count() == 0:
                confirm_btn = dlg.get_by_text("Delete", exact=True)
            if confirm_btn.count() > 0:
                confirm_btn.last.click(timeout=5000)
                confirmed = True
                logger.info("Confirmación OK (Eliminar)")
    except Exception as e:
        logger.error("Error confirmando: %s", e)

    if not confirmed:
        try:
            page.locator("text=Eliminar").last.click(timeout=4000)
            confirmed = True
            logger.info("Confirmación fallback OK")
        except Exception:
            logger.error("No se pudo confirmar el borrado")
            page.close(); b.close(); sys.exit(1)

    human_delay(4.0, 6.0, "after delete")

    # 5. Verificación
    comments = b.list_video_comments(args.video_id, max_comments=15)
    still = any(c["text"].strip().startswith(args.prefix) for c in comments)
    if still:
        logger.warning("⚠️  El comentario sigue visible tras eliminar")
        ok = False
    else:
        logger.info("✅ Comentario eliminado correctamente")
        ok = True
    page.close(); b.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
