"""Cliente del server → agente egress (delegación de todo el egress a YouTube).

CONTRATO FAIL-CLOSED (invariante "0 errores", nov 2026):
- Un canal "gestionado" (flag durable en DB ``channels.egress_managed`` **o**
  entrada en ``config/egress_agents.json``) DEBE delegar TODO su egress al
  agente. Si el agente no responde o el perfil está incompleto se lanza
  ``EgressAgentUnavailableError`` / ``EgressConfigurationError`` — NUNCA se cae
  al camino local (directo por la IP del server).
- Un canal SIN agente configurado devuelve ``None`` y sigue con el
  comportamiento actual (local, sin proxy): los canales existentes intactos.

El token secreto del agente vive en ``config/egress_agents.json`` (gitignored).
La marca de "gestionado" vive también en la DB para que una pérdida del JSON no
convierta el canal en local en silencio.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import requests

from api.services.egress_registry import (  # noqa: F401  (re-export compat)
    _AGENTS_FILE,
    EgressAgentUnavailableError,
    EgressConfigurationError,
    _load_agents,
    get_profile,
    is_egress_managed,
)

logger = logging.getLogger(__name__)


def get_egress_client(slug: str) -> Optional["EgressAgent"]:
    """Devuelve el cliente del agente para un canal gestionado, o None si no."""
    profile = get_profile(slug)
    if profile is None:
        return None
    return EgressAgent(slug, profile.url, profile.token)


class EgressAgent:
    """Cliente HTTP hacia el agente de una cuenta concreta."""

    def __init__(self, slug: str, base_url: str, token: str, timeout: int = 120):
        self.slug = slug
        self.base_url = base_url
        self.token = token
        self.timeout = timeout

    # ── bajo nivel ──────────────────────────────────────────────
    def _headers(self) -> dict:
        h = {}
        if self.token:
            h["X-Agent-Token"] = self.token
        return h

    def _check_egress_health(self) -> None:
        """Fail-closed: si el monitor marcó egress_down_<slug>, bloquea la op."""
        try:
            from database.db_extended import ExtendedDatabase
            _db = ExtendedDatabase()
            if _db.get_system_state(f"egress_down_{self.slug}") == "1":
                raise EgressAgentUnavailableError(
                    f"El gateway egress de {self.slug} está marcado como caído "
                    f"(egress_down). Operación bloqueada (fail-closed)."
                )
        except EgressAgentUnavailableError:
            raise
        except Exception:
            pass  # si no se puede leer el flag, no bloquear por esto

    def _post(self, path: str, payload: Optional[dict] = None,
              files: Optional[dict] = None, timeout: Optional[int] = None,
              stream: bool = False) -> dict:
        self._check_egress_health()
        url = f"{self.base_url}{path}"
        try:
            if files:
                resp = requests.post(url, headers=self._headers(), files=files,
                                     timeout=timeout or self.timeout, stream=stream)
            else:
                resp = requests.post(url, headers=self._headers(), json=payload or {},
                                     timeout=timeout or self.timeout, stream=stream)
        except requests.RequestException as exc:
            raise EgressAgentUnavailableError(
                f"agente de {self.slug} no accesible ({exc}) — fail-closed, sin fallback directo"
            ) from exc

        if resp.status_code == 401:
            raise EgressAgentUnavailableError(f"token de agente rechazado para {self.slug}")
        try:
            data = resp.json()
        except ValueError:
            raise EgressAgentUnavailableError(
                f"respuesta no-JSON del agente {self.slug} (HTTP {resp.status_code})"
            ) from None
        return data

    def _get(self, path: str) -> dict:
        self._check_egress_health()
        try:
            resp = requests.get(f"{self.base_url}{path}", headers=self._headers(),
                                timeout=self.timeout)
        except requests.RequestException as exc:
            raise EgressAgentUnavailableError(
                f"agente de {self.slug} no accesible ({exc}) — fail-closed"
            ) from exc
        if resp.status_code == 401:
            raise EgressAgentUnavailableError(f"token de agente rechazado para {self.slug}")
        try:
            return resp.json()
        except ValueError:
            raise EgressAgentUnavailableError(
                f"respuesta no-JSON del agente {self.slug} (HTTP {resp.status_code})"
            ) from None

    # ── operaciones de alto nivel ───────────────────────────────
    def ping(self) -> dict:
        return self._get("/ping")

    def healthz(self) -> dict:
        return self._get("/healthz")

    def egress_check(self) -> dict:
        return self._get("/egress-check")

    def egress_check_browser(self) -> dict:
        return self._get("/egress-check-browser")

    def browser_action(self, action: str, account: str = "", params: dict | None = None) -> dict:
        return self._post("/browser/action", {
            "action": action, "account": account, "params": params or {},
        })

    def api_call(self, op: str, params: dict | None = None) -> dict:
        return self._post("/api/call", {"op": op, "params": params or {}})

    def ytdlp(self, op: str, params: dict | None = None) -> dict:
        return self._post("/ytdlp", {"op": op, "params": params or {}})

    def fetch(self, url: str, timeout: int = 30) -> dict:
        return self._post("/fetch", {"url": url, "timeout": timeout})

    def download_audio(self, url: str, dest_path: str, timeout: int = 1200) -> str:
        """Descarga el audio de ``url`` vía yt-dlp en el agente (IP residencial).

        El agente (datacenter→proxy residencial) evita el HTTP 403 que recibe
        una descarga directa desde la IP del server.  Devuelve ``dest_path``.
        """
        res = self.ytdlp("audio", {"url": url})
        if not res.get("ok"):
            raise EgressAgentUnavailableError(
                f"yt-dlp del agente falló: {res.get('error')}"
            )
        remote = (res.get("result") or {}).get("path")
        if not remote:
            raise EgressAgentUnavailableError("el agente no devolvió ruta de audio")

        self._check_egress_health()
        try:
            resp = requests.get(
                f"{self.base_url}/download", headers=self._headers(),
                params={"path": remote}, stream=True,
                timeout=max(self.timeout, timeout),
            )
        except requests.RequestException as exc:
            raise EgressAgentUnavailableError(
                f"descarga de audio desde el agente falló ({exc})"
            ) from exc
        if resp.status_code != 200:
            raise EgressAgentUnavailableError(
                f"HTTP {resp.status_code} al descargar audio del agente"
            )
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)
        return str(dest)

    def upload(self, video_path: str, meta: dict,
               thumbnail_path: Optional[str] = None,
               staged_path: Optional[str] = None) -> dict:
        """Sube un vídeo al agente.

        Si ``staged_path`` se da, el agente ya tiene el archivo (transferencia
        previa vía ``stage``) → se envía solo la orden (sin el fichero).
        Si no, se envía el fichero en multipart (carga directa).
        """
        files = {"meta": (None, json.dumps(meta))}
        if staged_path:
            files["staged_path"] = (None, staged_path)
            if thumbnail_path:
                meta["thumbnail_path"] = thumbnail_path
            try:
                return self._post("/upload", files=files, timeout=max(self.timeout, 1800))
            finally:
                pass
        files["video"] = (Path(video_path).name, open(video_path, "rb"), "video/*")
        if thumbnail_path and Path(thumbnail_path).exists():
            files["thumbnail"] = (Path(thumbnail_path).name,
                                  open(thumbnail_path, "rb"), "image/jpeg")
        try:
            return self._post("/upload", files=files, timeout=max(self.timeout, 1800))
        finally:
            for key in ("video", "thumbnail"):
                fobj = files.get(key)
                if isinstance(fobj, tuple) and len(fobj) > 1:
                    try:
                        fobj[1].close()
                    except Exception:  # noqa: BLE001
                        pass

    def stage(self, video_path: str, ref: str,
              thumbnail_path: Optional[str] = None) -> dict:
        """Transfiere el vídeo (+thumb) al VPS SIN subir (estado intermedio).

        Devuelve {ok, staged_path, thumbnail_path} para pasarlos a ``upload``.
        """
        files = {
            "video": (Path(video_path).name, open(video_path, "rb"), "video/*"),
            "ref": (None, ref),
        }
        if thumbnail_path and Path(thumbnail_path).exists():
            files["thumbnail"] = (Path(thumbnail_path).name,
                                  open(thumbnail_path, "rb"), "image/jpeg")
        try:
            return self._post("/stage", files=files, timeout=max(self.timeout, 1800))
        finally:
            for key in ("video", "thumbnail"):
                fobj = files.get(key)
                if isinstance(fobj, tuple) and len(fobj) > 1:
                    try:
                        fobj[1].close()
                    except Exception:  # noqa: BLE001
                        pass

    def cleanup(self, staged_path: str) -> dict:
        """v64: pide al agente que borre el staged remoto tras una subida correcta.

        Fail-open: el llamador debe envolverlo en try/except (un agente antiguo
        sin endpoint ``/cleanup`` no debe romper la subida).
        """
        if not staged_path:
            return {"ok": True, "skipped": "no_staged_path"}
        return self._post("/cleanup", {"staged_path": staged_path})

    def set_thumbnail(self, video_id: str, thumbnail_path: str) -> dict:
        """Aplica un thumbnail a un vídeo ya subido (multipart al gateway)."""
        path = Path(thumbnail_path)
        files = {
            "video_id": (None, video_id),
            "thumbnail": (path.name, open(path, "rb"), "image/jpeg"),
        }
        try:
            return self._post("/thumbnail", files=files,
                              timeout=max(self.timeout, 300))
        finally:
            try:
                files["thumbnail"][1].close()
            except Exception:  # noqa: BLE001
                pass

    def fetch_bytes(self, url: str, timeout: int = 45) -> bytes:
        """Descarga binaria (thumbnail servido) desde la IP del gateway."""
        res = self._post("/fetch-bytes", {"url": url, "timeout": timeout})
        if not res.get("ok"):
            raise EgressAgentUnavailableError(res.get("error", "fetch-bytes falló"))
        remote = (res.get("result") or {}).get("path")
        if not remote:
            raise EgressAgentUnavailableError("fetch-bytes no devolvió ruta")
        self._check_egress_health()
        try:
            resp = requests.get(f"{self.base_url}/download", headers=self._headers(),
                                params={"path": remote},
                                timeout=max(self.timeout, timeout))
        except requests.RequestException as exc:
            raise EgressAgentUnavailableError(
                f"descarga de bytes desde el agente falló ({exc})") from exc
        if resp.status_code != 200:
            raise EgressAgentUnavailableError(
                f"HTTP {resp.status_code} al descargar bytes del agente")
        return resp.content

    def download_clip(self, url: str, section: str, dest_path: str,
                      timeout: int = 900) -> str:
        """Descarga una sección de vídeo desde la IP del gateway."""
        res = self.ytdlp("clip", {"url": url, "section": section})
        if not res.get("ok"):
            raise EgressAgentUnavailableError(
                f"yt-dlp clip del agente falló: {res.get('error')}")
        remote = (res.get("result") or {}).get("path")
        if not remote:
            raise EgressAgentUnavailableError("el agente no devolvió ruta del clip")
        self._check_egress_health()
        try:
            resp = requests.get(
                f"{self.base_url}/download", headers=self._headers(),
                params={"path": remote}, stream=True,
                timeout=max(self.timeout, timeout),
            )
        except requests.RequestException as exc:
            raise EgressAgentUnavailableError(
                f"descarga de clip desde el agente falló ({exc})") from exc
        if resp.status_code != 200:
            raise EgressAgentUnavailableError(
                f"HTTP {resp.status_code} al descargar clip del agente")
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)
        return str(dest)

    def get_video_status(self, video_id: str) -> dict:
        res = self.api_call("get_video_status", {"kwargs": {"video_id": video_id}})
        if not res.get("ok"):
            raise EgressAgentUnavailableError(res.get("error", "get_video_status falló"))
        return res.get("result", {})

    def oauth_url(self) -> str:
        data = self._post("/auth/oauth-url", {"account": self.slug})
        return data.get("auth_url", "")

    def auth_exchange(self, code: str) -> bool:
        data = self._post("/auth/exchange", {"account": self.slug, "code": code})
        return bool(data.get("ok"))
