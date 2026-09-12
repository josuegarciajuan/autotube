"""Title engine for long-form videos.

Replaces the old "generate one title then force a power word into it" flow
with a scored, multi-candidate pipeline:

1. Build N candidates (LLM, parametrised per channel) with a deterministic
   fallback from ``script.titulo_options`` + a complete natural template.
2. Score every candidate with a deterministic 7-criterion rubric (0-14) plus
   explicit penalties (banned tokens, dangling tails, injected ``|``/``[]``,
   excessive caps, clickbait suffix, length out of band).
3. Ask an LLM judge to pick the best candidate, falling back to the highest
   rubric score (tie-break: closest to the centre of the target length band).
4. Format the winner deterministically via :func:`apply_caps_policy`, always
   truncating on a word boundary — never mid-word.

The engine never mutates or degrades a title. If the LLM fails, the
deterministic path still returns a complete, grammatical title.
"""

from __future__ import annotations

import json
import logging
import re

from pipeline.title_tokens import (
    CONNECTOR_TAIL_WORDS,
    KNOWN_ACRONYMS,
    contains_banned_token,
    has_dangling_tail,
    is_all_caps,
    normalize,
    uppercase_words,
)

logger = logging.getLogger(__name__)

# ── Clickbait suffix detection (mirrors metadata_generator._SPAM_TITLE_SUFFIXES) ──
_CLICKBAIT_SUFFIX_RE = re.compile(
    r"\(\s*(?:real|caso real|revelaci[oó]n|impactante|archivos cia|expediente)\s*\)",
    re.IGNORECASE,
)

_EMPTY_PRONOUNS = frozenset({
    "esto", "esta", "estos", "estas", "eso", "esa", "esos", "esas",
    "aquello", "aquella", "aquellos", "aquellas",
})

_LETTERS_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

_SENTENCE_STOPWORDS = frozenset(normalize(w) for w in (
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "que", "y", "o", "a", "en", "con", "por", "para", "sin", "sobre",
    "entre", "desde", "hacia", "su", "sus", "al", "se", "lo", "ni",
    "pero", "porque", "cuando", "donde", "si", "no", "mas", "muy", "tan",
    "como", "the", "of", "to", "in", "and", "or", "is", "this", "that",
))

# AP-style minor words kept lowercase in title case (unless first/last).
_TITLE_CASE_MINOR = frozenset(normalize(w) for w in (
    "a", "al", "and", "as", "at", "but", "by", "con", "de", "del", "el",
    "en", "for", "from", "in", "la", "las", "los", "ni", "o", "of", "on",
    "or", "para", "por", "sin", "sobre", "the", "to", "un", "una", "unos",
    "unas", "y",
))

_CURIOSITY_MARKERS = (
    "nunca", "jamas", "sin explicacion", "inexplicable", "secreto",
    "oculto", "oculto", "descubrio", "descubrieron", "revelo", "cambio",
    "misterio", "paradoja", "contradice", "contradiccion", "no podia",
    "nadie", "enigma", "desconocido", "extrano", "anomalia", "aun",
)

_STAKES_STRONG = (
    "muerte", "murio", "muert", "peligro", "desapareci", "nunca regres",
    "sobrevivio", "salvo", "condeno", "perdio", "arruino", "colapso",
    "catastrofe", "tragedia", "riesgo", "amenaza", "ultima", "ultimo",
    "unico", "sin retorno", "trampa", "error fatal", "devasto",
)

_STAKES_MILD = ("cambio", "impacto", "sorprendio", "transformo", "marco", "desafio")


# ═══════════════════════════════════════════════════════════════════
# Deterministic formatting helpers
# ═══════════════════════════════════════════════════════════════════

def truncate_title_at_word(title: str, max_chars: int) -> str:
    """Truncate *title* to <= *max_chars* without splitting a word.

    If the text is a single token longer than the budget, it is returned
    intact (never cut mid-word).
    """
    text = " ".join(str(title or "").split())
    if not text:
        return ""
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        return text
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        return cut[: cut.rfind(" ")].rstrip(" .,;:·•–—|-/")
    return text


def _remove_injected_markers(text: str) -> str:
    """Drop ``|`` and square brackets (never valid in a long-form title)."""
    cleaned = str(text or "").replace("|", " ").replace("[", " ").replace("]", " ")
    return " ".join(cleaned.split())


