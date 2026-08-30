"""
FastAPI Backend for NyaaReader
"""
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Query, UploadFile, File, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, HttpUrl, computed_field
from datetime import datetime
import os
import re
import asyncio
import logging
import json
import html as html_mod

logger = logging.getLogger("novel-reader")

from database import init_db, get_db_session
from models import Novel, Chapter, ReadingProgress, NovelSettings, ScrapingLog, NovelMemory, Bookmark, DiaryEntry
from translator import get_translator, TranslationResult, MemoryContext, RelayAuthError
from scrapers import get_scraper_for_url, auto_detect_and_scrape

from pathlib import Path

# Persistent data dir (DB, backups, epub exports, generated covers)
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data")) if not os.name == "nt" else Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------
# Auth: single password + signed session cookie.
# Password empty  -> auth disabled (local/docker use, backward compatible).
# Password set    -> all HTML pages + APIs require a valid session cookie,
#                    except /login, /static, /api/health.
# ---------------------------------------------------------------
import hmac
import hashlib
import secrets as _secrets
import time as _time

_SESSION_TTL = 30 * 24 * 3600  # 30 days
_COOKIE_NAME = "nyaa_session"

# Brute-force guard: per-IP failed-login tracking with exponential backoff.
_login_attempts = {}  # ip -> {"fails": int, "locked_until": float}


def _login_guard_check(client_ip: str):
    rec = _login_attempts.get(client_ip)
    if rec and rec["locked_until"] > _time.time():
        wait = int(rec["locked_until"] - _time.time())
        raise HTTPException(status_code=429, detail=f"Too many attempts. Try again in {wait}s")


def _login_guard_fail(client_ip: str):
    rec = _login_attempts.get(client_ip, {"fails": 0, "locked_until": 0})
    rec["fails"] += 1
    # exponential: 5s, 20s, 60s, 4m, 15m, 1h cap
    wait = min(3600, 5 * (4 ** (rec["fails"] - 1)))
    rec["locked_until"] = _time.time() + wait
    _login_attempts[client_ip] = rec
    # keep the dict small: drop entries idle > 1h
    if len(_login_attempts) > 500:
        cutoff = _time.time() - 3600
        for k in [k for k, v in _login_attempts.items() if v["locked_until"] < cutoff]:
            _login_attempts.pop(k, None)


def _login_guard_success(client_ip: str):
    _login_attempts.pop(client_ip, None)


def _auth_enabled(db=None) -> bool:
    cfg = _get_config()
    return bool((cfg.get("auth_password") or "").strip())


def _auth_password(db=None) -> str:
    return (_get_config().get("auth_password") or "")


def _sign_session(token: str) -> str:
    return hmac.new(_SESSION_SECRET().encode(), token.encode(), hashlib.sha256).hexdigest()


def _SESSION_SECRET() -> str:
    """Persistent HMAC secret for cookie signing (stored in data dir)."""
    f = DATA_DIR / "session_secret"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    s = _secrets.token_hex(32)
    f.write_text(s, encoding="utf-8")
    return s


def _rotate_session_secret():
    """Rotate the cookie-signing secret → invalidates EVERY outstanding session
    token. Called on password change (and password removal) so a leaked cookie
    stops working immediately instead of lingering up to _SESSION_TTL."""
    f = DATA_DIR / "session_secret"
    try:
        f.write_text(_secrets.token_hex(32), encoding="utf-8")
    except OSError as e:
        logger.warning(f"could not rotate session secret: {e}")


def _make_session_token() -> str:
    token = _secrets.token_hex(32)
    return f"{token}.{int(_time.time())}.{_sign_session(token)}"


def _verify_session(cookie: str) -> bool:
    if not cookie:
        return False
    parts = cookie.split(".")
    if len(parts) != 3:
        return False
    token, ts, sig = parts
    try:
        ts_i = int(ts)
    except ValueError:
        return False
    if _time.time() - ts_i > _SESSION_TTL:
        return False
    expect = hmac.new(_SESSION_SECRET().encode(), token.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, sig)


async def _require_auth(request: Request):
    """FastAPI dependency: 401 redirect to /login when auth is on and no valid session."""
    from database import SessionLocal as _SL
    _db = _SL()
    try:
        if not _auth_enabled(_db):
            return None  # auth off — allow
    finally:
        _db.close()
    cookie = request.cookies.get(_COOKIE_NAME)
    if cookie and _verify_session(cookie):
        return None
    raise HTTPException(status_code=401, detail="Login required")


# Initialize database
init_db()

app = FastAPI(
    title="NyaaReader API",
    description="Web novel reader with AI translation",
    version="1.0.0",
)

# No CORS middleware: this is a same-origin app with cookie auth.
# An open CORS policy (* + credentials) would be a security risk on a
# public VPS — cross-origin sites must NOT be able to read our APIs.
# (If you ever need a separate frontend origin, add a locked-down
#  allow_origins list — never "*" with credentials.)

# Serve frontend static files.
# The frontend dir lives at <backend dir>/../frontend in local dev, or
# <backend dir>/frontend inside Docker (backend files are copied straight to /app).
_backend_dir = os.path.dirname(os.path.abspath(__file__))
frontend_path = next(
    (p for p in (
        os.path.join(_backend_dir, "..", "frontend"),
        os.path.join(_backend_dir, "frontend"),
    ) if os.path.isdir(p)),
    None,
)


_auth_flag_cache = {"value": None, "ts": 0.0}


def _auth_enabled_cached() -> bool:
    """_auth_enabled() hits the DB; the auth middleware runs on EVERY request.
    Cache the flag for a few seconds so config reads stay cheap."""
    import time as _t
    now = _t.time()
    if _auth_flag_cache["ts"] < now - 5:
        try:
            _auth_flag_cache["value"] = _auth_enabled()
            _auth_flag_cache["ts"] = now
        except Exception as e:
            # FAIL CLOSED, not open. This runs on EVERY request; a transient
            # DB error must never look like "no password configured" and let
            # every request through unauthenticated for the next 5s. Keep
            # whatever was last known-good (or assume auth IS required if we
            # have never successfully read it), and leave `ts` stale so the
            # very next request retries immediately instead of caching the
            # failure.
            logger.warning(f"auth-enabled check failed, keeping previous state: {e}")
            if _auth_flag_cache["value"] is None:
                _auth_flag_cache["value"] = True
    return _auth_flag_cache["value"]


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """When auth is enabled, require a valid session cookie for everything
    except the login page, static assets, and the health endpoint."""
    path = request.url.path
    # always-open paths
    if (path in ("/login", "/api/health") or path.startswith("/static/")
            or path == "/api/auth/login" or path == "/api/auth/logout"
            or path == "/api/auth/status"):
        return await call_next(request)
    if not _auth_enabled_cached():
        return await call_next(request)
    cookie = request.cookies.get(_COOKIE_NAME)
    if cookie and _verify_session(cookie):
        return await call_next(request)
    # API request -> 401 JSON; page request -> redirect to login
    if path.startswith("/api/"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Login required"}, status_code=401)
    return RedirectResponse(url="/login", status_code=303)

@app.post("/api/auth/login")
async def login(body: dict, response: Response, request: Request,
                db: Session = Depends(get_db_session)):
    client_ip = request.client.host if request.client else "unknown"
    _login_guard_check(client_ip)
    pw = (body.get("password") or "").strip()
    if not _auth_enabled(db):
        return {"status": "disabled"}
    if hmac.compare_digest(pw, _auth_password(db)):
        _login_guard_success(client_ip)
        token = _make_session_token()
        # secure=True when served over HTTPS (e.g. behind a TLS proxy) or when the
        # operator sets COOKIE_SECURE=1. Not forced on by default so plain-HTTP local
        # dev still works — otherwise the session cookie is never sent.
        import os as _os
        secure = _os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes") \
                 or (request.url.scheme == "https")
        response.set_cookie(_COOKIE_NAME, token, max_age=_SESSION_TTL,
                            httponly=True, samesite="lax", secure=secure)
        return {"status": "ok"}
    _login_guard_fail(client_ip)
    raise HTTPException(status_code=401, detail="Wrong password")


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(_COOKIE_NAME)
    return {"status": "ok"}


@app.get("/api/auth/status")
async def auth_status(db: Session = Depends(get_db_session)):
    """Whether password auth is enabled. Lets the login page skip straight through
    (and the config page hide the logout button) when no password is set."""
    return {"enabled": _auth_enabled(db)}

if frontend_path:
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")


# Pydantic models
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
        """Same key the server-rendered novel page already embeds per chapter
        (see novel_page's `ch_data` below) — added here so both payload shapes
        carry `has_content` instead of only this one carrying the full
        `original_content` string. frontend/lib/text.js's hasContent() still
        falls back to checking `original_content` for older cached payloads,
        but every fresh response now agrees on the same field."""
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


# API Routes

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "service": "novel-reader"}


@app.post("/api/novels", response_model=NovelResponse)
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


@app.post("/api/novels/manual", response_model=NovelResponse)
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


@app.post("/api/novels/{novel_id}/chapters/manual")
async def add_chapter_manual(novel_id: int, chapter_data: ChapterManualCreate,
                             db: Session = Depends(get_db_session)):
    """Add a single chapter manually (title + original content pasted by the user)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    # Auto-assign the next chapter number
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
    # SessionLocal uses autoflush=False, so the pending db.add()s above are
    # NOT visible to this count yet — flush first or total_chapters is set to
    # the PRE-insert number and drifts permanently behind the real count.
    db.flush()
    novel.total_chapters = db.query(Chapter).filter(Chapter.novel_id == novel_id).count()
    db.commit()
    db.refresh(chapter)
    return {"status": "ok", "chapter_id": chapter.id, "chapter_number": next_num}


async def _create_novel_from_url(db, source_url: str, target_language: str,
                                 auto_translate: bool, background_tasks=None):
    """Shared scrape+create logic used by the JSON API and the HTML form."""
    # Check if already exists
    existing = db.query(Novel).filter(Novel.source_url == source_url).first()
    if existing:
        raise ValueError("Novel already exists")

    # Scrape novel info
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

    # Determine source site
    from urllib.parse import urlparse
    source_site = urlparse(source_url).netloc.lower().replace("www.", "")

    # Create novel
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

    # Create default settings
    settings = NovelSettings(
        novel_id=novel.id,
        auto_translate=auto_translate,
    )
    db.add(settings)

    # Create chapters
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

    # Reconcile: the scraper's total_chapters estimate can disagree with the
    # actual chapter rows we just created. Derive the real count so the header
    # never drifts from ground truth (audit #18).
    actual = db.query(Chapter).filter(Chapter.novel_id == novel.id).count()
    if novel.total_chapters != actual:
        novel.total_chapters = actual
        db.commit()

    db.refresh(novel)

    # Background task to fetch first few chapters
    if background_tasks is not None:
        background_tasks.add_task(fetch_initial_chapters, novel.id, auto_translate)
        background_tasks.add_task(translate_novel_meta_bg, novel.id)

    return novel


def translate_novel_meta_bg(novel_id: int):
    """Translate novel title + description (background, best-effort)."""
    from database import SessionLocal
    from translator import get_translator
    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel:
            return
        if novel.title_translated and novel.description_translated:
            return
        translator = get_translator()
        if translator is None:
            logger.error(f"translate-meta aborted (novel {novel_id}): no API key configured (set FALLBACK_API_KEY in Settings)")
            return
        if not _set_batch(novel_id, "meta", 2):
            return
        # NOTE: this early stop-check used to `return` directly, skipping the
        # _finish_batch below entirely — the job stayed running=True until the
        # watchdog freed it ~10 minutes later. Every exit past _set_batch must
        # go through _finish_batch, so it's a fall-through, not a return.
        outcome = None
        if _batch_stop_requested(novel_id):
            outcome = "Stopped by user"
        else:
            try:
                if not novel.title_translated and novel.title:
                    t = translator.translate_short(
                        novel.title, novel.original_language, novel.target_language)
                    if t and t.strip():
                        novel.title_translated = t.strip()
                        _bump_batch(novel_id, label="Novel title")
                        db.commit()
                if not novel.description_translated and novel.description:
                    d = translator.translate_short(
                        novel.description, novel.original_language, novel.target_language)
                    if d and d.strip():
                        novel.description_translated = d.strip()
                        _bump_batch(novel_id, label="Synopsis")
                        db.commit()
            except RelayAuthError as e:
                logger.error(f"translate-meta stopped: relay key rejected ({e})")
                outcome = "Stopped — relay key rejected, check Settings"
            except Exception as e:
                logger.warning(f"translate novel meta {novel_id} failed: {e}")
                db.rollback()
                outcome = f"Translation failed: {str(e)[:120]}"
        _finish_batch(novel_id, outcome or "Title & synopsis translated")
    finally:
        db.close()


async def fetch_initial_chapters(novel_id: int, auto_translate: bool):
    """Background task to fetch the first 5 chapters, right after a novel is added.

    Delegates to fetch_chapters_range instead of hand-rolling its own loop — that
    used to open/close a fresh scraper session per chapter (instead of one shared
    session for the whole batch) and had no try/except around the fetch itself, so
    a single transient failure on e.g. chapter 2 raised straight past the loop and
    silently left chapters 3-5 unfetched, with no error, no log, no BatchJob outcome
    — nothing to tell the user their new novel came in half-populated."""
    await fetch_chapters_range(novel_id, start=1, count=5, do_translate=auto_translate)


@app.get("/api/novels", response_model=List[NovelResponse])
async def list_novels(db: Session = Depends(get_db_session)):
    """List all novels"""
    novels = db.query(Novel).order_by(Novel.updated_at.desc()).all()
    return novels


@app.get("/api/novels/{novel_id}", response_model=NovelResponse)
async def get_novel(novel_id: int, db: Session = Depends(get_db_session)):
    """Get novel details"""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    return novel


@app.get("/api/novels/{novel_id}/chapters", response_model=List[ChapterResponse])
async def list_chapters(novel_id: int, db: Session = Depends(get_db_session)):
    """List all chapters for a novel"""
    chapters = db.query(Chapter).filter(
        Chapter.novel_id == novel_id
    ).order_by(Chapter.chapter_number).all()
    return chapters


@app.get("/api/novels/{novel_id}/chapters/{chapter_number}", response_model=ChapterResponse)
async def get_chapter(novel_id: int, chapter_number: int, db: Session = Depends(get_db_session)):
    """Get a specific chapter"""
    chapter = db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.chapter_number == chapter_number
    ).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return chapter


@app.post("/api/chapters/{chapter_id}/translate", response_model=ChapterResponse)
async def translate_chapter(
    chapter_id: int,
    request: TranslateRequest,
    db: Session = Depends(get_db_session)
):
    """Translate a chapter using AI (memory-aware: reads & updates per-novel memory).

    Runs the blocking relay call in a worker thread — a slow AI (relay stalls
    can take minutes) must never occupy the event loop, or every other page
    and request freezes behind it."""
    chapter = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    # The relay call is blocking (up to 180s x retries) — run it off the event
    # loop so one translation doesn't freeze every other request.
    return await asyncio.to_thread(
        _translate_chapter, db, chapter, request.quality, request.force_retranslate)


def _load_glossary(mem_row) -> Optional[list]:
    """Return structured glossary entries. Parses free-text memory on first use
    (backward-compat), otherwise returns the stored JSON list."""
    if getattr(mem_row, "glossary_entries", None):
        return json.loads(mem_row.glossary_entries) if isinstance(mem_row.glossary_entries, str) else mem_row.glossary_entries
    entries = []
    # Characters: "Name (Original) - note" entries packed together, separated by
    # "; Name (Original) -" boundaries. Descriptions may contain ';', so split at
    # each 'Name (Original)' anchor rather than blindly on ';'.
    # An anchor looks like "Translated (Original) - " — translated side may contain
    # alias lists ("A / B / C (原名)") and must not contain parens or semicolons.
    char_text = mem_row.characters or ""
    anchors = list(re.finditer(r"([^;()]{1,80}?)\s*\(([^()]+)\)\s*[-–:]\s*", char_text))
    for i, m in enumerate(anchors):
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(char_text)
        note = char_text[m.end():end].strip().rstrip(";").strip()
        translated = m.group(1).strip()
        # Alias list: "Yancai / Jiang Xianzi / Noviya (焰彩/姜仙子/諾維雅)" — the
        # parenthesized part is one source name per alias; keep the FIRST pair as
        # primary and fold the rest into the note.
        aliases = re.split(r"\s*/\s*", translated)
        primary = aliases[0].strip()
        src = m.group(2).strip()
        src_aliases = re.split(r"\s*/\s*", src) if "/" in src else []
        if len(aliases) > 1:
            alias_txt = "; ".join(
                f"{a.strip()} ({s.strip()})" for a, s in zip(aliases[1:], src_aliases[1:])
                if a.strip() and s.strip()
            )
            note = (alias_txt + "; " + note).strip() if alias_txt and note else (alias_txt or note)
        entries.append({"type": "character", "translated": primary,
                        "source": src_aliases[0].strip() if src_aliases else src,
                        "note": note, "locked": False})
    # Terms: "Original = Translation" or "Original -> Translation"
    for line in (mem_row.terms or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(.*?)\s*(?:=|->|→)\s*(.*)$", line)
        if m and m.group(2).strip():
            entries.append({"type": "term", "source": m.group(1).strip(),
                            "translated": m.group(2).strip(), "note": "",
                            "locked": False})
    return entries or None


def _dump_glossary(entries):
    """Return the glossary list as-is. The column is SQLAlchemy JSON, which
    serializes natively — json.dumps() here would DOUBLE-encode (string inside
    JSON). Read-side code already handles legacy double-encoded values."""
    if not entries:
        return []
    return entries


def _translate_chapter(db, chapter, quality: str = "balanced", force: bool = False):
    """Shared memory-aware translate used by the JSON API and HTML pages."""
    if not chapter.original_content:
        raise HTTPException(status_code=400, detail="No original content to translate")

    if chapter.is_translated and not force:
        return chapter

    # Get novel for language info
    novel = db.query(Novel).filter(Novel.id == chapter.novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    # Load per-novel memory (create on first use)
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

    # Translate using memory (reads context, then updates memory)
    translator = get_translator()
    if translator is None:
        raise HTTPException(status_code=500,
                            detail="Translation unavailable: no API key configured (set FALLBACK_API_KEY in Settings)")
    result = translator.translate_with_memory(
        chapter.original_content,
        novel.original_language,
        novel.target_language,
        quality,
        memory=memory,
    )

    if result is None or not getattr(result, "success", False):
        detail = getattr(result, "error", "translation returned no result")
        raise HTTPException(status_code=500, detail=f"Translation failed: {detail}")

    chapter.translated_content = result.translated_text
    chapter.is_translated = True
    chapter.translation_model = result.model_used
    chapter.updated_at = datetime.utcnow()

    # Translate the chapter title too (best-effort, same target language)
    if chapter.title and not chapter.title_translated:
        try:
            translated_title = translator.translate_short(
                chapter.title, novel.original_language, novel.target_language)
            if translated_title and translated_title.strip():
                chapter.title_translated = translated_title.strip()
        except Exception as e:
            logger.warning(f"title translate failed for ch {chapter.chapter_number}: {e}")

    # Persist updated memory
    if result.memory:
        mem_row.characters = result.memory.characters
        mem_row.terms = result.memory.terms
        mem_row.plot = result.memory.plot
        mem_row.arc_plot = result.memory.arc_plot
        mem_row.chapter_plot = result.memory.chapter_plot
        mem_row.memory = result.memory.memory
        mem_row.glossary_entries = _dump_glossary(result.memory.glossary_entries)
        mem_row.updated_at = datetime.utcnow()
        # Bounded memory: compact when the file grows past budget (500+ chapter novels)
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


@app.get("/api/novels/{novel_id}/memory")
async def get_memory(novel_id: int, db: Session = Depends(get_db_session)):
    """Get the per-novel AI translator memory (characters, terms, plots, instruction)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
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
        "glossary_entries": _load_glossary(mem) or [],
    }


