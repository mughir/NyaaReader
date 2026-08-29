"""
scrapers/learn.py — the digest / validate / persistence machinery behind
"unknown sites configure themselves". No LLM involved: these test the
deterministic half of the pipeline, which is also the half doing the actual
safety work (nothing the model proposes is trusted before this runs).

Uses the same fully-invented fixtures as test_scraper_engine.py (see that
file's docstring for why these tests never import a real site plugin).
"""
import json
import os

import pytest
from bs4 import BeautifulSoup

import scrapers.learn as learn
from scrapers.spec import QueryPage

LISTING_URL = "https://example.test/n/42/chapters"
CHAPTER_URL = "https://example.test/n/42/ch-1.html"


def _soup(fixtures_dir, name):
    html = open(os.path.join(fixtures_dir, name), encoding="utf-8").read()
    return BeautifulSoup(html, "lxml"), html


class TestLinkClustering:
    """The ranking rule that decides which link cluster on a page IS the
    chapter list. Both halves of the rule are load-bearing against the
    fixture's trap: raw link count would pick an ancestor wrapper, and
    same-shaped count alone would pick the descending 'recent' decoy —
    which would number a novel backwards."""

    def test_picks_the_full_list_over_the_decoy(self, fixtures_dir):
        soup, _ = _soup(fixtures_dir, "example_listing_p1.html")
        clusters = learn.link_clusters(soup)
        assert clusters, "must find at least one cluster"
        best = clusters[0]
        assert best["homogeneous"] == 20
        assert best["links"][0].get("href").endswith("ch-1.html"), \
            "the winning cluster must be in ASCENDING (real) order, not the descending decoy"

    def test_decoy_is_still_visible_as_a_lower_ranked_candidate(self, fixtures_dir):
        soup, _ = _soup(fixtures_dir, "example_listing_p1.html")
        clusters = learn.link_clusters(soup)
        sizes = sorted(c["homogeneous"] for c in clusters)
        assert 3 in sizes, "the decoy cluster should still appear (for the LLM to see), just ranked lower"

    def test_best_cluster_size_matches_the_top_ranked_cluster(self, fixtures_dir):
        soup, _ = _soup(fixtures_dir, "example_listing_p1.html")
        assert learn.best_cluster_size(soup) == 20


class TestDigest:
    """The structural digest fed to the LLM. AIScraper's old text-flattening
    produced 29 characters for a real 540-chapter listing; the digest must be
    both small and actually informative."""

    def test_listing_digest_surfaces_the_real_list_first(self, fixtures_dir):
        soup, _ = _soup(fixtures_dir, "example_listing_p1.html")
        d = learn.digest_listing(soup, LISTING_URL)
        assert d["link_clusters"], "digest must not be empty"
        assert d["link_clusters"][0]["links"] == 20

    def test_listing_digest_is_compact(self, fixtures_dir):
        soup, _ = _soup(fixtures_dir, "example_listing_p1.html")
        d = learn.digest_listing(soup, LISTING_URL)
        assert len(json.dumps(d)) < 4000

    def test_listing_digest_exposes_meta_tags(self, fixtures_dir):
        soup, _ = _soup(fixtures_dir, "example_listing_p1.html")
        d = learn.digest_listing(soup, LISTING_URL)
        assert any(k.startswith("og:") for k in d["meta_tags"])

    def test_listing_digest_exposes_pagination_candidates(self, fixtures_dir):
        soup, _ = _soup(fixtures_dir, "example_listing_p1.html")
        d = learn.digest_listing(soup, LISTING_URL)
        hrefs = [p["href"] for p in d["pagination_candidates"]]
        assert any("page=2" in h for h in hrefs)

    def test_chapter_digest_ranks_the_body_over_short_notes(self, fixtures_dir):
        soup, _ = _soup(fixtures_dir, "example_chapter1.html")
        d = learn.digest_chapter(soup, CHAPTER_URL)
        assert d["text_blocks"], "digest must not be empty"
        assert d["text_blocks"][0]["text_len"] > 50


