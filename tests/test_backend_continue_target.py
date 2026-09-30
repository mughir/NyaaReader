"""
The library card and the novel page both offer "Continue". They used to compute
the target independently and disagreed (library: last opened chapter; novel
page: next translated chapter after the highest read one — which skipped the
reader's real position whenever it was on an untranslated chapter). Both now
come from views._continue_chapter.
"""
import json
import re

from models import Chapter


def _seed(client, db_session, tag):
    novel_id = client.post("/api/novels/manual",
                           json={"title": "Continue " + tag, "source_url": "manual://continue-" + tag}).json()["id"]
    for n in range(1, 5):
        db_session.add(Chapter(novel_id=novel_id, chapter_number=n, title="ch%d" % n,
                               source_url="manual://continue-%s/%d" % (tag, n),
                               is_translated=(n <= 2)))
    db_session.commit()
    ids = {c.chapter_number: c.id for c in db_session.query(Chapter).filter(Chapter.novel_id == novel_id)}
    return novel_id, ids


def _library_target(client, novel_id):
    html = client.get("/").text
    data = json.loads(re.search(r"window\.__LIBRARY__ = (\[.*?\]);", html).group(1))
    card = next(d for d in data if d["id"] == novel_id)
    return (card["last_read"] or {}).get("chapter_number")


def _novel_page_target(client, novel_id):
    html = client.get("/novel/%d" % novel_id).text
    return int(re.search(r'"continue_chapter": (\d+|null)', html).group(1).replace("null", "0")) or None


def test_never_opened_has_no_continue_target(client, db_session):
    novel_id, _ = _seed(client, db_session, "a")
    assert _library_target(client, novel_id) is None
    assert _novel_page_target(client, novel_id) is None


def test_both_pages_point_at_last_opened_chapter_even_if_untranslated(client, db_session):
    novel_id, ids = _seed(client, db_session, "b")
    client.post("/api/novels/%d/progress" % novel_id,
                json={"chapter_id": ids[3], "scroll_position": 10, "percentage": 20})
    assert _library_target(client, novel_id) == 3
    assert _novel_page_target(client, novel_id) == 3


def test_finished_chapter_advances_to_next(client, db_session):
    novel_id, ids = _seed(client, db_session, "c")
    client.post("/api/novels/%d/progress" % novel_id,
                json={"chapter_id": ids[3], "scroll_position": 900, "percentage": 97})
    assert _library_target(client, novel_id) == 4
    assert _novel_page_target(client, novel_id) == 4
    # last chapter finished: nothing after it, stay put
    client.post("/api/novels/%d/progress" % novel_id,
                json={"chapter_id": ids[4], "scroll_position": 900, "percentage": 100})
    assert _library_target(client, novel_id) == 4
