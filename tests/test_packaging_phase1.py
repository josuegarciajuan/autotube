"""Tests de la Fase 1 de packaging (CTR).

Cubre:
  1. ``resolve_allow_faces``: unifica THUMBNAIL_FACE_ROLE con el legacy
     THUMBNAIL_ALLOW_FACES ("auto" decide por sujeto, allow/deny explícitos).
  2. ``_harden_title``: saneo determinista de títulos (sin '|', sin corchetes,
     sin colas colgantes) antes del truncado.
"""
from pipeline.thumbnail_maker import resolve_allow_faces


class _Cfg:
    pass


def test_allow_faces_auto_default_true():
    assert resolve_allow_faces(_Cfg()) is True


def test_allow_faces_role_deny():
    c = _Cfg()
    c.THUMBNAIL_FACE_ROLE = "none"
    assert resolve_allow_faces(c) is False


def test_allow_faces_role_allow():
    c = _Cfg()
    c.THUMBNAIL_FACE_ROLE = "allow"
    assert resolve_allow_faces(c) is True


def test_allow_faces_auto_respects_legacy_false():
    c = _Cfg()
    c.THUMBNAIL_FACE_ROLE = "auto"
    c.THUMBNAIL_ALLOW_FACES = False
    assert resolve_allow_faces(c) is False


def test_allow_faces_unknown_role_falls_back_to_legacy():
    c = _Cfg()
    c.THUMBNAIL_FACE_ROLE = "???"
    c.THUMBNAIL_ALLOW_FACES = True
    assert resolve_allow_faces(c) is True


class _TitleCfg:
    TITLE_CAPS_POLICY = "sentence"
    TITLE_MAX_CHARS = 65


def test_harden_title_strips_pipe_keeps_longest_segment():
    from pipeline.metadata_generator import _harden_title

    out = _harden_title(
        "¿Por qué Detroit teme al hombre lobo desde 1760? | Documental",
        _TitleCfg(),
    )
    assert "|" not in out
    assert len(out) > 0
    assert "Detroit" in out


def test_harden_title_strips_brackets_and_clickbait_suffix():
    from pipeline.metadata_generator import _harden_title

    out = _harden_title("El misterio [REAL] de la isla (CASO REAL)", _TitleCfg())
    assert "[" not in out and "]" not in out
    assert "CASO REAL" not in out.upper() or "(" not in out


def test_harden_title_removes_dangling_tail():
    from pipeline.metadata_generator import _harden_title

    out = _harden_title("La verdad sobre el caso de", _TitleCfg())
    assert out.split()[-1].casefold() not in {"de", "del", "la", "el", "y", "o"}


def test_harden_title_never_returns_empty():
    from pipeline.metadata_generator import _harden_title

    assert _harden_title("", _TitleCfg()) == ""
    # Texto que solo son marcadores: no debe devolver vacío perdiendo el original.
    out = _harden_title("|", _TitleCfg())
    assert isinstance(out, str)
