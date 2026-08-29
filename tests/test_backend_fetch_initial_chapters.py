"""
Stage 4: chapter-fetching consolidation. fetch_initial_chapters (invoked once,
right after a novel is added, to prefetch its first 5 chapters) used to hand-roll
its own per-chapter loop with no try/except around the scraper call and no shared
HTTP session for the batch — one chapter's transient fetch failure raised past the
loop and silently killed every chapter after it, with zero user-visible signal
(unlike every other _bg function, it never touched the BatchJob outcome system at
all). Fixed by delegating to fetch_chapters_range, which already gets this right.
"""
import asyncio

from database import SessionLocal
from models import Chapter


class _FakeChapterData:
    def __init__(self, content, word_count=10):
        self.content = content
        self.word_count = word_count


class _FlakyScraper:
    """Errors fetching chapter 2's content; succeeds for every other chapter."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_chapter_content(self, source_url):
        if "ch2" in source_url:
            raise RuntimeError("simulated transient fetch failure")
        return _FakeChapterData(content="content for %s" % source_url)


def test_one_flaky_chapter_does_not_silently_abort_the_rest(client, monkeypatch):
    import main as _main

    novel_id = client.post("/api/novels/manual",
                           json={"title": "Initial Fetch Check", "source_url": "manual://initial-fetch-1"}).json()["id"]

    db = SessionLocal()
    for n in range(1, 6):
        db.add(Chapter(novel_id=novel_id, chapter_number=n, title="ch%d" % n,
                       source_url="manual://initial-fetch-1/ch%d" % n))
    db.commit()
    db.close()

    monkeypatch.setattr("scrapers.get_scraper_for_url", lambda url: _FlakyScraper())
    monkeypatch.setattr(_main, "FETCH_DELAY_SECONDS", 0)

    asyncio.run(_main.fetch_initial_chapters(novel_id, auto_translate=False))

    db = SessionLocal()
    chapters = {c.chapter_number: c for c in db.query(Chapter).filter(Chapter.novel_id == novel_id).all()}
    db.close()

    fetched = {n for n, c in chapters.items() if c.original_content}
    assert fetched == {1, 3, 4, 5}, (
        "chapter 2's failure must not stop chapters 3-5 from being fetched — got %r" % fetched)
