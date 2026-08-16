#!/usr/bin/env python3
"""Test Fases 1-3: video ~8 min con canal3 (civilizaciones olvidadas).

Ejecuta el pipeline completo con la config de canal3 (narrador, velocidad,
temática) pero activando visual_bible_enabled para probar el nuevo algoritmo
de coherencia visual + AI providers + chain de 5 tiers.

NO sube a YouTube. NO planifica. El video se guarda en output/videos/.
"""

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
        logging.FileHandler(LOGS_DIR / "test_phase3.log", encoding="utf-8"),
    ],
)
for noisy in ["urllib3", "googleapiclient", "google.auth", "apscheduler",
              "PIL", "moviepy", "openai", "httpx", "httpcore"]:
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("test_phase3")

# ── Banner ───────────────────────────────────────────────────
logger.info("=" * 70)
logger.info("  TEST FASES 1-3: Algoritmo completo")
logger.info("  Canal: canal3 (Civilizaciones Olvidadas)")
logger.info("  Providers IA: Pollinations.ai → Local SD 1.5")
logger.info("  Stock video: 20-30% escenas (dinámico)")
logger.info("  Biblia visual: LLM activado")
logger.info("  Upload: DESACTIVADO")
logger.info("=" * 70)

# ── 1. Config ────────────────────────────────────────────────
from config.config_bridge import get_channel_config

cfg = get_channel_config("canal3")

# Activar biblia visual + AI primary (ya vienen por defecto, forzamos explícito)
ms = dict(cfg.MEDIA_STRATEGY) if hasattr(cfg, "MEDIA_STRATEGY") else {}
ms["visual_bible_enabled"] = True
ms["ai_image_primary"] = True
ms["video_scene_pct_min"] = 20
ms["video_scene_pct_max"] = 30
ms["ai_image_fallback"] = True    # Pollo AI como último recurso
cfg.MEDIA_STRATEGY = ms

# Test mode con ~8 min (canal3 PROD es ~14 min, reducimos)
cfg.TEST_MODE = True
cfg.TEST_SCRIPT_WORDS_MIN = 1400
cfg.TEST_SCRIPT_WORDS_MAX = 2000
cfg.TEST_VIDEO_DURATION_TARGET = 8
cfg.TEST_SCRIPT_SCENES_MIN = 12
cfg.TEST_SCRIPT_SCENES_MAX = 20
cfg.TEST_SCRIPT_BLOCKS_MIN = 6
cfg.TEST_SCRIPT_BLOCKS_MAX = 12
# Mantener settings de narrador de canal3 (VOICE_RATE=0.80, kokoro em_santa)
# sin modificar — se heredan de la config

logger.info("Config: %s | words=%d-%d | scenes=%d-%d | video_target=%d min",
            cfg.CANAL_DISPLAY_NAME,
            cfg.TEST_SCRIPT_WORDS_MIN, cfg.TEST_SCRIPT_WORDS_MAX,
            cfg.TEST_SCRIPT_SCENES_MIN, cfg.TEST_SCRIPT_SCENES_MAX,
            cfg.TEST_VIDEO_DURATION_TARGET)
logger.info("MEDIA_STRATEGY: ai_primary=%s | visual_bible=%s | video=%d-%d%%",
            ms.get("ai_image_primary"), ms.get("visual_bible_enabled"),
            ms.get("video_scene_pct_min"), ms.get("video_scene_pct_max"))

# ── 2. DB init ───────────────────────────────────────────────
from database.db import Database, init_db
from database.db_extended import migrate_v2

db = Database()
init_db()
migrate_v2()

# ── 3. Orchestrator ──────────────────────────────────────────
from orchestrator import PipelineOrchestrator

orch = PipelineOrchestrator(canal="canal3")

# Inyectar la config modificada (con biblia visual activada)
# El orchestrator carga su propia config via config_bridge; la sobrescribimos
orch.config = cfg
# Forzar que media_fetcher re-lea la config al acceder a su propiedad lazy
if orch._media_fetcher is not None:
    orch._media_fetcher._config = cfg
    orch._media_fetcher._media_strategy = ms

# ── 4. Pipeline completo (sin upload) ────────────────────────
full_start = time.time()

try:
    success = orch.run_full_pipeline(skip_upload=True)
except Exception as e:
    logger.exception("Pipeline crashed: %s", e)
    success = False

elapsed = time.time() - full_start
minutes = int(elapsed / 60)
seconds = int(elapsed % 60)

# ── 5. Report ────────────────────────────────────────────────
logger.info("=" * 70)
if success:
    logger.info("  ✅ PIPELINE COMPLETADO en %dm %ds", minutes, seconds)
    logger.info("  Video guardado en output/videos/")
    logger.info("  Revisar logs para métricas de biblia visual y AI providers")
else:
    logger.error("  ❌ PIPELINE FALLÓ tras %dm %ds", minutes, seconds)

logger.info("  Log completo: logs/test_phase3.log")
logger.info("=" * 70)

sys.exit(0 if success else 1)
