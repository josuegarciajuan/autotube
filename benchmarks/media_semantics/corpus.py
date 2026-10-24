"""P03-D — real-scene corpus construction.

The corpus is built from narration/visual descriptions that already exist in
the production database (``scripts.bloques_json`` + ``video_scenes``).  Nothing
in this module invents a human relevance label: ``human_label`` stays ``null``.

A benchmark SceneSpec needs the same structured intent the production visual
bible provides.  When an LLM client is available we extract that intent from
the real Spanish scene description + the English stock query (one cheap text
call per scene, cached).  The deterministic fallback never calls the network.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pipeline.media_semantics.contracts import SceneSpec
from pipeline.scene_context import derive_scene_intent

CORPUS_VERSION = 1
DEFAULT_DB = Path("/root/autotube/autotube.db")
DEFAULT_SEED = 20261024
EXCLUDED_CHANNELS = {"test", "testing"}

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "people": (
        "persona", "personas", "hombre", "mujer", "familia", "niño", "niña",
        "rostro", "rostros", "multitud", "gente", "psicólogo", "físico",
        "científico", "médico", "doctor", "paciente", "soldado", "trabajador",
        "person", "people", "man", "woman", "family", "crowd", "scientist",
        "doctor", "patient", "soldier", "worker",
    ),
    "nature": (
        "bosque", "montaña", "montañas", "río", "mar", "océano", "jungla",
        "selva", "árbol", "árboles", "paisaje", "cielo", "tormenta", "nieve",
        "forest", "mountain", "river", "sea", "ocean", "jungle", "tree",
        "landscape", "sky", "storm", "snow", "waterfall", "cascada",
    ),
    "desert_geology": (
        "desierto", "duna", "dunas", "cañón", "roca", "rocas", "geología",
        "excavación", "cueva", "volcán", "yacimiento", "desert", "dune",
        "canyon", "rock", "geology", "excavation", "cave", "volcano",
    ),
    "city": (
        "ciudad", "calle", "edificio", "edificios", "tráfico", "mercado",
        "urbano", "aeropuerto", "estación", "city", "street", "building",
        "traffic", "market", "urban", "airport", "station", "skyline",
    ),
    "history": (
        "antiguo", "antigua", "arqueológico", "ruinas", "siglo", "guerra",
        "templo", "pirámide", "medieval", "imperio", "historical", "ancient",
        "ruins", "century", "war", "temple", "pyramid", "medieval", "empire",
    ),
    "technology": (
        "computadora", "ordenador", "robot", "máquina", "satélite", "teléfono",
        "laboratorio", "datos", "pantalla", "código", "tecnología", "computer",
        "robot", "machine", "satellite", "phone", "laboratory", "data",
        "screen", "code", "technology", "internet",
    ),
    "animals": (
        "animal", "animales", "pájaro", "perro", "gato", "caballo", "insecto",
        "pez", "tiburón", "ballena", "wildlife", "animal", "bird", "dog",
        "cat", "horse", "insect", "fish", "shark", "whale",
    ),
    "space_science": (
        "espacio", "planeta", "estrella", "estrellas", "galaxia", "luna",
        "astronauta", "telescopio", "física", "cuántica", "universo", "space",
        "planet", "star", "galaxy", "moon", "astronaut", "telescope",
        "physics", "quantum", "universe",
    ),
    "medical": (
        "hospital", "médico", "médica", "paciente", "cirugía", "enfermedad",
        "clínico", "hospital", "doctor", "patient", "surgery", "disease",
        "clinical", "medical",
    ),
    "action": (
        "corre", "corren", "camina", "caminan", "escala", "escalan", "lucha",
        "luchan", "huye", "huyen", "nada", "nadan", "vuela", "vuelan",
        "conduce", "conducen", "construye", "construyen", "busca", "buscan",
        "running", "walking", "climbing", "fighting", "escaping", "swimming",
        "flying", "driving", "building", "searching", "diving",
    ),
}

CATEGORY_ENVIRONMENT = {
    "people": "people in a real environment",
    "nature": "natural landscape",
    "desert_geology": "desert or geological landscape",
    "city": "city street or urban environment",
    "history": "historical archaeological setting",
    "technology": "technology laboratory or data environment",
    "animals": "wildlife in its habitat",
    "space_science": "space or scientific environment",
    "medical": "medical or hospital environment",
    "action": "observable physical action",
    "other": "documentary scene",
}

# Historical constraints are benchmark-injected to exercise era/negative
# handling.  They are derived from the era, not human labels.
ERA_AVOID = ["smartphone", "laptop", "modern car", "neon sign"]

_WORD_RE = re.compile(r"[a-z0-9áéíóúüñ]+", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-2][0-9])\b")


@dataclass
class Block:
    script_id: int
    video_id: int | None
    channel: str
    title: str
    block_idx: int
    narration: str
    description: str
    query_en: str
    media_type: str
    duration: float
    category: str = "other"

    @property
    def scene_id(self) -> str:
        return f"s{self.script_id:04d}-b{self.block_idx:02d}"


def _norm(text: Any) -> str:
    return " ".join(_WORD_RE.findall(str(text or "").lower()))


def classify_category(*texts: Any) -> str:
    """Deterministic keyword classification (first match by keyword count)."""
    haystack = " ".join(_norm(t) for t in texts if t)
    if not haystack:
        return "other"
    scores: dict[str, int] = {}
    for category, words in CATEGORY_KEYWORDS.items():
        score = sum(1 for word in words if word in haystack)
        if score:
            scores[category] = score
    if not scores:
        return "other"
    # Deterministic tie-break: highest score, then category name.
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[0][0]


def detect_era(*texts: Any) -> str:
    """Return a normalized era phrase when the scene is clearly historical.

    A year is mapped to a decade/century so the existing era vocabulary can
    act on it.  Modern scenes return ``""``.
    """
    text = " ".join(str(t or "") for t in texts)
    years = [int(y) for y in _YEAR_RE.findall(text)]
    historical_years = [y for y in years if y < 2000]
    if historical_years:
        year = min(historical_years)
        if year >= 1900:
            return f"{(year // 10) * 10}s"
        if year >= 1800:
            return "19th century"
        if year >= 1700:
            return "18th century"
        if year >= 1600:
            return "17th century"
        if year >= 1500:
            return "16th century"
        return "medieval"
    low = text.lower()
    if "medieval" in low or "edad media" in low:
        return "medieval"
    if "antiguo egipto" in low or "egipto" in low or "egipcio" in low:
        return "ancient"
    if "roma" in low or "romano" in low or "imperio romano" in low:
        return "ancient"
    if "prehistoria" in low or "prehistóric" in low:
        return "prehistoric"
    return ""


def load_blocks(db_path: str | Path = DEFAULT_DB) -> list[Block]:
    """Read every usable block from ``scripts.bloques_json`` (read-only)."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    blocks: list[Block] = []
    try:
        rows = connection.execute(
            """
            SELECT s.id AS script_id, v.id AS video_id, s.canal AS channel,
                   COALESCE(s.titulo_selected, '') AS title, s.bloques_json AS blocks
              FROM scripts s
              LEFT JOIN videos v ON v.script_id = s.id
             WHERE s.bloques_json IS NOT NULL
               AND length(s.bloques_json) > 100
               AND COALESCE(s.canal, '') NOT IN ('test', 'testing')
             ORDER BY s.id DESC
            """
        )
        for row in rows:
            try:
                items = json.loads(row["blocks"] or "[]")
            except Exception:
                continue
            if not isinstance(items, list):
                continue
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                narration = str(item.get("texto") or "").strip()
                description = str(item.get("escena_descripcion") or "").strip()
                query_en = str(item.get("search_query_en") or "").strip()
                if len(narration) < 40 or len(description) < 25 or len(query_en) < 5:
                    continue
                try:
                    duration = float(item.get("media_duracion") or 8.0)
                except (TypeError, ValueError):
                    duration = 8.0
                media_type = str(item.get("media_tipo") or "image").strip().lower()
                if media_type not in ("image", "video"):
                    media_type = "image"
                blocks.append(
                    Block(
                        script_id=int(row["script_id"]),
                        video_id=int(row["video_id"]) if row["video_id"] is not None else None,
                        channel=str(row["channel"] or ""),
                        title=str(row["title"] or ""),
                        block_idx=idx,
                        narration=narration,
                        description=description,
                        query_en=query_en,
                        media_type=media_type,
                        duration=duration,
                        category=classify_category(description, query_en, narration),
                    )
                )
    finally:
        connection.close()
    return blocks


