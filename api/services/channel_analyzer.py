"""Multi-pass LLM analysis for channel optimization.

3-phase pipeline running in background thread:
  1. EXPLORATION — find ALL patterns, anomalies, correlations in raw data
  2. HYPOTHESIS  — formulate causal explanations for each pattern
  3. RECOMMENDATIONS — convert hypotheses into actionable config changes

Progress is written to the ``channel_insights`` table after each phase.
The frontend polls ``GET /api/channels/{id}/insights/latest`` for updates.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from config.llm_client import create_llm_client
from config.llm_helpers import llm_json_call
from config.settings import LLM_MODEL_INSIGHTS
from database.db_extended import ExtendedDatabase
from config.config_bridge import get_channel_config

logger = logging.getLogger(__name__)

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
4. Pipeline errors — which phases fail most, common error messages
5. Content pillar balance vs performance
6. Growth trends (subs, views) and inflection points
7. Revenue patterns (CPM correlation with topic/duration)

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

_HYPOTHESIS_SYSTEM = """\
You are a YouTube growth strategist. You have discovered patterns in channel data.
Now formulate causal hypotheses for each pattern.

For each pattern, answer:
- WHY does this pattern exist? (causal mechanism)
- What would happen if we changed the variable?
- How confident are you in this explanation? (0-100)
- What is the counter-argument (why might this NOT work)?

Base confidence on: sample size, consistency across time, statistical significance.

Return ONLY valid JSON. No markdown."""

_HYPOTHESIS_USER = """\
<patterns>
{patterns_json}
</patterns>

<data>
{data_json}
</data>

<task>
For each pattern, formulate a causal hypothesis.
Filter out patterns with weak evidence (confidence < 30).

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
  ]
}}
</task>"""

_RECOMMENDATIONS_SYSTEM = """\
You are a channel optimization engineer. Convert validated hypotheses into
concrete, actionable recommendations with exact config key → value mappings.

RULES:
- Every recommendation must map to specific config keys from the provided list.
- Values must be valid for the key type (integer for durations, string for keywords, etc.).
- If a change requires CODE modifications (not config), mark requires_code=true
  and provide an opencode_prompt with instructions for the developer.
- Write all titles, details, and summaries in SPANISH.
- Cite specific data in every recommendation.

Return ONLY valid JSON. No markdown."""

_RECOMMENDATIONS_USER = """\
<patterns>
{patterns_json}
</patterns>

<hypotheses>
{hypotheses_json}
</hypotheses>

<current_config>
{config_json}
</current_config>

<available_config_keys>
{config_keys}
</available_config_keys>

<task>
Convert the strongest hypotheses into actionable recommendations.
For each recommendation, specify EXACTLY which config keys to change and their new values.

If a recommendation requires code changes (not just config), set requires_code=true
and provide a detailed opencode_prompt in Spanish.

