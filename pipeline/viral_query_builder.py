"""Viral Mirror — Query Builder.

Generates diverse YouTube search queries for viral discovery by combining:
  1. Base keywords from the selected playlist (2-3 queries)
  2. Base keywords from channel identity (2-3 queries)
  3. AI-invented concepts (3-4 fresh queries each execution)
  4. Viral-format variations on AI concepts (2-3 queries)

Avoids repeating queries already used in recent runs (30-day window).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from types import SimpleNamespace

logger = logging.getLogger(__name__)


# ── LLM Concept Generation Prompt ────────────────────────────────────

_CONCEPT_SYSTEM_PROMPT = """You are a viral YouTube content strategist. Your job is to invent
FRESH search angles to find high-performing English YouTube videos that will be
adapted for a Spanish-language documentary channel.

Generate CONCEPTS, not full queries. Each concept must be 2-5 English words,
specific enough to find real videos on YouTube, and DIFFERENT from recently used ones.

Rules:
- Must relate to the channel's theme AND the target playlist
- Must NOT repeat any concept from the "already used" list
- Must be specific (avoid vague terms like "interesting stories")
- Think about sub-niches, angles, formats, and variations
- Output as JSON array of strings: ["concept1", "concept2", "concept3"]

Generate EXACTLY 4 concepts."""


def _get_llm_client(config: Optional[SimpleNamespace] = None, enable_thinking: bool = False):
    """Get OpenAI-compatible client from config."""
    from config.settings import (
        LLM_MODEL_CREATIVE,
        OPENAI_MODEL,
    )
    from config.llm_client import create_llm_client

    model = LLM_MODEL_CREATIVE or OPENAI_MODEL

    return create_llm_client(enable_thinking=enable_thinking), model


def _llm_generate_concepts(
    channel_name: str,
    channel_theme: str,
    playlist_name: str,
    playlist_description: str,
    recently_used_keywords: list[str],
    config: Optional[SimpleNamespace] = None,
) -> list[str]:
    """Generate 4 fresh search concepts using LLM."""
    client, model = _get_llm_client(config)
    if not client:
        logger.warning("No LLM client — using fallback concepts")
        return [
            f"{channel_theme.split(',')[0].strip()} documentary",
            f"best {playlist_name.lower()} stories",
            f"shocking {channel_theme.split()[0]} stories",
            f"unexplained {playlist_name.lower()}",
        ]

    user_msg = f"""Channel: {channel_name}
Theme: {channel_theme}
Target Playlist: {playlist_name}
Playlist Description: {playlist_description}

