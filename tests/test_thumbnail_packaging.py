"""Tests D2 thumbnails (ago 2026).

Cubre:
  - P3: _dedupe_overlay recorta del texto de miniatura las palabras que
    repiten el inicio del título (curiosity gap).
  - P1: persistencia de thumbnail_style por video + agregación CTR por estilo
    (loop packaging).
  - Config: 4K badge desactivado y fuente sans-serif en canal3.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import importlib.util


# ── P3: dedupe overlay vs título ────────────────────────────────

def _maker():
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
        THUMBNAIL_RESCUE_MAYDAY = False
        THUMBNAIL_RESCUE_COORDINATES = False
        THUMBNAIL_RESCUE_SIN_SENAL = False
        THUMBNAIL_MEDICAL_ECG = False
        THUMBNAIL_MEDICAL_CROSS = False
        THUMBNAIL_MEDICAL_DIAGNOSIS = False
        THUMBNAIL_VISUAL_STYLE = "clinical_mystery"
        THUMBNAIL_MANUAL_STYLE = None
        COLOR_PALETTE = {"primary": "#0F203E", "accent": "#D4AF37", "text": "#FFFFFF"}
        CANAL_DISPLAY_NAME = "Test"
        THUMBNAILS_DIR = "/tmp/thumb_test"
        THUMBNAIL_WIDTH_DEFAULT = 1280
    return ThumbnailMaker(config=Cfg())


def test_dedupe_overlay_repite_inicio_titulo():
    m = _maker()
    out = m._dedupe_overlay("La Atlántida: ¿pruebas reales?", "La Atlántida NO existió")
    assert out == "NO existió"


def test_dedupe_overlay_no_repite_deja_intacto():
    m = _maker()
    out = m._dedupe_overlay("La Atlántida: ¿pruebas reales?", "NO existió")
    assert out == "NO existió"


def test_dedupe_overlay_una_sola_palabra_no_toca():
    m = _maker()
    # Una sola palabra coincidente no es repetición suficiente (mín 2)
    out = m._dedupe_overlay("El Misterio de la Cueva", "El enigma oculto")
    assert out == "El enigma oculto"


def test_dedupe_overlay_titulo_vacio():
    m = _maker()
    assert m._dedupe_overlay("", "Cualquier texto") == "Cualquier texto"
    assert m._dedupe_overlay("Título", "") == ""


# ── Regresión: el prompt de escena secundaria NUNCA se pinta ────
# Bug: el recuadro inferior izquierdo de la miniatura mostraba
# "UN PRIMER PLANO DE UN DIARIO D..." (= secondary_scene[:30].upper()).
# Fix: _draw_insets usa etiqueta fija o escena real. Estos tests
# bloquean cualquier reintroducción del parámetro en la composición.

def test_compose_final_no_acepta_secondary_scene():
    """_compose_final NO puede recibir secondary_scene (parámetro eliminado)."""
    import inspect
    from pipeline.thumbnail_maker import ThumbnailMaker
    sig = inspect.signature(ThumbnailMaker._compose_final)
    assert "secondary_scene" not in sig.parameters


def test_draw_insets_nunca_pinta_el_prompt():
    """El recuadro inset usa etiqueta fija; el prompt no puede llegar ahí."""
    import inspect
    from pipeline.thumbnail_maker import ThumbnailMaker
    src = inspect.getsource(ThumbnailMaker._draw_insets)
    # El bug original truncaba el prompt como etiqueta:
    assert "secondary_scene" not in src
    # La etiqueta del recuadro A (inferior izquierdo) es fija:
    assert 'label_a = "DOCUMENTO REAL" if "documento" in title.lower() else "EVIDENCIA"' in src


def test_flujo_thumbnails_sin_secondary_scene():
    """Ningún punto del flujo v2 pasa secondary_scene a la composición."""
    import inspect
    from pipeline.thumbnail_maker import ThumbnailMaker
    for method_name in ("make_viral_thumbnail", "make_variant_thumbnails", "_compose_final"):
        src = inspect.getsource(getattr(ThumbnailMaker, method_name))
        assert "secondary_scene=" not in src, f"{method_name} reintroduce secondary_scene"


# ── Config: P2 (4K badge off + sans-serif) ──────────────────────

def test_config_4k_badge_off_y_sans():
    for slug in ["canal2", "canal3", "canal4", "canal5"]:
        spec = importlib.util.spec_from_file_location(
            slug, f"config/{slug}_config.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        assert m.THUMBNAIL_SHOW_4K_BADGE is False, f"{slug} 4K badge on"
        assert "Sans" in m.THUMBNAIL_FONT_FAMILY, f"{slug} no es sans-serif"


# ── P1: persistencia + agregación CTR por estilo ────────────────

def test_thumbnail_style_persist_and_aggregate(tmp_path):
    from database.db_extended import ExtendedDatabase
    db_path = tmp_path / "thumb.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER, yt_video_id TEXT,
            titulo_final TEXT, status TEXT DEFAULT 'published',
            thumbnail_style TEXT DEFAULT '',
            thumbnail_layout TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE video_stats_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER, yt_video_id TEXT,
            views INTEGER DEFAULT 0, likes INTEGER DEFAULT 0, comments INTEGER DEFAULT 0,
            estimated_minutes_watched REAL DEFAULT 0,
            average_view_duration REAL DEFAULT 0,
            subscribers_gained INTEGER DEFAULT 0,
            estimated_revenue_min REAL DEFAULT 0,
            estimated_revenue_max REAL DEFAULT 0,
            embeddable INTEGER DEFAULT 1,
            analytics_data_exists INTEGER DEFAULT 0,
            impressions INTEGER DEFAULT 0, ctr REAL DEFAULT 0,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute("INSERT INTO videos (id, channel_id, yt_video_id, thumbnail_style) VALUES (1, 3, 'V1', '')")
    conn.execute("INSERT INTO videos (id, channel_id, yt_video_id, thumbnail_style) VALUES (2, 3, 'V2', '')")
    conn.commit()
    conn.close()

    db = ExtendedDatabase(db_path)
    assert db.update_video_thumbnail_style(1, "distress_signal", "split_face")
    assert db.update_video_thumbnail_style(2, "clinical_mystery", "shock_closeup")

    db.insert_video_stats(1, "V1", {"viewCount": 100, "impressions": 2000,
                                    "impressionsClickThroughRate": 0.05})
    db.insert_video_stats(2, "V2", {"viewCount": 50, "impressions": 1000,
                                    "impressionsClickThroughRate": 0.03})

    rows = db.get_thumbnail_style_ctr(3)
    by_style = {r["style"]: r for r in rows}
    assert "distress_signal" in by_style
    assert "clinical_mystery" in by_style
    assert abs(by_style["distress_signal"]["avg_ctr"] - 5.0) < 0.01
    assert abs(by_style["clinical_mystery"]["avg_ctr"] - 3.0) < 0.01
    assert by_style["distress_signal"]["total_impressions"] == 2000


def test_thumbnail_style_migration_v45(tmp_path):
    """La migración v45 añade la columna thumbnail_style (idempotente)."""
    import logging
    import database.db_extended as de
    db_path = tmp_path / "mig.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE videos (id INTEGER PRIMARY KEY, canal TEXT)")
    conn.commit()
    de._migrate_v45(conn, logging.getLogger("test"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(videos)")}
    assert "thumbnail_style" in cols
    assert "thumbnail_layout" in cols
    # idempotente: segunda llamada no falla
    de._migrate_v45(conn, logging.getLogger("test"))
    conn.close()
