"""
AIScraper -- what happens on a site with no hand-written plugin.

Three tiers, cheapest first:

  1. LEARNED SPEC (the normal path). If this domain has a cached spec, run it
     through the ordinary declarative engine in scrapers/spec.py. Zero LLM
     calls, deterministic, paginates like any other plugin.
  2. INFERENCE (once per site). No spec yet -- or the cached one no longer
     matches because the site was redesigned -- so send the LLM a STRUCTURAL
     DIGEST of the page, validate whatever selectors come back against that
     same page, and cache them only if they hold up. Then continue at tier 1.
  3. TEXT EXTRACTION (last resort). Inference failed or validated badly: fall
     back to asking the model to read the page text directly. Correct but slow
     and billed per chapter.

Tier 1 is what makes an unknown site cheap. Tier 3 used to be the ONLY path,
which meant every chapter of every plugin-less novel cost an LLM call forever.
"""
import json
import logging
import os
import re
from typing import Optional, Dict, Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, NovelInfo, ChapterData
from scrapers import learn

logger = logging.getLogger(__name__)

# Defaults only. The models actually used come from the user's Settings, which
# the backend pushes into these env vars (FALLBACK_MODEL / FALLBACK_MODEL_2 —
# the same pair the translator reads). See _extract_models().
EXTRACT_MODEL = "deepseek-v4-flash"       # cheap extraction tier
EXTRACT_MODEL_2 = "gpt-5.6-luna"          # quality fallback


def _extract_models() -> list:
    """The ordered list of models to try, resolved from user settings at call
    time. Falls back to the module defaults when Settings/env are unset."""
    primary = (os.getenv("FALLBACK_MODEL") or "").strip() or EXTRACT_MODEL
    secondary = (os.getenv("FALLBACK_MODEL_2") or "").strip() or EXTRACT_MODEL_2
    models = [primary]
    if secondary and secondary != primary:
        models.append(secondary)
    return models

# HTML tag blacklist for boilerplate removal
BOILERPLATE_TAGS = ["script", "style", "noscript", "iframe", "svg", "canvas",
                    "nav", "footer", "aside", "form", "button", "ins"]
# Attribute hints that mark boilerplate (site-agnostic heuristics)
BOILERPLATE_HINTS = ["nav", "menu", "footer", "sidebar", "advert", "cookie",
                     "share", "comment", "related", "recommend", "breadcrumb",
                     "header", "banner", "popup", "modal", "login", "signup"]
# Elements likely to contain the main content on unknown sites
CONTENT_HINTS = ["article", "content", "chapter", "novel-text", "read-content",
                 "chapter-content", "main-text", "story", "book-content", "txt"]

MAX_TEXT_CHARS = 18000   # prompt budget per extraction pass (roughly 4.5k tokens)
MAX_EXTRACT_PASSES = 6   # cost cap: at most ~108k chars of chapter body

# CJK ranges, used for word counting and source-language detection
_CJK_RE = re.compile(r"[一-鿿぀-ゟ゠-ヿ가-힯]")


def _count_words(text: str) -> int:
    """CJK scripts have no spaces: count characters there, words elsewhere."""
    cjk = len(_CJK_RE.findall(text or ""))
    return cjk if cjk else len((text or "").split())


def _chunk_for_extraction(text: str, limit: int) -> list:
    """Split long body text into <=limit chunks at paragraph boundaries."""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks, current = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:          # a single monster paragraph
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:limit])
            para = para[limit:]
        if len(current) + len(para) + 2 <= limit:
            current += ("\n\n" if current else "") + para
        else:
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


def _class_tokens(node) -> set:
    """Whole class names + id. Deliberately NOT substrings: matching substrings
    made the CSS utility class `is-justify-content-center` count as a 'content'
    hint, which let a 25-character div outrank a 540-chapter list."""
    attrs = getattr(node, "attrs", None) or {}     # can be None on some nodes
    cls = attrs.get("class") or []
    if not isinstance(cls, list):
        cls = [str(cls)]
    toks = {str(c).lower() for c in cls}
    if attrs.get("id"):
        toks.add(str(attrs["id"]).lower())
    return toks


