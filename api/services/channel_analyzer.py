"""Multi-pass LLM analysis for channel optimization.

2-phase pipeline running in background thread:
  1. EXPLORATION — find ALL patterns, anomalies, correlations in raw data
  2. HYPOTHESIS+RECOMMENDATIONS — unified pass: causal explanations → actionable config changes

Resilience features (v20.1):
  - Streaming LLM calls with dead-man switch (120s no-token = hung)
  - Heartbeat thread updates DB every 15s while analysis is alive
  - Auto-retry up to 3 attempts on recoverable failures
  - Configurable cancel flag for user-initiated stop
  - Data summarization for faster LLM processing (top 20 videos, 15 growth points)
  - Granular phase_detail for frontend feedback

Progress is written to the ``channel_insights`` table after each phase.
The frontend polls ``GET /api/channels/{id}/insights/latest`` for updates.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

from config.llm_client import create_llm_client
from config.settings import LLM_MODEL_INSIGHTS
from database.db_extended import ExtendedDatabase
from config.config_bridge import get_channel_config

logger = logging.getLogger(__name__)

# ── Cancel flags (populated by router, checked by analysis thread) ──

_CANCEL_FLAGS: dict[int, bool] = {}
_cancel_lock = threading.Lock()


def request_cancel(insight_id: int) -> None:
    """Set cancel flag for an in-progress analysis."""
    with _cancel_lock:
        _CANCEL_FLAGS[insight_id] = True


def _is_cancelled(insight_id: int) -> bool:
    """Check if the analysis has been cancelled."""
    with _cancel_lock:
        return _CANCEL_FLAGS.pop(insight_id, False)


# ── Streaming constants ─────────────────────────────────────────────

DEAD_MAN_INTERVAL = 120   # seconds without a token → API hung
TOTAL_TIMEOUT = 1200       # absolute ceiling per LLM call (20 min)
HEARTBEAT_INTERVAL = 15    # seconds between DB heartbeat updates

# ── Config keys the LLM can propose changes for ──────────────────────

_CONFIG_KEYS = [
    # Duration / production
    "VIDEO_AVERAGE_DURATION_MIN", "video_duration_discrepancy_min",
    "prod_script_words_min", "prod_script_words_max",
    "prod_script_scenes_min", "prod_script_scenes_max",
    "prod_script_blocks_min", "prod_script_blocks_max",
    "prod_video_duration_min", "prod_video_duration_max",
    # Publishing
    "PUBLISH_TARGET_HOUR", "PUBLISH_MODE", "PUBLISH_TIMEZONE",
    "PUBLISH_JITTER_MIN", "PUBLISH_WARMUP_MIN",
    # Keywords / SEO
    "CHANNEL_KEYWORDS", "SEO_SECONDARY_KEYWORDS", "SEO_PRIMARY_KEYWORD",
    "SEO_HASHTAGS", "NICHE_KEYWORDS_ENG",
    # Content / structure
    "CONTENT_PILLARS", "canal_tone", "canal_narrative_style",
    "script_hook_rule", "script_end_hook", "script_emotional_arc",
    # Titles
    "title_formulas", "title_power_words",
    # Visual
    "image_style_modifiers", "thumbnail_style",
]

# ── Phase prompts ────────────────────────────────────────────────────

_EXPLORATION_SYSTEM = """\
You are a senior data analyst specializing in YouTube channel performance optimization.
Your job is to find ALL statistically significant patterns, anomalies, and correlations
in raw channel data. Think step by step. Cite specific data points (exact numbers, dates).

Categories to explore exhaustively:
1. Video duration vs retention / watchtime / revenue
2. Publish hour and day vs 24h and 7d views
3. Keywords/topics vs CTR, views, and retention
4. Title power words: which emotional/impact words in titles correlate with higher views/CTR?
   Identify specific words that appear in top-performing titles and are MISSING from low performers.
5. Pipeline errors — which phases fail most, common error messages
6. Content pillar balance vs performance
7. Growth trends (subs, views) and inflection points
8. Revenue patterns (CPM correlation with topic/duration)

For each finding: state the EXACT magnitude (e.g. "3x", "+40%"), cite the data point,
and classify the category.

Return ONLY valid JSON. No markdown, no explanation outside the JSON."""

_EXPLORATION_USER = """\
<channel_data>
{data_json}
</channel_data>

<task>
Analyze this channel exhaustively. Find EVERY meaningful pattern, anomaly, correlation.

Return JSON:
{{
  "patterns": [
    {{
      "name": "short descriptive name",
      "category": "duracion|hora_publicacion|keywords|contenido|errores",
      "finding": "detailed data-backed description (Spanish, 1-2 sentences)",
      "data_points": ["specific metric: value", "specific metric: value"],
      "magnitude": "3x|+40%|-15%|23% error rate",
      "direction": "positive|negative|neutral"
    }}
  ],
  "data_summary": {{
    "total_videos_analyzed": <int>,
    "date_range": "YYYY-MM-DD to YYYY-MM-DD",
    "most_impactful_pattern": "<name of single most significant pattern>"
  }}
}}
</task>"""

# ── Unified Phase 2: hypothesis + recommendations in one LLM call ──

_UNIFIED_SYSTEM = """\
You are a YouTube channel optimization engineer. You have discovered patterns in channel data.
Now you must do TWO things in ONE response:
  1. Formulate causal hypotheses for each pattern (why it exists, confidence, counter-argument)
  2. Convert the strongest hypotheses into concrete, actionable recommendations with exact config key → value mappings.

