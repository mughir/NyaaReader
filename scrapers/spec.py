"""
Declarative site-plugin engine.

A site plugin is a DECLARATION, not a scraper: you state where things are
(selectors, pagination rule, junk to drop) and this engine does the work --
fetching, following list pagination, de-duplicating, numbering, extracting
metadata and chapter text, counting words.

Why an engine at all: every hand-written plugin re-implemented listing,
de-duplication and numbering, and each one had to REMEMBER to paginate the
chapter list. Two of three did not, so ncode.syosetu.com silently capped at
100 chapters (~600 missing) and the AI fallback never paginated either.
Pagination is a framework concern here, so a plugin cannot forget it.

Minimal plugin:

    class MySite(SiteScraper):
        domains       = ["mysite.com"]
        language      = "zh"
        chapter_links = "ul.toc li a"
        content       = ".chapter-body"

Selector mini-language, used by every field below:
    "css"                 -> the element's text
    "css@attr"            -> that attribute, e.g. "meta[property=og:image]@content"
    ["css a", "css b"]    -> try each in order, first hit wins
"""
import asyncio
import logging
import re
from typing import List, Optional, Sequence, Union
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse

from scrapers.base import BaseScraper, NovelInfo, ChapterData

logger = logging.getLogger(__name__)

SelectorSpec = Union[str, Sequence[str], None]

# Hard ceilings so a pagination rule that never terminates cannot loop forever.
MAX_LIST_PAGES = 200
MAX_CHAPTER_PARTS = 20

_CJK_RE = re.compile(r"[一-鿿぀-ゟ゠-ヿ가-힯]")


def count_words(text: str) -> int:
    """CJK has no spaces: count characters there, whitespace-words elsewhere."""
    cjk = len(_CJK_RE.findall(text or ""))
    return cjk if cjk else len((text or "").split())


def _as_list(spec: SelectorSpec) -> List[str]:
    if not spec:
        return []
    return [spec] if isinstance(spec, str) else list(spec)


def _split_attr(sel: str):
    """'div.x@href' -> ('div.x', 'href');  'div.x' -> ('div.x', None)."""
    if "@" in sel:
        css, attr = sel.rsplit("@", 1)
        # an attribute name never contains a space or a bracket; this keeps
        # CSS attribute selectors from being mistaken for the @attr suffix
        if css and " " not in attr and "]" not in attr:
            return css, attr
    return sel, None


def _alternatives(spec: SelectorSpec) -> List[str]:
    """Individual selectors, in priority order.

    A comma-joined string is split apart here so that each alternative can carry
    its own @attr -- ".a img@src, .b img@src" must not be read as one selector
    ending in '@src' (that leaves a stray '@' in the CSS and raises)."""
    out = []
    for item in _as_list(spec):
        out.extend(part.strip() for part in item.split(",") if part.strip())
    return out


def _select(soup, css: str) -> list:
    """soup.select, but a malformed selector in a plugin is a logged warning
    rather than an exception that aborts the whole scrape."""
    try:
        return soup.select(css)
    except Exception as e:
        logger.warning("bad selector %r: %s" % (css, e))
        return []


def pick(soup, spec: SelectorSpec) -> Optional[str]:
    """First non-empty text/attribute matching `spec`, else None.

    Alternatives are tried in the order written, so a spec lists selectors by
    PRIORITY (preferred markup first, legacy fallbacks after)."""
    for sel in _alternatives(spec):
        css, attr = _split_attr(sel)
        for node in _select(soup, css):
            val = node.get(attr) if attr else node.get_text(" ", strip=True)
            if val and str(val).strip():
                return str(val).strip()
    return None


def pick_nodes(soup, spec: SelectorSpec) -> list:
    """Every node matching `spec`.

    A comma-joined string is passed to the CSS engine whole, so matches come
    back in DOCUMENT order -- that is what a chapter list needs. Use a list
    instead when the order of the selectors themselves is what matters."""
    out = []
    for sel in _as_list(spec):
        css, _ = _split_attr(sel)
        out.extend(_select(soup, css))
    return out


def _classes(node) -> str:
    cls = node.get("class") or []
    if not isinstance(cls, list):
        cls = [str(cls)]
    return " ".join(cls + [str(node.get("id") or "")]).lower()


# ---------------------------------------------------------------------------
# Pagination strategies -- each one only has to answer "what is the next URL?".
# The engine stops on its own as soon as a page yields no NEW chapter links, so
# a strategy never needs to know how many pages exist.
# ---------------------------------------------------------------------------
class Pagination:
    def next_url(self, current_url: str, soup, page_no: int) -> Optional[str]:
        return None


class SinglePage(Pagination):
    """The whole chapter list is on one page (the default)."""


