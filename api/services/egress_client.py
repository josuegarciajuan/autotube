"""Cliente del server → agente egress (delegación de todo el egress a YouTube).

CONTRATO FAIL-CLOSED (invariante "0 errores", ago 2026):
- Un canal "gestionado" (presente en ``config/egress_agents.json``) DEBE delegar
  TODO su egress al agente. Si el agente no responde, se lanza
  ``EgressAgentUnavailableError`` — NUNCA se cae al camino local (directo por la
  IP del server), para que la IP del server jamás toque YouTube de ese canal.
- Un canal SIN agente configurado devuelve ``None`` y sigue con el comportamiento
  actual (local, sin proxy): los canales existentes quedan intactos.

El token secreto del agente vive en ``config/egress_agents.json`` (gitignored),
no en ``config_json`` (que se sincroniza vía bridge).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# Ruta del mapa secreto slug -> {url, token}. Gitignored.
_AGENTS_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "egress_agents.json"


class EgressAgentUnavailableError(RuntimeError):
    """El agente de un canal gestionado no está accesible (fail-closed).

    Los callers NO deben reintentar cayendo al camino local directo; deben
    mantener el vídeo/job en espera hasta que el agente vuelva a estar activo.
    """


def _load_agents() -> dict:
    if not _AGENTS_FILE.exists():
        return {}
    try:
        return json.loads(_AGENTS_FILE.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("No se pudo leer %s: %s", _AGENTS_FILE, exc)
        return {}


def get_agent_config(slug: str) -> Optional[dict]:
    """Devuelve {url, token} para un slug gestionado, o None si no está."""
    agents = _load_agents()
    cfg = agents.get(slug)
    if not cfg or not cfg.get("url"):
        return None
    return {"url": str(cfg["url"]).rstrip("/"), "token": str(cfg.get("token", ""))}


def is_egress_managed(slug: str) -> bool:
    """True si el canal debe delegar su egress al agente."""
    return get_agent_config(slug) is not None


def get_egress_client(slug: str) -> Optional["EgressAgent"]:
    """Devuelve el cliente del agente para un canal gestionado, o None si no."""
    cfg = get_agent_config(slug)
    if cfg is None:
        return None
    return EgressAgent(slug, cfg["url"], cfg["token"])


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
                    f"IP residencial de {self.slug} caída/inactiva (egress_down) — "
                    f"renovar en Geonix. Operación bloqueada (fail-closed)."
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
        try:
            return resp.json()
        except ValueError:
            raise EgressAgentUnavailableError(
                f"respuesta no-JSON del agente {self.slug} (HTTP {resp.status_code})"
            ) from None

    # ── operaciones de alto nivel ───────────────────────────────
    def ping(self) -> dict:
        return self._get("/ping")

    def egress_check(self) -> dict:
        return self._get("/egress-check")

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

    def oauth_url(self) -> str:
        data = self._post("/auth/oauth-url", {"account": self.slug})
        return data.get("auth_url", "")

    def auth_exchange(self, code: str) -> bool:
        data = self._post("/auth/exchange", {"account": self.slug, "code": code})
        return bool(data.get("ok"))