def _strip_clickbait_suffix_safe(title: str) -> str:
    """Lazily reuse metadata_generator's clickbait-suffix stripper."""
    try:
        from pipeline.metadata_generator import _strip_clickbait_suffix
        return _strip_clickbait_suffix(title)
    except Exception:  # pragma: no cover - defensive (circular-import safety)
        return title


def _map_letters(text: str, fn) -> str:
    tokens = _LETTERS_RE.findall(text)
    total = len(tokens)
    state = [0]

    def repl(match: re.Match) -> str:
        index = state[0]
        state[0] += 1
        return fn(match.group(0), index, total)

    return _LETTERS_RE.sub(repl, text)


def _sentence_word(word: str, index: int, total: int) -> str:
    if word.upper() in KNOWN_ACRONYMS:
        return word.upper()
    if word.isupper():
        lower = word.lower()
        return lower[0].upper() + lower[1:] if index == 0 else lower
    # Internal capital → brand/camelCase (YouTube, iPhone) — preserve.
    if any(c.isupper() for c in word[1:]):
        return word
    lower = word.lower()
    if index == 0:
        return lower[0].upper() + lower[1:]
    if word[0].isupper() and lower not in _SENTENCE_STOPWORDS:
        return word[0].upper() + lower[1:]
    return lower


def _to_sentence_case(text: str) -> str:
    """Spanish sentence case: first letter + proper nouns + acronyms."""
    return _map_letters(text, _sentence_word)


def _title_word(word: str, index: int, total: int) -> str:
    if word.upper() in KNOWN_ACRONYMS:
        return word.upper()
    if any(c.isupper() for c in word[1:]) and not word.isupper():
        return word  # camelCase brand
    lower = word.lower()
    if index == 0 or index == total - 1:
        return lower[0].upper() + lower[1:]
    if lower in _TITLE_CASE_MINOR or len(lower) < 4:
        return lower
    return lower[0].upper() + lower[1:]


def _to_title_case(text: str) -> str:
    """AP-ish Title Case (principal words capitalised, short words lower)."""
    return _map_letters(text, _title_word)


def _to_one_word_caps(text: str) -> str:
    """Sentence case leaving ONE already-emphasised word uppercase."""
    emphasized = None
    for word in uppercase_words(text):
        if word.upper() not in KNOWN_ACRONYMS:
            emphasized = word
            break
    base = _to_sentence_case(text)
    if emphasized:
        pattern = re.compile(
            r"(?<![^\W\d_])" + re.escape(emphasized) + r"(?![^\W\d_])",
            re.IGNORECASE,
        )
        base = pattern.sub(emphasized.upper(), base, count=1)
    return base


def apply_caps_policy(title: str, config) -> str:
    """Apply ``TITLE_CAPS_POLICY`` deterministically and word-truncate.

    Policies: ``sentence`` (default), ``title_case``, ``one_word_caps``.
    Always removes ``|``/``[``/``]`` and truncates at a word boundary.
    """
    policy = str(getattr(config, "TITLE_CAPS_POLICY", "sentence") or "sentence").lower()
    text = _remove_injected_markers(title)
    text = _strip_clickbait_suffix_safe(text)
    text = " ".join(text.split())
    if not text:
        return ""
    if policy == "title_case":
        text = _to_title_case(text)
    elif policy == "one_word_caps":
        text = _to_one_word_caps(text)
    else:
        text = _to_sentence_case(text)
    hard_max = int(getattr(config, "TITLE_MAX_CHARS", 100) or 100)
    return truncate_title_at_word(text, hard_max)


# ═══════════════════════════════════════════════════════════════════
# Deterministic fallback title
# ═══════════════════════════════════════════════════════════════════

def _parse_json_list(raw) -> list[str]:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    else:
        parsed = raw or []
    if not isinstance(parsed, list):
        return []
    return [str(x).strip() for x in parsed if isinstance(x, (str, int, float)) and str(x).strip()]


def deterministic_fallback_title(script: dict, config) -> str:
    """Return a complete, grammatical fallback title (never a broken suffix)."""
    script = script or {}
    keywords = _parse_json_list(script.get("keywords") or script.get("keywords_json"))
    keyword = keywords[0] if keywords else str(
        getattr(config, "SEO_PRIMARY_KEYWORD", "") or ""
    ).strip()
    if keyword:
        base = f"{keyword[:1].upper() + keyword[1:]}: la historia que cambió el caso"
    else:
        base = "La historia que cambió todo: un caso sin resolver"
    if contains_banned_token(base) or has_dangling_tail(base):
        return "La historia que cambió todo: un caso sin resolver"
    return base


