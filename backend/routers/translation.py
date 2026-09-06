"""
Translation, memory, and glossary endpoints.
"""
import asyncio
from datetime import datetime
import json
import logging
import queue
import sys
import threading
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database import get_db_session
from models import Chapter, Novel, NovelMemory
from schemas import (
    BatchMarkReadRequest,
    BatchTranslateSelectedRequest,
    ChapterResponse,
    TranslateRequest,
)
from security import _COOKIE_NAME, _auth_enabled, _verify_session
from services.job_service import (
    _async_novel_lock,
    _batch_running,
    _retry_failed_bg,
    _retranslate_bg,
    _retranslate_drift_bg,
    _translate_selected_bg,
    check_updates_bg,
    retranslate_match_bg,
    translate_ahead_bg,
    translate_novel_meta_bg,
    translate_titles_bg,
    translate_to_end_bg,
    translate_memory_bg,
)
from services.novel_service import (
    _dump_glossary,
    _load_glossary,
    _locked_terms,
    _translate_chapter,
    _translate_chapter_bg,
)
from translator import MemoryContext, get_translator

logger = logging.getLogger("novel-reader.translation_router")


def _get_main_attr(name: str, fallback):
    main_mod = sys.modules.get("main")
    if main_mod is not None and hasattr(main_mod, name):
        return getattr(main_mod, name)
    return fallback


def _get_translator_instance():
    main_mod = sys.modules.get("main")
    if main_mod is not None and hasattr(main_mod, "get_translator"):
        try:
            t = main_mod.get_translator()
            if t is not None:
                return t
        except Exception:
            pass
    import translator as _tr_mod
    return _tr_mod.get_translator()


router = APIRouter(tags=["translation"])


@router.post("/api/chapters/{chapter_id}/translate", response_model=ChapterResponse)
async def translate_chapter(
    chapter_id: int,
    request: TranslateRequest,
    db: Session = Depends(get_db_session)
):
    """Translate a chapter (JSON API)"""
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    
    tr_fn = _get_main_attr("_translate_chapter", _translate_chapter)
    chapter = await asyncio.to_thread(
        tr_fn, db, chapter, request.quality, request.force_retranslate
    )
    return chapter


@router.post("/api/novels/{novel_id}/chapters/{chapter_number}/translate")
async def translate_chapter_start(
    novel_id: int,
    chapter_number: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db_session),
):
    """Start translating a chapter in the background (JSON, no redirect)."""
    chapter = db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.chapter_number == chapter_number,
    ).first()
    if not chapter or not chapter.original_content:
        raise HTTPException(status_code=400, detail="Chapter content not fetched yet")
    if chapter.is_translated:
        return {"status": "already_translated"}
    trans_bg_fn = _get_main_attr("_translate_chapter_bg", _translate_chapter_bg)
    background_tasks.add_task(trans_bg_fn, novel_id, chapter_number, "balanced")
    return {"status": "started"}