def _strip_boilerplate(soup: BeautifulSoup) -> BeautifulSoup:
    """Remove obvious non-content nodes before extraction."""
    for tag in BOILERPLATE_TAGS:
        for node in soup.find_all(tag):
            node.decompose()
    for node in soup.find_all(True):
        toks = _class_tokens(node)
        if not toks:
            continue
        if any(h in toks for h in BOILERPLATE_HINTS) and len(node.get_text(" ", strip=True)) < 400:
            node.decompose()
    return soup


def _visible_text(soup: BeautifulSoup, limit: int = MAX_TEXT_CHARS) -> str:
    """Flatten the soup to readable text, preferring the main content block.

    Scoring note: the content-hint bonus is deliberately much SMALLER than the
    text-length term. When both capped at 20, any tiny hinted element tied with
    a full chapter list and won on document order."""
    candidates = soup.find_all(["article", "div", "main", "section", "ul", "ol"])
    best, best_score = None, -1
    for c in candidates:
        hint = any(h in _class_tokens(c) for h in CONTENT_HINTS)
        text_len = len(c.get_text("", strip=True))
        score = (min(text_len, 40000) // 1000) + (6 if hint else 0)
        if score > best_score:
            best_score, best = score, c
    text = best.get_text("\n", strip=True) if best else soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:limit] if limit else text


