"""
Database backup services for NyaaReader.
"""
import glob
import logging
import os
import shutil
import time
from datetime import datetime

from services.config_service import _get_config

logger = logging.getLogger("novel-reader.backup")


def _backup_dir() -> str:
    db_path = os.getenv("DATABASE_URL", "sqlite:///./novel_reader.db").replace("sqlite:///", "")
    d = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(d, exist_ok=True)
    return d


def run_backup(now=None) -> dict:
    """Copy the SQLite DB to backups/. Uses SQLite's online backup API so the copy
    is consistent even while the app is writing. Prunes old backups (keep N)."""
    now = now or datetime.utcnow()
    db_path = os.getenv("DATABASE_URL", "sqlite:///./novel_reader.db").replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return {"status": "error", "message": "DB file not found"}
    bdir = _backup_dir()
    fname = f"novel_reader-{now.strftime('%Y%m%d-%H%M%S')}.db"
    dest = os.path.join(bdir, fname)
    # Consistent copy via SQLite backup API
    try:
        from sqlalchemy import create_engine
        src_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        dst_engine = create_engine(f"sqlite:///{dest}")
        with src_engine.connect() as src, dst_engine.connect() as dst:
            src.connection.connection.backup(dst.connection.connection)
        src_engine.dispose()
        dst_engine.dispose()
    except Exception as e:
        logger.warning(f"run_backup: SQLite backup API failed, using plain copy instead: {e}")
        shutil.copy2(db_path, dest)
    # Prune old backups
    keep = _get_config().get("backup_keep", 14)
    backups = sorted(glob.glob(os.path.join(bdir, "novel_reader-*.db")))
    for old in backups[:-keep]:
        try:
            os.remove(old)
        except Exception as e:
            logger.warning(f"run_backup: could not remove old backup {old}: {e}")
    return {"status": "ok", "file": fname, "size": os.path.getsize(dest)}


def _backup_scheduler_loop():
    """Background thread: run a backup when due (checks hourly)."""
    while True:
        try:
            cfg = _get_config()
            if cfg.get("backup_enabled"):
                bdir = _backup_dir()
                existing = glob.glob(os.path.join(bdir, "novel_reader-*.db"))
                due = True
                if existing:
                    newest = max(os.path.getmtime(f) for f in existing)
                    age_h = (time.time() - newest) / 3600
                    due = age_h >= cfg.get("backup_interval_hours", 24)
                if due:
                    run_backup()
                    logger.info("Scheduled backup completed")
        except Exception as e:
            logger.warning(f"backup scheduler error: {e}")
        time.sleep(3600)  # check every hour
