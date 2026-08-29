"""
Stage 4: scrapers/ai.py's tier-3 text-extraction path (_llm_novel_info) used
to send only the first MAX_TEXT_CHARS (18000) characters of a listing page's
visible text to the model -- on any novel long enough that its chapter list
alone exceeds that budget, every chapter past the cutoff was invisible to the
model. The structural real_links fallback (learn.link_clusters) already
protects most real sites, since it scans the untruncated DOM directly -- but
when a site's markup doesn't cluster cleanly, real_links comes back empty and
the model's own (truncated) chapter list was the only source of chapters at
all. This test constructs exactly that case: every chapter href has a
unique, non-repeating shape (see _letters()), so link_clusters finds nothing
and the fix (chunking the listing text like _llm_chapter already does for
chapter bodies) is the only thing that can recover every chapter.
"""
import json
import re

from bs4 import BeautifulSoup

import scrapers.ai as ai

BASE_URL = "https://example.test/n/big-novel/chapters"
NUM_CHAPTERS = 900


def _letters(i: int) -> str:
    """Spreadsheet-column-style letters-only encoding (a, b, ..., z, aa, ab, ...) --
    globally unique per i and contains no digits, so _href_shape() never
    collapses two of these hrefs to the same shape (defeats link clustering
    on purpose, to isolate the llm_chapters-only code path)."""
    s = ""
    n = i
    while True:
        s = chr(97 + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def _make_long_listing_html() -> str:
    links = "".join(
        '<a href="/%s/read.html">Chapter %d: Padding Words To Make This Title Nice And Long</a><br>'
        % (_letters(i), i)
        for i in range(1, NUM_CHAPTERS + 1)
    )
    return "<html><body><h1>Big Novel</h1><p>A very long novel.</p><div class='list'>%s</div></body></html>" % links


class _FakeLLM:
    model_name = ""


def _router(prompt: str) -> str:
    """Stands in for the relay: returns exactly the chapters whose
    'Chapter N:' line is present in THIS pass's PAGE TEXT slice."""
    text = prompt.split("PAGE TEXT:\n", 1)[1]
    found = re.findall(r"Chapter (\d+):", text)
    chapters = [{"title": "Title %s" % n,
                "url": "https://example.test/%s/read.html" % _letters(int(n))}
               for n in found]
    return json.dumps({"title": "Big Novel", "author": "Someone", "description": "desc",
                       "chapters": chapters})


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_a_long_listing_page_is_not_silently_truncated():
    html = _make_long_listing_html()
    soup = BeautifulSoup(html, "html.parser")
    assert len(soup.get_text()) > ai.MAX_TEXT_CHARS * 2, \
        "test setup should produce a listing far bigger than one extraction pass"

    calls = []
    s = ai.AIScraper()
    s._get_llm = lambda: _FakeLLM()

    async def call_llm(llm, prompt):
        calls.append(prompt)
        return _router(prompt)
    s._call_llm = call_llm

    info = _run(s._llm_novel_info(BASE_URL, soup))

    assert info is not None
    assert len(calls) > 1, "the listing text must be long enough to require more than one extraction pass"
    assert info.total_chapters == NUM_CHAPTERS, (
        "every chapter must survive the split across passes, not just the first %d chars worth"
        % ai.MAX_TEXT_CHARS)
    numbers = sorted(int(m.group(1)) for c in info.chapters
                     for m in [re.search(r"(\d+)", c.title)] if m)
    assert numbers == list(range(1, NUM_CHAPTERS + 1))
