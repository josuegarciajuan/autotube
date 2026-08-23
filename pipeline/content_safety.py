"""Filtro duro de seguridad de contenido (anti-strike de YouTube).

Contexto (ago 2026): YouTube eliminó 4 videos (3 strikes + 1 retroactivo) en 4
días. Dos de los eliminados eran casos médicos de MENORES ("La niña que no
siente dolor", "Nació con 2 caras") — la categoría más agresivamente moderada
por YouTube (child-safety + desinformación médica). Este módulo rechaza topics
sensibles ANTES de guionar/renderizar/subir, en shorts Y long-forms.

Diseño en dos capas:
  1. Pre-filtro determinista (barato, sin LLM): lista de patrones en español.
  2. Clasificación LLM (solo si el pre-filtro no decide): devuelve JSON
     {"safe": bool, "reason": "...", "categories": [...]}.

Las categorías bloqueadas son:
  - menores en contexto médico/criminal/sexual (child-safety)
  - autolesión / suicidio / métodos
  - claims médicos de cura/tratamiento ("cura milagrosa", "tratamiento garantizado")
  - detalle gráfico de muertes / violencia extrema / gore
  - conspiranoia sanitaria / desinformación médica

Uso:
    from pipeline.content_safety import classify_topic_safety
    verdict = classify_topic_safety(topic, title, script_texts, config)
    if not verdict.safe:
        # rechazar topic → regenerar otro
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("autotube.content_safety")


@dataclass
class SafetyVerdict:
    safe: bool
    reason: str = ""
    categories: list = field(default_factory=list)
    source: str = "deterministic"  # "deterministic" | "llm" | "default"


# ── Pre-filtro determinista (español) ──────────────────────────────
# Bloques por categoría; cada patrón se busca sin distinción de acentos.
_BLOCK_PATTERNS: dict[str, list[str]] = {
    "menores": [
        "niño", "niña", "niños", "niñas", "bebé", "bebe", "bebés", "menor de",
        "adolescente", "recién nacido", "recien nacido", "de 8 años", "de 9 años",
        "de 10 años", "de 11 años", "de 12 años", "de 13 años", "de 14 años",
        "de 15 años", "de 16 años", "de 17 años", "de 5 años", "de 6 años",
        "de 7 años", "menores de edad", "infante", "pequeño de", "pequeña de",
        "niño que", "niña que", "niño de", "niña de",
        # Nacimiento / condiciones congénitas (suelen implicar a un bebé/menor)
        "nació con", "nacio con", "al nacer", "de nacimiento", "congénito",
        "congenito", "recién nacid", "recien nacid", "dos cabezas", "dos caras",
        "siames", "gemelos unidos", "bebé nació", "bebe nacio", "recién parido",
    ],
    "autolesion": [
        "autolesión", "autolesion", "suicid", "cortarse las venas", "ahorcó",
        "ahorco", "se quitó la vida", "se quito la vida", "quitarse la vida",
        "forma de morir", "método para", "metodo para", "sobredosis letal",
        "cómo suicidarse", "como suicidarse",
    ],
    "claims_medicos": [
        "cura milagrosa", "cura definitiva", "cura el cáncer", "cura el cancer",
        "tratamiento garantizado", "remedio infalible", "cura casera para",
        "revertir el cáncer", "revertir el cancer", "sana el", "cura cualquier",
        "sin quimioterapia", "médicos lo ocultan", "medicos lo ocultan",
        "la industria farmacéutica lo esconde", "cura probada",
    ],
    "violencia_grafica": [
        "desmembr", "decapita", "vísceras", "visceras", "sangre a borbotones",
        "cuerpo mutilado", "cadáver descompuesto", "cadaver descompuesto",
        "tortura detallada", "detalle gráfico", "detalle grafico",
    ],
    "desinformacion_sanitaria": [
        "vacunas causan", "vacuna causa", "el autismo lo causan las vacunas",
        "5g enferma", "curar el autismo", "la tierra es plana y",
        "virus fue creado en un laboratorio para", "engaño global de salud",
    ],
}


def _normalize(text: str) -> str:
    """Strip accents + lowercase so 'niño' matches 'nino'."""
    if not text:
        return ""
    text = text.lower()
    replacements = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
        "ü": "u", "ñ": "n",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


def _deterministic_check(texts: list[str]) -> SafetyVerdict | None:
    """Return a FLAGGED verdict on first deterministic match, else None."""
    joined = _normalize(" ".join(t for t in texts if t))
    if not joined:
        return None
    for category, patterns in _BLOCK_PATTERNS.items():
        for pat in patterns:
            if _normalize(pat) in joined:
                return SafetyVerdict(
                    safe=False,
                    reason=f"tema bloqueado por categoría '{category}' (patrón: '{pat}')",
                    categories=[category],
                    source="deterministic",
                )
    return None


def _llm_check(topic: str, title: str, script_texts: list[str]) -> SafetyVerdict:
    """Clasifica con LLM si el tema es seguro para YouTube (política de spam)."""
    import json as _json
    from config.settings import LLM_MODEL

    sample = " | ".join(t for t in ([topic, title] + list(script_texts)) if t)[:3000]
    try:
        from config.llm_client import create_llm_client
        from config.llm_helpers import llm_json_call
        client = create_llm_client(enable_thinking=False)
        result = llm_json_call(
            client,
            max_retries=2,
            retry_delay=1.0,
            model=LLM_MODEL,
            messages=[{
                "role": "user",
                "content": (
                    "Evalúa si este contenido es SEGURO para YouTube según las "
                    "políticas de spam/contenido. Devuelve SOLO JSON "
                    '{"safe": bool, "reason": "motivo corto en español", '
                    '"categories": ["menores","autolesion","claims_medicos",'
                    '"violencia_grafica","desinformacion_sanitaria"]}.\n'
                    "Reglas: NO es seguro (safe=false) si trata de menores en "
                    "contexto médico/criminal, autolesión/suicidio/métodos, "
                    "claims médicos de cura/tratamiento, detalle gráfico de "
                    "muertes o desinformación sanitaria. Es seguro (safe=true) "
                    "para historia/documental general.\n"
                    f"Contenido: {sample}"
                ),
            }],
            temperature=0.0,
            max_tokens=200,
        )
        if isinstance(result, dict):
            safe = bool(result.get("safe", True))
            return SafetyVerdict(
                safe=safe,
                reason=result.get("reason", "")[:300],
                categories=list(result.get("categories", [])) if not safe else [],
                source="llm",
            )
    except Exception as exc:  # noqa: BLE001 — fail-open con aviso
        logger.warning("Content-safety LLM check failed (fail-open): %s", exc)
    return SafetyVerdict(safe=True, reason="", categories=[], source="default")


def classify_topic_safety(
    topic: str = "",
    title: str = "",
    script_texts: list[str] | None = None,
    config=None,
    use_llm: bool = True,
) -> SafetyVerdict:
    """Devuelve SafetyVerdict.safe=False si el tema debe rechazarse.

    ``script_texts``: lista de fragmentos (bloques, guion, etc.) a revisar.
    ``use_llm``: si False, solo aplica el pre-filtro determinista (barato, sin
    llamada LLM) — útil para filtrar varios candidatos sin coste.
    """
    texts = [t for t in ([topic, title] + (script_texts or [])) if t]

    # 1. Kill-switch global: system_state["content_safety_disabled"]=true salta
    #    el filtro por completo (solo para casos extremos de operación).
    try:
        from database.db_extended import ExtendedDatabase
        from config.settings import DATABASE_PATH
        db = ExtendedDatabase(str(DATABASE_PATH))
        if db.get_system_state("content_safety_disabled") == "true":
            logger.info("Content-safety filter DISABLED by system_state kill-switch")
            return SafetyVerdict(safe=True, reason="kill-switch", source="default")
    except Exception:  # noqa: BLE001
        pass

    # 2. Determinista
    det = _deterministic_check(texts)
    if det is not None:
        return det

    # 3. LLM (opcional)
    if not use_llm:
        return SafetyVerdict(safe=True, reason="", categories=[], source="default")
    return _llm_check(topic, title, script_texts or [])
