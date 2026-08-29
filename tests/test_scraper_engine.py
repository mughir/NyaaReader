"""
scrapers/spec.py — the declarative plugin engine — exercised directly via
small test-only SiteScraper subclasses, not any real plugin.

This deliberately does NOT import scrapers.private_* (real site plugins,
gitignored and absent in the public checkout / CI): the engine is the public,
shared contract every plugin runs on, and that contract is what needs test
coverage here. A specific site's own selectors are that plugin's private
config, not the engine's concern.
"""
import asyncio
import os

from scrapers.spec import SiteScraper, QueryPage


class ExampleListingScraper(SiteScraper):
    """A minimal, fully invented site spec — see tests/fixtures/example_*.html."""
    domains = []
    language = "en"
    novel_id_re = r"^/n/(\d+)"
    index_url = "/n/{novel_id}/chapters"
    listing_url = "/n/{novel_id}/chapters"
    chapter_links = "ul.chapters li a"
    paginate = QueryPage("page", start=2)
    meta = {
        "title": ["h1.book-title", "meta[property='og:title']@content"],
        "author": "span.book-author",
        "desc": "#book-desc",
    }


class ExampleChapterScraper(SiteScraper):
    domains = []
    content = ["#main-body"]
    chapter_title = "h1.chapter-heading"
    chapter_title_strip = r"\s*\(Part \d+/\d+\)\s*$"
    chapter_number_re = r"/ch-(\d+)"
    drop = ("script", "style", "ins", ".ad-banner")
    drop_classes = ("preface", "afterword")
    drop_text_prefixes = ("NOTICE:",)

    async def next_part_url(self, url, soup):
        a = soup.select_one("a.next-part")
        if not a:
            return None
        from urllib.parse import urljoin
        nxt = urljoin(url, a.get("href"))
        # Only follow within the SAME chapter — same rule real multi-part
        # plugins need: stop at the next chapter's own "ch-2.html".
        import re
        cur_n = re.search(r"/ch-(\d+)", url)
        nxt_n = re.search(r"/ch-(\d+)", nxt)
        if cur_n and nxt_n and cur_n.group(1) == nxt_n.group(1):
            return nxt
        return None


def _wire(scraper, pages: dict):
    async def fetch(url, headers=None):
        return pages.get(url)
    scraper._fetch = fetch
    return scraper


def _run(coro):
    return asyncio.run(coro)


def _load(fixtures_dir, name):
    return open(os.path.join(fixtures_dir, name), encoding="utf-8").read()


class TestListingPagination:
    """The chapter list spans two pages, and one page also holds a small
    'recent chapters' decoy (descending, partial) beside the real list
    (ascending, complete). Picking the wrong cluster either loses chapters or
    numbers the book backwards."""

    def _pages(self, fixtures_dir):
        return {
            "https://example.test/n/42/chapters":
                _load(fixtures_dir, "example_listing_p1.html"),
            "https://example.test/n/42/chapters?page=2":
                _load(fixtures_dir, "example_listing_p2.html"),
        }

    def test_follows_pagination_and_returns_the_full_ascending_list(self, fixtures_dir):
        s = _wire(ExampleListingScraper(), self._pages(fixtures_dir))
        info = _run(s.get_novel_info("https://example.test/n/42/ch-5.html"))

        assert info is not None
        assert len(info.chapters) == 23, "20 on page 1 + 3 on page 2, not the 3-link decoy"
        assert info.chapters[0].number == 1
        assert info.chapters[0].url.endswith("ch-1.html")
        assert info.chapters[-1].number == 23
        assert info.chapters[-1].url.endswith("ch-23.html"), \
            "picking the decoy list would end the book at chapter 20 counting DOWN"

    def test_stops_when_a_page_yields_no_new_links(self, fixtures_dir):
        pages = self._pages(fixtures_dir)
        s = _wire(ExampleListingScraper(), {
            "https://example.test/n/42/chapters": pages["https://example.test/n/42/chapters"],
            # every ?page= request echoes page 1 again — nothing new to add
            "https://example.test/n/42/chapters?page=2": pages["https://example.test/n/42/chapters"],
        })
        info = _run(s.get_novel_info("https://example.test/n/42/ch-5.html"))
        assert len(info.chapters) == 20, "must stop, not loop, once a page adds nothing new"

    def test_novel_id_extracted_from_a_chapter_url(self, fixtures_dir):
        """A user pastes a chapter link, not the listing page — the scraper
        must still locate the book's own pages from it."""
        s = _wire(ExampleListingScraper(), self._pages(fixtures_dir))
        info = _run(s.get_novel_info("https://example.test/n/42/ch-17.html"))
        assert info.title == "Example Book"
        assert info.author == "Example Author"

    def test_declines_when_no_novel_id_is_extractable(self):
        s = ExampleListingScraper()
        info = _run(s.get_novel_info("https://example.test/not-a-book-path/"))
        assert info is None, \
            "falling through to the pasted URL instead of declining would create a title with ZERO chapters"


