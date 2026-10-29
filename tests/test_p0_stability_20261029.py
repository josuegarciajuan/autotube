"""Regression tests for the P0 stability changes (oct 2026)."""
from types import SimpleNamespace

from pipeline.niche_guard import niche_decision


def _cfg(**over):
    base = dict(
        NICHE_GUARD_ENABLED=True,
        NICHE_GUARD_STRICT=True,
        NICHE_GUARD_MODE="soft",
        NICHE_ANCHORS=["cerebro", "sincron"],
        NICHE_ADJACENT_TOPICS=["enigmas", "cirugia"],
        NICHE_BLOCKED_TOPICS=["futbol"],
        CHANNEL_KEYWORDS=[],
        SEO_SECONDARY_KEYWORDS=[],
        TITLE_GOOD_EXAMPLES=[],
        SEO_PRIMARY_KEYWORD="",
        TITLE_NICHE_FIT_MIN=0.30,
        NICHE_STRETCH_MIN_SCORE=0.20,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_soft_niche_allows_adjacent_theme():
    decision, score = niche_decision("7 enigmas sin resolver", _cfg())
    assert decision == "stretch"
    assert 0.20 <= score < 0.60


def test_soft_niche_keeps_core_theme():
    decision, _ = niche_decision("La sincronía que cambió su vida", _cfg())
    assert decision == "on_niche"


def test_soft_niche_rejects_blocked_theme():
    decision, _ = niche_decision("El partido de fútbol de ayer", _cfg())
    assert decision == "off_niche"


def test_reddit_source_chain_has_no_oauth():
    from scrapers.reddit import RedditScraper
    scraper = RedditScraper(config=SimpleNamespace(CANAL_NAME="canal2"))
    names = [name for name, _ in scraper._sources]
    assert "oauth" not in names
    assert names[0] == "arctic_shift"


def test_reddit_403_disables_only_that_source(monkeypatch):
    from scrapers.reddit import RedditScraper
    from scrapers.base import PermanentBlock

    scraper = RedditScraper(config=SimpleNamespace(CANAL_NAME="canal2"))
    calls = []

    def blocked(_sub):
        calls.append("blocked")
        raise PermanentBlock("403")

    def works(_sub):
        calls.append("works")
        return [{"url": "u", "title": "t", "text": "x" * 250, "subreddit": "s"}]

    scraper._sources = [("old_reddit", blocked), ("arctic_shift", works)]
    out = scraper._scrape_subreddit("paranormal")
    assert len(out) == 1
    assert calls == ["blocked", "works"]
    assert "old_reddit" in scraper._source_disabled


def test_viral_tries_next_candidate_when_first_fails(monkeypatch):
    from scrapers.youtube_viral import YouTubeViralScraper

    scraper = object.__new__(YouTubeViralScraper)
    scraper.canal = "canal2"
    scraper._db = None
    seen = []

    def fake_process(candidate):
        seen.append(candidate["url"])
        return None if len(seen) == 1 else {"url": candidate["url"], "ok": True}

    monkeypatch.setattr(scraper, "process_candidate", fake_process)
    out = scraper._try_next_candidates(
        [{"url": "a", "title": "A"}, {"url": "b", "title": "B"}],
        db=None, skip_url="",
    )
    assert out == [{"url": "b", "ok": True}]
    assert seen == ["a", "b"]


def test_wall_scraper_raises_feed_unavailable_on_404(monkeypatch):
    from pipeline import youtube_wall_scraper as ws

    class _Resp:
        status_code = 404
        content = b""

    monkeypatch.setattr(ws.requests, "get", lambda *a, **k: _Resp())
    try:
        ws.fetch_channel_public_video_ids("UCxxx", raise_on_http_error=True)
    except ws.FeedUnavailable:
        pass
    else:
        raise AssertionError("FeedUnavailable not raised")