class MemoryUpdate(BaseModel):
    general_instruction: Optional[str] = None
    characters: Optional[str] = None
    terms: Optional[str] = None
    plot: Optional[str] = None
    arc_plot: Optional[str] = None
    chapter_plot: Optional[str] = None
    memory: Optional[str] = None
    # Structured glossary entries: [{"type","source","translated","note","locked"}]
    glossary_entries: Optional[list] = None


@app.put("/api/novels/{novel_id}/memory")
async def update_memory(
    novel_id: int,
    payload: MemoryUpdate,
    db: Session = Depends(get_db_session),
):
    """Create/update the per-novel AI translator memory (user-editable)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
    if not mem:
        mem = NovelMemory(novel_id=novel_id)
        db.add(mem)
    data = payload.model_dump(exclude_unset=True)

    # glossary_entries is stored as JSON (not a free-text column)
    if "glossary_entries" in data:
        entries = data.pop("glossary_entries") or []
        mem.glossary_entries = _dump_glossary(entries)
        # Rebuild the free-text characters/terms from the structured entries so
        # the AI's prompt context stays in sync with the user's edits.
        chars = [e for e in entries if e.get("type") == "character" and (e.get("translated") or e.get("source"))]
        terms = [e for e in entries if e.get("type") == "term" and (e.get("translated") or e.get("source"))]
        if chars:
            mem.characters = "\n".join(
                f"{e.get('translated','') or e.get('source','')} ({e.get('source','')}) - {e.get('note','')}".strip()
                for e in chars
            )
        if terms:
            mem.terms = "\n".join(
                f"{e.get('source','')} = {e.get('translated','')}" for e in terms
            )

    for key, value in data.items():
        setattr(mem, key, value or "")
    mem.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


def _batch_running(novel_id: int, kind: str = None) -> bool:
    """True if a live (recently-updated, running) batch job exists for this
    novel. With `kind` given, matches only that kind (for callers that want a
    precise 'already running this job' answer); without it, ANY running job
    blocks — batches are serialized per novel so their progress counters
    (shared bump/clear helpers) can't corrupt each other."""
    from database import SessionLocal
    from models import BatchJob
    from datetime import datetime, timedelta
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


@app.post("/api/novels/{novel_id}/translate-to-end")
async def translate_to_end(novel_id: int, background_tasks: BackgroundTasks = None,
                           db: Session = Depends(get_db_session)):
    """Background: fetch+translate every remaining untranslated chapter (sequential)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    async with _async_novel_lock(novel_id):
        if _batch_running(novel_id):
            return {"status": "already_running", "pending": 0}
        pending = db.query(Chapter).filter(
            Chapter.novel_id == novel_id,
            Chapter.is_translated == False,
        ).count()
        if pending == 0:
            return {"status": "none", "pending": 0}
        background_tasks.add_task(translate_to_end_bg, novel_id)
    return {"status": "started", "pending": pending}


def translate_to_end_bg(novel_id: int):
    """Fetch+translate every untranslated chapter, sequentially, with politeness delays."""
    from database import SessionLocal
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
        if not _set_batch(novel_id, "to-end", len(chapters)):
            return
        done = 0
        outcome = None
        for ch in chapters:
            if _batch_stop_requested(novel_id):
                logger.info(f"translate-to-end stopped by user (novel {novel_id})")
                outcome = _stopped_label(done, len(chapters), "chapters translated")
                break
            try:
                if not ch.original_content:
                    ch_data = _fetch_chapter_content_sync(ch.source_url)
                    if ch_data and ch_data.content:
                        ch.original_content = ch_data.content
                        ch.word_count = getattr(ch_data, "word_count", None)
                        db.commit()
                # re-query (fetch helper commits content)
                db.refresh(ch)
                if not ch.original_content:
                    raise RuntimeError("fetch failed — no content")
                _translate_chapter_bg(novel_id, ch.chapter_number, "balanced")
                # _translate_chapter_bg records ordinary failures on
                # ch.last_error and returns NORMALLY rather than raising, so
                # "the call didn't raise" is not "it succeeded" — check the
                # real outcome, or a batch where every chapter quietly failed
                # would still report "Translated N/N chapters".
                db.refresh(ch)
                if ch.is_translated:
                    done += 1
                _bump_batch(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
            except RelayAuthError as e:
                # Relay key disabled/invalid — stop the batch NOW instead of
                # failing every remaining chapter.
                logger.error(f"translate-to-end stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(chapters), "chapters translated")
                break
            except Exception as e:
                logger.warning(f"translate-to-end ch{ch.chapter_number} failed: {e}")
                db.rollback()
        _finish_batch(novel_id, outcome or f"Translated {done}/{len(chapters)} chapters")
    finally:
        db.close()


@app.post("/api/novels/{novel_id}/retranslate-match")
async def retranslate_match(novel_id: int, payload: dict = None,
                            background_tasks: BackgroundTasks = None,
                            db: Session = Depends(get_db_session)):
    """Background: retranslate ONLY chapters whose translated_content contains `needle`.
    Lighter than full retranslate — use after changing a locked name (e.g. Angelia)."""
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
    async with _async_novel_lock(novel_id):
        if _batch_running(novel_id):
            return {"status": "already_running", "pending": 0}
        background_tasks.add_task(retranslate_match_bg, novel_id, needle)
    return {"status": "started", "pending": matched}


def retranslate_match_bg(novel_id: int, needle: str):
    """Retranslate chapters containing `needle` (sequential, force)."""
    from database import SessionLocal
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
        if not _set_batch(novel_id, "match", len(chapters), args={"needle": needle}):
            return
        done = 0
        outcome = None
        for ch in chapters:
            # NOTE: this loop had no stop-check at all — clicking "Stop" during
            # a match-retranslate had no effect until the whole list finished
            # or a rejected key broke it early. Every other batch loop honors
            # stop requests; this one must too.
            if _batch_stop_requested(novel_id):
                logger.info(f"match-retranslate stopped by user (novel {novel_id})")
                outcome = _stopped_label(done, len(chapters), "chapters retranslated")
                break
            try:
                _translate_chapter(db, ch, quality="balanced", force=True)
                done += 1
                _bump_batch(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
            except RelayAuthError as e:
                logger.error(f"match-retranslate stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(chapters), "chapters retranslated")
                break
            except Exception as e:
                logger.warning(f"match-retranslate ch{ch.chapter_number} failed: {e}")
                db.rollback()
        _finish_batch(novel_id, outcome or f"Retranslated {done}/{len(chapters)} matching chapters")
    finally:
        db.close()


@app.post("/api/novels/{novel_id}/check-updates")
async def check_updates(novel_id: int, background_tasks: BackgroundTasks = None,
                        db: Session = Depends(get_db_session)):
    """Check the source site for new chapters beyond the last known one; fetch them
    (and optionally translate) in the background."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel or novel.source_site == "manual":
        raise HTTPException(status_code=404, detail="Novel not found (or manual novel)")
    async with _async_novel_lock(novel_id):
        if _batch_running(novel_id):
            return {"status": "already_running"}
        background_tasks.add_task(check_updates_bg, novel_id)
    return {"status": "started"}


def check_updates_bg(novel_id: int):
    """Refresh the chapter list from the source; fetch+translate any new chapters.

    Claims the batch slot for the WHOLE operation (scrape included) so the UI can
    show what happened. Every exit path sets a label and clears the job —
    previously each failure was a bare `return`, so a broken scraper looked
    exactly like "no new chapters" and the user was told it had checked fine."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel or novel.source_site == "manual":
            return
        from scrapers import get_scraper_for_url
        scraper = get_scraper_for_url(novel.source_url)
        if not scraper:
            logger.warning(f"check-updates: no scraper for {novel.source_url}")
            return
        if not _set_batch(novel_id, "updates", 1, label="Checking the source for new chapters…"):
            logger.info(f"check-updates novel {novel_id}: another batch owns this novel — skipped")
            return
        try:
            # Full chapter list from the source (novel info carries the dir listing)
            info = _get_novel_info_sync(scraper, novel.source_url)
            if not info or not info.chapters:
                logger.warning(
                    f"check-updates novel {novel_id}: could not read a chapter list from "
                    f"{novel.source_url} — scraper returned "
                    f"{'nothing' if not info else '0 chapters'}")
                _finish_batch(novel_id, "Could not read the source chapter list")
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
                _finish_batch(novel_id, "No new chapters")
                return

            # Append new chapters (numbered after the last known)
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
            # autoflush=False: flush the new chapters before counting them, or
            # total_chapters is written as the PRE-insert number (516 instead of
            # 540) and the reader's "Ch N/total" + next-chapter nav go stale.
            db.flush()
            novel.total_chapters = db.query(Chapter).filter(Chapter.novel_id == novel_id).count()
            db.commit()
            logger.info(f"check-updates novel {novel_id}: added {added} new chapters "
                        f"({start_num}..{start_num + added - 1})")

            # Translate the first few NEW chapters (translate-ahead handles the rest
            # while reading). Scope this to chapter_number >= start_num: an unscoped
            # "first 5 untranslated" query picks the LOWEST-numbered untranslated
            # chapters in the whole novel, so on a part-translated novel this
            # translated e.g. ch 37-41 instead of the ones just discovered.
            todo = (db.query(Chapter)
                    .filter(Chapter.novel_id == novel_id,
                            Chapter.chapter_number >= start_num,
                            Chapter.is_translated == False)
                    .order_by(Chapter.chapter_number)
                    .limit(5).all())
            _set_batch_total(novel_id, len(todo) or 1,
                             label=f"Added {added} new chapter(s)")
            for ch in todo:
                try:
                    if not ch.original_content:
                        ch_data = _fetch_chapter_content_sync(ch.source_url)
                        if ch_data and ch_data.content:
                            ch.original_content = ch_data.content
                            ch.word_count = getattr(ch_data, "word_count", 0) or 0
                            db.commit()
                    db.refresh(ch)
                    if ch.original_content:
                        _translate_chapter_bg(novel_id, ch.chapter_number, "balanced")
                    _bump_batch(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
                except RelayAuthError as e:
                    logger.error(f"check-updates translate stopped: relay key rejected ({e})")
                    break
                except Exception as e:
                    logger.warning(f"check-updates translate ch{ch.chapter_number} failed: {e}")
            _finish_batch(novel_id, f"Added {added} new chapter(s)")
        except Exception as e:
            _finish_batch(novel_id, f"Unexpected error: {str(e)[:120]}")
            raise
    finally:
        db.close()


def _get_novel_info_sync(scraper, source_url: str):
    """Sync wrapper around the scraper's get_novel_info method."""
    import asyncio as _asyncio
    async def _fetch():
        async with scraper:
            return await scraper.get_novel_info(source_url)
    try:
        return _asyncio.run(_fetch())
    except Exception as e:
        logger.warning(f"novel info fetch failed: {e}")
        return None


