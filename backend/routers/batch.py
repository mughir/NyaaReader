"""
Batch Job management and status endpoints.
"""
from datetime import datetime
from typing import Dict, List, Optional
from fastapi import APIRouter, Depends
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from database import get_db_session
from models import BatchJob, Chapter, Novel, NovelMemory
from services.job_service import JOB_STALL_MINUTES, _batch_cache, _request_batch_stop
from services.novel_service import _load_glossary

router = APIRouter(tags=["batch"])

KIND_LABELS = {
    "meta": "Novel Title & Synopsis",
    "titles": "Chapter Titles",
    "memory": "AI Memory & Glossary",
    "to-end": "Chapter Translations",
    "translate-selected": "Selected Chapters",
    "retranslate": "Full Re-translation",
    "retranslate-drift": "Fix Glossary Drift",
    "retry-failed": "Retry Failed Chapters",
    "match": "Retranslate Matching Chapters",
    "translate-ahead": "Translate Ahead",
    "updates": "Check Updates",
    "epub": "Export EPUB",
}


@router.post("/api/novels/{novel_id}/batch-stop")
async def batch_stop(novel_id: int):
    """Ask the running batch (any kind) to stop after the current item."""
    stopped = _request_batch_stop(novel_id)
    return {"status": "stopped" if stopped else "idle"}


@router.get("/api/novels/{novel_id}/batch-status")
async def batch_status(novel_id: int):
    """Poll: current background batch progress for a novel."""
    cached = _batch_cache.get(novel_id)
    if cached:
        if cached["total"] > 0 and cached["done"] > cached["total"]:
            cached["done"] = cached["total"]
        return cached
    from database import SessionLocal
    db = SessionLocal()
    try:
        job = db.query(BatchJob).filter(
            BatchJob.novel_id == novel_id, BatchJob.running == True).order_by(
            BatchJob.id.desc()).first()
        if not job:
            return {"kind": None, "total": 0, "done": 0, "current_label": "", "running": False}
        if job.updated_at and (datetime.utcnow() - job.updated_at).total_seconds() > JOB_STALL_MINUTES * 60:
            return {"kind": None, "total": 0, "done": 0, "current_label": "", "running": False}
        done = min(job.done, job.total) if job.total > 0 else job.done
        data = {
            "kind": job.kind, "total": job.total, "done": done,
            "current_label": job.current_label or "", "running": True
        }
        _batch_cache[novel_id] = data
        return data
    finally:
        db.close()


