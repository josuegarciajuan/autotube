#!/usr/bin/env python3
"""Test de coherencia visual — imágenes IA + búsqueda + estilo unificado + protagonista.

Usa la configuración COMPLETA de canal3 (Civilizaciones Olvidadas) para la
generación, pero registra el vídeo bajo el canal ``test`` (Pruebas de
algoritmo), que está inactivo y sin cuenta de Google → imposible que suba a
YouTube.

Al terminar escribe un informe de verificación escena por escena en
``output/test_report_*.txt`` con: narración, search_query_en, concepto de la
biblia visual, asset final (ruta + source) y prompt IA completo.

Uso::

    # Con protagonista (por defecto) — sin scrape, reutiliza/genera desde topic
    python3 scripts/test_visual_coherence.py

    # Con un tema concreto
    python3 scripts/test_visual_coherence.py --topic "Tutankamón"

    # Scraping fresco de canal3 en vez de tema forzado
    python3 scripts/test_visual_coherence.py --skip-topic

    # Incluir metadatos + thumbnail (más lento)
    python3 scripts/test_visual_coherence.py --metadata
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import LOGS_DIR, LOG_FORMAT

# ── Logging ──────────────────────────────────────────────────
LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "test_visual_coherence.log", encoding="utf-8"),
    ],
)
for noisy in ["urllib3", "googleapiclient", "google.auth", "apscheduler",
              "PIL", "moviepy", "openai", "httpx", "httpcore"]:
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("test_visual_coherence")

# ── Tema con protagonista por defecto ────────────────────────
DEFAULT_TOPIC = "Percy Fawcett y la ciudad perdida de Z"
DEFAULT_TOPIC_TEXT = (
    "El explorador británico Percy Fawcett desapareció en 1925 en la selva "
    "amazónica mientras buscaba una antigua civilización perdida que él llamaba "
    "la Ciudad de Z. Obsesionado durante décadas, realizó múltiples expediciones "
    "desafiando enfermedades, ataques de tribus y la hostilidad de la jungla. "
    "Su desaparición generó una de las mayores leyendas de la exploración: "
    "¿encontró Fawcett la ciudad perdida o murió en el intento? Hoy los "
    "arqueólogos siguen debatiendo si la Ciudad de Z existió de verdad o fue "
    "solo una obsesión de un explorador visionario."
)


def build_synthetic_content(topic: str, topic_text: str) -> dict:
    """Construir un content_item sintético con un tema concreto."""
    return {
        "id": None,
        "title": topic,
        "text": topic_text or DEFAULT_TOPIC_TEXT,
        "source": "test-topic",
        "url": "",
        "subreddit": "test",
        "score": 0,
    }


def write_report(orch, script: dict, video_data: dict, topic: str) -> Path:
    """Escribir el informe de verificación escena por escena."""
    import json

    scene_ranges = getattr(orch, "_last_scene_ranges", None) or []
    media_assets = getattr(orch, "_last_media_assets", None) or []
    visual_bible = getattr(orch.media_fetcher, "_visual_bible", None) or {}
    ai_prompt_log = getattr(orch.media_fetcher, "_ai_prompt_log", None) or {}

    ts = time.strftime("%Y%m%d_%H%M%S")
    report_path = Path(f"output/test_report_{ts}.txt")

    lines: list[str] = []
    add = lines.append

    add("=" * 78)
    add("INFORME DE VERIFICACIÓN — COHERENCIA VISUAL")
    add("=" * 78)
    add(f"Tema: {topic}")
    add(f"Script ID: {script.get('id')} | Palabras: {len(script.get('guion', '').split())}")
    add(f"Escenas: {len(scene_ranges)} | Assets: {len(media_assets)}")
    add("")

    # ── Biblia visual ──────────────────────────────────────
    vu = visual_bible.get("visual_universe", "")
    arc = visual_bible.get("visual_tone_arc", "")
    add("─ BIBLIA VISUAL ───────────────────────────────────────────")
    add(f"  Universo visual: {vu}")
    add(f"  Arco de tono: {arc}")
    entity = visual_bible.get("central_entity", {}) or {}
    etype = entity.get("type", "none")
    if etype != "none":
        add(f"  ENTIDAD CENTRAL (protagonista): type={etype}")
        add(f"    Descripción: {entity.get('master_description', '')[:300]}")
        appears = entity.get("appears_in_scenes", [])
        add(f"    Aparece en escenas: {appears}")
        add(f"    Variaciones: {entity.get('variation_by_scene', {})}")
    else:
        add("  ENTIDAD CENTRAL: none (sin protagonista recurrente)")
    add(f"  Elementos recurrentes: {visual_bible.get('recurring_elements', [])}")
    add("")

    # ── Escenas ────────────────────────────────────────────
    add("─ ESCENAS ────────────────────────────────────────────────")
    scene_map = visual_bible.get("scene_visual_map", [])
    protagonist_scenes = set(entity.get("appears_in_scenes", []))

    for i, scene in enumerate(scene_ranges):
        tipo = scene.get("tipo", "?")
        dur = scene.get("duration", 0)
        narracion = (scene.get("texto", "") or "").strip()
        query = scene.get("search_query_en", "") or ""
        asset = media_assets[i] if i < len(media_assets) else {}
        src = asset.get("source", "?") or "?"
        path = asset.get("path", "") or ""
        atype = asset.get("type", "?")

        proto_mark = " ★PROTAGONISTA" if i in protagonist_scenes else ""

        add(f"Escena {i:02d} [{tipo}] dur={dur:.1f}s{proto_mark}")
        add(f"  Narración: {narracion[:160]}")
        if query:
            add(f"  search_query_en: {query}")

        # Concepto de la biblia visual
        vb_concept = ""
        if i < len(scene_map) and isinstance(scene_map[i], dict):
            vb_concept = scene_map[i].get("visual_concept", "")
            shot = scene_map[i].get("shot_type", "")
            if vb_concept:
                add(f"  Concepto (biblia): [{shot}] {vb_concept}")

        add(f"  Asset: {atype} ← {src}")
        if path:
            add(f"    {path}")

        # Prompt IA completo (si la escena usó imagen IA)
        prompt = ai_prompt_log.get(i, "")
        if prompt:
            add(f"  Prompt IA: {prompt}")
        add("")

    # ── Resumen de fuentes ─────────────────────────────────
    from collections import Counter
    sources = Counter(a.get("source", "?") for a in media_assets if a)
    add("─ RESUMEN DE FUENTES ─────────────────────────────────────")
    for src, cnt in sources.most_common():
        add(f"  {src}: {cnt}")
    add("")

    # ── Estadísticas de coherencia ──────────────────────────
    n_ai = sum(1 for a in media_assets if a and str(a.get("source", "")).startswith("ai_"))
    n_video = sum(1 for a in media_assets if a and a.get("type") == "video")
    n_image = sum(1 for a in media_assets if a and a.get("type") == "image")
    add("─ MÉTRICAS ───────────────────────────────────────────────")
    add(f"  Imágenes IA: {n_ai}")
    add(f"  Vídeos stock: {n_video}")
    add(f"  Imágenes (stock+IA): {n_image}")
    add(f"  Escenas con protagonista: {len(protagonist_scenes)}")
    add(f"  Vídeo: {video_data.get('video_path', '?')}")
    add("=" * 78)

    text = "\n".join(lines)
    report_path.write_text(text, encoding="utf-8")
    logger.info("Informe escrito: %s", report_path)
    print("\n" + text)
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Test de coherencia visual")
    parser.add_argument("--topic", type=str, default=DEFAULT_TOPIC,
                        help="Tema con protagonista para forzar (default: Percy Fawcett)")
    parser.add_argument("--skip-topic", action="store_true",
                        help="No forzar tema: usar contenido scrapeado de canal3")
    parser.add_argument("--skip-scrape", action="store_true",
                        help="No scrapear (solo relevante con --skip-topic)")
    parser.add_argument("--metadata", action="store_true",
                        help="Generar metadatos + thumbnail (más lento)")
    args = parser.parse_args()

    logger.info("=" * 70)
    logger.info("  TEST COHERENCIA VISUAL — canal3 (Civilizaciones Olvidadas)")
    logger.info("  Biblia visual: ON | Imágenes IA primarias: ON")
    logger.info("  Upload: DESACTIVADO (registro en canal 'test' inactivo)")
    logger.info("=" * 70)

    # ── 1. Config ────────────────────────────────────────────
    from config.config_bridge import get_channel_config

    cfg = get_channel_config("canal3")

    # Forzar flags del nuevo algoritmo (ai_image_primary ya es True por defecto)
    ms = dict(cfg.MEDIA_STRATEGY) if hasattr(cfg, "MEDIA_STRATEGY") else {}
    ms["visual_bible_enabled"] = True
    ms["ai_image_primary"] = True
    ms["video_scene_pct_min"] = 20
    ms["video_scene_pct_max"] = 30
    ms["ai_image_fallback"] = True
    cfg.MEDIA_STRATEGY = ms

    # Test mode con duración moderada (suficientes escenas para ver protagonista)
    cfg.TEST_MODE = True
    cfg.TEST_SCRIPT_WORDS_MIN = 600
    cfg.TEST_SCRIPT_WORDS_MAX = 1000
    cfg.TEST_VIDEO_DURATION_TARGET = 4
    cfg.TEST_SCRIPT_SCENES_MIN = 6
    cfg.TEST_SCRIPT_SCENES_MAX = 12
    cfg.TEST_SCRIPT_BLOCKS_MIN = 4
    cfg.TEST_SCRIPT_BLOCKS_MAX = 7

    # ── 2. DB init ───────────────────────────────────────────
    from database.db import Database, init_db
    from database.db_extended import migrate_v2, ExtendedDatabase

    db = Database()
    ext_db = ExtendedDatabase()
    init_db()
    migrate_v2()

    # ── 3. Orquestador ───────────────────────────────────────
    from orchestrator import PipelineOrchestrator

    orch = PipelineOrchestrator(canal="canal3")
    orch.config = cfg  # asegurar que el orquestador use la config modificada

    # ── 4. Contenido (tema forzado o scrapeado) ─────────────
    if args.skip_topic:
        if not args.skip_scrape:
            logger.info("📥 Scraping contenido de canal3...")
            orch.phase_scrape()
        items = db.get_unused_content(canal="canal3", limit=10)
        if not items:
            logger.error("Sin contenido disponible. Ejecuta sin --skip-topic o scrapea primero.")
            return 1
        content_item = items[0]
        topic = content_item.get("title", "Civilizaciones Olvidadas")
    else:
        content_item = build_synthetic_content(args.topic, DEFAULT_TOPIC_TEXT)
        topic = args.topic
        logger.info("🎯 Tema forzado con protagonista: %s", topic)

    # ── 5. Tema visual (necesario antes de generar el guion) ─
    orch._extract_and_set_theme(
        content_item.get("text", ""), content_item.get("title", "")
    )

    # ── 6. Guion ─────────────────────────────────────────────
    logger.info("📝 Generando guion...")
    script = orch.script_gen.generate(content_item)
    if not script:
        logger.error("❌ Generación de guion falló")
        return 1
    logger.info("   Guion generado (ID %s, %d palabras)",
                script.get("id"), len(script.get("guion", "").split()))

    # ── 7. TTS ───────────────────────────────────────────────
    logger.info("🔊 Generando TTS...")
    audio_data = orch.phase_tts(script)
    if not audio_data:
        logger.error("❌ TTS falló")
        return 1

    # ── 8. Media (aquí corre el algoritmo a verificar) ──────
    logger.info("🖼️  Buscando media (imágenes IA + stock + biblia visual)...")
    media_assets = orch.phase_media(script, audio_data)
    if not media_assets:
        logger.error("❌ Fetch de media falló")
        return 1

    # ── 9. Ensamblado de vídeo ───────────────────────────────
    logger.info("🎬 Ensamblando vídeo...")
    video_data = orch.phase_video(script, audio_data, media_assets)
    if not video_data:
        logger.error("❌ Ensamblado de vídeo falló")
        return 1

    # ── 10. Metadatos (opcional) ─────────────────────────────
    metadata = None
    if args.metadata:
        logger.info("📊 Generando metadatos...")
        metadata = orch.phase_metadata(script, video_data)

    # ── 11. Registrar bajo el canal 'test' (inactivo, sin YT) ─
    video_id = video_data.get("video_id")
    title = (metadata or {}).get("selected_title") or script.get("titulo_selected") or topic
    if video_id:
        try:
            ext_db.update_video(
                video_id,
                canal="test",
                channel_id=6,
                titulo_final=title,
                privacy_status="unlisted",
            )
            logger.info("📦 Vídeo %s registrado en canal 'test' (id=6, inactivo — sin upload)", video_id)
        except Exception as exc:
            logger.warning("No se pudo re-registrar bajo 'test': %s", exc)

    # ── 12. Informe ──────────────────────────────────────────
    report_path = write_report(orch, script, video_data, topic)
    logger.info("=" * 70)
    logger.info("  ✅ TEST COMPLETADO")
    logger.info("  Vídeo: %s", video_data.get("video_path"))
    logger.info("  Informe: %s", report_path)
    logger.info("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
