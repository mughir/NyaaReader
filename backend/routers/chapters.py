"""
Chapter endpoints (manual chapter creation, chapter retrieval, bookmarks, diary).
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db_session
from models import Bookmark, Chapter, DiaryEntry, Novel
from schemas import ChapterManualCreate, ChapterResponse

router = APIRouter(tags=["chapters"])


@router.post("/api/novels/{novel_id}/chapters/manual")
async def add_chapter_manual(novel_id: int, chapter_data: ChapterManualCreate,
                             db: Session = Depends(get_db_session)):
    """Add a single chapter manually (title + original content pasted by the user)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    last = db.query(Chapter).filter(Chapter.novel_id == novel_id).order_by(
        Chapter.chapter_number.desc()).first()
    next_num = (last.chapter_number + 1) if last else 1
    chapter = Chapter(
        novel_id=novel_id,
        chapter_number=next_num,
        title=(chapter_data.title or f"Chapter {next_num}").strip(),
        source_url=chapter_data.source_url or "",
        original_content=(chapter_data.content or "").strip(),
        word_count=len(chapter_data.content or ""),
        is_translated=False,
    )
    db.add(chapter)
    db.flush()
    novel.total_chapters = db.query(Chapter).filter(Chapter.novel_id == novel_id).count()
    db.commit()
    db.refresh(chapter)
    return {"status": "ok", "chapter_id": chapter.id, "chapter_number": next_num}


@router.get("/api/novels/{novel_id}/chapters", response_model=List[ChapterResponse])
async def list_chapters(novel_id: int, db: Session = Depends(get_db_session)):
    """List all chapters for a novel"""
    chapters = db.query(Chapter).filter(
        Chapter.novel_id == novel_id
    ).order_by(Chapter.chapter_number).all()
    return chapters


@router.get("/api/novels/{novel_id}/chapters/{chapter_number}", response_model=ChapterResponse)
async def get_chapter(novel_id: int, chapter_number: int, db: Session = Depends(get_db_session)):
    """Get a specific chapter"""
    chapter = db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.chapter_number == chapter_number
    ).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return chapter


@router.post("/api/chapters/{chapter_id}/bookmarks")
async def add_bookmark(chapter_id: int, payload: dict, db: Session = Depends(get_db_session)):
    """Save a highlight/bookmark on a chapter."""
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    quote = (payload.get("quote") or "").strip()
    if not quote:
        raise HTTPException(status_code=422, detail="quote required")
    bm = Bookmark(
        novel_id=chapter.novel_id,
        chapter_id=chapter.id,
        chapter_number=chapter.chapter_number,
        quote=quote[:2000],
        note=(payload.get("note") or "").strip()[:1000],
        color=(payload.get("color") or "yellow"),
    )
    db.add(bm)
    db.commit()
    db.refresh(bm)
    return {"status": "ok", "id": bm.id}


@router.get("/api/chapters/{chapter_id}/diary")
async def get_diary(chapter_id: int, db: Session = Depends(get_db_session)):
    """Get the user's diary entry for a chapter (empty string if none)."""
    ch = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Chapter not found")
    entry = db.query(DiaryEntry).filter(DiaryEntry.chapter_id == chapter_id).first()
    return {"chapter_id": chapter_id, "chapter_number": ch.chapter_number,
            "content": entry.content if entry else ""}


@router.put("/api/chapters/{chapter_id}/diary")
async def put_diary(chapter_id: int, payload: dict, db: Session = Depends(get_db_session)):
    """Save the user's diary entry for a chapter (upsert)."""
    ch = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Chapter not found")
    content = (payload.get("content") or "").strip()
    entry = db.query(DiaryEntry).filter(DiaryEntry.chapter_id == chapter_id).first()
    if content:
        if not entry:
            entry = DiaryEntry(novel_id=ch.novel_id, chapter_id=ch.id,
                               chapter_number=ch.chapter_number, content=content)
            db.add(entry)
        else:
            entry.content = content
        db.commit()
        return {"status": "ok"}
    else:
        if entry:
            db.delete(entry)
            db.commit()
        return {"status": "ok", "deleted": True}
