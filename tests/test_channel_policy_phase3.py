from types import SimpleNamespace


def test_config_bridge_deep_merges_policy_dicts_without_overriding_python_keys():
    from config.config_bridge import _merge_configs

    py = SimpleNamespace(
        MEDIA_STRATEGY={"video_ratio": 0.8, "providers": {"primary": "python"}},
        CROSS_PLATFORM={"instagram": {"enabled": False}},
    )
    defaults = SimpleNamespace(
        MEDIA_STRATEGY={"video_ratio": 0.5, "image_ratio": 0.5,
                        "providers": {"primary": "default", "backup": "default"}},
        CROSS_PLATFORM={"instagram": {"enabled": True, "account": "default"},
                        "tiktok": {"enabled": False}},
    )
    db = {
        "MEDIA_STRATEGY": {"image_ratio": 0.2, "providers": {"backup": "db"}},
        "CROSS_PLATFORM": {"instagram": {"account": "db"}, "youtube": {"enabled": True}},
    }

    result = _merge_configs(py, db, defaults)

    assert result.MEDIA_STRATEGY == {
        "video_ratio": 0.8,
        "image_ratio": 0.2,
        "providers": {"primary": "python", "backup": "db"},
    }
    assert result.CROSS_PLATFORM["instagram"] == {"enabled": False, "account": "db"}
    assert result.CROSS_PLATFORM["tiktok"] == {"enabled": False}
    assert result.CROSS_PLATFORM["youtube"] == {"enabled": True}


def test_planning_config_accepts_uppercase_longform_generation_key():
    from api.services.planning_service import _resolve_generation_per_day

    assert _resolve_generation_per_day(
        {"LONGFORM_GENERATION_PER_DAY": 3, "videos_per_day": 1}, "2026-08-31"
    ) == 3


def test_title_limit_is_shared_by_enricher_generator_and_validator():
    from pipeline.metadata_generator import MetadataGenerator
    from pipeline.title_enricher import resolve_title_max_chars
    from pipeline.video_validator import VideoValidator

    config = SimpleNamespace(TITLE_MAX_CHARS=65, TITLE_POWER_WORDS=[])
    assert resolve_title_max_chars(config) == 65
    assert MetadataGenerator(config).title_max_chars == 65
    assert VideoValidator(config).title_max_chars == 65


def test_metadata_fallback_does_not_create_prohibited_generic_tags():
    from pipeline.metadata_generator import MetadataGenerator

    config = SimpleNamespace(TITLE_POWER_WORDS=[])
    result = MetadataGenerator(config)._fallback_metadata({"titulo_options": [], "keywords": []})

    assert result["tags"] == []


def test_empty_active_channels_has_a_clear_error_in_optional_config_fallback(monkeypatch):
    from pipeline import media_fetcher

    monkeypatch.setattr(media_fetcher.settings, "ACTIVE_CHANNELS", [])
    try:
        media_fetcher.MediaFetcher()
    except ValueError as exc:
        assert "active channel" in str(exc).lower()
    else:
        raise AssertionError("MediaFetcher should not index an empty ACTIVE_CHANNELS list")
