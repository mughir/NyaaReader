"""
Pydantic request and response schemas for NyaaReader.
"""
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, HttpUrl, computed_field


class NovelCreate(BaseModel):
    source_url: HttpUrl
    target_language: str = "en"
    auto_translate: bool = True


class NovelManualCreate(BaseModel):
    title: str
    author: Optional[str] = None
    description: Optional[str] = None
    cover_url: Optional[str] = None
    source_url: Optional[str] = None
    original_language: str = "zh"
    target_language: str = "en"


class ChapterManualCreate(BaseModel):
    title: Optional[str] = None
    content: str
    source_url: Optional[str] = None


class NovelResponse(BaseModel):
    id: int
    title: str
    title_translated: Optional[str] = None
    author: Optional[str]
    description: Optional[str]
    description_translated: Optional[str] = None
    cover_url: Optional[str]
    source_url: str
    source_site: Optional[str]
    original_language: str
    target_language: str
    status: str
    reading_status: str = "ongoing"
    total_chapters: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ChapterResponse(BaseModel):
    id: int
    novel_id: int
    chapter_number: int
    title: Optional[str]
    title_translated: Optional[str] = None
    original_content: Optional[str]
    translated_content: Optional[str]
    is_translated: bool
    is_read: bool = False
    word_count: int
    translated_word_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @computed_field
    @property
    def has_content(self) -> bool:
        return bool(self.original_content)


class ProgressUpdate(BaseModel):
    chapter_id: int
    scroll_position: int = 0
    percentage: float = 0.0


class ReadingProgressResponse(BaseModel):
    novel_id: int
    chapter_id: int
    scroll_position: int
    percentage: float
    last_read_at: datetime

    class Config:
        from_attributes = True


class TranslateRequest(BaseModel):
    chapter_id: int
    quality: str = "balanced"  # fast, balanced, quality
    force_retranslate: bool = False


class SettingsUpdate(BaseModel):
    auto_translate: Optional[bool] = None
    translation_quality: Optional[str] = None
    font_size: Optional[int] = None
    line_height: Optional[float] = None
    theme: Optional[str] = None
    show_original: Optional[bool] = None
    auto_fetch_next: Optional[bool] = None
    custom_css: Optional[str] = None


class BatchTranslateSelectedRequest(BaseModel):
    chapters: List[int]


class BatchMarkReadRequest(BaseModel):
    chapters: List[int]
    is_read: bool = True