@app.post("/api/novels/{novel_id}/export-epub")
async def export_epub(novel_id: int, background_tasks: BackgroundTasks = None,
                      db: Session = Depends(get_db_session)):
    """Build an EPUB of the novel's translated chapters (background)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    async with _async_novel_lock(novel_id):
        if _batch_running(novel_id):
            return {"status": "already_running"}
        background_tasks.add_task(_export_epub_bg, novel_id)
    return {"status": "started"}


@app.get("/api/novels/{novel_id}/epub-download")
async def epub_download(novel_id: int, db: Session = Depends(get_db_session)):
    """Download the generated EPUB (404 until the job finishes)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    path = _epub_path(novel)
    if not path.exists():
        raise HTTPException(status_code=404, detail="EPUB not ready yet")
    return FileResponse(path, media_type="application/epub+zip",
                        filename=f"{_safe_filename(novel.title_translated or novel.title)}.epub")


def _safe_filename(name: str) -> str:
    return re.sub(r'[^\w\- ]', '', name)[:80].strip() or "novel"


def _epub_path(novel) -> Path:
    d = Path(DATA_DIR) / "epub"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"novel_{novel.id}.epub"


def _export_epub_bg(novel_id: int):
    """Background: assemble the EPUB from translated chapters.

    No stop-check in the per-chapter loop, unlike the translate batches: this
    does no relay calls, only local DB reads and in-memory EPUB assembly, so
    it doesn't share their multi-minute-per-item cost that makes a mid-run
    stop worth supporting."""
    from ebooklib import epub
    from database import SessionLocal
    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel:
            return
        chapters = (db.query(Chapter).filter(
            Chapter.novel_id == novel_id,
            Chapter.is_translated == True,
            Chapter.translated_content.isnot(None),
        ).order_by(Chapter.chapter_number).all())
        if not _set_batch(novel_id, "epub", len(chapters)):
            return
        if not chapters:
            # The endpoint queues this unconditionally with no pre-check
            # (unlike translate-to-end / retranslate, which return "none").
            # Without reporting an outcome here, clicking "Export EPUB" on a
            # novel with nothing translated yet showed the spinner and then
            # simply nothing — no file, no error, no explanation.
            _finish_batch(novel_id, "No translated chapters to export yet")
            return

        book = epub.EpubBook()
        book.set_identifier(f"nyaa-{novel_id}")
        book.set_title(novel.title_translated or novel.title)
        if novel.author:
            book.add_author(novel.author)
        book.set_language(novel.target_language or "en")
        if novel.description_translated or novel.description:
            book.add_metadata("DC", "description", novel.description_translated or novel.description)

        book_items = []
        for i, ch in enumerate(chapters):
            title = ch.title_translated or ch.title or f"Chapter {ch.chapter_number}"
            body = ch.translated_content or ""
            # paragraphs -> <p> blocks
            paras = _split_paragraphs(body)
            html_body = "".join(f"<p>{_html_escape(p)}</p>" for p in paras) or "<p></p>"
            item = epub.EpubHtml(
                title=title,
                file_name=f"ch_{ch.chapter_number:04d}.xhtml",
                lang=novel.target_language or "en",
                content=f"<h1>{_html_escape(title)}</h1>{html_body}",
            )
            book.add_item(item)
            book_items.append(item)
            _bump_batch(novel_id, label=f"Ch {ch.chapter_number} {title[:40]}")
            if (i + 1) % 25 == 0:
                db.close()  # keep memory tidy on huge novels
                db = SessionLocal()

        book.toc = tuple(book_items)  # EpubHtml items (NOT (title, href) tuples — that breaks nav)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav"] + book_items
        out = _epub_path(novel)
        epub.write_epub(str(out), book)
        _finish_batch(novel_id, f"EPUB built — {len(chapters)} chapters")
        logger.info(f"EPUB for novel {novel_id}: {out} ({len(chapters)} chapters)")
    except Exception as e:
        logger.error(f"EPUB export failed for {novel_id}: {e}")
        _finish_batch(novel_id, f"EPUB build failed: {str(e)[:120]}")
    finally:
        db.close()


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _split_paragraphs(text: str) -> list:
    """Same rule as frontend/lib/text.js's splitParagraphs (kept in sync
    manually — no shared runtime between Python and the browser build).

    Two paragraph conventions are in play: CJK source uses one paragraph per
    LINE (single \\n); a translation may come back blank-line separated
    instead, and the model's formatting is not consistent enough to rely on
    one or the other. Splitting only on blank lines (the previous EPUB
    behavior) rendered a single-newline chapter as one giant <p> — the same
    bug fixed in the reader, just never ported to the exporter."""
    text = text or ""
    by_blank = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    by_line = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chosen = by_line if len(by_line) > len(by_blank) * 2 else by_blank
    # a single \n inside a kept block (blank-line mode) is a wrapped line, not
    # a paragraph break — join it into a space, matching the JS version
    return [re.sub(r"\s*\n\s*", " ", p).strip() for p in chosen if p.strip()]


@app.post("/api/novels/{novel_id}/generate-cover")
async def generate_cover(novel_id: int, db: Session = Depends(get_db_session)):
    """Ask the relay to design an SVG cover from the novel's title/synopsis."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    try:
        svg = await _generate_cover_svg(novel)
    except Exception as e:
        logger.error(f"cover gen failed: {e}")
        raise HTTPException(status_code=502, detail=f"Cover generation failed: {e}")
    if not svg:
        raise HTTPException(status_code=502, detail="Cover generation returned nothing")
    d = DATA_DIR / "covers"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"novel_{novel_id}.svg"
    path.write_text(svg, encoding="utf-8")
    # serve via the static mount (frontend dir) is wrong — use a covers route instead
    novel.cover_url = f"/api/novels/{novel_id}/cover"
    db.commit()
    return {"status": "ok", "cover_url": novel.cover_url}


@app.post("/api/novels/{novel_id}/cover-upload")
async def upload_cover(novel_id: int, file: UploadFile = File(...),
                       db: Session = Depends(get_db_session)):
    """Upload a user-provided cover image (png/jpg/webp/gif). Replaces any existing cover."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    # Validate it's an image by extension + peek at content
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
    # remove any previously generated svg cover so it doesn't linger
    for old in d.glob(f"novel_{novel_id}.*"):
        if old != path:
            try:
                old.unlink()
            except OSError:
                # Genuinely inconsequential: novel_cover() always serves the
                # LATEST-mtime file matching this glob, so a stale sibling
                # that fails to delete is unused dead weight, never served.
                pass
    novel.cover_url = f"/api/novels/{novel_id}/cover"
    db.commit()
    return {"status": "ok", "cover_url": novel.cover_url}


@app.get("/api/novels/{novel_id}/cover")
async def novel_cover(novel_id: int, db: Session = Depends(get_db_session)):
    """Serve the novel's cover image (uploaded or AI-generated)."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    d = DATA_DIR / "covers"
    # prefer the latest image file for this novel (svg or raster)
    candidates = sorted(d.glob(f"novel_{novel_id}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise HTTPException(status_code=404, detail="Cover not found")
    path = candidates[0]
    mime = "image/svg+xml" if path.suffix == ".svg" else "image/" + (path.suffix[1:] or "png")
    return FileResponse(path, media_type=mime)


async def _generate_cover_svg(novel) -> str:
    """Build a cover SVG via the relay: ask for a color scheme + motif based on
    title/synopsis, then render a styled 2:3 cover locally (deterministic, safe)."""
    from translator import OpenAIRelayTranslator
    import asyncio

    title = novel.title_translated or novel.title or "NyaaReader"
    synopsis = (novel.description_translated or novel.description or "")[:600]

    prompt = (
        "You design book covers. For the novel below, output ONLY a JSON object:\n"
        '{"bg1":"#hex","bg2":"#hex","accent":"#hex","motif":"one of: mountain|ocean|moon|sword|flower|dragon|stars|tree|flame|city|mask|gate"}'
        "\nRules: bg1/bg2 = a moody vertical gradient pair matching the novel's vibe; "
        "accent = a readable highlight color; motif = a single symbolic element fitting the story. "
        f"\n\nTITLE: {title}\nSYNOPSIS: {synopsis}"
    )
    llm = OpenAIRelayTranslator(model="deepseek-v4-flash")
    raw = await asyncio.to_thread(llm._generate, prompt)
    import json as _json
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise RuntimeError("relay returned no JSON")
    d = _json.loads(m.group(0))
    bg1 = d.get("bg1", "#1b1b2f")
    bg2 = d.get("bg2", "#162447")
    accent = d.get("accent", "#e43f5a")
    motif = d.get("motif", "stars")
    if not re.match(r"^#[0-9a-fA-F]{6}$", str(bg1)): bg1 = "#1b1b2f"
    if not re.match(r"^#[0-9a-fA-F]{6}$", str(bg2)): bg2 = "#162447"
    if not re.match(r"^#[0-9a-fA-F]{6}$", str(accent)): accent = "#e43f5a"

    # minimal motif glyphs (SVG paths, 2:3 canvas 300x450)
    motifs = {
        "mountain": '<path d="M0 330 L90 200 L150 290 L210 220 L300 340 L300 450 L0 450 Z" fill="rgba(255,255,255,.10)"/>'
                    '<path d="M0 380 L120 260 L200 340 L300 290 L300 450 L0 450 Z" fill="rgba(255,255,255,.07)"/>',
        "ocean":   '<path d="M0 300 C60 280 120 320 180 300 C240 280 280 310 300 295 L300 450 L0 450 Z" fill="rgba(255,255,255,.12)"/>'
                   '<path d="M0 350 C70 330 140 370 210 350 C250 338 280 355 300 345 L300 450 L0 450 Z" fill="rgba(255,255,255,.08)"/>',
        "moon":    '<circle cx="225" cy="110" r="46" fill="none" stroke="rgba(255,255,255,.55)" stroke-width="3"/>'
                   '<circle cx="238" cy="98" r="42" fill="none" stroke="rgba(255,255,255,.35)" stroke-width="3"/>',
        "sword":   '<path d="M150 90 L240 330 L150 380 L60 330 Z" fill="rgba(255,255,255,.10)"/>'
                   '<path d="M143 100 L157 100 L152 90 Z" fill="rgba(255,255,255,.25)"/>',
        "flower":  '<g fill="rgba(255,255,255,.18)"><circle cx="150" cy="140" r="34"/><circle cx="132" cy="118" r="26"/><circle cx="168" cy="118" r="26"/><circle cx="132" cy="162" r="26"/><circle cx="168" cy="162" r="26"/></g>'
                   '<circle cx="150" cy="140" r="14" fill="rgba(255,255,255,.4)"/>',
        "dragon":  '<path d="M90 160 C130 120 200 130 210 180 C220 230 170 240 180 280 L150 270 C150 230 180 210 170 180 C160 150 120 150 105 175 Z" fill="rgba(255,255,255,.14)"/>',
        "stars":   '<g fill="rgba(255,255,255,.6)"><circle cx="70" cy="80" r="3"/><circle cx="240" cy="60" r="2.5"/><circle cx="200" cy="160" r="2"/><circle cx="110" cy="200" r="2.5"/><circle cx="260" cy="240" r="2"/><circle cx="50" cy="160" r="2"/></g>',
        "tree":    '<path d="M150 120 C120 180 110 220 115 300 L185 300 C190 220 180 180 150 120 Z" fill="rgba(255,255,255,.12)"/>'
                   '<path d="M150 120 C130 90 170 90 150 60 C140 90 160 90 150 120 Z" fill="rgba(255,255,255,.14)"/>',
        "flame":   '<path d="M150 100 C110 170 100 200 150 260 C200 200 190 170 150 100 Z" fill="rgba(255,255,255,.15)"/>',
        "city":    '<g fill="rgba(255,255,255,.10)"><rect x="40" y="250" width="40" height="140"/><rect x="90" y="210" width="34" height="180"/><rect x="135" y="260" width="42" height="130"/><rect x="190" y="200" width="36" height="190"/><rect x="238" y="250" width="40" height="140"/></g>',
        "mask":    '<path d="M110 170 C110 140 190 140 190 170 L205 250 C205 290 95 290 95 250 Z" fill="rgba(255,255,255,.12)"/>',
        "gate":    '<path d="M120 180 L120 340 L180 340 L180 180 L150 140 Z" fill="none" stroke="rgba(255,255,255,.25)" stroke-width="4"/>'
                   '<path d="M120 180 Q150 200 180 180" fill="none" stroke="rgba(255,255,255,.25)" stroke-width="4"/>',
    }
    glyph = motifs.get(motif, motifs["stars"])

    # escape title for SVG (limit to ~3 lines, wrap conservatively)
    # canvas is 300 wide; at font-size 23, ~16-18 chars fit per line safely.
    title_short = title[:80]
    words = title_short.split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > 17:
            if cur: lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur: lines.append(cur)
    lines = lines[:3]
    if len(lines) == 3 or max(len(l) for l in lines) > 16:
        font_size = 19
    else:
        font_size = 23
    tspans = "".join(
        f'<tspan x="150" dy="{30 if i else 0}">{_xml_escape(l)}</tspan>' for i, l in enumerate(lines)
    )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 450">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0.4" y2="1">
      <stop offset="0" stop-color="{bg1}"/>
      <stop offset="1" stop-color="{bg2}"/>
    </linearGradient>
    <linearGradient id="sh" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="rgba(0,0,0,0)"/>
      <stop offset="1" stop-color="rgba(0,0,0,.55)"/>
    </linearGradient>
  </defs>
  <rect width="300" height="450" fill="url(#g)"/>
  {glyph}
  <rect width="300" height="450" fill="url(#sh)"/>
  <rect x="10" y="10" width="280" height="430" fill="none" stroke="rgba(255,255,255,.28)" stroke-width="2" rx="8"/>
  <text font-family="Georgia, serif" font-size="{font_size}" font-weight="bold" fill="#ffffff"
        text-anchor="middle" x="150" y="270" letter-spacing="1">{tspans}</text>
  <text font-family="Georgia, serif" font-size="13" fill="rgba(255,255,255,.7)"
        text-anchor="middle" x="150" y="360">NyaaReader</text>
  <rect x="120" y="374" width="60" height="3" rx="1.5" fill="{accent}"/>
</svg>'''


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


@app.get("/api/novels/{novel_id}/drift-count")
async def drift_count(novel_id: int, db: Session = Depends(get_db_session)):
    """Chapters whose translated content misses a LOCKED glossary term
    (translated before the lock existed — candidates for smart retranslate)."""
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


def _locked_terms(db, novel_id: int) -> list:
    """Locked glossary names (terms locked by the user, must appear in translations)."""
    mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
    if not mem or not mem.glossary_entries:
        return []
    try:
        entries = json.loads(mem.glossary_entries) if isinstance(mem.glossary_entries, str) else mem.glossary_entries
    except Exception as e:
        # Corrupted glossary_entries silently disables every locked term for
        # this novel with no indication why — worth a log line since it's the
        # kind of thing that otherwise looks like the feature just stopped
        # working.
        logger.warning(f"_locked_terms: could not parse glossary_entries for novel {novel_id}: {e}")
        return []
    terms = []
    for e in entries or []:
        if isinstance(e, dict) and e.get("locked") and e.get("translated"):
            terms.append(str(e["translated"]).strip())
    return [t for t in terms if t]


@app.post("/api/novels/{novel_id}/retranslate-drift")
async def retranslate_drift(novel_id: int, background_tasks: BackgroundTasks = None,
                            db: Session = Depends(get_db_session)):
    """Retranslate chapters that miss a locked glossary term (drift fix)."""
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
    async with _async_novel_lock(novel_id):
        if _batch_running(novel_id):
            return {"status": "already_running", "pending": 0}
        background_tasks.add_task(_retranslate_drift_bg, novel_id, [c.chapter_number for c in targets])
    return {"status": "started", "pending": len(targets)}


def _retranslate_drift_bg(novel_id: int, chapter_numbers: list):
    """Background: force-retranslate the drifted chapters (locks re-applied)."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        if not _set_batch(novel_id, "retranslate-drift", len(chapter_numbers),
                         args={"chapter_numbers": chapter_numbers}):
            return
        done = 0
        outcome = None
        for n in chapter_numbers:
            # NOTE: this loop had neither a stop-check nor a RelayAuthError
            # shortcut — every other batch loop honors "Stop" and breaks
            # immediately on a rejected key instead of failing every
            # remaining chapter one at a time against a dead credential.
            if _batch_stop_requested(novel_id):
                logger.info(f"drift retranslate stopped by user (novel {novel_id})")
                outcome = _stopped_label(done, len(chapter_numbers), "drifted chapters fixed")
                break
            ch = db.query(Chapter).filter(
                Chapter.novel_id == novel_id, Chapter.chapter_number == n).first()
            if not ch:
                _bump_batch(novel_id, label=f"Ch {n} (missing)")
                continue
            try:
                _translate_chapter(db, ch, "balanced", force=True)
                db.commit()
                done += 1
                _bump_batch(novel_id, label=f"Ch {n} {ch.title_translated or ch.title or ''}")
            except RelayAuthError as e:
                logger.error(f"drift retranslate stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(chapter_numbers), "drifted chapters fixed")
                break
            except Exception as e:
                logger.warning(f"drift retranslate ch{n}: {e}")
                db.rollback()
        _finish_batch(novel_id, outcome or f"Fixed {done}/{len(chapter_numbers)} drifted chapters")
    finally:
        db.close()


@app.post("/api/novels/{novel_id}/retry-failed")
async def retry_failed(novel_id: int, background_tasks: BackgroundTasks = None,
                       db: Session = Depends(get_db_session)):
    """Retry chapters whose last translation attempt failed (last_error set)."""
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
    async with _async_novel_lock(novel_id):
        if _batch_running(novel_id):
            return {"status": "already_running", "pending": 0}
        background_tasks.add_task(_retry_failed_bg, novel_id)
    return {"status": "started", "pending": count}


@app.get("/api/novels/{novel_id}/failed-count")
async def failed_count(novel_id: int, db: Session = Depends(get_db_session)):
    """Number of chapters with a recorded translation error (for the retry badge)."""
    count = db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.last_error.isnot(None),
        Chapter.last_error != "",
        Chapter.is_translated == False,
    ).count()
    return {"failed": count}


