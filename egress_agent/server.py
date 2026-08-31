"""Servidor HTTP del agente egress.

Autentica cada petición con ``X-Agent-Token`` (el server principal lo conoce).
Todos los egress a YouTube/Google ocurren desde la red de la VPS.
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from egress_agent.config import AgentConfig
from egress_agent import browser as browser_mod
from egress_agent import youtube_api, scraper

logger = logging.getLogger("egress_agent.server")


class AgentApp:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.app = FastAPI(title=f"egress-agent-{cfg.slug}", version="1.0.0")
        self._register_routes()

    # ── Auth ───────────────────────────────────────────────────
    def _check_token(self, x_agent_token: Optional[str]) -> None:
        if not self.cfg.auth_token:
            return  # dev mode: token vacío → se permite
        if x_agent_token != self.cfg.auth_token:
            raise HTTPException(status_code=401, detail="token de agente inválido")

    # ── Modelos ────────────────────────────────────────────────
    class PingModel(BaseModel):
        pass

    class BrowserActionModel(BaseModel):
        action: str
        account: str = ""
        params: dict = {}

    class ApiOpModel(BaseModel):
        op: str
        params: dict = {}

    class YtdlpModel(BaseModel):
        op: str
        params: dict = {}

    class FetchModel(BaseModel):
        url: str
        timeout: int = 30

    class OAuthUrlModel(BaseModel):
        account: str = ""

    class OAuthCodeModel(BaseModel):
        account: str = ""
        code: str

    def _register_routes(self):
        app = self.app

        @app.get("/ping")
        def ping(x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            return {"ok": True, "slug": self.cfg.slug,
                    "egress_label": self.cfg.egress_label}

        @app.get("/egress-check")
        def egress_check(x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            return scraper.egress_check(self.cfg)

        @app.post("/browser/action")
        def browser_action(body: AgentApp.BrowserActionModel,
                           x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            return browser_mod.run_browser_action(self.cfg, body.action, body.params)

        @app.post("/api/call")
        def api_call(body: AgentApp.ApiOpModel,
                     x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            return youtube_api.run_api_operation(self.cfg, body.op, body.params)

        @app.post("/ytdlp")
        def ytdlp(body: AgentApp.YtdlpModel,
                  x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            return scraper.run_ytdlp(self.cfg, body.op, body.params)

        @app.post("/fetch")
        def fetch(body: AgentApp.FetchModel,
                  x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            return scraper.fetch(self.cfg, body.url, body.timeout)

        @app.post("/auth/oauth-url")
        def oauth_url(body: AgentApp.OAuthUrlModel,
                      x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            from pipeline.youtube_uploader import YouTubeUploader
            uploader = YouTubeUploader(account_name=self.cfg.slug, channel_slug=self.cfg.slug)
            url = uploader.get_auth_url()
            if not url:
                raise HTTPException(500, "no se pudo generar la URL de OAuth")
            return {"ok": True, "auth_url": url}

        @app.post("/auth/exchange")
        def auth_exchange(body: AgentApp.OAuthCodeModel,
                          x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            from pipeline.youtube_uploader import YouTubeUploader
            uploader = YouTubeUploader(account_name=self.cfg.slug, channel_slug=self.cfg.slug)
            ok = uploader.complete_auth_with_code(body.code)
            if not ok:
                raise HTTPException(400, "intercambio OAuth falló")
            return {"ok": True}

        @app.post("/upload")
        async def upload(video: UploadFile = File(...),
                         meta: str = Form("{}"),
                         thumbnail: Optional[UploadFile] = File(None),
                         x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            import json as _json
            try:
                meta_dict = _json.loads(meta)
            except _json.JSONDecodeError:
                meta_dict = {}

            upload_dir = Path(self.cfg.project_root) / "output" / "agent_uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d%H%M%S")
            run_id = uuid.uuid4().hex[:8]

            video_path = upload_dir / f"{ts}_{run_id}_{Path(video.filename or 'video').name}"
            with open(video_path, "wb") as out:
                while chunk := await video.read(1024 * 1024):
                    out.write(chunk)

            thumb_path = None
            if thumbnail:
                ext = Path(thumbnail.filename or "thumb.jpg").suffix or ".jpg"
                thumb_path = upload_dir / f"{ts}_{run_id}_thumb{ext}"
                with open(thumb_path, "wb") as out:
                    while chunk := await thumbnail.read(1024 * 1024):
                        out.write(chunk)
                meta_dict["thumbnail_path"] = str(thumb_path)

            try:
                return youtube_api.upload_video(self.cfg, str(video_path), meta_dict)
            finally:
                try:
                    video_path.unlink(missing_ok=True)
                    if thumb_path:
                        thumb_path.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass


def create_app(cfg: AgentConfig):
    return AgentApp(cfg).app