RULES:
- Every recommendation must map to specific config keys from the provided list.
- Values must be valid for the key type (integer for durations, string for keywords, etc.).
- If a change requires CODE modifications (not config), mark requires_code=true
  and provide an opencode_prompt with instructions for the developer.
- Write all titles, details, and summaries in SPANISH.
- Cite specific data in every recommendation.
- Filter out patterns with weak evidence (confidence < 30).
- All recommendations should BUILD UPON and ENRICH any previous analysis, not replace it.

Return ONLY valid JSON. No markdown."""

_UNIFIED_USER = """\
<patterns>
{patterns_json}
</patterns>

<data>
{data_json}
</data>

<current_config>
{config_json}
</current_config>

<available_config_keys>
{config_keys}
</available_config_keys>

<previous_recommendations>
The following recommendations were made in a PREVIOUS analysis of this channel.
- Applied recommendations have already been applied to the config. DO NOT repeat them.
- Discarded recommendations were intentionally rejected by the user. DO NOT repeat them
  unless you have strong NEW evidence that changes the situation.
- If a previous recommendation covers similar ground but you have updated data,
  acknowledge it and explain what changed.
- Your new recommendations should BUILD UPON and ENRICH the previous analysis,
  not replace it entirely. Focus on what is NEW or has CHANGED since the last analysis.
{prev_recommendations_json}
</previous_recommendations>

<task>
Step 1: For each pattern, formulate a causal hypothesis. Filter out patterns with confidence < 30.
Step 2: Convert the strongest hypotheses into actionable recommendations with exact config changes.

Return JSON:
{{
  "hypotheses": [
    {{
      "pattern_name": "<match pattern name exactly>",
      "category": "duracion|hora_publicacion|keywords|contenido|errores",
      "explanation": "causal mechanism (Spanish, 2-3 sentences)",
      "proposed_change": "what specific change to make",
      "expected_outcome": "quantified prediction (e.g. '+30% views in 24h')",
      "confidence": 85,
      "counter_argument": "why this might NOT work (Spanish, 1 sentence)",
      "evidence_strength": "fuerte|moderado|debil"
    }}
  ],
  "analysis_summary": "2-3 paragraph executive summary in Spanish covering the channel's overall health, top 3 opportunities, and biggest risks. Reference the previous analysis if applicable (e.g., 'Since the last analysis...').",
  "health_score": 72,
  "key_metrics": [
    {{ "label": "Views/dia", "value": "2,100", "sparkline": [10,12,11,15,13,14,15], "delta": "+8%", "delta_positive": true }}
  ],
  "recommendations": [
    {{
      "id": "<uuid-v4>",
      "category": "duracion|hora_publicacion|keywords|contenido|errores",
      "title": "short actionable title (Spanish, max 8 words)",
      "detail": "data-backed explanation (Spanish, 2-4 sentences)",
      "confidence": 85,
      "expected_impact": "alta|media|baja",
      "config_changes": {{ "VIDEO_AVERAGE_DURATION_MIN": 11 }},
      "data_cited": {{ "metric_name": "exact value from data" }},
      "requires_code": false,
      "opencode_prompt": null,
      "rationale_brief": "1-sentence summary of why (Spanish)"
    }}
  ]
}}