@router.get("/api/novels/{novel_id}/chapters/{chapter_number}/translate/stream")
async def translate_chapter_stream(
    novel_id: int,
    chapter_number: int,
    force: bool = False,
    request: Request = None,
    db: Session = Depends(get_db_session),
):
    """Progressive SSE streaming translation for chapter reading."""
    auth_en_fn = _get_main_attr("_auth_enabled", _auth_enabled)
    if auth_en_fn(db):
        # request is injected by FastAPI; the None default only exists so the
        # module-level alias in main.py stays importable without a Request.
        cookie = request.cookies.get(_COOKIE_NAME) if request is not None else None
        if not cookie or not _verify_session(cookie):
            raise HTTPException(status_code=401, detail="Login required")

    chapter = db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.chapter_number == chapter_number,
    ).first()
    if not chapter or not chapter.original_content:
        raise HTTPException(status_code=400, detail="Chapter content not fetched yet")

    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    async def event_generator():
        if chapter.is_translated and not force:
            yield f"event: init\ndata: {json.dumps({'chapter_number': chapter_number, 'title': chapter.title_translated or chapter.title, 'cached': True})}\n\n"
            yield f"event: delta\ndata: {json.dumps({'delta': chapter.translated_content})}\n\n"
            yield f"event: done\ndata: {json.dumps({'status': 'completed', 'translated_content': chapter.translated_content, 'title_translated': chapter.title_translated, 'cached': True})}\n\n"
            return

        yield f"event: init\ndata: {json.dumps({'chapter_number': chapter_number, 'title': chapter.title or f'Chapter {chapter_number}'})}\n\n"

        from database import SessionLocal
        stream_db = SessionLocal()
        try:
            mem_row = stream_db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
            if not mem_row:
                mem_row = NovelMemory(novel_id=novel_id)
                stream_db.add(mem_row)
                stream_db.flush()

            memory = MemoryContext(
                general_instruction=mem_row.general_instruction or "",
                characters=mem_row.characters or "",
                terms=mem_row.terms or "",
                plot=mem_row.plot or "",
                arc_plot=mem_row.arc_plot or "",
                chapter_plot=mem_row.chapter_plot or "",
                memory=mem_row.memory or "",
                glossary_entries=_load_glossary(mem_row),
            )

            translator = _get_translator_instance()
            if translator is None:
                yield f"event: error\ndata: {json.dumps({'error': 'Translator not configured'})}\n\n"
                return

            q = queue.Queue()

            def _stream_worker():
                try:
                    gen = translator.translate_with_memory_stream(
                        chapter.original_content,
                        source_lang=novel.original_language or "zh",
                        target_lang=novel.target_language or "en",
                        quality="balanced",
                        memory=memory,
                        session_id=f"nyaa-novel-{novel.id}",
                    )
                    for chunk in gen:
                        q.put(chunk)
                except Exception as ex:
                    q.put(ex)
                finally:
                    q.put(None)

            thread = threading.Thread(target=_stream_worker, daemon=True)
            thread.start()

            full_text = ""
            final_result = None

            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.04)
                    continue

                if item is None:
                    break
                if isinstance(item, Exception):
                    yield f"event: error\ndata: {json.dumps({'error': str(item)})}\n\n"
                    return

                if not getattr(item, "is_final", False):
                    delta_text = getattr(item, "delta", "")
                    if delta_text:
                        full_text += delta_text
                        yield f"event: delta\ndata: {json.dumps({'delta': delta_text})}\n\n"
                else:
                    final_result = getattr(item, "result", None)

            final_translated_text = ""
            if final_result and getattr(final_result, "success", False) and final_result.translated_text:
                final_translated_text = final_result.translated_text
            elif full_text.strip():
                final_translated_text = full_text.strip()

            if final_translated_text:
                ch_obj = stream_db.query(Chapter).filter(
                    Chapter.novel_id == novel_id,
                    Chapter.chapter_number == chapter_number,
                ).first()
                if ch_obj:
                    ch_obj.translated_content = final_translated_text
                    ch_obj.translated_word_count = len(final_translated_text.split())
                    ch_obj.is_translated = True
                    ch_obj.translation_model = getattr(final_result, "model_used", "") or getattr(translator, "model_name", "ai")
                    ch_obj.last_error = ""
                    ch_obj.updated_at = datetime.utcnow()

                    if ch_obj.title and not ch_obj.title_translated:
                        try:
                            ch_obj.title_translated = translator.translate_short(
                                ch_obj.title, novel.original_language or "zh", novel.target_language or "en"
                            )
                        except Exception:
                            ch_obj.title_translated = ch_obj.title

                    if final_result and getattr(final_result, "memory", None):
                        m = final_result.memory
                        mem_row.characters = getattr(m, "characters", "") or ""
                        mem_row.terms = getattr(m, "terms", "") or ""
                        mem_row.plot = getattr(m, "plot", "") or ""
                        mem_row.arc_plot = getattr(m, "arc_plot", "") or ""
                        mem_row.chapter_plot = getattr(m, "chapter_plot", "") or ""
                        mem_row.memory = getattr(m, "memory", "") or ""
                        mem_row.glossary_entries = _dump_glossary(getattr(m, "glossary_entries", []))

                    stream_db.commit()
                    yield f"event: done\ndata: {json.dumps({'status': 'completed', 'translated_content': final_translated_text, 'title_translated': ch_obj.title_translated or ch_obj.title})}\n\n"
            else:
                err = getattr(final_result, "error", None) or "Translation failed"
                yield f"event: error\ndata: {json.dumps({'error': err})}\n\n"
        finally:
            stream_db.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@router.post("/api/novels/{novel_id}/chapters/{chapter_number}/fetch")
