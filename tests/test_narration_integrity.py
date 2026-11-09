"""Narration integrity regression tests (nov 2026).

Covers the four root causes found in production:
- word-chopping guiones into mid-sentence fragments,
- edge-tts timestamps drifting behind the real audio,
- short stock clips collapsing the render timeline,
- long-form scene ranges dropping inter-block pauses,
- LLM ``segmentos`` omitting whole sentences.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── Script trimming ──────────────────────────────────────────────

def test_sentence_trim_never_cuts_mid_sentence():
    from pipeline.shorts_tts import trim_blocks_sentences

    bloques = [
        {"tipo": "hook", "texto": "Primera frase. Segunda frase."},
        {"tipo": "desarrollo1", "texto": "Uno. Dos. Tres. Cuatro."},
        {"tipo": "desarrollo2", "texto": "Cinco. Seis. Siete. Ocho."},
        {"tipo": "climax", "texto": "Nueve. Diez."},
        {"tipo": "cierre", "texto": "Cierre final."},
    ]
    result, report = trim_blocks_sentences(
        bloques, max_words=8, allow_word_fallback=False,
    )
    assert report["initial_words"] > 8
    for block in result:
        txt = block["texto"].strip()
        assert txt, "no block may be left empty"
        # Every narrated fragment ends at a sentence boundary.
        assert txt.endswith((".", "!", "?", "…")), txt
    assert report["needs_regeneration"] is False


def test_sentence_trim_reports_regeneration_when_impossible():
    from pipeline.shorts_tts import trim_blocks_sentences

    bloques = [
        {"tipo": "hook", "texto": "Una sola frase larga con muchas palabras aqui."},
        {"tipo": "desarrollo1", "texto": "Otra sola frase larga con muchas palabras."},
    ]
    result, report = trim_blocks_sentences(
        bloques, max_words=3, allow_word_fallback=False,
    )
    assert report["needs_regeneration"] is True
    assert all(b["texto"].strip() for b in result)


def test_prepare_short_script_uses_complete_sentences():
    from pipeline.shorts_tts import prepare_short_script

    long = " ".join(["palabra"] * 12) + "."
    script = {
        "tema": "t",
        "titulo": "x",
        "hook_text": "hook",
        "bloques": [
            {"tipo": "hook", "texto": "Frase uno. Frase dos."},
            {"tipo": "desarrollo1", "texto": f"Uno. Dos. {long}"},
            {"tipo": "desarrollo2", "texto": f"Cinco. Seis. {long}"},
            {"tipo": "climax", "texto": f"Nueve. Diez. {long}"},
            {"tipo": "cierre", "texto": "Cierre final."},
        ],
    }
    cfg = {"SHORTS_MAX_DURATION_SEC": 58.0}
    out, errors, report = prepare_short_script(script, cfg, max_words=45)
    assert not [e for e in errors if "too long" in e.lower()], errors
    for b in out["bloques"]:
        assert b["texto"].rstrip().endswith((".", "!", "?", "…"))


# ── segmentos coverage ───────────────────────────────────────────

def test_sanitize_segmentos_rejects_omitted_sentence():
    from pipeline.script_generator import ScriptGenerator

    block = "Uno. Dos. Tres."
    assert ScriptGenerator._sanitize_segmentos(block, [
        {"texto": "Uno. Dos."},
    ]) == []
    ok = ScriptGenerator._sanitize_segmentos(block, [
        {"texto": "Uno. Dos."},
        {"texto": "Tres."},
    ])
    assert len(ok) == 2


def test_split_block_segments_falls_back_when_segments_omit_text():
    from config.voice_resolver import split_block_segments

    cfg = {
        "EXPRESSIVE_NARRATION": True,
        "PROSODY_PROFILES": {"neutro": {"rate": "+0%", "pitch": "+0Hz"}},
        "TONO_DEFAULT": "neutro",
        "TTS_STRATEGY": {},
    }
    bloque = {
        "tipo": "desarrollo",
        "texto": "Uno. Dos. Tres.",
        "segmentos": [{"texto": "Uno. Dos.", "tono": "neutro"}],
    }
    segments = split_block_segments(bloque, cfg, expressive=True)
    assert len(segments) == 1
    assert "Tres." in segments[0]["texto"]


# ── Long-form scene ranges ──────────────────────────────────────

def _ts(words, start_s, step=0.5):
    out = []
    for i, word in enumerate(words):
        s = start_s + i * step
        out.append({"word": word, "start_ms": s * 1000, "end_ms": (s + step) * 1000})
    return out


def test_compute_block_ranges_is_contiguous():
    from pipeline.video_editor import VideoEditor

    editor = VideoEditor({})
    bloques = [
        {"tipo": "hook", "texto": "uno dos"},
        {"tipo": "cierre", "texto": "tres cuatro"},
    ]
    timestamps = _ts(["uno", "dos"], 0.0) + _ts(["tres", "cuatro"], 3.0)
    ranges = editor._compute_block_ranges(
        bloques, timestamps, audio_duration=4.0,
    )
    total = sum(r["duration"] for r in ranges)
    # Contiguous ranges cover the full narration span (including the 1s gap).
    assert total == pytest.approx(4.0, abs=0.2)


def test_compute_block_ranges_rejects_timeline_that_drops_narration():
    from pipeline.video_editor import VideoEditor

    editor = VideoEditor({})
    bloques = [{"tipo": "cierre", "texto": "uno dos"}]
    timestamps = _ts(["uno", "dos"], 0.0)
    with pytest.raises(RuntimeError, match="does not cover the narration"):
        editor._compute_block_ranges(bloques, timestamps, audio_duration=20.0)


def test_subscene_ranges_are_contiguous():
    from pipeline.video_editor import VideoEditor

    editor = VideoEditor({})
    words = [
        {"word": f"w{i}", "start": i * 0.5, "end": (i + 1) * 0.5}
        for i in range(10)
    ]
    scene = {
        "start": 0.0, "end": 5.0, "texto": " ".join(w["word"] for w in words),
        "word_timestamps": words, "tipo": "desarrollo", "media_tipo": "imagen",
    }
    subs = editor._semantic_subscene_ranges(scene, max_dur=2.0, count=3)
    assert sum(end - start for start, end, _, _ in subs) == pytest.approx(5.0)


def test_assert_body_timeline_sync_is_strict():
    from pipeline.video_editor import VideoEditor

    editor = VideoEditor({"LONGFORM_MAX_TRAILING_AUDIO_SEC": 2.0,
                          "SCENE_SYNC_TOLERANCE_SEC": 0.15})
    editor._assert_body_timeline_sync(40.0, 41.0)  # small trailing silence
    with pytest.raises(RuntimeError, match="longer than the video body"):
        editor._assert_body_timeline_sync(40.0, 50.0)


# ── Short render validation ────────────────────────────────────

def test_validate_short_render_rejects_truncated_narration(tmp_path, monkeypatch):
    import pipeline.shorts_media as sm

    fake = tmp_path / "short.mp4"
    fake.write_bytes(b"x")
    monkeypatch.setattr(
        sm, "_probe_short_media",
        lambda p: {
            "path": str(p), "exists": True, "bytes": 1,
            "has_video": True, "has_audio": True,
            "width": 1080, "height": 1920,
            "video_duration": 20.0, "audio_duration": 20.0,
            "container_duration": 20.0,
        },
    )
    with pytest.raises(sm.ShortRenderValidationError, match="truncated narration"):
        sm.validate_short_render(fake, expected_duration_sec=30.0)


def test_rescale_short_scene_ranges_matches_audio():
    from pipeline.shorts_media import rescale_short_scene_ranges

    ranges = [{"duration": 10.0}, {"duration": 10.0}]
    scaled = rescale_short_scene_ranges(ranges, 30.0)
    assert sum(r["duration"] for r in scaled) == pytest.approx(30.0)


# ── TTS audio helpers ──────────────────────────────────────────

def test_trim_trailing_silence_keeps_tail():
    import numpy as np
    from pydub import AudioSegment
    from pipeline.audio_utils import trim_trailing_silence

    sr = 16000
    tone = (0.2 * np.sin(2 * np.pi * 440 * np.arange(sr) / sr)).astype(np.float32)
    audio = AudioSegment(
        (np.concatenate([tone, np.zeros(sr, dtype=np.float32)]) * 32767)
        .astype(np.int16).tobytes(),
        frame_rate=sr, sample_width=2, channels=1,
    )
    trimmed = trim_trailing_silence(audio, keep_ms=100)
    assert 900 <= len(trimmed) <= 1250


def test_tts_engine_does_not_silently_skip_failed_segment(monkeypatch):
    import pipeline.tts_engine as te

    def _boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(te, "_run_async_with_timeout", _boom)
    engine = te.TTSEngine({"voice": "es-ES-AlvaroNeural"})
    with pytest.raises(RuntimeError, match="TTS segment failed"):
        engine._synthesize_with_retry("hola", "+0%", "+0Hz", "+0%", 5)
