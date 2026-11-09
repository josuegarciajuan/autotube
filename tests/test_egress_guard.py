"""Guard runtime: bloquea DNS directo a Google/YouTube para gestionados."""
from __future__ import annotations

import socket

import pytest

from api.services import egress_guard as guard


def test_blocks_google_for_managed(monkeypatch):
    original = socket.getaddrinfo
    monkeypatch.setattr(guard, "_installed", False)
    monkeypatch.setattr(
        "api.services.egress_client.is_egress_managed", lambda slug: slug == "canal6"
    )
    monkeypatch.setattr(guard, "install", guard.install)  # no-op
    try:
        guard.install_for_channel("canal6")
        with pytest.raises(guard.EgressBlockedError):
            socket.getaddrinfo("www.youtube.com", 443)
        # Un host no-Google sigue funcionando.
        socket.getaddrinfo("localhost", 80)
    finally:
        socket.getaddrinfo = original
        guard._installed = False


def test_unmanaged_not_blocked(monkeypatch):
    original = socket.getaddrinfo
    monkeypatch.setattr(guard, "_installed", False)
    monkeypatch.setattr(
        "api.services.egress_client.is_egress_managed", lambda slug: False
    )
    try:
        guard.install_for_channel("canal2")
        socket.getaddrinfo("localhost", 80)
    finally:
        socket.getaddrinfo = original
        guard._installed = False
