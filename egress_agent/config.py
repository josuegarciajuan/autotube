"""Configuración del agente egress (cargada por cuenta en cada VPS).

El agente se ejecuta con una sola cuenta Google a la vez (una VPS por cuenta),
así que la config es un único objeto con los paths y credenciales de ESA cuenta.

Fuentes de config (por orden de prioridad):
    1. Fichero JSON (--config <path>) — recomendado para despliegue.
    2. Variables de entorno (AGENT_*).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AgentConfig:
    """Configuración de un agente egress para una cuenta Google."""

    # ── Identidad ───────────────────────────────────────────────
    slug: str = ""                     # slug del canal (canal6, canal7, ...)
    google_account: str = ""           # cuenta Google (traza perfiles/tokens)
    egress_label: str = ""             # etiqueta UI (p. ej. "IP1-ES")
    auth_token: str = ""               # token secreto que valida el server principal

    # ── Paths (relativos a la raíz del repo o absolutos) ────────
    project_root: str = ""             # raíz del repo autotube en la VPS
    client_secret_path: str = ""       # client_secret_{slug}.json
    token_pickle_path: str = ""        # {slug}.pickle (OAuth Data API)
    browser_profile_dir: str = ""      # perfil persistente del navegador
    browser_session_path: str = ""     # snapshot storage_state (opcional)

    # ── Huella de navegador por cuenta (opcional) ───────────────
    fingerprint: dict = field(default_factory=dict)

    # ── Servidor ────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 9101
    max_body_mb: int = 4096            # límite subida multipart (MB)

    # ── Proxy Playwright opcional (si NO se usa túnel a nivel de SO) ──
    # Si la VPS enruta su salida a nivel de sistema, esto debe dejarse vacío.
    playwright_proxy: dict = field(default_factory=dict)

    # ── Resolver ────────────────────────────────────────────────

    def resolve_paths(self) -> "AgentConfig":
        """Resuelve rutas relativas contra project_root (raíz del repo)."""
        root = Path(self.project_root) if self.project_root else Path(__file__).resolve().parent.parent
        base = root if root.is_absolute() else Path.cwd() / root

        def _abs(p: str, default: str) -> Path:
            if p:
                q = Path(p)
                return q if q.is_absolute() else base / q
            return base / default

        self.client_secret_path = str(_abs(self.client_secret_path, f"config/client_secret_{self.slug}.json"))
        self.token_pickle_path = str(_abs(self.token_pickle_path, f"tokens/{self.slug}.pickle"))
        self.browser_profile_dir = str(_abs(self.browser_profile_dir, f"tokens/{self.google_account or self.slug}_browser_profile"))
        self.browser_session_path = str(_abs(self.browser_session_path, f"tokens/{self.google_account or self.slug}_browser_session.json"))
        self.project_root = str(base)
        return self

    @classmethod
    def from_json(cls, path: str) -> "AgentConfig":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = cls(**{k: v for k, v in data.items()
                     if k in cls.__dataclass_fields__})
        return cfg.resolve_paths()

    @classmethod
    def from_env(cls) -> "AgentConfig":
        def _g(name: str, default: str = "") -> str:
            return os.getenv(name, default)

        fingerprint = {}
        raw_fp = _g("AGENT_FINGERPRINT")
        if raw_fp:
            try:
                fingerprint = json.loads(raw_fp)
            except json.JSONDecodeError:
                fingerprint = {}

        proxy = {}
        raw_proxy = _g("AGENT_PLAYWRIGHT_PROXY")
        if raw_proxy:
            try:
                proxy = json.loads(raw_proxy)
            except json.JSONDecodeError:
                proxy = {}

        cfg = cls(
            slug=_g("AGENT_SLUG"),
            google_account=_g("AGENT_GOOGLE_ACCOUNT"),
            egress_label=_g("AGENT_EGRESS_LABEL"),
            auth_token=_g("AGENT_AUTH_TOKEN"),
            project_root=_g("AGENT_PROJECT_ROOT"),
            client_secret_path=_g("AGENT_CLIENT_SECRET_PATH"),
            token_pickle_path=_g("AGENT_TOKEN_PICKLE_PATH"),
            browser_profile_dir=_g("AGENT_BROWSER_PROFILE_DIR"),
            browser_session_path=_g("AGENT_BROWSER_SESSION_PATH"),
            fingerprint=fingerprint,
            host=_g("AGENT_HOST", "0.0.0.0"),
            port=int(_g("AGENT_PORT", "9101")),
            max_body_mb=int(_g("AGENT_MAX_BODY_MB", "4096")),
            playwright_proxy=proxy,
        )
        return cfg.resolve_paths()