@app.post("/api/novels/{novel_id}/translate-titles")
async def translate_titles(novel_id: int, background_tasks: BackgroundTasks = None,
                           db: Session = Depends(get_db_session)):
    """Background: translate only the chapter titles still missing title_translated.
    Lightweight (short text per title) — no full chapter retranslation."""
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
    async with _async_novel_lock(novel_id):
        if _batch_running(novel_id):
            return {"status": "already_running", "pending": 0}
        background_tasks.add_task(translate_titles_bg, novel_id)
    return {"status": "started", "pending": missing}


def translate_titles_bg(novel_id: int):
    """Translate missing chapter titles (short texts, cheap — no full chapters)."""
    from database import SessionLocal
    from translator import get_translator
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
        translator = get_translator()
        if translator is None:
            # Same guard _translate_chapter() has. Without it every chapter
            # raised AttributeError on None, was swallowed by the per-chapter
            # except, and the job reported "complete" with nothing translated.
            logger.error(f"translate-titles aborted (novel {novel_id}): no API key configured (set FALLBACK_API_KEY in Settings)")
            return
        if not _set_batch(novel_id, "titles", len(chapters)):
            return
        done = 0
        outcome = None
        for ch in chapters:
            if _batch_stop_requested(novel_id):
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
                _bump_batch(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
                # Small delay between relay calls (short texts, but stay polite)
                import time as _t
                _t.sleep(1.5)
            except RelayAuthError as e:
                # translate_short's underlying FallbackTranslator shares the
                # same relay key as every other tier, so a rejected key is
                # permanent — a bare `except Exception` here previously
                # swallowed this and looped through every remaining title,
                # failing each one individually against a dead credential.
                logger.error(f"translate-titles stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(chapters), "titles translated")
                break
            except Exception as e:
                logger.warning(f"title translate ch{ch.chapter_number} failed: {e}")
                db.rollback()
        _finish_batch(novel_id, outcome or f"Translated {done}/{len(chapters)} titles")
    finally:
        db.close()


@app.post("/api/novels/{novel_id}/translate-meta")
async def translate_novel_meta(novel_id: int, background_tasks: BackgroundTasks = None,
                               db: Session = Depends(get_db_session)):
    """Retry translating the NOVEL title + description (synopsis) — runs only
    once at add time normally; this lets you re-trigger it for existing novels."""
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
    async with _async_novel_lock(novel_id):
        if _batch_running(novel_id):
            return {"status": "already_running", "pending": 0}
        background_tasks.add_task(translate_novel_meta_bg, novel_id)
    return {"status": "started", "pending": pending}


@app.post("/api/novels/{novel_id}/retranslate")
async def retranslate_novel(novel_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db_session)):
    """Re-translate all already-translated chapters in the background so the
    current (possibly locked) glossary is applied consistently."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    count = db.query(Chapter).filter(
        Chapter.novel_id == novel_id, Chapter.is_translated == True).count()
    if count == 0:
        raise HTTPException(status_code=400, detail="No translated chapters to re-translate")
    async with _async_novel_lock(novel_id):
        if _batch_running(novel_id):
            return {"status": "already_running", "pending": 0}
        background_tasks.add_task(_retranslate_bg, novel_id)
    return {"status": "started", "chapters": count}


def _retranslate_bg(novel_id: int):
    """Background re-translate of every translated chapter (sequential, force)."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        chapters = db.query(Chapter).filter(
            Chapter.novel_id == novel_id, Chapter.is_translated == True,
            Chapter.original_content.isnot(None),
        ).order_by(Chapter.chapter_number).all()
        if not _set_batch(novel_id, "retranslate", len(chapters)):
            return
        done = 0
        outcome = None
        for ch in chapters:
            if _batch_stop_requested(novel_id):
                logger.info(f"retranslate stopped by user (novel {novel_id})")
                outcome = _stopped_label(done, len(chapters), "chapters retranslated")
                break
            try:
                _translate_chapter(db, ch, quality="balanced", force=True)
                done += 1
                _bump_batch(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
            except RelayAuthError as e:
                logger.error(f"retranslate stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(chapters), "chapters retranslated")
                break
            except Exception as e:
                logger.warning(f"retranslate ch{ch.chapter_number} failed: {e}")
                db.rollback()
        _finish_batch(novel_id, outcome or f"Retranslated {done}/{len(chapters)} chapters")
    finally:
        db.close()


@app.get("/api/novels/{novel_id}/progress", response_model=ReadingProgressResponse)
async def get_progress(novel_id: int, db: Session = Depends(get_db_session)):
    """Get reading progress for a novel"""
    progress = db.query(ReadingProgress).filter(
        ReadingProgress.novel_id == novel_id
    ).first()
    
    if not progress:
        # Return default progress
        return ReadingProgressResponse(
            novel_id=novel_id,
            chapter_id=0,
            scroll_position=0,
            percentage=0.0,
            last_read_at=datetime.utcnow()
        )
    return progress


@app.post("/api/novels/{novel_id}/progress")
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


@app.put("/api/novels/{novel_id}/reading-status")
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


@app.post("/api/novels/{novel_id}/search")
async def search_novel(novel_id: int, payload: dict, db: Session = Depends(get_db_session)):
    """Full-text search across translated chapter content. Returns matching chapters
    with a snippet around the first hit."""
    q = (payload.get("q") or "").strip()
    if len(q) < 2:
        return {"results": []}
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    chapters = (db.query(Chapter)
                .filter(Chapter.novel_id == novel_id,
                        Chapter.translated_content.contains(q, autoescape=True))
                .order_by(Chapter.chapter_number)
                .limit(30).all())
    results = []
    for ch in chapters:
        text = ch.translated_content or ""
        idx = text.lower().find(q.lower())
        if idx < 0:
            idx = text.find(q)
        if idx < 0:
            idx = 0   # SQL matched but Python didn't (case folding) — show the head
        start = max(0, idx - 80)
        end = min(len(text), idx + len(q) + 160)
        snippet = ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")
        results.append({
            "chapter_number": ch.chapter_number,
            "title": ch.title_translated or ch.title or f"Chapter {ch.chapter_number}",
            "snippet": snippet,
            "count": text.lower().count(q.lower()),
        })
    return {"results": results}


@app.get("/api/novels/{novel_id}/bookmarks")
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


@app.post("/api/chapters/{chapter_id}/bookmarks")
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


@app.delete("/api/bookmarks/{bookmark_id}")
async def delete_bookmark(bookmark_id: int, db: Session = Depends(get_db_session)):
    bm = db.query(Bookmark).filter(Bookmark.id == bookmark_id).first()
    if not bm:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    db.delete(bm)
    db.commit()
    return {"status": "ok"}


# ============ Reading diary (personal reflections) ============
@app.get("/api/chapters/{chapter_id}/diary")
async def get_diary(chapter_id: int, db: Session = Depends(get_db_session)):
    """Get the user's diary entry for a chapter (empty string if none)."""
    ch = db.query(Chapter).filter(Chapter.id == chapter_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Chapter not found")
    entry = db.query(DiaryEntry).filter(DiaryEntry.chapter_id == chapter_id).first()
    return {"chapter_id": chapter_id, "chapter_number": ch.chapter_number,
            "content": entry.content if entry else ""}


@app.put("/api/chapters/{chapter_id}/diary")
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


@app.get("/api/novels/{novel_id}/diary")
async def list_diary(novel_id: int, db: Session = Depends(get_db_session)):
    """All diary entries for a novel (by chapter number)."""
    entries = (db.query(DiaryEntry)
               .filter(DiaryEntry.novel_id == novel_id)
               .order_by(DiaryEntry.chapter_number)
               .all())
    return [{"chapter_number": e.chapter_number, "content": e.content,
             "updated_at": e.updated_at.isoformat() if e.updated_at else None}
            for e in entries]


@app.get("/api/novels/{novel_id}/settings", response_model=dict)
async def get_settings(novel_id: int, db: Session = Depends(get_db_session)):
    """Get novel settings"""
    settings = db.query(NovelSettings).filter(
        NovelSettings.novel_id == novel_id
    ).first()
    
    if not settings:
        # Return defaults
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


@app.put("/api/novels/{novel_id}/settings")
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


@app.delete("/api/novels/{novel_id}")
async def delete_novel(novel_id: int, db: Session = Depends(get_db_session)):
    """Delete a novel and all its data"""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")
    
    db.delete(novel)
    db.commit()
    return {"status": "deleted"}


@app.post("/api/novels/{novel_id}/fetch-chapters")
async def fetch_more_chapters(
    novel_id: int,
    start: int = 1,
    count: int = 10,
    translate: Optional[bool] = None,
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db_session)
):
    """Fetch more chapters for a novel (background task).

    Populates original_content (+ word_count) for chapters [start, start+count)
    whose content is missing, and translates them when translate=True (or the
    novel's auto_translate setting is on).
    """
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    if start < 1:
        raise HTTPException(status_code=400, detail="start must be >= 1")
    count = max(1, min(count, 100))

    # Honor per-novel auto_translate if not explicitly told otherwise
    settings = db.query(NovelSettings).filter(NovelSettings.novel_id == novel_id).first()
    do_translate = translate if translate is not None else bool(settings and settings.auto_translate)

    if background_tasks:
        background_tasks.add_task(fetch_chapters_range, novel_id, start, count, do_translate)
        return {"status": "queued", "message": f"Fetching {count} chapters from chapter {start}"}

    # Sync path (tests): run inline
    await fetch_chapters_range(novel_id, start, count, do_translate)
    return {"status": "done", "message": f"Fetched {count} chapters from chapter {start}"}


# Persistent background-job tracker (DB-backed — survives restarts, auto-resumes).
# In-memory cache mirrors the DB for cheap reads; every mutation commits to DB.
_batch_cache = {}  # novel_id -> {kind, total, done, current_label, running}

# Per-novel locks so two near-simultaneous batch POSTs can't both pass the
# "_batch_running check then add_task" window and spawn duplicate background
# workers on the same novel (that corrupted the shared progress counters).
import threading as _threading
_batch_locks = {}
_batch_locks_guard = _threading.Lock()

def _novel_lock(novel_id: int) -> _threading.Lock:
    with _batch_locks_guard:
        lock = _batch_locks.get(novel_id)
        if lock is None:
            lock = _batch_locks[novel_id] = _threading.Lock()
        return lock


# Async twin of _novel_lock for async ENDPOINTS. `with _novel_lock()` on the
# event loop BLOCKS THE WHOLE APP while a batch worker (slow relay AI — minutes
# per chapter) holds that threading.Lock. Async endpoints must take this
# asyncio.Lock instead: awaiting yields, so other requests keep being served
# while a batch runs. (Background worker threads keep using _novel_lock.)
_async_batch_locks: dict = {}


def _async_novel_lock(novel_id: int) -> asyncio.Lock:
    with _batch_locks_guard:
        lock = _async_batch_locks.get(novel_id)
        if lock is None:
            lock = _async_batch_locks[novel_id] = asyncio.Lock()
        return lock

# Politeness delay between consecutive source fetches (seconds) — avoids rate limits
FETCH_DELAY_SECONDS = float(os.getenv("FETCH_DELAY_SECONDS", "15"))


def _sleep_between_fetches():
    """Sleep between consecutive source fetches to avoid rate limiting."""
    import time as _time
    _time.sleep(FETCH_DELAY_SECONDS)


def _get_or_create_job(novel_id, kind):
    """Return the active BatchJob row for (novel_id, kind) or create one."""
    from database import SessionLocal
    from models import BatchJob
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
    from models import BatchJob
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
    """Start (or resume) a batch job. Returns True if THIS call owns the run
    (i.e. no other running job exists for this novel), False if a duplicate
    would start — callers should skip work when False.

    `args` (a JSON-serializable dict) is persisted on the row for kinds that
    need per-call arguments a restart wouldn't otherwise have — see
    _launch_batch(). Kinds that need nothing beyond novel_id can omit it.

    NOTE: only ONE batch job per novel may run at a time (any kind). The
    bump/clear helpers address the most-recent running row, so concurrent
    jobs of different kinds would corrupt each other's progress counters
    (e.g. translate-ahead done > total → frontend spinner never stops)."""
    from database import SessionLocal
    from models import BatchJob
    args_json = json.dumps(args) if args is not None else ""
    db = SessionLocal()
    try:
        # Any running job of this novel (any kind) → refuse to start another.
        # If its updated_at is stale (> 5 min), it's a crashed leftover — take over.
        existing = db.query(BatchJob).filter(
            BatchJob.novel_id == novel_id, BatchJob.running == True).order_by(
            BatchJob.id.desc()).first()
        if existing:
            from datetime import datetime, timedelta
            if existing.updated_at and (datetime.utcnow() - existing.updated_at).total_seconds() < JOB_STALL_MINUTES * 60:
                db.close()
                return False
            # Stale flag from a crash → take over this row
            existing.kind = kind
            existing.total = total
            existing.done = 0
            existing.current_label = label
            existing.args_json = args_json
            db.commit()
            db.refresh(existing)
            job_id = existing.id
        else:
            job = BatchJob(novel_id=novel_id, kind=kind, total=total, done=0,
                           current_label=label, running=True, args_json=args_json)
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id
    finally:
        db.close()
    _batch_cache[novel_id] = {"kind": kind, "total": total, "done": 0,
                              "current_label": label, "running": True,
                              "stop_requested": False}
    return True


def _request_batch_stop(novel_id: int) -> bool:
    """Ask the running batch (any kind) for this novel to stop after the
    current chapter. Returns True if a batch was running and got the signal."""
    b = _batch_cache.get(novel_id)
    if b and b.get("running"):
        b["stop_requested"] = True
        return True
    from database import SessionLocal
    from models import BatchJob
    db = SessionLocal()
    try:
        job = db.query(BatchJob).filter(
            BatchJob.novel_id == novel_id, BatchJob.running == True).order_by(
            BatchJob.id.desc()).first()
        if job:
            job.stop_requested = True
            db.commit()
            b = _batch_cache.setdefault(novel_id, {})
            b["running"] = True
            b["stop_requested"] = True
            return True
        return False
    finally:
        db.close()


def _batch_stop_requested(novel_id: int) -> bool:
    """True if the user asked the current batch to stop (checked between
    chapters by the background loops)."""
    b = _batch_cache.get(novel_id)
    if b and b.get("stop_requested"):
        return True
    from database import SessionLocal
    from models import BatchJob
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
    from models import BatchJob
    from sqlalchemy import update
    db = SessionLocal()
    try:
        # Atomic increment (UPDATE ... SET done = done + inc) — read-modify-write
        # across sessions could drop increments when two workers bump concurrently.
        res = db.execute(
            update(BatchJob)
            .where(BatchJob.novel_id == novel_id, BatchJob.running == True)
            .values(done=BatchJob.done + done_inc,
                    current_label=label if label else BatchJob.current_label)
        )
        db.commit()
        # refresh the cache with the actual (server-computed) value if we touched a row
        b = _batch_cache.get(novel_id)
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
    """Adjust a running job's total once the worker knows the real size (a job
    that has to scrape before it can count starts at a placeholder)."""
    from database import SessionLocal
    from models import BatchJob
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
            b = _batch_cache.get(novel_id)
            if b:
                b["total"] = total
                if label:
                    b["current_label"] = label
    finally:
        db.close()


def _finish_batch(novel_id, label=""):
    """Record an outcome label, then close the job. Lets a job report WHY it
    ended ("No new chapters", "Could not read the source chapter list")
    instead of vanishing and looking identical to a success."""
    if label:
        _bump_batch(novel_id, label=label, done_inc=0)
    _clear_batch(novel_id)


# Shared outcome labels for the per-chapter batch loops below. Every one of
# them used to end with a bare _clear_batch(novel_id) — the job vanished with
# no record of whether it completed, was stopped, or died against a rejected
# key, so a batch that failed instantly looked identical to one that finished
# cleanly. These three helpers are the vocabulary; each _*_bg function builds
# its own "completed" label (the verb differs — translated/retranslated/
# retried/built — but stopped/rejected are always phrased the same way).
def _stopped_label(done: int, total: int, what: str) -> str:
    return f"Stopped by user — {done}/{total} {what}"


def _auth_rejected_label(done: int, total: int, what: str) -> str:
    return f"Stopped — relay key rejected, check Settings — {done}/{total} {what}"


def _clear_batch(novel_id):
    """Mark all running batch jobs for this novel as finished (the bump helper
    may have been split across several rows by a race; leaving any row running
    would keep the frontend spinner alive forever)."""
    from database import SessionLocal
    from models import BatchJob
    db = SessionLocal()
    try:
        jobs = db.query(BatchJob).filter(
            BatchJob.novel_id == novel_id, BatchJob.running == True).all()
        for job in jobs:
            job.running = False
            job.done = job.total
        db.commit()
    finally:
        db.close()
    b = _batch_cache.get(novel_id)
    if b:
        b["running"] = False
        b["done"] = b["total"]


@app.post("/api/novels/{novel_id}/batch-stop")
async def batch_stop(novel_id: int):
    """Ask the running batch (any kind) to stop after the current chapter."""
    stopped = _request_batch_stop(novel_id)
    return {"status": "stopped" if stopped else "idle"}


@app.get("/api/novels/{novel_id}/batch-status")
async def batch_status(novel_id: int):
    """Poll: current background batch progress for a novel (translate-ahead / retranslate / fetch)."""
    cached = _batch_cache.get(novel_id)
    if cached:
        # Defensive clamp: a corrupted counter (done > total from a historical
        # concurrent-job bug) would keep the frontend spinner spinning forever.
        if cached["total"] > 0 and cached["done"] > cached["total"]:
            cached["done"] = cached["total"]
        return cached
    from database import SessionLocal
    from models import BatchJob
    db = SessionLocal()
    try:
        job = db.query(BatchJob).filter(
            BatchJob.novel_id == novel_id, BatchJob.running == True).order_by(
            BatchJob.id.desc()).first()
        if not job:
            return {"kind": None, "total": 0, "done": 0, "current_label": "", "running": False}
        done = min(job.done, job.total) if job.total > 0 else job.done
        data = {"kind": job.kind, "total": job.total, "done": done,
                "current_label": job.current_label or "", "running": True}
        _batch_cache[novel_id] = data
        return data
    finally:
        db.close()


def _resume_interrupted_jobs():
    """On startup: find jobs still marked running (killed by a restart) and relaunch them.
    The bg functions are idempotent — they skip already-done chapters, so resuming is safe.
    After a restart the old process is definitely dead, so EVERY running row is
    stale. Each row is therefore marked finished BEFORE anything is relaunched:
    the resumed worker's first act is _set_batch(), which refuses to start while
    a running row still looks fresh (updated_at < JOB_STALL_MINUTES). Leaving
    the row running silently dropped the job on any restart that happened within
    10 minutes of the last progress bump — and blocked new batches meanwhile.

    Whether a kind CAN be resumed is decided in one place: _launch_batch()
    returns False for kinds that need per-call args the BatchJob row doesn't
    carry (a needle, an after_chapter, a chapter list) and doesn't have a
    usable args_json to reconstruct them from."""
    from database import SessionLocal
    from models import BatchJob
    db = SessionLocal()
    try:
        stale = db.query(BatchJob).filter(BatchJob.running == True).all()
        pending = [(j.novel_id, j.kind, j.args_json or "") for j in stale]
        for job in stale:
            job.running = False
        db.commit()
    finally:
        db.close()
    for novel_id, kind, args_json in pending:
        if _launch_batch(novel_id, kind, args_json):
            logger.info(f"Resuming interrupted {kind} job for novel {novel_id}")
        else:
            logger.info(f"Not resuming {kind} for novel {novel_id} (needs per-call args) — marked finished")


JOB_STALL_MINUTES = 10  # no bump for this long = the worker is hung or died silently


def _watchdog_loop():
    """Reliability: periodically free jobs whose worker has gone silent.
    A healthy job bumps updated_at every chapter (~1-2 min). If a job shows
    running=True but hasn't been updated for JOB_STALL_MINUTES, the worker is
    hung (relay stall) or died without marking — mark it failed so the
    duplicate-guard stops blocking re-launches."""
    import time as _time
    from datetime import datetime, timedelta
    while True:
        try:
            _watchdog_pass()
        except Exception as e:
            logger.warning(f"watchdog error: {e}")
        _time.sleep(300)  # every 5 min


def _watchdog_pass():
    from database import SessionLocal
    from models import BatchJob
    from datetime import datetime, timedelta
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
                b = _batch_cache.get(job.novel_id)
                if b:
                    b["running"] = False
    finally:
        db.close()


def _retry_failed_bg(novel_id: int):
    """Retry chapters whose last_error is set (translation failures)."""
    from database import SessionLocal
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
        if not _set_batch(novel_id, "retry-failed", len(failed)):
            return
        done = 0
        outcome = None
        for ch in failed:
            if _batch_stop_requested(novel_id):
                logger.info(f"retry-failed stopped by user (novel {novel_id})")
                outcome = _stopped_label(done, len(failed), "chapters retried")
                break
            try:
                if not ch.original_content:
                    ch_data = _fetch_chapter_content_sync(ch.source_url)
                    if ch_data and ch_data.content:
                        ch.original_content = ch_data.content
                        ch.word_count = getattr(ch_data, "word_count", None)
                        db.commit()
                    db.refresh(ch)
                if ch.original_content:
                    _translate_chapter_bg(novel_id, ch.chapter_number, "balanced")
                    db.refresh(ch)
                    if ch.is_translated:
                        ch.last_error = ""
                        done += 1
                        db.commit()
                _bump_batch(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
            except RelayAuthError as e:
                logger.error(f"retry-failed stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(failed), "chapters retried")
                break
            except Exception as e:
                logger.warning(f"retry-failed ch{ch.chapter_number}: {e}")
                db.rollback()
        _finish_batch(novel_id, outcome or f"Retried {done}/{len(failed)} failed chapters")
    finally:
        db.close()


def _launch_batch(novel_id, kind, args_json="") -> bool:
    """Launch the background function for a job kind (used by restart resume).

    Returns True if a worker was started. Most kinds need nothing beyond
    novel_id. Three need a per-call argument the other BatchJob columns don't
    carry — translate-ahead (after_chapter/count), match (the needle),
    retranslate-drift (the chapter list) — which _set_batch() now persists as
    JSON on the row precisely so a restart can reconstruct the call instead of
    just cleaning up after it. `args_json` is that persisted value; if it's
    missing or doesn't carry what the kind needs (an old row from before this
    existed, or a genuinely malformed one), the kind is NOT resumed — silently
    guessing at a needle or chapter list would be worse than dropping the job.
    This map is the single source of truth for resumability —
    _resume_interrupted_jobs asks it rather than keeping a second list that
    can drift out of sync (that drift is how "meta" jobs ended up neither
    resumed nor cleared)."""
    import threading
    args = {}
    if args_json:
        try:
            args = json.loads(args_json)
        except Exception:
            args = {}
    targets = {
        "to-end": lambda: translate_to_end_bg(novel_id),
        "retranslate": lambda: _retranslate_bg(novel_id),
        "titles": lambda: translate_titles_bg(novel_id),
        "updates": lambda: check_updates_bg(novel_id),
        "retry-failed": lambda: _retry_failed_bg(novel_id),
        "epub": lambda: _export_epub_bg(novel_id),
        "meta": lambda: translate_novel_meta_bg(novel_id),
    }
    if kind == "match":
        needle = args.get("needle")
        if needle:
            targets["match"] = lambda: retranslate_match_bg(novel_id, needle)
    elif kind == "retranslate-drift":
        chapter_numbers = args.get("chapter_numbers")
        if chapter_numbers:
            targets["retranslate-drift"] = lambda: _retranslate_drift_bg(novel_id, chapter_numbers)
    elif kind == "translate-ahead":
        after_chapter = args.get("after_chapter")
        if after_chapter is not None:
            count = args.get("count", 5)
            targets["translate-ahead"] = lambda: translate_ahead_bg(novel_id, after_chapter, count)
    fn = targets.get(kind)
    if fn is None:
        return False
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return True


# ============================ BACKUPS (feature 2) ============================
def _backup_dir() -> str:
    """Directory where DB backups live (next to the DB)."""
    import os as _os
    db_path = _os.getenv("DATABASE_URL", "sqlite:///./novel_reader.db").replace("sqlite:///", "")
    d = _os.path.join(_os.path.dirname(_os.path.abspath(db_path)), "backups")
    _os.makedirs(d, exist_ok=True)
    return d


def run_backup(now=None) -> dict:
    """Copy the SQLite DB to backups/. Uses SQLite's online backup API so the copy
    is consistent even while the app is writing. Prunes old backups (keep N)."""
    import os as _os, shutil, glob
    from datetime import datetime as _dt
    now = now or _dt.utcnow()
    db_path = _os.getenv("DATABASE_URL", "sqlite:///./novel_reader.db").replace("sqlite:///", "")
    if not _os.path.exists(db_path):
        return {"status": "error", "message": "DB file not found"}
    bdir = _backup_dir()
    fname = f"novel_reader-{now.strftime('%Y%m%d-%H%M%S')}.db"
    dest = _os.path.join(bdir, fname)
    # Consistent copy via SQLite backup API
    try:
        from sqlalchemy import create_engine as _ce
        src_engine = _ce(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        dst_engine = _ce(f"sqlite:///{dest}")
        with src_engine.connect() as src, dst_engine.connect() as dst:
            src.connection.connection.backup(dst.connection.connection)
        src_engine.dispose(); dst_engine.dispose()
    except Exception as e:
        # Fallback: plain copy. Weaker consistency guarantee than the backup
        # API (a concurrent writer could produce a torn copy) — log so this
        # degraded path is visible instead of every backup silently using it.
        logger.warning(f"run_backup: SQLite backup API failed, using plain copy instead: {e}")
        shutil.copy2(db_path, dest)
    # Prune old backups
    keep = _get_config().get("backup_keep", 14)
    backups = sorted(glob.glob(_os.path.join(bdir, "novel_reader-*.db")))
    for old in backups[:-keep]:
        try:
            _os.remove(old)
        except Exception as e:
            logger.warning(f"run_backup: could not remove old backup {old}: {e}")
    return {"status": "ok", "file": fname, "size": _os.path.getsize(dest)}


def _get_config():
    """AppConfig singleton row as a dict (seeded from env on first use)."""
    from database import SessionLocal
    from models import AppConfig
    db = SessionLocal()
    try:
        cfg = db.query(AppConfig).filter(AppConfig.id == 1).first()
        if not cfg:
            cfg = AppConfig(id=1)
            cfg.gemini_api_key = os.getenv("GEMINI_API_KEY", "")
            cfg.fallback_api_key = os.getenv("FALLBACK_API_KEY", "")
            cfg.fallback_base_url = os.getenv("FALLBACK_BASE_URL", "https://opencode.ai/zen/go/v1")
            cfg.fallback_model = os.getenv("FALLBACK_MODEL", "deepseek-v4-flash")
            cfg.fallback_model_2 = os.getenv("FALLBACK_MODEL_2", "gpt-5.6-luna")
            cfg.fallback_2_base_url = os.getenv("FALLBACK_2_BASE_URL", "")
            cfg.fallback_2_api_key = os.getenv("FALLBACK_2_API_KEY", "")
            db.add(cfg)
            db.commit()
        return {
            "gemini_api_key": cfg.gemini_api_key or "",
            "fallback_api_key": cfg.fallback_api_key or "",
            "fallback_base_url": cfg.fallback_base_url or "",
            "fallback_model": cfg.fallback_model or "",
            "fallback_model_2": cfg.fallback_model_2 or "",
            "fallback_2_base_url": cfg.fallback_2_base_url or "",
            "fallback_2_api_key": cfg.fallback_2_api_key or "",
            "backup_enabled": bool(cfg.backup_enabled),
            "backup_interval_hours": cfg.backup_interval_hours or 24,
            "backup_keep": cfg.backup_keep or 14,
            "auth_password": cfg.auth_password or "",
        }
    finally:
        db.close()


# config field -> environment variable that actually powers the translator
_CONFIG_ENV = (
    ("gemini_api_key", "GEMINI_API_KEY"),
    ("fallback_api_key", "FALLBACK_API_KEY"),
    ("fallback_base_url", "FALLBACK_BASE_URL"),
    ("fallback_model", "FALLBACK_MODEL"),
    ("fallback_model_2", "FALLBACK_MODEL_2"),
    ("fallback_2_base_url", "FALLBACK_2_BASE_URL"),
    ("fallback_2_api_key", "FALLBACK_2_API_KEY"),
)


def _apply_config_to_env(cleared=None):
    """Push DB config into os.environ so get_translator() picks it up, and reset
    the translator singleton so the next call rebuilds with the new keys.

    `cleared` names the config fields the caller just BLANKED; their env vars are
    removed. Without this, clearing or rotating a key in Settings left the old
    (possibly revoked) credential live in os.environ until the process
    restarted, and the translator kept using it.

    A field that is merely empty is deliberately left alone: a key supplied only
    via .env and never saved in Settings must keep working (this is why
    _get_config()/get_config() also fall back to the environment).
    """
    cfg = _get_config()
    cleared = set(cleared or ())
    for field, env_name in _CONFIG_ENV:
        val = (cfg.get(field) or "").strip()
        if val:
            os.environ[env_name] = val
        elif field in cleared:
            os.environ.pop(env_name, None)
    import translator as _tr
    _tr._translator_instance = None


@app.get("/api/config")
async def get_config():
    """App config with secrets masked (show only last 4 chars)."""
    cfg = _get_config()
    import os as _os
    masked = {}
    for k in ("gemini_api_key", "fallback_api_key", "auth_password", "fallback_2_api_key"):
        v = cfg.get(k, "")
        masked[k] = (v[-4:] if len(v) >= 4 else "") if v else ""
        # A key "counts as set" if it's in the DB OR actually available in the
        # environment (env is what powers the translator). Otherwise the Settings
        # page would claim "Not set" while the key from .env works fine.
        if k == "fallback_api_key" and not v:
            v = _os.getenv("FALLBACK_API_KEY", "")
        masked[k + "_set"] = bool(v)
    # strip the raw secret values, keep the masked copies
    out = {k: v for k, v in cfg.items() if k not in ("gemini_api_key", "fallback_api_key", "auth_password", "fallback_2_api_key")}
    return {**out, **masked}


@app.put("/api/config")
async def put_config(payload: dict, background_tasks: BackgroundTasks = None,
                     db: Session = Depends(get_db_session)):
    """Update config: only non-empty values replace; empty strings keep the old value
    (so a masked field doesn't wipe a key). To clear a key, send "__clear": true."""
    from models import AppConfig
    cfg = db.query(AppConfig).filter(AppConfig.id == 1).first()
    if not cfg:
        cfg = AppConfig(id=1)
        db.add(cfg)
    fields = ["gemini_api_key", "fallback_api_key", "fallback_base_url",
              "fallback_model", "fallback_model_2", "auth_password",
              "fallback_2_base_url", "fallback_2_api_key"]
    password_changed = False
    cleared = set()
    for f in fields:
        if f in payload or payload.get(f + "__clear"):
            v = (payload.get(f) or "").strip()
            if payload.get(f + "__clear"):
                if f == "auth_password":
                    password_changed = True
                cleared.add(f)
                setattr(cfg, f, "")
            elif v and v != getattr(cfg, f):
                if f == "auth_password":
                    password_changed = True
                # Guard against the masked-fragment clobber: the config page
                # shows only the last 4 chars of every secret; if that fragment
                # comes back as a "new value" (short, and equal to the tail of
                # the stored one), keep the real secret instead of overwriting
                # it with garbage. auth_password is included deliberately — a
                # truncated password locks the user out of their own library.
                if (f.endswith("_api_key") or f == "auth_password") and len(v) < 8:
                    stored = getattr(cfg, f) or ""
                    if stored and stored.endswith(v):
                        continue  # masked fragment echo — ignore
                setattr(cfg, f, v)
    if "backup_enabled" in payload:
        cfg.backup_enabled = bool(payload["backup_enabled"])
    if "backup_interval_hours" in payload:
        cfg.backup_interval_hours = int(payload["backup_interval_hours"])
    if "backup_keep" in payload:
        cfg.backup_keep = int(payload["backup_keep"])
    db.commit()
    # Password set/changed/removed → rotate the cookie secret so every previously
    # issued session token is revoked (a stolen cookie can't keep working).
    if password_changed:
        _rotate_session_secret()
    # Pass `cleared` so a key the user just wiped is also removed from
    # os.environ — otherwise the revoked credential stays live until restart.
    _apply_config_to_env(cleared)
    # Refresh the cached health status too — the frontend's own pre-save check
    # already validated THIS payload, but a save can also come from anywhere
    # that skips that check, and this keeps /api/config/health-status honest
    # without making the save itself wait on a relay round-trip.
    if background_tasks is not None:
        background_tasks.add_task(_check_relay_health_bg)
    return {"status": "ok"}


@app.post("/api/config/health-check")
async def config_health_check(payload: dict = None):
    """Verify relay credentials before saving (Settings save-time check).

    Step 1 — key + base URL: GET {base}/models with the key.
    Step 2 — model names: each must appear in the returned model list.
    Step 3 — test query: at least ONE real chat round-trip ('hi') must be
    answered, proving the model actually produces output (a name on the
    /models list does not prove it can answer).

    Returns per-field validity so the frontend can save the good parts and
    clear only a bad model name (never the whole config). chat_ok=false
    blocks the save entirely — a config whose AI never answers is not
    worth saving.
    """
    import os as _os
    import urllib.request, json as _json, urllib.error
    # urlopen blocks; run OFF the event loop so Settings saves never freeze
    # the whole app while the relay is slow (25s timeout × 2 endpoints).
    p = await asyncio.to_thread(_config_health_check_sync, payload or {})
    return p


@app.get("/api/config/health-status")
async def config_health_status():
    """Last PROACTIVE relay/model health check — run automatically on startup
    and after every config save (see _check_relay_health_bg), not triggered by
    this request. {} until the first one has run. Lets the Settings page warn
    about a bad model without the user having to click Save to find out."""
    return _relay_health_cache or {"checked_at": None}


def _chat_completions_sync(base: str, key: str, model: str, timeout: int = 25) -> str:
    """Send one tiny chat message ('hi') and return the reply content.

    The /models list proves a name EXISTS and the key can LIST; it proves
    nothing about whether the model actually ANSWERS (wrong key scope,
    exhausted quota, retired-but-aliased model, relay routing bug). Settings
    save-time validation therefore requires at least one real chat round-trip.
    """
    import urllib.request as _ur, json as _json
    url = f"{base}/chat/completions"
    payload = _json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 64,
        "temperature": 0,
    }).encode()
    req = _ur.Request(url, data=payload, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://hermes-agent.nousresearch.com",
        "X-Title": "Hermes Agent",
        "User-Agent": "HermesAgent/3.1.0",
    })
    with _ur.urlopen(req, timeout=timeout) as resp:
        data = _json.loads(resp.read().decode())
    content = ""
    try:
        content = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        content = ""
    return content.strip()


def _config_health_check_sync(p: dict) -> dict:
    """Blocking implementation of the config health check (call via to_thread)."""
    import os as _os
    import urllib.request, json as _json, urllib.error
    
    # Test primary relay
    key = (p.get("api_key") or _os.getenv("FALLBACK_API_KEY", "")).strip()
    base = (p.get("base_url") or _os.getenv("FALLBACK_BASE_URL", "https://opencode.ai/zen/go/v1")).strip().rstrip("/")
    # Fall back to env ONLY when the caller omits the key entirely — a caller
    # that explicitly sends "" (Settings' own save-time check, when the user
    # cleared the field on purpose) must still test that literal blank, not a
    # stale env value. The proactive startup/post-save check calls this with
    # p={} to test whatever is ACTUALLY configured; without this fallback it
    # silently checked "" for both, which `valid()` below always accepts,
    # making the model check a no-op for that caller.
    model1 = (p["model"] if "model" in p else _os.getenv("FALLBACK_MODEL", "")).strip()
    model2 = (p["model_2"] if "model_2" in p else _os.getenv("FALLBACK_MODEL_2", "")).strip()
    
    key_ok = True
    available = []
    url = f"{base}/models"
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://hermes-agent.nousresearch.com",
            "X-Title": "Hermes Agent",
            "User-Agent": "HermesAgent/3.1.0",
        })
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = _json.loads(resp.read().decode())
        lst = data if isinstance(data, list) else data.get("data", [])
        available = [m.get("id") if isinstance(m, dict) else str(m) for m in lst]
    except Exception as e:
        key_ok = False
        return {"key_ok": False, "message": f"Could not reach relay at {base}: {e}",
                "models": {"model": False, "model_2": False}}

    def valid(name):
        if not name:
            return True  # blank tier 2 is allowed; blank model1 handled by caller
        n = name.strip().lower()
        return any(a.lower() == n for a in available) or any(n in a.lower() for a in available)

    m1_ok = valid(model1)
    m2_ok = valid(model2) if model2 else True
    message = "OK"
    if not m1_ok:
        message = f"Model '{model1}' not found on relay (cleared)"
    elif not m2_ok:
        message = f"Model 2 '{model2}' not found on relay (cleared)"

    # Real chat round-trip: at least ONE successful 'hi' query must be
    # answered before the config is accepted. A model name on the /models
    # list does not prove it answers (wrong key scope, quota, retired
    # alias, relay bug) — the user's requirement. Model 1 is mandatory;
    # Model 2 (own relay) is tested here too.
    chat_model = True
    chat_msg = "OK"
    chat_suggested = None
    if model1 and m1_ok:
        try:
            reply = _chat_completions_sync(base, key, model1)
            chat_model = bool(reply)
            if not chat_model:
                chat_msg = f"Model '{model1}' answered with empty content — check key/quota"
        except Exception as e:
            chat_model = False
            chat_msg = f"Model '{model1}' failed the test query: {e}"
            # Relay model ids are CASE-SENSITIVE (mixed-case 'MiMo-V2.5' is
            # a 401 ModelError even though the /models list matched it
            # case-insensitively). If the typed name is a case-only variant
            # of a real id, say exactly which id to use.
            sugg = next((a for a in available if a.lower() == model1.lower()), None)
            if sugg and sugg != model1:
                chat_suggested = sugg
                chat_msg += f" Did you mean '{sugg}'? Model ids are case-sensitive."
    elif not model1:
        chat_msg = "Model 1 not configured — no test query run"

    # Model 2's own relay (same value as `model2` above)
    m2_base = (p.get("fallback_2_base_url") or _os.getenv("FALLBACK_2_BASE_URL", "")).strip().rstrip("/")
    m2_key = (p.get("fallback_2_api_key") or _os.getenv("FALLBACK_2_API_KEY", "")).strip()
    m2_model = model2

    chat_model_2 = True
    chat_msg_2 = "OK"
    chat_suggested_2 = None
    fallback2_result = {}
    m2_separate = m2_base and m2_key and m2_base != base
    available2 = []
    if m2_separate:
        # Fetch Model 2's separate relay list first (needed for the name
        # check AND for case-sensitive suggestions on the chat test).
        url2 = f"{m2_base}/models"
        try:
            req = urllib.request.Request(url2, headers={
                "Authorization": f"Bearer {m2_key}",
                "HTTP-Referer": "https://hermes-agent.nousresearch.com",
                "X-Title": "Hermes Agent",
                "User-Agent": "HermesAgent/3.1.0",
            })
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = _json.loads(resp.read().decode())
            lst = data if isinstance(data, list) else data.get("data", [])
            available2 = [m.get("id") if isinstance(m, dict) else str(m) for m in lst]
        except Exception as e:
            fallback2_result = {"key_ok": False, "message": f"Could not reach Model 2 relay at {m2_base}: {e}", "models": {"model": False}}
            key_ok = False
        else:
            def valid2(name):
                if not name:
                    return True
                n = name.strip().lower()
                return any(a.lower() == n for a in available2) or any(n in a.lower() for a in available2)
            m2_model_ok = valid2(m2_model)
            if not m2_model_ok:
                fallback2_result = {"key_ok": True, "message": f"Model 2 '{m2_model}' not found on its relay (cleared)", "models": {"model": False}, "chat_ok": False, "chat_message": f"Model 2 '{m2_model}' not found on its relay"}
            else:
                fallback2_result = {"key_ok": True, "models": {"model": m2_model_ok}, "available_count": len(available2), "message": "OK", "chat_ok": chat_model_2, "chat_message": chat_msg_2}
    if m2_separate and m2_model and fallback2_result.get("models", {}).get("model"):
        # Real chat round-trip for Model 2's own relay — same case-sensitivity
        # handling as Model 1.
        try:
            reply = _chat_completions_sync(m2_base, m2_key, m2_model)
            chat_model_2 = bool(reply)
            if not chat_model_2:
                chat_msg_2 = f"Model 2 '{m2_model}' answered with empty content — check key/quota"
        except Exception as e:
            chat_model_2 = False
            chat_msg_2 = f"Model 2 '{m2_model}' failed the test query: {e}"
            sugg2 = next((a for a in available2 if a.lower() == m2_model.lower()), None)
            if sugg2 and sugg2 != m2_model:
                chat_suggested_2 = sugg2
                chat_msg_2 += f" Did you mean '{sugg2}'? Model ids are case-sensitive."
        fallback2_result["chat_ok"] = chat_model_2
        fallback2_result["chat_message"] = chat_msg_2
        if chat_suggested_2:
            fallback2_result["chat_suggested"] = chat_suggested_2

    return {
        "key_ok": key_ok,
        "models": {"model": m1_ok, "model_2": m2_ok},
        "chat_ok": chat_model,
        "chat_message": chat_msg,
        "chat_suggested": chat_suggested,
        "available_count": len(available),
        "message": message,
        "fallback_2": fallback2_result if fallback2_result else None
    }


