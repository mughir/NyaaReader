"""
FastAPI Backend for NyaaReader.
"""
import asyncio
import logging
import os
from pathlib import Path
import threading
from typing import List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

logger = logging.getLogger("novel-reader")

# Database & models
from database import SessionLocal, engine, get_db_session, init_db
from models import (
    AppConfig,
    BatchJob,
    Bookmark,
    Chapter,
    DiaryEntry,
    Novel,
    NovelMemory,
    NovelSettings,
    ReadingProgress,
    ScrapingLog,
)
from schemas import (
    BatchMarkReadRequest,
    BatchTranslateSelectedRequest,
    ChapterManualCreate,
    ChapterResponse,
    NovelCreate,
    NovelManualCreate,
    NovelResponse,
    ProgressUpdate,
    ReadingProgressResponse,
    SettingsUpdate,
    TranslateRequest,
)
from translator import (
    MemoryContext,
    RelayAuthError,
    TranslationResult,
    get_translator,
    sync_glossary_entries,
)
from scrapers import auto_detect_and_scrape, get_scraper_for_url

# Security & Auth
from security import (
    DATA_DIR,
    _COOKIE_NAME,
    _SESSION_SECRET,
    _SESSION_TTL,
    _auth_enabled,
    _auth_enabled_cached,
    _auth_flag_cache,
    _auth_password,
    _login_attempts,
    _login_guard_check,
    _login_guard_fail,
    _login_guard_success,
    _make_session_token,
    _require_auth,
    _rotate_session_secret,
    _sign_session,
    _verify_session,
)

# Services
from services.config_service import (
    _CONFIG_ENV,
    _apply_config_to_env,
    _chat_completions_sync,
    _check_relay_health_bg,
    _config_health_check_sync,
    _get_config,
    _relay_health_cache,
)
from services.backup_service import (
    _backup_dir,
    _backup_scheduler_loop,
    run_backup,
)
from services.export_service import (
    _epub_path,
    _export_epub_bg,
    _generate_cover_svg,
    _html_escape,
    _safe_filename,
    _split_paragraphs,
    _xml_escape,
)
from services.job_service import (
    JOB_STALL_MINUTES,
    _async_batch_locks,
    _async_novel_lock,
    _auth_rejected_label,
    _batch_cache,
    _batch_locks,
    _batch_locks_guard,
    _batch_running,
    _batch_stop_requested,
    _bump_batch,
    _clear_batch,
    _finish_batch,
    _get_or_create_job,
    _launch_batch,
    _novel_lock,
    _request_batch_stop,
    _resume_interrupted_jobs,
    _retry_failed_bg,
    _retranslate_bg,
    _retranslate_drift_bg,
    _set_batch,
    _set_batch_total,
    _stopped_label,
    _update_job,
    _watchdog_loop,
    _watchdog_pass,
    check_updates_bg,
    retranslate_match_bg,
    translate_ahead_bg,
    translate_novel_meta_bg,
    translate_titles_bg,
    translate_to_end_bg,
    _translate_selected_bg,
)
from services.novel_service import (
    FETCH_DELAY_SECONDS,
    _create_novel_from_url,
    _dump_glossary,
    _fetch_chapter_content_sync,
    _get_novel_info_sync,
    _load_glossary,
    _locked_terms,
    _sleep_between_fetches,
    _translate_chapter,
    _translate_chapter_bg,
    fetch_chapters_range,
    fetch_initial_chapters,
)
from views import (
    _asset_stamp,
    _get_recap,
    _icons_sprite_cache,
    _json,
    _page,
    frontend_path,
)

# Routers
from routers import auth as auth_router
from routers import batch as batch_router
from routers import chapters as chapters_router
from routers import config as config_router
from routers import novels as novels_router
from routers import pages as pages_router
from routers import translation as translation_router

# Router endpoint aliases for backward-compatible module-level access
login = auth_router.login
logout = auth_router.logout
auth_status = auth_router.auth_status
health_check = auth_router.health_check

