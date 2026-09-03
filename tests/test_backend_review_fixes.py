"""Regression tests for the 2026-09 code-review fixes.

- Search snippets must be HTML-escaped (XSS-safe v-html): raw chapter HTML
  must not survive in `snippet`, only our own <mark> tag.
- _clear_batch must preserve the real done count (no fake done=total).
- combine.py must validate the vault before touching scrapers/ and must
  never overlay _test_*.py files.
"""
import sys

from database import SessionLocal
from models import BatchJob, Chapter


def _seed_novel_with_chapter(client, title, source_url, body):
    novel_id = client.post(
        "/api/novels/manual", json={"title": title, "source_url": source_url}
    ).json()["id"]
    db = SessionLocal()
    db.add(Chapter(novel_id=novel_id, chapter_number=1, title="ch1",
                   original_content=body, translated_content=body,
                   is_translated=True))
    db.commit()
    db.close()
    return novel_id


class TestSearchSnippetEscaping:
    def test_script_tag_in_chapter_is_escaped_not_executed(self, client):
        novel_id = _seed_novel_with_chapter(
            client, "XSS Search", "manual://xss-search-1",
            'hello <script>alert("x")</script> world hello again')
        res = client.post("/api/novels/%d/search" % novel_id,
                          json={"q": "hello"}).json()["results"]
        assert res, "expected at least one search hit"
        snippet = res[0]["snippet"]
        assert "<script" not in snippet, "raw chapter HTML leaked into snippet: %r" % snippet
        assert "&lt;script" in snippet, "chapter HTML was not escaped: %r" % snippet
        assert '<mark class="search-hl">' in snippet

    def test_query_with_html_is_escaped(self, client):
        novel_id = _seed_novel_with_chapter(
            client, "XSS Query", "manual://xss-search-2",
            "nothing relevant here <b>bold</b>")
        res = client.post("/api/novels/%d/search" % novel_id,
                          json={"q": "<b>"}).json()["results"]
        for r in res:
            assert "<b>" not in r["snippet"].replace('<mark class="search-hl">', "").replace("</mark>", "")


class TestClearBatchHonesty:
    def test_clear_preserves_real_done_count(self, client, db_session):
        novel_id = client.post(
            "/api/novels/manual",
            json={"title": "Clear Honesty", "source_url": "manual://clear-honesty-1"},
        ).json()["id"]
        job = BatchJob(novel_id=novel_id, kind="to-end", total=10, done=2,
                       current_label="ch3", running=True)
        db_session.add(job)
        db_session.commit()

        import services.job_service as js
        js._clear_batch(novel_id)

        db_session.expire_all()
        row = db_session.query(BatchJob).filter(
            BatchJob.novel_id == novel_id).order_by(BatchJob.id.desc()).first()
        assert row.running is False
        assert row.done == 2, "clear faked done=%r, expected real progress 2" % row.done


class TestCombinePaths:
    def test_private_files_excludes_test_files(self, tmp_path):
        import combine as combine_mod
        vault = tmp_path / "vault"
        (vault / "scrapers").mkdir(parents=True)
        (vault / "scrapers" / "private_foo.py").write_text("# ok")
        (vault / "scrapers" / "_test_foo.py").write_text("# dev-only")
        (vault / "scrapers" / "spec.py").write_text("# public")
        got = [f.name for f in combine_mod._private_files(vault)]
        assert got == ["private_foo.py"]

    def test_missing_vault_returns_2(self, tmp_path, monkeypatch):
        import combine as combine_mod
        monkeypatch.setattr(sys, "argv", ["combine.py", str(tmp_path / "nope")])
        assert combine_mod.main() == 2

    def test_empty_vault_returns_3_without_creating_noise(self, tmp_path, monkeypatch):
        import combine as combine_mod
        vault = tmp_path / "empty-vault"
        (vault / "scrapers").mkdir(parents=True)
        monkeypatch.setattr(sys, "argv", ["combine.py", str(vault)])
        assert combine_mod.main() == 3
