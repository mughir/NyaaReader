"""
backend/main.py's search + retranslate-match: SQLAlchemy's .contains() builds
a `LIKE '%<text>%'` clause. Without autoescape=True, a needle containing % or
_ acts as a SQL wildcard, so a search/retranslate for e.g. "HP%" matched every
chapter merely containing "HP" — and for retranslate-match, billed for
re-translating all of them.
"""
from database import SessionLocal
from models import Chapter


def _seed_chapters(client, title, source_url, bodies):
    novel_id = client.post("/api/novels/manual", json={"title": title, "source_url": source_url}).json()["id"]
    db = SessionLocal()
    for i, body in enumerate(bodies, start=1):
        db.add(Chapter(novel_id=novel_id, chapter_number=i, title="ch%d" % i,
                       original_content=body, translated_content=body, is_translated=True))
    db.commit()
    db.close()
    return novel_id


class TestSearchEscapesWildcards:
    def test_percent_in_the_query_matches_only_the_literal(self, client):
        novel_id = _seed_chapters(client, "Wildcard Search", "manual://wildcard-search-1", [
            "her HP% meter glowed",     # literal "HP%" -> should match
            "HP dropped sharply",       # only "HP" -> should NOT match
            "the HPX reading rose",     # "HP" + any char -> should NOT match
        ])
        res = client.post("/api/novels/%d/search" % novel_id, json={"q": "HP%"}).json()["results"]
        assert [r["chapter_number"] for r in res] == [1], \
            "an unescaped '%' would also match chapters 2 and 3"


class TestRetranslateMatchEscapesWildcards:
    def test_percent_in_the_needle_targets_only_the_literal_match(self, client):
        novel_id = _seed_chapters(client, "Wildcard Retranslate", "manual://wildcard-retranslate-1", [
            "her HP% meter glowed",
            "HP dropped sharply",
            "the HPX reading rose",
        ])
        r = client.post("/api/novels/%d/retranslate-match" % novel_id, json={"needle": "HP%"})
        assert r.json().get("pending") == 1, \
            "unescaped, this needle would target 3 chapters instead of the 1 that literally contains it"
