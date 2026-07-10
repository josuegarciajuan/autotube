"""Unit tests for shorts components.

Tests cover:
- ShortsScheduler: CRUD, scheduling logic, stats
- ShortsExtractor: LLM response parsing, timestamp formatting, JSON extraction
- ShortsRenderer: SRT subtitle generation, resolution constants, FFmpeg escaping
"""

import json
import tempfile
import sqlite3
from pathlib import Path
from datetime import date, timedelta

import pytest

from pipeline.shorts_scheduler import ShortsScheduler, DEFAULT_CLIP_SCHEDULE
from pipeline.shorts_extractor import ShortsExtractor
from pipeline.shorts_renderer import (
    ShortsRenderer,
    _ms_to_srt_time,
    _timestamps_to_srt,
    _esc_ffmpeg,
)


# ── Fixtures ────────────────────────────────────────────────────

@pytest.fixture
def db_path():
    """Create a temporary SQLite database with shorts schema."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Create minimal schema
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, slug TEXT, active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo_final TEXT, video_path TEXT, channel_id INTEGER,
            script_id INTEGER, status TEXT DEFAULT 'uploaded'
        );
    """)

    # Load stats schema (v3 — needed for video_stats_history table)
    schema_v3 = Path(__file__).parent.parent / "database" / "schema_v3.sql"
    if schema_v3.exists():
        with open(schema_v3) as sf:
            conn.executescript(sf.read())

    # Load shorts schema (v4 — depends on v3)
    schema_v4 = Path(__file__).parent.parent / "database" / "schema_v4.sql"
    if schema_v4.exists():
        with open(schema_v4) as sf:
            conn.executescript(sf.read())

    # Seed test data
    conn.execute("INSERT INTO channels (name, slug) VALUES ('Test', 'test_channel')")
    conn.execute("INSERT INTO videos (id, titulo_final, channel_id, script_id, video_path, status) VALUES (1, 'Test Video', 1, 1, '/tmp/test.mp4', 'uploaded')")
    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def scheduler(db_path):
    return ShortsScheduler(db_path)


# ── ShortsScheduler ──────────────────────────────────────────────