IMPORTANT for key_metrics: provide exactly 4 metrics (views/dia, retention, errores_pipeline, subs_30d).
Each sparkline must be an array of 7 integers representing the last 7 data points.
</task>"""


# ── Data aggregation ────────────────────────────────────────────────

def _aggregate_channel_data(db: ExtendedDatabase, channel_id: int,
                            slug: str) -> dict[str, Any]:
    """Collect all relevant data for LLM analysis in one structured dict.

    v20.1: Summarizes data to speed up LLM processing without losing signal.
      - Top 20 videos by recency (not 50)
      - Last 15 channel growth points (not all)
      - Last 20 content patterns (not 30)
    """
    data: dict[str, Any] = {}

    # Channel identity
    ch = db.get_channel(channel_id)
    data["channel"] = {
        "id": channel_id, "slug": slug, "name": ch.get("name", ""),
        "description": ch.get("description", ""),
    }

    # Current config (key values the LLM can reason about)
    try:
        config = get_channel_config(slug)
        data["current_config"] = {
            k: getattr(config, k, None)
            for k in _CONFIG_KEYS
            if hasattr(config, k)
        }
    except Exception:
        data["current_config"] = {}

    # Title power words inventory
    data["title_power_words"] = data["current_config"].get("title_power_words", [])

    # ── Video performance — top 20 by recency ──
    with db._connect() as conn:
        rows = conn.execute("""
            SELECT
                v.id, v.titulo_final, v.duracion_seg,
                v.uploaded_at, v.published_at, v.publish_mode,
                vsh.views, vsh.likes, vsh.comments,
                vsh.average_view_duration, vsh.estimated_minutes_watched,
                vsh.estimated_revenue_min, vsh.estimated_revenue_max,
                vsh.fetched_at
            FROM videos v
            LEFT JOIN (
                SELECT video_id, views, likes, comments,
                       average_view_duration, estimated_minutes_watched,
                       estimated_revenue_min, estimated_revenue_max,
                       fetched_at,
                       ROW_NUMBER() OVER (PARTITION BY video_id ORDER BY fetched_at DESC) as rn
                FROM video_stats_history
            ) vsh ON v.id = vsh.video_id AND vsh.rn = 1
            WHERE v.channel_id = ? AND v.status NOT IN ('draft', 'error_deleted')
            ORDER BY v.uploaded_at DESC
            LIMIT 20
        """, (channel_id,)).fetchall()
        data["video_performance"] = [dict(r) for r in rows]

    # ── Channel growth — last 15 data points ──
    with db._connect() as conn:
        rows = conn.execute("""
            SELECT subscribers, total_views, video_count,
                   estimated_minutes_watched, fetched_at
            FROM channel_stats_history
            WHERE channel_id = ?
            ORDER BY fetched_at DESC
            LIMIT 15
        """, (channel_id,)).fetchall()
        data["channel_growth"] = list(reversed([dict(r) for r in rows]))

    # ── Pipeline health ──
    with db._connect() as conn:
        rows = conn.execute("""
            SELECT phase, status, COUNT(*) as count
            FROM pipeline_log
            WHERE canal = ? AND status = 'error'
            GROUP BY phase, status
        """, (slug,)).fetchall()
        data["pipeline_errors_by_phase"] = [dict(r) for r in rows]

        rows = conn.execute("""
            SELECT alert_type, severity, title, message, created_at
            FROM pipeline_alerts
            WHERE channel_id = ? AND resolved = 0
            ORDER BY created_at DESC LIMIT 20
        """, (channel_id,)).fetchall()
        data["pipeline_alerts"] = [dict(r) for r in rows]

    # ── Content patterns — last 20 ──
    with db._connect() as conn:
        rows = conn.execute("""
            SELECT s.keywords_json, s.bloques_json, s.titulo_selected,
                   s.duracion_estimada, v.id as video_id
            FROM scripts s
            JOIN videos v ON v.script_id = s.id
            WHERE v.channel_id = ? AND s.keywords_json IS NOT NULL
            ORDER BY s.created_at DESC
            LIMIT 20
        """, (channel_id,)).fetchall()
        data["content_patterns"] = []
        for r in rows:
            row_dict = dict(r)
            try:
                row_dict["keywords"] = json.loads(r["keywords_json"] or "[]")
            except json.JSONDecodeError:
                row_dict["keywords"] = []
            data["content_patterns"].append(row_dict)

    # ── Timing data — last 20 ──
    with db._connect() as conn:
        rows = conn.execute("""
            SELECT v.id, v.titulo_final, v.timing_data, v.uploaded_at,
                   v.published_at, v.publish_mode
            FROM videos v
            WHERE v.channel_id = ? AND v.timing_data IS NOT NULL
              AND v.timing_data != '{}'
            ORDER BY v.uploaded_at DESC
            LIMIT 20
        """, (channel_id,)).fetchall()
        data["timing_data"] = [dict(r) for r in rows]

    return data


def _serialize_config(config_ns) -> dict[str, Any]:
    """Convert SimpleNamespace config to plain dict for LLM prompt."""
    d = {}
    for k in _CONFIG_KEYS:
        if hasattr(config_ns, k):
            v = getattr(config_ns, k)
            if isinstance(v, (list, dict, str, int, float, bool, type(None))):
                d[k] = v
            else:
                d[k] = str(v)
    return d


# ── Streaming LLM phase runner with dead-man switch ──────────────────

def _extract_json_from_content(content: str) -> dict:
    """Robust JSON extraction from LLM streaming output.

    Handles markdown fences, thinking-mode preamble text, and other
    common LLM response wrapping patterns.
    """
    text = content.strip()

    # 1. Try markdown code fence extraction (anywhere in the text)
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    else:
        # 2. Try to extract the first JSON object { ... } from anywhere
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)

    return json.loads(text)


def _run_phase_streaming(
    client,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    insight_id: int,
    phase_label: str,
) -> dict:
    """Execute one LLM pass with streaming and dead-man switch.

    Streaming enables:
    - Dead-man switch: if no token arrives for DEAD_MAN_INTERVAL seconds,
      the API is considered hung and a TimeoutError is raised.
    - Total timeout: TOTAL_TIMEOUT seconds absolute ceiling.
    - Live phase_detail updates to the DB for frontend feedback.

    Args:
        client: OpenAI-compatible client (with thinking mode configured).
        model: Model name string.
        messages: Chat messages list.
        max_tokens: Max tokens for the completion.
        temperature: Temperature for the completion.
        insight_id: Insight row ID for DB updates.
        phase_label: Human-readable label for phase_detail (e.g. "Phase 1: Exploration").

    Returns:
        {content: dict, tokens_in: int, tokens_out: int, duration_ms: int}
    """
    db = ExtendedDatabase()
    t0 = time.monotonic()
    content_parts: list[str] = []
    last_token_at = t0
    token_count = 0
    reasoning_count = 0

    db.update_insight_phase_detail(
        insight_id,
        f"{phase_label} — iniciando streaming (max {max_tokens} tokens)...",
    )

    try:
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )

        for chunk in stream:
            now = time.monotonic()

            # Dead-man check
            if now - last_token_at > DEAD_MAN_INTERVAL:
                db.update_insight_phase_detail(
                    insight_id,
                    f"{phase_label} — TIMEOUT: sin tokens durante {DEAD_MAN_INTERVAL}s",
                )
                raise TimeoutError(
                    f"LLM stalled: no tokens for {DEAD_MAN_INTERVAL}s "
                    f"({token_count} content, {reasoning_count} reasoning tokens received)"
                )

            # Total timeout check
            if now - t0 > TOTAL_TIMEOUT:
                db.update_insight_phase_detail(
                    insight_id,
                    f"{phase_label} — TIMEOUT: excedido limite de {TOTAL_TIMEOUT}s",
                )
                raise TimeoutError(
                    f"LLM exceeded total timeout of {TOTAL_TIMEOUT}s"
                )

            # Collect content
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue

            if getattr(delta, "reasoning_content", None):
                reasoning_count += 1
                last_token_at = now
                # Update DB with reasoning progress every ~50 reasoning tokens
                if reasoning_count % 50 == 0:
                    db.update_insight_phase_detail(
                        insight_id,
                        f"{phase_label} — razonando... ({reasoning_count} tokens de pensamiento, "
                        f"{token_count} tokens de contenido)",
                    )
                continue

            if delta.content:
                content_parts.append(delta.content)
                token_count += 1
                last_token_at = now
                # Update DB every ~100 content tokens
                if token_count % 100 == 0:
                    db.update_insight_phase_detail(
                        insight_id,
                        f"{phase_label} — generando respuesta... ({token_count} tokens)",
                    )

        # ── Streaming complete ────────────────────────
        full_content = "".join(content_parts)
        db.update_insight_phase_detail(
            insight_id,
            f"{phase_label} — completo: {token_count} tokens en "
            f"{int(now - t0)}s",
        )

        if not full_content.strip():
            raise ValueError(
                f"LLM returned empty content after streaming "
                f"({token_count} tokens, {reasoning_count} reasoning)"
            )

        # Parse JSON
        result = _extract_json_from_content(full_content)

        # Rough token estimates
        user_messages_text = " ".join(
            m["content"] for m in messages if isinstance(m.get("content"), str)
        )
        tokens_in = len(user_messages_text) // 4
        tokens_out = len(full_content) // 4

        return {
            "content": result,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "duration_ms": int((time.monotonic() - t0) * 1000),
        }

    except TimeoutError:
        raise
    except Exception as e:
        db.update_insight_phase_detail(
            insight_id,
            f"{phase_label} — ERROR: {e}",
        )
        raise


def _run_phase_streaming_with_retry(
    client,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    insight_id: int,
    phase_label: str,
    max_retries: int = 1,
) -> dict:
    """Streaming LLM call with one retry on empty/broken content.

    If thinking mode produces empty content (reasoning consumed all tokens),
    retries once with thinking disabled and doubled max_tokens.
    """
    db = ExtendedDatabase()

    try:
        return _run_phase_streaming(
            client, model, messages, max_tokens, temperature,
            insight_id, phase_label,
        )
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning(
            "%s: LLM returned empty/broken content — "
            "retrying with thinking=disabled, max_tokens=%d",
            phase_label, max_tokens * 2,
        )
        db.update_insight_phase_detail(
            insight_id,
            f"{phase_label} — reintentando sin thinking mode...",
        )
        try:
            no_thinking_client = create_llm_client(
                enable_thinking=False,
                timeout=float(TOTAL_TIMEOUT),
                max_retries=0,
            )
            return _run_phase_streaming(
                no_thinking_client, model, messages, max_tokens * 2,
                temperature, insight_id,
                f"{phase_label} (sin thinking)",
            )
        except Exception as e2:
            logger.error("%s: thinking-disabled fallback also failed: %s", phase_label, e2)
            raise


# ── Heartbeat management ─────────────────────────────────────────────

def _start_heartbeat(insight_id: int, stop_event: threading.Event) -> threading.Thread:
    """Launch a daemon thread that updates heartbeat_at every HEARTBEAT_INTERVAL seconds.

    Args:
        insight_id: Insight row ID to heartbeat.
        stop_event: Set this event to stop the heartbeat thread.

    Returns:
        The heartbeat thread (already started, daemon=True).
    """
    def _heartbeat_loop():
        db = ExtendedDatabase()
        while not stop_event.is_set():
            try:
                db.update_insight_heartbeat(insight_id)
            except Exception:
                logger.debug("Heartbeat update failed (non-critical)", exc_info=True)
            stop_event.wait(HEARTBEAT_INTERVAL)

    t = threading.Thread(
        target=_heartbeat_loop,
        name=f"autotube-heartbeat-{insight_id}",
        daemon=True,
    )
    t.start()
    return t


# ── Main entry point (called from background thread) ─────────────────

MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5


def run_channel_analysis_sync(insight_id: int, channel_id: int,
                              slug: str) -> None:
    """Run the 2-phase LLM analysis with streaming, heartbeat, and retry.

    Designed for ThreadPoolExecutor. Updates ``channel_insights`` row
    after each phase so the frontend can poll for real-time progress.
    """
    db = ExtendedDatabase()
    t0 = time.monotonic()
    logger.info("Starting analysis for channel %s (insight %d, max %d attempts)",
                slug, insight_id, MAX_RETRY_ATTEMPTS)

    # ── Load previous analysis context (shared across retries) ──
    prev_recommendations = []
    try:
        prev_insight = db.get_latest_insight(channel_id)
        if (prev_insight
                and prev_insight.get("status") == "completed"
                and prev_insight.get("id") != insight_id):
            prev_json = prev_insight.get("insights_json", {})
            if isinstance(prev_json, str):
                try:
                    prev_json = json.loads(prev_json)
                except json.JSONDecodeError:
                    prev_json = {}
            prev_recommendations = prev_json.get("recommendations", [])
            logger.info("Loaded %d previous recommendations as context", len(prev_recommendations))
    except Exception as e:
        logger.warning("Failed to load previous insights: %s", e)

    # ── Retry loop ──────────────────────────────────────────────
    for attempt in range(MAX_RETRY_ATTEMPTS):
        if _is_cancelled(insight_id):
            logger.info("Analysis %d cancelled by user before attempt %d", insight_id, attempt + 1)
            db.fail_insight(insight_id, "Cancelado por el usuario")
            return

        db.update_insight_retry(insight_id, attempt)

        # ── Heartbeat ──────────────────────────────────────────
        heartbeat_stop = threading.Event()
        heartbeat_thread = _start_heartbeat(insight_id, heartbeat_stop)

        try:
            # ── Phase 0: Aggregate data ────────────────────────
            phase_label = f"Intento {attempt + 1}/{MAX_RETRY_ATTEMPTS}"
            db.update_insight_phase(insight_id, "exploration")
            db.update_insight_phase_detail(
                insight_id, f"{phase_label} — agregando datos del canal...",
            )
            data = _aggregate_channel_data(db, channel_id, slug)
            logger.info("Data aggregated: %d videos, %d growth points",
                         len(data.get("video_performance", [])),
                         len(data.get("channel_growth", [])))

            # Check cancel flag after data aggregation
            if _is_cancelled(insight_id):
                logger.info("Analysis %d cancelled after data aggregation", insight_id)
                db.fail_insight(insight_id, "Cancelado por el usuario")
                return

            # ── Create LLM client with thinking mode ───────────
            client = create_llm_client(
                enable_thinking=True,
                reasoning_effort="medium",
                timeout=float(TOTAL_TIMEOUT),
                max_retries=0,  # our retry logic handles this
            )
            model = LLM_MODEL_INSIGHTS
            total_tokens_in = 0
            total_tokens_out = 0

            # ── Phase 1: Exploration ──────────────────────────
            logger.info("%s: exploration for %s", phase_label, slug)
            db.update_insight_phase(insight_id, "exploration")

            exploration_messages = [
                {"role": "system", "content": _EXPLORATION_SYSTEM},
                {"role": "user", "content": _EXPLORATION_USER.replace(
                    "{data_json}", json.dumps(data, ensure_ascii=False, default=str),
                )},
            ]
            patterns = _run_phase_streaming_with_retry(
                client, model, exploration_messages,
                max_tokens=8000, temperature=0.7,
                insight_id=insight_id,
                phase_label=f"{phase_label} · Fase 1/2: Exploración",
            )
            total_tokens_in += patterns["tokens_in"]
            total_tokens_out += patterns["tokens_out"]

            db.update_insight_phase(
                insight_id, "hypothesis_recommendations",
                raw_patterns=json.dumps(patterns["content"], ensure_ascii=False),
            )
            logger.info("Phase 1 done: %d patterns found",
                         len(patterns["content"].get("patterns", [])))

            # Check cancel flag
            if _is_cancelled(insight_id):
                logger.info("Analysis %d cancelled after phase 1", insight_id)
                db.fail_insight(insight_id, "Cancelado por el usuario")
                return

            # ── Phase 2: Hypothesis + Recommendations unified ──
            logger.info("%s: hypothesis+recommendations for %s", phase_label, slug)

            try:
                config = get_channel_config(slug, force_reload=True)
                current_config = _serialize_config(config)
            except Exception:
                current_config = {}

            # Build previous context summary
            prev_context = (
                json.dumps([
                    {
                        "title": r.get("title"), "category": r.get("category"),
                        "applied": r.get("applied"), "discarded": r.get("discarded"),
                        "config_changes": r.get("config_changes"),
                        "detail": r.get("detail", "")[:300],
                    }
                    for r in prev_recommendations
                ], ensure_ascii=False, default=str)
                if prev_recommendations
                else "(no hay analisis previo — este es el primer analisis del canal)"
            )

            unified_user = _UNIFIED_USER.replace(
                "{patterns_json}",
                json.dumps(patterns["content"].get("patterns", []),
                           ensure_ascii=False, default=str),
            ).replace(
                "{data_json}",
                json.dumps(data, ensure_ascii=False, default=str),
            ).replace(
                "{config_json}",
                json.dumps(current_config, ensure_ascii=False, default=str),
            ).replace(
                "{config_keys}",
                json.dumps(_CONFIG_KEYS, ensure_ascii=False),
            ).replace(
                "{prev_recommendations_json}",
                prev_context,
            )

            unified_messages = [
                {"role": "system", "content": _UNIFIED_SYSTEM},
                {"role": "user", "content": unified_user},
            ]

            unified_result = _run_phase_streaming_with_retry(
                client, model, unified_messages,
                max_tokens=12000, temperature=0.5,
                insight_id=insight_id,
                phase_label=f"{phase_label} · Fase 2/2: Recomendaciones",
            )
            total_tokens_in += unified_result["tokens_in"]
            total_tokens_out += unified_result["tokens_out"]

            # Store raw_hypotheses for backward compatibility
            raw_hypotheses = json.dumps(
                unified_result["content"].get("hypotheses", []),
                ensure_ascii=False,
            )
            db.update_insight_phase(
                insight_id, "done",
                raw_hypotheses=raw_hypotheses,
                raw_patterns=json.dumps(patterns["content"], ensure_ascii=False),
            )
            logger.info("Phase 2 done: %d hypotheses, %d recommendations",
                         len(unified_result["content"].get("hypotheses", [])),
                         len(unified_result["content"].get("recommendations", [])))

            # ── Check cancel flag ─────────────────────────────
            if _is_cancelled(insight_id):
                logger.info("Analysis %d cancelled after phase 2", insight_id)
                db.fail_insight(insight_id, "Cancelado por el usuario")
                return

            # ── Merge: carry over applied/discarded/validated from previous ──
            if prev_recommendations:
                carried_over = [
                    r for r in prev_recommendations
                    if r.get("applied") or r.get("discarded") or r.get("validation")
                ]
                if carried_over:
                    new_recs = unified_result["content"].get("recommendations", [])
                    new_config_keys = [
                        set(nr.get("config_changes", {}).keys())
                        for nr in new_recs
                    ]
                    deduped_carried = []
                    for cr in carried_over:
                        if cr.get("applied") and cr.get("config_changes"):
                            cr_keys = set(cr["config_changes"].keys())
                            if any(cr_keys & nck for nck in new_config_keys):
                                continue
                        if cr.get("discarded"):
                            cr["from_previous"] = True
                        deduped_carried.append(cr)
                    if deduped_carried:
                        unified_result["content"]["recommendations"] = (
                            new_recs + deduped_carried
                        )
                        logger.info("Merged %d carried-over recommendations",
                                     len(deduped_carried))

            # ── Save final result ───────────────────────────────
            duration_ms = int((time.monotonic() - t0) * 1000)
            db.complete_insight(
                insight_id,
                insights_json=json.dumps(unified_result["content"],
                                         ensure_ascii=False),
                raw_patterns=json.dumps(patterns["content"], ensure_ascii=False),
                raw_hypotheses=raw_hypotheses,
                model=model,
                tokens_in=total_tokens_in,
                tokens_out=total_tokens_out,
                duration_ms=duration_ms,
            )
            db.update_insight_phase_detail(
                insight_id,
                f"Analisis completado en {duration_ms / 1000:.0f}s "
                f"({total_tokens_in + total_tokens_out} tokens, "
                f"{attempt + 1} intento{'s' if attempt > 0 else ''})",
            )
            logger.info("Analysis complete for %s: %dms, %d tokens (attempt %d)",
                         slug, duration_ms, total_tokens_in + total_tokens_out, attempt + 1)
            return  # Success — exit retry loop

        except TimeoutError as e:
            heartbeat_stop.set()
            db.update_insight_phase_detail(
                insight_id,
                f"Intento {attempt + 1}: TIMEOUT — {e}",
            )
            logger.warning("Analysis %s attempt %d/%d timed out: %s",
                            slug, attempt + 1, MAX_RETRY_ATTEMPTS, e)
            if attempt < MAX_RETRY_ATTEMPTS - 1:
                logger.info("Retrying in %ds...", RETRY_DELAY_SECONDS)
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            else:
                db.fail_insight(
                    insight_id,
                    f"Timeout tras {MAX_RETRY_ATTEMPTS} intentos: {e}"
                )

        except Exception as e:
            heartbeat_stop.set()
            db.update_insight_phase_detail(
                insight_id,
                f"Intento {attempt + 1}: ERROR — {e}",
            )
            logger.exception(
                "Analysis %s attempt %d/%d failed",
                slug, attempt + 1, MAX_RETRY_ATTEMPTS,
            )
            if attempt < MAX_RETRY_ATTEMPTS - 1:
                logger.info("Retrying in %ds...", RETRY_DELAY_SECONDS)
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            else:
                db.fail_insight(
                    insight_id,
                    f"Failed after {MAX_RETRY_ATTEMPTS} attempts: {e}"
                )

        finally:
            heartbeat_stop.set()
            # heartbeat thread will die within HEARTBEAT_INTERVAL seconds
            # (daemon thread, no need to join)

    # If we get here, all retries exhausted
    logger.error("All %d attempts exhausted for %s (insight %d)",
                 MAX_RETRY_ATTEMPTS, slug, insight_id)


# ── Validation prompt for code-change recommendations ────────────────

_VALIDATION_SYSTEM = """\
You are a senior YouTube analytics auditor. Your task is to verify whether a
previously diagnosed problem on a YouTube channel has been resolved.

