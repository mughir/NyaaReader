"""
backend/main.py's check_updates_bg: three bugs found together.

1. It translated the LOWEST-numbered untranslated chapter in the whole novel
   instead of the ones just discovered (e.g. ch 37-41 on a novel with new
   chapters 517-540), because the follow-up query wasn't scoped to the new
   range.
2. novel.total_chapters was counted BEFORE the pending inserts were flushed
   (SessionLocal uses autoflush=False), so it was written as the pre-insert
   number.
3. Every failure path was a bare `return`, indistinguishable from "no new
   chapters" — the underlying incident (a private site plugin raising
   NameError on every call) looked identical to a healthy check that simply
   found nothing.
"""
import main as app_module
import scrapers
from database import SessionLocal
from models import BatchJob, Chapter, Novel
from scrapers.base import ChapterData, NovelInfo


def _seed_novel(client, title, source_url, n_chapters, n_translated):
    created = client.post("/api/novels/manual", json={"title": title, "source_url": source_url}).json()
    novel_id = created["id"]
    db = SessionLocal()
    db.query(Novel).filter(Novel.id == novel_id).update({"source_site": "test-site.example"})
    for i in range(1, n_chapters + 1):
        db.add(Chapter(
            novel_id=novel_id, chapter_number=i,
            source_url="https://test-site.example/%d/ch_%d.html" % (novel_id, i),
            title="ch%d" % i, is_translated=(i <= n_translated),
            original_content="x" if i <= n_translated else None,
            translated_content="y" if i <= n_translated else None,
        ))
    db.commit()
    db.close()
    return novel_id


class _FakeScraper:
    """Answers get_novel_info() with a fixed, larger chapter list."""
    def __init__(self, base_url, total):
        self.base_url = base_url
        self.total = total

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_novel_info(self, url):
        chapters = [
            ChapterData(number=i, title="ch%d" % i,
                       url="%s/ch_%d.html" % (self.base_url, i), content="")
            for i in range(1, self.total + 1)
        ]
        return NovelInfo(title="t", chapters=chapters, total_chapters=len(chapters))


class TestCheckUpdatesScopesToNewChapters:
    def test_only_the_newly_discovered_chapters_are_queued_for_translation(self, client, monkeypatch):
        # Chapters 6..40 are PRE-EXISTING and untranslated on purpose: the bug
        # was an unscoped "first 5 untranslated in the whole novel" query,
        # which would pick these old ones instead of the newly-found 41..45.
        novel_id = _seed_novel(client, "Scoped Update", "manual://scoped-update-1",
                              n_chapters=40, n_translated=5)
        base = "https://test-site.example/%d" % novel_id
        fake = _FakeScraper(base, total=50)  # source now has 10 new chapters (41..50)
        attempted = []
        # check_updates_bg does `from scrapers import get_scraper_for_url`
        # INSIDE the function, so the patch target is the scrapers package
        # attribute (re-looked-up on every call), not main.get_scraper_for_url.
        monkeypatch.setattr(scrapers, "get_scraper_for_url", lambda url: fake)
        # Content must actually populate for the flow to reach the translate
        # step at all — a bare `None` return makes `if ch.original_content`
        # false and the chapter gets silently skipped either way, which would
        # make this test pass for the wrong reason.
        monkeypatch.setattr(app_module, "_fetch_chapter_content_sync",
                            lambda url, polite_delay=True: ChapterData(number=0, title="", content="fetched body", url=url, word_count=2))
        monkeypatch.setattr(app_module, "_translate_chapter_bg",
                            lambda nid, chapter_number, quality="balanced": attempted.append(chapter_number))

        app_module.check_updates_bg(novel_id)

        db = SessionLocal()
        total_chapters = db.query(Chapter).filter(Chapter.novel_id == novel_id).count()
        newest = db.query(Chapter).filter(Chapter.novel_id == novel_id).order_by(
            Chapter.chapter_number.desc()).first()
        db.close()
        assert total_chapters == 50, "10 new chapters must be added"
        assert newest.chapter_number == 50
        assert attempted, "the newly-added chapters must actually be queued for translation"
        assert all(n >= 41 for n in attempted), \
            "must translate the NEWLY FOUND chapters (41+), not the 35 pre-existing untranslated ones (6-40): %r" % attempted