async def fetch_chapter_json(novel_id: int, chapter_number: int, force: bool = False,
                             db: Session = Depends(get_db_session)):
    """Fetch one chapter's content synchronously (JSON)."""
    chapter = db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.chapter_number == chapter_number,
    ).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    if not chapter.original_content or force:
        gs_fn = _get_main_attr("get_scraper_for_url", lambda url: None)
        from scrapers import get_scraper_for_url as _default_gs
        scraper = gs_fn(chapter.source_url) or _default_gs(chapter.source_url)
        if scraper:
            async with scraper:
                ch_data = await scraper.get_chapter_content(chapter.source_url)
                if ch_data and ch_data.content:
                    chapter.original_content = ch_data.content
                    chapter.word_count = ch_data.word_count
                    chapter.is_translated = False
                    chapter.translated_content = None
                    chapter.last_error = ""
                    db.commit()
    return {
        "status": "ok" if chapter.original_content else "failed",
        "word_count": chapter.word_count,
        "is_translated": chapter.is_translated,
    }


@router.get("/api/novels/{novel_id}/memory")
async def get_memory(novel_id: int, db: Session = Depends(get_db_session)):
    """Get the current AI memory / knowledge for a novel."""
    mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
    if not mem:
        return {
            "novel_id": novel_id,
            "general_instruction": "",
            "characters": "",
            "terms": "",
            "plot": "",
            "arc_plot": "",
            "chapter_plot": "",
            "memory": "",
            "glossary_entries": [],
        }
    return {
        "novel_id": novel_id,
        "general_instruction": mem.general_instruction or "",
        "characters": mem.characters or "",
        "terms": mem.terms or "",
        "plot": mem.plot or "",
        "arc_plot": mem.arc_plot or "",
        "chapter_plot": mem.chapter_plot or "",
        "memory": mem.memory or "",
        "glossary_entries": _load_glossary(mem),
    }


@router.put("/api/novels/{novel_id}/memory")
async def update_memory(
    novel_id: int,
    payload: dict,
    db: Session = Depends(get_db_session)
):
    """User edits/pre-seeds the AI memory."""
    mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
    if not mem:
        mem = NovelMemory(novel_id=novel_id)
        db.add(mem)
    for key in ("general_instruction", "characters", "terms", "plot", "arc_plot", "chapter_plot", "memory"):
        if key in payload:
            setattr(mem, key, (payload[key] or "").strip())
    if "glossary_entries" in payload:
        mem.glossary_entries = _dump_glossary(payload["glossary_entries"])
    mem.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


@router.get("/api/novels/{novel_id}/drift-count")
async def drift_count(novel_id: int, db: Session = Depends(get_db_session)):
    """Chapters whose translated content misses a LOCKED glossary term."""
    locked = _locked_terms(db, novel_id)
    if not locked:
        return {"drift": 0, "locked_terms": []}
    chapters = (db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.is_translated == True,
        Chapter.translated_content.isnot(None),
    ).all())
    drift = []
    for ch in chapters:
        content = ch.translated_content or ""
        missing = [t for t in locked if t and t.lower() not in content.lower()]
        if missing:
            drift.append({"chapter_number": ch.chapter_number,
                          "title": ch.title_translated or ch.title or "",
                          "missing": missing})
    return {"drift": len(drift), "locked_terms": locked, "chapters": drift[:200]}


