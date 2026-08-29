"""
EXAMPLE PLUGIN — a site spec template (no real site).

HOW TO ADD A SITE (the whole workflow):
  1. Copy this file -> scrapers/<anything>.py
  2. Rename the class
  3. Set `domains` to your site's real domain(s)   <-- THIS activates it
  4. Fill in the selectors below

No registry edits, no other files touched. The registry auto-discovers every
BaseScraper subclass in scrapers/*.py and reads its `domains`.

ACTIVATION:
  ACTIVE   = file exists in scrapers/  AND  `domains` is non-empty.
             Until then the plugin is INERT — discovery skips it, so it can
             never run or error, and the app works out of the box.
  DEACTIVE = delete the file (or empty `domains`). That domain falls back
             to AIScraper.

You do NOT write fetching, pagination, de-duplication, numbering, retries or
word counting: the engine in scrapers/spec.py owns all of it. You only declare
WHERE things are. That split is deliberate — when every plugin hand-rolled its
own listing loop, two of three forgot to paginate the chapter list and silently
capped at one page (ncode.syosetu.com stored 100 of ~700 chapters).

SELECTOR MINI-LANGUAGE (used by every field):
    "css"                    -> that element's text
    "css@attr"               -> that attribute
    "css a, css b"           -> one CSS selector; matches come back in
                                DOCUMENT order (what a chapter list wants)
    ["css a", "css b"]       -> alternatives tried in PRIORITY order,
                                first hit wins (what metadata wants)
"""
from typing import Optional

from scrapers.spec import SiteScraper, QueryPage, NextLink, SinglePage


class YourSiteScraper(SiteScraper):
    """Template site spec — replace the class name (keep the `Scraper` suffix)."""

    # The domain(s) this plugin handles — THIS is what activates it.
    # Empty = inactive: discovery skips it, so it can never run or error.
    domains = []

    name = "example"              # identifier used in logs / batch labels
    source_site = "example.com"   # stored on novels scraped by this plugin
    language = "zh"               # zh | ja | ko — the SOURCE language

    # ------------------------------------------------------------------
    # 1) Finding the novel's pages
    # ------------------------------------------------------------------
    # OPTIONAL. Only needed when the chapter list lives at a different URL
    # than the one a user pastes (people paste chapter links). The regex runs
    # against the URL PATH and must have exactly one group.
    #
    #   novel_id_re = r"^/novel/(\d+)"
    #   index_url   = "/novel/{novel_id}"        # metadata page
    #   listing_url = "/novel/{novel_id}/toc"    # chapter list page
    #
    # Omit all three and the pasted URL is used for both.
    novel_id_re: Optional[str] = None
    index_url: Optional[str] = None
    listing_url: Optional[str] = None

    # ------------------------------------------------------------------
    # 2) The chapter list  (REQUIRED)
    # ------------------------------------------------------------------
    chapter_links = "ul.toc li a"

    # How the list paginates. Pick ONE:
    #   SinglePage()            — whole list on one page (default)
    #   QueryPage("p")          — ?p=2, ?p=3, ...
    #   QueryPage("page", start=1)
    #   NextLink("a.next")      — follow a "next page" anchor
    #   NextLink(texts=["次へ"]) — follow an anchor by its text
    # The engine stops by itself once a page adds no new links, so you never
    # need to know the page count.
    paginate = SinglePage()

    # ------------------------------------------------------------------
    # 3) Novel metadata — all optional, all fall back to None
    # ------------------------------------------------------------------
    meta = {
        "title": ["h1.novel-title", "meta[property='og:title']@content"],
        "author": "span.author-name",
        "desc": "#synopsis",
        "cover": "img.cover@src",
    }

    # ------------------------------------------------------------------
    # 4) The chapter page  (content is REQUIRED)
    # ------------------------------------------------------------------
    content = ".chapter-body"
    chapter_title = "h2.chapter-title"

    # Pull the chapter number out of the URL (one group). Optional.
    chapter_number_re: Optional[str] = None      # e.g. r"/chapter/(\d+)"
    # Strip junk from the chapter title, e.g. a "(1/2)" part marker. Optional.
    chapter_title_strip: Optional[str] = None

    # Nodes removed from the body before the text is taken.
    drop = ("script", "style", "ins", "iframe", ".ad")
    # Body CANDIDATES whose class/id contains any of these are skipped — use it
    # when a site puts an author's note in a node sharing the content class, so
    # a 48-char note can't be scraped instead of the 5000-char chapter.
    drop_classes = ()
    # Descendants whose text STARTS WITH any of these are removed (site notices).
    drop_text_prefixes = ()

    # ------------------------------------------------------------------
    # 5) Escape hatch — only if the site genuinely needs it
    # ------------------------------------------------------------------
    # Sites that split ONE chapter across several pages implement this to return
    # the next page OF THE SAME CHAPTER (return None to stop). The engine merges
    # the parts.
    #
    # async def next_part_url(self, url: str, soup) -> Optional[str]:
    #     return None