You will receive:
1. The original recommendation (title, detail, expected impact, symptoms)
2. Current channel data (freshly queried, same structure as the original analysis)

Your job: compare the symptoms described in the recommendation against the
current data. Has the problem been resolved? Is there evidence of improvement?

Return a JSON verdict with:
- status: "resolved" | "partial" | "not_resolved"
- summary: 2-3 sentence explanation in Spanish of what you found
- evidence: list of 1-3 specific data points that support your verdict
- confidence: 0-100 how confident you are in this verdict

Be honest. If you cannot determine, say so and set confidence low.
"""

_VALIDATION_USER = """\
## Original recommendation (the problem that was supposed to be fixed)

Title: {title}
Category: {category}
Detail: {detail}
Expected impact: {expected_impact}

## Current channel data (after the fix was supposedly applied)

{current_data_json}

## Task

Compare the original symptoms against current data. Has the problem been resolved?
Return only valid JSON.
"""


def run_validation_check(insight_id: int, channel_id: int, slug: str,
                         rec_id: str, recommendation: dict) -> dict:
    """Focused LLM pass: check if a code-change recommendation's symptoms have resolved.

    Returns:
        { status: "resolved"|"partial"|"not_resolved",
          summary, evidence: [...], confidence: 0-100 }
    """
    db = ExtendedDatabase()
    logger.info("Running validation for rec %s on channel %s", rec_id, slug)

    try:
        data = _aggregate_channel_data(db, channel_id, slug)
        data_json = json.dumps(data, ensure_ascii=False, default=str)

        client = create_llm_client(
            enable_thinking=True,
            reasoning_effort="high",
            timeout=120,
            max_retries=0,
        )
        model = LLM_MODEL_INSIGHTS

        from config.llm_helpers import llm_json_call
        result = llm_json_call(
            client,
            model=model,
            messages=[
                {"role": "system", "content": _VALIDATION_SYSTEM},
                {"role": "user", "content": _VALIDATION_USER.format(
                    title=recommendation.get("title", ""),
                    category=recommendation.get("category", ""),
                    detail=recommendation.get("detail", ""),
                    expected_impact=recommendation.get("expected_impact", ""),
                    current_data_json=data_json,
                )},
            ],
            max_tokens=2000,
            temperature=0.3,
            max_retries=1,
        )

        validation = {
            "status": result.get("status", "not_resolved"),
            "summary": result.get("summary", ""),
            "evidence": result.get("evidence", []),
            "confidence": result.get("confidence", 0),
            "validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "validated_by": "llm",
        }

        db.update_insight_recommendation(insight_id, rec_id, {"validation": validation})
        logger.info("Validation complete for rec %s: %s (conf %d)",
                     rec_id, validation["status"], validation["confidence"])
        return validation

    except Exception as e:
        logger.exception("Validation failed for rec %s", rec_id)
        return {
            "status": "not_resolved",
            "summary": f"No se pudo validar automaticamente: {e}",
            "evidence": [],
            "confidence": 0,
            "validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "validated_by": "error",
        }


# ── Refinement prompts ───────────────────────────────────────────────

_REFINE_SYSTEM = """\
You are a YouTube channel optimization assistant. A user wants to refine
a suggested config change before applying it to their channel.

