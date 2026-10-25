"""Lenient JSON extraction for LLM responses (no ``eval``, no silent guesses).

LLMs occasionally wrap JSON in markdown fences, append prose, leave a trailing
comma or get cut off by the output-token limit.  These are recoverable payload
defects.  This module extracts the first JSON document and performs bounded,
deterministic repairs.  If the payload is not a JSON object/array it returns
``None`` so callers can keep their existing failure path.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*")
_END_FENCE_RE = re.compile(r"\s*```\s*$")


def _strip_fences(text: str) -> str:
    text = _FENCE_RE.sub("", text or "", count=1)
    text = _END_FENCE_RE.sub("", text, count=1)
    return text.strip()


def _find_json_start(text: str) -> int:
    positions = [pos for pos in (text.find("{"), text.find("[")) if pos >= 0]
    return min(positions) if positions else -1


def _strip_trailing_commas(text: str) -> str:
    """Remove commas immediately before a closing bracket, outside strings."""
    out: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            out.append(char)
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                continue  # drop trailing comma
        out.append(char)
    return "".join(out)


def _close_truncated(text: str) -> str | None:
    """Close open brackets for a truncated object/array, dropping a partial tail.

    Only used after strict parsing failed.  It never invents keys or values:
    if the payload ends inside a string, the partial element is removed at the
    last complete element boundary before closing the containers.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    last_safe_cut = 0
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack:
                return None
            opener = stack.pop()
            if (opener, char) not in (("{", "}"), ("[", "]")):
                return None
            last_safe_cut = index + 1
            # A complete element/value boundary is a good cut candidate.
            if char in "}" and "[" in stack:
                last_safe_cut = index + 1
            elif char == "}" and "[" not in stack:
                last_safe_cut = index + 1
        elif char == "," and not in_string:
            # Cutting here drops the current (incomplete) element.
            last_safe_cut = index
    if not stack:
        return text
    stripped = text.rstrip()
    if in_string or (stripped and stripped[-1] in ":,"):
        # Remove the incomplete trailing element and close the containers.
        cut = stripped[:last_safe_cut].rstrip()
        while cut.endswith(","):
            cut = cut[:-1].rstrip()
        if not cut:
            return None
        return _close_truncated(cut)
    closers = "".join("}" if opener == "{" else "]" for opener in reversed(stack))
    return stripped.rstrip(",") + closers


def parse_json_lenient(content: str | bytes | None) -> Any:
    """Return the parsed JSON value, or ``None`` if it is not recoverable."""
    if content is None:
        return None
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8", errors="replace")
        except Exception:
            return None
    text = _strip_fences(str(content))
    if not text:
        return None
    start = _find_json_start(text)
    if start < 0:
        return None
    candidate = text[start:]

    decoder = json.JSONDecoder()
    for attempt in (candidate, _strip_trailing_commas(candidate)):
        try:
            value, _ = decoder.raw_decode(attempt.strip())
            if isinstance(value, (dict, list)):
                return value
        except json.JSONDecodeError:
            pass
        try:
            value = json.loads(attempt)
            if isinstance(value, (dict, list)):
                return value
        except json.JSONDecodeError:
            pass

    repaired = _close_truncated(candidate)
    if repaired:
        for attempt in (repaired, _strip_trailing_commas(repaired)):
            try:
                value, _ = decoder.raw_decode(attempt.strip())
                if isinstance(value, (dict, list)):
                    return value
            except json.JSONDecodeError:
                continue
    return None
