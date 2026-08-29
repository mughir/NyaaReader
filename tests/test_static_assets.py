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


def test_pages_render(client):
    novel_id = client.post("/api/novels/manual",
                           json={"title": "Smoke Test Novel", "source_url": "manual://smoke-1"}).json()["id"]
    client.post("/api/novels/%d/chapters/manual" % novel_id, json={"content": "hello"})

    for path in ("/api/health", "/", "/dashboard", "/config", "/login",
                "/novel/%d" % novel_id, "/novel/%d/review" % novel_id,
                "/novel/%d/chapter/1" % novel_id):
        r = client.get(path)
        assert r.status_code == 200, path