@router.get("/api/dashboard/tasks")
async def get_dashboard_tasks(db: Session = Depends(get_db_session)):
    """
    Return all active background translation tasks, recent finished/stopped tasks,
    and granular translation status per novel for dashboard monitoring and management.
    """
    now = datetime.utcnow()
    # 1. Active jobs
    raw_active_jobs = db.query(BatchJob).filter(BatchJob.running == True).order_by(BatchJob.id.desc()).all()
    active_jobs = []
    active_by_novel: Dict[int, dict] = {}

    for job in raw_active_jobs:
        # Check staleness
        if job.updated_at and (now - job.updated_at).total_seconds() > JOB_STALL_MINUTES * 60:
            continue
        cached = _batch_cache.get(job.novel_id)
        done = cached["done"] if (cached and cached.get("running")) else job.done
        total = cached["total"] if (cached and cached.get("running")) else job.total
        current_label = cached["current_label"] if (cached and cached.get("running")) else (job.current_label or "")
        stop_requested = cached.get("stop_requested", False) if cached else bool(job.stop_requested)
        done_clamped = min(done, total) if total > 0 else done
        percent = round((done_clamped / total) * 100) if total > 0 else 0

        novel = db.query(Novel).filter(Novel.id == job.novel_id).first()
        novel_title = (novel.title_translated or novel.title) if novel else f"Novel #{job.novel_id}"

        job_data = {
            "id": job.id,
            "novel_id": job.novel_id,
            "novel_title": novel_title,
            "kind": job.kind,
            "kind_label": KIND_LABELS.get(job.kind, job.kind.replace("-", " ").title()),
            "done": done_clamped,
            "total": total,
            "percent": percent,
            "current_label": current_label,
            "running": True,
            "stop_requested": stop_requested,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        }
        active_jobs.append(job_data)
        active_by_novel[job.novel_id] = job_data

    # 2. Recent completed / stopped jobs (up to 8)
    recent_raw = (db.query(BatchJob)
                  .filter(BatchJob.running == False)
                  .order_by(BatchJob.id.desc())
                  .limit(8).all())
    recent_jobs = []
    for job in recent_raw:
        novel = db.query(Novel).filter(Novel.id == job.novel_id).first()
        novel_title = (novel.title_translated or novel.title) if novel else f"Novel #{job.novel_id}"
        recent_jobs.append({
            "id": job.id,
            "novel_id": job.novel_id,
            "novel_title": novel_title,
            "kind": job.kind,
            "kind_label": KIND_LABELS.get(job.kind, job.kind.replace("-", " ").title()),
            "done": job.done,
            "total": job.total,
            "current_label": job.current_label or "",
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        })

    # 3. Per-novel metrics
    novels = db.query(Novel).order_by(Novel.updated_at.desc()).all()
    
    # Batch query chapter stats
    ch_stats_rows = (
        db.query(
            Chapter.novel_id,
            func.count(Chapter.id).label("total"),
            func.sum(case((Chapter.is_translated == True, 1), else_=0)).label("translated"),
            func.sum(case(((Chapter.last_error.isnot(None)) & (Chapter.last_error != "") & (Chapter.is_translated == False), 1), else_=0)).label("failed"),
            func.sum(case(((Chapter.title_translated.isnot(None)) & (Chapter.title_translated != ""), 1), else_=0)).label("titles_translated"),
            func.sum(case(((Chapter.title.isnot(None)) & (Chapter.title != "") & ((Chapter.title_translated.is_(None)) | (Chapter.title_translated == "")), 1), else_=0)).label("titles_missing"),
        )
        .group_by(Chapter.novel_id)
        .all()
    )
    ch_stats_map = {r[0]: r for r in ch_stats_rows}

    # Batch query memory
    memories = {m.novel_id: m for m in db.query(NovelMemory).all()}

    novels_data = []
    for n in novels:
        ch_row = ch_stats_map.get(n.id)
        total_ch = ch_row[1] if ch_row else (n.total_chapters or 0)
        translated_ch = ch_row[2] if ch_row else 0
        failed_ch = ch_row[3] if ch_row else 0
        titles_trans = ch_row[4] if ch_row else 0
        titles_missing = ch_row[5] if ch_row else 0
        pending_ch = max(0, total_ch - translated_ch)

        mem = memories.get(n.id)
        entries = _load_glossary(mem) if mem else []
        glossary_total = len(entries)
        glossary_trans = sum(1 for e in entries if (e.get("translated") or "").strip() and e.get("translated") != e.get("source"))
        glossary_pending = max(0, glossary_total - glossary_trans)
        has_memory = bool(mem and (mem.characters or mem.terms or mem.plot or entries))

        active_job = active_by_novel.get(n.id)
        active_kind = active_job["kind"] if active_job else None

        novel_info = {
            "id": n.id,
            "title": n.title,
            "title_translated": n.title_translated or "",
            "author": n.author or "",
            "cover_url": n.cover_url or "",
            "total_chapters": total_ch,
            "novel_title_status": {
                "is_translated": bool(n.title_translated and (n.description_translated or not n.description)),
                "title_translated": bool(n.title_translated),
                "description_translated": bool(n.description_translated or not n.description),
                "is_running": (active_kind == "meta"),
            },
            "chapter_title_status": {
                "total": total_ch,
                "translated": titles_trans,
                "pending": titles_missing,
                "is_running": (active_kind == "titles"),
            },
            "memory_status": {
                "total_entries": glossary_total,
                "translated_entries": glossary_trans,
                "pending_entries": glossary_pending,
                "has_memory": has_memory,
                "is_running": (active_kind == "memory"),
            },
            "chapter_content_status": {
                "total": total_ch,
                "translated": translated_ch,
                "failed": failed_ch,
                "pending": pending_ch,
                "is_running": (active_kind in ("to-end", "translate-selected", "retranslate", "retry-failed", "match", "retranslate-drift", "translate-ahead", "updates")),
            },
            "active_job": active_job,
        }
        novels_data.append(novel_info)

    return {
        "active_jobs": active_jobs,
        "recent_jobs": recent_jobs,
        "novels": novels_data,
        "total_active": len(active_jobs),
    }