@router.post("/api/novels/{novel_id}/retranslate-drift")
async def retranslate_drift(novel_id: int, background_tasks: BackgroundTasks = None,
                            db: Session = Depends(get_db_session)):
    """Retranslate chapters that miss a locked glossary term."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    locked = _locked_terms(db, novel_id)
    if not locked:
        return {"status": "none", "pending": 0, "reason": "no locked terms"}
    chapters = (db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.is_translated == True,
        Chapter.translated_content.isnot(None),
    ).all())
    targets = [ch for ch in chapters
               if any(t and t.lower() not in (ch.translated_content or "").lower() for t in locked)]
    if not targets:
        return {"status": "none", "pending": 0, "reason": "no drift found"}
    async_lock_fn = _get_main_attr("_async_novel_lock", _async_novel_lock)
    batch_run_fn = _get_main_attr("_batch_running", _batch_running)
    async with async_lock_fn(novel_id):
        if batch_run_fn(novel_id):
            return {"status": "already_running", "pending": 0}
        drift_bg_fn = _get_main_attr("_retranslate_drift_bg", _retranslate_drift_bg)
        background_tasks.add_task(drift_bg_fn, novel_id, [c.chapter_number for c in targets])
    return {"status": "started", "pending": len(targets)}


@router.get("/api/novels/{novel_id}/failed-count")
async def failed_count(novel_id: int, db: Session = Depends(get_db_session)):
    """Number of chapters with a recorded translation error."""
    count = db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.last_error.isnot(None),
        Chapter.last_error != "",
        Chapter.is_translated == False,
    ).count()
    return {"failed": count}


@router.post("/api/novels/{novel_id}/retry-failed")
async def retry_failed(novel_id: int, background_tasks: BackgroundTasks = None,
                       db: Session = Depends(get_db_session)):
    """Retry chapters whose last translation attempt failed."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    count = db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.last_error.isnot(None),
        Chapter.last_error != "",
        Chapter.is_translated == False,
    ).count()
    if count == 0:
        return {"status": "none", "pending": 0}
    async_lock_fn = _get_main_attr("_async_novel_lock", _async_novel_lock)
    batch_run_fn = _get_main_attr("_batch_running", _batch_running)
    async with async_lock_fn(novel_id):
        if batch_run_fn(novel_id):
            return {"status": "already_running", "pending": 0}
        retry_fn = _get_main_attr("_retry_failed_bg", _retry_failed_bg)
        background_tasks.add_task(retry_fn, novel_id)
    return {"status": "started", "pending": count}


@router.post("/api/novels/{novel_id}/translate-titles")
async def translate_titles(novel_id: int, background_tasks: BackgroundTasks = None,
                           db: Session = Depends(get_db_session)):
    """Background: translate only the chapter titles still missing title_translated."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    missing = db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.title.isnot(None),
        (Chapter.title_translated.is_(None)) | (Chapter.title_translated == ""),
    ).count()
    if missing == 0:
        return {"status": "none", "pending": 0}
    async_lock_fn = _get_main_attr("_async_novel_lock", _async_novel_lock)
    batch_run_fn = _get_main_attr("_batch_running", _batch_running)
    async with async_lock_fn(novel_id):
        if batch_run_fn(novel_id):
            return {"status": "already_running", "pending": 0}
        titles_fn = _get_main_attr("translate_titles_bg", translate_titles_bg)
        background_tasks.add_task(titles_fn, novel_id)
    return {"status": "started", "pending": missing}


@router.post("/api/novels/{novel_id}/translate-meta")
async def translate_novel_meta(novel_id: int, background_tasks: BackgroundTasks = None,
                               db: Session = Depends(get_db_session)):
    """Retry translating the NOVEL title + description."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    pending = 0
    if not novel.title_translated and novel.title:
        pending += 1
    if not novel.description_translated and novel.description:
        pending += 1
    if pending == 0:
        return {"status": "none", "pending": 0}
    async_lock_fn = _get_main_attr("_async_novel_lock", _async_novel_lock)
    batch_run_fn = _get_main_attr("_batch_running", _batch_running)
    async with async_lock_fn(novel_id):
        if batch_run_fn(novel_id):
            return {"status": "already_running", "pending": 0}
        meta_fn = _get_main_attr("translate_novel_meta_bg", translate_novel_meta_bg)
        background_tasks.add_task(meta_fn, novel_id)
    return {"status": "started", "pending": pending}