Already used concepts (DO NOT repeat any of these):
{json.dumps(recently_used_keywords[:15], indent=2)}"""

    try:
        from config.llm_helpers import llm_json_call_or_fallback
        concepts = llm_json_call_or_fallback(
            client,
            fallback=[],
            max_retries=3,
            retry_delay=2.0,
            model=model,
            messages=[
                {"role": "system", "content": _CONCEPT_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.85,
            max_tokens=800,
            response_format={"type": "json_object"},
        )
        concepts = concepts if isinstance(concepts, list) else concepts.get("concepts", [])
        concepts = [c.strip() for c in concepts if isinstance(c, str) and len(c.strip()) > 3]
        if concepts:
            logger.info("LLM generated %d fresh concepts: %s", len(concepts), concepts)
            return concepts[:4]
    except Exception as e:
        logger.warning("LLM concept generation failed after retries: %s", e)

    # Fallback
    return [
        f"unbelievable {playlist_name.lower().split()[0]} stories",
        f"rarest {channel_theme.split(',')[0]}",
        f"documentary {playlist_name.lower()}",
        f"viral {channel_theme.split()[0]} stories",
    ]


def _get_recently_used(db, canal: str, limit: int = 30) -> list[str]:
    """Extract keywords from recently used viral candidates to avoid repeats."""
    try:
        with db._connect() as conn:
            rows = conn.execute(
                """SELECT viral_original_title FROM raw_content
                   WHERE canal = ? AND source_mode = 'viral' AND used = 1
                   ORDER BY scraped_at DESC LIMIT ?""",
                (canal, limit),
            ).fetchall()

        # Extract significant words from titles (avoid generic words)
        stopwords = {"the", "a", "an", "of", "in", "on", "to", "for", "and", "or",
                     "is", "it", "that", "this", "with", "was", "are", "be", "from"}

        all_words = []
        for row in rows:
            title = row["viral_original_title"] or ""
            words = [w.lower().strip(",.!?;:()[]\"'") for w in title.split()
                     if len(w) > 3 and w.lower() not in stopwords]
            all_words.extend(words[:5])  # take top 5 significant words

        # Get unique, and also preserve original titles as full concepts
        seen = set()
        unique = []
        for w in all_words:
            if w not in seen:
                seen.add(w)
                unique.append(w)

        logger.debug("Recently used keywords for %s: %s", canal, unique[:10])
        return unique[:15]
    except Exception as e:
        logger.debug("Could not get recently used keywords: %s", e)
        return []


# ── Public API ─────────────────────────────────────────────────────────

def build_viral_queries(
    channel_slug: str,
    channel_name: str,
    channel_theme: str,
    playlist_name: str,
    playlist_description: str,
    canal_keywords_eng: list[str],
    playlist_keywords: list[str],
    db,
    config: Optional[SimpleNamespace] = None,
) -> list[str]:
    """Build 10-12 diverse search queries for viral discovery.

    Args:
        channel_slug: Channel slug (canal2, canal3, ...)
        channel_name: Display name ("Sincronías")
        channel_theme: Channel tagline/theme
        playlist_name: Selected playlist name
        playlist_description: Playlist description
        canal_keywords_eng: Base English keywords for the channel
        playlist_keywords: English keywords for this specific playlist
        db: Database instance (for recently-used tracking)
        config: Optional channel config

    Returns:
        List of 10-12 unique English search queries.
    """
    queries = []

    # ── 1. Base keywords from playlist (2-3 queries) ────────────
    for kw in playlist_keywords[:3]:
        if kw not in queries:
            queries.append(kw)

    # ── 2. Base keywords from channel (2-3 queries) ─────────────
    for kw in canal_keywords_eng[:3]:
        if kw not in queries:
            queries.append(kw)

    # ── 3. AI-invented concepts (3-4 fresh queries) ─────────────
    recently_used = _get_recently_used(db, channel_slug, limit=30)

    ai_concepts = _llm_generate_concepts(
        channel_name=channel_name,
        channel_theme=channel_theme,
        playlist_name=playlist_name,
        playlist_description=playlist_description,
        recently_used_keywords=recently_used,
        config=config,
    )

    for concept in ai_concepts[:4]:
        if concept not in queries:
            queries.append(concept)

    # ── 4. Viral-format variations on AI concepts (2-3 queries) ──
    viral_formats = [
        "top 5 {}",
        "most shocking {}",
        "{} documentary",
        "the {} you won't believe",
        "incredible {} stories",
    ]
    import random
    random.shuffle(viral_formats)
    for concept in ai_concepts[:2]:
        for fmt in viral_formats[:2]:
            formatted = fmt.format(concept)
            if formatted not in queries and len(queries) < 15:
                queries.append(formatted)

    # ── 5. Filter out recently-used keywords ───────────────────
    filtered = []
    for q in queries:
        q_lower = q.lower().strip()
        is_duplicate = False
        for used in recently_used:
            if used.lower() in q_lower or q_lower in used.lower():
                is_duplicate = True
                break
        if not is_duplicate:
            filtered.append(q)

    if len(filtered) < 6:
        # If too many filtered out, keep the original queries
        filtered = queries

    # Mix order for variety
    random.shuffle(filtered)

    result = filtered[:12]
    logger.info("[%s] Query builder: %d total → %d after dedup → %d final (playlist='%s')",
                channel_slug, len(queries), len(filtered), len(result), playlist_name)
    return result


# ── Strategy 2: AI-generated semantic concepts (20 concepts) ──────────

_BUILD_CONCEPTS_SYSTEM = """You are a viral content discovery strategist. Your job is to generate
diverse search concepts to find high-performing English YouTube videos about a specific niche.

Generate SHORT search concepts (2-5 words each, in English) that someone would type into YouTube
to discover fascinating, high-view-count documentary-style videos on the given topic.

