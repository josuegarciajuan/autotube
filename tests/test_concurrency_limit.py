"""Tests del límite efectivo de generaciones long-form concurrentes.

Regla: 2 solo cuando el render se distribuye por la flota de SuperServer
(``AUTOTUBE_DIST_RENDER_V2=1``); en caso contrario 1 (seguro, evita dos
renders locales simultáneos que saturarían el host).
"""
from config.settings import (
    effective_max_concurrent_longform_jobs,
    MAX_CONCURRENT_LONGFORM_JOBS,
)


def test_configured_value_is_at_least_two():
    assert MAX_CONCURRENT_LONGFORM_JOBS >= 2


def test_effective_limit_is_one_without_dist_render(monkeypatch):
    monkeypatch.delenv("AUTOTUBE_DIST_RENDER_V2", raising=False)
    assert effective_max_concurrent_longform_jobs() == 1


def test_effective_limit_is_configured_with_dist_render(monkeypatch):
    monkeypatch.setenv("AUTOTUBE_DIST_RENDER_V2", "1")
    monkeypatch.delenv("MAX_CONCURRENT_LONGFORM_JOBS", raising=False)
    assert effective_max_concurrent_longform_jobs() == 2
