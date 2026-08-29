"""
Stage 2: every background job now records a terminal outcome on every exit
path (completed / stopped / relay key rejected / an explicit failure reason)
instead of a bare _clear_batch() that left the job's fate unrecorded.

Auditing every *_bg function for this uncovered three REAL bugs, not just
missing labels — each fixed alongside the labels and regression-tested here:

1. translate_novel_meta_bg: the stop-check `return`ed directly, skipping
   _finish_batch entirely — the job stayed running=True until the watchdog
   freed it ~10 minutes later.
2. retranslate_match_bg and _retranslate_drift_bg: no stop-check in the loop
   at all — clicking "Stop" during either had no effect.
3. translate_titles_bg and _retranslate_drift_bg: no RelayAuthError
   shortcut — a dead/revoked relay key made these loop through every
   remaining item, failing each one individually, instead of stopping once.
"""
import main as app_module
import translator as translator_module
from database import SessionLocal
from models import BatchJob, Chapter
from translator import RelayAuthError


def _seed_novel(client, title, source_url, n_chapters=3, translated=False):
    novel_id = client.post("/api/novels/manual", json={"title": title, "source_url": source_url}).json()["id"]
    db = SessionLocal()
    for i in range(1, n_chapters + 1):
        db.add(Chapter(novel_id=novel_id, chapter_number=i, title="ch%d" % i,
                       original_content="body %d" % i,
                       translated_content=("translated %d" % i) if translated else None,
                       is_translated=translated))
    db.commit()
    db.close()
    return novel_id


def _job_for(novel_id):
    db = SessionLocal()
    job = db.query(BatchJob).filter(BatchJob.novel_id == novel_id).order_by(BatchJob.id.desc()).first()
    db.close()
    return job


def _stop_immediately(monkeypatch):
    """Make the very first stop-check inside the target function's loop fire.

    Pre-claiming the batch via _set_batch() before calling the function
    doesn't work: the function makes its OWN _set_batch() call, which would
    then see an already-running row and exit before ever reaching the loop.
    Monkeypatching the check itself exercises the real "stop was requested"
    code path without that ordering conflict."""
    monkeypatch.setattr(app_module, "_batch_stop_requested", lambda novel_id: True)


class TestTranslateNovelMetaBgReleasesOnStop:
    """Bug 1: the old code returned before _finish_batch when stop was
    requested right after the batch was claimed."""

    def test_stop_right_after_claiming_the_batch_still_releases_it(self, client, monkeypatch):
        novel_id = client.post("/api/novels/manual",
                               json={"title": "Meta Stop", "source_url": "manual://meta-stop-1"}).json()["id"]

        class _FakeTranslator:
            def translate_short(self, *a, **k):
                raise AssertionError("must not be called — stop was requested before any work")

        monkeypatch.setattr(translator_module, "get_translator", lambda *a, **k: _FakeTranslator())
        _stop_immediately(monkeypatch)

        app_module.translate_novel_meta_bg(novel_id)

        job = _job_for(novel_id)
        assert job is not None
        assert job.running is False, "the job must be released, not left running=True until the watchdog frees it"
        assert job.current_label == "Stopped by user"


class TestRetranslateMatchBgHonorsStop:
    """Bug 2: no stop-check existed in this loop at all."""

    def test_stop_requested_before_the_loop_stops_it_immediately(self, client, monkeypatch):
        novel_id = _seed_novel(client, "Match Stop", "manual://match-stop-1", n_chapters=3, translated=True)
        db = SessionLocal()
        for ch in db.query(Chapter).filter(Chapter.novel_id == novel_id):
            ch.translated_content = "needle here"
        db.commit()
        db.close()

        _stop_immediately(monkeypatch)
        app_module.retranslate_match_bg(novel_id, "needle")

        job = _job_for(novel_id)
        assert job.running is False
        assert "Stopped by user" in job.current_label
        assert "0/3" in job.current_label, "no chapter should have been attempted"


class TestRetranslateDriftBgHonorsStopAndAuth:
    """Bug 3 (both halves): no stop-check, and RelayAuthError was swallowed
    by a bare `except Exception`, so a dead key looped through every
    drifted chapter instead of stopping on the first one."""

    def test_stop_requested_before_the_loop_stops_it_immediately(self, client, monkeypatch):
        novel_id = _seed_novel(client, "Drift Stop", "manual://drift-stop-1", n_chapters=3, translated=True)
        _stop_immediately(monkeypatch)
        app_module._retranslate_drift_bg(novel_id, [1, 2, 3])

        job = _job_for(novel_id)
        assert job.running is False
        assert "Stopped by user" in job.current_label
        assert "0/3" in job.current_label

    def test_relay_auth_error_stops_after_the_first_chapter_not_every_one(self, client, monkeypatch):
        novel_id = _seed_novel(client, "Drift Auth", "manual://drift-auth-1", n_chapters=3, translated=True)
        attempts = []

        def fake_translate(db, ch, quality, force=False):
            attempts.append(ch.chapter_number)
            raise RelayAuthError("key rejected")

        monkeypatch.setattr(app_module, "_translate_chapter", fake_translate)

        app_module._retranslate_drift_bg(novel_id, [1, 2, 3])

        assert attempts == [1], "must stop after the FIRST rejected-key failure, not try chapters 2 and 3 too"
        job = _job_for(novel_id)
        assert job.running is False
        assert "relay key rejected" in job.current_label


