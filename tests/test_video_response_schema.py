"""Tests for VideoResponse schema — timing_data field.

Run:  python3 -m pytest tests/test_video_response_schema.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import pytest
from pydantic import ValidationError


class TestVideoResponseTimingData:
    """Test that VideoResponse exposes timing_data correctly."""

    @pytest.fixture
    def bare_minimal_video(self):
        """Return a dict with the minimum required fields for VideoResponse."""
        return {
            "id": 1,
            "canal": "canal2",
            "video_path": "/tmp/test.mp4",
        }

    def test_timing_data_field_exists(self, bare_minimal_video):
        """timing_data is accepted as a field and survives serialization."""
        from api.schemas.models import VideoResponse

        bare_minimal_video["timing_data"] = '{"phases":{"scrape":1234},"total_duration_ms":5678}'
        v = VideoResponse(**bare_minimal_video)

        assert v.timing_data == '{"phases":{"scrape":1234},"total_duration_ms":5678}'

    def test_timing_data_is_optional(self, bare_minimal_video):
        """VideoResponse can be constructed without timing_data — it defaults to None."""
        from api.schemas.models import VideoResponse

        v = VideoResponse(**bare_minimal_video)
        assert v.timing_data is None

    def test_timing_data_defaults_to_none(self, bare_minimal_video):
        """Explicit None and default should both result in None."""
        from api.schemas.models import VideoResponse

        # Without the field
        v1 = VideoResponse(**bare_minimal_video)
        assert v1.timing_data is None

        # With explicit None
        bare_minimal_video["timing_data"] = None
        v2 = VideoResponse(**bare_minimal_video)
        assert v2.timing_data is None

    def test_timing_data_empty_json_object(self, bare_minimal_video):
        """An empty JSON object '{}' is valid (for pre-existing videos)."""
        from api.schemas.models import VideoResponse

        bare_minimal_video["timing_data"] = "{}"
        v = VideoResponse(**bare_minimal_video)
        assert v.timing_data == "{}"

    def test_timing_data_with_all_phases(self, bare_minimal_video):
        """Full timing JSON with all phases round-trips correctly."""
        from api.schemas.models import VideoResponse

        data = json.dumps({
            "phases": {
                "scrape": 1234,
                "script": 2345,
                "tts": 3456,
                "media": 4567,
                "video_assembly": 5678,
                "metadata": 6789,
                "upload": 7890,
            },
            "total_duration_ms": 31959,
        })
        bare_minimal_video["timing_data"] = data
        v = VideoResponse(**bare_minimal_video)

        parsed = json.loads(v.timing_data)
        assert parsed["phases"]["scrape"] == 1234
        assert parsed["phases"]["upload"] == 7890
        assert parsed["total_duration_ms"] == 31959

    def test_timing_data_matches_frontend_interface(self, bare_minimal_video):
        """The JSON shape matches what VideoTiming.tsx expects (TypeScript interface)."""
        from api.schemas.models import VideoResponse

        data = json.dumps({
            "phases": {"scrape": 500, "script": 3000, "tts": 15000},
            "total_duration_ms": 20000,
        })
        bare_minimal_video["timing_data"] = data
        v = VideoResponse(**bare_minimal_video)

        parsed = json.loads(v.timing_data)
        # Frontend expects: timing.phases[key] → number, timing.total_duration_ms → number
        assert isinstance(parsed["phases"], dict)
        assert all(isinstance(v, (int, float)) for v in parsed["phases"].values())
        assert isinstance(parsed["total_duration_ms"], (int, float))

    def test_timing_data_accepts_json_nulls(self, bare_minimal_video):
        """Null values within the JSON string are valid (just stored as text)."""
        from api.schemas.models import VideoResponse

        bare_minimal_video["timing_data"] = '{"phases":null,"total_duration_ms":null}'
        v = VideoResponse(**bare_minimal_video)
        assert v.timing_data == '{"phases":null,"total_duration_ms":null}'
