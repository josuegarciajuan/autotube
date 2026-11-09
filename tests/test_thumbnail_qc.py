"""Tests de la ronda CTR (nov 2026): QC determinista, copy y relevancia."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PIL import Image, ImageDraw

from pipeline.packaging_brief import (
    _has_new_information,
    _truncate_words,
    build_overlay_spec,
    line_is_dangling,
)
from pipeline.thumbnail_qc import (
    contrast_ratio,
    line_is_dangling as qc_line_is_dangling,
    parse_box,
    run_qc,
)
from pipeline.thumbnail_relevance import (
    check_relevance,
    enforce_subject_relevance,
    extract_places,
)


class Cfg:
    THUMBNAIL_OVERLAY_BUDGETS = {"l1": 14, "l2": 24, "badge": 10}
    THUMBNAIL_REQUIRE_TEXT = True
    THUMBNAIL_BADGE_CLICHES = []
    THUMBNAIL_WIDTH = 1280
    THUMBNAIL_HEIGHT = 720


class ComposeCfg(Cfg):
    THUMBNAIL_FONT_SIZE = 64
    THUMBNAIL_BORDER_WIDTH = 5
    THUMBNAIL_BORDER_COLOR = "#CC0000"
    THUMBNAIL_FONT_FAMILY = "DejaVuSans-Bold"
    THUMBNAIL_SHOW_4K_BADGE = False
    THUMBNAIL_TEXT_STROKE_WIDTH = 3
    THUMBNAIL_TEXT_STROKE_COLOR = "#000000"
    THUMBNAIL_RESCUE_MAYDAY = False
    THUMBNAIL_RESCUE_COORDINATES = False
    THUMBNAIL_RESCUE_SIN_SENAL = False
    THUMBNAIL_MEDICAL_ECG = False
    THUMBNAIL_MEDICAL_CROSS = False
    THUMBNAIL_MEDICAL_DIAGNOSIS = False
    THUMBNAIL_VISUAL_STYLE = "ancient_mystery"
    THUMBNAIL_MANUAL_STYLE = None
    COLOR_PALETTE = {"primary": "#0F203E", "accent": "#D4AF37", "text": "#FFFFFF"}
    CANAL_DISPLAY_NAME = "Test"
    THUMBNAILS_DIR = "/tmp/thumb_qc_test"
    THUMBNAIL_SALIENT_CROP = False
    THUMBNAIL_TEXT_PANEL_MODE = "never"
    THUMBNAIL_EMPHASIS_ENABLED = False


# ── Copy: nunca truncar una frase ────────────────────────────────

def test_truncate_never_leaves_dangling_connector():
    assert _truncate_words("ERA UNA CIUDAD BAJO LA", 24) == "ERA UNA CIUDAD"
    assert _truncate_words("3.000 METROS SIN", 24) == "3.000 METROS"
    assert _truncate_words("PILAR DEL", 14) == "PILAR"
    assert line_is_dangling("CIUDAD BAJO LA") is True
    assert qc_line_is_dangling("CIUDAD BAJO LA") is True
    assert line_is_dangling("CIUDAD BAJO CENIZA") is False


def test_l2_without_new_information_is_dropped():
    title = "Enfermera despedida en California: 2 semanas tras la queja"
    assert _has_new_information("LA QUEJA SILENCIADA", title) is False
    spec = build_overlay_spec(
        title, Cfg(), raw_l1="2 SEMANAS", raw_l2="LA QUEJA SILENCIADA",
    )
    assert spec.l2 == ""
    assert spec.l1 == "2 SEMANAS"


def test_l2_with_data_is_kept():
    title = "El K2: la montaña que devora a quienes se atreven a tocarla"
    spec = build_overlay_spec(
        title, Cfg(), raw_l1="K2", raw_l2="EL CUELLO DE BOTELLA",
    )
    assert spec.l1 == "K2"
    assert spec.l2 == "EL CUELLO DE BOTELLA"


def test_overlay_line_never_ends_in_stopword():
    title = "¿Y si la Atlántida nunca fue una isla sino algo mucho más?"
    spec = build_overlay_spec(
        title, Cfg(), raw_l1="ATLÁNTIDA", raw_l2="ERA UNA CIUDAD BAJO LA",
    )
    assert spec.l2
    assert spec.l2.split()[-1] not in {"LA", "BAJO", "DE", "QUE", "SIN"}


# ── QC ───────────────────────────────────────────────────────────

def _flat(color=(40, 40, 40), size=(1280, 720)):
    return Image.new("RGB", size, color)


def test_qc_flags_dangling_and_empty_overlay():
    result = run_qc(_flat(), l1="CIUDAD BAJO LA", l2="", require_text=True)
    assert "text_dangling_l1" in result.issues
    assert result.score < 100


def test_qc_flags_crushed_blacks():
    result = run_qc(_flat((0, 0, 0)), l1="K2", l2="", require_text=True)
    assert "too_dark" in result.issues


def test_qc_flags_text_on_subject():
    img = _flat((120, 120, 120))
    result = run_qc(
        img, l1="K2", l2="", require_text=True,
        text_boxes=[(100, 500, 900, 640)],
        subject_bbox=(80, 480, 940, 660),
    )
    assert "text_overlaps_subject" in result.issues


def test_qc_flags_small_text_at_feed():
    # 56 px at 720 → 7.8 px at a 100 px feed.
    result = run_qc(
        _flat(), l1="K2", l2="", require_text=True,
        text_font_px={"l1": 56},
    )
    assert "l1_too_small" in result.issues


def test_qc_flags_too_many_accents():
    result = run_qc(
        _flat(), l1="K2", l2="", require_text=True,
        accent_colors=[(255, 0, 0), (0, 255, 0), (0, 0, 255)],
    )
    assert "too_many_accents" in result.issues


def test_parse_box_and_contrast():
    assert parse_box("1,2,3,4") == (1, 2, 3, 4)
    assert parse_box("bad") is None
    assert contrast_ratio((0, 0, 0), (255, 255, 255)) > 20


# ── Relevancia geográfica ───────────────────────────────────────

def test_extract_places_cachemira():
    assert "cachemira" in extract_places("Cachemira: el conflicto olvidado")
    assert "australia" in extract_places("Un mapa de Australia")


def test_relevance_detects_wrong_country():
    rel = check_relevance(
        "Cachemira: el conflicto que el mundo prefiere ignorar",
        "Un mapa antiguo de Australia con chinchetas",
    )
    assert rel.ok is False
    assert "australia" in rel.mismatched_places


def test_relevance_rewrites_prompt():
    out = enforce_subject_relevance(
        "Un mapa antiguo con chinchetas",
        "Cachemira: el conflicto que el mundo prefiere ignorar",
        "El valle de Cachemira",
    )
    assert "Cachemira" in out
    assert "Do not depict maps" in out


# ── Composición: L2 no se descarta en layouts de una línea ──────

def test_compose_keeps_l2_on_one_line_layout(tmp_path):
    from pipeline.thumbnail_maker import ThumbnailMaker
    from pipeline.thumbnail_brainstorm import ThumbnailBrief

    base = tmp_path / "base.jpg"
    _flat((70, 60, 40)).save(base, quality=90)
    m = ThumbnailMaker(config=ComposeCfg())
    out = m._compose_final(
        base_image=base,
        brief=ThumbnailBrief(layout="subject_closeup"),
        style={
            "visual_style": "ancient_mystery",
            "effects": {"contrast_boost": 1.1, "saturation": 0.9},
            "text_style": {"uppercase": True},
        },
        overlay_text="K2 | EL CUELLO DE BOTELLA",
        title="El K2: la montaña que devora a quienes se atreven a tocarla",
        canal_slug="test",
        video_id=0,
        text_gancho="K2",
        text_complemento="EL CUELLO DE BOTELLA",
        layout="subject_closeup",
    )
    assert Path(out).exists()
    assert m.last_text_meta["font_px"]["l2"]
    assert m.last_qc_result is not None


def test_face_policy_person_only():
    from pipeline.thumbnail_art_director import ThumbnailArtDirector
    art = ThumbnailArtDirector()
    plan = art.plan(
        title="El síndrome de la mano ajena",
        raw_subject_type="medical",
        allow_faces=True,
        face_role_policy="person_only",
    )
    assert plan.face_role == "none"


def test_variant_selection_uses_primary_spec_and_qc_winner(tmp_path, monkeypatch):
    from pipeline.thumbnail_maker import ThumbnailMaker
    from pipeline.thumbnail_brainstorm import ThumbnailBrief
    from pipeline.thumbnail_qc import QCResult

    m = ThumbnailMaker(config=ComposeCfg())
    base = tmp_path / "base.jpg"
    _flat((60, 60, 60)).save(base, quality=90)

    monkeypatch.setattr(m, "_get_or_create_style", lambda **kw: {
        "visual_style": "ancient_mystery",
        "effects": {"contrast_boost": 1.1, "saturation": 0.9},
        "text_style": {"uppercase": True},
    })
    monkeypatch.setattr(m, "_load_recent_context", lambda *a, **k: {})
    monkeypatch.setattr(m, "_generate_subject_image", lambda **kw: base)
    monkeypatch.setattr(m, "_resolve_color_plan", lambda *a, **k: {
        "accent": "#D4AF37", "text": "#FFFFFF", "shadow": "#000000",
        "color_key": "hue_045",
    })
    monkeypatch.setattr(m, "_fetch_face_chip", lambda *a, **k: None)
    monkeypatch.setattr(m, "_pick_inset_image", lambda *a, **k: None)

    calls = []

    def fake_compose(**kwargs):
        calls.append(kwargs)
        out = tmp_path / f"v{len(calls)}.jpg"
        _flat((60, 60, 60)).save(out, quality=90)
        # First variant (primary) scores lower on purpose.
        m.last_qc_result = QCResult(score=70 if len(calls) == 1 else 95)
        return out

    monkeypatch.setattr(m, "_compose_final", fake_compose)

    briefs = [
        ThumbnailBrief(layout="topic_hero", variant_strategy="subject_hero"),
        ThumbnailBrief(layout="dark_reveal", variant_strategy="data_question"),
    ]
    paths = m.make_variant_thumbnails(
        title="El K2: la montaña que devora a quienes se atreven a tocarla",
        variant_briefs=briefs,
        canal_slug="test",
        video_id=0,
        primary_overlay_spec={
            "l1": "K2", "l2": "EL CUELLO DE BOTELLA", "badge": "",
            "emphasis": "k2", "subject_noun": "K2", "source": "metadata",
        },
    )
    assert paths[0].name == "v2.jpg"  # best QC first
    assert calls[0]["text_gancho"] == "K2"
    assert calls[0]["text_complemento"] == "EL CUELLO DE BOTELLA"
