"""
AIScraper — generic LLM-powered fallback scraper for sites with no plugin.

When no site-specific plugin matches a URL, this scraper:
  1. Fetches the page (browser UA, same rate-limit/retry machinery as BaseScraper)
  2. Strips scripts/styles/nav/footer boilerplate with BeautifulSoup
  3. Asks the relay LLM (deepseek-v4-flash by default) to extract structured data:
       - novel info:  {title, author, description, chapters: [{title, url}]}
       - chapter:     {title, content}
  4. Falls back to the quality tier (gpt-5.6-luna) if the first model fails.

This is the PUBLIC default — site plugins are optional and can be removed
(e.g. for a public repo) without breaking anything: unknown sites route here.
"""
import json
import logging
import re
from typing import Optional, Dict, Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, NovelInfo, ChapterData

logger = logging.getLogger(__name__)

EXTRACT_MODEL = "deepseek-v4-flash"       # cheap extraction tier
EXTRACT_MODEL_2 = "gpt-5.6-luna"          # quality fallback

# HTML tag blacklist for boilerplate removal
BOILERPLATE_TAGS = ["script", "style", "noscript", "iframe", "svg", "canvas",
                    "nav", "footer", "aside", "form", "button", "ins", "iframe"]
# Attribute hints that mark boilerplate (site-agnostic heuristics)
BOILERPLATE_HINTS = ["nav", "menu", "footer", "sidebar", "advert", "cookie",
                     "share", "comment", "related", "recommend", "breadcrumb",
                     "header", "banner", "popup", "modal", "login", "signup"]
# Elements likely to contain the main content on unknown sites
CONTENT_HINTS = ["article", "content", "chapter", "novel-text", "read-content",
                 "chapter-content", "main-text", "story", "book-content", "txt"]

MAX_TEXT_CHARS = 18000   # prompt budget for extraction (roughly 4.5k tokens)


def _strip_boilerplate(soup: BeautifulSoup) -> BeautifulSoup:
    """Remove obvious non-content nodes before extraction."""
    for tag in BOILERPLATE_TAGS:
        for node in soup.find_all(tag):
            node.decompose()
    for node in soup.find_all(True):
        # Heuristic: elements whose id/class smells like chrome
        attrs = node.attrs or {}
        attrs_str = " ".join([
            str(attrs.get("id", "")),
            " ".join(attrs.get("class", []) if isinstance(attrs.get("class"), list) else []),
        ]).lower()
        if any(hint in attrs_str for hint in BOILERPLATE_HINTS) and len(node.get_text(" ", strip=True)) < 400:
            node.decompose()
    return soup


def _visible_text(soup: BeautifulSoup) -> str:
    """Flatten the soup to readable text with paragraph breaks.
    Chooses the best container: prefer a content-hinted element, else the
    block with the most chapter-like links, else the biggest text block."""
    candidates = soup.find_all(["article", "div", "main", "section", "ul", "ol"])
    best = None
    best_score = -1
    for c in candidates:
        attrs = c.attrs or {}
        cls = " ".join(attrs.get("class", []) if isinstance(attrs.get("class"), list) else []).lower()
        cid = str(attrs.get("id", "")).lower()
        hint = any(h in (cls + " " + cid) for h in CONTENT_HINTS)
        # count chapter-like links inside
        links = c.find_all("a", href=True)
        ch_links = [a for a in links if re.search(r"(chapter|ch[_-]?\d+|/ch/|/read/|vol)", a["href"], re.I)]
        link_score = min(len(ch_links), 50) * 2
        text_len = len(c.get_text("", strip=True))
        score = link_score + (min(text_len, 20000) // 1000) + (20 if hint else 0)
        if score > best_score:
            best_score = score
            best = c
    text = best.get_text("\n", strip=True) if best else soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:MAX_TEXT_CHARS]