You will receive:
1. The original recommendation (title, detail, config_changes proposed)
2. The user's feedback (what they want changed)
3. The current channel config values
4. Optional conversation history

Your task: produce a REVISED version of the config_changes that incorporates
the user's feedback while still optimizing for the original goal.

Rules:
- Only use config keys from the allowed list
- NEVER remove a key from config_changes that the user didn't mention
- If the user asks for a more conservative change, adjust values but keep the direction
- If the user asks for something that contradicts the goal, explain why and offer alternatives
- Return the revised config_changes WITH an explanation of what you changed and why
"""

_REFINE_USER = """\
## Original recommendation

Title: {title}
Category: {category}
Detail: {detail}
Original config_changes: {original_changes}

## Current channel config
{current_config}

## User feedback
{user_feedback}

## Conversation history
{history}

## Allowed config keys
{config_keys}

Return ONLY valid JSON:
{{
  "explanation": "What you changed and why (Spanish, conversational, 2-4 sentences)",
  "revised_config_changes": {{"KEY": "new_value", ...}},
  "cannot_fulfill": false,
  "cannot_fulfill_reason": ""
}}
"""

_REFINE_DISCUSS_SYSTEM = """\
You are a YouTube channel strategy assistant. A user wants to discuss
a code-change suggestion — a new feature or module they're considering implementing.