class TestShortsScheduler:
    """Tests for ShortsScheduler: schedule, CRUD, stats."""

    def test_schedule_clips_basic(self, scheduler):
        """Scheduling clips creates DB entries with correct staggered dates."""
        clips = [
            {"start_time": 10, "end_time": 60, "hook_title": "Clip 1", "hook_text": "Hook 1", "ranking": 1},
            {"start_time": 70, "end_time": 120, "hook_title": "Clip 2", "hook_text": "Hook 2", "ranking": 2},
            {"start_time": 130, "end_time": 190, "hook_title": "Clip 3", "hook_text": "Hook 3", "ranking": 3},
        ]

        ids = scheduler.schedule_clips(1, "test_channel", clips)

        assert len(ids) == 3
        assert all(isinstance(i, int) and i > 0 for i in ids)

    def test_schedule_clips_staggered_dates(self, scheduler):
        """Clip scheduling respects the offset_days stagger pattern."""
        clips = [{"start_time": 10, "end_time": 60, "hook_title": f"Clip {i}", "hook_text": f"Hook {i}", "ranking": i} for i in range(1, 6)]

        ids = scheduler.schedule_clips(1, "test_channel", clips)

        today = date.today()
        shorts = scheduler.get_shorts()
        dates = []

        for s in shorts:
            if s["id"] in ids and s["scheduled_date"]:
                dates.append(s["scheduled_date"])

        # With DEFAULT_CLIP_SCHEDULE: +1d, +2d, +3d → 3 batches of 1 each
        assert len(dates) == 3  # Only 3 scheduled (max 5 clips but schedule has 3 slots)

        expected_dates = [
            (today + timedelta(days=b["offset_days"])).isoformat()
            for b in DEFAULT_CLIP_SCHEDULE
        ]
        for d in expected_dates:
            assert d in dates

    def test_schedule_clips_empty(self, scheduler):
        """Empty clips list returns no IDs."""
        ids = scheduler.schedule_clips(1, "test_channel", [])
        assert ids == []

    def test_get_shorts_with_filters(self, scheduler):
        """Filtering shorts by type, status, and channel works."""
        # Create some shorts
        conn = sqlite3.connect(scheduler.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO shorts (channel_id, type, status, hook_title, scheduled_date) VALUES (1, 'clip', 'pending', 'Test 1', ?)",
            (date.today().isoformat(),),
        )
        conn.execute(
            "INSERT INTO shorts (channel_id, type, status, hook_title, scheduled_date) VALUES (1, 'native', 'published', 'Test 2', ?)",
            (date.today().isoformat(),),
        )
        conn.commit()
        conn.close()

        # Filter by type
        clips = scheduler.get_shorts(type_filter="clip")
        assert len(clips) >= 1
        assert all(s["type"] == "clip" for s in clips)

        # Filter by status
        published = scheduler.get_shorts(status="published")
        assert len(published) >= 1
        assert all(s["status"] == "published" for s in published)

    def test_get_short(self, scheduler):
        """get_short returns correct short by ID."""
        shorts = scheduler.get_shorts()
        if not shorts:
            pytest.skip("No shorts to test")

        s = scheduler.get_short(shorts[0]["id"])
        assert s is not None
        assert s["id"] == shorts[0]["id"]

    def test_get_short_not_found(self, scheduler):
        """get_short returns None for nonexistent ID."""
        assert scheduler.get_short(99999) is None

    def test_mark_published(self, scheduler):
        """mark_published updates short status and YouTube metadata."""
        conn = sqlite3.connect(scheduler.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        cursor = conn.execute(
            "INSERT INTO shorts (channel_id, type, status, hook_title) VALUES (1, 'clip', 'ready', 'Test')"
        )
        short_id = cursor.lastrowid
        conn.commit()
        conn.close()

        scheduler.mark_published(short_id, "yt123", "https://youtube.com/watch?v=yt123", "/tmp/short.mp4")

        s = scheduler.get_short(short_id)
        assert s["status"] == "published"
        assert s["youtube_id"] == "yt123"
        assert s["youtube_url"] == "https://youtube.com/watch?v=yt123"
        assert s["published_at"] is not None

    def test_mark_failed(self, scheduler):
        """mark_failed sets status and error message."""
        conn = sqlite3.connect(scheduler.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        cursor = conn.execute(
            "INSERT INTO shorts (channel_id, type, status, hook_title) VALUES (1, 'clip', 'rendering', 'Test')"
        )
        short_id = cursor.lastrowid
        conn.commit()
        conn.close()

        scheduler.mark_failed(short_id, "Test error message")

        s = scheduler.get_short(short_id)
        assert s["status"] == "failed"
        assert "Test error message" in s["error_message"]

    def test_update_short(self, scheduler):
        """update_short modifies allowed fields."""
        conn = sqlite3.connect(scheduler.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        cursor = conn.execute(
            "INSERT INTO shorts (channel_id, type, status, hook_title) VALUES (1, 'clip', 'pending', 'Old Title')"
        )
        short_id = cursor.lastrowid
        conn.commit()
        conn.close()

        scheduler.update_short(short_id, hook_title="New Title", ranking=2)
        s = scheduler.get_short(short_id)
        assert s["hook_title"] == "New Title"
        assert s["ranking"] == 2

    def test_delete_short(self, scheduler):
        """delete_short removes from DB."""
        conn = sqlite3.connect(scheduler.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        cursor = conn.execute(
            "INSERT INTO shorts (channel_id, type, status, hook_title) VALUES (1, 'clip', 'pending', 'Delete Me')"
        )
        short_id = cursor.lastrowid
        conn.commit()
        conn.close()

        scheduler.delete_short(short_id)
        assert scheduler.get_short(short_id) is None

    def test_get_stats(self, scheduler):
        """get_stats returns aggregate counts."""
        conn = sqlite3.connect(scheduler.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO shorts (channel_id, type, status, hook_title) VALUES (1, 'clip', 'published', 'A')")
        conn.execute("INSERT INTO shorts (channel_id, type, status, hook_title) VALUES (1, 'native', 'pending', 'B')")
        conn.execute("INSERT INTO shorts (channel_id, type, status, hook_title) VALUES (1, 'clip', 'ready', 'C')")
        conn.commit()
        conn.close()

        stats = scheduler.get_stats()
        assert stats["total"] >= 3
        assert "published" in stats
        assert "pending" in stats
        assert "ready" in stats
        assert "by_type" in stats

    def test_get_today_pending(self, scheduler):
        """get_today_pending returns only today's pending shorts."""
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()

        conn = sqlite3.connect(scheduler.db_path)
        conn.execute("PRAGMA foreign_keys=ON")

        # Today pending
        conn.execute(
            "INSERT INTO shorts (channel_id, type, status, hook_title, scheduled_date) VALUES (1, 'clip', 'pending', 'Today', ?)",
            (today,),
        )
        # Tomorrow — should not appear
        conn.execute(
            "INSERT INTO shorts (channel_id, type, status, hook_title, scheduled_date) VALUES (1, 'clip', 'pending', 'Tomorrow', ?)",
            (tomorrow,),
        )
        # Yesterday published — should not appear
        conn.execute(
            "INSERT INTO shorts (channel_id, type, status, hook_title, scheduled_date) VALUES (1, 'clip', 'published', 'Yesterday', ?)",
            (yesterday,),
        )
        conn.commit()
        conn.close()

        pending = scheduler.get_today_pending()
        assert len(pending) >= 1
        assert all(s["scheduled_date"] == today for s in pending)

    def test_get_clips_for_video(self, scheduler):
        """get_clips_for_video returns only clips from a specific video."""
        conn = sqlite3.connect(scheduler.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO shorts (channel_id, source_video_id, type, status, hook_title, ranking) VALUES (1, 1, 'clip', 'pending', 'Clip A', 1)"
        )
        conn.execute(
            "INSERT INTO shorts (channel_id, source_video_id, type, status, hook_title, ranking) VALUES (1, 1, 'clip', 'pending', 'Clip B', 2)"
        )
        conn.execute(
            "INSERT INTO shorts (channel_id, type, status, hook_title) VALUES (1, 'native', 'pending', 'Native C')"
        )
        conn.commit()
        conn.close()

        clips = scheduler.get_clips_for_video(1)
        assert len(clips) == 2
        assert all(c["type"] == "clip" for c in clips)
        assert all(c["source_video_id"] == 1 for c in clips)

    def test_mark_rendering_and_ready(self, scheduler):
        """Status transitions: pending → rendering → ready."""
        conn = sqlite3.connect(scheduler.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        cursor = conn.execute(
            "INSERT INTO shorts (channel_id, type, status, hook_title) VALUES (1, 'clip', 'pending', 'Test')"
        )
        short_id = cursor.lastrowid
        conn.commit()
        conn.close()

        scheduler.mark_rendering(short_id)
        assert scheduler.get_short(short_id)["status"] == "rendering"

        scheduler.mark_ready(short_id, "/tmp/test_short.mp4")
        s = scheduler.get_short(short_id)
        assert s["status"] == "ready"
        assert s["file_path"] == "/tmp/test_short.mp4"


# ── ShortsExtractor ──────────────────────────────────────────────

class TestShortsExtractor:
    """Tests for ShortsExtractor: JSON parsing, timestamp formatting."""

    def test_format_timestamps_groups_words(self):
        """Word timestamps are grouped into 5-second intervals."""
        extractor = ShortsExtractor()
        timestamps = [
            {"word": "Hola", "start": 0.0},
            {"word": "mundo", "start": 0.5},
            {"word": "esto", "start": 1.0},
            {"word": "es", "start": 6.0},
            {"word": "una", "start": 6.3},
            {"word": "prueba", "start": 12.5},
        ]

        result = extractor._format_timestamps(timestamps)

        assert "[0s]" in result
        assert "[6s]" in result
        assert "[12s]" in result
        assert "Hola mundo esto" in result
        assert "es una" in result

    def test_format_timestamps_empty(self):
        """Empty timestamps returns fallback message."""
        extractor = ShortsExtractor()
        result = extractor._format_timestamps([])
        assert "no timestamps available" in result.lower()

    def test_extract_json_clean(self):
        """JSON extraction from clean markdown block."""
        extractor = ShortsExtractor()
        text = '```json\n{"clips": [{"start_time": 10, "end_time": 60}]}\n```'
        result = extractor._extract_json(text)
        assert "clips" in result
        assert "10" in result

    def test_extract_json_no_markers(self):
        """JSON extraction from raw text without markers."""
        extractor = ShortsExtractor()
        text = 'Here is the result: {"clips": [{"start_time": 10}]} done.'
        result = extractor._extract_json(text)
        assert "clips" in result

    def test_extract_json_missing(self):
        """Returns original text when no JSON found."""
        extractor = ShortsExtractor()
        text = "No JSON here, just text."
        result = extractor._extract_json(text)
        assert result == text


# ── ShortsRenderer ────────────────────────────────────────────────

class TestShortsRenderer:
    """Tests for ShortsRenderer: SRT generation, escaping, constants."""

    # ── _ms_to_srt_time ────────────────────────────────────────

    def test_ms_to_srt_time_zero(self):
        """0ms → 00:00:00,000."""
        assert _ms_to_srt_time(0) == "00:00:00,000"

    def test_ms_to_srt_time_one_second(self):
        """1000ms → 00:00:01,000."""
        assert _ms_to_srt_time(1000) == "00:00:01,000"

    def test_ms_to_srt_time_one_minute(self):
        """60000ms → 00:01:00,000."""
        assert _ms_to_srt_time(60_000) == "00:01:00,000"

    def test_ms_to_srt_time_one_hour(self):
        """3600000ms → 01:00:00,000."""
        assert _ms_to_srt_time(3_600_000) == "01:00:00,000"

    def test_ms_to_srt_time_with_millis(self):
        """61234ms → 00:01:01,234."""
        assert _ms_to_srt_time(61234) == "00:01:01,234"

    # ── _timestamps_to_srt ─────────────────────────────────────

    def test_timestamps_to_srt_empty(self):
        """Empty timestamps list returns empty string."""
        assert _timestamps_to_srt([]) == ""

    def test_timestamps_to_srt_single_word(self):
        """Single word produces one SRT block."""
        ts = [{"word": "Hola", "start_ms": 100, "end_ms": 500}]
        srt = _timestamps_to_srt(ts)
        assert "1\n" in srt
        assert "00:00:00,100 --> 00:00:00,500" in srt
        assert "Hola" in srt

    def test_timestamps_to_srt_groups_words(self):
        """Words within 200ms gap are grouped into the same block."""
        ts = [
            {"word": "El", "start_ms": 0, "end_ms": 150},
            {"word": "misterio", "start_ms": 200, "end_ms": 500},  # gap=50ms (<200)
            {"word": "continúa", "start_ms": 2000, "end_ms": 2500},  # gap=1500ms (>200)
        ]
        srt = _timestamps_to_srt(ts)
        # First block: "El misterio"
        assert "El misterio" in srt
        # Second block: "continúa"
        assert "continúa" in srt
        # Should have exactly 2 subtitle blocks (N blocks → N-1 \n\n separators)
        assert srt.count("\n\n") == 1
        assert "1\n" in srt and "2\n" in srt  # both indices present

    def test_timestamps_to_srt_respects_max_chars(self):
        """Group stops when accumulated text exceeds 42 chars."""
        words = [{"word": f"palabra{i}", "start_ms": i * 100, "end_ms": i * 100 + 90} for i in range(20)]
        srt = _timestamps_to_srt(words)
        # Each "palabra0 palabra1..." is 10+1=11 chars → max 3-4 words per block
        # With 20 words we expect multiple blocks (>1)
        assert srt.count("\n\n") > 1

    def test_timestamps_to_srt_respects_gap_threshold(self):
        """200ms gap threshold triggers a new subtitle block."""
        ts = [
            {"word": "Uno", "start_ms": 0, "end_ms": 100},
            {"word": "Dos", "start_ms": 500, "end_ms": 600},  # gap=400ms > 200
        ]
        srt = _timestamps_to_srt(ts)
        # 2 blocks → 1 separator
        assert srt.count("\n\n") == 1
        assert "Uno" in srt
        assert "Dos" in srt
        assert "1\n" in srt and "2\n" in srt

    # ── _esc_ffmpeg ────────────────────────────────────────────

    def test_esc_ffmpeg_single_quote(self):
        """Single quotes are escaped for FFmpeg filter values."""
        result = _esc_ffmpeg("texto con 'comilla' adentro")
        assert "\\\\\\'" in result  # escaped single quote

    def test_esc_ffmpeg_colon(self):
        """Colons are escaped."""
        result = _esc_ffmpeg("C:\\path")
        assert "\\\\:" in result

    def test_esc_ffmpeg_plain_text(self):
        """Plain text without special chars is unchanged."""
        result = _esc_ffmpeg("hello_world.mp4")
        assert "'" not in result
        assert ":" not in result

    # ── Constants ──────────────────────────────────────────────

    def test_renderer_resolution_constants(self):
        """Shorts resolution constants are correct 9:16."""
        from pipeline.shorts_renderer import SHORTS_RESOLUTION, SHORTS_FPS
        assert SHORTS_RESOLUTION == (1080, 1920)
        assert SHORTS_FPS == 30

    def test_subtitle_style_no_background_box(self):
        """Subtitles should have BorderStyle=1 (outline only, no box)."""
        from pipeline.shorts_renderer import SUBTITLE_FORCE_STYLE
        assert "BorderStyle=1" in SUBTITLE_FORCE_STYLE
        assert "OutlineColour=&H00000000" in SUBTITLE_FORCE_STYLE
        assert "PrimaryColour=&H00FFFFFF" in SUBTITLE_FORCE_STYLE

    # ── Backward compat: render() without timestamps ───────────

    def test_render_without_timestamps_does_not_crash(self, tmp_path):
        """Calling render without word_timestamps is backward-compatible."""
        renderer = ShortsRenderer()
        # Should return None because source file doesn't exist
        result = renderer.render(
            tmp_path / "nonexistent.mp4",
            {"start_time": 0, "end_time": 30},
            word_timestamps=None,
        )
        assert result is None  # Source not found → None


# ── Integration tests ────────────────────────────────────────────

class TestShortsIntegration:
    """End-to-end tests using the scheduler + DB together."""

    def test_full_lifecycle(self, scheduler):
        """Complete short lifecycle: schedule → render → publish."""
        # Schedule
        clips = [{"start_time": 5, "end_time": 55, "hook_title": "Lifecycle Test", "hook_text": "Testing lifecycle", "ranking": 1}]
        ids = scheduler.schedule_clips(1, "test_channel", clips)
        assert len(ids) == 1
        short_id = ids[0]

        # Verify initial state
        s = scheduler.get_short(short_id)
        assert s["status"] == "pending"
        assert s["scheduled_date"] is not None

        # Render
        scheduler.mark_rendering(short_id)
        assert scheduler.get_short(short_id)["status"] == "rendering"

        scheduler.mark_ready(short_id, "/tmp/test_lifecycle.mp4")
        s = scheduler.get_short(short_id)
        assert s["status"] == "ready"
        assert s["file_path"] == "/tmp/test_lifecycle.mp4"

        # Publish
        scheduler.mark_published(short_id, "yt_lifecycle", "https://youtube.com/watch?v=yt_lifecycle", "/tmp/test_lifecycle.mp4")
        s = scheduler.get_short(short_id)
        assert s["status"] == "published"
        assert s["youtube_id"] == "yt_lifecycle"
        assert s["published_at"] is not None

    def test_multiple_clips_same_video(self, scheduler):
        """Multiple clips from same video get sequential rankings."""
        clips = [
            {"start_time": 10, "end_time": 70, "hook_title": f"Clip {i}", "hook_text": f"Hook {i}", "ranking": i}
            for i in range(1, 4)
        ]
        ids = scheduler.schedule_clips(1, "test_channel", clips)

        all_clips = scheduler.get_clips_for_video(1)
        rankings = [c["ranking"] for c in all_clips if c["id"] in ids]
        assert rankings == sorted(rankings)  # Sorted ascending