@app.post("/api/backup")
async def backup_now():
    """Manual backup trigger."""
    return run_backup()


@app.get("/api/backups")
async def list_backups():
    """List existing DB backups (name, size, date)."""
    import os as _os, glob
    from datetime import datetime as _dt
    bdir = _backup_dir()
    out = []
    for f in sorted(glob.glob(_os.path.join(bdir, "novel_reader-*.db")), reverse=True):
        st = _os.stat(f)
        out.append({
            "name": _os.path.basename(f),
            "size": st.st_size,
            "date": _dt.utcfromtimestamp(st.st_mtime).isoformat(),
        })
    return out


@app.get("/api/backups/{name}/download")
async def download_backup(name: str):
    """Download a backup file."""
    import os as _os
    safe = _os.path.basename(name)
    if not safe or safe in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid backup name")
    path = _os.path.join(_backup_dir(), safe)
    if not _os.path.exists(path):
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(path, filename=safe)


@app.post("/api/backups/restore")
async def restore_backup(file: UploadFile):
    """Restore the library from an uploaded backup .db file.

    Replaces the live SQLite DB with the uploaded snapshot. Because the app
    holds the DB open, we write the uploaded data into a staging file, stop
    writes, swap it into place, and reload. Returns the new DB size.
    """
    import os as _os
    if not file.filename or not file.filename.endswith(".db"):
        raise HTTPException(status_code=400, detail="Upload a .db backup file")
    db_path = _os.getenv("DATABASE_URL", "sqlite:///./novel_reader.db").replace("sqlite:///", "")
    if not _os.path.exists(db_path):
        raise HTTPException(status_code=500, detail="DB file not found")
    # Staging file: write upload fully, then copy over the live DB (copy2 is
    # atomic-ish on same filesystem; SQLite handles the swap on next open).
    staging = db_path + ".restore-staging"
    try:
        with open(staging, "wb") as out:
            while chunk := await file.read(1 << 20):
                out.write(chunk)
        # Sanity: must be a valid SQLite DB before we destroy the live one.
        # `probe` must be closed BEFORE removing its own file: on Windows an
        # open connection blocks deleting the file it points at (WinError 32),
        # so closing only on the success path left the delete-on-failure below
        # raising a PermissionError that escaped as an unrelated 500 instead
        # of the intended 400 "not a valid SQLite database".
        import sqlite3 as _sqlite
        probe = _sqlite.connect(staging)
        try:
            probe.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception:
            probe.close()
            _os.remove(staging)
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid SQLite database")
        else:
            probe.close()
        import sqlite3 as _sqlite2
        import shutil as _shutil
        # Swap the restored file in using SQLite's own backup API against a
        # fresh connection to the staging file — copying bytes over the live
        # DB while pooled connections hold it open (and WAL sidecars exist)
        # can tear the database or resurrect stale WAL pages.
        _shutil.copy2(staging, db_path + ".restored-new")
        # DIRECTION MATTERS: sqlite3's Connection.backup(target) copies SELF
        # INTO target. The restored snapshot is the SOURCE and the live DB is
        # the TARGET — reversing these silently copies the live DB over the
        # upload and reports success while restoring nothing.
        live = _sqlite2.connect(db_path)
        try:
            live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            restored = _sqlite2.connect(db_path + ".restored-new")
            try:
                restored.backup(live)
            finally:
                restored.close()
        finally:
            live.close()
        import os as _os2
        try:
            _os2.remove(db_path + ".restored-new")
        except OSError as e:
            # Left behind, this is a full DB-sized temp file — silent failures
            # here accumulate real disk usage across repeated restores.
            logger.warning(f"restore: could not remove {db_path}.restored-new: {e}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Restore failed: {e}")
    finally:
        if _os.path.exists(staging):
            try:
                _os.remove(staging)
            except OSError as e:
                logger.warning(f"restore: could not remove staging file {staging}: {e}")
    size = _os.path.getsize(db_path)
    # Wipe in-memory batch cache so it doesn't reference now-gone rows, and drop
    # every pooled connection so later requests read the restored file.
    _batch_cache.clear()
    try:
        from database import engine as _engine
        _engine.dispose()
    except Exception as e:
        logger.warning(f"could not dispose engine after restore: {e}")
    return {"status": "ok", "size": size, "message": "Restored — reload the page"}


