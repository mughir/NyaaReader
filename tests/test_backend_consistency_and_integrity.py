"""
Tests for end-to-end integration, consistency, and integrity fixes:
1. ScrapingLog cascading delete on Novel and disk artifact cleanup (covers, epub).
2. Translate-ahead avoiding gap jumps when immediate next chapter is already translated.
3. Translate-novel-meta batch total accuracy when single field needs translation.
4. NovelMemory glossary persistence and SQLAlchemy modification tracking.
5. fetch_chapters_range routing through _translate_chapter pipeline.
"""
import os
from pathlib import Path
import pytest

from database import SessionLocal
from models import Chapter, Novel, NovelMemory, ScrapingLog, BatchJob
import main as app_module
from routers.novels import DATA_DIR
from services.export_service import _epub_path


class TestNovelDeletionIntegrityAndArtifactCleanup:
    def test_delete_novel_cascades_scraping_logs_and_cleans_disk(self, client):
        # 1. Create novel
        res = client.post("/api/novels/manual", json={"title": "Delete Me", "source_url": "manual://delete-me-1"})
        novel_id = res.json()["id"]

        # 2. Add ScrapingLog row and disk artifacts
        db = SessionLocal()
        try:
            log = ScrapingLog(novel_id=novel_id, chapter_number=1, status="success", response_time=0.5)
            db.add(log)
            db.commit()
        finally:
            db.close()

        covers_dir = DATA_DIR / "covers"
        covers_dir.mkdir(parents=True, exist_ok=True)
        cover_file = covers_dir / f"novel_{novel_id}.png"
        cover_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        epub_dir = DATA_DIR / "epub"
        epub_dir.mkdir(parents=True, exist_ok=True)
        epub_file = epub_dir / f"novel_{novel_id}.epub"
        epub_file.write_text("fake epub", encoding="utf-8")

        # Set a batch cache entry
        app_module._batch_cache[novel_id] = {"running": True, "kind": "to-end"}

        # 3. Call DELETE /api/novels/{novel_id}
        del_res = client.delete(f"/api/novels/{novel_id}")
        assert del_res.status_code == 200
        assert del_res.json() == {"status": "deleted"}

        # 4. Verify DB rows are gone without FK IntegrityError
        db = SessionLocal()
        try:
            assert db.query(Novel).filter(Novel.id == novel_id).first() is None
            assert db.query(ScrapingLog).filter(ScrapingLog.novel_id == novel_id).first() is None
        finally:
            db.close()

        # 5. Verify disk artifacts are removed
        assert not cover_file.exists()
        assert not epub_file.exists()
        assert novel_id not in app_module._batch_cache


class TestTranslateAheadImmediateNextTranslated:
    def test_translate_ahead_returns_none_if_immediate_next_is_translated(self, client):
        novel_id = client.post(
            "/api/novels/manual",
            json={"title": "Ahead Gap", "source_url": "manual://ahead-gap-1"}
        ).json()["id"]

        db = SessionLocal()
        try:
            # Ch 1 translated, Ch 2 translated, Ch 3 untranslated
            db.add(Chapter(novel_id=novel_id, chapter_number=1, title="ch1",
                           original_content="c1", translated_content="t1", is_translated=True))
            db.add(Chapter(novel_id=novel_id, chapter_number=2, title="ch2",
                           original_content="c2", translated_content="t2", is_translated=True))
            db.add(Chapter(novel_id=novel_id, chapter_number=3, title="ch3",
                           original_content="c3", is_translated=False))
            db.commit()
        finally:
            db.close()

        # When on chapter 1, immediate next chapter (2) is already translated: should NOT queue
        res = client.post(f"/api/novels/{novel_id}/translate-ahead?after_chapter=1&count=5")
        assert res.status_code == 200
        assert res.json() == {"status": "none", "pending": 0}

        # When on chapter 2, immediate next chapter (3) is raw: should queue Ch 3
        res2 = client.post(f"/api/novels/{novel_id}/translate-ahead?after_chapter=2&count=5")
        assert res2.status_code == 200
        assert res2.json() == {"status": "started", "pending": 1}


