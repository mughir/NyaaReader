"""
Batch Job management and status endpoints.
"""
from fastapi import APIRouter
from services.job_service import _batch_cache, _request_batch_stop

router = APIRouter(tags=["batch"])


@router.post("/api/novels/{novel_id}/batch-stop")
async def batch_stop(novel_id: int):
    """Ask the running batch (any kind) to stop after the current chapter."""
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
    from models import BatchJob
    db = SessionLocal()
    try:
        job = db.query(BatchJob).filter(
            BatchJob.novel_id == novel_id, BatchJob.running == True).order_by(
            BatchJob.id.desc()).first()
        if not job:
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
