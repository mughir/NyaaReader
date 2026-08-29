"""
backend/main.py's background-job resume on restart, and the resumability
table (_launch_batch) that decides which job kinds can survive one.

The original bug: rows were relaunched while still marked running, and a
resumed worker's own _set_batch() refuses to start against a row that still
looks fresh (updated_at newer than JOB_STALL_MINUTES) — so any restart
shortly after the last progress bump silently dropped the job AND left the
novel blocked for new batches until the watchdog freed it ~10 minutes later.

A second bug rode along: "meta" jobs were in neither the resumable list nor
the not-resumable list, so a restart left them stuck running=True forever.
"""
import time

import main as app_module
from database import SessionLocal
from models import BatchJob, Chapter


class TestLaunchBatchIsTheSourceOfTruth:
    def test_resumable_kinds_return_true_without_actually_needing_the_thread_to_finish(self, client):
        novel_id = client.post("/api/novels/manual",
                               json={"title": "Launch Check", "source_url": "manual://launch-check-1"}).json()["id"]
        # "meta" needs only novel_id, so it must be launchable.
        started = app_module._launch_batch(novel_id, "meta")
        assert started is True

    def test_unresumable_kinds_return_false(self, client):
        novel_id = client.post("/api/novels/manual",
                               json={"title": "Launch Check 2", "source_url": "manual://launch-check-2"}).json()["id"]
        # "match" needs a needle the BatchJob row doesn't carry.
        assert app_module._launch_batch(novel_id, "match") is False


class TestResumeFreesRowsBeforeRelaunching:
    def test_an_interrupted_resumable_job_is_released_not_left_stuck(self, client):
        novel_id = client.post("/api/novels/manual",
                               json={"title": "Resume Me", "source_url": "manual://resume-me-1"}).json()["id"]
        db = SessionLocal()
        for i in (1, 2, 3):
            db.add(Chapter(novel_id=novel_id, chapter_number=i, title="ch%d" % i,
                           original_content="body %d" % i, is_translated=False))
        job = BatchJob(novel_id=novel_id, kind="to-end", total=10, done=3, running=True)
        db.add(job)
        db.commit()
        job_id = job.id
        db.close()

        app_module._resume_interrupted_jobs()
        time.sleep(1.5)  # let the relaunched daemon thread claim the slot

        db = SessionLocal()
        row = db.query(BatchJob).filter(BatchJob.id == job_id).first()
        db.close()
        # The ORIGINAL row is freed immediately (marked running=False before
        # anything is relaunched); a genuinely resumed worker creates its OWN
        # fresh row once it recomputes the real remaining count.
        assert row is not None and row.running is False

    def test_the_resumed_worker_recomputes_total_from_what_is_actually_left(self, client):
        """The stale row carried total=10 from before the restart; a REAL
        resumed worker recomputes it from the 3 chapters that actually remain
        untranslated — proof it's a live worker, not the dead row."""
        novel_id = client.post("/api/novels/manual",
                               json={"title": "Resume Recompute", "source_url": "manual://resume-recompute-1"}).json()["id"]
        db = SessionLocal()
        for i in (1, 2, 3):
            db.add(Chapter(novel_id=novel_id, chapter_number=i, title="ch%d" % i,
                           original_content="body %d" % i, is_translated=False))
        stale = BatchJob(novel_id=novel_id, kind="to-end", total=10, done=3, running=True)
        db.add(stale)
        db.commit()
        stale_id = stale.id
        db.close()

        app_module._resume_interrupted_jobs()

        # _resume_interrupted_jobs marks the STALE row not-running BEFORE
        # relaunching anything, so "not running" alone doesn't prove a real
        # worker ran — the stale row satisfies that instantly. _set_batch()
        # finds no running row (the stale one was just freed) and creates a
        # brand NEW row instead, so wait specifically for a DIFFERENT id to
        # appear: that is the live worker, and its `total` is recomputed from
        # what's actually left, never the stale row's total=10.
        deadline = time.time() + 25
        fresh = None
        while time.time() < deadline:
            db = SessionLocal()
            candidates = db.query(BatchJob).filter(
                BatchJob.novel_id == novel_id, BatchJob.id != stale_id).all()
            db.close()
            if candidates:
                fresh = candidates[-1]
                if not fresh.running:
                    break
            time.sleep(0.05)
        assert fresh is not None, "no worker ever started a fresh job row — the resume silently dropped it"
        assert fresh.total == 3, \
            "a live resumed worker must recompute total from the 3 remaining chapters, not keep the stale 10"


class TestUnresumableKindsAreFreedNotStuck:
    def test_a_match_job_interrupted_by_a_restart_is_marked_finished(self, client):
        novel_id = client.post("/api/novels/manual",
                               json={"title": "Unresumable", "source_url": "manual://unresumable-1"}).json()["id"]
        db = SessionLocal()
        db.add(BatchJob(novel_id=novel_id, kind="match", total=5, done=1, running=True))
        db.commit()
        db.close()

        app_module._resume_interrupted_jobs()

        db = SessionLocal()
        still_running = db.query(BatchJob).filter(
            BatchJob.novel_id == novel_id, BatchJob.kind == "match", BatchJob.running == True).count()
        db.close()
        assert still_running == 0, \
            "an unresumable kind left running=True blocks every new batch for this novel indefinitely"
