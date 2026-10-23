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


def test_l2_no_se_mutila_por_compartir_palabra_con_titulo():
    """build_overlay_spec no debe borrar palabras de L2 por aparecer en el
    título o en L1: debe conservar la frase o reformularla entera."""
    title = "¿Viajeros del tiempo reales? los 10 casos que la ciencia"
    spec = build_overlay_spec(title, CFG, raw_l1="10 CASOS", raw_l2="LA CIENCIA LO CALLÓ")
    assert spec.l2 == "LA CIENCIA LO CALLÓ"


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


def test_interrogativos_no_son_terminos_de_contenido():
    terms = title_terms("¿Quién construyó las maravillas imposibles de la antigüedad?")
    assert "quien" not in terms


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


def test_l1_dentro_de_presupuesto_cabe_con_margen_en_layout_1_linea():
    """L1 de 14 chars (máximo del presupuesto) debe caber con margen seguro:
    el ajuste de fuente reduce el tamaño en vez de desbordar el canvas."""
    from PIL import Image, ImageDraw

    from pipeline.thumbnail_maker import _fit_font_size

    draw = ImageDraw.Draw(Image.new("RGB", (1280, 720)))
    start = max(80, int(720 * 0.19))  # punto de partida del layout de 1 línea
    max_w = 1280 - 2 * 48
    size, font = _fit_font_size(draw, "GUERRAS MUNDIAL", start, max_w, 56)
    bbox = draw.textbbox((0, 0), "GUERRAS MUNDIAL", font=font)
    assert bbox[2] - bbox[0] <= max_w
    assert size < start  # hubo reducción respecto al tamaño fijo original


def test_red_de_seguridad_no_corta_a_mitad_de_palabra():
    """La red de seguridad del maker recorta en frontera de palabra:
    "GUERRAS MUNDIAL" (15 chars) → "GUERRAS", nunca "GUERRAS MUNDIA"."""
    from pipeline.packaging_brief import _truncate_words

    assert _truncate_words("GUERRAS MUNDIAL", 14) == "GUERRAS"


# ═══════════════════════════════════════════════════════════════════
# FALLO 4 — Título completo truncado tras la selección (metadata_generator:646)
# ═══════════════════════════════════════════════════════════════════


def test_paso_post_seleccion_nunca_rompe_titulo_completo():
    """La resolución de título del generador elige un candidato COMPLETO
    dentro de presupuesto; si el seleccionado no cabe, se reelige — nunca
    se devuelve un corte a mitad de cláusula."""
    from pipeline.metadata_generator import MetadataGenerator

    gen = MetadataGenerator.__new__(MetadataGenerator)
    gen.title_max_chars = 65
    titulo_completo_largo = (
        "El misterio del sistema solar exterior que la NASA aún no logra explicar"
    )
    alternativo = "La NASA confirma el hallazgo que cambia el sistema solar"
    elegido = gen._pick_complete_title(
        [titulo_completo_largo, alternativo], 65
    )
    assert elegido == alternativo
    assert is_complete_title(elegido)[0] and len(elegido) <= 65
