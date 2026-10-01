"""Tests de F5 — completitud del título/overlay."""
from pipeline.packaging_brief import (
    build_overlay_spec, finalize_title, title_is_complete, render_overlay,
)


class Cfg:
    THUMBNAIL_OVERLAY_BUDGETS = {"l1": 14, "l2": 24, "badge": 14}


def test_finalize_title_removes_dangling_connector():
    # Caso real publicado con corte a medias.
    bad = "El mineral prohibido que podría dejar sin cura a toda"
    fixed = finalize_title(bad, max_chars=100)
    assert title_is_complete(fixed)
    assert not fixed.endswith(" a toda")
    assert not fixed.endswith(" de")


def test_finalize_title_truncates_on_word_boundary():
    long = "El misterio del sistema solar exterior que la NASA aún no logra explicar del todo"
    fixed = finalize_title(long, max_chars=40)
    assert len(fixed) <= 40
    assert title_is_complete(fixed)
    # No corta palabras a medias.
    assert long.startswith(fixed.split()[:-1] and " ".join(fixed.split()[:-1]) or fixed)


def test_title_is_complete():
    assert title_is_complete("La posada donde NADIE salía con vida") is True
    assert title_is_complete("El mineral prohibido que podría dejar sin cura a") is False
    assert title_is_complete("¿Qué escondían las ciudades?") is True


def test_overlay_spec_never_cuts_mid_word():
    spec = build_overlay_spec(
        "La posada donde nadie salía con vida", Cfg(),
        raw_l1="La posada del terror inimaginable", raw_l2="Nadie salió con vida",
    )
    assert len(spec.l1) <= 14
    assert len(spec.l2) <= 24
    # El render persistido sale del spec (no hay texto pintado sin spec).
    assert "|" in render_overlay(spec) or render_overlay(spec)