@router.post("/api/novels/{novel_id}/translate-memory")
async def translate_memory(novel_id: int, background_tasks: BackgroundTasks = None,
                           db: Session = Depends(get_db_session)):
    """Background: translate untranslated glossary and memory items for a novel."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
    entries = _load_glossary(mem) if mem else []
    pending = 0
    if entries:
        for e in entries:
            src = (e.get("source") or "").strip()
            trans = (e.get("translated") or "").strip()
            if src and (not trans or trans == src):
                pending += 1
    elif mem and (mem.characters or mem.terms):
        pending = 1

    if pending == 0 and (not mem or (not mem.characters and not mem.terms and not entries)):
        return {"status": "none", "pending": 0}

    async_lock_fn = _get_main_attr("_async_novel_lock", _async_novel_lock)
    batch_run_fn = _get_main_attr("_batch_running", _batch_running)
    async with async_lock_fn(novel_id):
        if batch_run_fn(novel_id):
            return {"status": "already_running", "pending": 0}
        mem_fn = _get_main_attr("translate_memory_bg", translate_memory_bg)
        background_tasks.add_task(mem_fn, novel_id)
    return {"status": "started", "pending": pending or 1}


@router.post("/api/novels/{novel_id}/retranslate")
async def retranslate_novel(novel_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db_session)):
    """Re-translate all already-translated chapters in the background."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    count = db.query(Chapter).filter(
        Chapter.novel_id == novel_id, Chapter.is_translated == True).count()
    if count == 0:
        raise HTTPException(status_code=400, detail="No translated chapters to re-translate")
    async_lock_fn = _get_main_attr("_async_novel_lock", _async_novel_lock)
    batch_run_fn = _get_main_attr("_batch_running", _batch_running)
    async with async_lock_fn(novel_id):
        if batch_run_fn(novel_id):
            return {"status": "already_running", "pending": 0}
        retrans_fn = _get_main_attr("_retranslate_bg", _retranslate_bg)
        background_tasks.add_task(retrans_fn, novel_id)
    return {"status": "started", "chapters": count}


@router.post("/api/novels/{novel_id}/retranslate-match")
async def retranslate_match(novel_id: int, payload: dict = None,
                            background_tasks: BackgroundTasks = None,
                            db: Session = Depends(get_db_session)):
    """Background: retranslate ONLY chapters whose translated_content contains needle."""
    needle = (payload or {}).get("needle", "")
    needle = needle.strip()
    if not needle:
        raise HTTPException(status_code=422, detail="needle required")
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    matched = db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.is_translated == True,
        Chapter.translated_content.contains(needle, autoescape=True),
    ).count()
    if matched == 0:
        return {"status": "none", "pending": 0}
    async_lock_fn = _get_main_attr("_async_novel_lock", _async_novel_lock)
    batch_run_fn = _get_main_attr("_batch_running", _batch_running)
    async with async_lock_fn(novel_id):
        if batch_run_fn(novel_id):
            return {"status": "already_running", "pending": 0}
        match_fn = _get_main_attr("retranslate_match_bg", retranslate_match_bg)
        background_tasks.add_task(match_fn, novel_id, needle)
    return {"status": "started", "pending": matched}


