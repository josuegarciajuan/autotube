"""Render de shorts distribuido: worker thin + cliente de la casa."""
import hashlib
import os

from pipeline_dist import short_render_client as C
from pipeline_dist import short_render_worker as W


def test_worker_resolve_rutas_relativas_y_absolutas(tmp_path):
    assert str(W._resolve("in/a.mp4")) == "/work/in/a.mp4"
    assert str(W._resolve("/abs/x.mp4")) == "/abs/x.mp4"


def test_worker_sha256(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hola")
    assert W._sha256(p) == hashlib.sha256(b"hola").hexdigest()


def test_cliente_enabled_por_env(monkeypatch):
    monkeypatch.delenv("AUTOTUBE_DIST_SHORTS", raising=False)
    assert C.enabled() is False
    monkeypatch.setenv("AUTOTUBE_DIST_SHORTS", "1")
    assert C.enabled() is True
    monkeypatch.setenv("AUTOTUBE_DIST_SHORTS", "on")
    assert C.enabled() is True
    monkeypatch.setenv("AUTOTUBE_DIST_SHORTS", "0")
    assert C.enabled() is False


def test_cliente_excluye_casa_por_env(monkeypatch):
    monkeypatch.setenv("AUTOTUBE_DIST_EXCLUDED_NODES", "liveyourdre2, x ,")
    assert C._excluded_nodes() == ["liveyourdre2", "x"]
    monkeypatch.delenv("AUTOTUBE_DIST_EXCLUDED_NODES", raising=False)
    assert C._excluded_nodes() == []
