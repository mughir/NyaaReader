"""
Unit tests for SSE translation streaming endpoint in NyaaReader.
"""
from database import SessionLocal
from models import Chapter, Novel, NovelMemory
import json
from unittest.mock import patch, MagicMock
from translator import StreamChunk, MemoryTranslationResult, MemoryContext


class TestStreamingTranslation:
    def test_streaming_cached_chapter(self, client):
        """Streaming an already-translated chapter returns cached content immediately."""
        novel_id = client.post("/api/novels/manual", json={
            "title": "Stream Novel 1", "source_url": "manual://stream-1"
        }).json()["id"]

        db = SessionLocal()
        ch = Chapter(
            novel_id=novel_id,
            chapter_number=1,
            title="Raw Title",
            title_translated="Translated Title",
            original_content="原始文本",
            translated_content="Translated text content.",
            is_translated=True,
        )
        db.add(ch)
        db.commit()
        db.close()

        res = client.get(f"/api/novels/{novel_id}/chapters/1/translate/stream")
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]
        text = res.text
        assert "event: init" in text
        assert "event: delta" in text
        assert "event: done" in text
        assert "Translated text content." in text

    def test_streaming_untranslated_chapter(self, client):
        """Streaming an untranslated chapter streams deltas and updates DB."""
        novel_id = client.post("/api/novels/manual", json={
            "title": "Stream Novel 2", "source_url": "manual://stream-2"
        }).json()["id"]

        db = SessionLocal()
        ch = Chapter(
            novel_id=novel_id,
            chapter_number=1,
            title="第一章",
            original_content="这是原始文本。",
            translated_content=None,
            is_translated=False,
        )
        db.add(ch)
        db.commit()
        db.close()

        # Mock translator stream generator
        def fake_stream(*args, **kwargs):
            yield StreamChunk(delta="This is ", is_final=False)
            yield StreamChunk(delta="streamed text.", is_final=False)
            yield StreamChunk(
                is_final=True,
                result=MemoryTranslationResult(
                    translated_text="This is streamed text.",
                    model_used="test-model",
                    success=True,
                    memory=MemoryContext(characters="Hero (主角) - brave"),
                ),
            )

        with patch("main.get_translator") as mock_get_t:
            mock_t = MagicMock()
            mock_t.translate_with_memory_stream.side_effect = fake_stream
            mock_t.translate_short.return_value = "Chapter 1"
            mock_get_t.return_value = mock_t

            res = client.get(f"/api/novels/{novel_id}/chapters/1/translate/stream")
            assert res.status_code == 200
            text = res.text
            assert "event: init" in text
            assert "event: delta" in text
            assert "This is " in text
            assert "streamed text." in text
            assert "event: done" in text

        # Verify DB updated
        db = SessionLocal()
        ch_after = db.query(Chapter).filter(Chapter.novel_id == novel_id, Chapter.chapter_number == 1).first()
        assert ch_after.is_translated is True
        assert ch_after.translated_content == "This is streamed text."
        assert ch_after.title_translated == "Chapter 1"

        mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
        assert mem is not None
        assert "Hero" in mem.characters
        db.close()
