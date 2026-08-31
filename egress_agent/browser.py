"""Dispatcher de acciones de navegador (Studio / watch page) para el agente.

Reutiliza ``pipeline.youtube_browser`` pero lanzado desde la VPS del agente,
con huella por cuenta (``fingerprint``) y, opcionalmente, proxy Playwright.
La IP de salida la determina la red de la VPS (túnel a IP residencial o IP propia).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from egress_agent.config import AgentConfig

logger = logging.getLogger("egress_agent.browser")

# Mapa acción -> (método de YouTubeBrowser, nombre de arg del video_id)
_ACTIONS = {
    "mark_altered": ("mark_altered_content", "video_id"),
    "end_screens": ("add_end_screens", "video_id"),
    "hold_private": ("set_video_private_unschedule", "video_id"),
    "link_longform": ("link_longform_video", None),  # args: short_yt_id, longform_yt_id
}


def _get_browser(cfg: AgentConfig, account: str = ""):
    from pipeline.youtube_browser import get_browser, close_all_browsers

    acc = account or cfg.google_account
    if not acc:
        raise ValueError("account no resuelto (falta google_account o param account)")
    fp = cfg.fingerprint or {}
    proxy = cfg.playwright_proxy or None
    browser = get_browser(acc, fingerprint=fp, proxy=proxy)
    return browser, close_all_browsers


def run_browser_action(cfg: AgentConfig, action: str, params: dict) -> dict:
    """Ejecuta una acción de navegador. Devuelve {ok, result|error}."""
    _KNOWN = {"studio_scan", "comments", "reply_comment",
              "collab_search", "collab_channel_videos"} | set(_ACTIONS)
    if action not in _KNOWN:
        return {"ok": False, "error": f"Acción de navegador desconocida: {action}"}

    account = params.get("account") or cfg.google_account
    browser, _close_all = _get_browser(cfg, account)

    try:
        if action == "studio_scan":
            return _studio_scan(cfg, browser, params)
        if action == "comments":
            return _comments(cfg, browser, params)
        if action == "reply_comment":
            return _reply_comment(cfg, browser, params)
        if action == "collab_search":
            return {"ok": True, "result": browser.search_channels(
                params.get("keyword", ""),
                max_results=int(params.get("max_results", 5)),
            )}
        if action == "collab_channel_videos":
            return {"ok": True, "result": browser.get_channel_videos(
                params.get("channel_url", ""),
                limit=int(params.get("limit", 3)),
            )}

        if action in _ACTIONS:
            method_name, vid_arg = _ACTIONS[action]
            method = getattr(browser, method_name)
            if method_name == "link_longform_video":
                ok = method(params.get("short_yt_id", ""), params.get("longform_yt_id", ""))
            else:
                ok = method(params.get(vid_arg or "video_id", ""))
            return {"ok": bool(ok), "result": ok}

        return {"ok": False, "error": f"Acción de navegador desconocida: {action}"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("browser action %s failed", action)
        return {"ok": False, "error": str(exc)[:1000]}


def _studio_scan(cfg: AgentConfig, browser, params: dict) -> dict:
    """Escanea Studio del canal y extrae posibles restricciones (0 cuota)."""
    uc = params.get("yt_channel_id", "")
    if not uc:
        return {"ok": False, "error": "falta yt_channel_id"}
    browser._ensure_browser()
    page = browser._context.new_page()
    try:
        page.goto(f"https://studio.youtube.com/channel/{uc}",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(10000)
        body = page.evaluate("() => document.body ? document.body.innerText : ''")
        findings = []
        patterns = ["strike", "aviso por", "advertencia", "restricci", "monetizaci",
                    "desmonetiza", "suspendida", "suspensión", "violaci", "reclamaci",
                    "recurrente", "no cumple", "sintético", "sintetico", "engañosa"]
        for line in body.splitlines():
            ll = line.strip().lower()
            if len(ll) > 3 and any(p in ll for p in patterns):
                findings.append(line.strip()[:220])
        return {"ok": True, "result": {"findings": findings}}
    except Exception as exc:  # noqa: BLE001
        logger.exception("studio_scan failed")
        return {"ok": False, "error": str(exc)[:1000]}
    finally:
        try:
            page.close()
        except Exception:
            pass


def _comments(cfg: AgentConfig, browser, params: dict) -> dict:
    video_id = params.get("video_id", "")
    if not video_id:
        return {"ok": False, "error": "falta video_id"}
    comments = browser.list_video_comments(
        video_id, max_comments=int(params.get("max_comments", 50)),
    )
    return {"ok": True, "result": comments}


def _reply_comment(cfg: AgentConfig, browser, params: dict) -> dict:
    video_id = params.get("video_id", "")
    idx = int(params.get("comment_index", 0))
    text = params.get("text", "")
    expected = params.get("expected_text")
    if not video_id or not text:
        return {"ok": False, "error": "faltan video_id/text"}
    ok = browser.post_comment_reply(video_id, idx, text, expected_text=expected)
    return {"ok": bool(ok), "result": ok}


def _detect_webrtc_disabled(page) -> bool:
    """Best-effort: True si el navegador NO expone IPs locales vía WebRTC.

    La política ``--force-webrtc-ip-handling-policy=disable_non_proxied_udp``
    debería impedirlo; este evaluador lo comprueba en runtime intentando
    enumerar candidatos ICE. Si no aparece ninguna IP no-mDNS → seguro.
    """
    js = """
    () => new Promise((resolve) => {
      try {
        const pc = new RTCPeerConnection({
          iceServers: [{urls: ['stun:stun.l.google.com:19302']}]
        });
        const ips = new Set();
        pc.onicecandidate = (e) => {
          if (e && e.candidate) {
            const c = e.candidate.candidate || '';
            if (c.indexOf('.local') === -1) {
              const parts = c.split(' ');
              const i = parts.indexOf('raddr');
              if (i !== -1 && parts[i+1] && parts[i+1] !== '0.0.0.0') {
                ips.add(parts[i+1]);
              }
            }
          } else {
            resolve(Array.from(ips));
          }
        };
        pc.createDataChannel('probe');
        pc.createOffer().then(o => pc.setLocalDescription(o)).catch(() => resolve([]));
        setTimeout(() => resolve(Array.from(ips)), 4000);
      } catch (e) { resolve([]); }
    })
    """
    try:
        found = page.evaluate(js)
        return not bool(found)  # sin IPs no-mDNS expuestas → seguro
    except Exception:
        return True  # si no se puede evaluar, asumir seguro


def browser_egress_probe(cfg: AgentConfig) -> dict:
    """Verifica el egress REAL del navegador (no solo curl).

    Lanza una página headless vía Playwright POR el proxy residencial y devuelve
    la IP que ve la página (``browser_ip``) y si WebRTC está desactivado
    (``webrtc_disabled``). Si la IP del navegador no coincide con la esperada,
    hay una fuga de capa-browser que el probe curl no detectaría.
    """
    page = None
    try:
        browser, _close_all = _get_browser(cfg, cfg.google_account or cfg.slug)
        browser._ensure_browser()  # inicializa el contexto persistente
        page = browser._context.new_page()
        page.goto("https://api.ipify.org?format=json",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        body = page.evaluate("() => document.body ? document.body.innerText : ''")
        import json as _json
        try:
            data = _json.loads(body)
            browser_ip = str(data.get("ip", "")).strip()
        except Exception:  # noqa: BLE001
            browser_ip = body.strip()
        webrtc_disabled = _detect_webrtc_disabled(page)
        expected = (cfg.expected_ip or "").strip()
        return {
            "ok": True,
            "result": {
                "browser_ip": browser_ip,
                "expected_ip": expected,
                "webrtc_disabled": webrtc_disabled,
                "match": bool(browser_ip) and browser_ip == expected,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:1000]}
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:  # noqa: BLE001
            pass
