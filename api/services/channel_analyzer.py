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
Convert the strongest hypotheses into actionable recommendations.
For each recommendation, specify EXACTLY which config keys to change and their new values.

If a recommendation requires code changes (not just config), set requires_code=true
and provide a detailed opencode_prompt in Spanish.

Return JSON:
{{
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
        prev_recs = extra_context.get("previous_recommendations", [])
        user_prompt = user_prompt.replace(
            "{prev_recommendations_json}",
            json.dumps(prev_recs, ensure_ascii=False, default=str) if prev_recs
            else "(no hay analisis previo — este es el primer analisis del canal)")
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

        # ── Load previous analysis for context and enrichment ────
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
                # Include previous context in aggregated data for exploration phase
                data["previous_analysis"] = {
                    "summary": prev_json.get("analysis_summary", ""),
                    "health_score": prev_json.get("health_score"),
                    "recommendations_count": len(prev_recommendations),
                    "applied_count": sum(1 for r in prev_recommendations if r.get("applied")),
                    "discarded_count": sum(1 for r in prev_recommendations if r.get("discarded")),
                }
                logger.info("Loaded %d previous recommendations from insight %d as context",
                           len(prev_recommendations), prev_insight["id"])
        except Exception as e:
            logger.warning("Failed to load previous insights: %s", e)

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
                "previous_recommendations": [
                    {
                        "title": r.get("title"), "category": r.get("category"),
                        "applied": r.get("applied"), "discarded": r.get("discarded"),
                        "config_changes": r.get("config_changes"),
                        "detail": r.get("detail", "")[:300],
                    }
                    for r in prev_recommendations
                ],
            },
        )
        total_tokens_in += recommendations["tokens_in"]
        total_tokens_out += recommendations["tokens_out"]
        logger.info("Phase 3 done: %d recommendations",
                     len(recommendations["content"].get("recommendations", [])))

        # ── Merge: carry over applied/discarded/validated recs from previous analysis ──
        if prev_recommendations:
            carried_over = [
                r for r in prev_recommendations
                if r.get("applied") or r.get("discarded") or r.get("validation")
            ]
            if carried_over:
                new_recs = recommendations["content"].get("recommendations", [])
                # Dedup: skip previously applied recs whose config_changes are
                # already reflected in the current config (no need to show them again)
                deduped_carried = []
                new_config_changes_sets = []
                for nr in new_recs:
                    ncc = nr.get("config_changes", {})
                    if ncc:
                        new_config_changes_sets.append(set(ncc.keys()))
                for cr in carried_over:
                    if cr.get("applied") and cr.get("config_changes"):
                        cr_keys = set(cr["config_changes"].keys())
                        # Check if any new recommendation covers the same config keys
                        if any(cr_keys & nccs for nccs in new_config_changes_sets):
                            continue  # Already addressed by a new rec, skip
                    if cr.get("discarded"):
                        # Mark as from previous analysis for the frontend
                        cr["from_previous"] = True
                    deduped_carried.append(cr)
                if deduped_carried:
                    recommendations["content"]["recommendations"] = (
                        new_recs + deduped_carried
                    )
                    logger.info("Merged %d carried-over recommendations (was %d before dedup)",
                               len(deduped_carried), len(carried_over))

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
        # Aggregate current data (same sources as analysis)
        data = _aggregate_channel_data(db, channel_id, slug)
        data_json = json.dumps(data, ensure_ascii=False, default=str)

        client = create_llm_client(
            enable_thinking=True,
            reasoning_effort="high",
            timeout=120,
            max_retries=1,
        )
        model = LLM_MODEL_INSIGHTS

        user_prompt = _VALIDATION_USER.format(
            title=recommendation.get("title", ""),
            category=recommendation.get("category", ""),
            detail=recommendation.get("detail", ""),
            expected_impact=recommendation.get("expected_impact", ""),
            current_data_json=data_json,
        )

        result = llm_json_call(
            client,
            model=model,
            messages=[
                {"role": "system", "content": _VALIDATION_SYSTEM},
                {"role": "user", "content": user_prompt},
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

        # Persist to DB
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


# ── Refinement prompt for config-change recommendations ─────────────

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
    logger.info("Refining rec %s (code_change=%s): user feedback length=%d", rec_id, is_code_change, len(user_feedback))

    try:
        client = create_llm_client(
            enable_thinking=True,
            reasoning_effort="medium",
            timeout=120,
            max_retries=1,
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