# ═══════════════════════════════════════════════════════════════════
# Rubric
# ═══════════════════════════════════════════════════════════════════

def _has_number_or_year(text: str) -> bool:
    return bool(re.search(r"\b\d+\b", text)) or bool(
        re.search(r"\b(?:1[0-9]{3}|20[0-9]{2})\b", text)
    )


def _proper_nouns(text: str) -> list[str]:
    out: list[str] = []
    for i, word in enumerate(text.split()):
        core = word.strip("¿?¡!.,;:()«»\"'")
        if not core or core.isupper():
            continue
        if i == 0 or not core[0].isupper():
            continue
        if normalize(core) in CONNECTOR_TAIL_WORDS:
            continue
        if any(c.islower() for c in core[1:]):
            out.append(core)
    return out


def _primary_keyword(script: dict, config) -> str:
    script = script or {}
    keywords = _parse_json_list(script.get("keywords") or script.get("keywords_json"))
    if keywords:
        return keywords[0]
    return str(getattr(config, "SEO_PRIMARY_KEYWORD", "") or "").strip()


def score_title(title: str, script: dict, config) -> tuple[int, dict]:
    """Deterministic 7-criterion rubric (0-14) plus penalties.

    Returns ``(final_score, breakdown)`` where ``breakdown`` contains each
    criterion, the penalty map, and the totals.
    """
    text = " ".join(str(title or "").split())
    words = text.split()
    first_norm = normalize(words[0]).strip("¿?¡!.,;:()«»\"'") if words else ""
    norm = normalize(text)

    # 1. Subject clarity — reject empty demonstratives ("Esto/Esta/Estos").
    if not text:
        subject = 0
    elif first_norm in _EMPTY_PRONOUNS:
        subject = 0
    elif len(words) >= 3:
        subject = 2
    else:
        subject = 1

    # 2. Primary keyword in the first three words.
    keyword = _primary_keyword(script, config)
    if not keyword:
        keyword_front = 1
    else:
        norm_kw = normalize(keyword)
        first3 = " ".join(norm.split()[:3])
        kw_tokens = [t for t in norm_kw.split() if len(t) >= 4]
        if norm_kw in first3 or any(t in first3.split() for t in kw_tokens):
            keyword_front = 2
        elif norm_kw in norm:
            keyword_front = 1
        else:
            keyword_front = 0

    # 3. Curiosity — real unknown / revelation / contradiction.
    if any(p in norm.split() for p in _EMPTY_PRONOUNS):
        curiosity = 0
    elif any(m in norm for m in _CURIOSITY_MARKERS):
        curiosity = 2
    elif text.rstrip().endswith("?"):
        curiosity = 1 if len(words) >= 4 else 0
    else:
        curiosity = 1

    # 4. Stakes — tension / consequence.
    if any(m in norm for m in _STAKES_STRONG):
        stakes = 2
    elif any(m in norm for m in _STAKES_MILD):
        stakes = 1
    else:
        stakes = 0

    # 5. Specificity — number / year / proper noun / place.
    proper = _proper_nouns(text)
    has_fact = _has_number_or_year(text)
    if has_fact or proper:
        specificity = 2
    elif len(words) >= 5:
        specificity = 1
    else:
        specificity = 0

    # 6. Evidence — verifiable detail, not an empty adjective.
    if has_fact and proper:
        evidence = 2
    elif has_fact or proper:
        evidence = 1
    else:
        evidence = 0

    # 7. Thumbnail synergy — offers a concrete, non-redundant angle.
    if not text:
        thumbnail = 0
    elif first_norm in _EMPTY_PRONOUNS:
        thumbnail = 1
    elif has_fact or proper:
        thumbnail = 2
    else:
        thumbnail = 1

    criteria = {
        "subject_clarity": subject,
        "keyword_front": keyword_front,
        "curiosity": curiosity,
        "stakes": stakes,
        "specificity": specificity,
        "evidence": evidence,
        "thumbnail_synergy": thumbnail,
    }
    criteria_total = sum(criteria.values())

    # ── Deterministic penalties ──
    t_min = int(getattr(config, "TITLE_TARGET_MIN_CHARS", 45) or 45)
    t_max = int(getattr(config, "TITLE_TARGET_MAX_CHARS", 70) or 70)
    hard_max = int(getattr(config, "TITLE_MAX_CHARS", 100) or 100)
    band_max = min(t_max, hard_max)
    penalties: dict[str, int] = {}
    if len(text) < t_min or len(text) > band_max:
        penalties["length"] = -3
    if contains_banned_token(text):
        penalties["banned_token"] = -6
    if has_dangling_tail(text):
        penalties["dangling_tail"] = -4
    if "|" in text or "[" in text or "]" in text:
        penalties["injected_suffix"] = -4
    if len(uppercase_words(text)) > 1:
        penalties["excessive_caps"] = -2
    if is_all_caps(text):
        penalties["all_caps"] = -5
    if _CLICKBAIT_SUFFIX_RE.search(text):
        penalties["clickbait_suffix"] = -3

    final = max(0, criteria_total + sum(penalties.values()))
    breakdown = dict(criteria)
    breakdown["criteria_total"] = criteria_total
    breakdown["penalties"] = penalties
    breakdown["total"] = final
    return final, breakdown


