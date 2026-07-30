"""Test fixtures shared across all test modules.

Provides mock database, mock channel configs (canal2, canal3, canal4),
mock LLM responses and a helper to build the test environment.
"""

import sys
sys.path.insert(0, "/root/autotube")

import json
import pytest
from unittest.mock import MagicMock


# ═══════════════════════════════════════════════════════════════
# Mock Channel Configs (matching real channel settings)
# ═══════════════════════════════════════════════════════════════

class _BaseMockConfig:
    """Minimal channel config for tests."""
    CANAL_NAME = "canal2"
    TEST_MODE = False
    CANAL_TONE = "Test tone"
    CANAL_NARRATIVE_STYLE = "documental de asombro"
    CANAL_STYLE_DESCRIPTION = ""
    TARGET_AUDIENCE = "público LATAM adulto curioso"
    CANAL_OUTRO_TAGLINE = "outro"
    SCRIPT_HOOK_RULE = "hook rule"
    SCRIPT_STRUCTURE = []
    SCRIPT_EMOTIONAL_ARC = {}
    RETENTION_ANCHORS = {}
    VIRALITY_TRIGGERS = []
    TITLE_FORMULAS = []
    TITLE_POWER_WORDS = []
    SEO_PRIMARY_KEYWORD = "test"
    SEO_SECONDARY_KEYWORDS = []
    TEST_SCRIPT_WORDS_MIN = 200
    TEST_SCRIPT_WORDS_MAX = 600
    TEST_SCRIPT_BLOCKS_MIN = 3
    TEST_SCRIPT_BLOCKS_MAX = 6
    TEST_VIDEO_DURATION_TARGET = 2
    TTS_ENGINE = "edgetts"
    VOICE_SSML = {}


class MockConfigCanal2(_BaseMockConfig):
    """canal2 (Sincronías): edge-tts with rate="-10%"."""
    CANAL_NAME = "canal2"
    VIDEO_AVERAGE_DURATION_MIN = 14
    VIDEO_DURATION_DISCREPANCY_MIN = 3
    TTS_STRATEGY = {"rate_base": "-10%"}


class MockConfigCanal3(_BaseMockConfig):
    """canal3 (Civilizaciones Olvidadas): edge-tts with rate="-8%"."""
    CANAL_NAME = "canal3"
    VIDEO_AVERAGE_DURATION_MIN = 12
    VIDEO_DURATION_DISCREPANCY_MIN = 3
    TTS_STRATEGY = {"rate_base": "-8%"}

class MockConfigKokoro(_BaseMockConfig):
    """Kokoro float speed."""
    CANAL_NAME = "canal2"
    VIDEO_AVERAGE_DURATION_MIN = 14
    VIDEO_DURATION_DISCREPANCY_MIN = 3
    TTS_STRATEGY = {"rate_base": 0.85}


# ═══════════════════════════════════════════════════════════════
# Mock Database
# ═══════════════════════════════════════════════════════════════

