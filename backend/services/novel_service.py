"""
Novel and Chapter business logic services for NyaaReader.
"""
import asyncio
from datetime import datetime
import json
import logging
import os
import sys
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException
from database import get_db_session, init_db
from models import Chapter, Novel, NovelMemory, NovelSettings
from scrapers import auto_detect_and_scrape, get_scraper_for_url
from translator import (
    MemoryContext,
    RelayAuthError,
    get_translator,
    sync_glossary_entries,
)

logger = logging.getLogger("novel-reader.novel_service")

# Politeness delay between consecutive source fetches (seconds) — avoids rate limits
FETCH_DELAY_SECONDS = float(os.getenv("FETCH_DELAY_SECONDS", "15"))


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


def _sleep_between_fetches():
    """Sleep between consecutive source fetches to avoid rate limiting."""
    import time
    delay = _get_main_attr("FETCH_DELAY_SECONDS", FETCH_DELAY_SECONDS)
    time.sleep(delay)


def _load_glossary(mem_row) -> Optional[list]:
    """Return structured glossary entries. Automatically synchronizes newly
    learned characters and terms into the structured glossary list while
    preserving user locks and custom edits."""
    if not mem_row:
        return []
    raw = getattr(mem_row, "glossary_entries", None)
    existing = json.loads(raw) if isinstance(raw, str) else (raw or [])
    synced = sync_glossary_entries(mem_row.characters or "", mem_row.terms or "", existing)
    if synced and synced != existing:
        try:
            mem_row.glossary_entries = _dump_glossary(synced)
        except Exception:
            pass
    return synced or existing or []


def _dump_glossary(entries):
    """Return the glossary list as-is. The column is SQLAlchemy JSON, which
    serializes natively — json.dumps() here would DOUBLE-encode (string inside
    JSON). Read-side code already handles legacy double-encoded values."""
    if not entries:
        return []
    return entries


def _locked_terms(db, novel_id: int) -> list:
    """Locked glossary names (terms locked by the user, must appear in translations)."""
    mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
    if not mem or not mem.glossary_entries:
        return []
    try:
        entries = json.loads(mem.glossary_entries) if isinstance(mem.glossary_entries, str) else mem.glossary_entries
    except Exception as e:
        logger.warning(f"_locked_terms: could not parse glossary_entries for novel {novel_id}: {e}")
        return []
    terms = []
    for e in entries or []:
        if isinstance(e, dict) and e.get("locked") and e.get("translated"):
            terms.append(str(e["translated"]).strip())
    return [t for t in terms if t]


