"""
Database models for NyaaReader
"""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Float, JSON
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime

Base = declarative_base()


class Novel(Base):
    __tablename__ = "novels"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=False)
    title_translated = Column(String(500))  # AI-translated title (target_language)
    author = Column(String(200))
    description = Column(Text)
    description_translated = Column(Text)  # AI-translated synopsis (target_language)
    cover_url = Column(String(500))
    source_url = Column(String(500), unique=True, index=True)  # Original novel page
    source_site = Column(String(50))  # syosetu, kakuyomu, jjwxc, novelpia, etc.
    original_language = Column(String(10))  # zh, ja, ko
    target_language = Column(String(10), default="en")  # Translate to
    status = Column(String(20), default="ongoing")  # ongoing, completed, hiatus (source status)
    reading_status = Column(String(20), default="ongoing")  # user's shelf: ongoing, read_later, done, dropped
    total_chapters = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    chapters = relationship("Chapter", back_populates="novel", cascade="all, delete-orphan")
    settings = relationship("NovelSettings", back_populates="novel", uselist=False, cascade="all, delete-orphan")
    memory = relationship("NovelMemory", back_populates="novel", uselist=False, cascade="all, delete-orphan")
    bookmarks = relationship("Bookmark", back_populates="novel", cascade="all, delete-orphan")
    diary_entries = relationship("DiaryEntry", cascade="all, delete-orphan", overlaps="novel")
    reading_progress = relationship("ReadingProgress", cascade="all, delete-orphan", overlaps="novel")
    batch_jobs = relationship("BatchJob", cascade="all, delete-orphan", overlaps="novel")


class Chapter(Base):
    __tablename__ = "chapters"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False, index=True)
    chapter_number = Column(Integer, nullable=False)
    title = Column(String(500))
    title_translated = Column(String(500))  # AI-translated title
    original_content = Column(Text)  # Raw scraped content
    translated_content = Column(Text)  # AI translated content
    source_url = Column(String(500))  # Chapter URL
    word_count = Column(Integer, default=0)
    translated_word_count = Column(Integer, default=0)
    is_translated = Column(Boolean, default=False)
    translation_model = Column(String(50))  # Which model translated
    last_error = Column(Text, default="")   # last translation/fetch error (for retry queue)
    is_read = Column(Boolean, default=False)          # user has opened/read this chapter
    read_at = Column(DateTime)                          # last time it was opened
    translation_cost = Column(Float, default=0.0)  # Estimated cost
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    novel = relationship("Novel", back_populates="chapters")
    bookmarks = relationship("Bookmark", back_populates="chapter", cascade="all, delete-orphan")
    reading_progress = relationship("ReadingProgress", back_populates="chapter", uselist=False, cascade="all, delete-orphan")


class ReadingProgress(Base):
    __tablename__ = "reading_progress"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False, index=True)
    chapter_id = Column(Integer, ForeignKey("chapters.id"), unique=True, nullable=False)
    scroll_position = Column(Integer, default=0)  # Scroll position in pixels
    percentage = Column(Float, default=0.0)  # Reading percentage
    last_read_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    novel = relationship("Novel")
    chapter = relationship("Chapter", back_populates="reading_progress")


class NovelSettings(Base):
    __tablename__ = "novel_settings"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), unique=True, nullable=False)
    auto_translate = Column(Boolean, default=True)
    translation_quality = Column(String(20), default="balanced")  # fast, balanced, quality
    font_size = Column(Integer, default=18)
    line_height = Column(Float, default=1.7)
    theme = Column(String(20), default="light")  # light, dark, sepia
    show_original = Column(Boolean, default=False)  # Show original + translation
    auto_fetch_next = Column(Boolean, default=True)
    custom_css = Column(Text)  # Custom CSS for this novel
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    novel = relationship("Novel", back_populates="settings")


class ScrapingLog(Base):
    __tablename__ = "scraping_logs"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False, index=True)
    chapter_number = Column(Integer)
    status = Column(String(20))  # success, failed, skipped
    error_message = Column(Text)
    response_time = Column(Float)  # seconds
    created_at = Column(DateTime, default=datetime.utcnow)