The user will ask questions, raise concerns, or want to explore tradeoffs.
Your job: have a natural, helpful conversation about this suggestion.

Guidelines:
- Answer the user's questions directly and honestly
- If the suggestion could harm the channel (e.g., violate YouTube policies, spam behaviors, algorithm risks), SAY SO clearly
- If the suggestion is beneficial, explain why and suggest implementation priorities
- Propose concrete alternatives if the suggestion has issues
- Keep responses conversational (Spanish, 2-5 sentences)
- Use the current channel config for context when relevant
- If you agree the suggestion should be DISCARDED, set cannot_fulfill=true and explain why clearly
- If the suggestion could work with modifications, explain what needs to change
"""

_REFINE_DISCUSS_USER = """\
## Original code-change suggestion

Title: {title}
Category: {category}
Detail: {detail}
Implementation prompt (truncated): {opencode_prompt}

## Current channel config (for context)
{current_config}

## User message
{user_feedback}

## Conversation history
{history}

Return ONLY valid JSON:
{{
  "explanation": "Your conversational response to the user (Spanish, 2-5 sentences). Answer their question, discuss tradeoffs, suggest alternatives if needed.",
  "revised_config_changes": {{}},
  "cannot_fulfill": false,
  "cannot_fulfill_reason": ""
}}

