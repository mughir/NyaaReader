"""
scrapers/ai.py's three-tier ladder for sites with no hand-written plugin:

    1. learned spec (cached)  -> the ordinary engine, 0 LLM calls
    2. inference               -> 1 LLM call, validated, cached
    3. text extraction         -> the old per-page LLM reading (last resort)

A canned LLM stands in for the relay, so this exercises the full pipeline —
inference, validation, caching, self-healing, and the reject-and-fall-through
path — with zero network calls and zero cost, against the same fully-invented
fixtures used by test_scraper_engine.py (see that file's docstring for why).
"""
import json
import os

import pytest

import scrapers.ai as ai
import scrapers.learn as learn

LIST_URL = "https://example.test/n/42/chapters"
CH_URL = "https://example.test/n/42/ch-1.html"

GOOD_LISTING_SPEC = {
    "chapter_links": "ul.chapters li a",
    "paginate": None,
    "language": "en",
    "meta": {
        "title": "h1.book-title",
        "author": "span.book-author",
        "desc": "#book-desc",
    },
}
GOOD_CHAPTER_SPEC = {
    "content": "#main-body",
    "chapter_title": "h1.chapter-heading",
}


class _FakeLLM:
    model_name = ""


def _make_scraper(fixtures_dir, reply_fn, calls):
    """An AIScraper wired to fixture pages, with _call_llm recording every
    prompt and answering via `reply_fn(prompt) -> str`."""
    listing = open(os.path.join(fixtures_dir, "example_listing_p1.html"), encoding="utf-8").read()
    ch = open(os.path.join(fixtures_dir, "example_chapter1.html"), encoding="utf-8").read()

    s = ai.AIScraper()
    s.session = object()  # never touched: _fetch below bypasses real HTTP

    async def fetch(url, headers=None):
        return ch if "ch-1" in url else listing
    s._fetch = fetch
    s._get_llm = lambda: _FakeLLM()

    async def call_llm(llm, prompt):
        calls.append(prompt[:80])
        return reply_fn(prompt)
    s._call_llm = call_llm
    return s


def _router(p):
    return (json.dumps(GOOD_CHAPTER_SPEC) if "chapter body" in p or "CHAPTER page" in p
            else json.dumps(GOOD_LISTING_SPEC))


@pytest.fixture()
def isolated_specs(tmp_path, monkeypatch):
    """Give every test its own DATA_DIR so cached specs from one test can
    never leak into another."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


class TestTier2Inference:
    def test_first_visit_infers_validates_and_caches(self, fixtures_dir, isolated_specs):
        calls = []
        s = _make_scraper(fixtures_dir, _router, calls)
        assert learn.load_spec(LIST_URL) is None

        info = _run(s.get_novel_info(LIST_URL))

        assert info is not None and info.chapters
        assert len(info.chapters) == 20
        assert info.title == "Example Book"
        assert len(calls) == 1, "exactly one LLM call should be needed"
        spec = learn.load_spec(LIST_URL)
        assert spec is not None and spec["chapter_links"] == "ul.chapters li a"


class TestTier1LearnedSpec:
    def test_second_visit_makes_zero_llm_calls(self, fixtures_dir, isolated_specs):
        calls = []
        _run(_make_scraper(fixtures_dir, _router, calls).get_novel_info(LIST_URL))
        calls.clear()

        info2 = _run(_make_scraper(fixtures_dir, _router, calls).get_novel_info(LIST_URL))

        assert len(info2.chapters) == 20
        assert calls == [], "a cached, still-valid spec must cost zero LLM calls"


class TestChapterInferenceAndReuse:
    def test_learns_content_selector_once_then_reuses_it(self, fixtures_dir, isolated_specs):
        calls = []
        s = _make_scraper(fixtures_dir, _router, calls)
        ch = _run(s.get_chapter_content(CH_URL))

        assert ch is not None and len(ch.content) > 100
        assert ch.word_count > 20
        assert len(calls) == 1

        calls.clear()
        ch2 = _run(_make_scraper(fixtures_dir, _router, calls).get_chapter_content(CH_URL))
        assert calls == []
        assert ch2.content == ch.content


class TestRejectionAndFallthrough:
    def test_a_hallucinated_selector_is_rejected_not_cached(self, fixtures_dir, isolated_specs):
        calls = []

        def bad_router(p):
            return json.dumps({"chapter_links": "div.totally-made-up a", "content": ".nope"})

        s = _make_scraper(fixtures_dir, bad_router, calls)
        info = _run(s.get_novel_info(LIST_URL))

        assert learn.load_spec(LIST_URL) is None, "a spec that fails validation must never be cached"
        assert len(calls) >= 2, "it must fall through to tier-3 text extraction"


class TestSelfHealing:
    def test_a_spec_that_stops_matching_is_re_inferred(self, fixtures_dir, isolated_specs):
        learn.save_spec(LIST_URL, {"chapter_links": "ul.gone-after-a-redesign li a"})
        calls = []

        s = _make_scraper(fixtures_dir, _router, calls)
        info = _run(s.get_novel_info(LIST_URL))

        assert len(info.chapters) == 20, "must recover after the stale spec fails validation"
        assert len(calls) == 1
        assert learn.load_spec(LIST_URL)["chapter_links"] == "ul.chapters li a", \
            "the stale spec on disk must be replaced"


def _run(coro):
    import asyncio
    return asyncio.run(coro)
