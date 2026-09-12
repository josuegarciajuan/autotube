"""Tests for viral source-mode planning + robust transcript fetching.

Covers:
  - ``_build_source_mode_sequence``: garantía de mínimo viral y reparto
    probabilístico por slot (≈80-90% con 2-3 vídeos/día).
  - ``YouTubeViralScraper._vtt_to_text`` / ``_fetch_transcript``: híbrido
    subtítulos → audio → title+description (no descarta por fallo de descarga).

Run:  python3 -m pytest tests/test_viral_generation_robustness.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date, timedelta

import pytest

from api.services.planning_service import _build_source_mode_sequence


def _dates(n: int):
    base = date(2026, 1, 1)
    return [(base + timedelta(days=i)).isoformat() for i in range(n)]


# ── _build_source_mode_sequence ─────────────────────────────────


def test_guarantees_minimum_viral_without_boost():
    ch = {"channel_id": 1, "viral_per_day": 1, "viral_day_boost_weight": 0.0}
    for d in _dates(60):
        seq = _build_source_mode_sequence(2, ch, d)
        assert len(seq) == 2
        assert seq.count("viral") >= 1


def test_explicit_zero_without_boost_is_all_original():
    ch = {"channel_id": 1, "viral_per_day": 0, "viral_day_boost_weight": 0.0}
    seq = _build_source_mode_sequence(2, ch, "2026-02-01")
    assert seq == ["original", "original"]


def test_zero_min_with_boost_is_probabilistic_not_guaranteed():
    # Mínimo 0 = sin garantía; el boost todavía reparte virales (coherente con
    # "probabilidad por cada slot restante"). Para opt-out total: mínimo 0 Y boost 0.
    ch = {"channel_id": 1, "viral_per_day": 0, "viral_day_boost_weight": 0.8}
    dates = _dates(500)
    viral = sum(_build_source_mode_sequence(2, ch, d).count("viral") for d in dates)
    ratio = viral / (2 * len(dates))
    assert 0.75 <= ratio <= 0.88


def test_minimum_above_total_is_all_viral():
    ch = {"channel_id": 2, "viral_per_day": 5, "viral_day_boost_weight": 0.0}
    assert _build_source_mode_sequence(2, ch, "2026-02-01") == ["viral", "viral"]


def test_viral_first_ordering_with_one_viral():
    # Con 1 viral garantizado debe ir primero (despacho prioritario).
    ch = {"channel_id": 3, "viral_per_day": 1, "viral_day_boost_weight": 0.0}
    for d in _dates(30):
        seq = _build_source_mode_sequence(3, ch, d)
        assert seq[0] == "viral"


def test_expected_ratio_two_slots_is_80_90_percent():
    ch = {"channel_id": 4, "viral_per_day": 1, "viral_day_boost_weight": 0.8}
    dates = _dates(500)
    viral = sum(_build_source_mode_sequence(2, ch, d).count("viral") for d in dates)
    ratio = viral / (2 * len(dates))
    assert 0.80 <= ratio <= 0.92
    # Garantía dura en cada día individual.
    assert all(_build_source_mode_sequence(2, ch, d).count("viral") >= 1 for d in dates)


def test_expected_ratio_three_slots_stays_high():
    ch = {"channel_id": 5, "viral_per_day": 1, "viral_day_boost_weight": 0.8}
    dates = _dates(500)
    viral = sum(_build_source_mode_sequence(3, ch, d).count("viral") for d in dates)
    ratio = viral / (3 * len(dates))
    assert 0.80 <= ratio <= 0.92


def test_deterministic_same_date_same_sequence():
    ch = {"channel_id": 6, "viral_per_day": 1, "viral_day_boost_weight": 0.5}
    a = _build_source_mode_sequence(3, ch, "2026-03-15")
    b = _build_source_mode_sequence(3, ch, "2026-03-15")
    assert a == b


# ── Transcript híbrido ──────────────────────────────────────────

SAMPLE_VTT = """WEBVTT

00:00:00.000 --> 00:00:02.000
Hello world this is the first line

00:00:02.000 --> 00:00:04.000
Hello world this is the first line

00:00:04.000 --> 00:00:06.000
<c>Second</c> distinct line with <00:00:05.000>tags
"""


@pytest.fixture
def scraper():
    from scrapers.youtube_viral import YouTubeViralScraper
    return YouTubeViralScraper(config=None)


def test_vtt_to_text_dedups_and_strips_tags(tmp_path, scraper):
    p = tmp_path / "sub.vtt"
    p.write_text(SAMPLE_VTT, encoding="utf-8")
    text = scraper._vtt_to_text(p)
    assert "Hello world this is the first line" in text
    assert text.count("Hello world this is the first line") == 1  # dedup
    assert "<c>" not in text and "<00:00:05.000>" not in text
    assert "Second distinct line with tags" in text


def test_fetch_transcript_prefers_subtitles(scraper, monkeypatch):
    long_text = "word " * 100
    monkeypatch.setattr(scraper, "_fetch_subtitles", lambda url, vid: long_text)
    called = {"audio": False}
    monkeypatch.setattr(scraper, "_download_audio",
                        lambda url, vid: called.__setitem__("audio", True) or None)
    out = scraper._fetch_transcript({"url": "u", "video_id": "v", "title": "t"})
    assert out == long_text
    assert called["audio"] is False


def test_fetch_transcript_falls_back_to_audio(scraper, monkeypatch):
    monkeypatch.setattr(scraper, "_fetch_subtitles", lambda url, vid: None)
    monkeypatch.setattr(scraper, "_download_audio", lambda url, vid: "/tmp/x.mp3")
    monkeypatch.setattr(scraper, "_transcribe", lambda path: "audio " * 100)
    out = scraper._fetch_transcript({"url": "u", "video_id": "v", "title": "t"})
    assert out and out.startswith("audio")


def test_fetch_transcript_last_resort_title_description(scraper, monkeypatch):
    monkeypatch.setattr(scraper, "_fetch_subtitles", lambda url, vid: None)
    monkeypatch.setattr(scraper, "_download_audio", lambda url, vid: None)
    out = scraper._fetch_transcript({
        "url": "u", "video_id": "v",
        "title": "A very long documentary title",
        "description": "description " * 20,
    })
    assert out and "A very long documentary title" in out
