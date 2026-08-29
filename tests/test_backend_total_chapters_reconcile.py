"""
Stage 3: total_chapters reconciliation. Two different call sites
(check_updates_bg, add_chapter_manual) each independently hit the same
autoflush-undercount bug and each got its own point fix once found — which
only guards the exact path already discovered. _reconcile_total_chapters()
runs on every start and corrects any drift regardless of its cause, so a
future bug (or a restored backup, or a direct DB edit) can't leave a novel's
chapter count silently wrong forever.
"""
from database import SessionLocal, _reconcile_total_chapters
from models import Chapter, Novel


def test_a_novel_with_the_wrong_total_chapters_is_corrected(client):
    novel_id = client.post("/api/novels/manual",
                           json={"title": "Reconcile Check", "source_url": "manual://reconcile-1"}).json()["id"]
    db = SessionLocal()
    for i in (1, 2, 3):
        db.add(Chapter(novel_id=novel_id, chapter_number=i, title="ch%d" % i))
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    novel.total_chapters = 99   # deliberately wrong, simulating drift from any cause
    db.commit()
    db.close()

    _reconcile_total_chapters()

    db = SessionLocal()
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    db.close()
    assert novel.total_chapters == 3


def test_a_novel_that_is_already_correct_is_left_alone(client):
    novel_id = client.post("/api/novels/manual",
                           json={"title": "Reconcile Correct", "source_url": "manual://reconcile-2"}).json()["id"]
    db = SessionLocal()
    db.add(Chapter(novel_id=novel_id, chapter_number=1, title="ch1"))
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    novel.total_chapters = 1
    db.commit()
    db.close()

    _reconcile_total_chapters()   # must not touch an already-correct row

    db = SessionLocal()
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    db.close()
    assert novel.total_chapters == 1


def test_a_novel_with_zero_chapters_reconciles_to_zero(client):
    novel_id = client.post("/api/novels/manual",
                           json={"title": "Reconcile Zero", "source_url": "manual://reconcile-3"}).json()["id"]
    db = SessionLocal()
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    novel.total_chapters = 5   # wrong — this novel has no chapters at all
    db.commit()
    db.close()

    _reconcile_total_chapters()

    db = SessionLocal()
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    db.close()
    assert novel.total_chapters == 0


def test_running_it_twice_in_a_row_is_a_safe_no_op(client):
    novel_id = client.post("/api/novels/manual",
                           json={"title": "Reconcile Idempotent", "source_url": "manual://reconcile-4"}).json()["id"]
    db = SessionLocal()
    db.add(Chapter(novel_id=novel_id, chapter_number=1, title="ch1"))
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    novel.total_chapters = 7
    db.commit()
    db.close()

    _reconcile_total_chapters()
    _reconcile_total_chapters()

    db = SessionLocal()
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    db.close()
    assert novel.total_chapters == 1