add_novel = novels_router.add_novel
add_novel_manual = novels_router.add_novel_manual
list_novels = novels_router.list_novels
get_novel = novels_router.get_novel
delete_novel = novels_router.delete_novel
fetch_more_chapters = novels_router.fetch_more_chapters
get_progress = novels_router.get_progress
update_progress = novels_router.update_progress
set_reading_status = novels_router.set_reading_status
search_novel = novels_router.search_novel
list_bookmarks = novels_router.list_bookmarks
delete_bookmark = novels_router.delete_bookmark
list_diary = novels_router.list_diary
export_epub = novels_router.export_epub
epub_download = novels_router.epub_download
generate_cover = novels_router.generate_cover
upload_cover = novels_router.upload_cover
novel_cover = novels_router.novel_cover
get_stats = novels_router.get_stats

add_chapter_manual = chapters_router.add_chapter_manual
list_chapters = chapters_router.list_chapters
get_chapter = chapters_router.get_chapter
add_bookmark = chapters_router.add_bookmark
get_diary = chapters_router.get_diary
put_diary = chapters_router.put_diary

translate_chapter = translation_router.translate_chapter
translate_chapter_stream = translation_router.translate_chapter_stream
fetch_chapter_json = translation_router.fetch_chapter_json
get_memory = translation_router.get_memory
update_memory = translation_router.update_memory
drift_count = translation_router.drift_count
retranslate_drift = translation_router.retranslate_drift
failed_count = translation_router.failed_count
retry_failed = translation_router.retry_failed
translate_titles = translation_router.translate_titles
translate_novel_meta = translation_router.translate_novel_meta
retranslate_novel = translation_router.retranslate_novel
retranslate_match = translation_router.retranslate_match
translate_to_end = translation_router.translate_to_end
translate_ahead = translation_router.translate_ahead
batch_translate_selected = translation_router.batch_translate_selected
batch_mark_read = translation_router.batch_mark_read
check_updates = translation_router.check_updates

batch_stop = batch_router.batch_stop
batch_status = batch_router.batch_status

get_config = config_router.get_config
put_config = config_router.put_config
config_health_check = config_router.config_health_check
config_health_status = config_router.config_health_status
backup_now = config_router.backup_now
list_backups = config_router.list_backups
download_backup = config_router.download_backup
restore_backup = config_router.restore_backup
delete_backup = config_router.delete_backup

library_page = pages_router.library_page
novel_page = pages_router.novel_page
chapter_page = pages_router.chapter_page
novel_review_page = pages_router.novel_review_page
login_page = pages_router.login_page
config_page = pages_router.config_page
dashboard_page = pages_router.dashboard_page

# Initialize database
init_db()

app = FastAPI(
    title="NyaaReader API",
    description="Web novel reader with AI translation",
    version="1.0.0",
)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """When auth is enabled, require a valid session cookie for everything
    except the login page, static assets, and the health endpoint."""
    path = request.url.path
    if (path in ("/login", "/api/health") or path.startswith("/static/")
            or path == "/api/auth/login" or path == "/api/auth/logout"
            or path == "/api/auth/status"):
        return await call_next(request)
    if not _auth_enabled_cached():
        return await call_next(request)
    cookie = request.cookies.get(_COOKIE_NAME)
    if cookie and _verify_session(cookie):
        return await call_next(request)
    if path.startswith("/api/"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Login required"}, status_code=401)
    return RedirectResponse(url="/login", status_code=303)


if frontend_path:
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")

# Mount Routers
app.include_router(auth_router.router)
app.include_router(novels_router.router)
app.include_router(chapters_router.router)
app.include_router(translation_router.router)
app.include_router(batch_router.router)
app.include_router(config_router.router)
app.include_router(pages_router.router)


def _startup_reliability_sync():
    _apply_config_to_env()
    _resume_interrupted_jobs()
    _check_relay_health_bg()


@app.on_event("startup")
async def _startup_reliability():
    """Startup: resume interrupted jobs + start backup scheduler + watchdog threads."""
    try:
        await asyncio.to_thread(_startup_reliability_sync)
    except Exception as e:
        logger.warning(f"startup reliability failed: {e}")
    t = threading.Thread(target=_backup_scheduler_loop, daemon=True)
    t.start()
    w = threading.Thread(target=_watchdog_loop, daemon=True)
    w.start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)