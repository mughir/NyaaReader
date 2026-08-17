"""
Database setup and session management
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from contextlib import contextmanager
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./novel_reader.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    echo=False,
)

# SQLite concurrency safety: WAL lets readers never block writers; busy_timeout
# makes concurrent background jobs wait instead of throwing "database is locked".
if "sqlite" in DATABASE_URL:
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA busy_timeout=5000"))
        conn.execute(text("PRAGMA synchronous=NORMAL"))
        conn.commit()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def init_db():
    """Initialize database tables + lightweight column migrations"""
    from models import Base
    Base.metadata.create_all(bind=engine)
    _ensure_columns()


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