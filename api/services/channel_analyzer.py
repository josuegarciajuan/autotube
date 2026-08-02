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

import datetime as dt
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
You are a successful YouTuber and marketing growth specialist with 15+ years
of experience scaling channels from 0 to millions of subscribers. You've personally
grown multiple channels to 100K+ subscribers by mastering audience psychology,
content positioning, and YouTube's recommendation algorithm. You think like a
top-tier content strategist — not a generic data analyst.

Your job is to find ALL statistically significant patterns, anomalies, and correlations
in raw channel data — from a purely marketing and audience-growth perspective.
Think step by step. Cite specific data points (exact numbers, dates).

CRITICAL — DATA FRESHNESS AND RECENCY:
  Videos tagged [RECENT] (<7 days old) carry the MOST decision-making weight.
  Videos tagged [MODERATE] (8-30 days) carry moderate weight.
  Videos tagged [OLD] (30+ days) should be SIGNIFICANTLY discounted — they may
  reflect outdated channel configs, fixed pipeline bugs, or audience behaviors
  that no longer apply. The Autotube system is actively evolving: what the
  channel was doing 2 months ago may be irrelevant today.
  PRIORITIZE patterns visible in [RECENT] videos. If a pattern only appears in
  old videos, note it as "historical / potentially resolved".

CRITICAL — BRAINSTORMING BEFORE EACH FINDING:
  1. Generate 3-5 possible interpretations of the data pattern.
  2. For each, estimate expected impact (high/medium/low) and confidence.
  3. Present ONLY the strongest, most actionable interpretation.

CRITICAL — ORIGINALITY:
  Do NOT report generic observations that apply to any YouTube channel.
  Every finding must be SPECIFIC to THIS channel's unique data. If you find
  yourself writing something like "videos with better titles get more views",
  STOP — that's too generic. Instead, say "the 3 videos with the word [X] in
  titles averaged +65% more views than the channel average of Y."

Categories to explore exhaustively:
1. Video duration vs retention / watchtime / revenue — especially in [RECENT] videos
2. Publish hour and day vs 24h and 7d views — with time-of-day granularity
3. Keywords/topics vs CTR, views, and retention — which specific topics over/underperform
4. Title power words: which emotional/impact words in titles correlate with higher views/CTR?
   Identify SPECIFIC words that appear in top-performing titles and are MISSING from low performers.
5. Audience behavior patterns: seasonal trends, binge-watching signals, drop-off points in retention curves.
6. Content pillar balance vs performance: does the channel over-index on one topic while underperforming?
7. Niche positioning and competitive differentiation: what unique angle could increase CTR and loyalty?
8. Growth trends (subs, views) and inflection points: what triggered past growth spikes or drops?
9. Revenue patterns (CPM correlation with topic/duration)

DO NOT look for bugs, pipeline errors, or technical failures. Focus exclusively on
marketing, content strategy, audience growth, and revenue optimization.

For each finding: state the EXACT magnitude (e.g. "3x", "+40%"), cite the SPECIFIC data point
with the date or recency tag, and classify the category.

Return ONLY valid JSON. No markdown, no explanation outside the JSON."""

_EXPLORATION_USER = """\
<channel_data>
{data_json}
</channel_data>

<task>
Analyze this channel from a MARKETING PERSPECTIVE. Find EVERY meaningful pattern,
anomaly, and correlation related to audience growth, content performance, and revenue.
Focus on actionable insights, not technical issues.

For each pattern: before writing the finding, silently brainstorm 3-5 interpretations
and pick the strongest one.

