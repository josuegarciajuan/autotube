"""Tests estructurales anti-contaminación del egress.

Garantizan que las credenciales de los canales gestionados (los que delegan su
egress a un agente VPS) NUNCA viven en el repositorio del server principal.

Si un ``tokens/<slug>.pickle`` o un ``client_secret_<slug>.json`` de un canal
gestionado existiera en el server, el server podría hacer egress local con la IP
del datacenter, rompiendo el aislamiento (fail-closed). Este test lo impide.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _managed_slugs() -> list:
    """Slugs listados en config/egress_agents.json (canales gestionados)."""
    agents_file = ROOT / "config" / "egress_agents.json"
    if not agents_file.exists():
        return []
    import json
    try:
        return list((json.loads(agents_file.read_text(encoding="utf-8")) or {}).keys())
    except Exception:
        return []


def test_no_managed_credentials_in_server():
    """Ningún canal gestionado tiene pickle/client_secret en el server principal."""
    for slug in _managed_slugs():
        # tokens/<slug>.pickle (credencial OAuth Data API)
        assert not (ROOT / "tokens" / f"{slug}.pickle").exists(), (
            f"Fuga: {slug}.pickle presente en el server principal. El token OAuth "
            f"de un canal gestionado DEBE vivir solo en el VPS del agente."
        )
        # config/client_secret_<slug>.json (secreto del proyecto GCP del canal)
        assert not (ROOT / "config" / f"client_secret_{slug}.json").exists(), (
            f"Fuga: client_secret_{slug}.json presente en el server principal. "
            f"El client_secret de un canal gestionado DEBE vivir solo en el VPS."
        )


def test_no_client_secret_leaks_into_git():
    """Ningún client_secret_*.json se versiona: están gitignored (secreto)."""
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "client_secret" in gitignore, (
        "Los client_secret_*.json son secretos y deben estar en .gitignore."
    )


def test_egress_agents_is_gitignored():
    """egress_agents.json (con tokens) está en .gitignore (no se versiona)."""
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "egress_agents.json" in gitignore, (
        "config/egress_agents.json contiene el token del agente y debe estar "
        "en .gitignore para no versionarse."
    )
