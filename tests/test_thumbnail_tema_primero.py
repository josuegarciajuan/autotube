"""Tests del rediseño de miniaturas tema-primero (sep 2026).

Cubre:
  - Color por contenido: buckets de tono, distancia, acento anti-repetición.
  - Dirección de arte: subject_type -> face_role, plan de proveedores y rotación
    de layout sin repetir los últimos N.
  - Validador de diversidad (layout/color/overlay).
  - Composición: layouts nuevos, énfasis de texto, marco corporativo y chip de
    cara no rompen el render.
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from pipeline import thumbnail_color as tcolor
from pipeline.thumbnail_art_director import (
    TOPIC_FIRST_DIRECTIVE,
    ThumbnailArtDirector,
    build_recent_context,
    normalize_subject_type,
    resolve_face_role,
    resolve_layout,
    resolve_provider_plan,
)


# ── Color ───────────────────────────────────────────────────────

def test_hue_bucket_and_distance():
    assert tcolor.hue_bucket((255, 0, 0)) == 0
    assert tcolor.hue_bucket((0, 0, 255)) == 8
    assert tcolor.hue_distance(350, 10) == 20
    assert tcolor.hue_distance(0, 180) == 180


def test_color_key_roundtrip():
    key = tcolor.color_key((200, 60, 40))
    assert key.startswith("hue_")
    assert tcolor.hue_of_key(key) is not None
    assert tcolor.hue_of_key("garbage") is None


def test_contrast_text_color():
    assert tcolor.contrast_text_color((10, 10, 10)) == "#FFFFFF"
    assert tcolor.contrast_text_color((245, 245, 245)) == "#111111"


def test_choose_accent_avoids_recent_hue():
    dominant = [(200, 40, 40)]  # red
    recent = [tcolor.hue_bucket((200, 40, 40)) * 30]
    accent = tcolor.choose_accent(dominant, recent_hues=recent,
                                  min_distance=40, seed=0)
    accent_rgb = tuple(int(accent.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    hue = tcolor.rgb_to_hsv(accent_rgb)[0] * 360
    assert tcolor.hue_distance(hue, recent[0]) >= 40


# ── Dirección de arte ───────────────────────────────────────────

def test_subject_type_heuristics():
    assert normalize_subject_type("", "La expedición perdida de Franklin") == "place"
    assert normalize_subject_type("", "El síndrome de la mano ajena") == "medical"
    assert normalize_subject_type("place") == "place"
    assert normalize_subject_type("", "Un concepto abstracto") == "concept"


def test_face_role_only_protagonist_for_person():
    assert resolve_face_role("person", True) == "protagonist"
    assert resolve_face_role("place", True) in ("none", "secondary")
    assert resolve_face_role("person", False) == "none"


def test_provider_plan_face_always_stock():
    plan = resolve_provider_plan("medical", "secondary")
    assert plan["face"] == "stock"
    plan_none = resolve_provider_plan("place", "none")
    assert plan_none["face"] == "none"


def test_resolve_layout_avoids_recent():
    pool = ["topic_hero", "subject_closeup", "artifact_document", "split_diagonal"]
    recent = ["topic_hero", "subject_closeup", "artifact_document"]
    chosen = resolve_layout(pool, recent, depth=3, seed="v1")
    assert chosen not in recent[:3]


def test_resolve_layout_honours_preferred_when_not_blocked():
    pool = ["topic_hero", "subject_closeup"]
    assert resolve_layout(pool, ["topic_hero"], preferred="subject_closeup") == "subject_closeup"


def test_art_director_plan_person_stock():
    d = ThumbnailArtDirector()
    art = d.plan(
        title="El caso de Phineas Gage y su cerebro",
        script_text="un hombre sobrevivio a una barra de hierro",
        layout_pool=["topic_hero", "portrait_hero"],
        recent_context={"layouts": [], "colors": [], "subjects": []},
    )
    assert art.subject_type == "person"
    assert art.face_role == "protagonist"
    assert art.provider_face == "stock"


def test_build_recent_context_filters_empty():
    ctx = build_recent_context([
        {"thumbnail_layout": "topic_hero", "thumbnail_color_key": "hue_210",
         "thumbnail_subject": "Franklin"},
        {"thumbnail_layout": "", "thumbnail_color_key": "", "thumbnail_subject": ""},
    ])
    assert ctx["layouts"] == ["topic_hero"]
    assert ctx["colors"] == ["hue_210"]
    assert ctx["subjects"] == ["Franklin"]


def test_topic_first_directive_mentions_subject_rule():
    assert "IMAGEN PRINCIPAL" in TOPIC_FIRST_DIRECTIVE
    assert "ROSTRO HUMANO SOLO ES PROTAGONISTA" in TOPIC_FIRST_DIRECTIVE


# ── Diversidad ──────────────────────────────────────────────────

def test_validate_diversity_flags_repeated_layout_and_color():
    from api.services.packaging_policy import validate_thumbnail_diversity

    class Cfg:
        THUMBNAIL_LAYOUT_HISTORY_DEPTH = 3
        THUMBNAIL_ACCENT_HUE_DISTANCE_MIN = 40

    recent = [
        {"layout": "topic_hero", "color_key": "hue_210", "overlay": "1971 MADRID"},
        {"layout": "split_diagonal", "color_key": "hue_030", "overlay": "COLAPSO"},
    ]
    new = {"layout": "topic_hero", "color_key": "hue_210", "overlay": "1971 MADRID"}
    res = validate_thumbnail_diversity(new, recent, Cfg())
    assert not res.valid
    assert "layout_repeated" in res.reasons
    assert "color_repeated" in res.reasons
    assert "overlay_repeated" in res.reasons


def test_validate_diversity_ok_when_distinct():
    from api.services.packaging_policy import validate_thumbnail_diversity

    class Cfg:
        THUMBNAIL_LAYOUT_HISTORY_DEPTH = 3
        THUMBNAIL_ACCENT_HUE_DISTANCE_MIN = 40

    res = validate_thumbnail_diversity(
        {"layout": "negative_space_top", "color_key": "hue_150", "overlay": "NUEVO ANGULO"},
        [{"layout": "topic_hero", "color_key": "hue_210", "overlay": "1971 MADRID"}],
        Cfg(),
    )
    assert res.valid


# ── Composición ─────────────────────────────────────────────────

def _maker(tmp_path):
    from pipeline.thumbnail_maker import ThumbnailMaker

    class Cfg:
        THUMBNAIL_WIDTH = 1280
        THUMBNAIL_HEIGHT = 720
        THUMBNAIL_FONT_SIZE = 56
        THUMBNAIL_BORDER_WIDTH = 5
        THUMBNAIL_BORDER_COLOR = "#CC0000"
        THUMBNAIL_FONT_FAMILY = "DejaVuSans-Bold"
        THUMBNAIL_SHOW_4K_BADGE = False
        THUMBNAIL_TEXT_STROKE_WIDTH = 3
        THUMBNAIL_TEXT_STROKE_COLOR = "#000000"
        THUMBNAIL_VISUAL_STYLE = "moody_atmospheric"
        THUMBNAIL_MANUAL_STYLE = None
        THUMBNAIL_FRAME_STYLE = "corner_marks"
        COLOR_PALETTE = {"primary": "#1A1A3E", "accent": "#D4AF37", "text": "#FFFFFF"}
        CANAL_DISPLAY_NAME = "Test"
        THUMBNAILS_DIR = str(tmp_path)

    return ThumbnailMaker(config=Cfg())


def test_compose_all_new_layouts(tmp_path):
    base = tmp_path / "base.jpg"
    Image.new("RGB", (640, 360), (30, 90, 160)).save(base)
    m = _maker(tmp_path)
    m.layout_pool = ["topic_hero", "subject_closeup"]
    from pipeline.thumbnail_brainstorm import ThumbnailBrief
    brief = ThumbnailBrief(text_gancho="1971 MADRID", text_complemento="Lo que ocultaron")
    layouts = [
        "topic_hero", "subject_closeup", "artifact_document", "split_diagonal",
        "negative_space_top", "center_burst", "portrait_hero",
    ]
    for layout in layouts:
        out = m._compose_final(
            base_image=base, brief=brief, style={}, overlay_text="", title="Prueba",
            canal_slug="test", video_id=0, text_gancho=brief.text_gancho,
            text_complemento=brief.text_complemento, badge_text="ARCHIVO",
            layout=layout, color_plan={"accent": "#FF5C00", "text": "#FFFFFF"},
            emphasis_word="MADRID",
        )
        assert Path(out).exists(), layout


def test_compose_face_chip_and_frame(tmp_path):
    base = tmp_path / "base.jpg"
    face = tmp_path / "face.jpg"
    Image.new("RGB", (640, 360), (30, 90, 160)).save(base)
    Image.new("RGB", (400, 500), (200, 180, 150)).save(face)
    m = _maker(tmp_path)
    m.frame_style = "film_strip"
    from pipeline.thumbnail_brainstorm import ThumbnailBrief
    out = m._compose_final(
        base_image=base, brief=ThumbnailBrief(text_gancho="SIN SEÑAL"),
        style={}, overlay_text="", title="x", canal_slug="t", video_id=1,
        text_gancho="SIN SEÑAL", text_complemento="", badge_text="ARCHIVO",
        layout="topic_hero", color_plan={"accent": "#FF5C00", "text": "#FFFFFF"},
        face_chip_path=face,
    )
    assert Path(out).exists()


def test_draw_text_line_emphasis_signature():
    """El énfasis de palabra existe y el helper sigue aceptando accent_rgb."""
    from pipeline.thumbnail_maker import ThumbnailMaker
    sig = inspect.signature(ThumbnailMaker._draw_text_line)
    assert "emphasis_word" in sig.parameters
    assert "accent_rgb" in sig.parameters


def test_thematic_overlays_off_by_default():
    from pipeline.thumbnail_maker import ThumbnailMaker
    src = inspect.getsource(ThumbnailMaker._compose_final)
    assert "thematic_overlays_enabled" in src


# ── Configs por canal ───────────────────────────────────────────

def test_channels_define_topic_first_pools():
    import importlib.util

    for slug in ("canal2", "canal3", "canal4", "canal5"):
        spec = importlib.util.spec_from_file_location(slug, f"config/{slug}_config.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert getattr(mod, "THUMBNAIL_COLOR_MODE", "") == "image_content", slug
        pool = getattr(mod, "THUMBNAIL_LAYOUT_POOL", [])
        assert len(pool) >= 4, f"{slug} layout pool too small"
        assert "topic_hero" in pool, f"{slug} missing topic_hero"


def test_defaults_thumbnail_diversity_settings():
    from config import defaults

    assert defaults.THUMBNAIL_COLOR_MODE == "image_content"
    assert defaults.THUMBNAIL_LAYOUT_HISTORY_DEPTH >= 1
    assert defaults.THUMBNAIL_ACCENT_HUE_DISTANCE_MIN >= 20
    assert defaults.THUMBNAIL_EMPHASIS_ENABLED is True
    assert defaults.THUMBNAIL_THEMATIC_OVERLAYS_ENABLED is False
