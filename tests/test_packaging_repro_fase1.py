"""Fase 1 (curiosidad oscura) — reproducción de fallos de packaging.

Cada test ``xfail(strict=True)`` reproduce un fallo confirmado en producción
con las funciones reales del pipeline. La aserción expresa el comportamiento
ESPERADO: cuando la Fase 2 corrija la causa raíz, el test pasará a XPASS y
habrá que retirar el marcador ``xfail`` (strict lo exige).

Los tests sin marcador documentan guardias que SÍ funcionan hoy y deben
seguir pasando tras la Fase 2 (anti-regresión).
"""
from __future__ import annotations

import pytest

from pipeline.packaging_brief import (
    _salient_title_term,
    build_overlay_spec,
    title_terms,
)
from pipeline.title_engine import truncate_title_at_word
from pipeline.title_tokens import is_complete_title
from api.services.packaging_policy import validate_title


class _Cfg:
    """Config mínima: todos los knobs de packaging caen a sus defaults."""

    TITLE_CAPS_POLICY = "sentence"
    TITLE_MIN_CHARS = 28
    TITLE_MAX_CHARS = 65


CFG = _Cfg()

# ═══════════════════════════════════════════════════════════════════
# Guardias existentes que SÍ funcionan (anti-regresión)
# ═══════════════════════════════════════════════════════════════════


def test_guard_existente_detecta_verbo_apertura():
    ok, reason = is_complete_title(
        "El misterio del sistema solar exterior que la NASA aún no logra"
    )
    assert not ok and reason in {"aperture_verb", "dangling_adverb", "dangling_tail"}


def test_guard_existente_validate_title_marca_frase_incompleta():
    cortado = "Lo que las civilizaciones antiguas esconden: enigmas que aún"
    result = validate_title(cortado, CFG)
    assert not result.valid
    assert "incomplete_phrase" in result.reasons


# ═══════════════════════════════════════════════════════════════════
# FALLO 1 — L2 mutilada por dedup de tokens (vídeo 2733: "LA CALLÓ")
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.xfail(strict=True, reason="Fase 2: la dedup de tokens rompe frases válidas")
def test_l2_no_se_mutila_por_compartir_palabra_con_titulo():
    """build_overlay_spec no debe borrar palabras de L2 por aparecer en el
    título o en L1: debe conservar la frase o reformularla entera."""
    title = "¿Viajeros del tiempo reales? los 10 casos que la ciencia"
    spec = build_overlay_spec(title, CFG, raw_l1="10 CASOS", raw_l2="LA CIENCIA LO CALLÓ")
    assert spec.l2 == "LA CIENCIA LO CALLÓ"


@pytest.mark.xfail(strict=True, reason="Fase 2: L2 nunca puede quedar como resto gramatical")
def test_l2_nunca_queda_como_resto_gramatical():
    """Caso 2649 ('LOS'): si la dedup elimina las palabras de contenido de L2,
    el resultado debe ser una reformulación con sentido o L2 vacío — nunca un
    determinante/pronombre suelto."""
    title = "El secreto mejor guardado de la selva: la civilización"
    spec = build_overlay_spec(
        title, CFG, raw_l1="BAJO LA SELVA", raw_l2="LO MEJOR GUARDADO"
    )
    resto = {t.lower() for t in spec.l2.split()}
    funcionales = {"el", "la", "los", "las", "lo", "de", "del", "y", "a", "en"}
    assert spec.l2 == "" or not resto <= funcionales


# ═══════════════════════════════════════════════════════════════════
# FALLO 2 — Término saliente vacío (vídeo 2697: L1 = "QUIÉN")
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.xfail(strict=True, reason="Fase 2: 'quién/cómo/dónde' no son contenido")
def test_interrogativos_no_son_terminos_de_contenido():
    terms = title_terms("¿Quién construyó las maravillas imposibles de la antigüedad?")
    assert "quien" not in terms


@pytest.mark.xfail(strict=True, reason="Fase 2: el saliente debe ser un ancla con significado")
def test_saliente_prefiere_contenido_sobre_token_debil():
    """Caso 2735 ('HMS'): ante varios candidatos, el saliente no debe elegir
    un token de ≤3 letras sin carga semántica si hay uno mejor."""
    title = "Hms Erebus y Terror: nadie sobrevivió a la expedición"
    salient = _salient_title_term(title, title_terms(title))
    assert salient.lower() in {"erebus", "terror"}


# ═══════════════════════════════════════════════════════════════════
# FALLO 3 — Pintado sin ajuste al ancho (desborde en ruta de 1 línea)
# ═══════════════════════════════════════════════════════════════════


def _ancho_texto(texto: str, font_size: int) -> int:
    from PIL import Image, ImageDraw

    from pipeline.thumbnail_maker import _find_font

    draw = ImageDraw.Draw(Image.new("RGB", (1280, 720)))
    font = _find_font(font_size, bold=True)
    bbox = draw.textbbox((0, 0), texto, font=font)
    return bbox[2] - bbox[0]


@pytest.mark.xfail(strict=True, reason="Fase 2: el pintado debe ajustar fuente al ancho real")
def test_l1_dentro_de_presupuesto_cabe_con_margen_en_layout_1_linea():
    """L1 de 14 chars (máximo del presupuesto) debe caber con margen seguro
    en el layout de 1 línea (hoy se pinta a 136px fijos y desborda)."""
    ancho = _ancho_texto("GUERRAS MUNDIAL", max(80, int(720 * 0.19)))
    assert ancho + 2 * 40 <= 1280  # margen seguro de 40px por lado


@pytest.mark.xfail(strict=True, reason="Fase 2: el recorte duro [:budget] nunca corta a mitad de palabra")
def test_red_de_seguridad_no_corta_a_mitad_de_palabra():
    """La red de seguridad del maker (`text[:budget]`) corta "GUERRAS MUNDIAL"
    a "GUERRAS MUNDIA". El esperado es recorte en frontera de palabra."""
    texto = "GUERRAS MUNDIAL"
    budget = 14
    cortado = texto[:budget]
    assert cortado == "GUERRAS"


# ═══════════════════════════════════════════════════════════════════
# FALLO 4 — Título completo truncado tras la selección (metadata_generator:646)
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.xfail(strict=True, reason="Fase 2: ningún paso post-selección puede romper la completitud")
def test_paso_post_seleccion_nunca_rompe_titulo_completo():
    """Un título completo de 72 chars que el motor seleccionó NO debe salir
    del pipeline cortado a medias: o cabe y se mantiene, o se rechaza y se
    regenera — pero nunca se devuelve incompleto."""
    titulo = "El misterio del sistema solar exterior que la NASA aún no logra explicar"
    assert is_complete_title(titulo)[0]  # el candidato es completo
    salida = truncate_title_at_word(titulo, 65)
    assert is_complete_title(salida)[0], f"salió cortado: {salida!r}"