# ═══════════════════════════════════════════════════════════════════
# Prompt builders
# ═══════════════════════════════════════════════════════════════════

_CANDIDATE_SYSTEM = (
    "Eres un editor de títulos de documentales para YouTube en español. "
    "Escribes titulares concretos, creíbles y gramaticalmente perfectos. "
    "Nunca usas promesas vacías, ni frases cortadas, ni sufijos de credibilidad."
)

_CAPS_INSTRUCTIONS = {
    "sentence": "Frase normal en español: solo la primera letra y los nombres propios en mayúscula.",
    "title_case": "Title Case (cada palabra principal en mayúscula), como pide este canal.",
    "one_word_caps": "Frase normal con UNA sola palabra clave en MAYÚSCULAS para enfatizar.",
}


def _build_candidate_prompt(config, script: dict, source_content: dict, count: int) -> tuple[str, str]:
    name = getattr(config, "CANAL_DISPLAY_NAME", getattr(config, "CANAL_NAME", "canal"))
    style = getattr(config, "CANAL_NARRATIVE_STYLE", "documental")
    tone = str(getattr(config, "CANAL_TONE", ""))[:400]
    guide = str(getattr(config, "TITLE_STYLE_GUIDE", "") or "")
    formulas = getattr(config, "TITLE_FORMULAS", []) or []
    good = getattr(config, "TITLE_GOOD_EXAMPLES", []) or []
    bad = getattr(config, "TITLE_BAD_EXAMPLES", []) or []
    policy = str(getattr(config, "TITLE_CAPS_POLICY", "sentence") or "sentence").lower()
    t_min = int(getattr(config, "TITLE_TARGET_MIN_CHARS", 45) or 45)
    t_max = int(getattr(config, "TITLE_TARGET_MAX_CHARS", 70) or 70)
    hard_max = int(getattr(config, "TITLE_MAX_CHARS", 100) or 100)
    band_max = min(t_max, hard_max)

    guion = str(script.get("guion", "") or "")[:2500]
    keyword = _primary_keyword(script, config)
    source_title = ""
    if source_content:
        source_title = str(source_content.get("title", "") or "")
    if not keyword:
        keyword = source_title[:80]

    formulas_str = "\n".join(f"  - {f}" for f in formulas[:8]) or "  (sin fórmulas configuradas)"
    good_str = "\n".join(f"  - {g}" for g in good[:5]) or "  (sin ejemplos)"
    bad_str = "\n".join(f"  - {b}" for b in bad[:5]) or "  (sin ejemplos)"
    guide_str = guide or "(sin guía adicional para este canal)"

    user = f"""CANAL: {name}
NICHO: {style}
TONO: {tone}

ESTILO DE TITULARES (guía del canal):
{guide_str}

FÓRMULAS VÁLIDAS (orientativas, no las copies literalmente):
{formulas_str}

BUENOS EJEMPLOS (imita el registro, no el tema):
{good_str}

MALOS EJEMPLOS (nunca hagas esto):
{bad_str}

TEMA / KEYWORD PRINCIPAL: {keyword or "(deducir del guion)"}
TÍTULO DE LA FUENTE (si existe): {source_title or "(sin fuente)"}

GUION (fragmento):
{guion}

REGLAS OBLIGATORIAS:
- Devuelve exactamente {count} títulos DISTINTOS, en español.
- Longitud objetivo {t_min}-{band_max} caracteres. El gancho debe quedar en los primeros 40.
- Capitalización: {_CAPS_INSTRUCTIONS.get(policy, _CAPS_INSTRUCTIONS["sentence"])}
- Sin emojis. Sin sufijos tipo (REAL), (IMPACTANTE), (REVELACIÓN).
- Prohibido terminar en preposición, artículo o conector.
- Prohibido usar '|' o '[' ']'.
- Máximo UNA palabra en MAYÚSCULAS por título.
- Un número/cifra/nombre propio al frente cuando exista.
- Oculta la RESPUESTA, no el TEMA. ~80% premisa + 20% incógnita.
- Una sola idea por título. Sin promesas que el vídeo no cumpla.

Responde SOLO con JSON: {{"candidates": ["título 1", "título 2", ...]}}"""

    return _CANDIDATE_SYSTEM, user