Rules:
- Vary the angle: some should be specific, some broad, some trending-style
- Include at least 3 concepts with "documentary" or "full documentary"
- Include at least 3 with "2024" or "recent" for freshness
- Include at least 3 that use viral-title formats (shocking, unbelievable, you won't believe, etc.)
- Avoid repeating concepts from the "already used" list
- Output as JSON object: {"concepts": ["concept1", "concept2", ...]}"""


def build_expanded_queries(
    channel_slug: str,
    channel_name: str,
    channel_theme: str,
    niche_keywords: list[str],
    count: int = 20,
    db=None,
    config=None,
) -> list[str]:
    """Strategy 2: Generate 20 diverse AI search concepts for viral discovery.

    Uses LLM to invent fresh search angles, avoiding recently-used keywords.
    """
    client, model = _get_llm_client(config)
    recently_used = _get_recently_used(db, channel_slug, limit=30) if db else []

    if not client:
        logger.warning("No LLM client — using keyword-based fallback concepts")
        fallback = []
        viral_formats = [
            "{} documentary", "{} full documentary",
            "incredible {}", "shocking {} stories",
            "{} 2024 documentary"
        ]
        for kw in niche_keywords[:4]:
            for fmt in viral_formats:
                formatted = fmt.format(kw)
                if formatted not in fallback:
                    fallback.append(formatted)
        for kw in niche_keywords[:8]:
            if kw not in fallback:
                fallback.append(kw)
        return fallback[:count]

    user_msg = f"""Channel: {channel_name}
Theme: {channel_theme}
Niche keywords: {', '.join(niche_keywords[:8])}

Already used concepts (DO NOT repeat):
{json.dumps(recently_used[:20], indent=2)}"""

    concepts = []
    try:
        from config.llm_helpers import llm_json_call_or_fallback
        result = llm_json_call_or_fallback(
            client,
            fallback={},
            max_retries=3,
            retry_delay=2.0,
            model=model,
            messages=[
                {"role": "system", "content": _BUILD_CONCEPTS_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.9,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
        raw = result if isinstance(result, list) else result.get("concepts", [])
        concepts = [c.strip() for c in raw if isinstance(c, str) and len(c.strip()) > 3]
    except Exception as e:
        logger.warning("AI concept generation failed after retries: %s", e)

    if len(concepts) < 5:
        # Fallback: expand keywords
        for kw in niche_keywords[:15]:
            if kw not in concepts:
                concepts.append(kw)

    # Filter out recently used
    filtered = []
    for c in concepts:
        c_lower = c.lower()
        is_dup = any(used.lower() in c_lower or c_lower in used.lower() for used in recently_used)
        if not is_dup:
            filtered.append(c)

    if len(filtered) < 8:
        filtered = concepts  # keep all if too many filtered

    import random
    random.shuffle(filtered)
    result = filtered[:count]
    logger.info("[%s] Expanded queries: %d concepts → %d after dedup → %d final",
                channel_slug, len(concepts), len(filtered), len(result))
    return result


# ── Strategy 4: Natural-language search queries (15 queries) ──────────

_NATURAL_LANG_SYSTEM = """You are a YouTube search expert. Your job is to generate natural-language
search queries that a real human would type into YouTube's search bar to find fascinating
documentary-style videos on a specific topic.

Generate full-sentence or long-phrase queries (5-12 words each) in English.
These should sound like what someone would actually type:
- "most mysterious archaeological discoveries of 2024 explained"
- "ancient technology that scientists still can't explain"
- "the lost city that rewrote human history documentary"

Rules:
- Each query must be 5-12 words long
- Must sound natural (like a real person's search)
- Cover different angles of the niche
- At least 5 should include "documentary" or "full documentary"
- At least 3 should have a curiosity-gap ("that changed everything", "they don't want you to know")
- Output as JSON object: {"queries": ["query1", "query2", ...]}"""


def build_natural_language_queries(
    channel_slug: str,
    channel_name: str,
    channel_theme: str,
    niche_keywords: list[str],
    count: int = 15,
    db=None,
    config=None,
) -> list[str]:
    """Strategy 4: Generate 15 natural-language YouTube search queries via LLM."""
    client, model = _get_llm_client(config, enable_thinking=True)
    recently_used = _get_recently_used(db, channel_slug, limit=30) if db else []

    if not client:
        logger.warning("No LLM client — using template-based fallback queries")
        templates = [
            "the most incredible {} documentary",
            "fascinating {} explained",
            "{} that changed history",
            "unbelievable {} stories",
            "{} full documentary 2024",
            "top 10 {} discoveries",
            "{} the world's greatest mysteries",
            "shocking {} documentary",
        ]
        fallback = []
        for kw in niche_keywords[:3]:
            for tpl in templates:
                q = tpl.format(kw)
                if q not in fallback:
                    fallback.append(q)
        return fallback[:count]

    user_msg = f"""Channel: {channel_name}
Theme: {channel_theme}
Niche keywords: {', '.join(niche_keywords[:5])}

Already used topics to avoid:
{json.dumps(recently_used[:15], indent=2)}"""

    queries = []
    try:
        from config.llm_helpers import llm_json_call_or_fallback
        result = llm_json_call_or_fallback(
            client,
            fallback={},
            max_retries=3,
            retry_delay=2.0,
            model=model,
            messages=[
                {"role": "system", "content": _NATURAL_LANG_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.9,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
        raw = result if isinstance(result, list) else result.get("queries", [])
        queries = [q.strip() for q in raw if isinstance(q, str) and len(q.strip()) > 8]
    except Exception as e:
        logger.warning("Natural language query generation failed after retries: %s", e)

    if len(queries) < 5:
        # Fallback
        templates = [
            "the most incredible {} documentary",
            "fascinating {} full documentary",
            "{} explained documentary",
            "unbelievable {} stories",
            "{} documentary 2024",
        ]
        for kw in niche_keywords[:3]:
            for tpl in templates:
                q = tpl.format(kw)
                if q not in queries:
                    queries.append(q)

    # Filter recently used
    filtered = []
    for q in queries:
        q_lower = q.lower()
        is_dup = any(used.lower() in q_lower for used in recently_used)
        if not is_dup:
            filtered.append(q)

    if len(filtered) < 5:
        filtered = queries

    import random
    random.shuffle(filtered)
    result = filtered[:count]
    logger.info("[%s] Natural language queries: %d generated → %d final",
                channel_slug, len(queries), len(result))
    return result