class TestTranslateTitlesBgHonorsAuth:
    """Bug 4: no RelayAuthError shortcut — a dead key made this attempt every
    remaining chapter's title individually instead of stopping once."""

    def test_relay_auth_error_stops_after_the_first_title_not_every_one(self, client, monkeypatch):
        novel_id = client.post("/api/novels/manual",
                               json={"title": "Titles Auth", "source_url": "manual://titles-auth-1"}).json()["id"]
        db = SessionLocal()
        for i in (1, 2, 3):
            db.add(Chapter(novel_id=novel_id, chapter_number=i, title="ch%d" % i, is_translated=False))
        db.commit()
        db.close()

        attempts = []

        class _FakeTranslator:
            def translate_short(self, title, *a, **k):
                attempts.append(title)
                raise RelayAuthError("key rejected")

        monkeypatch.setattr(translator_module, "get_translator", lambda *a, **k: _FakeTranslator())

        app_module.translate_titles_bg(novel_id)

        assert attempts == ["ch1"], "must stop after the FIRST rejected-key failure, not try ch2 and ch3 too"
        job = _job_for(novel_id)
        assert job.running is False
        assert "relay key rejected" in job.current_label


class TestOutcomeLabelsOnCompletion:
    """A representative sweep: every batch kind ends with a human-readable,
    non-empty outcome label, and the job is released."""

    def test_export_epub_with_no_translated_chapters_reports_why(self, client):
        novel_id = _seed_novel(client, "Empty Epub", "manual://empty-epub-1", n_chapters=2, translated=False)
        app_module._export_epub_bg(novel_id)
        job = _job_for(novel_id)
        assert job.running is False
        assert job.current_label == "No translated chapters to export yet"

    def test_export_epub_with_chapters_reports_the_count(self, client):
        novel_id = _seed_novel(client, "Real Epub", "manual://real-epub-1", n_chapters=3, translated=True)
        app_module._export_epub_bg(novel_id)
        job = _job_for(novel_id)
        assert job.running is False
        assert "3 chapters" in job.current_label

    def test_translate_to_end_with_no_relay_key_still_reports_an_outcome(self, client):
        """No FALLBACK_API_KEY is configured (conftest scrubs it), so every
        chapter fails — but the job must still report a clean outcome
        instead of vanishing silently."""
        novel_id = _seed_novel(client, "No Key ToEnd", "manual://no-key-to-end-1", n_chapters=2, translated=False)
        app_module.translate_to_end_bg(novel_id)
        job = _job_for(novel_id)
        assert job.running is False
        assert job.current_label  # non-empty — some outcome was recorded
        assert "0/2" in job.current_label


class TestParagraphSplitInEpub:
    """The same single-newline-chapter bug fixed in the reader (frontend/lib/
    text.js) existed in the EPUB exporter too — a different implementation of
    the identical rule, never ported to this second copy."""

    def test_single_newline_chapter_splits_into_multiple_paragraphs(self):
        text = "Line one.\nLine two.\nLine three.\nLine four.\nLine five.\nLine six."
        paras = app_module._split_paragraphs(text)
        assert len(paras) == 6

    def test_blank_line_prose_still_works(self):
        text = "Wrapped\nline one.\n\nSecond para."
        paras = app_module._split_paragraphs(text)
        assert paras == ["Wrapped line one.", "Second para."]

    def test_epub_body_actually_uses_multiple_paragraph_tags_for_single_newline_content(self, client):
        novel_id = _seed_novel(client, "Epub Paragraphs", "manual://epub-paras-1", n_chapters=0)
        db = SessionLocal()
        db.add(Chapter(novel_id=novel_id, chapter_number=1, title="ch1",
                       translated_content="A.\nB.\nC.\nD.\nE.\nF.", is_translated=True))
        db.commit()
        db.close()

        app_module._export_epub_bg(novel_id)

        import zipfile
        path = app_module._epub_path(app_module_novel(novel_id))
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if "ch_" in n]  # ebooklib nests under EPUB/
            assert names, "expected at least one chapter xhtml in the epub"
            body = z.read(names[0]).decode("utf-8")
        assert body.count("<p>") >= 6, "single-newline content must render as multiple <p> blocks, not one"


def app_module_novel(novel_id):
    from models import Novel
    db = SessionLocal()
    try:
        return db.query(Novel).filter(Novel.id == novel_id).first()
    finally:
        db.close()