If you believe the suggestion should be ABANDONED (e.g., it violates YouTube policies, creates spam patterns, or would harm the channel), set:
{{
  "explanation": "Clear explanation of why this is a bad idea",
  "revised_config_changes": {{}},
  "cannot_fulfill": true,
  "cannot_fulfill_reason": "Short tagline: e.g. 'Puede penalizar el canal en YouTube'"
}}
"""


def run_refine_recommendation(rec_id: str, recommendation: dict,
                              user_feedback: str,
                              current_config: dict,
                              conversation_history: list[dict] | None = None,
                              is_code_change: bool = False
                              ) -> dict:
    """Refine or discuss a recommendation based on user feedback.

    For config-change recommendations: produces revised config_changes.
    For code-change recommendations (is_code_change=True): acts as a
    discussion partner to help the user decide on the suggestion.

    Returns:
        { explanation, revised_config_changes, cannot_fulfill, cannot_fulfill_reason }
    """
    logger.info("Refining rec %s (code_change=%s): user feedback length=%d",
                 rec_id, is_code_change, len(user_feedback))

    try:
        client = create_llm_client(
            enable_thinking=True,
            reasoning_effort="medium",
            timeout=120,
            max_retries=0,
        )
        model = LLM_MODEL_INSIGHTS

        history_str = ""
        if conversation_history:
            history_str = "\n".join(
                f"[{m['role']}]: {m['content']}" for m in conversation_history
            )

        if is_code_change:
            system_prompt = _REFINE_DISCUSS_SYSTEM
            user_prompt = _REFINE_DISCUSS_USER.format(
                title=recommendation.get("title", ""),
                category=recommendation.get("category", ""),
                detail=recommendation.get("detail", ""),
                opencode_prompt=recommendation.get("opencode_prompt", "")[:1500],
                current_config=json.dumps(current_config, ensure_ascii=False, default=str),
                user_feedback=user_feedback,
                history=history_str or "(primer mensaje — no hay historial previo)",
            )
        else:
            system_prompt = _REFINE_SYSTEM
            user_prompt = _REFINE_USER.format(
                title=recommendation.get("title", ""),
                category=recommendation.get("category", ""),
                detail=recommendation.get("detail", ""),
                original_changes=json.dumps(recommendation.get("config_changes", {}), ensure_ascii=False),
                current_config=json.dumps(current_config, ensure_ascii=False, default=str),
                user_feedback=user_feedback,
                history=history_str or "(primer mensaje — no hay historial previo)",
                config_keys=json.dumps(_CONFIG_KEYS, ensure_ascii=False),
            )

        from config.llm_helpers import llm_json_call
        result = llm_json_call(
            client,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=3000,
            temperature=0.5,
            max_retries=1,
        )

        return {
            "explanation": result.get("explanation", ""),
            "revised_config_changes": result.get("revised_config_changes", {}),
            "cannot_fulfill": result.get("cannot_fulfill", False),
            "cannot_fulfill_reason": result.get("cannot_fulfill_reason", ""),
        }

    except Exception as e:
        logger.exception("Refine failed for rec %s", rec_id)
        return {
            "explanation": f"Error al refinar: {e}",
            "revised_config_changes": {},
            "cannot_fulfill": True,
            "cannot_fulfill_reason": str(e),
        }
