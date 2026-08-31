"""
Tests for batch operations:
- batch-translate-selected
- batch-mark-read
"""
import pytest
from database import SessionLocal
from models import Chapter, Novel


def test_batch_mark_read_and_unread(client):
    res = client.post("/api/novels/manual", json={"title": "Batch Read Novel", "source_url": "manual://batch-read"})
    assert res.status_code == 200
    novel_id = res.json()["id"]

    # add 3 chapters
    for n in [1, 2, 3]:
        client.post(f"/api/novels/{novel_id}/chapters/manual", json={"chapter_number": n, "title": f"Ch {n}", "content": "Content"})

    # batch mark read chapters 1 & 3
    r = client.post(f"/api/novels/{novel_id}/batch-mark-read", json={"chapters": [1, 3], "is_read": True})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["updated"] == 2

    db = SessionLocal()
    ch1 = db.query(Chapter).filter(Chapter.novel_id == novel_id, Chapter.chapter_number == 1).first()
    ch2 = db.query(Chapter).filter(Chapter.novel_id == novel_id, Chapter.chapter_number == 2).first()
    ch3 = db.query(Chapter).filter(Chapter.novel_id == novel_id, Chapter.chapter_number == 3).first()
    assert ch1.is_read is True
    assert ch2.is_read is False
    assert ch3.is_read is True
    db.close()

    # batch unmark read chapter 1
    r2 = client.post(f"/api/novels/{novel_id}/batch-mark-read", json={"chapters": [1], "is_read": False})
    assert r2.status_code == 200

    db = SessionLocal()
    ch1 = db.query(Chapter).filter(Chapter.novel_id == novel_id, Chapter.chapter_number == 1).first()
    assert ch1.is_read is False
    db.close()


def test_batch_translate_selected_endpoint(client, monkeypatch):
    res = client.post("/api/novels/manual", json={"title": "Batch Trans Novel", "source_url": "manual://batch-trans"})
    novel_id = res.json()["id"]

    for n in [1, 2]:
        client.post(f"/api/novels/{novel_id}/chapters/manual", json={"chapter_number": n, "title": f"Ch {n}", "content": "Content"})

    import main as app_module
    bg_calls = []
    monkeypatch.setattr(app_module, "_translate_selected_bg", lambda nid, chs: bg_calls.append((nid, chs)))

    r = client.post(f"/api/novels/{novel_id}/batch-translate-selected", json={"chapters": [1, 2]})
    assert r.status_code == 200
    assert r.json()["status"] == "started"
    assert r.json()["count"] == 2
