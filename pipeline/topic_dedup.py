"""Temáticas consumidas — normalización y similitud por solapamiento de palabras.

Registro anti-repetición **compartido** entre long-forms y shorts (por canal).
Este módulo es puro (sin acceso a DB): la persistencia vive en la tabla
``consumed_topics`` (``database/db_extended.py``).

Métrica
-------
Contención de tokens significativos ``|A ∩ B| / max(|A|, |B|)`` — la misma que
usa el guard de títulos de shorts (``shorts_scheduler._titles_too_similar``) —
más un mínimo de tokens compartidos (``min_shared``) para no bloquear temas por
compartir una única palabra genérica del nicho.

Ver ``specs/`` para el contrato funcional.
"""

from __future__ import annotations

import json
import re
import unicodedata

# ── Stopwords ES + EN ──────────────────────────────────────────────
# Ampliación del set de shorts_scheduler.TITLE_SIMILARITY_STOPWORDS. Las
# palabras función no aportan evidencia temática y producen falsos positivos
# ("el misterio de la X" vs "el misterio de la Y").
STOPWORDS = frozenset({
    # — ES —
    "el", "la", "los", "las", "lo", "un", "una", "unos", "unas",
    "y", "o", "u", "e", "de", "del", "al", "a", "ante", "bajo", "con",
    "contra", "desde", "en", "entre", "hacia", "hasta", "para", "por",
    "según", "sin", "sobre", "tras", "durante", "mediante",
    "que", "quien", "quienes", "cual", "cuales", "cuyo", "cuya",
    "se", "su", "sus", "mi", "mis", "tu", "tus", "nos", "os",
    "es", "son", "era", "eran", "fue", "fueron", "ser", "esta", "este",
    "esto", "estos", "estas", "ese", "esa", "esos", "esas",
    "no", "si", "sí", "ya", "muy", "más", "menos", "pero", "porque",
    "también", "tampoco", "como", "cuando", "donde", "hay", "había",
    "fue", "han", "ha", "he", "tiene", "tienen", "todo", "toda", "todos",
    "todas", "otro", "otra", "otros", "otras", "mismo", "misma", "tan",
    "así", "solo", "sólo", "aún", "aun", "cada", "puede", "pueden",
    # — EN —
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "by",
    "for", "with", "from", "as", "is", "are", "was", "were", "be", "been",
    "this", "that", "these", "those", "it", "its", "his", "her", "their",
    "he", "she", "they", "we", "you", "i", "not", "no", "but", "if",
    "when", "where", "how", "why", "which", "who", "what", "than", "then",
    "into", "over", "under", "about", "after", "before", "has", "have",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIN_TOKEN_LEN = 3

# Umbrales por defecto (espejo de config/defaults.py)
DEFAULT_THRESHOLD = 0.5
DEFAULT_MIN_TOKENS = 2


def strip_accents(text: str) -> str:
    """Quita acentos/diacríticos preservando el resto de caracteres."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_topic(text: str) -> str:
    """Normaliza una etiqueta de tema para comparación/almacenamiento.

    Minúsculas, sin acentos, sin puntuación, espacios colapsados. Es la clave
    de unicidad exacta (``topic_norm``).
    """
    if not text:
        return ""
    text = strip_accents(str(text)).lower()
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def topic_tokens(text: str) -> set[str]:
    """Tokens significativos de un tema (sin stopwords, longitud >= 3)."""
    norm = normalize_topic(text)
    if not norm:
        return set()
    return {
        tok for tok in _TOKEN_RE.findall(norm)
        if len(tok) >= _MIN_TOKEN_LEN and tok not in STOPWORDS
    }


def tokens_to_json(tokens: set[str]) -> str:
    """Serializa un set de tokens para la columna ``tokens_json``."""
    return json.dumps(sorted(tokens), ensure_ascii=False)


def tokens_from_json(raw) -> set[str]:
    """Deserializa ``tokens_json`` de forma tolerante (lista JSON o string)."""
    if not raw:
        return set()
    if isinstance(raw, (set, frozenset, list, tuple)):
        return {str(t) for t in raw}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(t) for t in data}


def shared_token_count(a: set[str], b: set[str]) -> int:
    """Nº de tokens significativos compartidos."""
    return len(a & b)


def topic_similarity(a: set[str], b: set[str]) -> float:
    """Contención de tokens ``|A ∩ B| / max(|A|, |B|)`` en [0, 1]."""
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


def is_topic_duplicate(
    candidate_tokens: set[str],
    consumed: list[tuple[str, set[str]]] | list[dict],
    threshold: float = DEFAULT_THRESHOLD,
    min_shared: int = DEFAULT_MIN_TOKENS,
) -> tuple[bool, str | None]:
    """¿La temática candidata ya está consumida?

    Args:
        candidate_tokens: tokens significativos del tema candidato.
        consumed: iterable de ``(label, tokens)`` **o** dicts con las claves
            ``topic_label`` y ``tokens_json``/``tokens``.
        threshold: solapamiento mínimo para considerarlo duplicado.
        min_shared: tokens significativos compartidos mínimos.

    Returns:
        ``(True, label_coincidente)`` si duplica; ``(False, None)`` si no.
    """
    if not candidate_tokens or not consumed:
        return False, None
    best_label: str | None = None
    best_sim = 0.0
    for entry in consumed:
        if isinstance(entry, dict):
            label = entry.get("topic_label") or entry.get("label") or ""
            toks = entry.get("tokens")
            if toks is None:
                toks = tokens_from_json(entry.get("tokens_json"))
        else:
            label, toks = entry[0], entry[1]
        if not toks:
            continue
        shared = shared_token_count(candidate_tokens, toks)
        if shared < min_shared:
            continue
        sim = topic_similarity(candidate_tokens, toks)
        if sim >= threshold and sim > best_sim:
            best_sim = sim
            best_label = label
    return (best_label is not None), best_label


def find_best_duplicate(
    candidate_tokens: set[str],
    consumed: list[tuple[str, set[str]]] | list[dict],
    threshold: float = DEFAULT_THRESHOLD,
    min_shared: int = DEFAULT_MIN_TOKENS,
) -> tuple[str | None, float]:
    """Devuelve ``(mejor_label_coincidente, similitud)`` o ``(None, 0.0)``."""
    if not candidate_tokens or not consumed:
        return None, 0.0
    best_label: str | None = None
    best_sim = 0.0
    for entry in consumed:
        if isinstance(entry, dict):
            label = entry.get("topic_label") or entry.get("label") or ""
            toks = entry.get("tokens")
            if toks is None:
                toks = tokens_from_json(entry.get("tokens_json"))
        else:
            label, toks = entry[0], entry[1]
        if not toks:
            continue
        shared = shared_token_count(candidate_tokens, toks)
        if shared < min_shared:
            continue
        sim = topic_similarity(candidate_tokens, toks)
        if sim >= threshold and sim > best_sim:
            best_sim = sim
            best_label = label
    return best_label, best_sim


def get_dedup_settings(config=None) -> tuple[bool, float, int]:
    """Resuelve ``(enabled, threshold, min_shared)`` desde la config del canal.

    Fail-open por defecto (enabled=True) para no desactivar el dedup si la
    config no trae los flags.
    """
    if config is None:
        return True, DEFAULT_THRESHOLD, DEFAULT_MIN_TOKENS
    enabled = bool(getattr(config, "TOPIC_DEDUP_ENABLED", True))
    try:
        threshold = float(getattr(config, "TOPIC_DEDUP_THRESHOLD", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        threshold = DEFAULT_THRESHOLD
    try:
        min_shared = int(getattr(config, "TOPIC_DEDUP_MIN_TOKENS", DEFAULT_MIN_TOKENS))
    except (TypeError, ValueError):
        min_shared = DEFAULT_MIN_TOKENS
    return enabled, max(0.0, min(1.0, threshold)), max(1, min_shared)
