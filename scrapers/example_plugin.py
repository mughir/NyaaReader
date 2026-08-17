"""
EXAMPLE PLUGIN — a site-specific scraper template (no real site).

HOW TO BUILD YOUR OWN (the whole workflow):
  1. Copy this file -> scrapers/<anything>.py
  2. Rename the class (e.g. MySiteScraper)
  3. Set `domains` to your site's real domain(s)   <-- THIS activates it
  4. Fill in the two methods with real selectors

No registry edits, no other files touched. The registry auto-discovers
every BaseScraper subclass in scrapers/*.py and reads its `domains`.

ACTIVATION:
  ACTIVE   = file exists in scrapers/  AND  `domains` is non-empty.
             Until then this plugin is INERT — it is skipped by discovery
             and can never run or error, so the app works out of the box.
  DEACTIVE = delete the file (or empty `domains`). That domain falls back
             to AIScraper.

You only need to implement TWO async methods; `BaseScraper` gives you
fetching, retries, rate-limiting and HTML parsing for free.
"""
from typing import Optional

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, NovelInfo, ChapterData


class YourSiteScraper(BaseScraper):
    """Template scraper — replace the class name (keep the `Scraper` suffix)."""

    # The domain(s) this plugin handles — THIS is what activates it.
    # Set to your real domain(s) (e.g. ["mysite.com"]) and the plugin turns on.
    # Empty = inactive: discovery skips this plugin, so it can never run
    # or error until you fill this in.
    domains = []

    # Identifier used in logs / batch labels.
    name = "example"
    # Stored on novels scraped by this plugin (shows in the library meta).
    source_site = "example.com"

    # ------------------------------------------------------------------
    # 1) Novel listing page -> metadata + chapter list
    # ------------------------------------------------------------------
    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        html = await self._fetch(url)          # GET with retries + rate limit
        if not html:
            return None
        soup = self._parse_html(html)          # BeautifulSoup (lxml), ready to use

        # --- FILL IN: extract from `soup` (site-specific selectors) ---
        title = self._pick_title(soup)
        if not title:
            return None                        # not a novel page -> None

        chapters = []
        for i, a_tag in enumerate(self._pick_chapter_links(soup), start=1):
            href = a_tag.get("href", "")
            if not href:
                continue
            chapters.append(ChapterData(
                number=i,
                title=a_tag.get_text(strip=True) or f"Chapter {i}",
                url=href,                      # ABSOLUTE url (use urljoin if relative)
                content="",                    # content fetched separately
            ))

        return NovelInfo(
            title=title,
            author=self._pick_author(soup),    # or None
            description=self._pick_description(soup),  # or ""
            chapters=chapters,
            total_chapters=len(chapters),
        )

    # ------------------------------------------------------------------
    # 2) Chapter page -> chapter text
    # ------------------------------------------------------------------
    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        html = await self._fetch(url)
        if not html:
            return None
        soup = self._parse_html(html)

        # --- FILL IN: extract the main text node(s) ---
        content_node = soup.select_one(".chapter-content")   # example selector
        if not content_node:
            return None
        content = content_node.get_text("\n", strip=True)   # paragraph-per-line

        return ChapterData(
            number=0,                          # filled in by the caller pipeline
            title=self._pick_chapter_title(soup) or "",
            content=self._clean_content(content),  # BaseScraper helper
            url=url,
        )

    # ------------------------------------------------------------------
    # Helper extractors — these are EXAMPLES; replace selectors per site.
    # Tip: inspect the site's HTML (DevTools) to find real selectors.
    # ------------------------------------------------------------------
    def _pick_title(self, soup: BeautifulSoup) -> Optional[str]:
        # Example: <h1 class="novel-title">Title</h1>
        h = soup.select_one("h1.novel-title")
        return h.get_text(strip=True) if h else None

    def _pick_author(self, soup: BeautifulSoup) -> Optional[str]:
        # Example: <span class="author-name">Name</span>
        s = soup.select_one("span.author-name")
        return s.get_text(strip=True) if s else None

    def _pick_description(self, soup: BeautifulSoup) -> str:
        # Example: <div id="synopsis">long text…</div>
        d = soup.select_one("#synopsis")
        return d.get_text(" ", strip=True) if d else ""

    def _pick_chapter_links(self, soup: BeautifulSoup):
        # Example: <a class="chapter-item" href="/novel/.../ch/1">Ch 1</a>
        # Return an iterable of <a> tags, in reading order.
        return soup.select("a.chapter-item")

    def _pick_chapter_title(self, soup: BeautifulSoup) -> Optional[str]:
        # Example: <h2 class="chapter-title">…</h2>
        h = soup.select_one("h2.chapter-title")
        return h.get_text(strip=True) if h else None


# Keep a module-level alias so the registry line reads naturally:
#   "example.com": YourSiteScraper
