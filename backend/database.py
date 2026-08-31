"""
Database setup and session management
"""
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, declarative_base
from contextlib import contextmanager
import logging
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./novel_reader.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_pre_ping=True,
)

# SQLite concurrency safety: WAL lets readers never block writers; busy_timeout
# makes concurrent background jobs wait instead of throwing "database is locked".
if "sqlite" in DATABASE_URL:
    # foreign_keys, busy_timeout AND synchronous are all per-CONNECTION
    # settings — SQLite does not persist any of them in the database file
    # (unlike journal_mode, which is a file-format flag). The "connect" event
    # fires for every new DBAPI connection the pool creates, which is the only
    # way to guarantee all three apply everywhere: with pool_size=10 +
    # max_overflow=20, setting them on just one connection (the way this code
    # used to, via a one-off `with engine.connect()` block) leaves every other
    # pooled connection silently running with foreign_keys=OFF, busy_timeout=0
    # and synchronous=FULL. The busy_timeout=0 gap is what made
    # `_clean_orphans()` intermittently raise "database is locked" instead of
    # waiting: any connection other than the first-ever one had no timeout at
    # all, so real (short-lived) contention surfaced as an immediate error
    # instead of a bounded wait.
    #
    # journal_mode=WAL is the one exception that's actually safe to leave on a
    # single connect — it's persisted in the file — but it's cheap and
    # idempotent to set again per-connection too, so it lives here alongside
    # the other three rather than in a second, separate code path.
    #
    # Nothing in this codebase relies on writing a child row before its
    # parent exists — every insert path already fetches/creates the parent
    # first — and models.py's cascade="all, delete-orphan" relationships
    # delete children via individual ORM statements in dependency order, not
    # a DB-level ON DELETE CASCADE, so foreign_keys=ON does not change delete
    # ordering. It closes a real gap: nothing currently stops an orphaned row
    # (a stale bookmark/reading-progress pointing at a chapter that no longer
    # exists) from being written in the first place.
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def init_db():
    """Initialize database tables + lightweight column migrations"""
    from models import Base
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _clean_orphans()
    _reconcile_total_chapters()
    _init_fts()


def _init_fts():
    """Create and sync FTS5 full-text search virtual table for translated chapters."""
    from sqlalchemy import text
    with engine.begin() as conn:
        try:
            conn.execute(text("""
                CREATE VIRTUAL TABLE IF NOT EXISTS chapters_fts USING fts5(
                    chapter_id UNINDEXED,
                    novel_id UNINDEXED,
                    chapter_number UNINDEXED,
                    title_translated,
                    translated_content,
                    tokenize='unicode61 remove_diacritics 2'
                )
            """))
            conn.execute(text("""
                CREATE TRIGGER IF NOT EXISTS chapters_fts_ai AFTER INSERT ON chapters
                BEGIN
                    INSERT INTO chapters_fts(chapter_id, novel_id, chapter_number, title_translated, translated_content)
                    VALUES (new.id, new.novel_id, new.chapter_number, coalesce(new.title_translated, ''), coalesce(new.translated_content, ''));
                END
            """))
            conn.execute(text("""
                CREATE TRIGGER IF NOT EXISTS chapters_fts_au AFTER UPDATE ON chapters
                BEGIN
                    DELETE FROM chapters_fts WHERE chapter_id = old.id;
                    INSERT INTO chapters_fts(chapter_id, novel_id, chapter_number, title_translated, translated_content)
                    VALUES (new.id, new.novel_id, new.chapter_number, coalesce(new.title_translated, ''), coalesce(new.translated_content, ''));
                END
            """))
            conn.execute(text("""
                CREATE TRIGGER IF NOT EXISTS chapters_fts_ad AFTER DELETE ON chapters
                BEGIN
                    DELETE FROM chapters_fts WHERE chapter_id = old.id;
                END
            """))
            conn.execute(text("""
                INSERT INTO chapters_fts(chapter_id, novel_id, chapter_number, title_translated, translated_content)
                SELECT id, novel_id, chapter_number, coalesce(title_translated, ''), coalesce(translated_content, '')
                FROM chapters
                WHERE is_translated = 1 AND id NOT IN (SELECT chapter_id FROM chapters_fts)
            """))
        except Exception as e:
            logging.getLogger("novel-reader").warning(f"FTS5 initialization warning (skipping or unsupported): {e}")


