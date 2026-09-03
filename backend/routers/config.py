"""
Config, Relay health-check, and Backup endpoints.
"""
import asyncio
from datetime import datetime as _dt
import glob
import logging
import os
import shutil
import sqlite3
import sys
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import get_db_session
from models import AppConfig
from security import _rotate_session_secret
from services.backup_service import _backup_dir, run_backup
from services.config_service import (
    _apply_config_to_env,
    _check_relay_health_bg,
    _config_health_check_sync,
    _get_config,
    _relay_health_cache,
)
from services.job_service import _batch_cache

logger = logging.getLogger("novel-reader.config_router")


def _get_main_attr(name: str, fallback):
    main_mod = sys.modules.get("main")
    if main_mod is not None and hasattr(main_mod, name):
        return getattr(main_mod, name)
    return fallback


router = APIRouter(tags=["config"])


@router.get("/api/config")
async def get_config():
    """App config with secrets masked (show only last 4 chars)."""
    cfg_fn = _get_main_attr("_get_config", _get_config)
    cfg = cfg_fn()
    masked = {}
    for k in ("gemini_api_key", "fallback_api_key", "auth_password", "fallback_2_api_key"):
        v = cfg.get(k, "")
        masked[k] = (v[-4:] if len(v) >= 4 else "") if v else ""
        if k == "fallback_api_key" and not v:
            v = os.getenv("FALLBACK_API_KEY", "")
        masked[k + "_set"] = bool(v)
    out = {k: v for k, v in cfg.items() if k not in ("gemini_api_key", "fallback_api_key", "auth_password", "fallback_2_api_key")}
    return {**out, **masked}


@router.put("/api/config")
async def put_config(payload: dict, background_tasks: BackgroundTasks = None,
                     db: Session = Depends(get_db_session)):
    """Update config: only non-empty values replace; empty strings keep old value."""
    cfg = db.query(AppConfig).filter(AppConfig.id == 1).first()
    if not cfg:
        cfg = AppConfig(id=1)
        db.add(cfg)
    fields = [
        "gemini_api_key", "fallback_api_key", "fallback_base_url",
        "fallback_model", "fallback_model_2", "auth_password",
        "fallback_2_base_url", "fallback_2_api_key"
    ]
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
                if f.endswith("_api_key") or f == "auth_password":
                    # Reject masked fragments AND any short secret outright:
                    # a 1-char "key" is never valid and previously clobbered
                    # the good env-backed key.
                    stored = (getattr(cfg, f) or "")
                    if len(v) < 8 or (stored and stored.endswith(v)):
                        continue
                setattr(cfg, f, v)
    if "backup_enabled" in payload:
        cfg.backup_enabled = bool(payload["backup_enabled"])
    if "backup_interval_hours" in payload:
        cfg.backup_interval_hours = int(payload["backup_interval_hours"])
    if "backup_keep" in payload:
        cfg.backup_keep = int(payload["backup_keep"])
    db.commit()
    if password_changed:
        _rotate_session_secret()
    apply_fn = _get_main_attr("_apply_config_to_env", _apply_config_to_env)
    apply_fn(cleared)
    if background_tasks is not None:
        health_bg_fn = _get_main_attr("_check_relay_health_bg", _check_relay_health_bg)
        background_tasks.add_task(health_bg_fn)
    return {"status": "ok"}


@router.post("/api/config/health-check")
async def config_health_check(payload: dict = None):
    """Verify relay credentials before saving (Settings save-time check)."""
    check_fn = _get_main_attr("_config_health_check_sync", _config_health_check_sync)
    p = await asyncio.to_thread(check_fn, payload or {})
    return p


@router.get("/api/config/health-status")
async def config_health_status():
    """Last PROACTIVE relay/model health check."""
    cache = _get_main_attr("_relay_health_cache", _relay_health_cache)
    return cache or {"checked_at": None}


@router.post("/api/backup")
async def backup_now():
    """Manual backup trigger."""
    b_fn = _get_main_attr("run_backup", run_backup)
    return b_fn()


@router.get("/api/backups")
async def list_backups():
    """List existing DB backups (name, size, date)."""
    bdir_fn = _get_main_attr("_backup_dir", _backup_dir)
    bdir = bdir_fn()
    out = []
    for f in sorted(glob.glob(os.path.join(bdir, "novel_reader-*.db")), reverse=True):
        st = os.stat(f)
        out.append({
            "name": os.path.basename(f),
            "size": st.st_size,
            "date": _dt.utcfromtimestamp(st.st_mtime).isoformat(),
        })
    return out


@router.get("/api/backups/{name}/download")
async def download_backup(name: str):
    """Download a backup file."""
    safe = os.path.basename(name)
    if not safe or safe in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid backup name")
    bdir_fn = _get_main_attr("_backup_dir", _backup_dir)
    path = os.path.join(bdir_fn(), safe)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(path, filename=safe)


@router.post("/api/backups/restore")
async def restore_backup(file: UploadFile):
    """Restore the library from an uploaded backup .db file."""
    if not file.filename or not file.filename.endswith(".db"):
        raise HTTPException(status_code=400, detail="Upload a .db backup file")
    db_path = os.getenv("DATABASE_URL", "sqlite:///./novel_reader.db").replace("sqlite:///", "")
    if not os.path.exists(db_path):
        raise HTTPException(status_code=500, detail="DB file not found")
    staging = db_path + ".restore-staging"
    try:
        with open(staging, "wb") as out:
            while chunk := await file.read(1 << 20):
                out.write(chunk)
        probe = sqlite3.connect(staging)
        try:
            probe.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception:
            probe.close()
            os.remove(staging)
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid SQLite database")
        else:
            probe.close()
        shutil.copy2(staging, db_path + ".restored-new")
        live = sqlite3.connect(db_path)
        try:
            live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            restored = sqlite3.connect(db_path + ".restored-new")
            try:
                restored.backup(live)
            finally:
                restored.close()
        finally:
            live.close()
        try:
            os.remove(db_path + ".restored-new")
        except OSError as e:
            logger.warning(f"restore: could not remove {db_path}.restored-new: {e}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Restore failed: {e}")
    finally:
        if os.path.exists(staging):
            try:
                os.remove(staging)
            except OSError as e:
                logger.warning(f"restore: could not remove staging file {staging}: {e}")
    size = os.path.getsize(db_path)
    cache = _get_main_attr("_batch_cache", _batch_cache)
    cache.clear()
    try:
        from database import engine as _engine
        _engine.dispose()
    except Exception as e:
        logger.warning(f"could not dispose engine after restore: {e}")
    return {"status": "ok", "size": size, "message": "Restored — reload the page"}


@router.delete("/api/backups/{name}")
async def delete_backup(name: str):
    """Delete a single backup file."""
    safe = os.path.basename(name)
    if safe in (".", "..") or not safe:
        raise HTTPException(status_code=400, detail="Invalid backup name")
    bdir_fn = _get_main_attr("_backup_dir", _backup_dir)
    path = os.path.join(bdir_fn(), safe)
    if not os.path.exists(path) or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Backup not found")
    try:
        os.remove(path)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not delete backup: {e}")
    return {"status": "ok"}
