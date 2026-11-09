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
import re
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


_SMART_QUOTES = str.maketrans({
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u00ab": '"', "\u00bb": '"',
})


def _insert_missing_colons(text: str) -> str:
    """Insert ``:`` after object keys emitted without a colon.

    Only acts when the just-closed string is a key inside an object (the
    previous non-space char is ``{`` or ``,`` and the container stack top is
    ``{``). Array elements and string values are untouched.
    """
    out: list[str] = []
    stack: list[str] = []
    in_str = False
    esc = False
    string_is_key = False
    string_is_value = False
    i = 0
    n = len(text)

    def _prev_nonspace(idx: int) -> str:
        k = idx
        while k >= 0 and out[k] in " \t\r\n":
            k -= 1
        return out[k] if k >= 0 else ""

    while i < n:
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                if string_is_key:
                    j = i + 1
                    while j < n and text[j] in " \t\r\n":
                        j += 1
                    if j < n and text[j] != ":":
                        out.append('"')
                        out.append(":")
                        string_is_key = False
                        i += 1
                        continue
                elif string_is_value:
                    # Missing comma between a string value and the next key:
                    # {"a": "x" "b": "y"} → {"a": "x", "b": "y"}
                    j = i + 1
                    while j < n and text[j] in " \t\r\n":
                        j += 1
                    if j < n and text[j] == '"':
                        out.append('"')
                        out.append(",")
                        string_is_value = False
                        i += 1
                        continue
                string_is_key = False
                string_is_value = False
        else:
            if ch == '"':
                in_str = True
                esc = False
                prev = _prev_nonspace(len(out) - 1) if out else ""
                string_is_key = bool(stack and stack[-1] == "{" and prev in "{,")
                string_is_value = prev == ":"
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
        out.append(ch)
        i += 1
    return "".join(out)


def repair_json_text(text: str) -> Optional[dict]:
    """Repair common LLM JSON malformations.

    Covers smart quotes, trailing commas and missing ``:`` after object keys
    (the ``Expecting ':' delimiter`` failure seen in channel_analyzer).
    Returns a dict or ``None`` when the text is still unparseable.
    """
    if not text:
        return None
    s = _strip_fences(text)
    if not s:
        return None
    s = s.translate(_SMART_QUOTES)
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    s = _insert_missing_colons(s)
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None
