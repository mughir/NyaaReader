"""
Tests for Dashboard Tasks API, Memory Translation Background Job,
and Cancel / Retry workflows.
"""
import json
import pytest
from database import SessionLocal
from models import BatchJob, Chapter, Novel, NovelMemory
import main as app_module
import translator as translator_module
from services.job_service import translate_memory_bg, _batch_cache


class DummyTranslator:
    def translate_short(self, text, source_lang, target_lang="en"):
        return f"Translated {text}"


def _create_test_novel(client, title="Test Novel", source_url="https://example.com/novel/1", n_chapters=3):
    res = client.post("/api/novels/manual", json={
        "title": title,
        "source_url": source_url,
        "original_language": "zh",
        "target_language": "en",
    })
    novel_id = res.json()["id"]
    db = SessionLocal()
    for i in range(1, n_chapters + 1):
        db.add(Chapter(
            novel_id=novel_id,
            chapter_number=i,
            title=f"Original Title {i}",
            original_content=f"Original content {i}",
            is_translated=(i == 1),
            title_translated=(f"Translated Title {i}" if i == 1 else None),
            translated_content=(f"Translated content {i}" if i == 1 else None),
            last_error=("Network error" if i == 3 else ""),
        ))
    
    # Add memory with glossary entries
    mem = NovelMemory(
        novel_id=novel_id,
        characters="Hero (主角) - the hero",
        terms="魔力 = mana",
        glossary_entries=[
            {"type": "character", "source": "主角", "translated": "Hero", "note": "the hero", "locked": False},
            {"type": "term", "source": "魔石", "translated": "", "note": "magic stone", "locked": False},
        ],
    )
    db.add(mem)
    db.commit()
    db.close()
    return novel_id


def test_dashboard_tasks_endpoint_structure(client):
    novel_id = _create_test_novel(client, title="Task Test Novel 1", source_url="https://example.com/novel/tasks-1")
    
    res = client.get("/api/dashboard/tasks")
    assert res.status_code == 200
    data = res.json()
    assert "active_jobs" in data
    assert "recent_jobs" in data
    assert "novels" in data
    assert "total_active" in data

    novel_entry = next((n for n in data["novels"] if n["id"] == novel_id), None)
    assert novel_entry is not None
    assert novel_entry["total_chapters"] == 3
    
    # Check chapter title status
    assert novel_entry["chapter_title_status"]["total"] == 3
    assert novel_entry["chapter_title_status"]["translated"] == 1
    assert novel_entry["chapter_title_status"]["pending"] == 2
    assert novel_entry["chapter_title_status"]["is_running"] is False

    # Check memory status (主角, 魔石, 魔力 synced)
    assert novel_entry["memory_status"]["total_entries"] == 3
    assert novel_entry["memory_status"]["translated_entries"] >= 1
    assert novel_entry["memory_status"]["pending_entries"] >= 1
    assert novel_entry["memory_status"]["has_memory"] is True
    assert novel_entry["memory_status"]["is_running"] is False

    # Check chapter content status
    assert novel_entry["chapter_content_status"]["total"] == 3
    assert novel_entry["chapter_content_status"]["translated"] == 1
    assert novel_entry["chapter_content_status"]["failed"] == 1
    assert novel_entry["chapter_content_status"]["pending"] == 2


def test_translate_memory_background_job(client, monkeypatch):
    novel_id = _create_test_novel(client, title="Memory Test Novel", source_url="https://example.com/novel/mem-1")
    monkeypatch.setattr(translator_module, "get_translator", lambda *a, **k: DummyTranslator())

    translate_memory_bg(novel_id)

    db = SessionLocal()
    mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
    entries = json.loads(mem.glossary_entries) if isinstance(mem.glossary_entries, str) else mem.glossary_entries
    db.close()

    magic_stone = next((e for e in entries if e["source"] == "魔石"), None)
    assert magic_stone is not None
    assert magic_stone["translated"] == "Translated 魔石"


def test_translate_memory_endpoint_starts_job(client, monkeypatch):
    novel_id = _create_test_novel(client, title="Endpoint Test Novel", source_url="https://example.com/novel/endpoint-1")
    monkeypatch.setattr(translator_module, "get_translator", lambda *a, **k: DummyTranslator())

    res = client.post(f"/api/novels/{novel_id}/translate-memory")
    assert res.status_code == 200
    assert res.json()["status"] == "started"


def test_translate_memory_cancellation(client, monkeypatch):
    novel_id = _create_test_novel(client, title="Cancel Test Novel", source_url="https://example.com/novel/cancel-1")
    monkeypatch.setattr(translator_module, "get_translator", lambda *a, **k: DummyTranslator())
    monkeypatch.setattr(app_module, "_batch_stop_requested", lambda nid: True)

    translate_memory_bg(novel_id)

    db = SessionLocal()
    job = db.query(BatchJob).filter(BatchJob.novel_id == novel_id, BatchJob.kind == "memory").order_by(BatchJob.id.desc()).first()
    db.close()

    assert job is not None
    assert job.running is False
    assert "Stopped by user" in (job.current_label or "")


def test_batch_stop_endpoint(client):
    novel_id = _create_test_novel(client, title="Stop Route Novel", source_url="https://example.com/novel/stop-route-1")
    
    # Claim a batch job
    db = SessionLocal()
    job = BatchJob(novel_id=novel_id, kind="titles", total=5, done=1, running=True, current_label="Ch 1")
    db.add(job)
    db.commit()
    db.close()

    res = client.post(f"/api/novels/{novel_id}/batch-stop")
    assert res.status_code == 200
    assert res.json()["status"] == "stopped"

    db = SessionLocal()
    job = db.query(BatchJob).filter(BatchJob.novel_id == novel_id).order_by(BatchJob.id.desc()).first()
    assert job.stop_requested is True
    db.close()