class TestTotalChaptersFlush:
    """autoflush=False means a count() issued right after db.add() but before
    flush() misses the pending rows."""

    def test_total_chapters_reflects_the_post_insert_count(self, client, monkeypatch):
        novel_id = _seed_novel(client, "Flush Check", "manual://flush-check-1",
                              n_chapters=10, n_translated=2)
        base = "https://test-site.example/%d" % novel_id
        fake = _FakeScraper(base, total=13)  # 3 new chapters
        # check_updates_bg does `from scrapers import get_scraper_for_url`
        # INSIDE the function, so the patch target is the scrapers package
        # attribute (re-looked-up on every call), not main.get_scraper_for_url.
        monkeypatch.setattr(scrapers, "get_scraper_for_url", lambda url: fake)
        monkeypatch.setattr(app_module, "_fetch_chapter_content_sync", lambda url, polite_delay=True: None)
        monkeypatch.setattr(app_module, "_translate_chapter_bg", lambda *a, **k: None)

        app_module.check_updates_bg(novel_id)

        nv = client.get("/api/novels/%d" % novel_id).json()
        db = SessionLocal()
        real = db.query(Chapter).filter(Chapter.novel_id == novel_id).count()
        db.close()
        assert nv["total_chapters"] == real == 13, \
            "total_chapters must be the POST-insert count, not the pre-insert 10"


class TestOutcomeIsAlwaysReported:
    def test_no_new_chapters_reports_a_reason_not_silence(self, client, monkeypatch):
        novel_id = _seed_novel(client, "Nothing New", "manual://nothing-new-1",
                              n_chapters=5, n_translated=5)
        base = "https://test-site.example/%d" % novel_id
        fake = _FakeScraper(base, total=5)  # nothing new
        # check_updates_bg does `from scrapers import get_scraper_for_url`
        # INSIDE the function, so the patch target is the scrapers package
        # attribute (re-looked-up on every call), not main.get_scraper_for_url.
        monkeypatch.setattr(scrapers, "get_scraper_for_url", lambda url: fake)

        app_module.check_updates_bg(novel_id)

        db = SessionLocal()
        job = db.query(BatchJob).filter(BatchJob.novel_id == novel_id).order_by(BatchJob.id.desc()).first()
        db.close()
        assert job is not None
        assert job.current_label == "No new chapters"

    def test_a_scraper_that_cannot_read_the_list_reports_why(self, client, monkeypatch):
        novel_id = _seed_novel(client, "Broken Scrape", "manual://broken-scrape-1",
                              n_chapters=5, n_translated=5)

        class _BrokenScraper(_FakeScraper):
            async def get_novel_info(self, url):
                return None

        monkeypatch.setattr(scrapers, "get_scraper_for_url",
                            lambda url: _BrokenScraper("https://x", 0))

        app_module.check_updates_bg(novel_id)

        db = SessionLocal()
        job = db.query(BatchJob).filter(BatchJob.novel_id == novel_id).order_by(BatchJob.id.desc()).first()
        db.close()
        assert job is not None
        assert "Could not read" in job.current_label, \
            "a broken scrape must not be reported identically to 'no new chapters'"

    def test_a_successful_update_reports_how_many_were_added(self, client, monkeypatch):
        novel_id = _seed_novel(client, "Reports Count", "manual://reports-count-1",
                              n_chapters=5, n_translated=5)
        base = "https://test-site.example/%d" % novel_id
        monkeypatch.setattr(scrapers, "get_scraper_for_url", lambda url: _FakeScraper(base, total=8))
        monkeypatch.setattr(app_module, "_fetch_chapter_content_sync", lambda url, polite_delay=True: None)
        monkeypatch.setattr(app_module, "_translate_chapter_bg", lambda *a, **k: None)

        app_module.check_updates_bg(novel_id)

        db = SessionLocal()
        job = db.query(BatchJob).filter(BatchJob.novel_id == novel_id).order_by(BatchJob.id.desc()).first()
        db.close()
        assert "Added 3" in job.current_label

    def test_check_updates_can_be_cancelled_by_user(self, client, monkeypatch):
        novel_id = _seed_novel(client, "Cancel Check", "manual://cancel-check-1",
                              n_chapters=5, n_translated=5)
        base = "https://test-site.example/%d" % novel_id
        monkeypatch.setattr(scrapers, "get_scraper_for_url", lambda url: _FakeScraper(base, total=10))
        monkeypatch.setattr(app_module, "_fetch_chapter_content_sync",
                            lambda url, polite_delay=True: ChapterData(number=0, title="", content="txt", url=url, word_count=1))
        
        # Request batch stop during the first chapter translation
        def _mock_translate(nid, ch_num, quality="balanced"):
            app_module._request_batch_stop(nid)

        monkeypatch.setattr(app_module, "_translate_chapter_bg", _mock_translate)

        app_module.check_updates_bg(novel_id)

        db = SessionLocal()
        job = db.query(BatchJob).filter(BatchJob.novel_id == novel_id).order_by(BatchJob.id.desc()).first()
        db.close()
        assert job is not None
        assert "Stopped by user" in job.current_label

