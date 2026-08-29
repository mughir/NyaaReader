"""
Stage 4: has_content/original_content payload-shape unification. The
server-rendered novel page embeds a boolean `has_content` per chapter (it
never ships full chapter bodies into the initial HTML payload); the JSON API
(/api/novels/{id}/chapters and /api/novels/{id}/chapters/{n}) only ever
carried the full `original_content` string, with no `has_content` key at all.
frontend/lib/text.js's hasContent() helper papered over the two shapes on the
client; this closes the gap at the source instead, so both shapes carry the
same key and a future caller doesn't have to know which endpoint it hit.
"""


def test_list_chapters_json_includes_has_content_matching_original_content(client):
    novel_id = client.post("/api/novels/manual",
                           json={"title": "Has Content Check", "source_url": "manual://has-content-1"}).json()["id"]
    client.post("/api/novels/%d/chapters/manual" % novel_id, json={"content": "some real body text"})

    chapters = client.get("/api/novels/%d/chapters" % novel_id).json()
    assert len(chapters) == 1
    assert chapters[0]["has_content"] is True
    assert chapters[0]["original_content"] == "some real body text"


def test_get_chapter_json_has_content_is_false_when_empty(client):
    novel_id = client.post("/api/novels/manual",
                           json={"title": "Has Content Check 2", "source_url": "manual://has-content-2"}).json()["id"]
    from database import SessionLocal
    from models import Chapter
    db = SessionLocal()
    db.add(Chapter(novel_id=novel_id, chapter_number=1, title="ch1", source_url="manual://has-content-2/ch1"))
    db.commit()
    db.close()

    ch = client.get("/api/novels/%d/chapters/1" % novel_id).json()
    assert ch["has_content"] is False
    assert not ch["original_content"]