def _stable_key(block: Block) -> str:
    # Deterministic, portable ordering: no Python hash randomisation.
    import hashlib

    raw = f"{block.script_id}:{block.block_idx}:{block.query_en}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def sample_blocks(
    blocks: Iterable[Block],
    *,
    limit: int = 44,
    per_category: int = 6,
    seed: int = DEFAULT_SEED,
    channels: tuple[str, ...] | None = None,
) -> list[Block]:
    """Deterministic diverse sample: round-robin across categories/channels."""
    pool = [b for b in blocks if not channels or b.channel in channels]
    by_category: dict[str, list[Block]] = {}
    for block in pool:
        by_category.setdefault(block.category, []).append(block)
    for items in by_category.values():
        items.sort(key=_stable_key)

    # Rotate categories deterministically so the first N are diverse.
    categories = sorted(by_category)
    if seed:
        categories = categories[seed % len(categories):] + categories[: seed % len(categories)]

    selected: list[Block] = []
    per_channel: dict[str, int] = {}
    # Round-robin, respecting per-category caps and balancing channels.
    cursors = {c: 0 for c in categories}
    while len(selected) < limit:
        progressed = False
        for category in categories:
            items = by_category[category]
            cursor = cursors[category]
            while cursor < len(items):
                candidate = items[cursor]
                cursor += 1
                if per_category and sum(
                    1 for b in selected if b.category == category
                ) >= per_category:
                    break
                channel_count = per_channel.get(candidate.channel, 0)
                # Prefer under-represented channels (soft constraint).
                if channel_count >= max(2, (limit // 5) + 1):
                    continue
                selected.append(candidate)
                per_channel[candidate.channel] = channel_count + 1
                progressed = True
                break
            cursors[category] = cursor
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


def _default_intent(block: Block) -> dict[str, Any]:
    intent = derive_scene_intent(
        {"fragment_text": block.narration, "search_query_en": block.query_en}
    )
    environment = CATEGORY_ENVIRONMENT.get(block.category, "")
    return {
        "subject": block.query_en,
        "action": intent.get("action", "") or "",
        "environment": environment,
        "visual_concept": block.query_en,
        "must_include": [],
        "source": "deterministic",
    }


def _sanitize_intent(payload: Any, block: Block) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _default_intent(block)
    fallback = _default_intent(block)

    def text(key: str) -> str:
        value = str(payload.get(key) or "").strip()
        return value[:180]

    must_include = []
    for item in payload.get("must_include") or []:
        value = str(item or "").strip()
        if value and len(value) <= 80 and value not in must_include:
            must_include.append(value)
    result = {
        "subject": text("subject") or fallback["subject"],
        "action": text("action") or fallback["action"],
        "environment": text("environment") or fallback["environment"],
        "visual_concept": text("visual_concept") or fallback["visual_concept"],
        "must_include": must_include[:2],
        "source": "llm_intent_extraction",
    }
    return result


def extract_intents_llm(
    blocks: Iterable[Block],
    cache_path: str | Path,
    *,
    client: Any = None,
    model: str = "",
    allow_network: bool = False,
    log_fn: Any = None,
) -> dict[str, dict[str, Any]]:
    """Cache-backed intent extraction.  A cached entry never calls the LLM."""
    cache_path = Path(cache_path)
    cache: dict[str, dict[str, Any]] = {}
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    pending = [b for b in blocks if b.scene_id not in cache]
    if pending and not allow_network:
        pending = []
    if pending:
        if client is None:
            from config.llm_client import create_llm_client

            client = create_llm_client(enable_thinking=False)
        for block in pending:
            prompt = (
                "Extract filmable visual intent from this real documentary scene. "
                "Return ONLY JSON with keys subject, action, environment, "
                "visual_concept, must_include. Values must be short English "
                "phrases. 'action' must be a gerund. Do not invent details that "
                "are not supported by the description or the query.\n\n"
                f"SCENE DESCRIPTION (Spanish): {block.description}\n"
                f"STOCK QUERY (English): {block.query_en}\n"
                f"NARRATION (Spanish, intent only): {block.narration[:700]}"
            )
            try:
                response = client.chat.completions.create(
                    model=model or "deepseek-chat",
                    messages=[
                        {"role": "system", "content": "Return only compact JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.0,
                    max_tokens=260,
                )
                raw = str(response.choices[0].message.content or "")
                match = re.search(r"\{.*\}", raw, re.DOTALL)
                payload = json.loads(match.group(0)) if match else {}
            except Exception as exc:  # fail-open to deterministic intent
                if log_fn:
                    log_fn(f"intent extraction failed for {block.scene_id}: {exc}")
                payload = {}
            cache[block.scene_id] = _sanitize_intent(payload, block)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    return {b.scene_id: cache.get(b.scene_id) or _default_intent(b) for b in blocks}


def build_scene_spec(block: Block, intent: dict[str, Any] | None = None) -> SceneSpec:
    intent = intent or _default_intent(block)
    era = detect_era(block.narration, block.description)
    must_avoid = list(ERA_AVOID) if era else []
    return SceneSpec(
        scene_idx=block.block_idx,
        block_idx=block.block_idx,
        origin="block",
        narration_fragment=block.narration,
        subject=str(intent.get("subject") or block.query_en),
        action=str(intent.get("action") or ""),
        environment=str(intent.get("environment") or ""),
        era=era,
        desired_shot="wide establishing shot, documentary b-roll",
        visual_concept=str(intent.get("visual_concept") or block.query_en),
        mood_style="",
        must_include=list(intent.get("must_include") or []),
        must_avoid=must_avoid,
        duration=block.duration,
        media_type=block.media_type,
        depiction_mode="documentary",
        source="llm_intent_extraction"
        if intent.get("source") == "llm_intent_extraction"
        else "deterministic",
        confidence=0.9 if intent.get("source") == "llm_intent_extraction" else 0.6,
        extra={
            "script_title": block.title,
            "query_en": block.query_en,
            "category": block.category,
            "benchmark_injected_must_avoid": must_avoid,
        },
    )


def build_corpus(
    db_path: str | Path,
    out_path: str | Path,
    intent_cache: str | Path,
    *,
    limit: int = 44,
    per_category: int = 6,
    seed: int = DEFAULT_SEED,
    allow_llm: bool = False,
    llm_client: Any = None,
    llm_model: str = "",
    log_fn: Any = None,
) -> dict[str, Any]:
    blocks = load_blocks(db_path)
    selected = sample_blocks(blocks, limit=limit, per_category=per_category, seed=seed)
    intents = extract_intents_llm(
        selected,
        intent_cache,
        client=llm_client,
        model=llm_model,
        allow_network=allow_llm,
        log_fn=log_fn,
    )
    entries = []
    for block in selected:
        spec = build_scene_spec(block, intents.get(block.scene_id))
        entries.append(
            {
                "scene_id": block.scene_id,
                "script_id": block.script_id,
                "video_id": block.video_id,
                "channel": block.channel,
                "script_title": block.title,
                "block_idx": block.block_idx,
                "category": block.category,
                "era": spec.era,
                "narration_fragment": block.narration,
                "visual_description_es": block.description,
                "query_en": block.query_en,
                "media_type": block.media_type,
                "duration": block.duration,
                "intent": intents.get(block.scene_id) or {},
                "scene_spec": spec.to_dict(),
                "human_label": None,
            }
        )
    corpus = {
        "version": CORPUS_VERSION,
        "source_db": str(Path(db_path).resolve()),
        "seed": seed,
        "limit": limit,
        "per_category": per_category,
        "intent_source": sorted({e["intent"].get("source", "") for e in entries}),
        "entries": entries,
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(corpus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return corpus


def load_corpus(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if int(data.get("version") or 0) != CORPUS_VERSION:
        raise ValueError(f"unsupported corpus version: {data.get('version')}")
    return data


def corpus_diversity(corpus: dict[str, Any]) -> dict[str, Any]:
    entries = corpus.get("entries") or []
    categories: dict[str, int] = {}
    channels: dict[str, int] = {}
    media_types: dict[str, int] = {}
    era_scenes = 0
    must_avoid_scenes = 0
    for entry in entries:
        categories[entry["category"]] = categories.get(entry["category"], 0) + 1
        channels[entry["channel"]] = channels.get(entry["channel"], 0) + 1
        media_types[entry["media_type"]] = media_types.get(entry["media_type"], 0) + 1
        if entry.get("era"):
            era_scenes += 1
        if (entry.get("scene_spec") or {}).get("must_avoid"):
            must_avoid_scenes += 1
    return {
        "scenes": len(entries),
        "categories": dict(sorted(categories.items())),
        "channels": dict(sorted(channels.items())),
        "media_types": dict(sorted(media_types.items())),
        "era_scenes": era_scenes,
        "must_avoid_scenes": must_avoid_scenes,
    }