class NovelMemory(Base):
    """
    Persistent per-novel knowledge used by the AI translator.

    The translator READS these fields as context before translating, and
    UPDATES them after each chapter (adding new characters, terms, plot points,
    arc progress, etc.). Users may edit/pre-seed them as well.
    """
    __tablename__ = "novel_memory"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), unique=True, nullable=False, index=True)

    # General instruction for ALL translations of this novel
    general_instruction = Column(Text, default="")

    # Structured knowledge (JSON-ish text maintained by the AI / editable by user)
    characters = Column(Text, default="")      # name, gender, gender-bender/crossdressing, role, notes
    # Structured glossary entries: [{"type":"character"|"term","source":"安潔莉雅",
    #   "translated":"Angelia","note":"...","locked":false}]
    glossary_entries = Column(JSON, default=list)
    terms = Column(Text, default="")           # glossary: term -> translation, per novel
    plot = Column(Text, default="")            # overall story plot summary
    arc_plot = Column(Text, default="")        # plot of the current story arc
    chapter_plot = Column(Text, default="")    # plot of the most recently translated chapter

    # Accumulated running memory (bounded) maintained by the translator
    memory = Column(Text, default="")

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    novel = relationship("Novel", back_populates="memory")


class Bookmark(Base):
    """User highlight/bookmark on a translated chapter."""
    __tablename__ = "bookmarks"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False, index=True)
    chapter_id = Column(Integer, ForeignKey("chapters.id"), nullable=False, index=True)
    chapter_number = Column(Integer, nullable=False)
    quote = Column(Text, nullable=False)        # the highlighted text
    note = Column(Text, default="")             # optional user note
    color = Column(String(20), default="yellow")  # highlight color
    created_at = Column(DateTime, default=datetime.utcnow)

    novel = relationship("Novel", back_populates="bookmarks")
    chapter = relationship("Chapter", back_populates="bookmarks")


class DiaryEntry(Base):
    """User's personal reading reflection for a chapter (free-text journal)."""
    __tablename__ = "diary_entries"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False, index=True)
    chapter_id = Column(Integer, ForeignKey("chapters.id"), nullable=False, index=True)
    chapter_number = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    novel = relationship("Novel")
    chapter = relationship("Chapter")

# NOTE: Novel.delete cascades to diary_entries via the relationship above —
# chapter_id is NOT NULL, so without the cascade a novel delete would emit
# UPDATE diary_entries SET chapter_id=NULL and fail with an IntegrityError.


class BatchJob(Base):
    """Persistent background-job record (translate-ahead / to-end / retranslate / titles…).
    Lives in the DB so jobs survive container restarts and auto-resume."""
    __tablename__ = "batch_jobs"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False, index=True)
    kind = Column(String(30), nullable=False)   # translate-ahead, to-end, retranslate, titles, match, updates
    total = Column(Integer, default=0)
    done = Column(Integer, default=0)
    current_label = Column(String(200), default="")
    running = Column(Boolean, default=True)
    stop_requested = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    novel = relationship("Novel")


class AppConfig(Base):
    """Single-row app configuration (API keys, backup settings) editable from the UI."""
    __tablename__ = "app_config"

    id = Column(Integer, primary_key=True)      # always 1 (singleton row)
    gemini_api_key = Column(String(500), default="")
    fallback_api_key = Column(String(500), default="")
    fallback_base_url = Column(String(500), default="https://opencode.ai/zen/go/v1")
    fallback_model = Column(String(100), default="deepseek-v4-flash")
    fallback_model_2 = Column(String(100), default="gpt-5.6-luna")
    # Model 2 can use separate URL/key (optional; empty = use Model 1's)
    fallback_2_base_url = Column(String(500), default="")
    fallback_2_api_key = Column(String(500), default="")
    backup_enabled = Column(Boolean, default=True)
    backup_interval_hours = Column(Integer, default=24)
    backup_keep = Column(Integer, default=14)
    # Auth: single password. Empty = auth disabled (local use).
    auth_password = Column(String(200), default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)