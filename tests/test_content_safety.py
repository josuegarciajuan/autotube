"""Tests del filtro anti-strike ampliado (ago 2026).

Verifica que las nuevas categorías del pre-filtro determinista rechazan los
patrones reales de los videos eliminados por YouTube (canal4/canal5 true-crime,
canal2 sobrenatural-como-real, marcadores clickbait de alto riesgo) sin romper
el folklore narrativo que sobrevive.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.content_safety import classify_topic_safety


def _verdict(title: str, topic: str = "") -> bool:
    return classify_topic_safety(
        topic=topic or title, title=title, use_llm=False,
    ).safe


# ── True crime (canal4/canal5 eliminados) ─────────────────────────
def test_true_crime_asesinato_bloqueado():
    assert not _verdict("El capitán que la guerrilla asesinó (CASO REAL)")


def test_true_crime_desaparicion_bloqueada():
    assert not _verdict("129 hombres desaparecieron en el ártico")


def test_true_crime_sin_rastro_bloqueado():
    assert not _verdict("Desapareció sin dejar rastro")


def test_true_crime_secuestro_bloqueado():
    assert not _verdict("El secuestro que conmocionó a la ciudad")


def test_true_crime_nunca_regreso_bloqueado():
    assert not _verdict("5 perdidos en la montaña y NUNCA regresaron")


# ── Sobrenatural presentado como real (canal2 eliminados) ─────────
def test_sobrenatural_susurros_bloqueado():
    assert not _verdict("Los secretos que la luna me susurraba")


def test_sobrenatural_mensaje_universo_bloqueado():
    assert not _verdict("¿Qué mensaje intenta enviarte el universo con esas señales?")


def test_sobrenatural_criptidos_bloqueado():
    assert not _verdict("3 críptidos que la ciencia NO puede explicar (REAL)")


def test_sobrenatural_destino_marcado_bloqueado():
    assert not _verdict("Su destino estaba marcado en una feria (REAL)")


def test_sobrenatural_mas_alla_bloqueado():
    assert not _verdict("Las últimas llamadas desde el más allá")


# ── Marcadores clickbait de alto riesgo (decisión binaria) ────────
def test_clickbait_maldito_bloqueado():
    assert not _verdict("El rodaje maldito de Saigón 1948 (REAL)")


def test_clickbait_nadie_te_conto_bloqueado():
    assert not _verdict("Licencia de enfermería: el loophole que NADIE te contó")


def test_clickbait_increible_bloqueado():
    assert not _verdict("Historias médicas que nadie debería escuchar (Increible)")


def test_clickbait_imposible_bloqueado():
    assert not _verdict("Ruinas que guardan secretos IMPOSIBLES de explicar")


# ── Folklore narrativo que SOBREVIVE (no debe romperse) ───────────
def test_folklore_fantasma_permite():
    assert _verdict("El reto al fantasma que salió terriblemente mal")


def test_folklore_demonio_narrativo_permite():
    assert _verdict("El pastor desafió al demonio en el sótano")


def test_documental_neutro_permite():
    assert _verdict("La técnica ancestral que salva ruinas milenarias")


def test_marcador_real_seguro_permite():
    # 'REAL' es neutro en los datos (0.9-1.7x); solo se bloquea vía tema.
    assert _verdict("¿Qué civilización construyó un santuario 7.000 años (REAL)?")
