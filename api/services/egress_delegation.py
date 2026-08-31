"""Delegación central del egress a YouTube hacia los agentes por cuenta.

Cualquier punto del server que vaya a contactar con YouTube/Google para un
canal DEBE pasar por aquí: si el canal es gestionado (presente en
``config/egress_agents.json``), se delega al agente; si no, se devuelve el
comportamiento local (el canal actual, sin cambios).

Fail-closed: nunca se cae al camino local para un canal gestionado.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from api.services.egress_client import get_egress_client, EgressAgent

logger = logging.getLogger(__name__)


def egress_client_for(slug: str) -> Optional[EgressAgent]:
    """Devuelve el cliente del agente si el canal es gestionado, o None.

    Los callers:
        client = egress_client_for(slug)
        if client is not None:
            # delegar al agente
        else:
            # comportamiento local actual
    """
    return get_egress_client(slug)


def browser_action(slug: str, action: str, account: str = "",
                   params: Optional[dict] = None,
                   local_fn: Optional[Callable[[], dict]] = None) -> dict:
    """Ejecuta una acción de navegador delegando al agente si es gestionado.

    - Canal gestionado: delega ``action`` al agente (local_fn NO se ejecuta).
    - Canal no gestionado: ejecuta ``local_fn`` (comportamiento actual).

    Si el canal es gestionado y el agente no responde, lanza
    ``EgressAgentUnavailableError`` (fail-closed) — nunca cae a local.
    """
    client = get_egress_client(slug)
    if client is not None:
        return client.browser_action(action, account=account, params=params or {})
    if local_fn is None:
        return {"ok": False, "error": "no hay camino local definido"}
    return local_fn()
