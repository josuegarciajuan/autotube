"""Recuperación best-effort de JSON truncado por límite de tokens.

Los LLM a veces devuelven un JSON válido pero cortado a mitad (p. ej.
`Unterminated string`), típicamente cuando la respuesta excede `max_tokens`.
En vez de tirar toda la respuesta, intentamos cerrar el último elemento
completo y los contenedores abiertos.

Es una utilidad pura (sin dependencias del proyecto) para poder reutilizarla
en cualquier worker distribuido.
"""
from __future__ import annotations

import json
from typing import Optional


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _scan(text: str) -> tuple[list[str], bool]:
    """Devuelve (pila de contenedores abiertos, dentro_de_string)."""
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    return stack, in_str


def _closers(stack: list[str]) -> str:
    return "".join("]" if c == "[" else "}" for c in reversed(stack))


def repair_truncated_json(text: str) -> Optional[dict]:
    """Intenta parsear JSON truncado. Devuelve dict o None si es irrecuperable."""
    if not text:
        return None
    s = _strip_fences(text)
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    # 1) Recortar por el último separador y cerrar contenedores: conserva los
    #    elementos completos anteriores al corte.
    commas = [i for i, ch in enumerate(s) if ch == ","]
    for pos in reversed(commas[-16:]):
        prefix = s[:pos]
        stack, in_str = _scan(prefix)
        if in_str:
            continue
        candidate = prefix + _closers(stack)
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue

    # 2) Último recurso: cerrar el string abierto y los contenedores.
    stack, in_str = _scan(s)
    candidate = s + ('"' if in_str else "") + _closers(stack)
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None
