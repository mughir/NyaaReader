"""
Novel-level endpoints (CRUD, search, export, cover, settings, progress, shelf, stats).
"""
from datetime import datetime
import html as _html
import logging
import os
from pathlib import Path
import re
import sys
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from database import get_db_session
from models import Bookmark, Chapter, DiaryEntry, Novel, NovelSettings, ReadingProgress
from schemas import (
    NovelCreate,
    NovelManualCreate,
    NovelResponse,
    ProgressUpdate,
    ReadingProgressResponse,
    SettingsUpdate,
)
from services.export_service import (
    _epub_path,
    _export_epub_bg,
    _generate_cover_svg,
    _safe_filename,
)
from services.job_service import _async_novel_lock, _batch_running
from services.novel_service import _create_novel_from_url, fetch_chapters_range

logger = logging.getLogger("novel-reader.novels_router")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data")) if not os.name == "nt" else Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _get_main_attr(name: str, fallback):
    main_mod = sys.modules.get("main")
    if main_mod is not None and hasattr(main_mod, name):
        return getattr(main_mod, name)
    return fallback


def _build_search_snippet(content: str, query: str, window_before: int = 80, window_after: int = 160) -> str:
    """Build a search snippet with HTML-escaped text + a single safe <mark>.

    Snippets are rendered with v-html on the frontend, so raw chapter text
    must NEVER be interpolated as HTML — escape first, then highlight the
    (escaped) query. Only the <mark class="search-hl"> tag we emit survives.
    """
    text_str = content or ""
    idx = text_str.lower().find(query.lower())
    if idx < 0:
        idx = 0
    start = max(0, idx - window_before)
    end = min(len(text_str), idx + len(query) + window_after)
    raw = text_str[start:end]
    escaped = _html.escape(raw)
    # Highlight on the escaped text (query itself escaped first so a query
    # like "<img>" can't inject markup).
    pattern = re.compile(re.escape(_html.escape(query)), re.IGNORECASE)
    hl = pattern.sub(r'<mark class="search-hl">\g<0></mark>', escaped)
    return ("…" if start > 0 else "") + hl + ("…" if end < len(text_str) else "")


router = APIRouter(tags=["novels"])