# ═══════════════════════════════════════════════════════════════════
# TitleEngine
# ═══════════════════════════════════════════════════════════════════

class TitleEngine:
    """Generate, score and select the long-form title for a video script."""

    def __init__(self, config):
        self.config = config

    # ── Public API ────────────────────────────────────────────────

    def generate(
        self,
        script: dict,
        source_content: dict = None,
        use_llm: bool = True,
    ) -> dict:
        """Return ``{selected_title, candidates, rationale}``.

        ``use_llm=False`` forces the deterministic fallback path (used by
        failure-recovery callers and tests).
        """
        script = script or {}
        raw_candidates: list[tuple[str, str]] = []

        if use_llm:
            for title in self._llm_generate_candidates(script, source_content):
                raw_candidates.append((title, "llm"))

        if not raw_candidates:
            raw_candidates = self._fallback_candidates(script, source_content)
            if not raw_candidates:
                raw_candidates = [
                    (deterministic_fallback_title(script, self.config), "deterministic")
                ]

        candidates: list[dict] = []
        seen: set[str] = set()
        for title, source in raw_candidates:
            cleaned = _strip_clickbait_suffix_safe(" ".join(str(title or "").split()))
            cleaned = _remove_injected_markers(cleaned)
            key = normalize(cleaned)
            if not cleaned or key in seen:
                continue
            seen.add(key)
            score, breakdown = score_title(cleaned, script, self.config)
            candidates.append({
                "title": cleaned,
                "rubric_score": score,
                "rubric_breakdown": breakdown,
                "source": source,
            })

        if not candidates:
            fallback = deterministic_fallback_title(script, self.config)
            score, breakdown = score_title(fallback, script, self.config)
            candidates = [{
                "title": fallback,
                "rubric_score": score,
                "rubric_breakdown": breakdown,
                "source": "deterministic",
            }]

        best_index, rationale = self._llm_judge(
            candidates, script, self.config, use_llm=use_llm
        )
        chosen = candidates[best_index]
        selected_title = apply_caps_policy(chosen["title"], self.config)

        # Never ship a banned / dangling / broken selection: fall back to the
        # best clean candidate, then to the deterministic fallback.
        if not selected_title or contains_banned_token(selected_title) or has_dangling_tail(selected_title):
            clean = [
                c for c in candidates
                if not contains_banned_token(c["title"]) and not has_dangling_tail(c["title"])
            ]
            if clean:
                clean.sort(key=lambda c: (-c["rubric_score"], len(c["title"])))
                chosen = clean[0]
                selected_title = apply_caps_policy(chosen["title"], self.config)
            else:
                chosen = {
                    "title": deterministic_fallback_title(script, self.config),
                    "rubric_score": 0,
                    "rubric_breakdown": {},
                    "source": "deterministic",
                }
                selected_title = apply_caps_policy(chosen["title"], self.config)
            if not rationale:
                rationale = "Selección determinista por descarte de candidatos inválidos."
                best_index = candidates.index(chosen) if chosen in candidates else best_index

        return {
            "selected_title": selected_title,
            "candidates": candidates,
            "rationale": rationale,
        }

    # ── Candidate generation ──────────────────────────────────────

    def _llm_generate_candidates(self, script: dict, source_content: dict) -> list[str]:
        try:
            from config.llm_client import create_llm_client
            from config.llm_helpers import llm_json_call
            from config.settings import LLM_MODEL_CREATIVE
        except Exception as exc:  # pragma: no cover - import safety
            logger.warning("TitleEngine: LLM imports unavailable: %s", exc)
            return []

        count = int(getattr(self.config, "TITLE_CANDIDATE_COUNT", 5) or 5)
        system, user = _build_candidate_prompt(self.config, script, source_content, count)
        try:
            client = create_llm_client(enable_thinking=False, timeout=90.0, max_retries=2)
            result = llm_json_call(
                client,
                max_retries=2,
                retry_delay=1.0,
                model=LLM_MODEL_CREATIVE,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.9,
                max_tokens=700,
            )
        except Exception as exc:
            logger.warning("TitleEngine: candidate LLM call failed: %s", exc)
            return []

        if not isinstance(result, dict):
            return []
        raw = result.get("candidates") or result.get("titles") or []
        if isinstance(raw, str):
            raw = _parse_json_list(raw)
        if not isinstance(raw, list):
            return []
        return [str(t).strip() for t in raw if str(t).strip()][:count]

    def _fallback_candidates(
        self, script: dict, source_content: dict
    ) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for title in _parse_json_list(script.get("titulo_options"))[:8]:
            out.append((title, "script"))
        selected = script.get("titulo_selected") or script.get("selected_title")
        if selected:
            out.append((str(selected), "script"))
        if source_content:
            source_title = source_content.get("title")
            if source_title:
                out.append((str(source_title), "source"))
        out.append((deterministic_fallback_title(script, self.config), "deterministic"))
        return out

    # ── Judge ─────────────────────────────────────────────────────

    def _deterministic_best(self, candidates: list[dict]) -> int:
        t_min = int(getattr(self.config, "TITLE_TARGET_MIN_CHARS", 45) or 45)
        t_max = int(getattr(self.config, "TITLE_TARGET_MAX_CHARS", 70) or 70)
        hard_max = int(getattr(self.config, "TITLE_MAX_CHARS", 100) or 100)
        center = (t_min + min(t_max, hard_max)) / 2.0

        def key(item: tuple[int, dict]):
            index, cand = item
            return (-cand["rubric_score"], abs(len(cand["title"]) - center), index)

        return min(enumerate(candidates), key=key)[0]

    def _llm_judge(
        self,
        candidates: list[dict],
        script: dict,
        config,
        use_llm: bool = True,
    ) -> tuple[int, str]:
        if not use_llm or len(candidates) < 2:
            best = self._deterministic_best(candidates)
            return best, self._fallback_rationale(candidates, best)

        try:
            from config.llm_client import create_llm_client
            from config.llm_helpers import llm_json_call
            from config.settings import LLM_MODEL_CREATIVE

            listing = []
            for i, cand in enumerate(candidates):
                listing.append(
                    f'[{i}] "{cand["title"]}" (rubric {cand["rubric_score"]}/14)'
                )
            listing_str = "\n".join(listing)
            keyword = _primary_keyword(script, config)
            user = (
                f"TEMA: {keyword or '(deducir)'}\n\n"
                f"CANDIDATOS:\n{listing_str}\n\n"
                "Elige el mejor título para maximizar clics SIN prometer más de lo "
                "que el vídeo entrega. Prioriza claridad + incógnita real + detalle "
                "concreto. Descarta promesas vacías, frases cortadas y exceso de "
                'mayúsculas. Responde SOLO JSON: {"best_index": <int>, '
                '"rationale": "<motivo breve en español>"}'
            )
            client = create_llm_client(enable_thinking=False, timeout=60.0, max_retries=2)
            result = llm_json_call(
                client,
                max_retries=2,
                retry_delay=1.0,
                model=LLM_MODEL_CREATIVE,
                messages=[
                    {"role": "system", "content": "Eres un editor jefe de YouTube. Eliges el mejor titular."},
                    {"role": "user", "content": user},
                ],
                temperature=0.3,
                max_tokens=250,
            )
            if isinstance(result, dict):
                index = result.get("best_index")
                if isinstance(index, str) and index.strip().isdigit():
                    index = int(index.strip())
                if isinstance(index, int) and 0 <= index < len(candidates):
                    rationale = str(result.get("rationale", "") or "").strip()
                    return index, rationale or self._fallback_rationale(candidates, index)
        except Exception as exc:
            logger.warning("TitleEngine: judge LLM call failed: %s", exc)

        best = self._deterministic_best(candidates)
        return best, self._fallback_rationale(candidates, best)

    @staticmethod
    def _fallback_rationale(candidates: list[dict], index: int) -> str:
        if not candidates or not (0 <= index < len(candidates)):
            return "Selección determinista."
        cand = candidates[index]
        return (
            f"Selección determinista: mayor rubric ({cand['rubric_score']}/14) "
            f"con longitud más cercana al centro de la banda objetivo."
        )