@app.delete("/api/backups/{name}")
async def delete_backup(name: str):
    """Delete a single backup file (manual cleanup — the auto-prune only keeps
    the newest `backup_keep`, so this lets you drop old ones by hand)."""
    import os as _os
    safe = _os.path.basename(name)
    if safe in (".", "..") or not safe:
        raise HTTPException(status_code=400, detail="Invalid backup name")
    path = _os.path.join(_backup_dir(), safe)
    if not _os.path.exists(path) or not _os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Backup not found")
    try:
        _os.remove(path)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not delete backup: {e}")
    return {"status": "ok"}


def _backup_scheduler_loop():
    """Background thread: run a backup when due (checks hourly)."""
    import time as _time
    while True:
        try:
            cfg = _get_config()
            if cfg.get("backup_enabled"):
                bdir = _backup_dir()
                import glob, os as _os
                existing = glob.glob(_os.path.join(bdir, "novel_reader-*.db"))
                due = True
                if existing:
                    newest = max(_os.path.getmtime(f) for f in existing)
                    from datetime import datetime as _dt
                    age_h = (_time.time() - newest) / 3600
                    due = age_h >= cfg.get("backup_interval_hours", 24)
                if due:
                    run_backup()
                    logger.info("Scheduled backup completed")
        except Exception as e:
            logger.warning(f"backup scheduler error: {e}")
        _time.sleep(3600)  # check every hour


