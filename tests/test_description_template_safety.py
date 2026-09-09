"""Test that DESCRIPTION_TEMPLATE formatting never breaks an upload.

Regression (ago 2026): canal4's DESCRIPTION_TEMPLATE references {related_videos}
but orchestrator.py formatted it without that key → KeyError at upload time,
video #2350 was retried 9× in a silent loop (worker reported success=True).
`safe_format_template` fills every referenced placeholder (absent → "") so an
extra/typo template placeholder can never raise.
"""

import pytest

from pipeline.utils import safe_format_template

# canal4's template contains {related_videos}; a plain .format(...) without that
# key raises KeyError.
_CANAL4_LIKE = (
    "⛵ {titulo}\n———\n\n{descripcion_seo}\n\n❄️ EN ESTE VIDEO\n..."
    "\n{related_videos}\n\n📌 #historia #{tags}"
)


def test_safe_format_template_never_raises_on_missing_placeholder():
    out = safe_format_template(
        _CANAL4_LIKE,
        titulo="Título",
        descripcion_seo="SEO",
        chapters="cap",
    )
    # No KeyError; the absent placeholder is rendered empty (no {related_videos}).
    assert "{related_videos}" not in out
    assert "{titulo}" not in out
    assert "Título" in out
    assert "SEO" in out


def test_safe_format_template_supplies_provided_values():
    out = safe_format_template(
        "{titulo} | {related_videos}",
        titulo="Hola",
        related_videos="Síguenos",
    )
    assert out == "Hola | Síguenos"


def test_safe_format_template_handles_empty():
    assert safe_format_template("") == ""


def test_safe_format_template_unknown_placeholder_is_blank_not_crash():
    # A template with a typo/extra {typo_placeholder} must render blank, never raise.
    out = safe_format_template("A {titulo} B {typo_placeholder}", titulo="X")
    assert out == "A X B "
