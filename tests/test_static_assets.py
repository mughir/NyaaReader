"""
Smoke tests for the pages and static wiring the review touched — including
frontend/lib/text.js, which reader.js and novel.js now depend on and which
must actually be reachable at /static/lib/text.js (a StaticFiles subdirectory
mount, easy to get wrong silently).
"""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONTEND = os.path.join(_ROOT, "frontend")


def test_lib_text_js_is_served(client):
    r = client.get("/static/lib/text.js")
    assert r.status_code == 200
    assert "NyaaText" in r.text


def test_service_worker_shell_lists_lib_text_js():
    with open(os.path.join(_FRONTEND, "sw.js"), encoding="utf-8") as fh:
        sw = fh.read()
    assert "/static/lib/text.js" in sw


def test_reader_and_novel_js_reference_the_shared_lib():
    for name in ("reader.js", "novel.js"):
        with open(os.path.join(_FRONTEND, name), encoding="utf-8") as fh:
            src = fh.read()
        assert "window.NyaaText" in src, "%s must use the shared helpers, not a private copy" % name


def test_novel_page_surfaces_the_outcome_for_every_job_kind_not_just_updates():
    """Every batch kind now ends with a real outcome label, but the progress
    panel that used to be the only place showing current_label is
    v-if="batch.running" and unmounts the instant the job ends — that used to
    make every kind's completion invisible except "updates" (special-cased
    into the persistent `note` banner). Guards against re-narrowing this back
    to one kind."""
    with open(os.path.join(_FRONTEND, "novel.js"), encoding="utf-8") as fh:
        src = fh.read()
    assert 'batch.value.kind === "updates"' not in src or "CHANGES_CHAPTERS" in src or "changedChapters" in src, \
        "the outcome-to-note.value logic must not be narrowed back to the 'updates' kind alone"
    assert "batch.value.current_label" in src


def test_reader_completion_banner_shows_the_real_outcome_not_a_generic_phrase():
    """The post-completion banner discarded ahead.label (the job's real
    outcome) in favor of a static "<phase> finished" phrase."""
    with open(os.path.join(_FRONTEND, "reader.js"), encoding="utf-8") as fh:
        src = fh.read()
    assert "ahead.label ||" in src, \
        "the completion banner must prefer the real outcome label over the generic KIND_LABEL phrase"


def test_pages_render(client):
    novel_id = client.post("/api/novels/manual",
                           json={"title": "Smoke Test Novel", "source_url": "manual://smoke-1"}).json()["id"]
    client.post("/api/novels/%d/chapters/manual" % novel_id, json={"content": "hello"})

    for path in ("/api/health", "/", "/dashboard", "/config", "/login",
                "/novel/%d" % novel_id, "/novel/%d/review" % novel_id,
                "/novel/%d/chapter/1" % novel_id):
        r = client.get(path)
        assert r.status_code == 200, path