class TestTranslateNovelMetaTotalAndOutcome:
    def test_meta_batch_total_and_label_for_single_field(self, client, monkeypatch):
        novel_id = client.post(
            "/api/novels/manual",
            json={"title": "Raw Title", "source_url": "manual://raw-title-meta-1"}
        ).json()["id"]

        class _FakeTranslator:
            def translate_short(self, text, *a, **k):
                return f"Translated {text}"

        import translator as translator_module
        monkeypatch.setattr(translator_module, "get_translator", lambda *a, **k: _FakeTranslator())

        app_module.translate_novel_meta_bg(novel_id)

        db = SessionLocal()
        try:
            novel = db.query(Novel).filter(Novel.id == novel_id).first()
            assert novel.title_translated == "Translated Raw Title"
            job = db.query(BatchJob).filter(BatchJob.novel_id == novel_id).order_by(BatchJob.id.desc()).first()
            assert job is not None
            assert job.total == 1
            assert job.done == 1
            assert job.current_label == "Title translated"
        finally:
            db.close()


class TestNovelMemoryGlossaryFlagModified:
    def test_put_memory_persists_glossary_entries(self, client):
        novel_id = client.post(
            "/api/novels/manual",
            json={"title": "Mem Test", "source_url": "manual://mem-test-1"}
        ).json()["id"]

        glossary_payload = [
            {"type": "character", "source": "\u738b\u6797", "translated": "Wang Lin", "note": "MC", "locked": True}
        ]
        res = client.put(f"/api/novels/{novel_id}/memory", json={"glossary_entries": glossary_payload})
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}

        get_res = client.get(f"/api/novels/{novel_id}/memory")
        assert get_res.status_code == 200
        entries = get_res.json()["glossary_entries"]
        assert len(entries) == 1
        assert entries[0]["translated"] == "Wang Lin"
        assert entries[0]["locked"] is True


class TestFetchChaptersRangeConsistency:
    def test_fetch_chapters_range_with_translation_updates_memory(self, client, monkeypatch):
        import asyncio
        from services.novel_service import fetch_chapters_range
        from translator import MemoryContext, MemoryTranslationResult
        import translator as translator_module

        novel_id = client.post(
            "/api/novels/manual",
            json={"title": "Range Trans", "source_url": "manual://range-trans-1"}
        ).json()["id"]

        db = SessionLocal()
        try:
            db.add(Chapter(novel_id=novel_id, chapter_number=1, title="ch1",
                           source_url="manual://range-trans-1/ch1"))
            db.commit()
        finally:
            db.close()

        class _FakeScraper:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get_chapter_content(self, source_url):
                class _Data:
                    content = "raw chapter 1 text"
                    word_count = 100
                return _Data()

        class _FakeTranslator:
            def translate_with_memory(self, content, original_lang, target_lang, quality, memory, session_id):
                updated_mem = MemoryContext(
                    characters="Protagonist: Lin",
                    terms="Term: Cultivation",
                    plot="Chapter 1 story",
                )
                return MemoryTranslationResult(
                    translated_text="translated chapter 1 text",
                    success=True,
                    model_used="test-model",
                    memory=updated_mem,
                )
            def translate_short(self, text, *a, **k):
                return f"Trans {text}"

        monkeypatch.setattr("scrapers.get_scraper_for_url", lambda url: _FakeScraper())
        monkeypatch.setattr(translator_module, "get_translator", lambda *a, **k: _FakeTranslator())
        monkeypatch.setattr(app_module, "FETCH_DELAY_SECONDS", 0)

        asyncio.run(fetch_chapters_range(novel_id, start=1, count=1, do_translate=True))

        db = SessionLocal()
        try:
            ch = db.query(Chapter).filter(Chapter.novel_id == novel_id, Chapter.chapter_number == 1).first()
            assert ch.is_translated is True
            assert ch.translated_content == "translated chapter 1 text"
            assert ch.title_translated == "Trans ch1"

            mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
            assert mem is not None
            assert "Protagonist: Lin" in mem.characters
            assert "Term: Cultivation" in mem.terms
            assert "Chapter 1 story" in mem.plot
        finally:
            db.close()

