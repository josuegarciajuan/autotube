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


# ── Falsos positivos corregidos (ago 2026): el filtro determinista era
# demasiado amplio e inanicionaba canales legítimos (médico/histórico).
# Estos casos deben pasar el pre-filtro; el LLM evalúa los matices. ──
def test_historia_asesinato_no_sensacionalista_permite():
    # Título con 'muerte' + guion con 'asesinado' sin marcador criminal
    # (guerrilla/cartel/víctima): documental histórico, NO true-crime spam.
    assert classify_topic_safety(
        topic="La trágica muerte de Daniel Tupý: un eco nacional en Eslovaquia",
        title="La trágica muerte de Daniel Tupý",
        script_texts=[
            "Daniel Tupý fue asesinado en 2005 en un incidente que conmocionó a "
            "Eslovaquia. La investigación histórica..."
        ],
        use_llm=False,
    ).safe


def test_medico_congenito_recien_nacido_permite():
    # Documental médico de síndromes congénitos (nicho legítimo de canal5).
    assert classify_topic_safety(
        topic="El síndrome Allan–Herndon–Dudley: un misterio médico sin resolver",
        title="El síndrome Allan–Herndon–Dudley",
        script_texts=[
            "Este síndrome congénito afecta a niños recién nacidos y causa "
            "problemas de desarrollo..."
        ],
        use_llm=False,
    ).safe


def test_medico_sindromes_raros_permite():
    assert _verdict("Los síndromes raros: misterios que desafían la medicina moderna")


def test_historia_reina_tragica_permite():
    assert _verdict("El legado oculto de un monarca y su reina trágica")


def test_religion_ciencia_y_biblia_permite():
    assert _verdict("La Ciencia y la Biblia: Revelando la Historia Oculta de la Creación")


def test_culturas_tierra_madre_permite():
    assert _verdict("América como Tierra Madre: Misterios y Conexiones Esenciales")


# ── El contexto adverso SÍ bloquea (no se debilitó la protección) ──
def test_menor_asesinado_contexto_bloquea():
    assert not _verdict("La niña asesinada por el cartel")


def test_menor_violado_contexto_bloquea():
    assert not _verdict("La niña víctima de abuso en el orfanato")


def test_bebe_desaparecido_contexto_bloquea():
    assert not _verdict("El bebé desapareció de la guardería y nadie lo encontró")
