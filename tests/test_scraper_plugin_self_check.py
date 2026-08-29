"""
Stage 3: hand-written plugins (Novel543Scraper, SyosetuScraper, and any future
scrapers/private_*.py) never had a safety net for a site redesign silently
breaking their FIXED selectors -- learn.validate_listing/validate_content only
ever ran on learned/inferred specs (scrapers/ai.py's own tier-1/2 self-check).
SiteScraper.get_novel_info/get_chapter_content now run the same validators
after every real fetch (gated by a `self_check` class flag, default True) and
log a warning when a selector no longer matches -- the only way a redesign
that quietly returns empty/wrong content would ever surface anywhere.

Uses the same fully-invented fixtures as test_scraper_engine.py, not any real
site's markup.
"""
import asyncio
import logging
import os

from scrapers.spec import SiteScraper, QueryPage


def _load(fixtures_dir, name):
    return open(os.path.join(fixtures_dir, name), encoding="utf-8").read()


def _wire(scraper, pages: dict):
    async def fetch(url, headers=None):
        return pages.get(url)
    scraper._fetch = fetch
    return scraper


def _run(coro):
    return asyncio.run(coro)


class _GoodListingScraper(SiteScraper):
    domains = []
    novel_id_re = r"^/n/(\d+)"
    index_url = "/n/{novel_id}/chapters"
    listing_url = "/n/{novel_id}/chapters"
    chapter_links = "ul.chapters li a"
    meta = {"title": "h1.book-title", "author": "span.book-author", "desc": "#book-desc"}


class _RedesignedListingScraper(_GoodListingScraper):
    """Same site, but the chapter list moved -- exactly what a redesign does."""
    chapter_links = "div.totally-gone-after-a-redesign a"


class _RedesignedListingScraperNoSelfCheck(_RedesignedListingScraper):
    self_check = False


class _GoodChapterScraper(SiteScraper):
    domains = []
    content = "#main-body"
    chapter_title = "h1.chapter-heading"


class _RedesignedChapterScraper(_GoodChapterScraper):
    content = ".this-class-does-not-exist-anymore"


LISTING_URL = "https://example.test/n/42/chapters"
CHAPTER_URL = "https://example.test/n/42/ch-1.html"


class TestListingSelfCheck:
    def test_a_matching_selector_logs_no_warning(self, fixtures_dir, caplog):
        pages = {LISTING_URL: _load(fixtures_dir, "example_listing_p1.html")}
        s = _wire(_GoodListingScraper(), pages)
        with caplog.at_level(logging.WARNING, logger="scrapers.spec"):
            info = _run(s.get_novel_info(LISTING_URL))
        assert info is not None and info.chapters
        assert "may be stale" not in caplog.text

    def test_a_selector_that_no_longer_matches_logs_a_warning(self, fixtures_dir, caplog):
        pages = {LISTING_URL: _load(fixtures_dir, "example_listing_p1.html")}
        s = _wire(_RedesignedListingScraper(), pages)
        with caplog.at_level(logging.WARNING, logger="scrapers.spec"):
            _run(s.get_novel_info(LISTING_URL))
        assert "may be stale" in caplog.text
        assert "_RedesignedListingScraper" in caplog.text

    def test_self_check_false_suppresses_the_warning(self, fixtures_dir, caplog):
        """learn.build_scraper() sets this on every scraper IT spawns, since
        ai.py already ran the identical check itself right before spawning."""
        pages = {LISTING_URL: _load(fixtures_dir, "example_listing_p1.html")}
        s = _wire(_RedesignedListingScraperNoSelfCheck(), pages)
        with caplog.at_level(logging.WARNING, logger="scrapers.spec"):
            _run(s.get_novel_info(LISTING_URL))
        assert "may be stale" not in caplog.text


class TestContentSelfCheck:
    def test_a_matching_selector_logs_no_warning(self, fixtures_dir, caplog):
        pages = {CHAPTER_URL: _load(fixtures_dir, "example_chapter1.html")}
        s = _wire(_GoodChapterScraper(), pages)
        with caplog.at_level(logging.WARNING, logger="scrapers.spec"):
            ch = _run(s.get_chapter_content(CHAPTER_URL))
        assert ch is not None and ch.content
        assert "may be stale" not in caplog.text

    def test_a_selector_that_no_longer_matches_logs_a_warning(self, fixtures_dir, caplog):
        pages = {CHAPTER_URL: _load(fixtures_dir, "example_chapter1.html")}
        s = _wire(_RedesignedChapterScraper(), pages)
        with caplog.at_level(logging.WARNING, logger="scrapers.spec"):
            _run(s.get_chapter_content(CHAPTER_URL))
        assert "may be stale" in caplog.text
        assert "_RedesignedChapterScraper" in caplog.text


class TestLearnedScraperOptsOut:
    def test_build_scraper_output_has_self_check_disabled(self):
        from scrapers import learn
        scraper = learn.build_scraper({"chapter_links": "ul.chapters li a"})
        assert scraper.self_check is False