Return JSON:
{{
  "analysis_summary": "2-3 paragraph executive summary in Spanish covering the channel's overall health, top 3 opportunities, and biggest risks.",
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
    """Collect all relevant data for LLM analysis in one structured dict."""
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

    # ── Video performance (JOIN videos + video_stats_history) ──
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
            LIMIT 50
        """, (channel_id,)).fetchall()
        data["video_performance"] = [dict(r) for r in rows]

    # ── Channel growth ──
    with db._connect() as conn:
        rows = conn.execute("""
            SELECT subscribers, total_views, video_count,
                   estimated_minutes_watched, fetched_at
            FROM channel_stats_history
            WHERE channel_id = ?
            ORDER BY fetched_at ASC
        """, (channel_id,)).fetchall()
        data["channel_growth"] = [dict(r) for r in rows]

    # ── Pipeline health ──
    with db._connect() as conn:
        # Errors by phase
        rows = conn.execute("""
            SELECT phase, status, COUNT(*) as count
            FROM pipeline_log
            WHERE canal = ? AND status = 'error'
            GROUP BY phase, status
        """, (slug,)).fetchall()
        data["pipeline_errors_by_phase"] = [dict(r) for r in rows]

        # Recent alerts
        rows = conn.execute("""
            SELECT alert_type, severity, title, message, created_at
            FROM pipeline_alerts
            WHERE channel_id = ? AND resolved = 0
            ORDER BY created_at DESC LIMIT 50
        """, (channel_id,)).fetchall()
        data["pipeline_alerts"] = [dict(r) for r in rows]

    # ── Content patterns (keywords, pillars) ──
    with db._connect() as conn:
        rows = conn.execute("""
            SELECT s.keywords_json, s.bloques_json, s.titulo_selected,
                   s.duracion_estimada, v.id as video_id
            FROM scripts s
            JOIN videos v ON v.script_id = s.id
            WHERE v.channel_id = ? AND s.keywords_json IS NOT NULL
            ORDER BY s.created_at DESC
            LIMIT 30
        """, (channel_id,)).fetchall()
        data["content_patterns"] = []
        for r in rows:
            row_dict = dict(r)
            try:
                row_dict["keywords"] = json.loads(r["keywords_json"] or "[]")
            except json.JSONDecodeError:
                row_dict["keywords"] = []
            data["content_patterns"].append(row_dict)

    # ── Timing data ──
    with db._connect() as conn:
        rows = conn.execute("""
            SELECT v.id, v.titulo_final, v.timing_data, v.uploaded_at,
                   v.published_at, v.publish_mode
            FROM videos v
            WHERE v.channel_id = ? AND v.timing_data IS NOT NULL
              AND v.timing_data != '{}'
            ORDER BY v.uploaded_at DESC
            LIMIT 30
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


# ── Phase runner ────────────────────────────────────────────────────

def _run_phase(client, model: str, data: dict, phase: str,
               system_prompt: str, user_template: str,
               extra_context: dict | list | None = None) -> dict:
    """Execute one LLM pass and return {content, tokens_in, tokens_out, duration_ms}."""
    t0 = time.monotonic()

    # Build user prompt with context
    if phase == "exploration":
        user_prompt = _EXPLORATION_USER.replace(
            "{data_json}", json.dumps(data, ensure_ascii=False, default=str))
    elif phase == "hypothesis":
        user_prompt = _HYPOTHESIS_USER.replace(
            "{patterns_json}", json.dumps(extra_context, ensure_ascii=False, default=str))
        user_prompt = user_prompt.replace(
            "{data_json}", json.dumps(data, ensure_ascii=False, default=str))
    elif phase == "recommendations":
        user_prompt = _RECOMMENDATIONS_USER.replace(
            "{patterns_json}", json.dumps(extra_context.get("patterns", []), ensure_ascii=False, default=str))
        user_prompt = user_prompt.replace(
            "{hypotheses_json}", json.dumps(extra_context.get("hypotheses", []), ensure_ascii=False, default=str))
        user_prompt = user_prompt.replace(
            "{config_json}", json.dumps(extra_context.get("current_config", {}), ensure_ascii=False, default=str))
        user_prompt = user_prompt.replace(
            "{config_keys}", json.dumps(extra_context.get("config_keys", _CONFIG_KEYS), ensure_ascii=False))
    else:
        user_prompt = user_template

    try:
        result = llm_json_call(
            client,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=8000,
            temperature=0.7 if phase == "exploration" else 0.5,
            max_retries=2,
        )
    except Exception as e:
        logger.error("Phase %s failed: %s", phase, e)
        raise

    # Estimate tokens (conservative heuristic — actual usage from API if available)
    content_str = json.dumps(result, ensure_ascii=False)
    tokens_in = len(user_prompt) // 4  # rough estimate
    tokens_out = len(content_str) // 4

    return {
        "content": result,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


# ── Main entry point (called from background thread) ───────────────

def run_channel_analysis_sync(insight_id: int, channel_id: int,
                              slug: str) -> None:
    """Run the full 3-pass LLM analysis. Designed for ThreadPoolExecutor.

    Updates the ``channel_insights`` row after each phase so the frontend
    can poll and show real-time progress.
    """
    db = ExtendedDatabase()
    t0 = time.monotonic()
    logger.info("Starting analysis for channel %s (insight %d)", slug, insight_id)

    try:
        # ── Phase 0: Aggregate all data ──────────────────────────
        db.update_insight_phase(insight_id, "exploration")
        data = _aggregate_channel_data(db, channel_id, slug)
        logger.info("Data aggregated: %d videos, %d growth points",
                     len(data.get("video_performance", [])),
                     len(data.get("channel_growth", [])))

        # ── Create LLM client with thinking mode ─────────────────
        client = create_llm_client(
            enable_thinking=True,
            reasoning_effort="high",
            timeout=180,
            max_retries=1,
        )
        model = LLM_MODEL_INSIGHTS
        total_tokens_in = 0
        total_tokens_out = 0

        # ── Phase 1: Exploration ────────────────────────────────
        logger.info("Phase 1/3: exploration for %s", slug)
        db.update_insight_phase(insight_id, "exploration")
        patterns = _run_phase(client, model, data, "exploration",
                              _EXPLORATION_SYSTEM, _EXPLORATION_USER)
        total_tokens_in += patterns["tokens_in"]
        total_tokens_out += patterns["tokens_out"]
        db.update_insight_phase(
            insight_id, "hypothesis",
            raw_patterns=json.dumps(patterns["content"], ensure_ascii=False),
        )
        logger.info("Phase 1 done: %d patterns found",
                     len(patterns["content"].get("patterns", [])))

        # ── Phase 2: Hypothesis ─────────────────────────────────
        logger.info("Phase 2/3: hypothesis for %s", slug)
        hypotheses = _run_phase(
            client, model, data, "hypothesis",
            _HYPOTHESIS_SYSTEM, _HYPOTHESIS_USER,
            extra_context=patterns["content"].get("patterns", []),
        )
        total_tokens_in += hypotheses["tokens_in"]
        total_tokens_out += hypotheses["tokens_out"]
        db.update_insight_phase(
            insight_id, "recommendations",
            raw_hypotheses=json.dumps(hypotheses["content"], ensure_ascii=False),
        )
        logger.info("Phase 2 done: %d hypotheses",
                     len(hypotheses["content"].get("hypotheses", [])))

        # ── Phase 3: Recommendations ────────────────────────────
        logger.info("Phase 3/3: recommendations for %s", slug)
        try:
            config = get_channel_config(slug, force_reload=True)
            current_config = _serialize_config(config)
        except Exception:
            current_config = {}
        recommendations = _run_phase(
            client, model, data, "recommendations",
            _RECOMMENDATIONS_SYSTEM, _RECOMMENDATIONS_USER,
            extra_context={
                "patterns": patterns["content"].get("patterns", []),
                "hypotheses": hypotheses["content"].get("hypotheses", []),
                "current_config": current_config,
                "config_keys": _CONFIG_KEYS,
            },
        )
        total_tokens_in += recommendations["tokens_in"]
        total_tokens_out += recommendations["tokens_out"]
        logger.info("Phase 3 done: %d recommendations",
                     len(recommendations["content"].get("recommendations", [])))

        # ── Save final result ───────────────────────────────────
        duration_ms = int((time.monotonic() - t0) * 1000)
        db.complete_insight(
            insight_id,
            insights_json=json.dumps(recommendations["content"], ensure_ascii=False),
            raw_patterns=json.dumps(patterns["content"], ensure_ascii=False),
            raw_hypotheses=json.dumps(hypotheses["content"], ensure_ascii=False),
            model=model,
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            duration_ms=duration_ms,
        )
        logger.info("Analysis complete for %s: %dms, %d tokens",
                     slug, duration_ms, total_tokens_in + total_tokens_out)

    except Exception as e:
        logger.exception("Analysis failed for %s (insight %d)", slug, insight_id)
        db.fail_insight(insight_id, str(e))
