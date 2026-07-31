"""Power Word Analyzer — Data-driven power word list generation per channel.

Runs weekly (or on-demand) to:
1. Collect all video titles + their performance stats from the DB
2. Use LLM to identify which emotional/impact words correlate with high views/CTR/retention
3. Generate an expanded TITLE_POWER_WORDS list (60-80 words) per channel
4. Store the result back to the channel's DB config for the enforcer to use

Architecture:
  - Phase 1: Data collection (SQL JOIN videos + video_stats_history)
  - Phase 2: LLM correlation analysis (which words drive performance?)
  - Phase 3: Generate expanded list + store to DB

The LLM is particularly good at this because it can identify semantic clusters
of high-impact words that go beyond simple keyword frequency analysis.
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
from config.config_bridge import get_channel_config, sync_config_to_db

logger = logging.getLogger(__name__)

# ── System prompt for the power word analysis ────────────────────────

_PW_ANALYSIS_SYSTEM = """Eres un experto en YouTube SEO y psicología viral especializado en nichos de documental/misterio en español.

Tu tarea: analizar títulos de videos junto con sus métricas de rendimiento
(visualizaciones, retención, engagement) para identificar qué PALABRAS DE ALTO
IMPACTO EMOCIONAL (power words) generan más clics y retención.

INSTRUCCIONES:
1. Analiza cada título y correlaciona sus palabras clave con el rendimiento.
2. Identifica patrones: ¿qué adjetivos, verbos o frases aparecen MÁS en los títulos exitosos?
3. Clasifica las palabras en estas categorías:
   - URGENCIA/EXCLUSIVIDAD: palabras que crean FOMO o sensación de acceso privilegiado
   - IMPACTO EMOCIONAL: palabras que activan emociones fuertes (sorpresa, miedo, asombro)
   - CURIOSIDAD/MISTERIO: palabras que abren un "curiosity gap"
   - NICHO ESPECÍFICO: palabras propias del tema del canal que conectan con la audiencia
4. Genera una lista de 60-80 power words optimizadas para ESTE canal específico.
5. Las palabras deben ser DIVERSAS — no deben ser solo sinónimos de las mismas 3-4 ideas.
6. Incluye palabras NUEVAS que no aparecen en los títulos actuales pero que, según tu
   conocimiento de psicología viral y el nicho, podrían funcionar muy bien.

IMPORTANTE: La diversidad es CLAVE. Si todos los títulos usan las mismas 10 palabras,
el canal se vuelve predecible y pierde efectividad. Genera una lista AMPLIA y VARIADA.

Responde SOLO con JSON válido, sin markdown."""

_PW_ANALYSIS_USER = """<channel_data>
Canal: {channel_name}
Nicho: {channel_niche}
Tono: {channel_tone}
</channel_data>

<current_power_words>
Esta es la lista ACTUAL de power words del canal. Tu trabajo es EXPANDIRLA,
no reemplazarla (conserva las que sigan siendo efectivas y añade nuevas):
{current_pw}
</current_power_words>

<video_performance>
Aquí tienes los títulos de los videos publicados junto con su rendimiento
(visualizaciones, tiempo de retención promedio). Úsalos para identificar
qué palabras correlacionan con mejor rendimiento:

{video_data}
</video_performance>

<task>
1. Analiza qué palabras/frases aparecen consistentemente en los títulos
   con mejor rendimiento (más views, mejor retención).
2. Identifica qué palabras NO están siendo usadas pero PODRÍAN funcionar
   en este nicho basándote en psicología viral.
3. Genera una lista EXPANDIDA de 60-80 power words para este canal,
   organizada por categorías.
4. NO elimines palabras de la lista actual que sigan siendo relevantes.
   AÑADE nuevas palabras para diversificar.
5. Asegúrate de que la lista sea lo suficientemente VARIADA para que
   los títulos no parezcan repetitivos.

