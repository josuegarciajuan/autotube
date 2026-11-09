"""Content pillars: one schema, one matcher, one usage signal.

`CONTENT_PILLARS` used to be decorative: it was shown to the LLM but never
affected topic selection, and two different schemas coexisted
(``{pillar, pct, description}`` in canal2 vs ``{name, ratio, desc}`` elsewhere).

This module normalises both schemas and exposes the two functions the ranking
pipeline needs:

* :func:`resolve_pillars` — canonical list with ratios summing to 100.
* :func:`match_pillar` — best pillar for a candidate + match score.
* :func:`pillar_usage` — observed share of the last N selections.
* :func:`overuse_penalty` — soft penalty when a pillar exceeds its target.

Everything is fail-open: invalid config returns an empty list and the ranking
keeps working exactly as before.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"[a-záéíóúüñ0-9]{4,}", re.IGNORECASE)

# Small local stopword set: avoids importing pipeline modules at config load.
_STOPWORDS = {
    "para", "como", "cuando", "donde", "sobre", "entre", "desde", "hasta",
    "este", "esta", "esto", "estos", "estas", "aquel", "aquella", "porque",
    "pero", "sino", "también", "solo", "cada", "todo", "toda", "todos",
    "todas", "más", "menos", "muy", "sin", "con", "por", "que", "los",
    "las", "una", "unos", "unas", "del", "las", "sus", "les", "hay",
}


@dataclass(frozen=True)
class Pillar:
    name: str
    ratio: float
    desc: str = ""
    keywords: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ratio": self.ratio,
            "desc": self.desc,
            "keywords": list(self.keywords),
        }


def _tokens(text: str) -> set[str]:
    return {
        t.lower() for t in _TOKEN_RE.findall(str(text or ""))
        if t.lower() not in _STOPWORDS
    }


def _coerce_pillar(item: object) -> Pillar | None:
    if not isinstance(item, dict):
        return None
    name = str(
        item.get("name") or item.get("pillar") or item.get("title") or ""
    ).strip()
    if not name:
        return None
    ratio_raw = item.get("ratio", item.get("pct", item.get("percentage", 0)))
    try:
        ratio = max(0.0, float(ratio_raw))
    except (TypeError, ValueError):
        ratio = 0.0
    desc = str(item.get("desc") or item.get("description") or "").strip()
    keywords = item.get("keywords") or item.get("keyword") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    try:
        keywords = tuple(str(k).strip() for k in keywords if str(k).strip())
    except TypeError:
        keywords = ()
    return Pillar(name=name, ratio=ratio, desc=desc, keywords=keywords)


def normalize_pillars(cfg) -> list[dict]:
    """Return canonical ``[{name, ratio, desc, keywords}]`` (ratios as given)."""
    raw = cfg.get("CONTENT_PILLARS") if isinstance(cfg, dict) else getattr(
        cfg, "CONTENT_PILLARS", None
    )
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for item in raw:
        pillar = _coerce_pillar(item)
        if pillar is not None:
            out.append(pillar.as_dict())
    return out


def resolve_pillars(cfg) -> list[dict]:
    """Canonical pillars with ratios normalised to sum 100 (fail-open)."""
    pillars = normalize_pillars(cfg)
    total = sum(p["ratio"] for p in pillars)
    if not pillars:
        return []
    if total <= 0:
        equal = round(100.0 / len(pillars), 2)
        for p in pillars:
            p["ratio"] = equal
        return pillars
    for p in pillars:
        p["ratio"] = round(p["ratio"] / total * 100.0, 2)
    return pillars


def match_pillar(text: str, pillars: list[dict]) -> tuple[str, float]:
    """Best matching pillar and a 0-1 score based on token coverage."""
    text_tokens = _tokens(text)
    if not text_tokens or not pillars:
        return "", 0.0
    best_name, best_score = "", 0.0
    for pillar in pillars:
        name = str(pillar.get("name", ""))
        corpus = " ".join([
            name,
            str(pillar.get("desc", "")),
            " ".join(str(k) for k in (pillar.get("keywords") or [])),
        ])
        p_tokens = _tokens(corpus) | _tokens(name)
        if not p_tokens:
            continue
        overlap = len(text_tokens & p_tokens)
        if not overlap:
            continue
        # Coverage of the pillar vocabulary, with a small bonus for name hits.
        score = overlap / max(1, len(p_tokens))
        if _tokens(name) & text_tokens:
            score = min(1.0, score + 0.15)
        if score > best_score:
            best_name, best_score = name, score
    return best_name, round(best_score, 4)


def pillar_usage(records: list[dict], window: int = 20) -> dict[str, float]:
    """Observed share per pillar from recent provenance records.

    Each record may be a dict with ``signals_json`` (JSON string/dict) or a
    plain ``pillar`` key. Records without a pillar are ignored.
    """
    counts: dict[str, int] = {}
    considered = 0
    for record in (records or [])[: max(1, int(window))]:
        signals = record.get("signals_json", record) if isinstance(record, dict) else {}
        if isinstance(signals, str):
            try:
                signals = json.loads(signals)
            except (json.JSONDecodeError, TypeError):
                signals = {}
        name = ""
        if isinstance(signals, dict):
            name = str(
                signals.get("pillar") or signals.get("pillar_name") or ""
            ).strip()
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
        considered += 1
    if not considered:
        return {}
    return {name: count / considered for name, count in counts.items()}


def overuse_penalty(
    pillar_name: str,
    usage: dict[str, float],
    pillars: list[dict],
    *,
    margin: float = 0.10,
    penalty: float = 0.15,
) -> float:
    """Penalty when a pillar is above its target share + margin."""
    if not pillar_name or not usage or not pillars:
        return 0.0
    target = next(
        (float(p.get("ratio", 0)) / 100.0 for p in pillars
         if str(p.get("name", "")) == pillar_name),
        0.0,
    )
    if target <= 0:
        return 0.0
    observed = float(usage.get(pillar_name, 0.0))
    return float(penalty) if observed > target + max(0.0, margin) else 0.0
