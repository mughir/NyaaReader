"""
Stage 4: five legacy plain-HTML-form routes were removed (POST /add, POST
/novel/{id}/chapter/{n}/translate, POST /novel/{id}/chapter/{n}/fetch, POST
/novel/{id}/fetch-more, POST /novel/{id}/delete). The app has been Vue-driven
since the multi-page rewrite; a repo-wide grep found zero references to any
of them outside their own route definitions, and delete_novel_page in
particular bulk-deleted 4 tables directly (bypassing the ORM's own cascade on
Novel) while still missing bookmarks/diary_entries/batch_jobs. Duplicate,
unreachable, and already wrong is not something to fix — it's something to
delete. The JSON API is the one real path for each.
"""
from database import SessionLocal
from models import BatchJob, Bookmark, Chapter, DiaryEntry, ReadingProgress


class TestLegacyRoutesAreGone:
    def test_all_five_dead_routes_return_404(self, client):
        for method, path in (
            ("post", "/add"),
            ("post", "/novel/1/chapter/1/translate"),
            ("post", "/novel/1/chapter/1/fetch"),
            ("post", "/novel/1/fetch-more"),
            ("post", "/novel/1/delete"),
        ):
            r = getattr(client, method)(path)
            assert r.status_code == 404, "%s %s should no longer exist" % (method.upper(), path)


class TestApiDeleteNovelCascadesEverything:
    """The bug the dead HTML-form path had: it manually bulk-deleted 4 tables
    and still missed bookmarks, diary entries and batch jobs. The API path
    relies on the ORM's own cascade relationships on Novel, which cover all
    of them."""

    def test_delete_removes_every_related_row(self, client):
        novel_id = client.post("/api/novels/manual",
                               json={"title": "Cascade Check", "source_url": "manual://cascade-check-1"}).json()["id"]
        db = SessionLocal()
        ch = Chapter(novel_id=novel_id, chapter_number=1, title="ch1",
                    original_content="x", translated_content="y", is_translated=True)
        db.add(ch)
        db.commit()
        db.add(Bookmark(novel_id=novel_id, chapter_id=ch.id, chapter_number=1, quote="a quote"))
        db.add(DiaryEntry(novel_id=novel_id, chapter_id=ch.id, chapter_number=1, content="notes"))
        db.add(ReadingProgress(novel_id=novel_id, chapter_id=ch.id, scroll_position=10))
        db.add(BatchJob(novel_id=novel_id, kind="to-end", total=1, done=0, running=False))
        db.commit()
        db.close()

        r = client.delete("/api/novels/%d" % novel_id)
        assert r.status_code == 200

        db = SessionLocal()
        counts = {
            "chapters": db.query(Chapter).filter(Chapter.novel_id == novel_id).count(),
            "bookmarks": db.query(Bookmark).filter(Bookmark.novel_id == novel_id).count(),
            "diary_entries": db.query(DiaryEntry).filter(DiaryEntry.novel_id == novel_id).count(),
            "reading_progress": db.query(ReadingProgress).filter(ReadingProgress.novel_id == novel_id).count(),
            "batch_jobs": db.query(BatchJob).filter(BatchJob.novel_id == novel_id).count(),
        }
        db.close()
        assert counts == {k: 0 for k in counts}, \
            "every related table must be empty after delete: %r" % counts