class TestValidation:
    """Nothing an LLM proposes is cached before it passes these checks."""

    def test_accepts_a_correct_listing_spec(self, fixtures_dir):
        soup, _ = _soup(fixtures_dir, "example_listing_p1.html")
        scraper = learn.build_scraper({"chapter_links": "ul.chapters li a"})
        ok, why, n = learn.validate_listing(scraper, soup, LISTING_URL)
        assert ok and n == 20, why

    def test_rejects_the_recent_chapters_trap(self, fixtures_dir):
        """A spec that (wrongly) targets only the small 'recent' list."""
        soup, _ = _soup(fixtures_dir, "example_listing_p1.html")
        scraper = learn.build_scraper({"chapter_links": "ul.recent li a"})
        ok, why, n = learn.validate_listing(scraper, soup, LISTING_URL)
        assert not ok
        assert n == 3

    def test_rejects_a_selector_matching_nothing(self, fixtures_dir):
        soup, _ = _soup(fixtures_dir, "example_listing_p1.html")
        scraper = learn.build_scraper({"chapter_links": "div.does-not-exist a"})
        ok, why, _ = learn.validate_listing(scraper, soup, LISTING_URL)
        assert not ok

    def test_rejects_offsite_links(self):
        soup = BeautifulSoup(
            "<html><body><ul class='x'>" +
            "".join('<li><a href="https://other.example/%d">ch%d</a></li>' % (i, i) for i in range(10)) +
            "</ul></body></html>", "lxml")
        scraper = learn.build_scraper({"chapter_links": "ul.x li a"})
        ok, why, _ = learn.validate_listing(scraper, soup, LISTING_URL)
        assert not ok

    def test_accepts_the_real_content_selector(self, fixtures_dir):
        soup, _ = _soup(fixtures_dir, "example_chapter1.html")
        scraper = learn.build_scraper({"content": "#main-body"})
        ok, why, n = learn.validate_content(scraper, soup)
        assert ok, why
        assert n > 20

    def test_rejects_a_navigation_block_as_content(self, fixtures_dir):
        soup, _ = _soup(fixtures_dir, "example_chapter1.html")
        scraper = learn.build_scraper({"content": "a.next-part"})
        ok, why, _ = learn.validate_content(scraper, soup)
        assert not ok


class TestSpecBuildAndPaginate:
    def test_paginate_dict_becomes_a_query_page(self):
        scraper = learn.build_scraper({"chapter_links": "a", "paginate": {"type": "query", "param": "p"}})
        assert isinstance(scraper.paginate, QueryPage)
        assert scraper.paginate.param == "p"

    def test_missing_paginate_defaults_to_single_page(self):
        from scrapers.spec import SinglePage
        scraper = learn.build_scraper({"chapter_links": "a"})
        assert isinstance(scraper.paginate, SinglePage)


class TestPersistence:
    def test_round_trips_through_disk(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        spec = {"chapter_links": "ul.chapters li a", "language": "en"}
        path = learn.save_spec("https://example.test/x", spec)
        assert os.path.exists(path)
        back = learn.load_spec("https://example.test/other/page")
        assert back is not None and back["chapter_links"] == "ul.chapters li a", \
            "spec is stored per-DOMAIN, so a different path on the same site must still resolve it"

    def test_unknown_domain_has_no_spec(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert learn.load_spec("https://never-seen.example/x") is None

    def test_forget_spec_removes_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        learn.save_spec("https://example.test/x", {"chapter_links": "a"})
        assert learn.forget_spec("https://example.test/x") is True
        assert learn.load_spec("https://example.test/x") is None

    def test_stored_file_is_readable_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        path = learn.save_spec("https://example.test/x", {"chapter_links": "a"})
        assert open(path, encoding="utf-8").read().lstrip().startswith("{")


class TestReplyParsing:
    def test_parses_fenced_json(self):
        assert learn.parse_spec_reply('```json\n{"a": 1}\n```') == {"a": 1}

    def test_parses_json_with_surrounding_prose(self):
        assert learn.parse_spec_reply("Sure, here it is:\n{\"a\": 2}\nhope that helps") == {"a": 2}

    def test_returns_none_on_garbage(self):
        assert learn.parse_spec_reply("no json here") is None

    def test_returns_none_on_empty(self):
        assert learn.parse_spec_reply("") is None