def _translate_chapter(db, chapter, quality: str = "balanced", force: bool = False):
    """Shared memory-aware translate used by the JSON API and HTML pages."""
    if not chapter.original_content:
        raise HTTPException(status_code=400, detail="No original content to translate")

    if chapter.is_translated and not force:
        return chapter

    novel = db.query(Novel).filter(Novel.id == chapter.novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    mem_row = db.query(NovelMemory).filter(NovelMemory.novel_id == novel.id).first()
    if not mem_row:
        mem_row = NovelMemory(novel_id=novel.id)
        db.add(mem_row)
        db.flush()

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
        raise HTTPException(
            status_code=500,
            detail="Translation unavailable: no API key configured (set FALLBACK_API_KEY in Settings)",
        )
    session_id = f"nyaa-novel-{novel.id}"
    result = translator.translate_with_memory(
        chapter.original_content,
        novel.original_language,
        novel.target_language,
        quality,
        memory=memory,
        session_id=session_id,
    )

    if result is None or not getattr(result, "success", False):
        detail = getattr(result, "error", "translation returned no result")
        raise HTTPException(status_code=500, detail=f"Translation failed: {detail}")

    chapter.translated_content = result.translated_text
    chapter.is_translated = True
    chapter.translation_model = result.model_used
    chapter.updated_at = datetime.utcnow()

    if chapter.title and not chapter.title_translated:
        try:
            translated_title = translator.translate_short(
                chapter.title, novel.original_language, novel.target_language,
                session_id=session_id,
            )
            if translated_title and translated_title.strip():
                chapter.title_translated = translated_title.strip()
        except Exception as e:
            logger.warning(f"title translate failed for ch {chapter.chapter_number}: {e}")

    if result.memory:
        mem_row.characters = result.memory.characters
        mem_row.terms = result.memory.terms
        mem_row.plot = result.memory.plot
        mem_row.arc_plot = result.memory.arc_plot
        mem_row.chapter_plot = result.memory.chapter_plot
        mem_row.memory = result.memory.memory
        mem_row.glossary_entries = _dump_glossary(result.memory.glossary_entries)
        mem_row.updated_at = datetime.utcnow()
        try:
            if result.memory.needs_compaction():
                logger.info(f"Memory over budget — compacting (novel {novel.id})")
                compacted = translator.compact_memory(result.memory)
                if compacted is not result.memory:
                    mem_row.characters = compacted.characters
                    mem_row.terms = compacted.terms
                    mem_row.plot = compacted.plot
                    mem_row.arc_plot = compacted.arc_plot
                    mem_row.chapter_plot = compacted.chapter_plot
                    mem_row.memory = compacted.memory
        except Exception as e:
            logger.warning(f"memory compaction check failed: {e}")

    db.commit()
    db.refresh(chapter)
    return chapter


def _translate_chapter_bg(novel_id: int, chapter_number: int, quality: str = "balanced"):
    """Background translate: own DB session.
    Records failures on the chapter's last_error column for the retry queue."""
    from database import SessionLocal
    lock_fn = _get_main_attr("_novel_lock", lambda nid: None)
    lock = lock_fn(novel_id) if lock_fn else None

    db = SessionLocal()
    try:
        def _do_translate():
            chapter = db.query(Chapter).filter(
                Chapter.novel_id == novel_id,
                Chapter.chapter_number == chapter_number,
            ).first()
            if chapter and chapter.original_content and not chapter.is_translated:
                try:
                    tr_fn = _get_main_attr("_translate_chapter", _translate_chapter)
                    tr_fn(db, chapter, quality, force=False)
                    if chapter.is_translated:
                        chapter.last_error = ""
                        db.commit()
                except HTTPException as e:
                    chapter.last_error = str(e.detail)[:500]
                    db.commit()
                    logger.warning(f"bg translate {novel_id}/{chapter_number}: {e.detail}")
                except RelayAuthError as e:
                    logger.error(f"bg translate {novel_id}/{chapter_number}: {e}")
                    raise
                except Exception as e:
                    chapter.last_error = str(e)[:500]
                    db.commit()
                    logger.warning(f"bg translate {novel_id}/{chapter_number}: {e}")

        if lock:
            with lock:
                _do_translate()
        else:
            _do_translate()
    finally:
        db.close()


def _fetch_chapter_content_sync(source_url: str, polite_delay: bool = True):
    """Fetch one chapter's content synchronously (used by background jobs)."""
    import scrapers
    scraper = scrapers.get_scraper_for_url(source_url)
    if not scraper:
        return None

    async def _fetch():
        async with scraper:
            return await scraper.get_chapter_content(source_url)

    try:
        result = asyncio.run(_fetch())
        if polite_delay:
            _sleep_between_fetches()
        return result
    except Exception as e:
        logger.warning(f"fetch {source_url} failed: {e}")
        return None


def _get_novel_info_sync(scraper, source_url: str):
    """Sync wrapper around the scraper's get_novel_info method."""
    async def _fetch():
        async with scraper:
            return await scraper.get_novel_info(source_url)
    try:
        return asyncio.run(_fetch())
    except Exception as e:
        logger.warning(f"novel info fetch failed: {e}")
        return None


async def _create_novel_from_url(db, source_url: str, target_language: str,
                                 auto_translate: bool, background_tasks=None):
    """Shared scrape+create logic used by the JSON API and the HTML form."""
    existing = db.query(Novel).filter(Novel.source_url == source_url).first()
    if existing:
        raise ValueError("Novel already exists")

    try:
        novel_info = await auto_detect_and_scrape(
            source_url,
            delay=1.0,
            timeout=30,
        )
        if not novel_info:
            raise ValueError("Failed to scrape novel")
    except Exception as e:
        raise ValueError(f"Scraping failed: {str(e)}")

    source_site = urlparse(source_url).netloc.lower().replace("www.", "")

    novel = Novel(
        title=novel_info.title,
        author=novel_info.author,
        description=novel_info.description,
        cover_url=novel_info.cover_url,
        source_url=source_url,
        source_site=source_site,
        original_language=novel_info.original_language,
        target_language=target_language,
        total_chapters=novel_info.total_chapters,
    )
    db.add(novel)
    db.flush()

    settings = NovelSettings(
        novel_id=novel.id,
        auto_translate=auto_translate,
    )
    db.add(settings)

    for ch in novel_info.chapters:
        chapter = Chapter(
            novel_id=novel.id,
            chapter_number=ch.number,
            title=ch.title,
            source_url=ch.url,
            word_count=ch.word_count,
        )
        db.add(chapter)

    db.commit()

    actual = db.query(Chapter).filter(Chapter.novel_id == novel.id).count()
    if novel.total_chapters != actual:
        novel.total_chapters = actual
        db.commit()

    db.refresh(novel)

    if background_tasks is not None:
        fetch_init_fn = _get_main_attr("fetch_initial_chapters", fetch_initial_chapters)
        trans_meta_fn = _get_main_attr("translate_novel_meta_bg", lambda nid: None)
        background_tasks.add_task(fetch_init_fn, novel.id, auto_translate)
        background_tasks.add_task(trans_meta_fn, novel.id)

    return novel


async def fetch_initial_chapters(novel_id: int, auto_translate: bool):
    """Background task to fetch the first 5 chapters, right after a novel is added."""
    fetch_range_fn = _get_main_attr("fetch_chapters_range", fetch_chapters_range)
    await fetch_range_fn(novel_id, 1, 5, auto_translate)


async def fetch_chapters_range(novel_id: int, start: int, count: int, do_translate: bool):
    """Background task: fetch + optionally translate chapters [start, start+count)."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel:
            return

        chapters = db.query(Chapter).filter(
            Chapter.novel_id == novel_id,
            Chapter.chapter_number >= start,
            Chapter.chapter_number < start + count,
        ).order_by(Chapter.chapter_number).all()

        translator = _get_translator_instance() if do_translate else None

        scraper = None
        first_url = chapters[0].source_url if chapters else None
        if first_url:
            import scrapers
            scraper = scrapers.get_scraper_for_url(first_url)

        if scraper:
            await scraper.__aenter__()

        try:
            for chapter in chapters:
                if chapter.original_content:
                    continue

                if not scraper:
                    continue

                try:
                    ch_data = await scraper.get_chapter_content(chapter.source_url)
                    delay = _get_main_attr("FETCH_DELAY_SECONDS", FETCH_DELAY_SECONDS)
                    if not (ch_data and ch_data.content):
                        await asyncio.sleep(delay)
                        continue

                    chapter.original_content = ch_data.content
                    chapter.word_count = ch_data.word_count

                    await asyncio.sleep(delay)

                    if translator:
                        result = await asyncio.to_thread(
                            translator.translate_chapter,
                            ch_data.content,
                            novel.original_language,
                            novel.target_language,
                            "balanced",
                        )
                        if result.success:
                            chapter.translated_content = result.translated_text
                            chapter.is_translated = True
                            chapter.translated_word_count = result.output_tokens * 4
                            chapter.translation_model = result.model_used
                            chapter.translation_cost = result.estimated_cost

                    db.commit()
                except Exception as e:
                    logger.warning(f"fetch_chapters_range: chapter {chapter.chapter_number} failed: {e}")
                    db.rollback()
        finally:
            if scraper:
                try:
                    await scraper.__aexit__(None, None, None)
                except Exception:
                    pass
    finally:
        db.close()
