"""
backend/main.py's backup/restore round trip.

sqlite3.Connection.backup(target) copies SELF into target. The restore
endpoint had this reversed — the live DB was copied over the uploaded
snapshot (which was then deleted) while the API reported success, so a
restore silently did nothing.
"""
import os

import main as app_module


def test_backup_destroy_restore_actually_restores(client):
    created = client.post("/api/novels/manual",
                          json={"title": "Restore Me", "source_url": "manual://restore-me"})
    assert created.status_code == 200
    novel_id = created.json()["id"]

    backup = client.post("/api/backup").json()
    assert backup["status"] == "ok"
    backup_path = os.path.join(app_module._backup_dir(), backup["file"])

    deleted = client.delete("/api/novels/%d" % novel_id)
    assert deleted.status_code == 200
    assert client.get("/api/novels").json() == [], "novel must really be gone before restoring"

    with open(backup_path, "rb") as fh:
        r = client.post("/api/backups/restore",
                        files={"file": ("backup.db", fh.read(), "application/octet-stream")})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    titles = [n["title"] for n in client.get("/api/novels").json()]
    assert titles == ["Restore Me"], \
        "the pre-delete snapshot must actually come back, not be a silent no-op"


def test_restore_rejects_a_non_sqlite_upload(client):
    r = client.post("/api/backups/restore",
                    files={"file": ("backup.db", b"not a real sqlite file", "application/octet-stream")})
    assert r.status_code == 400


def test_restore_rejects_a_non_db_filename(client):
    r = client.post("/api/backups/restore",
                    files={"file": ("backup.txt", b"irrelevant", "text/plain")})
    assert r.status_code == 400