@router.post("/api/novels", response_model=NovelResponse)
async def add_novel(novel_data: NovelCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db_session)):
    """Add a new novel by URL (JSON API)"""
    try:
        novel = await _create_novel_from_url(
            db, str(novel_data.source_url), novel_data.target_language,
            novel_data.auto_translate, background_tasks,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return novel


@router.post("/api/novels/manual", response_model=NovelResponse)
async def add_novel_manual(novel_data: NovelManualCreate, db: Session = Depends(get_db_session)):
    """Add a novel manually (no scraping) — for sites without a scraper or test novels."""
    existing = db.query(Novel).filter(Novel.source_url == novel_data.source_url).first()
    if existing:
        raise HTTPException(status_code=400, detail="Novel already exists")
    novel = Novel(
        title=novel_data.title.strip(),
        author=novel_data.author or "",
        description=novel_data.description or "",
        cover_url=novel_data.cover_url or "",
        source_url=novel_data.source_url or f"manual://{re.sub(r'[^a-z0-9]+', '-', novel_data.title.lower()).strip('-')}",
        source_site="manual",
        original_language=novel_data.original_language or "zh",
        target_language=novel_data.target_language or "en",
        total_chapters=0,
    )
    db.add(novel)
    db.commit()
    db.refresh(novel)
    return novel


@router.get("/api/novels", response_model=List[NovelResponse])
async def list_novels(db: Session = Depends(get_db_session)):
    """List all novels"""
    novels = db.query(Novel).order_by(Novel.updated_at.desc()).all()
    return novels


@router.get("/api/novels/{novel_id}", response_model=NovelResponse)
async def get_novel(novel_id: int, db: Session = Depends(get_db_session)):
    """Get novel details"""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    return novel


@router.delete("/api/novels/{novel_id}")
async def delete_novel(novel_id: int, db: Session = Depends(get_db_session)):
    """Delete a novel and all its data"""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    db.delete(novel)
    db.commit()
    return {"status": "deleted"}


@router.post("/api/novels/{novel_id}/fetch-chapters")
async def fetch_more_chapters(
    novel_id: int,
    start: int = 1,
    count: int = 10,
    translate: Optional[bool] = None,
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db_session)
):
    """Fetch more chapters for a novel (background task)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    if start < 1:
        raise HTTPException(status_code=400, detail="start must be >= 1")
    count = max(1, min(count, 100))

    settings = db.query(NovelSettings).filter(NovelSettings.novel_id == novel_id).first()
    do_translate = translate if translate is not None else bool(settings and settings.auto_translate)

    fetch_range_fn = _get_main_attr("fetch_chapters_range", fetch_chapters_range)
    if background_tasks:
        background_tasks.add_task(fetch_range_fn, novel_id, start, count, do_translate)
        return {"status": "queued", "message": f"Fetching {count} chapters from chapter {start}"}

    await fetch_range_fn(novel_id, start, count, do_translate)
    return {"status": "done", "message": f"Fetched {count} chapters from chapter {start}"}


@router.get("/api/novels/{novel_id}/progress", response_model=ReadingProgressResponse)
async def get_progress(novel_id: int, db: Session = Depends(get_db_session)):
    """Get reading progress for a novel"""
    progress = db.query(ReadingProgress).filter(
        ReadingProgress.novel_id == novel_id
    ).first()
    if not progress:
        return ReadingProgressResponse(
            novel_id=novel_id,
            chapter_id=0,
            scroll_position=0,
            percentage=0.0,
            last_read_at=datetime.utcnow()
        )
    return progress


@router.post("/api/novels/{novel_id}/progress")
async def update_progress(
    novel_id: int,
    chapter_id: Optional[int] = Query(None),
    scroll_position: int = Query(0),
    percentage: float = Query(0.0),
    payload: Optional[ProgressUpdate] = None,
    db: Session = Depends(get_db_session)
):
    """Update reading progress (JSON body or query params)"""
    if payload:
        chapter_id = payload.chapter_id
        scroll_position = payload.scroll_position
        percentage = payload.percentage
    if not chapter_id:
        raise HTTPException(status_code=422, detail="chapter_id required")
    if not db.query(Chapter).filter(Chapter.id == chapter_id, Chapter.novel_id == novel_id).first():
        raise HTTPException(status_code=404, detail="Chapter not found in this novel")
    progress = db.query(ReadingProgress).filter(
        ReadingProgress.novel_id == novel_id
    ).first()
    if not progress:
        progress = ReadingProgress(novel_id=novel_id, chapter_id=chapter_id)
        db.add(progress)
    progress.chapter_id = chapter_id
    progress.scroll_position = scroll_position
    progress.percentage = percentage
    progress.last_read_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


@router.put("/api/novels/{novel_id}/reading-status")
async def set_reading_status(novel_id: int, payload: dict, db: Session = Depends(get_db_session)):
    """Set the user's shelf status: ongoing | read_later | done | dropped"""
    status = (payload.get("status") or "").strip()
    if status not in ("ongoing", "read_later", "done", "dropped"):
        raise HTTPException(status_code=422, detail="status must be ongoing|read_later|done|dropped")
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    novel.reading_status = status
    db.commit()
    return {"status": "ok", "reading_status": status}


@router.get("/api/novels/{novel_id}/settings", response_model=dict)
async def get_settings(novel_id: int, db: Session = Depends(get_db_session)):
    """Get novel settings"""
    settings = db.query(NovelSettings).filter(
        NovelSettings.novel_id == novel_id
    ).first()
    if not settings:
        return {
            "auto_translate": True,
            "translation_quality": "balanced",
            "font_size": 18,
            "line_height": 1.7,
            "theme": "light",
            "show_original": False,
            "auto_fetch_next": True,
            "custom_css": "",
        }
    return {
        "auto_translate": settings.auto_translate,
        "translation_quality": settings.translation_quality,
        "font_size": settings.font_size,
        "line_height": settings.line_height,
        "theme": settings.theme,
        "show_original": settings.show_original,
        "auto_fetch_next": settings.auto_fetch_next,
        "custom_css": settings.custom_css,
    }


@router.put("/api/novels/{novel_id}/settings")
async def update_settings(
    novel_id: int,
    settings_data: SettingsUpdate,
    db: Session = Depends(get_db_session)
):
    """Update novel settings"""
    settings = db.query(NovelSettings).filter(
        NovelSettings.novel_id == novel_id
    ).first()
    if not settings:
        settings = NovelSettings(novel_id=novel_id)
        db.add(settings)
    update_data = settings_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(settings, key, value)
    settings.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