class QueryPage(Pagination):
    """Page number lives in a query parameter: ?p=2, ?page=3, ..."""

    def __init__(self, param: str = "p", start: int = 2, step: int = 1):
        self.param, self.start, self.step = param, start, step

    def next_url(self, current_url, soup, page_no):
        parts = urlparse(current_url)
        q = parse_qs(parts.query)
        q[self.param] = [str(self.start + (page_no - 1) * self.step)]
        return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


class NextLink(Pagination):
    """Follow a 'next page' anchor, e.g. NextLink("a.next") or
    NextLink(texts=["next"])."""

    def __init__(self, selector: str = "", texts: Sequence[str] = ()):
        self.selector, self.texts = selector, tuple(texts)

    def next_url(self, current_url, soup, page_no):
        nodes = soup.select(self.selector) if self.selector else soup.find_all("a", href=True)
        for a in nodes:
            if self.texts and a.get_text(strip=True) not in self.texts:
                continue
            href = a.get("href")
            if href:
                nxt = urljoin(current_url, href)
                if nxt != current_url:
                    return nxt
        return None


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class SiteScraper(BaseScraper):
    """Runs a declarative site spec. Subclass and set the class attributes."""

    domains: List[str] = []          # empty => inert, discovery skips it
    language = "zh"

    # --- locating pages -----------------------------------------------------
    # Optional regex with ONE group, matched against the URL PATH, yielding a
    # novel id for the templates below. Lets a plugin accept any URL on the site
    # (a chapter URL included) and still find the novel's own pages.
    novel_id_re: Optional[str] = None
    index_url: Optional[str] = None      # e.g. "/{novel_id}/"     (metadata page)
    listing_url: Optional[str] = None    # e.g. "/{novel_id}/dir"  (chapter list)

    # --- chapter list -------------------------------------------------------
    chapter_links: SelectorSpec = None   # REQUIRED: selector for the <a> elements
    paginate: Pagination = SinglePage()

    # --- novel metadata -----------------------------------------------------
    meta = {}            # {"title": spec, "author": spec, "desc": spec, "cover": spec}

    # --- chapter page -------------------------------------------------------
    content: SelectorSpec = None         # REQUIRED: the body node(s)
    chapter_title: SelectorSpec = None
    # Regex removed from the chapter title, e.g. a trailing part marker "(1/2)"
    # that a site appends when one chapter is split across pages.
    chapter_title_strip: Optional[str] = None
    chapter_number_re: Optional[str] = None   # one group, matched against the URL
    drop: Sequence[str] = ("script", "style", "ins", "iframe")
    drop_classes: Sequence[str] = ()          # skip body candidates with these in class/id
    drop_text_prefixes: Sequence[str] = ()    # remove descendants whose text starts with these

    # ------------------------------------------------------------------ utils
    def _base(self, url: str) -> str:
        p = urlparse(url)
        return p.scheme + "://" + p.netloc

    def novel_id(self, url: str) -> Optional[str]:
        if not self.novel_id_re:
            return None
        m = re.search(self.novel_id_re, urlparse(url).path)
        return m.group(1) if m else None

    def _template(self, template: Optional[str], url: str) -> Optional[str]:
        if not template:
            return None
        if "{novel_id}" in template:
            nid = self.novel_id(url)
            if not nid:
                return None
            template = template.replace("{novel_id}", nid)
        return urljoin(self._base(url), template)

    # {url: html} for pages the CALLER already downloaded. Consumed once each, so
    # a wrapper that had to fetch a page in order to decide which scraper to use
    # does not make the source serve it twice.
    prefetched: Optional[dict] = None

    async def _soup(self, url: str):
        html = None
        if self.prefetched:
            html = self.prefetched.pop(url, None)
        if html is None:
            html = await self._fetch(url)
        return self._parse_html(html) if html else None

    # -------------------------------------------------------------- listing
    def _links_on(self, soup, page_url: str):
        """(title, absolute_url) for every chapter anchor on one listing page."""
        out = []
        for a in pick_nodes(soup, self.chapter_links):
            href = a.get("href")
            title = a.get_text(strip=True)
            if not href or not title:
                continue
            out.append((title, urljoin(page_url, href)))
        return out

    async def collect_chapter_links(self, listing_url: str, first_soup=None):
        """Walk every listing page, de-duplicating by URL and preserving order.

        Stops as soon as a page contributes no new links. That one rule covers a
        strategy running past the last page, a site that clamps ?p=999 back to
        the last page, and a 'next' link that points at itself."""
        seen, links = set(), []
        page_url, soup, page_no = listing_url, first_soup, 1
        while page_no <= MAX_LIST_PAGES:
            if soup is None:
                soup = await self._soup(page_url)
            if soup is None:
                break
            fresh = [(t, u) for (t, u) in self._links_on(soup, page_url) if u not in seen]
            for t, u in fresh:
                seen.add(u)
                links.append((t, u))
            if not fresh:
                break
            nxt = self.paginate.next_url(page_url, soup, page_no)
            if not nxt or nxt == page_url:
                break
            page_url, soup, page_no = nxt, None, page_no + 1
        if page_no > 1:
            logger.info("%s: chapter list spanned %d page(s), %d chapters"
                        % (type(self).__name__, page_no, len(links)))
        return links

    # ------------------------------------------------------------- interface
    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        # A plugin whose templates need {novel_id} genuinely cannot locate the
        # novel's pages without one. Decline instead of silently falling back to
        # the pasted URL -- that path "succeeds" with a scraped title and ZERO
        # chapters, and the caller then creates an empty novel rather than
        # reporting a failed scrape.
        needs_id = any("{novel_id}" in (t or "") for t in (self.index_url, self.listing_url))
        if needs_id and not self.novel_id(url):
            logger.warning("%s: no novel id found in %s -- cannot locate the novel's pages"
                           % (type(self).__name__, url))
            return None

        index_url = self._template(self.index_url, url) or url
        listing_url = self._template(self.listing_url, url) or index_url

        if listing_url == index_url:
            index_soup = await self._soup(index_url)
            listing_soup = index_soup
        else:
            index_soup, listing_soup = await asyncio.gather(
                self._soup(index_url), self._soup(listing_url))
        if index_soup is None:
            logger.warning("%s: could not fetch %s" % (type(self).__name__, index_url))
            return None

        pairs = await self.collect_chapter_links(listing_url, listing_soup)
        chapters = [
            ChapterData(number=i, title=t or ("Chapter %d" % i), content="", url=u, word_count=0)
            for i, (t, u) in enumerate(pairs, start=1)
        ]
        cover = pick(index_soup, self.meta.get("cover"))
        # Fall back to the page's own heading/<title> when the declared selector
        # finds nothing. This matters most for LEARNED specs: their meta
        # selectors are inferred from one page, and the metadata may simply not
        # be on the page being listed (some sites keep og: tags on the novel's
        # index but the chapters on a separate directory page). "Unknown" is a
        # poor thing to store as a title.
        title = (pick(index_soup, self.meta.get("title"))
                 or pick(index_soup, ["h1", "title"])
                 or "Unknown")
        return NovelInfo(
            title=title,
            author=pick(index_soup, self.meta.get("author")),
            description=pick(index_soup, self.meta.get("desc")),
            cover_url=urljoin(index_url, cover) if cover else None,
            chapters=chapters,
            original_language=self.language,
            total_chapters=len(chapters),
        )

    def _body_node(self, soup):
        """Pick the chapter body, skipping preface/afterword-style notes.

        Candidates are tried in `content` order; a node whose class/id contains a
        `drop_classes` token is skipped, so a 48-char author's note can never win
        over a 5000-char chapter. If EVERY candidate is a note, keep the longest
        rather than returning nothing."""
        candidates = pick_nodes(soup, self.content)
        if not candidates:
            return None
        for node in candidates:
            cls = _classes(node)
            if any(bad in cls for bad in self.drop_classes):
                continue
            return node
        return max(candidates, key=lambda n: len(n.get_text("\n", strip=True)))

    def _text_of(self, node) -> str:
        if self.drop:
            for junk in node.select(", ".join(self.drop)):
                junk.decompose()
        for prefix in self.drop_text_prefixes:
            for el in node.select("span, p, div"):
                if el.get_text(" ", strip=True).startswith(prefix):
                    el.decompose()
        return self._clean_content(node.get_text("\n", strip=True))

    async def next_part_url(self, url: str, soup) -> Optional[str]:
        """Hook: URL of the NEXT PAGE OF THE SAME CHAPTER, or None.

        Only for sites that split one chapter across several pages. Default:
        chapters are never split."""
        return None

    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        parts, title, seen, current = [], "", set(), url
        while current and current not in seen and len(seen) < MAX_CHAPTER_PARTS:
            seen.add(current)
            soup = await self._soup(current)
            if soup is None:
                break
            if not title:
                title = pick(soup, self.chapter_title) or ""
            node = self._body_node(soup)
            if node is not None:
                text = self._text_of(node)
                if text:
                    parts.append(text)
            current = await self.next_part_url(current, soup)
        if not parts:
            return None
        content = "\n\n".join(parts)
        if title and self.chapter_title_strip:
            title = re.sub(self.chapter_title_strip, "", title).strip()
        number = 0
        if self.chapter_number_re:
            m = re.search(self.chapter_number_re, url)
            if m:
                number = int(m.group(1))
        return ChapterData(number=number, title=title, content=content, url=url,
                           word_count=count_words(content))
