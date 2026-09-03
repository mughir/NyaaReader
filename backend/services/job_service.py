"""
Background Batch Job Management & Worker loops for NyaaReader.
"""
import asyncio
from datetime import datetime
import json
import logging
import os
import sys
import threading
import time
from typing import List, Optional

from sqlalchemy import update
from models import BatchJob, Chapter, Novel
from translator import RelayAuthError, get_translator

logger = logging.getLogger("novel-reader.job_service")

# Persistent background-job tracker
_batch_cache = {}

# Per-novel locks
_batch_locks = {}
_batch_locks_guard = threading.Lock()
_async_batch_locks = {}


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


def _novel_lock(novel_id: int) -> threading.Lock:
    with _batch_locks_guard:
        lock = _batch_locks.get(novel_id)
        if lock is None:
            lock = _batch_locks[novel_id] = threading.Lock()
        return lock


def _async_novel_lock(novel_id: int) -> asyncio.Lock:
    with _batch_locks_guard:
        lock = _async_batch_locks.get(novel_id)
        if lock is None:
            lock = _async_batch_locks[novel_id] = asyncio.Lock()
        return lock


JOB_STALL_MINUTES = 10


def _get_or_create_job(novel_id, kind):
    """Return the active BatchJob row for (novel_id, kind) or create one."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        job = db.query(BatchJob).filter(
            BatchJob.novel_id == novel_id, BatchJob.kind == kind,
            BatchJob.running == True).first()
        if not job:
            job = BatchJob(novel_id=novel_id, kind=kind, total=0, done=0,
                           current_label="", running=True)
            db.add(job)
            db.commit()
            db.refresh(job)
        return job.id
    finally:
        db.close()


def _update_job(job_id, **fields):
    from database import SessionLocal
    db = SessionLocal()
    try:
        job = db.query(BatchJob).filter(BatchJob.id == job_id).first()
        if job:
            for k, v in fields.items():
                setattr(job, k, v)
            db.commit()
    finally:
        db.close()


def _set_batch(novel_id, kind, total, label="", args=None):
    """Claim the batch slot for (novel_id). Serialized by _novel_lock so two
    threads in this process can't both pass the check-then-insert window.
    Cross-process double-start is out of scope (single-container app) — the
    DB stall check (JOB_STALL_MINUTES) is the second line of defense."""
    from database import SessionLocal
    args_json = json.dumps(args) if args is not None else ""
    with _novel_lock(novel_id):
        db = SessionLocal()
        try:
            existing = db.query(BatchJob).filter(
                BatchJob.novel_id == novel_id, BatchJob.running == True).order_by(
                BatchJob.id.desc()).first()
            if existing:
                if existing.updated_at and (datetime.utcnow() - existing.updated_at).total_seconds() < JOB_STALL_MINUTES * 60:
                    return False
                existing.kind = kind
                existing.total = total
                existing.done = 0
                existing.current_label = label
                existing.args_json = args_json
                db.commit()
                db.refresh(existing)
            else:
                job = BatchJob(novel_id=novel_id, kind=kind, total=total, done=0,
                               current_label=label, running=True, args_json=args_json)
                db.add(job)
                db.commit()
                db.refresh(job)
        finally:
            db.close()
    cache = _get_main_attr("_batch_cache", _batch_cache)
    cache[novel_id] = {
        "kind": kind, "total": total, "done": 0,
        "current_label": label, "running": True,
        "stop_requested": False
    }
    return True


def _request_batch_stop(novel_id: int) -> bool:
    cache = _get_main_attr("_batch_cache", _batch_cache)
    b = cache.get(novel_id)
    if b and b.get("running"):
        b["stop_requested"] = True
        return True
    from database import SessionLocal
    db = SessionLocal()
    try:
        job = db.query(BatchJob).filter(
            BatchJob.novel_id == novel_id, BatchJob.running == True).order_by(
            BatchJob.id.desc()).first()
        if job:
            job.stop_requested = True
            db.commit()
            b = cache.setdefault(novel_id, {})
            b["running"] = True
            b["stop_requested"] = True
            return True
        return False
    finally:
        db.close()


def _batch_stop_requested(novel_id: int) -> bool:
    cache = _get_main_attr("_batch_cache", _batch_cache)
    b = cache.get(novel_id)
    if b and b.get("stop_requested"):
        return True
    from database import SessionLocal
    db = SessionLocal()
    try:
        job = db.query(BatchJob).filter(
            BatchJob.novel_id == novel_id, BatchJob.running == True).order_by(
            BatchJob.id.desc()).first()
        if job and job.stop_requested:
            return True
        return False
    finally:
        db.close()


def _bump_batch(novel_id, label="", done_inc=1):
    from database import SessionLocal
    db = SessionLocal()
    try:
        res = db.execute(
            update(BatchJob)
            .where(BatchJob.novel_id == novel_id, BatchJob.running == True)
            .values(
                done=BatchJob.done + done_inc,
                current_label=label if label else BatchJob.current_label,
            )
        )
        db.commit()
        cache = _get_main_attr("_batch_cache", _batch_cache)
        b = cache.get(novel_id)
        if b and res.rowcount:
            job = db.query(BatchJob).filter(
                BatchJob.novel_id == novel_id, BatchJob.running == True).order_by(
                BatchJob.id.desc()).first()
            if job:
                b["done"] = job.done
                if label:
                    b["current_label"] = label
    finally:
        db.close()


def _set_batch_total(novel_id, total, label=""):
    from database import SessionLocal
    db = SessionLocal()
    try:
        job = db.query(BatchJob).filter(
            BatchJob.novel_id == novel_id, BatchJob.running == True).order_by(
            BatchJob.id.desc()).first()
        if job:
            job.total = total
            if label:
                job.current_label = label
            db.commit()
            cache = _get_main_attr("_batch_cache", _batch_cache)
            b = cache.get(novel_id)
            if b:
                b["total"] = total
                if label:
                    b["current_label"] = label
    finally:
        db.close()


def _finish_batch(novel_id, label=""):
    bump_fn = _get_main_attr("_bump_batch", _bump_batch)
    clear_fn = _get_main_attr("_clear_batch", _clear_batch)
    if label:
        bump_fn(novel_id, label=label, done_inc=0)
    clear_fn(novel_id)


def _stopped_label(done: int, total: int, what: str) -> str:
    return f"Stopped by user — {done}/{total} {what}"


def _auth_rejected_label(done: int, total: int, what: str) -> str:
    return f"Stopped — relay key rejected, check Settings — {done}/{total} {what}"


def _clear_batch(novel_id):
    from database import SessionLocal
    db = SessionLocal()
    try:
        jobs = db.query(BatchJob).filter(
            BatchJob.novel_id == novel_id, BatchJob.running == True).all()
        for job in jobs:
            job.running = False
            # Keep the real done count — faking done=total lied to the UI
            # on stop/fail ("green done" while chapters were unfinished).
        db.commit()
    finally:
        db.close()
    cache = _get_main_attr("_batch_cache", _batch_cache)
    b = cache.get(novel_id)
    if b:
        b["running"] = False
        # keep b["done"] as-is (real progress), don't snap to total


def _batch_running(novel_id: int, kind: str = None) -> bool:
    from database import SessionLocal
    db = SessionLocal()
    try:
        q = db.query(BatchJob).filter(
            BatchJob.novel_id == novel_id, BatchJob.running == True)
        if kind:
            q = q.filter(BatchJob.kind == kind)
        job = q.order_by(BatchJob.id.desc()).first()
        if not job:
            return False
        if job.updated_at and (datetime.utcnow() - job.updated_at).total_seconds() < JOB_STALL_MINUTES * 60:
            return True
        return False
    finally:
        db.close()


def translate_novel_meta_bg(novel_id: int):
    from database import SessionLocal
    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel:
            return
        if novel.title_translated and novel.description_translated:
            return
        translator = _get_translator_instance()
        if translator is None:
            logger.error(f"translate-meta aborted (novel {novel_id}): no API key configured (set FALLBACK_API_KEY in Settings)")
            return
        set_batch_fn = _get_main_attr("_set_batch", _set_batch)
        if not set_batch_fn(novel_id, "meta", 2):
            return
        outcome = None
        stop_req_fn = _get_main_attr("_batch_stop_requested", _batch_stop_requested)
        bump_fn = _get_main_attr("_bump_batch", _bump_batch)
        if stop_req_fn(novel_id):
            outcome = "Stopped by user"
        else:
            try:
                if not novel.title_translated and novel.title:
                    t = translator.translate_short(
                        novel.title, novel.original_language, novel.target_language)
                    if t and t.strip():
                        novel.title_translated = t.strip()
                        bump_fn(novel_id, label="Novel title")
                        db.commit()
                if not novel.description_translated and novel.description:
                    d = translator.translate_short(
                        novel.description, novel.original_language, novel.target_language)
                    if d and d.strip():
                        novel.description_translated = d.strip()
                        bump_fn(novel_id, label="Synopsis")
                        db.commit()
            except RelayAuthError as e:
                logger.error(f"translate-meta stopped: relay key rejected ({e})")
                outcome = "Stopped — relay key rejected, check Settings"
            except Exception as e:
                logger.warning(f"translate novel meta {novel_id} failed: {e}")
                db.rollback()
                outcome = f"Translation failed: {str(e)[:120]}"
        finish_fn = _get_main_attr("_finish_batch", _finish_batch)
        finish_fn(novel_id, outcome or "Title & synopsis translated")
    finally:
        db.close()


def translate_to_end_bg(novel_id: int):
    from database import SessionLocal
    from services.novel_service import _fetch_chapter_content_sync, _translate_chapter_bg

    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel:
            return
        chapters = (db.query(Chapter)
                    .filter(Chapter.novel_id == novel_id, Chapter.is_translated == False)
                    .order_by(Chapter.chapter_number).all())
        if not chapters:
            return
        set_batch_fn = _get_main_attr("_set_batch", _set_batch)
        if not set_batch_fn(novel_id, "to-end", len(chapters)):
            return
        done = 0
        outcome = None
        stop_req_fn = _get_main_attr("_batch_stop_requested", _batch_stop_requested)
        fetch_sync_fn = _get_main_attr("_fetch_chapter_content_sync", _fetch_chapter_content_sync)
        trans_bg_fn = _get_main_attr("_translate_chapter_bg", _translate_chapter_bg)
        bump_fn = _get_main_attr("_bump_batch", _bump_batch)

        for ch in chapters:
            if stop_req_fn(novel_id):
                logger.info(f"translate-to-end stopped by user (novel {novel_id})")
                outcome = _stopped_label(done, len(chapters), "chapters translated")
                break
            try:
                if not ch.original_content:
                    ch_data = fetch_sync_fn(ch.source_url)
                    if ch_data and ch_data.content:
                        ch.original_content = ch_data.content
                        ch.word_count = getattr(ch_data, "word_count", None)
                        db.commit()
                db.refresh(ch)
                if not ch.original_content:
                    raise RuntimeError("fetch failed — no content")
                trans_bg_fn(novel_id, ch.chapter_number, "balanced")
                db.refresh(ch)
                if ch.is_translated:
                    done += 1
                bump_fn(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
            except RelayAuthError as e:
                logger.error(f"translate-to-end stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(chapters), "chapters translated")
                break
            except Exception as e:
                logger.warning(f"translate-to-end ch{ch.chapter_number} failed: {e}")
                db.rollback()
        finish_fn = _get_main_attr("_finish_batch", _finish_batch)
        finish_fn(novel_id, outcome or f"Translated {done}/{len(chapters)} chapters")
    finally:
        db.close()


def _translate_selected_bg(novel_id: int, chapter_numbers: List[int]):
    from database import SessionLocal
    from services.novel_service import _fetch_chapter_content_sync, _translate_chapter_bg

    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel:
            return
        chapters = (db.query(Chapter)
                    .filter(Chapter.novel_id == novel_id, Chapter.chapter_number.in_(chapter_numbers))
                    .order_by(Chapter.chapter_number).all())
        if not chapters:
            return
        set_batch_fn = _get_main_attr("_set_batch", _set_batch)
        if not set_batch_fn(novel_id, "translate-selected", len(chapters)):
            return
        done = 0
        outcome = None
        stop_req_fn = _get_main_attr("_batch_stop_requested", _batch_stop_requested)
        fetch_sync_fn = _get_main_attr("_fetch_chapter_content_sync", _fetch_chapter_content_sync)
        trans_bg_fn = _get_main_attr("_translate_chapter_bg", _translate_chapter_bg)
        bump_fn = _get_main_attr("_bump_batch", _bump_batch)

        for ch in chapters:
            if stop_req_fn(novel_id):
                outcome = _stopped_label(done, len(chapters), "selected chapters translated")
                break
            try:
                if not ch.original_content and ch.source_url:
                    ch_data = fetch_sync_fn(ch.source_url)
                    if ch_data and ch_data.content:
                        ch.original_content = ch_data.content
                        ch.word_count = getattr(ch_data, "word_count", None)
                        db.commit()
                db.refresh(ch)
                if not ch.original_content:
                    continue
                trans_bg_fn(novel_id, ch.chapter_number, "balanced")
                db.refresh(ch)
                if ch.is_translated:
                    done += 1
                bump_fn(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
            except RelayAuthError as e:
                logger.error(f"translate-selected stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(chapters), "selected chapters translated")
                break
            except Exception as e:
                logger.warning(f"translate-selected ch{ch.chapter_number} failed: {e}")
                db.rollback()
        finish_fn = _get_main_attr("_finish_batch", _finish_batch)
        finish_fn(novel_id, outcome or f"Translated {done}/{len(chapters)} selected chapters")
    finally:
        db.close()


def retranslate_match_bg(novel_id: int, needle: str):
    from database import SessionLocal
    from services.novel_service import _translate_chapter

    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel:
            return
        chapters = db.query(Chapter).filter(
            Chapter.novel_id == novel_id,
            Chapter.is_translated == True,
            Chapter.translated_content.contains(needle, autoescape=True),
        ).order_by(Chapter.chapter_number).all()
        if not chapters:
            return
        set_batch_fn = _get_main_attr("_set_batch", _set_batch)
        if not set_batch_fn(novel_id, "match", len(chapters), args={"needle": needle}):
            return
        done = 0
        outcome = None
        stop_req_fn = _get_main_attr("_batch_stop_requested", _batch_stop_requested)
        trans_fn = _get_main_attr("_translate_chapter", _translate_chapter)
        bump_fn = _get_main_attr("_bump_batch", _bump_batch)

        for ch in chapters:
            if stop_req_fn(novel_id):
                logger.info(f"match-retranslate stopped by user (novel {novel_id})")
                outcome = _stopped_label(done, len(chapters), "chapters retranslated")
                break
            try:
                trans_fn(db, ch, quality="balanced", force=True)
                done += 1
                bump_fn(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
            except RelayAuthError as e:
                logger.error(f"match-retranslate stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(chapters), "chapters retranslated")
                break
            except Exception as e:
                logger.warning(f"match-retranslate ch{ch.chapter_number} failed: {e}")
                db.rollback()
        finish_fn = _get_main_attr("_finish_batch", _finish_batch)
        finish_fn(novel_id, outcome or f"Retranslated {done}/{len(chapters)} matching chapters")
    finally:
        db.close()


def check_updates_bg(novel_id: int):
    from database import SessionLocal
    from scrapers import get_scraper_for_url
    from services.novel_service import (
        _fetch_chapter_content_sync,
        _get_novel_info_sync,
        _translate_chapter_bg,
    )

    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel or novel.source_site == "manual":
            return
        scraper = get_scraper_for_url(novel.source_url)
        if not scraper:
            logger.warning(f"check-updates: no scraper for {novel.source_url}")
            return
        set_batch_fn = _get_main_attr("_set_batch", _set_batch)
        finish_fn = _get_main_attr("_finish_batch", _finish_batch)
        if not set_batch_fn(novel_id, "updates", 1, label="Checking the source for new chapters…"):
            logger.info(f"check-updates novel {novel_id}: another batch owns this novel — skipped")
            return
        try:
            info_sync_fn = _get_main_attr("_get_novel_info_sync", _get_novel_info_sync)
            info = info_sync_fn(scraper, novel.source_url)
            if not info or not info.chapters:
                logger.warning(
                    f"check-updates novel {novel_id}: could not read a chapter list from "
                    f"{novel.source_url} — scraper returned "
                    f"{'nothing' if not info else '0 chapters'}")
                finish_fn(novel_id, "Could not read the source chapter list")
                return
            chapters = info.chapters
            last_known = db.query(Chapter).filter(Chapter.novel_id == novel_id).order_by(
                Chapter.chapter_number.desc()).first()
            known = set(c.source_url for c in db.query(Chapter).filter(
                Chapter.novel_id == novel_id).all())
            new_entries = [c for c in chapters if c.url not in known]
            if not new_entries:
                logger.info(f"check-updates novel {novel_id}: no new chapters "
                            f"({len(chapters)} on source, {len(known)} known)")
                finish_fn(novel_id, "No new chapters")
                return

            start_num = (last_known.chapter_number + 1) if last_known else 1
            added = 0
            for i, entry in enumerate(new_entries):
                num = start_num + i
                ch = Chapter(
                    novel_id=novel_id,
                    chapter_number=num,
                    title=entry.title or f"Chapter {num}",
                    source_url=entry.url,
                    is_translated=False,
                )
                db.add(ch)
                added += 1
            db.flush()
            novel.total_chapters = db.query(Chapter).filter(Chapter.novel_id == novel_id).count()
            db.commit()
            logger.info(f"check-updates novel {novel_id}: added {added} new chapters "
                        f"({start_num}..{start_num + added - 1})")

            todo = (db.query(Chapter)
                    .filter(Chapter.novel_id == novel_id,
                            Chapter.chapter_number >= start_num,
                            Chapter.is_translated == False)
                    .order_by(Chapter.chapter_number)
                    .limit(5).all())
            set_batch_total_fn = _get_main_attr("_set_batch_total", _set_batch_total)
            set_batch_total_fn(novel_id, len(todo) or 1,
                               label=f"Added {added} new chapter(s)")
            fetch_sync_fn = _get_main_attr("_fetch_chapter_content_sync", _fetch_chapter_content_sync)
            trans_bg_fn = _get_main_attr("_translate_chapter_bg", _translate_chapter_bg)
            bump_fn = _get_main_attr("_bump_batch", _bump_batch)

            for ch in todo:
                try:
                    if not ch.original_content:
                        ch_data = fetch_sync_fn(ch.source_url)
                        if ch_data and ch_data.content:
                            ch.original_content = ch_data.content
                            ch.word_count = getattr(ch_data, "word_count", 0) or 0
                            db.commit()
                    db.refresh(ch)
                    if ch.original_content:
                        trans_bg_fn(novel_id, ch.chapter_number, "balanced")
                    bump_fn(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
                except RelayAuthError as e:
                    logger.error(f"check-updates translate stopped: relay key rejected ({e})")
                    break
                except Exception as e:
                    logger.warning(f"check-updates translate ch{ch.chapter_number} failed: {e}")
            finish_fn(novel_id, f"Added {added} new chapter(s)")
        except Exception as e:
            finish_fn(novel_id, f"Unexpected error: {str(e)[:120]}")
            raise
    finally:
        db.close()


def _retranslate_drift_bg(novel_id: int, chapter_numbers: list):
    from database import SessionLocal
    from services.novel_service import _translate_chapter

    db = SessionLocal()
    try:
        set_batch_fn = _get_main_attr("_set_batch", _set_batch)
        if not set_batch_fn(novel_id, "retranslate-drift", len(chapter_numbers),
                            args={"chapter_numbers": chapter_numbers}):
            return
        done = 0
        outcome = None
        stop_req_fn = _get_main_attr("_batch_stop_requested", _batch_stop_requested)
        trans_fn = _get_main_attr("_translate_chapter", _translate_chapter)
        bump_fn = _get_main_attr("_bump_batch", _bump_batch)

        for n in chapter_numbers:
            if stop_req_fn(novel_id):
                logger.info(f"drift retranslate stopped by user (novel {novel_id})")
                outcome = _stopped_label(done, len(chapter_numbers), "drifted chapters fixed")
                break
            ch = db.query(Chapter).filter(
                Chapter.novel_id == novel_id, Chapter.chapter_number == n).first()
            if not ch:
                bump_fn(novel_id, label=f"Ch {n} (missing)")
                continue
            try:
                trans_fn(db, ch, "balanced", force=True)
                db.commit()
                done += 1
                bump_fn(novel_id, label=f"Ch {n} {ch.title_translated or ch.title or ''}")
            except RelayAuthError as e:
                logger.error(f"drift retranslate stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(chapter_numbers), "drifted chapters fixed")
                break
            except Exception as e:
                logger.warning(f"drift retranslate ch{n}: {e}")
                db.rollback()
        finish_fn = _get_main_attr("_finish_batch", _finish_batch)
        finish_fn(novel_id, outcome or f"Fixed {done}/{len(chapter_numbers)} drifted chapters")
    finally:
        db.close()


def _retry_failed_bg(novel_id: int):
    from database import SessionLocal
    from services.novel_service import _fetch_chapter_content_sync, _translate_chapter_bg

    db = SessionLocal()
    try:
        failed = db.query(Chapter).filter(
            Chapter.novel_id == novel_id,
            Chapter.last_error.isnot(None),
            Chapter.last_error != "",
            Chapter.is_translated == False,
        ).order_by(Chapter.chapter_number).all()
        if not failed:
            return
        set_batch_fn = _get_main_attr("_set_batch", _set_batch)
        if not set_batch_fn(novel_id, "retry-failed", len(failed)):
            return
        done = 0
        outcome = None
        stop_req_fn = _get_main_attr("_batch_stop_requested", _batch_stop_requested)
        fetch_sync_fn = _get_main_attr("_fetch_chapter_content_sync", _fetch_chapter_content_sync)
        trans_bg_fn = _get_main_attr("_translate_chapter_bg", _translate_chapter_bg)
        bump_fn = _get_main_attr("_bump_batch", _bump_batch)

        for ch in failed:
            if stop_req_fn(novel_id):
                logger.info(f"retry-failed stopped by user (novel {novel_id})")
                outcome = _stopped_label(done, len(failed), "chapters retried")
                break
            try:
                if not ch.original_content:
                    ch_data = fetch_sync_fn(ch.source_url)
                    if ch_data and ch_data.content:
                        ch.original_content = ch_data.content
                        ch.word_count = getattr(ch_data, "word_count", None)
                        db.commit()
                    db.refresh(ch)
                if ch.original_content:
                    trans_bg_fn(novel_id, ch.chapter_number, "balanced")
                    db.refresh(ch)
                    if ch.is_translated:
                        ch.last_error = ""
                        done += 1
                        db.commit()
                bump_fn(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
            except RelayAuthError as e:
                logger.error(f"retry-failed stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(failed), "chapters retried")
                break
            except Exception as e:
                logger.warning(f"retry-failed ch{ch.chapter_number}: {e}")
                db.rollback()
        finish_fn = _get_main_attr("_finish_batch", _finish_batch)
        finish_fn(novel_id, outcome or f"Retried {done}/{len(failed)} failed chapters")
    finally:
        db.close()


def translate_titles_bg(novel_id: int):
    from database import SessionLocal
    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel:
            return
        chapters = db.query(Chapter).filter(
            Chapter.novel_id == novel_id,
            Chapter.title.isnot(None),
            (Chapter.title_translated.is_(None)) | (Chapter.title_translated == ""),
        ).order_by(Chapter.chapter_number).all()
        if not chapters:
            return
        translator = _get_translator_instance()
        if translator is None:
            logger.error(f"translate-titles aborted (novel {novel_id}): no API key configured (set FALLBACK_API_KEY in Settings)")
            return
        set_batch_fn = _get_main_attr("_set_batch", _set_batch)
        if not set_batch_fn(novel_id, "titles", len(chapters)):
            return
        done = 0
        outcome = None
        stop_req_fn = _get_main_attr("_batch_stop_requested", _batch_stop_requested)
        bump_fn = _get_main_attr("_bump_batch", _bump_batch)

        for ch in chapters:
            if stop_req_fn(novel_id):
                logger.info(f"translate-titles stopped by user (novel {novel_id})")
                outcome = _stopped_label(done, len(chapters), "titles translated")
                break
            try:
                t = translator.translate_short(
                    ch.title, novel.original_language, novel.target_language)
                if t and t.strip() and t.strip() != ch.title.strip():
                    ch.title_translated = t.strip()
                    db.commit()
                done += 1
                bump_fn(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
                time.sleep(1.5)
            except RelayAuthError as e:
                logger.error(f"translate-titles stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(chapters), "titles translated")
                break
            except Exception as e:
                logger.warning(f"title translate ch{ch.chapter_number} failed: {e}")
                db.rollback()
        finish_fn = _get_main_attr("_finish_batch", _finish_batch)
        finish_fn(novel_id, outcome or f"Translated {done}/{len(chapters)} titles")
    finally:
        db.close()


def translate_memory_bg(novel_id: int):
    from database import SessionLocal
    from models import NovelMemory
    from services.novel_service import _load_glossary, _dump_glossary

    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel:
            return
        mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
        if not mem:
            mem = NovelMemory(novel_id=novel_id)
            db.add(mem)
            db.commit()
            db.refresh(mem)

        entries = _load_glossary(mem) or []
        items_to_translate = []
        for idx, entry in enumerate(entries):
            src = (entry.get("source") or "").strip()
            trans = (entry.get("translated") or "").strip()
            if src and (not trans or trans == src):
                items_to_translate.append((idx, src, entry.get("type", "term")))

        total = len(items_to_translate)
        set_batch_fn = _get_main_attr("_set_batch", _set_batch)
        finish_fn = _get_main_attr("_finish_batch", _finish_batch)

        if total == 0:
            if not set_batch_fn(novel_id, "memory", 1, label="Checking memory…"):
                return
            finish_fn(novel_id, "AI memory & glossary up to date")
            return

        translator = _get_translator_instance()
        if translator is None:
            logger.error(f"translate-memory aborted (novel {novel_id}): no API key configured (set FALLBACK_API_KEY in Settings)")
            return

        if not set_batch_fn(novel_id, "memory", total):
            return

        done = 0
        outcome = None
        stop_req_fn = _get_main_attr("_batch_stop_requested", _batch_stop_requested)
        bump_fn = _get_main_attr("_bump_batch", _bump_batch)

        for idx, src, entry_type in items_to_translate:
            if stop_req_fn(novel_id):
                logger.info(f"translate-memory stopped by user (novel {novel_id})")
                outcome = _stopped_label(done, total, "memory items translated")
                break
            try:
                t = translator.translate_short(src, novel.original_language, novel.target_language)
                if t and t.strip() and t.strip() != src:
                    entries[idx]["translated"] = t.strip()
                    mem.glossary_entries = _dump_glossary(entries)
                    db.commit()
                done += 1
                bump_fn(novel_id, label=f"{entry_type.title()}: {src}")
                time.sleep(0.5)
            except RelayAuthError as e:
                logger.error(f"translate-memory stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, total, "memory items translated")
                break
            except Exception as e:
                logger.warning(f"translate memory item {src} failed: {e}")
                db.rollback()

        finish_fn(novel_id, outcome or f"Translated {done}/{total} memory items")
    finally:
        db.close()



def _retranslate_bg(novel_id: int):
    from database import SessionLocal
    from services.novel_service import _translate_chapter

    db = SessionLocal()
    try:
        chapters = db.query(Chapter).filter(
            Chapter.novel_id == novel_id, Chapter.is_translated == True,
            Chapter.original_content.isnot(None),
        ).order_by(Chapter.chapter_number).all()
        if not chapters:
            return
        set_batch_fn = _get_main_attr("_set_batch", _set_batch)
        if not set_batch_fn(novel_id, "retranslate", len(chapters)):
            return
        done = 0
        outcome = None
        stop_req_fn = _get_main_attr("_batch_stop_requested", _batch_stop_requested)
        trans_fn = _get_main_attr("_translate_chapter", _translate_chapter)
        bump_fn = _get_main_attr("_bump_batch", _bump_batch)

        for ch in chapters:
            if stop_req_fn(novel_id):
                logger.info(f"retranslate stopped by user (novel {novel_id})")
                outcome = _stopped_label(done, len(chapters), "chapters retranslated")
                break
            try:
                trans_fn(db, ch, quality="balanced", force=True)
                done += 1
                bump_fn(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
            except RelayAuthError as e:
                logger.error(f"retranslate stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(chapters), "chapters retranslated")
                break
            except Exception as e:
                logger.warning(f"retranslate ch{ch.chapter_number} failed: {e}")
                db.rollback()
        finish_fn = _get_main_attr("_finish_batch", _finish_batch)
        finish_fn(novel_id, outcome or f"Retranslated {done}/{len(chapters)} chapters")
    finally:
        db.close()


def translate_ahead_bg(novel_id: int, after_chapter: int, count: int = 5):
    from database import SessionLocal
    from services.novel_service import _fetch_chapter_content_sync, _translate_chapter_bg

    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel:
            return
        run = [after_chapter + 1]
        taken = []
        for chnum in run:
            if len(taken) >= count:
                break
            ch = db.query(Chapter).filter(
                Chapter.novel_id == novel_id,
                Chapter.chapter_number == chnum,
            ).first()
            if not ch:
                break
            if ch.is_translated:
                break
            taken.append(ch)
            run.append(chnum + 1)
        if not taken:
            return
        next_chs = taken
        set_batch_fn = _get_main_attr("_set_batch", _set_batch)
        if not set_batch_fn(novel_id, "translate-ahead", len(next_chs),
                            args={"after_chapter": after_chapter, "count": count}):
            return
        done = 0
        outcome = None
        stop_req_fn = _get_main_attr("_batch_stop_requested", _batch_stop_requested)
        fetch_sync_fn = _get_main_attr("_fetch_chapter_content_sync", _fetch_chapter_content_sync)
        trans_bg_fn = _get_main_attr("_translate_chapter_bg", _translate_chapter_bg)
        bump_fn = _get_main_attr("_bump_batch", _bump_batch)

        for ch in next_chs:
            if stop_req_fn(novel_id):
                logger.info(f"translate-ahead stopped by user (novel {novel_id}, after ch{ch.chapter_number})")
                outcome = _stopped_label(done, len(next_chs), "chapters prepared")
                break
            try:
                if not ch.original_content:
                    ch_data = fetch_sync_fn(ch.source_url)
                    if ch_data and ch_data.content:
                        ch.original_content = ch_data.content
                        ch.word_count = ch_data.word_count
                        db.commit()
                if not ch.original_content:
                    raise RuntimeError("fetch failed — no content")
                if stop_req_fn(novel_id):
                    logger.info(f"translate-ahead stopped mid-fetch (novel {novel_id})")
                    outcome = _stopped_label(done, len(next_chs), "chapters prepared")
                    break
                trans_bg_fn(novel_id, ch.chapter_number, "balanced")
                db.refresh(ch)
                if ch.is_translated:
                    done += 1
                bump_fn(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
            except RelayAuthError as e:
                logger.error(f"translate-ahead stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(next_chs), "chapters prepared")
                break
            except Exception as e:
                logger.warning(f"translate-ahead ch{ch.chapter_number} failed: {e}")
        finish_fn = _get_main_attr("_finish_batch", _finish_batch)
        finish_fn(novel_id, outcome or f"Prepared {done}/{len(next_chs)} chapters ahead")
    finally:
        db.close()


def _launch_batch(novel_id, kind, args_json="") -> bool:
    args = {}
    if args_json:
        try:
            args = json.loads(args_json)
        except Exception:
            args = {}
    from services.export_service import _export_epub_bg

    targets = {
        "to-end": lambda: _get_main_attr("translate_to_end_bg", translate_to_end_bg)(novel_id),
        "retranslate": lambda: _get_main_attr("_retranslate_bg", _retranslate_bg)(novel_id),
        "titles": lambda: _get_main_attr("translate_titles_bg", translate_titles_bg)(novel_id),
        "updates": lambda: _get_main_attr("check_updates_bg", check_updates_bg)(novel_id),
        "retry-failed": lambda: _get_main_attr("_retry_failed_bg", _retry_failed_bg)(novel_id),
        "epub": lambda: _get_main_attr("_export_epub_bg", _export_epub_bg)(novel_id),
        "meta": lambda: _get_main_attr("translate_novel_meta_bg", translate_novel_meta_bg)(novel_id),
        "memory": lambda: _get_main_attr("translate_memory_bg", translate_memory_bg)(novel_id),
    }
    if kind == "match":
        needle = args.get("needle")
        if needle:
            targets["match"] = lambda: _get_main_attr("retranslate_match_bg", retranslate_match_bg)(novel_id, needle)
    elif kind == "retranslate-drift":
        chapter_numbers = args.get("chapter_numbers")
        if chapter_numbers:
            targets["retranslate-drift"] = lambda: _get_main_attr("_retranslate_drift_bg", _retranslate_drift_bg)(novel_id, chapter_numbers)
    elif kind == "translate-ahead":
        after_chapter = args.get("after_chapter")
        if after_chapter is not None:
            count = args.get("count", 5)
            targets["translate-ahead"] = lambda: _get_main_attr("translate_ahead_bg", translate_ahead_bg)(novel_id, after_chapter, count)
    fn = targets.get(kind)
    if fn is None:
        return False
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return True


def _resume_interrupted_jobs():
    from database import SessionLocal
    db = SessionLocal()
    try:
        stale = db.query(BatchJob).filter(BatchJob.running == True).all()
        pending = [(j.novel_id, j.kind, j.args_json or "") for j in stale]
        for job in stale:
            job.running = False
        db.commit()
    finally:
        db.close()
    launch_fn = _get_main_attr("_launch_batch", _launch_batch)
    for novel_id, kind, args_json in pending:
        if launch_fn(novel_id, kind, args_json):
            logger.info(f"Resuming interrupted {kind} job for novel {novel_id}")
        else:
            logger.info(f"Not resuming {kind} for novel {novel_id} (needs per-call args) — marked finished")


def _watchdog_pass():
    from database import SessionLocal
    db = SessionLocal()
    try:
        jobs = db.query(BatchJob).filter(BatchJob.running == True).all()
        for job in jobs:
            if job.updated_at and (datetime.utcnow() - job.updated_at).total_seconds() > JOB_STALL_MINUTES * 60:
                logger.warning(
                    f"Watchdog: freeing stalled {job.kind} job (novel {job.novel_id}, "
                    f"stuck at {job.done}/{job.total} for >{JOB_STALL_MINUTES} min)")
                job.running = False
                db.commit()
                cache = _get_main_attr("_batch_cache", _batch_cache)
                b = cache.get(job.novel_id)
                if b:
                    b["running"] = False
    finally:
        db.close()


def _watchdog_loop():
    while True:
        try:
            pass_fn = _get_main_attr("_watchdog_pass", _watchdog_pass)
            pass_fn()
        except Exception as e:
            logger.warning(f"watchdog error: {e}")
        time.sleep(300)
