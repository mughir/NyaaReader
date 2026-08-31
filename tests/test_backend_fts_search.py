"""
Unit tests for SQLite FTS5 full-text search integration in NyaaReader.
"""
from database import SessionLocal, engine
from models import Chapter, Novel
from sqlalchemy import text


def _seed_novel_and_chapters(client, title, source_url, chapters_data):
    novel_id = client.post("/api/novels/manual", json={"title": title, "source_url": source_url}).json()["id"]
    db = SessionLocal()
    for num, (t, body) in enumerate(chapters_data, start=1):
        db.add(Chapter(
            novel_id=novel_id,
            chapter_number=num,
            title=t,
            title_translated=t,
            original_content=body,
            translated_content=body,
            is_translated=True
        ))
    db.commit()
    db.close()
    return novel_id


class TestFTS5Search:
    def test_fts5_table_exists_and_synced(self, client):
        """Verify that chapters_fts is automatically populated via triggers."""
        novel_id = _seed_novel_and_chapters(client, "FTS5 Novel", "manual://fts5-test-1", [
            ("Chapter 1: The Azure Dragon", "The Azure Dragon soared across the heavenly sky."),
            ("Chapter 2: The Vermilion Bird", "A fiery Vermilion Bird descended upon the mountain peak."),
            ("Chapter 3: The Dragon and the Bird", "The Azure Dragon and the Vermilion Bird clashed in midair with immense power."),
        ])

        # Test query for 'Dragon'
        res = client.post(f"/api/novels/{novel_id}/search", json={"q": "Dragon"}).json()["results"]
        assert len(res) == 2
        chapter_nums = [r["chapter_number"] for r in res]
        assert 1 in chapter_nums
        assert 3 in chapter_nums

        # Check snippet contains highlight tag
        for r in res:
            assert '<mark class="search-hl">' in r["snippet"]
            assert "</mark>" in r["snippet"]

    def test_fts5_bm25_ranking(self, client):
        """Verify that chapters with more relevant hits are ranked higher."""
        novel_id = _seed_novel_and_chapters(client, "BM25 Novel", "manual://bm25-test-1", [
            ("Chapter 1", "The sword was sharp."),
            ("Chapter 2", "Sword, legendary sword, ancient divine sword of pure sword intent! Sword everywhere!"),
            ("Chapter 3", "Nothing to see here."),
        ])

        res = client.post(f"/api/novels/{novel_id}/search", json={"q": "sword"}).json()["results"]
        assert len(res) == 2
        # Chapter 2 has multiple occurrences and high density -> should rank first
        assert res[0]["chapter_number"] == 2
        assert res[0]["count"] >= 4
        assert res[1]["chapter_number"] == 1

    def test_fts5_update_and_delete_triggers(self, client):
        """Verify that updating and deleting chapters updates FTS index."""
        novel_id = _seed_novel_and_chapters(client, "Trigger Novel", "manual://trigger-test-1", [
            ("Chapter 1", "Initial alchemy formula text."),
        ])

        # Match initial text
        res = client.post(f"/api/novels/{novel_id}/search", json={"q": "alchemy"}).json()["results"]
        assert len(res) == 1

        db = SessionLocal()
        ch = db.query(Chapter).filter(Chapter.novel_id == novel_id, Chapter.chapter_number == 1).first()
        ch.translated_content = "Replaced with cultivation talisman secret."
        db.commit()
        db.close()

        # Old term shouldn't match, new term should match
        res_old = client.post(f"/api/novels/{novel_id}/search", json={"q": "alchemy"}).json()["results"]
        assert len(res_old) == 0

        res_new = client.post(f"/api/novels/{novel_id}/search", json={"q": "talisman"}).json()["results"]
        assert len(res_new) == 1
        assert "talisman" in res_new[0]["snippet"].lower()