@router.post("/api/novels/{novel_id}/search")
async def search_novel(novel_id: int, payload: dict, db: Session = Depends(get_db_session)):
    """Full-text search across translated chapter content."""
    q = (payload.get("q") or "").strip()
    if len(q) < 2:
        return {"results": []}
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    results = []
    fts_success = False
    try:
        clean_terms = re.findall(r'[\w\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]+', q)
        if clean_terms:
            fts_query = " ".join(f'"{t}"' for t in clean_terms)
            sql = sa_text("""
                SELECT 
                    chapter_number,
                    title_translated,
                    snippet(chapters_fts, 4, '<mark class="search-hl">', '</mark>', '…', 24) AS snippet_text,
                    translated_content,
                    bm25(chapters_fts) AS rank
                FROM chapters_fts
                WHERE novel_id = :novel_id AND chapters_fts MATCH :fts_query
                ORDER BY rank
                LIMIT 50
            """)
            rows = db.execute(sql, {"novel_id": novel_id, "fts_query": fts_query}).fetchall()
            if rows:
                for row in rows:
                    ch_num = row[0]
                    title = row[1] or f"Chapter {ch_num}"
                    content = row[3] or ""
                    if q.lower() not in content.lower() and q.lower() not in title.lower():
                        continue
                    cnt = content.lower().count(q.lower())
                    # Never trust the FTS snippet() raw HTML or raw chapter
                    # text — rebuild escaped via the helper (XSS-safe v-html).
                    snippet = _build_search_snippet(content, q)
                    results.append({
                        "chapter_number": ch_num,
                        "title": title,
                        "snippet": snippet,
                        "count": max(1, cnt),
                    })
                if results:
                    fts_success = True
    except Exception as e:
        logger.debug(f"FTS5 search fallback: {e}")
        fts_success = False

    if not fts_success and not results:
        chapters = (db.query(Chapter)
                    .filter(Chapter.novel_id == novel_id,
                            Chapter.translated_content.contains(q, autoescape=True))
                    .order_by(Chapter.chapter_number)
                    .limit(50).all())
        for ch in chapters:
            text_str = ch.translated_content or ""
            snippet = _build_search_snippet(text_str, q)
            results.append({
                "chapter_number": ch.chapter_number,
                "title": ch.title_translated or ch.title or f"Chapter {ch.chapter_number}",
                "snippet": snippet,
                "count": text_str.lower().count(q.lower()),
            })

    return {"results": results}


@router.get("/api/novels/{novel_id}/bookmarks")
async def list_bookmarks(novel_id: int, db: Session = Depends(get_db_session)):
    """All user bookmarks/highlights for a novel (newest first)."""
    bms = db.query(Bookmark).filter(Bookmark.novel_id == novel_id).order_by(
        Bookmark.created_at.desc()).all()
    return [{
        "id": b.id,
        "chapter_number": b.chapter_number,
        "chapter_id": b.chapter_id,
        "quote": b.quote,
        "note": b.note or "",
        "color": b.color or "yellow",
        "created_at": b.created_at.isoformat() if b.created_at else None,
    } for b in bms]


@router.delete("/api/bookmarks/{bookmark_id}")
async def delete_bookmark(bookmark_id: int, db: Session = Depends(get_db_session)):
    bm = db.query(Bookmark).filter(Bookmark.id == bookmark_id).first()
    if not bm:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    db.delete(bm)
    db.commit()
    return {"status": "ok"}


@router.get("/api/novels/{novel_id}/diary")
async def list_diary(novel_id: int, db: Session = Depends(get_db_session)):
    """All diary entries for a novel (by chapter number)."""
    entries = (db.query(DiaryEntry)
               .filter(DiaryEntry.novel_id == novel_id)
               .order_by(DiaryEntry.chapter_number)
               .all())
    return [{"chapter_number": e.chapter_number, "content": e.content,
             "updated_at": e.updated_at.isoformat() if e.updated_at else None}
            for e in entries]


@router.post("/api/novels/{novel_id}/export-epub")
async def export_epub(novel_id: int, background_tasks: BackgroundTasks = None,
                      db: Session = Depends(get_db_session)):
    """Build an EPUB of the novel's translated chapters (background)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    async_lock_fn = _get_main_attr("_async_novel_lock", _async_novel_lock)
    batch_run_fn = _get_main_attr("_batch_running", _batch_running)
    async with async_lock_fn(novel_id):
        if batch_run_fn(novel_id):
            return {"status": "already_running"}
        export_fn = _get_main_attr("_export_epub_bg", _export_epub_bg)
        background_tasks.add_task(export_fn, novel_id)
    return {"status": "started"}


@router.get("/api/novels/{novel_id}/epub-download")
async def epub_download(novel_id: int, db: Session = Depends(get_db_session)):
    """Download the generated EPUB (404 until the job finishes)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    epub_p_fn = _get_main_attr("_epub_path", _epub_path)
    path = epub_p_fn(novel)
    if not path.exists():
        raise HTTPException(status_code=404, detail="EPUB not ready yet")
    return FileResponse(path, media_type="application/epub+zip",
                        filename=f"{_safe_filename(novel.title_translated or novel.title)}.epub")


