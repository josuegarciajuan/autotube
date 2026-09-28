"""Tests for the W3 OverlaySpec authority (pipeline/packaging_brief.py)."""

import os
import sys
from types import SimpleNamespace

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from pipeline.packaging_brief import (  # noqa: E402
    OverlaySpec,
    build_overlay_spec,
    overlay_budgets,
    render_overlay,
    spec_to_db_fields,
    split_overlay_text,
    title_terms,
    validate_overlay_spec,
)


def _cfg(**overrides):
    base = dict(
        THUMBNAIL_OVERLAY_BUDGETS={"l1": 14, "l2": 24, "badge": 14},
        THUMBNAIL_REQUIRE_TEXT=True,
        THUMBNAIL_BADGE_CLICHES=["real", "caso real", "archivo", "expediente"],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_budgets_default_and_override():
    assert overlay_budgets(SimpleNamespace()) == {"l1": 14, "l2": 24, "badge": 14}
    assert overlay_budgets(_cfg(THUMBNAIL_OVERLAY_BUDGETS={"l1": 10, "l2": 20, "badge": 8})) == {
        "l1": 10, "l2": 20, "badge": 8,
    }


def test_split_overlay_text_two_lines():
    assert split_overlay_text("SIN SANGRE | El informe secreto") == (
        "SIN SANGRE", "El informe secreto",
    )
    assert split_overlay_text("SOLO UNA LINEA") == ("SOLO UNA LINEA", "")


def test_build_spec_anchors_l1_to_title_term():
    spec = build_overlay_spec(
        title="La expedición Franklin desapareció en el hielo",
        cfg=_cfg(),
        raw_l1="ALGO RARO",      # shares no term with the title
        raw_l2="nadie volvió a verlos",
    )
    terms = set(title_terms("La expedición Franklin desapareció en el hielo"))
    assert set(title_terms(spec.l1)) & terms
    assert len(spec.l1) <= 14
    assert spec.l1 == spec.l1.upper()


def test_build_spec_l2_does_not_repeat_title_or_l1():
    spec = build_overlay_spec(
        title="El naufragio del Batavia en 1629",
        cfg=_cfg(),
        raw_l1="BATAVIA",
        raw_l2="Batavia naufragio del mar",
    )
    assert "batavia" not in spec.l2.lower()


def test_build_spec_strips_cliche_badge():
    spec = build_overlay_spec(
        title="La expedición Franklin desapareció en el hielo",
        cfg=_cfg(),
        raw_l1="FRANKLIN",
        raw_l2="129 hombres",
        raw_badge="CASO REAL",
    )
    assert spec.badge == ""


def test_build_spec_keeps_evidence_badge():
    spec = build_overlay_spec(
        title="La expedición Franklin desapareció en el hielo",
        cfg=_cfg(),
        raw_l1="FRANKLIN",
        raw_l2="129 hombres",
        raw_badge="DOCUMENTAL",
    )
    assert spec.badge == "DOCUMENTAL"


def test_emphasis_belongs_to_overlay():
    spec = build_overlay_spec(
        title="La expedición Franklin desapareció en el hielo",
        cfg=_cfg(),
        raw_l1="FRANKLIN",
        raw_l2="129 hombres",
    )
    assert spec.emphasis
    assert spec.emphasis in {w.lower() for w in (spec.l1 + " " + spec.l2).split()}


def test_render_and_db_fields_roundtrip():
    spec = OverlaySpec(l1="FRANKLIN", l2="129 HOMBRES", badge="DOCUMENTAL",
                       emphasis="franklin", variant_strategy="subject_hero",
                       source="llm")
    assert render_overlay(spec) == "FRANKLIN | 129 HOMBRES"
    fields = spec_to_db_fields(spec)
    assert fields["thumbnail_text"] == "FRANKLIN | 129 HOMBRES"
    assert fields["thumbnail_emphasis"] == "franklin"
    assert fields["thumbnail_variant_strategy"] == "subject_hero"
    assert OverlaySpec.from_dict(fields and spec.as_dict()).l1 == "FRANKLIN"


def test_validate_spec_flags_empty_offtopic_and_credibility():
    cfg = _cfg()
    reasons, _ = validate_overlay_spec(OverlaySpec(), "Un caso", cfg)
    assert "overlay_empty" in reasons

    reasons2, _ = validate_overlay_spec(
        OverlaySpec(l1="ZZZ", l2="algo"), "La expedición Franklin", cfg
    )
    assert "overlay_off_topic" in reasons2

    reasons3, _ = validate_overlay_spec(
        OverlaySpec(l1="FRANKLIN", l2="129", badge="CASO REAL"),
        "La expedición Franklin", cfg,
    )
    assert "credibility_stamp" in reasons3


def test_validate_spec_accepts_coherent_spec():
    cfg = _cfg()
    reasons, _ = validate_overlay_spec(
        OverlaySpec(l1="FRANKLIN", l2="129 HOMBRES", badge="DOCUMENTAL"),
        "La expedición Franklin desapareció en el hielo", cfg,
    )
    assert reasons == []