class AIScraper(BaseScraper):
    """Fetches any URL and uses an LLM to extract novel/chapter structure."""

    name = "ai"
    source_site = "ai"

    def _get_llm(self):
        """Lazily build the relay extractor (avoids import-time coupling to backend).
        backend/ is on PYTHONPATH in both Docker (/app) and local runs."""
        from translator import OpenAIRelayTranslator
        return OpenAIRelayTranslator(model=EXTRACT_MODEL)

    async def _extract(self, prompt: str) -> Optional[Dict[str, Any]]:
        """Run an extraction prompt; try the cheap model, then the quality tier."""
        llm = self._get_llm()
        last_err = None
        for model in (EXTRACT_MODEL, EXTRACT_MODEL_2):
            try:
                llm.model_name = model
                raw = await self._call_llm(llm, prompt)
                if not raw:
                    last_err = "empty reply"
                    continue
                # The model may wrap JSON in fences or prose — find the object.
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if not m:
                    last_err = "no JSON object in reply"
                    continue
                data = json.loads(m.group(0))
                if isinstance(data, dict):
                    return data
            except Exception as e:
                last_err = str(e)
                logger.warning(f"AIScraper extract with {model} failed: {e}")
        logger.error(f"AIScraper extraction failed: {last_err}")
        return None

    async def _call_llm(self, llm, prompt: str) -> str:
        """Run the relay call in a thread (its _generate is sync/blocking)."""
        import asyncio
        return await asyncio.to_thread(llm._generate, prompt)

    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        html = await self._fetch(url)
        if not html:
            return None
        soup = self._parse_html(html)
        soup = _strip_boilerplate(soup)
        text = _visible_text(soup)
        if len(text) < 200:
            logger.warning(f"AIScraper: page too short to extract ({len(text)} chars) — may be JS-rendered")

        # Real chapter links found in the raw HTML (fallback + validator)
        real_links = []
        for m in re.finditer(r'href="([^"]+)"', html):
            href = m.group(1)
            if re.search(r"(chapter|/ch/|/read/)", href, re.I):
                real_links.append(urljoin(url, href))
        # de-dup preserving order
        seen = set()
        real_links = [l for l in real_links if not (l in seen or seen.add(l))]

        prompt = (
            "You are a web-novel site extractor. Below is the visible text of a novel "
            f"listing page fetched from {url}. Extract a JSON object with EXACTLY this shape:\n"
            '{"title": string, "author": string|null, "description": string, '
            '"chapters": [{"title": string, "url": string}]}\n'
            "Rules:\n"
            "- chapters: include EVERY chapter link you can find, in reading order; "
            "title WITHOUT the chapter number prefix (e.g. 'The Price of Development 7'), "
            "url as an ABSOLUTE url (resolve relative paths against the base).\n"
            "- If this page is not a novel page, return {\"title\":\"\", \"chapters\":[]}.\n"
            "- description: the synopsis/blurb, or empty string.\n"
            "- Output ONLY the JSON object, no prose, no markdown fences.\n\n"
            f"BASE URL: {url}\nPAGE TEXT:\n{text}"
        )
        data = await self._extract(prompt)
        if not data or not data.get("title"):
            return None
        llm_chapters = data.get("chapters", [])
        chapters = []
        # Trust real links over the LLM's URLs: models hallucinate/truncate URLs.
        # Pair real links with LLM titles by position when counts are comparable.
        if real_links and len(real_links) >= min(len(llm_chapters), 5):
            used = set()
            for i, real in enumerate(real_links):
                if real in used:
                    continue
                used.add(real)
                title = ""
                if i < len(llm_chapters):
                    title = str(llm_chapters[i].get("title", "")).strip()
                if not title:
                    # derive from the last path segment, humanized
                    slug = real.rstrip("/").split("/")[-1]
                    title = re.sub(r"[-_+]", " ", slug).strip() or f"Chapter {i + 1}"
                chapters.append(ChapterData(number=i + 1, title=title, url=real, content=""))
        else:
            for i, ch in enumerate(llm_chapters, start=1):
                ch_title = str(ch.get("title", "")).strip() or f"Chapter {i}"
                ch_url = str(ch.get("url", "")).strip()
                if not ch_url:
                    continue
                ch_url = urljoin(url, ch_url)
                chapters.append(ChapterData(number=i, title=ch_title, url=ch_url, content=""))
        return NovelInfo(
            title=str(data.get("title", "")).strip(),
            author=(data.get("author") or None),
            description=(data.get("description") or "").strip(),
            chapters=chapters,
            total_chapters=len(chapters),
        )

    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        html = await self._fetch(url)
        if not html:
            return None
        soup = self._parse_html(html)
        soup = _strip_boilerplate(soup)
        text = _visible_text(soup)
        prompt = (
            "You are a web-novel chapter extractor. Below is the visible text of a chapter "
            f"page fetched from {url}. Extract a JSON object with EXACTLY this shape:\n"
            '{"title": string, "content": string}\n'
            "Rules:\n"
            "- title: the chapter title WITHOUT a chapter-number prefix (e.g. 'The Price of Development 7').\n"
            "- content: the FULL chapter body text, preserving paragraph breaks as \\n\\n. "
            "Exclude navigation text, menus, ads, copyright notices, 'next chapter' links.\n"
            "- If this is not a chapter page, return {\"title\":\"\", \"content\":\"\"}.\n"
            "- Output ONLY the JSON object, no prose, no markdown fences.\n\n"
            f"PAGE TEXT:\n{text}"
        )
        data = await self._extract(prompt)
        if not data or not data.get("content"):
            return None
        content = str(data.get("content", "")).strip()
        title = str(data.get("title", "")).strip()
        return ChapterData(number=0, title=title, content=self._clean_content(content), url=url)