class AIScraper(BaseScraper):
    """Learned-spec first, inference second, raw LLM text extraction last."""

    name = "ai"
    source_site = "ai"

    # ---------------------------------------------------------------- LLM
    def _get_llm(self):
        """Lazily build the relay extractor (avoids import-time coupling to
        backend). backend/ is on PYTHONPATH in Docker and local runs alike."""
        from translator import OpenAIRelayTranslator
        return OpenAIRelayTranslator(model=_extract_models()[0])

    async def _call_llm(self, llm, prompt: str) -> str:
        """Run the relay call in a thread (its _generate is sync/blocking)."""
        import asyncio
        return await asyncio.to_thread(llm._generate, prompt)

    async def _extract(self, prompt: str) -> Optional[Dict[str, Any]]:
        """Run an extraction prompt; try the cheap model, then the quality tier."""
        llm = self._get_llm()
        last_err = None
        for model in _extract_models():
            try:
                llm.model_name = model
                raw = await self._call_llm(llm, prompt)
                if not raw:
                    last_err = "empty reply"
                    continue
                data = learn.parse_spec_reply(raw)
                if data is not None:
                    return data
                last_err = "no JSON object in reply"
            except Exception as e:
                last_err = str(e)
                logger.warning("AIScraper extract with %s failed: %s" % (model, e))
        logger.error("AIScraper extraction failed: %s" % last_err)
        return None

    # ------------------------------------------------------- learned specs
    def _spawn(self, spec: dict, prefetched: dict = None):
        """A live engine-backed scraper for `spec`, sharing our HTTP session and
        reusing any page we have already downloaded."""
        s = learn.build_scraper(spec, delay=self.delay, max_retries=self.max_retries)
        s.session = self.session      # reuse the open session; no second __aenter__
        s.timeout = self.timeout
        s.last_request_time = self.last_request_time
        s.prefetched = dict(prefetched or {})
        return s

    async def _learned_listing(self, url: str, soup, html: str = None):
        """Tier 1 + 2 for a listing page. Returns a ready scraper, or None."""
        pre = {url: html} if html else None
        spec = learn.load_spec(url)
        if spec:
            scraper = self._spawn(spec, pre)
            ok, why, _ = learn.validate_listing(scraper, soup, url)
            if ok:
                logger.info("using learned spec for %s (%s)" % (urlparse(url).netloc, why))
                return scraper
            # Site redesign (or a bad guess we cached): drop it and re-learn.
            logger.warning("cached spec for %s no longer matches (%s) -- re-inferring"
                           % (urlparse(url).netloc, why))
            learn.forget_spec(url)

        digest = learn.digest_listing(soup, url)
        proposal = await self._extract(learn.LISTING_PROMPT % json.dumps(digest, ensure_ascii=False))
        if not proposal:
            return None
        scraper = self._spawn(proposal, pre)
        ok, why, n = learn.validate_listing(scraper, soup, url)
        if not ok:
            logger.warning("inferred spec for %s rejected: %s" % (urlparse(url).netloc, why))
            return None
        logger.info("inferred a site spec for %s: %s" % (urlparse(url).netloc, why))
        learn.save_spec(url, proposal)
        return scraper

    async def _learned_chapter(self, url: str, soup, html: str = None):
        """Tier 1 + 2 for a chapter page. Returns a ready scraper, or None."""
        pre = {url: html} if html else None
        spec = learn.load_spec(url) or {}
        if spec.get("content"):
            scraper = self._spawn(spec, pre)
            ok, why, _ = learn.validate_content(scraper, soup)
            if ok:
                return scraper
            logger.warning("learned content selector for %s stopped working (%s)"
                           % (urlparse(url).netloc, why))
            spec = dict(spec)
            spec.pop("content", None)

        digest = learn.digest_chapter(soup, url)
        proposal = await self._extract(learn.CHAPTER_PROMPT % json.dumps(digest, ensure_ascii=False))
        if not proposal:
            return None
        merged = dict(spec)
        merged.update({k: v for k, v in proposal.items() if v})
        scraper = self._spawn(merged, pre)
        ok, why, _ = learn.validate_content(scraper, soup)
        if not ok:
            logger.warning("inferred content selector for %s rejected: %s"
                           % (urlparse(url).netloc, why))
            return None
        logger.info("learned a content selector for %s: %s" % (urlparse(url).netloc, why))
        learn.save_spec(url, merged)
        return scraper

    # --------------------------------------------------------- public API
    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        html = await self._fetch(url)
        if not html:
            return None
        soup = self._parse_html(html)

        learned = await self._learned_listing(url, soup, html)
        if learned is not None:
            info = await learned.get_novel_info(url)
            if info and info.chapters:
                return info
            logger.warning("learned spec produced nothing for %s -- using text extraction" % url)

        return await self._llm_novel_info(url, self._parse_html(html))

    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        html = await self._fetch(url)
        if not html:
            return None
        soup = self._parse_html(html)

        learned = await self._learned_chapter(url, soup, html)
        if learned is not None:
            ch = await learned.get_chapter_content(url)
            if ch and ch.content:
                return ch
            logger.warning("learned spec produced no text for %s -- using text extraction" % url)

        return await self._llm_chapter(url, self._parse_html(html))

    # --------------------------------------------- tier 3: text extraction
    async def _llm_novel_info(self, url: str, soup) -> Optional[NovelInfo]:
        soup = _strip_boilerplate(soup)
        text = _visible_text(soup)
        if len(text) < 200:
            logger.warning("AIScraper: page too short to extract (%d chars) -- may be JS-rendered"
                           % len(text))

        # Real chapter links, found STRUCTURALLY (the largest cluster of
        # same-shaped hrefs). The old keyword regex -- chapter|/ch/|/read/ --
        # matched 0 of 540 links on a real listing page, because real chapter
        # URLs frequently look like /1234567/8096_1.html or /n2267be/1/.
        clusters = learn.link_clusters(soup, limit=1)
        real_links = []
        if clusters:
            seen = set()
            for a in clusters[0]["links"]:
                absolute = urljoin(url, a.get("href"))
                if absolute not in seen:
                    seen.add(absolute)
                    real_links.append((a.get_text(strip=True), absolute))

        prompt = (
            "You are a web-novel site extractor. Below is the visible text of a novel "
            "listing page fetched from %s. Extract a JSON object with EXACTLY this shape:\n"
            '{"title": string, "author": string|null, "description": string, '
            '"chapters": [{"title": string, "url": string}]}\n'
            "Rules:\n"
            "- chapters: include EVERY chapter link you can find, in reading order; "
            "title WITHOUT the chapter number prefix; url as an ABSOLUTE url.\n"
            '- If this is not a novel page, return {"title":"", "chapters":[]}.\n'
            "- description: the synopsis/blurb, or empty string.\n"
            "- Output ONLY the JSON object, no prose, no markdown fences.\n\n"
            "BASE URL: %s\nPAGE TEXT:\n%s" % (url, url, text)
        )
        data = await self._extract(prompt)
        if not data or not data.get("title"):
            return None
        llm_chapters = data.get("chapters", []) or []

        chapters = []
        # Trust the structurally-found links over the model's URLs: models
        # hallucinate and truncate hrefs. Pair them with model titles by
        # position when the counts are comparable.
        if real_links and len(real_links) >= min(len(llm_chapters), 5):
            for i, (link_text, real) in enumerate(real_links):
                title = ""
                if i < len(llm_chapters):
                    title = str(llm_chapters[i].get("title", "")).strip()
                title = title or link_text or ""
                if not title:
                    slug = real.rstrip("/").split("/")[-1]
                    title = re.sub(r"[-_+]", " ", slug).strip() or ("Chapter %d" % (i + 1))
                chapters.append(ChapterData(number=i + 1, title=title, url=real, content=""))
        else:
            for i, ch in enumerate(llm_chapters, start=1):
                ch_url = str(ch.get("url", "")).strip()
                if not ch_url:
                    continue
                ch_title = str(ch.get("title", "")).strip() or ("Chapter %d" % i)
                chapters.append(ChapterData(number=i, title=ch_title,
                                            url=urljoin(url, ch_url), content=""))

        lang_sample = " ".join([
            str(data.get("title", "")), str(data.get("description", "")),
            " ".join(c.title or "" for c in chapters[:30]),
        ]).strip()
        return NovelInfo(
            title=str(data.get("title", "")).strip(),
            author=(data.get("author") or None),
            description=(data.get("description") or "").strip(),
            chapters=chapters,
            original_language=self._detect_language(lang_sample or text),
            total_chapters=len(chapters),
        )

    async def _llm_chapter(self, url: str, soup) -> Optional[ChapterData]:
        soup = _strip_boilerplate(soup)
        # Take the FULL body, then extract it in passes. Truncating to
        # MAX_TEXT_CHARS here silently stored long chapters with their tail
        # missing -- the reader just showed a chapter that stopped mid-scene.
        full = _visible_text(soup, limit=0)
        chunks = _chunk_for_extraction(full, MAX_TEXT_CHARS)
        if len(chunks) > MAX_EXTRACT_PASSES:
            logger.warning("AIScraper: chapter at %s is %d chars -- extracting only the "
                           "first %d of %d passes (cost cap)"
                           % (url, len(full), MAX_EXTRACT_PASSES, len(chunks)))
            chunks = chunks[:MAX_EXTRACT_PASSES]
        elif len(chunks) > 1:
            logger.info("AIScraper: chapter is %d chars -- extracting in %d passes"
                        % (len(full), len(chunks)))

        title, bodies = "", []
        for i, chunk in enumerate(chunks):
            part_note = ""
            if len(chunks) > 1:
                part_note = ("\nNOTE: this is part %d of %d of one chapter. Extract ONLY the "
                             "body text present below; do not summarise, do not add an "
                             "introduction, and do not repeat text from other parts."
                             % (i + 1, len(chunks)))
            prompt = (
                "You are a web-novel chapter extractor. Below is the visible text of a chapter "
                "page fetched from %s. Extract a JSON object with EXACTLY this shape:\n"
                '{"title": string, "content": string}\n'
                "Rules:\n"
                "- title: the chapter title WITHOUT a chapter-number prefix.\n"
                "- content: the FULL chapter body text, preserving paragraph breaks as \\n\\n. "
                "Exclude navigation text, menus, ads, copyright notices, 'next chapter' links.\n"
                '- If this is not a chapter page, return {"title":"", "content":""}.\n'
                "- Output ONLY the JSON object, no prose, no markdown fences.%s\n\n"
                "PAGE TEXT:\n%s" % (url, part_note, chunk)
            )
            data = await self._extract(prompt)
            if not data:
                if i == 0:
                    return None
                logger.warning("AIScraper: part %d/%d failed for %s -- keeping earlier parts"
                               % (i + 1, len(chunks), url))
                break
            body = str(data.get("content") or "").strip()
            if body:
                bodies.append(body)
            if i == 0:
                title = str(data.get("title") or "").strip()

        if not bodies:
            return None
        content = self._clean_content("\n\n".join(bodies))
        return ChapterData(number=0, title=title, content=content, url=url,
                           word_count=_count_words(content))