Return JSON:
{{
  "analysis_summary": "2-3 frases en español resumiendo los hallazgos principales",
  "top_performing_words": ["palabra1", "palabra2", ...],
  "underperforming_words": ["palabra1", ...],
  "new_words_by_category": {{
    "urgencia_exclusividad": ["nueva1", "nueva2", ...],
    "impacto_emocional": ["nueva1", "nueva2", ...],
    "curiosidad_misterio": ["nueva1", "nueva2", ...],
    "nicho_especifico": ["nueva1", "nueva2", ...]
  }},
  "final_power_words": ["palabra1", "palabra2", ..., "palabra80"],
  "key_insight": "El hallazgo más importante en una frase"
}}
</task>"""


# ── Data collection ──────────────────────────────────────────────────

def _collect_title_performance_data(db: ExtendedDatabase,
                                     channel_id: int) -> list[dict]:
    """Collect video titles with their latest performance stats.

    Joins videos + latest video_stats_history snapshot.
    Returns list of {title, views, retention, published_at} sorted by views desc.
    """
    with db._connect() as conn:
        rows = conn.execute("""
            SELECT
                v.titulo_final,
                vsh.views,
                vsh.average_view_duration,
                vsh.likes,
                vsh.comments,
                v.uploaded_at
            FROM videos v
            LEFT JOIN (
                SELECT video_id, views, likes, comments,
                       average_view_duration,
                       ROW_NUMBER() OVER (PARTITION BY video_id ORDER BY fetched_at DESC) as rn
                FROM video_stats_history
            ) vsh ON v.id = vsh.video_id AND vsh.rn = 1
            WHERE v.channel_id = ?
              AND v.titulo_final IS NOT NULL
              AND v.titulo_final != ''
              AND v.status NOT IN ('draft', 'error', 'error_deleted')
            ORDER BY vsh.views DESC
            LIMIT 50
        """, (channel_id,)).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        title = d.get("titulo_final", "")
        views = d.get("views") or 0
        retention = d.get("average_view_duration") or 0
        likes = d.get("likes") or 0
        comments = d.get("comments") or 0
        if title:
            results.append({
                "title": title,
                "views": views,
                "retention_sec": round(retention, 1),
                "likes": likes,
                "comments": comments,
            })
    return results


def _build_video_data_text(videos: list[dict]) -> str:
    """Format video performance data as a readable text block for the LLM prompt."""
    if not videos:
        return "(no hay datos de rendimiento de videos disponibles)"

    lines = []
    for i, v in enumerate(videos[:50], 1):
        views_str = f"{v['views']:,}".replace(",", ".")
        lines.append(
            f"{i}. \"{v['title']}\"\n"
            f"   Views: {views_str} | Retención: {v['retention_sec']}s | "
            f"Likes: {v['likes']} | Comments: {v['comments']}"
        )
    return "\n".join(lines)


# ── Main entry point ─────────────────────────────────────────────────

def analyze_channel_power_words(channel_slug: str) -> dict | None:
    """Run the full power word analysis for a single channel.

    Args:
        channel_slug: Channel slug (e.g. 'canal2').

    Returns:
        dict with {analysis_summary, top_performing_words, final_power_words, ...}
        or None on failure.
    """
    db = ExtendedDatabase()
    t0 = time.monotonic()
    logger.info("Power word analysis START for %s", channel_slug)

    try:
        # ── Load channel config ──────────────────────────────────
        config = get_channel_config(channel_slug, force_reload=True)
        channel_name = getattr(config, "CANAL_DISPLAY_NAME", channel_slug)
        channel_tone = getattr(config, "CANAL_TONE", "misterioso")
        channel_niche = getattr(config, "NICHE_KEYWORDS_ENG", [])
        if isinstance(channel_niche, list):
            channel_niche = ", ".join(channel_niche[:5])
        current_pw = getattr(config, "TITLE_POWER_WORDS", [])

        # ── Find channel DB ID ───────────────────────────────────
        ch = db.get_channel_by_slug(channel_slug)
        if not ch:
            logger.error("Channel not found in DB: %s", channel_slug)
            return None
        channel_id = ch["id"]

        # ── Phase 1: Collect data ────────────────────────────────
        video_data = _collect_title_performance_data(db, channel_id)
        logger.info("Collected %d video titles with performance data for %s",
                     len(video_data), channel_slug)

        if not video_data:
            logger.warning("No video performance data for %s — using config-only",
                          channel_slug)

        video_text = _build_video_data_text(video_data)
        current_pw_text = json.dumps(current_pw, ensure_ascii=False, indent=2)

        # ── Phase 2: LLM analysis ────────────────────────────────
        client = create_llm_client(
            enable_thinking=True,
            reasoning_effort="medium",
            timeout=180,
            max_retries=2,
        )

        user_prompt = _PW_ANALYSIS_USER.format(
            channel_name=channel_name,
            channel_niche=str(channel_niche),
            channel_tone=channel_tone,
            current_pw=current_pw_text,
            video_data=video_text,
        )

        result = llm_json_call(
            client,
            model=LLM_MODEL_INSIGHTS,
            messages=[
                {"role": "system", "content": _PW_ANALYSIS_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=8000,
            temperature=0.7,
            max_retries=2,
        )

        final_list = result.get("final_power_words", [])
        if not isinstance(final_list, list) or len(final_list) < 10:
            logger.error("LLM returned insufficient power words for %s: %d words",
                        channel_slug, len(final_list) if isinstance(final_list, list) else 0)
            return None

        # ── Phase 3: Store to DB ─────────────────────────────────
        # Update the channel config with the new power words
        try:
            # Write directly to channels.config_json
            with db._connect() as conn:
                # Get current config_json
                row = conn.execute(
                    "SELECT config_json FROM channels WHERE id = ?",
                    (channel_id,),
                ).fetchone()
                current_config_json = {}
                if row and row["config_json"]:
                    try:
                        current_config_json = json.loads(row["config_json"])
                    except json.JSONDecodeError:
                        current_config_json = {}

                # Update with new power words
                current_config_json["TITLE_POWER_WORDS"] = final_list

                conn.execute(
                    "UPDATE channels SET config_json = ? WHERE id = ?",
                    (json.dumps(current_config_json, ensure_ascii=False), channel_id),
                )
                conn.commit()
                logger.info(
                    "Updated TITLE_POWER_WORDS in DB for %s: %d words (was %d)",
                    channel_slug, len(final_list), len(current_pw),
                )
        except Exception as e:
            logger.error("Failed to store power words to DB for %s: %s",
                        channel_slug, e)
            # Don't fail — the analysis itself succeeded, just couldn't persist

        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "Power word analysis COMPLETE for %s: %dms, %d final words",
            channel_slug, duration_ms, len(final_list),
        )

        return {
            "channel_slug": channel_slug,
            "channel_name": channel_name,
            "analysis_summary": result.get("analysis_summary", ""),
            "top_performing_words": result.get("top_performing_words", []),
            "underperforming_words": result.get("underperforming_words", []),
            "new_words_by_category": result.get("new_words_by_category", {}),
            "final_power_words": final_list,
            "key_insight": result.get("key_insight", ""),
            "previous_count": len(current_pw),
            "new_count": len(final_list),
            "duration_ms": duration_ms,
        }

    except Exception as e:
        logger.exception("Power word analysis FAILED for %s: %s", channel_slug, e)
        return None


def analyze_all_channels() -> list[dict]:
    """Run power word analysis for all active channels.

    Returns list of result dicts (one per channel).
    """
    db = ExtendedDatabase()
    results = []

    try:
        channels = db.get_channels(active_only=True)
        if not channels:
            logger.warning("No active channels found for power word analysis")
            return []

        for ch in channels:
            slug = ch.get("slug")
            if not slug:
                continue

            try:
                result = analyze_channel_power_words(slug)
                if result:
                    results.append(result)
            except Exception as e:
                logger.error("Power word analysis failed for %s: %s", slug, e)
                results.append({"channel_slug": slug, "error": str(e)})

    except Exception as e:
        logger.exception("analyze_all_channels failed: %s", e)

    return results
