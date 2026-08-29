"""
Stage 4: translate-ahead, match, and retranslate-drift used to be permanently
unresumable — _launch_batch's docstring explained why: each needs a per-call
argument (after_chapter/count, a needle, a chapter list) that the BatchJob row
never carried, so a restart mid-job always dropped the work (correctly marked
finished, never left stuck, but the work itself was lost — the user had to
notice and re-trigger it by hand). BatchJob now carries an args_json column,
populated by _set_batch(..., args=...) at the moment each of these three jobs
starts, so a restart can reconstruct the call and actually resume the job.
"""
import json
import time

import main as app_module
from database import SessionLocal
from models import BatchJob


def _novel(client, key):
    return client.post("/api/novels/manual",
                       json={"title": key, "source_url": "manual://%s" % key}).json()["id"]


def test_set_batch_persists_args_as_json(client):
    novel_id = _novel(client, "args-persist-1")
    assert app_module._set_batch(novel_id, "match", 3, args={"needle": "Angelia"})

    db = SessionLocal()
    row = db.query(BatchJob).filter(BatchJob.novel_id == novel_id, BatchJob.kind == "match").first()
    db.close()
    assert json.loads(row.args_json) == {"needle": "Angelia"}


def test_set_batch_leaves_args_json_empty_for_kinds_that_need_nothing(client):
    novel_id = _novel(client, "args-empty-1")
    assert app_module._set_batch(novel_id, "to-end", 3)

    db = SessionLocal()
    row = db.query(BatchJob).filter(BatchJob.novel_id == novel_id, BatchJob.kind == "to-end").first()
    db.close()
    assert row.args_json == ""


def test_launch_batch_resumes_match_with_its_persisted_needle(client, monkeypatch):
    novel_id = _novel(client, "resume-match-1")
    calls = []
    monkeypatch.setattr(app_module, "retranslate_match_bg", lambda nid, needle: calls.append((nid, needle)))

    started = app_module._launch_batch(novel_id, "match", json.dumps({"needle": "Angelia"}))
    assert started is True
    time.sleep(0.3)
    assert calls == [(novel_id, "Angelia")]


def test_launch_batch_resumes_retranslate_drift_with_its_chapter_list(client, monkeypatch):
    novel_id = _novel(client, "resume-drift-1")
    calls = []
    monkeypatch.setattr(app_module, "_retranslate_drift_bg",
                        lambda nid, chapter_numbers: calls.append((nid, chapter_numbers)))

    started = app_module._launch_batch(novel_id, "retranslate-drift", json.dumps({"chapter_numbers": [3, 5, 7]}))
    assert started is True
    time.sleep(0.3)
    assert calls == [(novel_id, [3, 5, 7])]


def test_launch_batch_resumes_translate_ahead_with_its_after_chapter_and_count(client, monkeypatch):
    novel_id = _novel(client, "resume-ahead-1")
    calls = []
    monkeypatch.setattr(app_module, "translate_ahead_bg",
                        lambda nid, after_chapter, count=5: calls.append((nid, after_chapter, count)))

    started = app_module._launch_batch(novel_id, "translate-ahead", json.dumps({"after_chapter": 12, "count": 3}))
    assert started is True
    time.sleep(0.3)
    assert calls == [(novel_id, 12, 3)]


def test_launch_batch_still_refuses_these_kinds_without_usable_args(client):
    # Unchanged behavior for a row with no args_json (an old pre-fix row, or a
    # genuinely empty one) — resuming with a guessed needle/chapter list would
    # be worse than dropping the job, so this must still return False.
    novel_id = _novel(client, "no-args-1")
    assert app_module._launch_batch(novel_id, "match") is False
    assert app_module._launch_batch(novel_id, "retranslate-drift") is False
    assert app_module._launch_batch(novel_id, "translate-ahead") is False
    # Malformed JSON must fail the same way, not raise.
    assert app_module._launch_batch(novel_id, "match", "{not json") is False


def test_a_stale_match_job_with_persisted_args_is_actually_resumed_on_restart(client, monkeypatch):
    novel_id = _novel(client, "full-resume-match-1")
    calls = []
    monkeypatch.setattr(app_module, "retranslate_match_bg", lambda nid, needle: calls.append((nid, needle)))

    db = SessionLocal()
    db.add(BatchJob(novel_id=novel_id, kind="match", total=5, done=1, running=True,
                    args_json=json.dumps({"needle": "Kael"})))
    db.commit()
    db.close()

    app_module._resume_interrupted_jobs()
    time.sleep(0.3)

    assert calls == [(novel_id, "Kael")], "restart must actually relaunch the job with its real needle"
    db = SessionLocal()
    still_running = db.query(BatchJob).filter(
        BatchJob.novel_id == novel_id, BatchJob.kind == "match", BatchJob.running == True).count()
    db.close()
    assert still_running == 0, "the stale row must still be freed even though a resumed worker was launched"