Return JSON:
{{
  "patterns": [
    {{
      "name": "short descriptive name",
      "category": "duracion|hora_publicacion|keywords|contenido",
      "finding": "detailed data-backed marketing insight (Spanish, 1-2 sentences)",
      "data_points": ["specific metric: value", "specific metric: value"],
      "magnitude": "3x|+40%|-15%|50% higher CTR",
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
You are a successful YouTuber and marketing growth specialist with 15+ years
of experience growing channels from zero to millions of subscribers. You think like
a top-tier YouTube content strategist who has personally scaled multiple channels to
100K+ subs. You know that generic advice kills channels — what works is SPECIFIC,
data-backed, channel-unique recommendations.

You have discovered patterns in channel data. Now you must do TWO things in ONE response:
  1. Formulate causal hypotheses for each pattern (why it exists, confidence, counter-argument)
  2. Convert the strongest hypotheses into concrete, actionable recommendations with exact config key → value mappings.

CRITICAL — ORIGINALITY RULE:
  Do NOT suggest generic advice that could apply to any channel (e.g., "optimiza los titulos",
  "publica a horas punta", "usa palabras clave en la descripcion"). Every recommendation
  must be SPECIFIC to THIS channel's unique data and audience behavior.
  Before writing each recommendation, do the ORIGINALITY TEST:
  "Would this exact same advice apply equally well to 50 other YouTube channels?"
  If YES → dig deeper into the data to find something UNIQUE to THIS channel.
  Each recommendation MUST cite exact data points from THIS channel's metrics
  (not general YouTube best practices). Example: instead of "mejora los titulos",
  write "los 3 videos con la palabra 'misterio' en el titulo tienen +72% views
  que el promedio del canal — duplica el uso de esta palabra."

CRITICAL — HONESTY RULE:
  If since the last analysis there have been MINIMAL changes in channel performance
  (few new videos, similar metrics, no significant new trends), state this CLEARLY
  in the analysis_summary. Example phrasing: "Desde el ultimo analisis (hace X dias)
  no ha habido cambios significativos en el rendimiento del canal. Solo se publicaron
  N videos nuevos y las metricas se mantienen estables. Las recomendaciones anteriores
  siguen siendo validas y no hay nueva evidencia que justifique cambios adicionales."
  It is BETTER to give 2 authentic, specific, data-backed recommendations than 8
  generic, low-confidence filler recommendations. Quality > quantity.

CRITICAL — RECENCY WEIGHTING:
  Give 3x MORE weight to patterns observed in the last 2 weeks (videos tagged [RECENT])
  than to patterns from 30+ days ago (videos tagged [OLD]). The channel's configuration
  and pipeline are continuously improved; historical data may contain artifacts from
  resolved bugs or outdated settings that no longer apply.

CRITICAL — BRAINSTORMING BEFORE EACH RECOMMENDATION:
  Before writing any recommendation, silently brainstorm:
  a) 3-5 different possible approaches to capitalize on the pattern.
  b) For each: estimate expected impact (views/CTR/revenue %) and implementation difficulty.
  c) Pick the single strongest, most cost-effective approach.
  d) Present ONLY that approach as the recommendation.

RULES:
- Every recommendation must map to specific config keys from the provided list.
- Values must be valid for the key type (integer for durations, string for keywords, etc.).
- If a change requires CODE modifications (not config), mark requires_code=true
  and provide an opencode_prompt with instructions for the developer.
- Write all titles, details, and summaries in SPANISH.
- Cite specific data from this channel in every recommendation.
- Filter out patterns with weak evidence (confidence < 30).
- All recommendations should BUILD UPON and ENRICH any previous analysis, not replace it.
- Focus on MARKETING improvements: titles, thumbnails, keywords, publishing strategy,
  audience growth, content differentiation, retention hooks, monetization optimization.
- DO NOT recommend bug fixes, pipeline error patches, or technical infrastructure changes.
- DO NOT repeat recommendations that exist in <previous_analyses> unless you have STRONG
  new evidence that changes the analysis. If a prior recommendation is still valid,
  mention it in the summary instead of repeating it as a new recommendation.

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

<since_last_analysis>
{since_last_analysis}
</since_last_analysis>

<previous_analyses>
The following are ALL recommendations from prior analyses of this channel,
grouped by analysis date. This is your DEDUP REFERENCE:
- If you find yourself about to recommend something semantically similar
  (same category + similar config change + similar reasoning) to ANY prior
  recommendation, DO NOT include it as a new recommendation. Instead, note in
  analysis_summary: "La recomendacion sobre [X] del analisis del [fecha]
  sigue siendo valida y no requiere actualizacion."
- Applied (applied=true) recommendations have already been applied to the
  channel config. DO NOT repeat them unless you have STRONG new data showing
  the change had a negative effect.
- Discarded (discarded=true) recommendations were intentionally rejected by
  the user. DO NOT repeat them unless the data has changed dramatically.
- Your new recommendations should BUILD UPON and ENRICH prior analysis,
  focusing on what is genuinely NEW or has CHANGED.
{previous_analyses_json}
</previous_analyses>

<cross_channel_context>
The following are RECENT recommendations from OTHER channels managed by this
system. Review them before generating new recommendations:
- If a recommendation here is ALSO relevant to THIS channel, you may include it
  BUT you must add "rationale_for_reuse" explaining why it applies here.
- Do NOT simply copy cross-channel recommendations without justification
  specific to this channel's data.
{cross_channel_json}
</cross_channel_context>

<task>
Step 1: For each pattern, formulate a causal hypothesis. Filter out patterns with confidence < 30.
         Before writing each hypothesis, brainstorm 3+ possible causal explanations and pick the strongest.
Step 2: For each strong hypothesis, brainstorm 3-5 possible marketing actions. Rank by impact vs feasibility.
         Convert only the BEST approach into an actionable recommendation with exact config changes.
Step 3: Before finalizing recommendations, review <previous_analyses> and <cross_channel_context>.
         Remove any recommendation that duplicates an existing one. If you reuse a cross-channel
         recommendation, add "rationale_for_reuse" to explain why it fits this channel.

Return JSON:
{{
  "hypotheses": [
    {{
      "pattern_name": "<match pattern name exactly>",
      "category": "duracion|hora_publicacion|keywords|contenido",
      "explanation": "causal mechanism (Spanish, 2-3 sentences)",
      "proposed_change": "what specific marketing change to make",
      "expected_outcome": "quantified prediction (e.g. '+30% views in 24h')",
      "confidence": 85,
      "counter_argument": "why this might NOT work (Spanish, 1 sentence)",
      "evidence_strength": "fuerte|moderado|debil"
    }}
  ],
  "analysis_summary": "2-3 paragraph executive summary in Spanish covering the channel's overall health, top 3 growth opportunities, and biggest competitive risks. Explicitly mention what has CHANGED since the last analysis — or state honestly if nothing significant has changed. If prior recommendations are still valid, mention them instead of repeating.",
  "health_score": 72,
  "key_metrics": [
    {{ "label": "Views/dia", "value": "2,100", "sparkline": [10,12,11,15,13,14,15], "delta": "+8%", "delta_positive": true }}
  ],
  "recommendations": [
    {{
      "id": "<uuid-v4>",
      "category": "duracion|hora_publicacion|keywords|contenido",
      "title": "short actionable title (Spanish, max 8 words)",
      "detail": "data-backed marketing insight (Spanish, 2-4 sentences)",
      "confidence": 85,
      "expected_impact": "alta|media|baja",
      "config_changes": {{ "VIDEO_AVERAGE_DURATION_MIN": 11 }},
      "data_cited": {{ "metric_name": "exact value from data" }},
      "requires_code": false,
      "opencode_prompt": null,
      "rationale_brief": "1-sentence summary of why (Spanish)",
      "rationale_for_reuse": null
    }}
  ]
}}

IMPORTANT for key_metrics: provide 4-6 marketing metrics. Prefer these when data is available:
  - Views/dia (daily view average)
  - Retention promedio (average viewer retention %)
  - CTR promedio (average click-through rate %)
  - Suscripciones por video (average new subs per published video)
  - Ingresos estimados (estimated revenue, if monetized)
  - Subs 30d (subscriber growth in last 30 days)
Each sparkline must be an array of 7 integers representing the last 7 data points.
</task>"""




# ── Data aggregation ────────────────────────────────────────────────

def _aggregate_channel_data(db: ExtendedDatabase, channel_id: int,
                            slug: str) -> dict[str, Any]:
    """Collect all relevant data for LLM analysis in one structured dict.

    v21.1: Adds recency tags, data freshness summary, and since_last_analysis context.
      - Top 20 videos by recency (not 50)
      - Last 15 channel growth points (not all)
      - Last 20 content patterns (not 30)
      - Recency tags: [RECENT] <7d, [MODERATE] 8-30d, [OLD] >30d
      - Data freshness summary: count per bucket, date range
      - Since last analysis: delta metrics from previous completed analysis
    """
    data: dict[str, Any] = {}
    now = dt.datetime.utcnow()

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
        video_list = [dict(r) for r in rows]

        # ── Add recency tags to each video ──
        recency_counts = {"RECENT": 0, "MODERATE": 0, "OLD": 0}
        for vid in video_list:
            uploaded_str = vid.get("uploaded_at")
            recency_tag = "OLD"
            if uploaded_str:
                try:
                    if isinstance(uploaded_str, str):
                        uploaded_dt = dt.datetime.strptime(
                            uploaded_str, "%Y-%m-%d %H:%M:%S"
                        )
                    else:
                        uploaded_dt = uploaded_str
                    age_days = (now - uploaded_dt).days
                    if age_days <= 7:
                        recency_tag = "RECENT"
                    elif age_days <= 30:
                        recency_tag = "MODERATE"
                    else:
                        recency_tag = "OLD"
                    vid["days_since_upload"] = age_days
                except (ValueError, TypeError):
                    vid["days_since_upload"] = None
            else:
                vid["days_since_upload"] = None
            vid["recency"] = recency_tag
            recency_counts[recency_tag] += 1
        data["video_performance"] = video_list

    # ── Data freshness summary ──
    data["data_freshness_summary"] = {
        "analysis_timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "videos_by_recency": recency_counts,
        "oldest_video_date": (
            video_list[-1].get("uploaded_at") if video_list else None
        ),
        "newest_video_date": (
            video_list[0].get("uploaded_at") if video_list else None
        ),
    }

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

    # ── Since last analysis context ──
    since_last = _compute_since_last_analysis(db, channel_id, now)
    data["since_last_analysis"] = since_last

    return data


def _compute_since_last_analysis(db: ExtendedDatabase, channel_id: int,
                                  now: dt.datetime) -> dict[str, Any]:
    """Compute what has changed since the last completed analysis.

    Returns dict with: previous_analysis_date, days_since, new_videos_since,
    config_changes_since (count of applied recommendations).
    """
    result: dict[str, Any] = {
        "previous_analysis_date": None,
        "days_since": None,
        "new_videos_since": 0,
        "applied_recommendations_since": 0,
        "note": "Este es el primer analisis de este canal.",
    }

    try:
        prev = db.get_latest_completed_insight(channel_id)
        if not prev or not prev.get("generated_at"):
            return result

        prev_date_str = prev["generated_at"]
        try:
            if isinstance(prev_date_str, str):
                prev_date = dt.datetime.strptime(
                    prev_date_str, "%Y-%m-%d %H:%M:%S"
                )
            else:
                prev_date = prev_date_str
        except (ValueError, TypeError):
            return result

        days_since = (now - prev_date).days
        result["previous_analysis_date"] = prev_date_str
        result["days_since"] = max(days_since, 0)

        # Count new videos uploaded since last analysis
        with db._connect() as conn:
            count = conn.execute(
                """SELECT COUNT(*) FROM videos
                   WHERE channel_id = ? AND uploaded_at > ?""",
                (channel_id, prev_date_str),
            ).fetchone()
            result["new_videos_since"] = count[0] if count else 0

        # Count applied recommendations from the previous analysis
        prev_json = prev.get("insights_json", {})
        if isinstance(prev_json, str):
            try:
                prev_json = json.loads(prev_json)
            except json.JSONDecodeError:
                prev_json = {}
        recs = prev_json.get("recommendations", [])
        applied_count = sum(
            1 for r in recs
            if r.get("applied") and not r.get("discarded")
        )
        result["applied_recommendations_since"] = applied_count

        if days_since <= 1:
            result["note"] = (
                f"El analisis anterior fue hace {days_since} dia(s). "
                f"Se publicaron {result['new_videos_since']} video(s) nuevos "
                f"y se aplicaron {applied_count} recomendacion(es). "
                f"Si los cambios son minimos, se honesto en el analysis_summary."
            )
        elif days_since <= 7:
            result["note"] = (
                f"El analisis anterior fue hace {days_since} dias. "
                f"Se publicaron {result['new_videos_since']} video(s) nuevos "
                f"y se aplicaron {applied_count} recomendacion(es). "
                f"Enfocate en lo que ha CAMBIADO desde entonces."
            )
        else:
            result["note"] = (
                f"El analisis anterior fue hace {days_since} dias. "
                f"Se publicaron {result['new_videos_since']} video(s) nuevos "
                f"y se aplicaron {applied_count} recomendacion(es). "
                f"Hay suficiente tiempo transcurrido para detectar tendencias nuevas."
            )

    except Exception as e:
        logger.warning("Failed to compute since_last_analysis: %s", e)

    return result


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


# ── Semantic deduplication ──────────────────────────────────────────

# Stop words for Spanish titles (excluded from Jaccard similarity)
_DEDUP_STOP_WORDS = {
    "de", "la", "el", "en", "los", "las", "un", "una", "del", "al",
    "por", "para", "con", "sin", "que", "es", "y", "o", "a", "se",
    "su", "lo", "como", "mas", "pero", "sus", "le", "ya", "este",
    "entre", "todo", "esta", "ser", "son", "fue", "era", "han",
}


def _tokenize_title(title: str) -> set[str]:
    """Extract meaningful word tokens from a title for similarity comparison."""
    if not title:
        return set()
    tokens = set()
    for word in title.lower().split():
        word = word.strip(",.!?¿¡;:()[]\"'")
        if len(word) >= 3 and word not in _DEDUP_STOP_WORDS:
            tokens.add(word)
    return tokens


def _jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    """Compute Jaccard similarity between two sets of tokens."""
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union) if union else 0.0


def _config_key_overlap(rec_a: dict, rec_b: dict) -> float:
    """Compute overlap of config_changes keys between two recommendations."""
    keys_a = set(rec_a.get("config_changes", {}).keys())
    keys_b = set(rec_b.get("config_changes", {}).keys())
    if not keys_a or not keys_b:
        return 0.0
    intersection = keys_a & keys_b
    union = keys_a | keys_b
    return len(intersection) / len(union) if union else 0.0


def _compute_rec_similarity(rec_a: dict, rec_b: dict) -> float:
    """Compute overall similarity score between two recommendations.

    Combination of: title word overlap (Jaccard) + config key overlap + category match.
    Returns 0.0 to 1.0.
    """
    title_a = rec_a.get("title", "")
    title_b = rec_b.get("title", "")
    title_sim = _jaccard_similarity(
        _tokenize_title(title_a), _tokenize_title(title_b)
    )
    config_sim = _config_key_overlap(rec_a, rec_b)

    # Category match bonus
    cat_a = rec_a.get("category", "")
    cat_b = rec_b.get("category", "")
    cat_match = 1.0 if cat_a and cat_b and cat_a == cat_b else 0.0

    # Weighted combination: title overlap is most important, config keys second,
    # category match adds a tiebreaker
    return 0.5 * title_sim + 0.3 * config_sim + 0.2 * cat_match


def _dedup_recommendations(
    new_recs: list[dict],
    prev_all_recs: list[dict],
    cross_channel_recs: list[dict],
    similarity_threshold: float = 0.55,
) -> tuple[list[dict], dict]:
    """Deduplicate new recommendations against prior and cross-channel history.

    Returns (filtered_recs, similarity_report).
    similarity_report: {rec_id: {duplicate_of: ..., cross_channel_similar: ..., cross_channel_name: ...}}
    """
    report: dict[str, dict] = {}

    for new_rec in new_recs:
        rec_id = new_rec.get("id", "")

        # ── Check against prior recommendations (intra-channel) ──
        for prev_rec in prev_all_recs:
            sim = _compute_rec_similarity(new_rec, prev_rec)
            if sim >= similarity_threshold:
                prev_id = prev_rec.get("id", "unknown")
                logger.info(
                    "Dedup: new rec '%s' is similar (%.2f) to prior rec '%s' (%s)",
                    new_rec.get("title", "")[:60], sim,
                    prev_rec.get("title", "")[:60], prev_id,
                )
                report[rec_id] = {
                    "duplicate_of": prev_id,
                    "similarity_score": round(sim, 2),
                    "prior_title": prev_rec.get("title", "")[:100],
                    "prior_analysis_date": prev_rec.get("analysis_date", ""),
                }
                break  # Stop at first match

        if rec_id in report:
            continue  # Skip cross-channel check if already intra-channel duplicate

        # ── Check against cross-channel recommendations ──
        for cc_rec in cross_channel_recs:
            sim = _compute_rec_similarity(new_rec, cc_rec)
            if sim >= similarity_threshold:
                cc_name = cc_rec.get("channel_name", "otro canal")
                logger.info(
                    "Cross-dedup: new rec '%s' is similar (%.2f) to rec from '%s'",
                    new_rec.get("title", "")[:60], sim, cc_name,
                )
                report[rec_id] = {
                    "cross_channel_similar": True,
                    "cross_channel_name": cc_name,
                    "similarity_score": round(sim, 2),
                    "cross_channel_rec_id": cc_rec.get("id", ""),
                    "cross_channel_title": cc_rec.get("title", "")[:100],
                }
                # Note: cross-channel matches are NOT removed, just flagged
                # (user decided: keep if relevant, just add the badge)
                break

    # ── Filter out strict intra-channel duplicates ──
    filtered = []
    for new_rec in new_recs:
        rec_id = new_rec.get("id", "")
        if rec_id in report and "duplicate_of" in report[rec_id]:
            # Intra-channel duplicate — remove from main list
            new_rec["hidden_as_duplicate"] = True
            new_rec["duplicate_of"] = report[rec_id]["duplicate_of"]
            new_rec["similarity_score"] = report[rec_id]["similarity_score"]
            filtered.append(new_rec)  # Keep but mark as hidden
        else:
            # Cross-channel similar — include but flag
            if rec_id in report:
                new_rec["cross_channel_similar"] = True
                new_rec["cross_channel_name"] = report[rec_id].get(
                    "cross_channel_name", ""
                )
                new_rec["similarity_to_previous"] = report[rec_id].get(
                    "similarity_score", 0
                )
            filtered.append(new_rec)

    return filtered, report


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

    # ── Load ALL previous analysis context (shared across retries) ──
    # v21.1: load all completed insights (not just last one) for full dedup history
    all_prev_recommendations: list[dict] = []
    prev_insights_list: list[dict] = []
    try:
        prev_insights_list = db.get_channel_insights(channel_id, limit=10)
        for prev_insight in prev_insights_list:
            if (prev_insight.get("status") == "completed"
                    and prev_insight.get("id") != insight_id):
                prev_json = prev_insight.get("insights_json", {})
                if isinstance(prev_json, str):
                    try:
                        prev_json = json.loads(prev_json)
                    except json.JSONDecodeError:
                        prev_json = {}
                recs = prev_json.get("recommendations", [])
                for r in recs:
                    r["analysis_date"] = prev_insight.get("generated_at", "")
                    r["analysis_id"] = prev_insight.get("id")
                all_prev_recommendations.extend(recs)
        logger.info("Loaded %d previous recommendations from %d completed analyses",
                     len(all_prev_recommendations),
                     sum(1 for p in prev_insights_list if p.get("status") == "completed"))
    except Exception as e:
        logger.warning("Failed to load previous insights: %s", e)

    # ── Load cross-channel recommendations ──
    cross_channel_recs: list[dict] = []
    try:
        all_channels = db.get_channels()
        for ch in all_channels:
            ch_id = ch.get("id")
            if ch_id == channel_id:
                continue
            ch_insights = db.get_channel_insights(ch_id, limit=3)
            for ci in ch_insights:
                if ci.get("status") != "completed":
                    continue
                ci_json = ci.get("insights_json", {})
                if isinstance(ci_json, str):
                    try:
                        ci_json = json.loads(ci_json)
                    except json.JSONDecodeError:
                        ci_json = {}
                for rec in ci_json.get("recommendations", []):
                    if rec.get("applied") or rec.get("discarded"):
                        continue  # Skip applied/discarded from other channels
                    rec["channel_name"] = ch.get("name", f"canal {ch_id}")
                    rec["channel_slug"] = ch.get("slug", "")
                    cross_channel_recs.append(rec)
        logger.info("Loaded %d cross-channel recommendations from other channels",
                     len(cross_channel_recs))
    except Exception as e:
        logger.warning("Failed to load cross-channel recommendations: %s", e)

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

            # v20.2: strip pipeline error data — LLM analysis is marketing-only,
            # not bug-hunting. Pipeline error logs accumulate historical noise
            # and distract the LLM from actionable audience-growth insights.
            data.pop("pipeline_errors_by_phase", None)
            data.pop("pipeline_alerts", None)

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

            # Build previous analyses context (all history, grouped by analysis)
            prev_analyses_groups: list[dict] = []
            seen_analysis_ids = set()
            for rec in all_prev_recommendations:
                aid = rec.get("analysis_id")
                if aid and aid not in seen_analysis_ids:
                    seen_analysis_ids.add(aid)
                    analysis_recs = [
                        {
                            "title": r.get("title"),
                            "category": r.get("category"),
                            "applied": r.get("applied"),
                            "discarded": r.get("discarded"),
                            "config_changes": r.get("config_changes"),
                            "detail": r.get("detail", "")[:200],
                        }
                        for r in all_prev_recommendations
                        if r.get("analysis_id") == aid
                    ]
                    prev_analyses_groups.append({
                        "analysis_date": rec.get("analysis_date", ""),
                        "analysis_id": aid,
                        "recommendations": analysis_recs,
                    })

            previous_analyses_json = (
                json.dumps(prev_analyses_groups, ensure_ascii=False, default=str)
                if prev_analyses_groups
                else "(no hay analisis previo — este es el primer analisis del canal)"
            )

            # Build cross-channel context (dedup reference)
            cross_channel_json = (
                json.dumps([
                    {
                        "title": r.get("title"),
                        "category": r.get("category"),
                        "config_changes": r.get("config_changes"),
                        "channel_name": r.get("channel_name", ""),
                        "detail": r.get("detail", "")[:200],
                    }
                    for r in cross_channel_recs[:20]  # Cap at 20 for prompt size
                ], ensure_ascii=False, default=str)
                if cross_channel_recs
                else "(no hay recomendaciones de otros canales disponibles)"
            )

            # Build since_last_analysis context from aggregated data
            since_la = data.get("since_last_analysis", {})
            since_la_str = (
                f"Ultimo analisis: {since_la.get('previous_analysis_date', 'N/A')}\n"
                f"Dias transcurridos: {since_la.get('days_since', 'N/A')}\n"
                f"Videos nuevos publicados desde entonces: {since_la.get('new_videos_since', 0)}\n"
                f"Recomendaciones aplicadas desde entonces: {since_la.get('applied_recommendations_since', 0)}\n"
                f"Nota: {since_la.get('note', '')}"
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
                "{since_last_analysis}",
                since_la_str,
            ).replace(
                "{previous_analyses_json}",
                previous_analyses_json,
            ).replace(
                "{cross_channel_json}",
                cross_channel_json,
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
            if all_prev_recommendations:
                carried_over = [
                    r for r in all_prev_recommendations
                    if r.get("applied") or r.get("discarded") or r.get("validation")
                ]
                # Deduplicate carried_over by title (keep latest analysis_date)
                seen_titles = {}
                unique_carried = []
                for cr in carried_over:
                    key = cr.get("title", "")[:80]
                    if key in seen_titles:
                        existing = seen_titles[key]
                        if (cr.get("analysis_date", "") >
                                existing.get("analysis_date", "")):
                            unique_carried.remove(existing)
                            unique_carried.append(cr)
                            seen_titles[key] = cr
                    else:
                        unique_carried.append(cr)
                        seen_titles[key] = cr
                carried_over = unique_carried

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

            # ── v21.1: Post-generation semantic dedup ──
            new_recs = unified_result["content"].get("recommendations", [])
            clean_prev_recs = [
                {k: v for k, v in r.items()
                 if k not in ("analysis_date", "analysis_id", "channel_name", "channel_slug")}
                for r in all_prev_recommendations
            ]
            clean_cross = [
                {k: v for k, v in r.items()
                 if k not in ("channel_name", "channel_slug")}
                for r in cross_channel_recs
            ]
            deduped_recs, dedup_report = _dedup_recommendations(
                new_recs, clean_prev_recs, clean_cross,
            )
            # Tag cross-channel recs in the report
            for rec_id, info in dedup_report.items():
                if info.get("cross_channel_similar"):
                    for rec in deduped_recs:
                        if rec.get("id") == rec_id:
                            rec["cross_channel_similar"] = True
                            rec["cross_channel_name"] = info.get("cross_channel_name", "")
                            break

            unified_result["content"]["recommendations"] = deduped_recs
            unified_result["content"]["_dedup_report"] = dedup_report
            logger.info("Dedup: %d recs before, %d after (removed %d intra-channel duplicates)",
                         len(new_recs), len(deduped_recs),
                         len(new_recs) - len(deduped_recs))

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
