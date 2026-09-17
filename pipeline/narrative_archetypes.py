"""Arquetipos narrativos variables (T1.5 del experimento de recuperación).

Problema: el generador aplicaba SIEMPRE el mismo esquema narrativo, lo que
produce vídeos intercambiables entre sí — la definición de "contenido de
plantilla" que YouTube penaliza (ver ``specs/experimento-recuperacion-alcance.md``).

Solución: elegir un arquetipo distinto por vídeo y reflejarlo en el prompt del
outline, de modo que la estructura del relato varíe aunque el nicho sea el mismo.
La variedad de estructura es "sustancia" que el detector de plantillas no puede
atribuir a producción en serie.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

logger = logging.getLogger("autotube.narrative_archetypes")

DEFAULT_ARCHETYPES: list[dict] = [
    {
        "key": "cronologia",
        "name": "Reconstrucción cronológica",
        "guidance": (
            "Ordena el relato desde el primer indicio hasta el desenlace, con "
            "fechas y lugares concretos en cada capítulo. El suspense nace de "
            "'qué pasó después', no de adjetivos."
        ),
    },
    {
        "key": "investigacion",
        "name": "Investigación abierta",
        "guidance": (
            "Sigue a quienes investigaron el caso: qué preguntaban, qué "
            "hallaron y qué quedó sin respuesta. Alterna hallazgo y nueva duda."
        ),
    },
    {
        "key": "caso_paralelo",
        "name": "Caso paralelo",
        "guidance": (
            "Intercala el caso principal con un caso análogo (moderno o "
            "histórico) y usa el contraste para iluminar ambos. Nunca inventes "
            "el caso paralelo: debe ser verificable."
        ),
    },
    {
        "key": "misterio_abierto",
        "name": "Enigma sin resolver",
        "guidance": (
            "Presenta el enigma, las explicaciones candidatas y la evidencia a "
            "favor y en contra de cada una. Termina reconociendo lo que aún no "
            "se sabe, sin fabricar una conclusión falsa."
        ),
    },
    {
        "key": "evidencia_documental",
        "name": "Evidencia documental",
        "guidance": (
            "Construye el relato alrededor de documentos, registros y fuentes "
            "citables (fechas, cifras, testimonios publicados). Cada afirmación "
            "se apoya en una fuente concreta."
        ),
    },
    {
        "key": "escalada_cientifica",
        "name": "Escalada científica",
        "guidance": (
            "Parte del fenómeno observable, escala a la explicación científica "
            "o histórica, y cierra con sus implicaciones. Prohibido el consejo "
            "o el diagnóstico en primera persona."
        ),
    },
]


@dataclass(frozen=True)
class Archetype:
    key: str
    name: str
    guidance: str


def _coerce(raw: list) -> list[Archetype]:
    out: list[Archetype] = []
    for item in raw or []:
        if isinstance(item, dict) and item.get("key"):
            out.append(Archetype(
                key=str(item.get("key")),
                name=str(item.get("name") or item.get("key")),
                guidance=str(item.get("guidance") or ""),
            ))
    return out


def get_archetypes(config=None) -> list[Archetype]:
    """Arquetipos del canal (``NARRATIVE_ARCHETYPES``) o los por defecto."""
    raw = getattr(config, "NARRATIVE_ARCHETYPES", None) if config else None
    return _coerce(raw) or _coerce(DEFAULT_ARCHETYPES)


def pick_archetype(config=None, seed: int | str | None = None) -> Archetype:
    """Elige un arquetipo. Con ``seed`` es determinista (tests/reproducibilidad)."""
    archetypes = get_archetypes(config)
    rng = random.Random(seed)
    return rng.choice(archetypes)


def archetype_prompt_block(archetype: Archetype) -> str:
    """Bloque de prompt para inyectar en el outline."""
    if archetype is None:
        return ""
    return (
        "ARQUETIPO NARRATIVO OBLIGATORIO PARA ESTE VIDEO:\n"
        f"- {archetype.name} ({archetype.key})\n"
        f"- {archetype.guidance}\n"
        "La estructura DEBE seguir este arquetipo y DIFERIR de la de otros "
        "videos del canal. No repitas el mismo esquema capítulo a capítulo."
    )