@app.on_event("startup")
async def _startup_reliability():
    """Startup: resume interrupted jobs + start backup scheduler + watchdog threads.

    Config application + job resume touch the DB/files synchronously — run them
    in a worker thread so a slow disk or a stale DB never delays (or blocks)
    the event loop during startup."""
    try:
        await asyncio.to_thread(_startup_reliability_sync)
    except Exception as e:
        logger.warning(f"startup reliability failed: {e}")
    import threading
    t = threading.Thread(target=_backup_scheduler_loop, daemon=True)
    t.start()
    w = threading.Thread(target=_watchdog_loop, daemon=True)
    w.start()


def _startup_reliability_sync():
    _apply_config_to_env()
    _resume_interrupted_jobs()
    _check_relay_health_bg()


# Cache of the last background relay/model health check — {} until the first
# one runs. Exposed via GET /api/config/health-status so the Settings page can
# show a warning without the user having to click "Save" to discover a model
# the relay silently deprecated out from under an already-saved config.
_relay_health_cache: dict = {}


def _check_relay_health_bg():
    """Run the same check config_health_check does, against whatever config is
    ACTUALLY effective right now (env-sourced, since payload={}) — proactively,
    instead of only when a user happens to open Settings and click Save. Called
    on startup and after every config save, so drift (a model renamed/retired on
    the relay's side, or a bad value that reached os.environ some other way than
    the Settings save-time check) surfaces on its own instead of silently
    breaking every translation until someone notices and investigates."""
    from datetime import datetime as _dt
    try:
        result = _config_health_check_sync({})
    except Exception as e:
        logger.warning(f"relay health check itself failed: {e}")
        return
    result["checked_at"] = _dt.utcnow().isoformat()
    _relay_health_cache.clear()
    _relay_health_cache.update(result)
    if not result.get("key_ok"):
        logger.warning(f"relay health check: {result.get('message')}")
    elif not all(result.get("models", {}).values()):
        logger.warning(f"relay health check: {result.get('message')}")
    elif result.get("chat_ok") is False:
        logger.warning(f"relay health check: {result.get('chat_message')}")
    else:
        logger.info("relay health check: OK")


def _fetch_chapter_content_sync(source_url: str, polite_delay: bool = True):
    """Fetch one chapter's content synchronously (used by background jobs)."""
    from scrapers import get_scraper_for_url
    import asyncio as _asyncio
    scraper = get_scraper_for_url(source_url)
    if not scraper:
        return None

    async def _fetch():
        async with scraper:
            return await scraper.get_chapter_content(source_url)

    try:
        result = _asyncio.run(_fetch())
        # Politeness delay AFTER the fetch so consecutive batches don't hammer the site
        if polite_delay:
            _sleep_between_fetches()
        return result
    except Exception as e:
        logger.warning(f"fetch {source_url} failed: {e}")
        return None


def translate_ahead_bg(novel_id: int, after_chapter: int, count: int = 5):
    """Background: fetch+translate the next `count` RAW chapters, but ONLY if they
    form a contiguous run right after `after_chapter`. If the immediately-next
    chapter is already translated (a gap), do nothing — don't jump ahead across
    translated chapters (that used to fetch Ch 23+ right after reading Ch 1)."""
    from database import SessionLocal
    from scrapers import get_scraper_for_url
    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel:
            return
        # Contiguous raw run: chapters strictly after the current one, in order,
        # stopping at the first that is already translated or missing.
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
                break                        # gap in numbering — stop
            if ch.is_translated:
                break                        # next chapter already ready — stop (the key fix)
            taken.append(ch)
            run.append(chnum + 1)
        if not taken:
            return
        next_chs = taken
        if not _set_batch(novel_id, "translate-ahead", len(next_chs),
                         args={"after_chapter": after_chapter, "count": count}):
            # Another batch job (any kind) already owns this novel — don't
            # fight over the progress counters (that corrupted done>total and
            # left the frontend spinner spinning forever).
            return
        done = 0
        outcome = None
        for ch in next_chs:
            if _batch_stop_requested(novel_id):
                logger.info(f"translate-ahead stopped by user (novel {novel_id}, after ch{ch.chapter_number})")
                outcome = _stopped_label(done, len(next_chs), "chapters prepared")
                break
            try:
                # Fetch original content first if missing
                if not ch.original_content:
                    ch_data = _fetch_chapter_content_sync(ch.source_url)
                    if ch_data and ch_data.content:
                        ch.original_content = ch_data.content
                        ch.word_count = ch_data.word_count
                        db.commit()
                if not ch.original_content:
                    raise RuntimeError("fetch failed — no content")
                # Stop may have been requested during the fetch — don't start
                # a long translation if the user already asked to stop.
                if _batch_stop_requested(novel_id):
                    logger.info(f"translate-ahead stopped mid-fetch (novel {novel_id})")
                    outcome = _stopped_label(done, len(next_chs), "chapters prepared")
                    break
                _translate_chapter_bg(novel_id, ch.chapter_number, "balanced")
                # As in translate_to_end_bg: an ordinary per-chapter failure
                # is recorded on ch.last_error and returns normally rather
                # than raising, so check the real outcome before counting it.
                db.refresh(ch)
                if ch.is_translated:
                    done += 1
                _bump_batch(novel_id, label=f"Ch {ch.chapter_number} {ch.title or ''}")
            except RelayAuthError as e:
                logger.error(f"translate-ahead stopped: relay key rejected ({e})")
                outcome = _auth_rejected_label(done, len(next_chs), "chapters prepared")
                break
            except Exception as e:
                logger.warning(f"translate-ahead ch{ch.chapter_number} failed: {e}")
        _finish_batch(novel_id, outcome or f"Prepared {done}/{len(next_chs)} chapters ahead")
    finally:
        db.close()