@router.post("/api/novels/{novel_id}/generate-cover")
async def generate_cover(novel_id: int, db: Session = Depends(get_db_session)):
    """Ask the relay to design an SVG cover from the novel's title/synopsis."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    try:
        gen_cov_fn = _get_main_attr("_generate_cover_svg", _generate_cover_svg)
        svg = await gen_cov_fn(novel)
    except Exception as e:
        logger.error(f"cover gen failed: {e}")
        raise HTTPException(status_code=502, detail=f"Cover generation failed: {e}")
    if not svg:
        raise HTTPException(status_code=502, detail="Cover generation returned nothing")
    d = DATA_DIR / "covers"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"novel_{novel_id}.svg"
    path.write_text(svg, encoding="utf-8")
    novel.cover_url = f"/api/novels/{novel_id}/cover"
    db.commit()
    return {"status": "ok", "cover_url": novel.cover_url}


@router.post("/api/novels/{novel_id}/cover-upload")
async def upload_cover(novel_id: int, file: UploadFile = File(...),
                       db: Session = Depends(get_db_session)):
    """Upload a user-provided cover image (png/jpg/webp/gif). Replaces any existing cover."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    name = (file.filename or "").lower()
    if not name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        raise HTTPException(status_code=400, detail="Unsupported image type (use png/jpg/webp/gif)")
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 10 MB)")
    if not data[:8].startswith((b"\x89PNG", b"\xff\xd8", b"GIF8")) and not data[:4].startswith(b"RIFF"):
        raise HTTPException(status_code=400, detail="Not a valid image file")
    d = DATA_DIR / "covers"
    d.mkdir(parents=True, exist_ok=True)
    ext = {"jpg": "jpg", "jpeg": "jpg", "png": "png", "webp": "webp", "gif": "gif"}[name.rsplit(".", 1)[-1]]
    path = d / f"novel_{novel_id}.{ext}"
    path.write_bytes(data)
    for old in d.glob(f"novel_{novel_id}.*"):
        if old != path:
            try:
                old.unlink()
            except OSError:
                pass
    novel.cover_url = f"/api/novels/{novel_id}/cover"
    db.commit()
    return {"status": "ok", "cover_url": novel.cover_url}


@router.get("/api/novels/{novel_id}/cover")
async def novel_cover(novel_id: int, db: Session = Depends(get_db_session)):
    """Serve the novel's cover image (uploaded or AI-generated)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    d = DATA_DIR / "covers"
    candidates = sorted(d.glob(f"novel_{novel_id}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise HTTPException(status_code=404, detail="Cover not found")
    path = candidates[0]
    mime = "image/svg+xml" if path.suffix == ".svg" else "image/" + (path.suffix[1:] or "png")
    return FileResponse(path, media_type=mime)


@router.get("/api/stats")
async def get_stats(db: Session = Depends(get_db_session)):
    """Get library statistics (dashboard data)."""
    total_novels = db.query(Novel).count()
    total_chapters = db.query(Chapter).count()
    translated_chapters = db.query(Chapter).filter(Chapter.is_translated == True).count()
    read_chapters = db.query(Chapter).filter(Chapter.is_read == True).count()
    bookmarks = db.query(Bookmark).count()
    diary_entries = db.query(DiaryEntry).count()
    shelves = {}
    for n in db.query(Novel.reading_status).all():
        k = n[0] or "ongoing"
        shelves[k] = shelves.get(k, 0) + 1
    recent = []
    for ch in (db.query(Chapter, Novel.title_translated, Novel.title)
               .join(Novel, Chapter.novel_id == Novel.id)
               .filter(Chapter.read_at.isnot(None))
               .order_by(Chapter.read_at.desc()).limit(6).all()):
        recent.append({
            "novel_id": ch[0].novel_id,
            "chapter_number": ch[0].chapter_number,
            "chapter_title": ch[0].title_translated or ch[0].title or "",
            "novel": ch[1] or ch[2] or "?",
            "read_at": ch[0].read_at.isoformat() if ch[0].read_at else None,
        })
    return {
        "total_novels": total_novels,
        "total_chapters": total_chapters,
        "translated_chapters": translated_chapters,
        "translation_rate": f"{(translated_chapters / total_chapters * 100):.1f}%" if total_chapters > 0 else "0%",
        "read_chapters": read_chapters,
        "bookmarks": bookmarks,
        "diary_entries": diary_entries,
        "shelves": shelves,
        "recent": recent,
    }