class TestChapterContent:
    """Content extraction, junk removal, the notice-strip rule, the preface
    trap, and the multi-part merge that must stop at the next chapter."""

    def _pages(self, fixtures_dir):
        return {
            "https://example.test/n/42/ch-1.html": _load(fixtures_dir, "example_chapter1.html"),
            "https://example.test/n/42/ch-1-part2.html": _load(fixtures_dir, "example_chapter1_part2.html"),
        }

    def test_merges_both_parts_and_stops_before_the_next_chapter(self, fixtures_dir):
        s = _wire(ExampleChapterScraper(), self._pages(fixtures_dir))
        ch = _run(s.get_chapter_content("https://example.test/n/42/ch-1.html"))

        assert ch is not None
        assert "placeholder body text" in ch.content
        assert "third placeholder paragraph" in ch.content, "part 2 must be merged in"
        assert "closing placeholder paragraph" in ch.content
        assert ch.number == 1
        assert ch.word_count > 20

    def test_title_part_marker_is_stripped(self, fixtures_dir):
        s = _wire(ExampleChapterScraper(), self._pages(fixtures_dir))
        ch = _run(s.get_chapter_content("https://example.test/n/42/ch-1.html"))
        assert ch.title == "Chapter One", ch.title

    def test_preface_and_afterword_do_not_win_over_the_real_body(self, fixtures_dir):
        s = _wire(ExampleChapterScraper(), self._pages(fixtures_dir))
        ch = _run(s.get_chapter_content("https://example.test/n/42/ch-1.html"))
        assert "A short preface paragraph" not in ch.content
        assert "A short afterword paragraph" not in ch.content

    def test_junk_and_site_notice_are_removed(self, fixtures_dir):
        s = _wire(ExampleChapterScraper(), self._pages(fixtures_dir))
        ch = _run(s.get_chapter_content("https://example.test/n/42/ch-1.html"))
        assert "advertisement placeholder" not in ch.content
        assert "NOTICE:" not in ch.content
        assert "var junk" not in ch.content

    def test_single_page_chapter_does_not_fetch_a_second_page(self, fixtures_dir):
        """If part 1's 'next part' link actually points at chapter 2, the
        merge must stop rather than absorbing the next chapter."""
        pages = dict(self._pages(fixtures_dir))
        pages["https://example.test/n/42/ch-1.html"] = (
            pages["https://example.test/n/42/ch-1.html"]
            .replace('href="/n/42/ch-1-part2.html"', 'href="/n/42/ch-2.html"')
        )
        s = _wire(ExampleChapterScraper(), pages)
        ch = _run(s.get_chapter_content("https://example.test/n/42/ch-1.html"))
        assert "third placeholder paragraph" not in ch.content, \
            "a 'next' link to chapter 2 must not be followed as part 2 of chapter 1"