@router.post("/api/novels/{novel_id}/translate-to-end")
async def translate_to_end(novel_id: int, background_tasks: BackgroundTasks = None,
                           db: Session = Depends(get_db_session)):
    """Background: fetch+translate every remaining untranslated chapter (sequential)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    async_lock_fn = _get_main_attr("_async_novel_lock", _async_novel_lock)
    batch_run_fn = _get_main_attr("_batch_running", _batch_running)
    async with async_lock_fn(novel_id):
        if batch_run_fn(novel_id):
            return {"status": "already_running", "pending": 0}
        pending = db.query(Chapter).filter(
            Chapter.novel_id == novel_id,
            Chapter.is_translated == False,
        ).count()
        if pending == 0:
            return {"status": "none", "pending": 0}
        to_end_fn = _get_main_attr("translate_to_end_bg", translate_to_end_bg)
        background_tasks.add_task(to_end_fn, novel_id)
    return {"status": "started", "pending": pending}


@router.post("/api/novels/{novel_id}/translate-ahead")
async def translate_ahead(novel_id: int, after_chapter: int,
                          count: int = 5,
                          background_tasks: BackgroundTasks = None,
                          db: Session = Depends(get_db_session)):
    """Queue the next N untranslated chapters (fetch+translate) in the background."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    existing = db.query(Chapter).filter(
        Chapter.novel_id == novel_id, Chapter.chapter_number > after_chapter,
        Chapter.is_translated == False).count()
    if existing == 0:
        return {"status": "none", "pending": 0}
    async_lock_fn = _get_main_attr("_async_novel_lock", _async_novel_lock)
    batch_run_fn = _get_main_attr("_batch_running", _batch_running)
    async with async_lock_fn(novel_id):
        if batch_run_fn(novel_id):
            return {"status": "already_running", "pending": 0}
        ahead_fn = _get_main_attr("translate_ahead_bg", translate_ahead_bg)
        background_tasks.add_task(ahead_fn, novel_id, after_chapter, count)
    return {"status": "started", "pending": min(existing, count)}


@router.post("/api/novels/{novel_id}/batch-translate-selected")
async def batch_translate_selected(
    novel_id: int,
    req: BatchTranslateSelectedRequest,
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db_session),
):
    """Background: translate specific list of selected chapters sequentially."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    if not req.chapters:
        return {"status": "none", "count": 0}
    async_lock_fn = _get_main_attr("_async_novel_lock", _async_novel_lock)
    batch_run_fn = _get_main_attr("_batch_running", _batch_running)
    async with async_lock_fn(novel_id):
        if batch_run_fn(novel_id):
            return {"status": "already_running", "count": 0}
        selected_fn = _get_main_attr("_translate_selected_bg", _translate_selected_bg)
        background_tasks.add_task(selected_fn, novel_id, req.chapters)
    return {"status": "started", "count": len(req.chapters)}


@router.post("/api/novels/{novel_id}/batch-mark-read")
async def batch_mark_read(
    novel_id: int,
    req: BatchMarkReadRequest,
    db: Session = Depends(get_db_session),
):
    """Mark a set of selected chapters as read or unread."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    if not req.chapters:
        return {"status": "ok", "updated": 0}
    now = datetime.utcnow() if req.is_read else None
    updated = (db.query(Chapter)
               .filter(Chapter.novel_id == novel_id, Chapter.chapter_number.in_(req.chapters))
               .update({Chapter.is_read: req.is_read, Chapter.read_at: now}, synchronize_session="fetch"))
    # NOTE: Novel has no read_chapters column — read counts are computed via
    # COUNT() queries (see novels.py / pages.py). Do not assign transient
    # attributes here; they are silently lost on commit.
    db.commit()
    return {"status": "ok", "updated": updated}


@router.post("/api/novels/{novel_id}/check-updates")
async def check_updates(novel_id: int, background_tasks: BackgroundTasks = None,
                        db: Session = Depends(get_db_session)):
    """Check the source site for new chapters beyond the last known one."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel or novel.source_site == "manual":
        raise HTTPException(status_code=404, detail="Novel not found (or manual novel)")
    async_lock_fn = _get_main_attr("_async_novel_lock", _async_novel_lock)
    batch_run_fn = _get_main_attr("_batch_running", _batch_running)
    async with async_lock_fn(novel_id):
        if batch_run_fn(novel_id):
            return {"status": "already_running"}
        check_up_fn = _get_main_attr("check_updates_bg", check_updates_bg)
        background_tasks.add_task(check_up_fn, novel_id)
    return {"status": "started"}
