"""
Server-rendered HTML page routes for NyaaReader.
"""
from datetime import datetime
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from database import get_db_session
from models import Chapter, DiaryEntry, Novel, NovelMemory, ReadingProgress
from views import _get_recap, _json, _page

logger = logging.getLogger("novel-reader.pages_router")

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def library_page(request: Request, db: Session = Depends(get_db_session)):
    novels = db.query(Novel).order_by(Novel.updated_at.desc()).all()
    shelf = request.query_params.get("shelf", "")
    data = []
    for n in novels:
        translated = db.query(Chapter).filter(
            Chapter.novel_id == n.id, Chapter.is_translated == True).count()
        read_count = db.query(Chapter).filter(
            Chapter.novel_id == n.id, Chapter.is_read == True).count()
        prog = db.query(ReadingProgress).filter(ReadingProgress.novel_id == n.id).first()
        last_read = None
        if prog and prog.chapter_id:
            ch = db.query(Chapter).filter(Chapter.id == prog.chapter_id).first()
            if ch:
                last_read = {
                    "chapter_number": ch.chapter_number,
                    "title": ch.title_translated or ch.title,
                }
        data.append({
            "id": n.id,
            "title": n.title,
            "title_translated": n.title_translated,
            "author": n.author,
            "cover_url": n.cover_url,
            "total_chapters": n.total_chapters,
            "translated_chapters": translated,
            "read_chapters": read_count,
            "reading_status": n.reading_status or "ongoing",
            "source_site": n.source_site,
            "last_read": last_read,
        })
    if shelf and shelf != "all":
        data = [d for d in data if d["reading_status"] == shelf]
    return _page("My Library",
                 '<div id="library-app"></div>',
                 page_js="library.js",
                 data_js=f"window.__LIBRARY__ = {_json(data)}; window.__SHELF__ = {_json(shelf)};")


@router.get("/novel/{novel_id}", response_class=HTMLResponse)
async def novel_page(novel_id: int, db: Session = Depends(get_db_session)):
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        return _page("Not found", '<p>Novel not found. <a href="/">← Back</a></p>')
    chapters = db.query(Chapter).filter(Chapter.novel_id == novel_id) \
        .order_by(Chapter.chapter_number).all()

    novel_data = {
        "id": novel.id,
        "title": novel.title,
        "title_translated": novel.title_translated,
        "author": novel.author,
        "description": novel.description,
        "description_translated": novel.description_translated,
        "cover_url": novel.cover_url,
        "source_site": novel.source_site,
        "original_language": novel.original_language,
        "target_language": novel.target_language,
        "status": novel.status,
        "reading_status": novel.reading_status or "ongoing",
        "total_chapters": novel.total_chapters,
    }
    ch_data = [{
        "id": c.id,
        "chapter_number": c.chapter_number,
        "title": c.title,
        "title_translated": c.title_translated,
        "is_translated": c.is_translated,
        "is_read": bool(c.is_read),
        "has_content": bool(c.original_content),
    } for c in chapters]

    return _page(novel.title_translated or novel.title,
                 '<div id="novel-app"></div>',
                 page_js="novel.js",
                 data_js=f"window.__NOVEL__ = {_json({'novel': novel_data, 'chapters': ch_data})};")


@router.get("/novel/{novel_id}/chapter/{chapter_number}", response_class=HTMLResponse)
async def chapter_page(novel_id: int, chapter_number: int, db: Session = Depends(get_db_session)):
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        return _page("Not found", '<p>Novel not found. <a href="/">← Back</a></p>')
    chapter = db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.chapter_number == chapter_number,
    ).first()
    if not chapter:
        return _page("Not found", f'<p>Chapter not found. <a href="/novel/{novel_id}">← Back</a></p>')

    if not chapter.is_read or chapter.read_at is None:
        chapter.is_read = True
        chapter.read_at = datetime.utcnow()
        db.commit()

    title = chapter.title_translated or chapter.title or f"Chapter {chapter_number}"

    toc_chapters = (db.query(Chapter)
                    .filter(Chapter.novel_id == novel_id)
                    .order_by(Chapter.chapter_number)
                    .all())
    toc = [{"n": c.chapter_number, "t": c.title_translated or c.title or f"Ch {c.chapter_number}",
            "done": bool(c.is_translated)} for c in toc_chapters]

    reader_data = {
        "novel_id": novel.id,
        "novel_title": novel.title,
        "novel_title_translated": novel.title_translated,
        "chapter_id": chapter.id,
        "chapter_number": chapter_number,
        "total_chapters": novel.total_chapters,
        "title": title,
        "title_translated": chapter.title_translated,
        "original": chapter.original_content or "",
        "translated": chapter.translated_content or "",
        "is_translated": bool(chapter.is_translated),
        "target_lang": novel.target_language,
        "toc": toc,
        "recap": _get_recap(db, novel_id),
    }
    return _page(f"{title} - {novel.title_translated or novel.title}",
                 '<div id="reader-app"></div>',
                 page_js="reader.js",
                 data_js=f"window.__READER__ = {_json(reader_data)};")


@router.get("/novel/{novel_id}/review", response_class=HTMLResponse)
async def novel_review_page(novel_id: int, db: Session = Depends(get_db_session)):
    """Story-so-far review page: AI memory (characters, plot, arcs) + user diary."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        return _page("Not found", '<p>Novel not found. <a href="/">← Back</a></p>')
    mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
    memory_data = {}
    if mem:
        gl = mem.glossary_entries
        if isinstance(gl, str):
            try:
                gl = json.loads(gl)
            except Exception as e:
                logger.warning(f"review page: could not parse glossary_entries for novel {novel_id}: {e}")
                gl = []
        memory_data = {
            "characters": mem.characters or "",
            "terms": mem.terms or "",
            "plot": mem.plot or "",
            "arc_plot": mem.arc_plot or "",
            "chapter_plot": mem.chapter_plot or "",
            "memory": mem.memory or "",
            "glossary_entries": gl or [],
        }
    entries = (db.query(DiaryEntry)
               .filter(DiaryEntry.novel_id == novel_id)
               .order_by(DiaryEntry.chapter_number)
               .all())
    diary = [{"chapter_number": e.chapter_number, "content": e.content} for e in entries]
    return _page(f"Story so far — {novel.title_translated or novel.title}",
                 '<div id="review-app"></div>',
                 page_js="review.js",
                 data_js=f"window.__REVIEW__ = {_json({'novel': {'id': novel.id, 'title': novel.title, 'title_translated': novel.title_translated}, 'memory': memory_data, 'diary': diary})};")


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return _page("Login", '<div id="login-app"></div>', page_js="login.js")


@router.get("/config", response_class=HTMLResponse)
async def config_page():
    """Settings page: API keys, fallback models, backup preferences."""
    return _page("Settings",
                 '<div id="config-app"></div>',
                 page_js="config.js",
                 data_js="window.__CONFIG__ = true;")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    """Reading dashboard: stats, shelves, recent activity."""
    return _page("Dashboard", '<div id="dashboard-app"></div>', page_js="dashboard.js")
