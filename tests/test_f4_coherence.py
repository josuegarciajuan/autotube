"""Tests de F4 — coherencia de nicho estricta y series de contenido."""
from pipeline.niche_guard import filter_on_niche, is_guard_enabled
from pipeline.topic_series import series_seed_queries, series_names, series_episode_hints
from pipeline.topic_seeding import seeds_from_config


class Cfg:
    NICHE_GUARD_ENABLED = True
    NICHE_ANCHORS = ["civiliz", "arqueolog"]
    TITLE_NICHE_FIT_MIN = 0.3
    CHANNEL_KEYWORDS = []
    SEO_SECONDARY_KEYWORDS = []
    TITLE_GOOD_EXAMPLES = []
    SEO_PRIMARY_KEYWORD = ""
    NICHE_KEYWORDS_ENG = []
    TOPIC_SEED_QUERIES = []
    CONTENT_SERIES = [
        {"name": "Misterios", "seed_queries": ["civilizaciones perdidas", "ruinas"]},
        {"name": "Enigmas", "seed_queries": ["ruinas", "tecnologia antigua"]},
    ]


def test_strict_filter_defers_instead_of_accepting_off_niche():
    cfg = Cfg()
    assert is_guard_enabled(cfg) is True
    items = [{"title": "David y Goliat"}, {"title": "Partido de futbol"}]

    kept_soft, dropped_soft = filter_on_niche(items, lambda i: i["title"], cfg, strict=False)
    assert len(kept_soft) == 2 and dropped_soft == 0  # no hambruna

    kept_strict, dropped_strict = filter_on_niche(items, lambda i: i["title"], cfg, strict=True)
    assert kept_strict == [] and dropped_strict == 2  # difiere


def test_strict_filter_keeps_on_niche():
    cfg = Cfg()
    items = [{"title": "Civilizaciones perdidas"}, {"title": "Partido de futbol"}]
    kept, dropped = filter_on_niche(items, lambda i: i["title"], cfg, strict=True)
    assert [i["title"] for i in kept] == ["Civilizaciones perdidas"]
    assert dropped == 1


def test_series_seed_queries_dedup():
    cfg = Cfg()
    q = series_seed_queries(cfg)
    assert q[0] == "civilizaciones perdidas"
    assert "ruinas" in q
    assert q.count("ruinas") == 1  # deduplicado entre series
    assert set(series_names(cfg)) == {"Misterios", "Enigmas"}


def test_series_seed_queries_included_in_seeds():
    cfg = Cfg()
    cfg.SEO_PRIMARY_KEYWORD = "civilizaciones antiguas"
    seeds = seeds_from_config(cfg)
    assert "civilizaciones perdidas" in seeds


def test_series_episode_hints():
    cfg = Cfg()
    cfg.CONTENT_SERIES = [{"name": "S", "episodes": ["Ep1", "Ep2"]}]
    hints = series_episode_hints(cfg)
    assert hints == [{"series": "S", "episode": "Ep1"}, {"series": "S", "episode": "Ep2"}]