def _reconcile_total_chapters():
    """Self-healing safeguard against novels.total_chapters drift, not a patch
    for one specific cause. Two DIFFERENT call sites (check_updates_bg,
    add_chapter_manual) each independently hit the same SQLAlchemy autoflush
    bug — counting chapters before the pending insert was flushed undercounted
    by exactly one — and each got its own point fix once found. A point fix
    only guards the exact path already discovered; it does nothing for a
    future path (or a restored backup, or a manual DB edit) that adds/removes
    chapters without recomputing this column. Runs on every start and simply
    corrects whatever's wrong, from whatever cause, instead of trusting every
    present and future call site to always remember to keep it in sync."""
    from sqlalchemy import text
    with engine.begin() as conn:
        fixed = conn.execute(text(
            "UPDATE novels SET total_chapters = "
            "(SELECT COUNT(*) FROM chapters WHERE chapters.novel_id = novels.id) "
            "WHERE total_chapters IS NOT "
            "(SELECT COUNT(*) FROM chapters WHERE chapters.novel_id = novels.id)"
        )).rowcount
        if fixed:
            logging.getLogger("novel-reader").warning(
                "startup: corrected total_chapters drift on %d novel(s)" % fixed)


def _clean_orphans():
    """One-time cleanup for rows written before foreign_keys=ON existed.

    FK enforcement only checks FUTURE writes — it does not retroactively
    validate rows already in the database, so a pre-existing dangling
    reference (found in this project's own live database: a reading_progress
    row pointing at a chapter_id that no longer exists) would sit there
    forever, undetected, unless removed explicitly. Safe to run every start:
    a no-op once the dangling rows are gone."""
    from sqlalchemy import text
    with engine.begin() as conn:
        removed = conn.execute(text(
            "DELETE FROM reading_progress WHERE chapter_id NOT IN (SELECT id FROM chapters)"
        )).rowcount
        if removed:
            logging.getLogger("novel-reader").warning(
                "startup: removed %d orphaned reading_progress row(s) (chapter_id pointed at "
                "a chapter that no longer exists)" % removed)


def _ensure_columns():
    """Add columns introduced after the DB was first created (SQLite)."""
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, column, ddl in [
            ("novels", "title_translated", "ALTER TABLE novels ADD COLUMN title_translated VARCHAR(500)"),
            ("novels", "description_translated", "ALTER TABLE novels ADD COLUMN description_translated TEXT"),
            ("chapters", "title_translated", "ALTER TABLE chapters ADD COLUMN title_translated VARCHAR(500)"),
            ("novel_memory", "glossary_entries", "ALTER TABLE novel_memory ADD COLUMN glossary_entries TEXT"),
            ("novels", "reading_status", "ALTER TABLE novels ADD COLUMN reading_status VARCHAR(20) DEFAULT 'ongoing'"),
            ("chapters", "is_read", "ALTER TABLE chapters ADD COLUMN is_read BOOLEAN DEFAULT 0"),
            ("chapters", "read_at", "ALTER TABLE chapters ADD COLUMN read_at TIMESTAMP"),
            ("chapters", "last_error", "ALTER TABLE chapters ADD COLUMN last_error TEXT DEFAULT ''"),
            ("app_config", "auth_password", "ALTER TABLE app_config ADD COLUMN auth_password VARCHAR(200) DEFAULT ''"),
            ("batch_jobs", "stop_requested", "ALTER TABLE batch_jobs ADD COLUMN stop_requested BOOLEAN DEFAULT 0"),
            ("batch_jobs", "args_json", "ALTER TABLE batch_jobs ADD COLUMN args_json TEXT DEFAULT ''"),
        ]:
            cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()]
            if column not in cols:
                conn.execute(text(ddl))


@contextmanager
def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_session():
    """FastAPI dependency for database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()