"""
add_chapter_manual had the same autoflush-before-flush undercount as
check_updates_bg: total_chapters was counted before the pending insert was
flushed, so it never advanced past the pre-insert number.
"""


def test_total_chapters_advances_with_each_manually_added_chapter(client):
    novel_id = client.post("/api/novels/manual",
                           json={"title": "Manual Chapters", "source_url": "manual://manual-chapters-1"}).json()["id"]

    for i in (1, 2, 3):
        client.post("/api/novels/%d/chapters/manual" % novel_id, json={"content": "body %d" % i})
        nv = client.get("/api/novels/%d" % novel_id).json()
        assert nv["total_chapters"] == i, \
            "total_chapters must reflect this chapter, not lag one insert behind"