@app.post("/api/novels/{novel_id}/translate-ahead")
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
    async with _async_novel_lock(novel_id):
        if _batch_running(novel_id):
            return {"status": "already_running", "pending": 0}
        background_tasks.add_task(translate_ahead_bg, novel_id, after_chapter, count)
    return {"status": "started", "pending": min(existing, count)}


async def fetch_chapters_range(novel_id: int, start: int, count: int, do_translate: bool):
    """Background task: fetch + optionally translate chapters [start, start+count)."""
    from database import SessionLocal
    from translator import get_translator
    from scrapers import get_scraper_for_url

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

        translator = get_translator() if do_translate else None

        # One scraper session for the whole batch (uses rate limiting internally)
        from scrapers import get_scraper_for_url as _gsfu
        scraper = None
        first_url = chapters[0].source_url if chapters else None
        if first_url:
            scraper = _gsfu(first_url)

        if scraper:
            await scraper.__aenter__()

        try:
            for chapter in chapters:
                # Skip chapters already populated
                if chapter.original_content:
                    continue

                if not scraper:
                    continue

                try:
                    ch_data = await scraper.get_chapter_content(chapter.source_url)
                    if not (ch_data and ch_data.content):
                        # Still wait between attempts to stay polite to the source
                        await asyncio.sleep(FETCH_DELAY_SECONDS)
                        continue

                    chapter.original_content = ch_data.content
                    chapter.word_count = ch_data.word_count

                    # Politeness delay after each fetch (rate-limit protection)
                    await asyncio.sleep(FETCH_DELAY_SECONDS)

                    if translator:
                        # Run the blocking relay call OFF the event loop — a single
                        # chapter can take minutes (180s × 2 retries); running it
                        # inline here would freeze every other request/task.
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
                    # Closing an HTTP session that's already done being used
                    # has no user-visible consequence either way — the batch's
                    # own per-chapter try/except already reported anything
                    # that mattered.
                    pass
    finally:
        db.close()


@app.get("/novel/{novel_id}/review", response_class=HTMLResponse)
async def novel_review_page(novel_id: int, db: Session = Depends(get_db_session)):
    """Story-so-far review page: AI memory (characters, plot, arcs) + user diary."""
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        return _page("Not found", '<p>Novel not found. <a href="/">← Back</a></p>')
    mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
    memory_data = {}
    if mem:
        # glossary_entries is stored double-encoded (JSON string inside JSON column) —
        # parse like the API does, otherwise Vue iterates the raw string char-by-char.
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


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _page("Login", '<div id="login-app"></div>', page_js="login.js")


@app.get("/config", response_class=HTMLResponse)
async def config_page():
    """Settings page: API keys, fallback models, backup preferences."""
    return _page("Settings",
                 '<div id="config-app"></div>',
                 page_js="config.js",
                 data_js="window.__CONFIG__ = true;")


@app.get("/api/stats")
async def get_stats(db: Session = Depends(get_db_session)):
    """Get library statistics (dashboard data)."""
    total_novels = db.query(Novel).count()
    total_chapters = db.query(Chapter).count()
    translated_chapters = db.query(Chapter).filter(Chapter.is_translated == True).count()
    read_chapters = db.query(Chapter).filter(Chapter.is_read == True).count()
    bookmarks = db.query(Bookmark).count()
    diary_entries = db.query(DiaryEntry).count()
    # novels per shelf
    shelves = {}
    for n in db.query(Novel.reading_status).all():
        k = n[0] or "ongoing"
        shelves[k] = shelves.get(k, 0) + 1
    # recent activity: last 6 read chapters (novel title + chapter)
    recent = []
    for ch in (db.query(Chapter, Novel.title_translated, Novel.title)
               .join(Novel, Chapter.novel_id == Novel.id)
               .filter(Chapter.read_at.isnot(None))
               .order_by(Chapter.read_at.desc()).limit(6).all()):
        recent.append({
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


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    """Reading dashboard: stats, shelves, recent activity."""
    return _page("Dashboard", '<div id="dashboard-app"></div>', page_js="dashboard.js")


# ============================================================================
# Pages — Vue-powered multi-page UI (library / novel / reader).
# Each page is a full server-rendered HTML document that bootstraps a small
# Vue app with initial data via window.__X__. No client-side routing.
# ============================================================================


def _asset_stamp() -> str:
    """Cache-busting stamp for static assets.

    Returns a short hash of the frontend shell files' mtimes. Any frontend
    edit (styles.css, *.js) changes the hash, so every rendered page references
    assets as /static/x.css?v=<stamp>. The service worker matches its cache by
    FULL url, so a changed stamp makes the old SW-cached copy miss and fetch
    fresh from network — this is what makes the manual "bump nyaa-reader-vN in
    sw.js" step obsolete: you cannot forget it, because there is nothing to
    remember.
    """
    try:
        h = hashlib.md5()
        for name in ("styles.css", "lib/text.js", "library.js", "novel.js", "reader.js",
                     "config.js", "dashboard.js", "review.js", "login.js"):
            p = os.path.join(frontend_path, name)
            if os.path.exists(p):
                h.update(str(os.path.getmtime(p)).encode())
        return h.hexdigest()[:10]
    except Exception as e:
        # Falling back to a constant stamp silently disables cache-busting —
        # every page keeps serving whatever a browser already cached, and a
        # future frontend edit stops reaching anyone. Worth knowing about.
        logger.warning(f"_asset_stamp: could not hash frontend files, cache-busting disabled: {e}")
        return "0"


# Loaded once at import; a missing file degrades to an empty sprite instead of
# 500-ing every HTML page.
try:
    with open(os.path.join(frontend_path or ".", "icons.svg"), encoding="utf-8") as _f:
        _icons_sprite_cache = _f.read()
except OSError:
    logger.warning("icons.svg not found — pages will render without the icon sprite")
    _icons_sprite_cache = ""


def _page(title: str, body: str, page_js: Optional[str] = None,
          data_js: Optional[str] = None, refresh: Optional[int] = None) -> HTMLResponse:
    refresh_tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    data_tag = f"<script>{data_js}</script>" if data_js else ""
    stamp = _asset_stamp()
    js_tags = ""
    if page_js:
        js_tags = (
            f'<script src="/static/vendor/vue.global.prod.js?v={stamp}"></script>\n'
            f'<script src="/static/lib/text.js?v={stamp}"></script>\n'
            f'<script src="/static/{page_js}?v={stamp}"></script>'
        )
    icons_sprite = _icons_sprite_cache
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_mod.escape(title)}</title>
{refresh_tag}
<link rel="icon" href="/static/favicon.ico" sizes="any">
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="apple-touch-icon" href="/static/favicon.svg">
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" content="#6d5ae0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="stylesheet" href="/static/styles.css?v={stamp}">
</head>
<body>
{icons_sprite}
{body}
{data_tag}
{js_tags}
<script>
if ('serviceWorker' in navigator) {{
  navigator.serviceWorker.register('/static/sw.js').catch(function () {{}});
}}
</script>
</body>
</html>""")


def _json(data) -> str:
    """JSON for embedding inside <script> tags. Escapes "</" so a scraped
    title like `</script><img onerror=...>` cannot break out of the script
    block (classic JSON-in-HTML XSS)."""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


@app.get("/", response_class=HTMLResponse)
async def library_page(request: Request, db: Session = Depends(get_db_session)):
    novels = db.query(Novel).order_by(Novel.updated_at.desc()).all()
    shelf = request.query_params.get("shelf", "")  # ongoing | read_later | done | dropped
    data = []
    for n in novels:
        translated = db.query(Chapter).filter(
            Chapter.novel_id == n.id, Chapter.is_translated == True).count()
        read_count = db.query(Chapter).filter(
            Chapter.novel_id == n.id, Chapter.is_read == True).count()
        # Last read position for "Continue reading" on cards
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


# The legacy POST-and-redirect "add a novel" HTML-form handler that used to
# live here was removed: unreachable from the current Vue UI (library.js's
# add-novel form uses @submit.prevent and calls POST /api/novels instead), and
# it's the same shared _create_novel_from_url() either way.


@app.get("/novel/{novel_id}", response_class=HTMLResponse)
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


def _get_recap(db, novel_id: int) -> dict:
    """Build the 'Previously on…' recap from AI memory: current arc plot + most
    recent chapter summary. Returns {} when memory is empty (reader hides the card)."""
    mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
    if not mem:
        return {}
    recap = {}
    arc = (mem.arc_plot or "").strip()
    ch = (mem.chapter_plot or "").strip()
    if arc:
        recap["arc"] = arc
    if ch:
        recap["chapter"] = ch
    return recap


@app.get("/novel/{novel_id}/chapter/{chapter_number}", response_class=HTMLResponse)
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

    # Track reading: mark the chapter read
    if not chapter.is_read or chapter.read_at is None:
        chapter.is_read = True
        chapter.read_at = datetime.utcnow()
        db.commit()

    title = chapter.title_translated or chapter.title or f"Chapter {chapter_number}"

    # Chapter list for the reader TOC drawer (number + translated title)
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
        # "Previously on…" recap from AI memory (current arc + recent chapter plot)
        "recap": _get_recap(db, novel_id),
    }
    return _page(f"{title} - {novel.title_translated or novel.title}",
                 '<div id="reader-app"></div>',
                 page_js="reader.js",
                 data_js=f"window.__READER__ = {_json(reader_data)};")


# The legacy POST-and-redirect "translate a chapter" HTML-form handler that
# used to live here was removed: unreachable from the current Vue UI (a
# repo-wide grep found no caller), superseded by the JSON API's own POST
# /api/novels/{novel_id}/chapters/{chapter_number}/translate below.


@app.post("/api/novels/{novel_id}/chapters/{chapter_number}/translate")
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
    background_tasks.add_task(_translate_chapter_bg, novel_id, chapter_number, "balanced")
    return {"status": "started"}


@app.post("/api/novels/{novel_id}/chapters/{chapter_number}/fetch")
async def fetch_chapter_json(novel_id: int, chapter_number: int, force: bool = False,
                             db: Session = Depends(get_db_session)):
    """Fetch one chapter's content synchronously (JSON).

    force=True refetches even when the chapter already has content — the
    escape hatch for a bad/truncated scrape (e.g. an author-note preface
    saved instead of the story). Without force, already-populated chapters
    are left untouched.
    """
    chapter = db.query(Chapter).filter(
        Chapter.novel_id == novel_id,
        Chapter.chapter_number == chapter_number,
    ).first()
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    if not chapter.original_content or force:
        from scrapers import get_scraper_for_url
        scraper = get_scraper_for_url(chapter.source_url)
        if scraper:
            async with scraper:
                ch_data = await scraper.get_chapter_content(chapter.source_url)
                if ch_data and ch_data.content:
                    chapter.original_content = ch_data.content
                    chapter.word_count = ch_data.word_count
                    # A refetch replaces the source text; the old translation
                    # is now stale — mark it untranslated so the reader offers
                    # a fresh translate instead of showing mismatched text.
                    chapter.is_translated = False
                    chapter.translated_content = None
                    chapter.last_error = ""
                    db.commit()
    return {
        "status": "ok" if chapter.original_content else "failed",
        "word_count": chapter.word_count,
        "is_translated": chapter.is_translated,
    }


def _translate_chapter_bg(novel_id: int, chapter_number: int, quality: str = "balanced"):
    """Background translate: own DB session (the request session is closed by then).
    Records failures on the chapter's last_error column for the retry queue.

    Serialized per-novel via _novel_lock so two queued triggers can't both see
    is_translated=False and double-translate the same chapter (re-entrancy)."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        # Re-check under the per-novel lock: the batch system serializes one job
        # per novel, but a single-chapter "translate" click can race a running
        # batch covering that same chapter.
        with _novel_lock(novel_id):
            chapter = db.query(Chapter).filter(
                Chapter.novel_id == novel_id,
                Chapter.chapter_number == chapter_number,
            ).first()
            if chapter and chapter.original_content and not chapter.is_translated:
                try:
                    _translate_chapter(db, chapter, quality, force=False)
                    if chapter.is_translated:
                        chapter.last_error = ""
                        db.commit()
                except HTTPException as e:
                    chapter.last_error = str(e.detail)[:500]
                    db.commit()
                    logger.warning(f"bg translate {novel_id}/{chapter_number}: {e.detail}")
                except RelayAuthError as e:
                    # Key cancelled/invalid on the router dashboard — PERMANENT.
                    # Don't record per-chapter errors; stop the whole batch now.
                    logger.error(f"bg translate {novel_id}/{chapter_number}: {e}")
                    raise
                except Exception as e:
                    chapter.last_error = str(e)[:500]
                    db.commit()
                    logger.warning(f"bg translate {novel_id}/{chapter_number}: {e}")
    finally:
        db.close()


# Note: the legacy plain-HTML-form handlers that used to live here (fetch one
# chapter, fetch the next batch, delete a novel — all POST-and-redirect, no
# JSON) were removed. The app has been Vue-driven since the multi-page
# rewrite; every page's own <form> uses @submit.prevent and calls the JSON
# API instead (frontend/library.js, login.js). A repo-wide grep found zero
# references to any of these paths outside their own route definitions, and
# they had drifted out of sync with their JSON-API equivalents: this one, for
# instance, bulk-deleted 4 tables directly (bypassing the ORM's own cascade
# relationships on Novel) and still missed bookmarks/diary_entries/batch_jobs
# — duplicate, unreachable, and already wrong is not something to "fix", it's
# something to delete. See DELETE /api/novels/{novel_id} for the real path.


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)