"""
scrapers/ai.py — the pure helpers, and the three bugs found in the old
text-flattening fallback path (still the tier-3 last resort, see
test_scraper_ai_tiers.py for the tiered pipeline around it).
"""
from bs4 import BeautifulSoup

import scrapers.ai as ai


class TestWordCount:
    def test_counts_cjk_characters(self):
        assert ai._count_words("你好世界") == 4

    def test_counts_words_for_non_cjk(self):
        assert ai._count_words("hello there world") == 3

    def test_empty_string(self):
        assert ai._count_words("") == 0


class TestChunking:
    def test_short_text_is_a_single_chunk(self):
        text = "para one\n\npara two"
        assert ai._chunk_for_extraction(text, 100) == [text]

    def test_long_text_is_split_losslessly(self):
        paras = ["p%03d " % i + "x" * 90 for i in range(60)]
        long_text = "\n\n".join(paras)
        chunks = ai._chunk_for_extraction(long_text, 1000)
        assert len(chunks) > 1
        assert all(len(c) <= 1000 for c in chunks)
        assert "\n\n".join(chunks) == long_text, "no content may be lost or reordered"

    def test_a_monster_single_paragraph_is_hard_split_not_dropped(self):
        mono = "y" * 2500
        chunks = ai._chunk_for_extraction(mono, 1000)
        assert "".join(chunks) == mono
        assert all(len(c) <= 1000 for c in chunks)

    def test_empty_input(self):
        assert ai._chunk_for_extraction("", 100) == []


class TestClassTokenFix:
    """CONTENT_HINTS used to match SUBSTRINGS, so the Bulma utility class
    `is-justify-content-center` counted as a "content" hint and let a
    25-character div outrank a 540-chapter list. Tokens are now compared
    whole."""

    def test_utility_class_is_not_a_content_hint(self):
        node = BeautifulSoup('<div class="is-justify-content-center">x</div>', "lxml").div
        toks = ai._class_tokens(node)
        assert not any(h in toks for h in ai.CONTENT_HINTS)

    def test_a_real_content_class_still_matches(self):
        node = BeautifulSoup('<div class="chapter-content">x</div>', "lxml").div
        toks = ai._class_tokens(node)
        assert any(h in toks for h in ai.CONTENT_HINTS)

    def test_node_with_no_attrs_does_not_raise(self):
        # some parsed nodes (e.g. NavigableString-adjacent) can have attrs=None
        node = BeautifulSoup("<div>x</div>", "lxml").div
        node.attrs = None
        assert ai._class_tokens(node) == set()


class TestVisibleTextScoring:
    """The scoring bug: the content-hint bonus (+20) equalled the capped
    text-length score (also 20), so any tiny hinted element tied with the
    real chapter list and won purely on document order."""

    def test_the_larger_real_content_block_wins_over_a_tiny_hinted_decoy(self):
        html = (
            '<html><body>'
            '<div class="is-justify-content-center">tiny decoy</div>'
            '<div class="chapter-body">' + ("real chapter prose " * 200) + '</div>'
            '</body></html>'
        )
        soup = BeautifulSoup(html, "lxml")
        text = ai._visible_text(soup)
        assert "real chapter prose" in text
        assert "tiny decoy" not in text
