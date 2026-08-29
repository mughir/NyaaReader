"""
Stage 4: PRAGMA foreign_keys=ON, enabled via a SQLAlchemy "connect" event so
it actually applies to every pooled connection (the pragma is per-connection,
not persisted in the database file — setting it once on a single connection,
as the app's other SQLite pragmas do, would leave every other connection in
the pool unenforced).
"""
import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from database import SessionLocal, engine, _clean_orphans
from models import Bookmark, Chapter, Novel


class TestForeignKeysAreEnforced:
    def test_a_new_connection_has_foreign_keys_on(self):
        """Every pooled connection must have it on, not just the one used at
        startup — checked via a fresh raw connection from the same engine."""
        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            cur.execute("PRAGMA foreign_keys")
            assert cur.fetchone()[0] == 1
        finally:
            raw.close()

    def test_writing_a_dangling_reference_now_raises(self, client):
        """Before this fix, inserting a bookmark for a chapter_id that
        doesn't exist silently succeeded — the exact shape of bug that left
        a real orphaned reading_progress row in the live database."""
        novel_id = client.post("/api/novels/manual",
                               json={"title": "FK Check", "source_url": "manual://fk-check-1"}).json()["id"]
        db = SessionLocal()
        try:
            db.add(Bookmark(novel_id=novel_id, chapter_id=999999, chapter_number=1, quote="x"))
            with pytest.raises(IntegrityError):
                db.commit()
        finally:
            db.rollback()
            db.close()


class TestOrphanCleanupOnStartup:
    def test_an_existing_dangling_reading_progress_row_is_removed(self, client):
        """_clean_orphans() must be idempotent and safe to run every start —
        exercised directly here since init_db() itself only runs once at
        process import, before the isolated per-test DB exists."""
        from models import ReadingProgress
        novel_id = client.post("/api/novels/manual",
                               json={"title": "Orphan Check", "source_url": "manual://orphan-check-1"}).json()["id"]
        db = SessionLocal()
        ch = Chapter(novel_id=novel_id, chapter_number=1, title="ch1")
        db.add(ch)
        db.commit()
        chapter_id = ch.id

        db.close()

        # Write the dangling row AND delete its parent chapter via a single
        # raw connection with foreign_keys=OFF, bypassing the ORM entirely.
        # This is the only way such a row can exist at all now that the app's
        # own pooled connections enforce the constraint: deleting the chapter
        # through the ORM (an FK-enforced connection) while this reading_progress
        # row still referenced it would itself raise IntegrityError — which is
        # the fix working correctly, not a bug. The orphan this test reproduces
        # is the pre-existing one already found live in production (written
        # before FK enforcement existed), so it has to be manufactured the same
        # way: entirely outside FK enforcement.
        raw = sqlite3.connect(str(engine.url).replace("sqlite:///", ""))
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("INSERT INTO reading_progress (novel_id, chapter_id, scroll_position) VALUES (?, ?, 0)",
                    (novel_id, chapter_id))
        raw.execute("DELETE FROM chapters WHERE id = ?", (chapter_id,))
        raw.commit()
        raw.close()

        still_there = SessionLocal().query(ReadingProgress).filter(
            ReadingProgress.chapter_id == chapter_id).count()
        assert still_there == 1, "test setup should have produced the dangling row"

        _clean_orphans()

        gone = SessionLocal().query(ReadingProgress).filter(
            ReadingProgress.chapter_id == chapter_id).count()
        assert gone == 0

    def test_running_it_again_with_nothing_dangling_is_a_safe_no_op(self):
        _clean_orphans()
        _clean_orphans()
