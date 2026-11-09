"""Servidor HTTP del agente egress.

Autentica cada petición con ``X-Agent-Token`` (el server principal lo conoce).
Todos los egress a YouTube/Google ocurren desde la red de la VPS.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
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


class EgressGuardError(Exception):
    """Fallo de verificación de egress (IP residencial no esperada/indeterminada)."""


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

    # ── Guardia de egress por operación (cierra la ventana del monitor) ──
    _egress_verify_cache = {"ts": 0.0, "ok": False}
    _EGRESS_VERIFY_TTL = 20  # segundos

    def _probe_egress_ip(self) -> str:
        """IP real de salida del agente, consultada A TRAVÉS del proxy residencial."""
        import requests as _req
        for url in ("https://api.ipify.org?format=json", "https://ipwho.is/"):
            try:
                r = _req.get(url, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict) and data.get("ip"):
                        return str(data["ip"])
            except Exception:  # noqa: BLE001
                continue
        return ""

    def _require_egress(self) -> None:
        """Fail-closed: antes de tocar YT, confirma que la salida es la IP esperada.

        - Si no hay ``expected_ip`` configurado (misconfiguration), se rechaza la
          operación: un agente sin IP esperada NO puede verificar su egress, y
          permitir la operación dejaría la salida sin verificar (riesgo de usar
          la IP del datacenter del VPS). Ya no se hace skip silencioso.
        - Si la IP cambió/caducó (o no se puede verificar), se rechaza la
          operación: jamás se toca YouTube desde una IP distinta a la residencial.
        """
        if not self.cfg.egress_verify:
            return  # opt-out explícito (solo desarrollo/test, NUNCA en producción)
        expected = (self.cfg.expected_ip or "").strip()
        if not expected:
            raise EgressGuardError(
                "expected_ip no configurado en agent_config.json — la salida del "
                "agente NO está verificada. Rellene expected_ip (IP residencial) "
                "en el VPS, o el agente no debe operar (fail-closed)."
            )
        now = time.time()
        cache = AgentApp._egress_verify_cache
        if now - cache["ts"] < AgentApp._EGRESS_VERIFY_TTL and cache["ok"]:
            return
        ip = self._probe_egress_ip()
        ok = bool(ip) and ip == expected
        cache["ts"] = now
        cache["ok"] = ok
        if not ok:
            raise EgressGuardError(
                f"egress IP no verificada (actual={ip or 'desconocida'}, "
                f"esperada={expected}) — IP caducada, renovar en Geonix. "
                f"Operación bloqueada (fail-closed)."
            )

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

    class CleanupModel(BaseModel):
        staged_path: str = ""

    def _register_routes(self):
        app = self.app

        @app.exception_handler(EgressGuardError)
        async def _egress_guard_handler(_req, exc):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=200, content={"ok": False, "error": str(exc)})

        @app.get("/ping")
        def ping(x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            return {"ok": True, "slug": self.cfg.slug,
                    "egress_label": self.cfg.egress_label}

        @app.get("/egress-check")
        def egress_check(x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            return scraper.egress_check(self.cfg)

        @app.get("/healthz")
        def healthz(x_agent_token: Optional[str] = Header(None)):
            """Health completo del agente en una sola llamada (para monitor/CI)."""
            self._check_token(x_agent_token)
            expected = (self.cfg.expected_ip or "").strip()
            ip = ""
            try:
                self._require_egress()
                ip = self._probe_egress_ip()
                egress_ok = bool(ip) and ip == expected
            except EgressGuardError:
                egress_ok = False
            try:
                free_gb = round(shutil.disk_usage(str(self.cfg.staging_dir)).free / (1024 ** 3), 2)
            except OSError:
                free_gb = None
            return {
                "ok": True,
                "slug": self.cfg.slug,
                "egress_label": self.cfg.egress_label,
                "token_valid": True,
                "expected_ip": expected,
                "egress_ip": ip,
                "egress_ok": egress_ok,
                "staging_gb": round(self._staging_gb(), 3),
                "staging_max_gb": self.cfg.staging_max_gb,
                "disk_free_gb": free_gb,
            }

        @app.get("/egress-check-browser")
        def egress_check_browser(x_agent_token: Optional[str] = Header(None)):
            """Verifica el egress REAL del navegador (no solo curl): lanza una
            página headless vía Playwright por el proxy residencial y devuelve
            la IP que ve la página + estado de WebRTC (fuga anti-detección)."""
            self._check_token(x_agent_token)
            return browser_mod.browser_egress_probe(self.cfg)

        @app.post("/browser/action")
        def browser_action(body: AgentApp.BrowserActionModel,
                           x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            self._require_egress()
            return browser_mod.run_browser_action(self.cfg, body.action, body.params)

        @app.post("/api/call")
        def api_call(body: AgentApp.ApiOpModel,
                     x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            self._require_egress()
            return youtube_api.run_api_operation(self.cfg, body.op, body.params)

        @app.post("/ytdlp")
        def ytdlp(body: AgentApp.YtdlpModel,
                  x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            self._require_egress()
            return scraper.run_ytdlp(self.cfg, body.op, body.params)

        @app.post("/fetch")
        def fetch(body: AgentApp.FetchModel,
                  x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            self._require_egress()
            return scraper.fetch(self.cfg, body.url, body.timeout)

        @app.post("/fetch-bytes")
        def fetch_bytes(body: AgentApp.FetchModel,
                        x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            self._require_egress()
            return scraper.fetch_bytes(self.cfg, body.url, body.timeout)

        @app.get("/download")
        def download(path: str, x_agent_token: Optional[str] = Header(None)):
            """Sirve un fichero descargado por el agente (p.ej. audio yt-dlp).

            Restringido al directorio temporal del sistema para no exponer
            ficheros arbitrarios del VPS.
            """
            self._check_token(x_agent_token)
            import tempfile
            from fastapi.responses import FileResponse

            base = Path(tempfile.gettempdir()).resolve()
            target = Path(path).resolve()
            if base != target and base not in target.parents:
                raise HTTPException(400, "ruta fuera del directorio temporal")
            if not target.is_file():
                raise HTTPException(404, "fichero no encontrado")
            return FileResponse(str(target), filename=target.name,
                                media_type="application/octet-stream")

        @app.post("/auth/oauth-url")
        def oauth_url(body: AgentApp.OAuthUrlModel,
                      x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            self._require_egress()
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
            self._require_egress()
            from pipeline.youtube_uploader import YouTubeUploader
            uploader = YouTubeUploader(account_name=self.cfg.slug, channel_slug=self.cfg.slug)
            ok = uploader.complete_auth_with_code(body.code)
            if not ok:
                raise HTTPException(400, "intercambio OAuth falló")
            return {"ok": True}

        @app.post("/stage")
        async def stage(video: UploadFile = File(...),
                        ref: str = Form(""),
                        thumbnail: Optional[UploadFile] = File(None),
                        x_agent_token: Optional[str] = Header(None)):
            """Guarda el vídeo + thumb en el VPS SIN subir (estado intermedio).

            Devuelve {ok, staged_path, thumbnail_path} para que el server lo
            envíe después a /upload (transferencia previa de generación→VPS).
            """
            self._check_token(x_agent_token)
            self._require_egress()
            self._purge_staging()
            paths = self._save_upload(video, thumbnail, ref)
            return {"ok": True, **paths}

        @app.post("/upload")
        async def upload(video: Optional[UploadFile] = File(None),
                         meta: str = Form("{}"),
                         staged_path: str = Form(""),
                         thumbnail: Optional[UploadFile] = File(None),
                         x_agent_token: Optional[str] = Header(None)):
            self._check_token(x_agent_token)
            self._require_egress()
            self._purge_staging()
            import json as _json
            try:
                meta_dict = _json.loads(meta)
            except _json.JSONDecodeError:
                meta_dict = {}

            # ── Idempotencia: si este ref ya se subió, devolver el video_id ──
            upload_ref = str(meta_dict.get("upload_ref") or "")
            if upload_ref:
                cached = self._ledger_get(upload_ref)
                if cached and cached.get("status") == "done":
                    logger.info("upload idempotente: ref=%s ya completado", upload_ref)
                    return {"ok": True, **cached}

            # Subida desde un archivo ya stageado (transferencia previa).
            if staged_path:
                thumb_path = meta_dict.pop("thumbnail_path", None)
                meta_dict["thumbnail_path"] = thumb_path
                try:
                    return self._upload_and_record(staged_path, meta_dict)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("upload (staged) failed")
                    return {"ok": False, "error": str(exc)[:1000]}
            if video is None:
                raise HTTPException(400, "falta video o staged_path")
            paths = self._save_upload(video, thumbnail, ref=meta_dict.get("ref", ""))
            try:
                meta_dict["thumbnail_path"] = paths.get("thumbnail_path")
                return self._upload_and_record(paths["staged_path"], meta_dict)
            except Exception as exc:  # noqa: BLE001
                logger.exception("upload (fresh) failed")
                return {"ok": False, "error": str(exc)[:1000]}
            finally:
                try:
                    Path(paths["staged_path"]).unlink(missing_ok=True)
                    if paths.get("thumbnail_path"):
                        Path(paths["thumbnail_path"]).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass

        @app.post("/cleanup")
        def cleanup(body: AgentApp.CleanupModel,
                    x_agent_token: Optional[str] = Header(None)):
            """Borra el staging remoto tras una subida correcta (idempotente).

            No toca YouTube: no requiere verificación de egress. Solo acepta
            rutas dentro del directorio de staging del canal.
            """
            self._check_token(x_agent_token)
            return self._cleanup_staged(body.staged_path)

        @app.post("/thumbnail")
        async def thumbnail(video_id: str = Form(...),
                            thumbnail: UploadFile = File(...),
                            x_agent_token: Optional[str] = Header(None)):
            """Aplica un thumbnail a un vídeo ya subido, desde la IP del gateway."""
            self._check_token(x_agent_token)
            self._require_egress()
            d = Path(self.cfg.staging_dir) / f"thumb-{uuid.uuid4().hex[:8]}"
            d.mkdir(parents=True, exist_ok=True)
            ext = Path(thumbnail.filename or "thumb.jpg").suffix or ".jpg"
            p = d / f"thumb{ext}"
            try:
                with open(p, "wb") as out:
                    while chunk := thumbnail.file.read(1024 * 1024):
                        out.write(chunk)
                return youtube_api.set_thumbnail(self.cfg, video_id, str(p))
            except Exception as exc:  # noqa: BLE001
                logger.exception("thumbnail failed")
                return {"ok": False, "error": str(exc)[:1000]}
            finally:
                try:
                    p.unlink(missing_ok=True)
                    d.rmdir()
                except OSError:
                    pass

    # ── Ledger de subida idempotente ────────────────────────────────
    def _ledger_dir(self) -> Path:
        d = Path(self.cfg.state_dir) / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ledger_path(self, ref: str) -> Path:
        safe = "".join(c for c in str(ref) if c.isalnum() or c in "-_")[:80]
        return self._ledger_dir() / f"{safe or 'noref'}.json"

    def _ledger_get(self, ref: str) -> dict:
        try:
            p = self._ledger_path(ref)
            if not p.exists():
                return {}
            return json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("ledger get failed (%s): %s", ref, exc)
            return {}

    def _ledger_put(self, ref: str, data: dict) -> None:
        try:
            p = self._ledger_path(ref)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ledger put failed (%s): %s", ref, exc)

    def _upload_and_record(self, video_path: str, meta: dict) -> dict:
        ref = str(meta.get("upload_ref") or "")
        res = youtube_api.upload_video(self.cfg, video_path, meta)
        if res.get("ok") and ref:
            self._ledger_put(ref, {
                "status": "done",
                "video_id": res.get("video_id"),
                "url": res.get("url"),
                "thumbnail_applied": res.get("thumbnail_applied"),
                "completed_at": time.time(),
            })
        return res

    def _cleanup_staged(self, staged_path: str) -> dict:
        if not staged_path:
            return {"ok": True, "skipped": "no_staged_path"}
        root = Path(self.cfg.staging_dir).resolve()
        try:
            target = Path(staged_path).resolve()
        except OSError as exc:
            return {"ok": False, "error": str(exc)[:500]}
        if root != target and root not in target.parents:
            raise HTTPException(400, "ruta fuera del staging")
        removed: list[str] = []
        try:
            if target.is_file():
                target.unlink(missing_ok=True)
                removed.append(str(target))
            parent = target.parent
            for sib in parent.glob("thumb*"):
                if sib.is_file():
                    sib.unlink(missing_ok=True)
                    removed.append(str(sib))
            try:
                parent.rmdir()
            except OSError:
                pass
        except OSError as exc:
            logger.warning("cleanup failed for %s: %s", staged_path, exc)
            return {"ok": False, "error": str(exc)[:500]}
        return {"ok": True, "removed": removed}

    def _purge_staging(self) -> int:
        """Purga staging huérfano por TTL. Devuelve nº de entradas borradas."""
        root = Path(self.cfg.staging_dir)
        if not root.exists():
            return 0
        cutoff = time.time() - max(1, int(self.cfg.staging_ttl_hours)) * 3600
        removed = 0
        try:
            for entry in root.iterdir():
                try:
                    if entry.stat().st_mtime >= cutoff:
                        continue
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    continue
        except OSError:
            pass
        return removed

    def _staging_gb(self) -> float:
        root = Path(self.cfg.staging_dir)
        if not root.exists():
            return 0.0
        total = 0
        try:
            for p in root.rglob("*"):
                try:
                    if p.is_file():
                        total += p.stat().st_size
                except OSError:
                    continue
        except OSError:
            pass
        return total / (1024 ** 3)

    def _save_upload(self, video: UploadFile, thumbnail: Optional[UploadFile],
                     ref: str = "") -> dict:
        """Guarda el vídeo (y thumb) en el staging del gateway y valida cuotas."""
        staging_root = Path(self.cfg.staging_dir)
        staging_root.mkdir(parents=True, exist_ok=True)

        try:
            free_gb = shutil.disk_usage(str(staging_root)).free / (1024 ** 3)
        except OSError as exc:
            raise HTTPException(507, f"no se pudo leer el disco del staging: {exc}")
        if free_gb < float(self.cfg.min_free_gb):
            raise HTTPException(
                507,
                f"disco insuficiente en el gateway: {free_gb:.2f} GB libres "
                f"(mínimo {self.cfg.min_free_gb} GB)",
            )

        ref = ref or uuid.uuid4().hex[:8]
        safe_ref = "".join(c for c in ref if c.isalnum() or c in "-_")[:64]
        d = staging_root / safe_ref
        d.mkdir(parents=True, exist_ok=True)

        video_path = d / "video.mp4"
        try:
            with open(video_path, "wb") as out:
                while chunk := video.file.read(1024 * 1024):
                    out.write(chunk)

            size = video_path.stat().st_size
            max_bytes = int(float(self.cfg.max_body_mb) * 1024 * 1024)
            if max_bytes > 0 and size > max_bytes:
                video_path.unlink(missing_ok=True)
                raise HTTPException(
                    413,
                    f"vídeo demasiado grande: {size / (1024 ** 2):.1f} MB "
                    f"(máx {self.cfg.max_body_mb} MB)",
                )
            staging_gb = self._staging_gb()
            if staging_gb > float(self.cfg.staging_max_gb):
                video_path.unlink(missing_ok=True)
                raise HTTPException(
                    413,
                    f"cuota de staging excedida: {staging_gb:.2f} GB "
                    f"(máx {self.cfg.staging_max_gb} GB)",
                )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            video_path.unlink(missing_ok=True)
            raise HTTPException(500, f"no se pudo guardar el vídeo: {exc}")

        thumb_path = None
        if thumbnail:
            ext = Path(thumbnail.filename or "thumb.jpg").suffix or ".jpg"
            thumb_path = d / f"thumb{ext}"
            try:
                with open(thumb_path, "wb") as out:
                    while chunk := thumbnail.file.read(1024 * 1024):
                        out.write(chunk)
            except Exception as exc:  # noqa: BLE001
                video_path.unlink(missing_ok=True)
                raise HTTPException(500, f"no se pudo guardar el thumbnail: {exc}")
        return {"staged_path": str(video_path),
                "thumbnail_path": str(thumb_path) if thumb_path else None}


def create_app(cfg: AgentConfig):
    return AgentApp(cfg).app