class MockDB:
    """Minimal DB stub for pipeline testing.

    Tracks inserted scripts, pipeline logs, and video records.
    """

    def __init__(self):
        self.last_script_id = 100
        self.inserted_scripts: list[dict] = []
        self.pipeline_logs: list[tuple] = []
        self.job_statuses: dict[int, str] = {}
        self.videos: dict[int, dict] = {}
        self.raw_content: list[dict] = []

    # ── Scripts ────────────────────────────────────────────────
    def insert_script(self, **kwargs):
        self.last_script_id += 1
        kwargs["id"] = self.last_script_id
        self.inserted_scripts.append(kwargs)
        return self.last_script_id

    def get_script(self, sid):
        for s in self.inserted_scripts:
            if s["id"] == sid:
                return s
        return None

    def mark_script_used(self, sid):
        pass

    def mark_content_used(self, content_id):
        pass

    # ── Jobs ───────────────────────────────────────────────────
    def update_job(self, job_id, **kwargs):
        if "status" in kwargs:
            self.job_statuses[job_id] = kwargs["status"]

    def get_job(self, job_id):
        return {"id": job_id, "status": self.job_statuses.get(job_id, "unknown")}

    def create_job(self, channel_id, action, video_id=None):
        return 1

    # ── Videos ─────────────────────────────────────────────────
    def update_video(self, video_id, **kwargs):
        if video_id not in self.videos:
            self.videos[video_id] = {}
        self.videos[video_id].update(kwargs)

    def get_video(self, video_id):
        return self.videos.get(video_id, {})

    def get_channel(self, channel_id):
        return {"slug": "canal2", "name": "Test Channel", "id": channel_id}

    # ── Content ────────────────────────────────────────────────
    def get_unused_content(self, canal="canal2", limit=10, strategy="best_first"):
        return self.raw_content[:limit]

    def get_unused_count(self, canal="canal2"):
        return len(self.raw_content)

    # ── Pipeline log ───────────────────────────────────────────
    def log_pipeline(self, canal, phase, status, message, **kwargs):
        self.pipeline_logs.append((canal, phase, status, message))

    # ── Generation attempts (v22) ────────────────────────────
    def log_generation_attempt(self, **kwargs):
        pass  # no-op for tests — tests mock LLM calls directly

    def get_generation_failure_patterns(self, canal=None, days=7):
        return {
            "total_attempts": 0,
            "total_failures": 0,
            "by_model": [],
            "by_error_type": [],
            "by_phase": [],
            "emergency_activations": 0,
            "recent_attempts": [],
        }

    def _connect(self):
        return self


# ═══════════════════════════════════════════════════════════════
# Mock LLM responses
# ═══════════════════════════════════════════════════════════════

def make_mock_llm_response(bloques_textos: list[str]) -> dict:
    """Build a mock OpenAI response with block texts.

    Returns the dict that would be parsed from the LLM's JSON output
    during the enrichment phase.
    """
    bloques = []
    for text in bloques_textos:
        bloques.append({
            "tipo": "desarrollo",
            "emocion": "curiosidad",
            "texto": text,
            "escena_descripcion": "test scene",
            "search_query_en": "test query cinematic 16:9",
            "media_tipo": "imagen",
            "media_duracion": 6,
        })

    full_guion = "\n\n".join(b["texto"] for b in bloques)

    return {
        "titulo_options": ["Título de prueba"],
        "descripcion_seo": "Descripción SEO de prueba para el video.",
        "guion": full_guion,
        "parrafos": [{"idea_central": "Test", "bloques": bloques}],
        "cta": {"tipo": "cta", "texto": "Suscríbete."},
        "escenas": [],
        "emociones": [],
        "keywords": ["test"],
        "hashtags": ["#Test"],
        "duracion_estimada": 99.9,  # LLM hallucination — should be overridden
        "chapters": [],
        "fuentes_citadas": [],
        "bloques": bloques,
    }


def make_mock_openai_response(data: dict):
    """Wrap a response dict mimicking openai.ChatCompletion.

    Returns a real object (not MagicMock) so .strip() and .content work properly.
    """
    from types import SimpleNamespace
    msg = SimpleNamespace(content=json.dumps(data))
    choice = SimpleNamespace(message=msg)
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=200)
    return SimpleNamespace(choices=[choice], usage=usage)


def make_mock_content_batch_response(textos: list[str]) -> MagicMock:
    """Wrap a content-only batch response: {"bloques": [{"texto": ...}]}."""
    bloques = [{"texto": t} for t in textos]
    return make_mock_openai_response({"bloques": bloques})


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def mock_db():
    return MockDB()


@pytest.fixture
def config_canal2():
    return MockConfigCanal2


@pytest.fixture
def config_canal3():
    return MockConfigCanal3



@pytest.fixture
def content_item():
    return {
        "id": 1,
        "title": "Historia de prueba sobre casualidades inexplicables",
        "source": "reddit",
        "subreddit": "test",
        "score": 100,
        "text": "Source text for testing the pipeline.",
        "_palabras_objetivo": 2772,
    }
