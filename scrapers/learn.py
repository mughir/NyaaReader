"""
Learned site specs: infer a plugin ONCE with the LLM, then run deterministically.

Pipeline for a site with no hand-written plugin:

    page -> structural DIGEST -> LLM proposes selectors -> VALIDATE against the
    real page -> cache the spec -> every later fetch runs scrapers/spec.py with
    zero LLM calls

Two design decisions, both taken because the old approach demonstrably failed:

1. The LLM is shown STRUCTURE, not text. AIScraper fed it flattened page text,
   which for a real 540-chapter listing came to 29 characters (the container
   heuristic picked a 25-char div whose Bulma utility class
   `is-justify-content-center` happens to contain the substring "content"). A
   digest of candidate containers -- tag, classes, link counts, sample hrefs --
   is both far smaller and actually sufficient to choose a selector.

2. Nothing is trusted until it is VALIDATED deterministically. A proposed spec
   must find a plausible chapter list on the page it was inferred from before it
   is cached, so a hallucinated selector fails immediately and loudly instead of
   silently yielding an empty novel.
"""
import json
import logging
import os
import re
from typing import Optional, Tuple
from urllib.parse import urlparse

from scrapers.spec import SiteScraper, QueryPage, NextLink, SinglePage

logger = logging.getLogger(__name__)

# A spec must find at least this many chapter links to be believable...
MIN_CHAPTERS = 3
# ...and at least this share of the biggest link cluster on the page, so a spec
# that grabs a "latest 12 chapters" sidebar instead of the full list is rejected
# (a real site has exactly that trap: a 12-link "latest" ul sitting beside the
# 540-link full list).
MIN_CLUSTER_SHARE = 0.5
MIN_CONTENT_CHARS = 200
# Above this share of the body text sitting inside <a>, it is navigation, not prose.
MAX_LINK_TEXT_SHARE = 0.3

_CONTAINER_TAGS = ["div", "ul", "ol", "section", "article", "main", "table", "dl"]


def _spec_dir() -> str:
    d = os.path.join(os.getenv("DATA_DIR", "data"), "site_specs")
    os.makedirs(d, exist_ok=True)
    return d


def _domain(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# Structural digest -- what the model actually gets to look at
# ---------------------------------------------------------------------------
def _sel_for(node) -> str:
    """A concrete CSS selector for this node, preferring id then classes."""
    if node.get("id"):
        return "%s#%s" % (node.name, node["id"])
    cls = [c for c in (node.get("class") or []) if c]
    if cls:
        return node.name + "".join("." + c for c in cls)
    return node.name


def _href_shape(href: str) -> str:
    """'/1234567/8096_12.html' -> '/#/#_#.html'. Chapter links in one list
    share a shape; site navigation does not."""
    h = (href or "").split("?")[0].split("#")[0]
    return re.sub(r"\d+", "#", h)


def link_clusters(soup, limit: int = 12) -> list:
    """Candidate chapter lists, best first.

    Ranked by the number of DISTINCT same-shaped links, then by tightest
    container. Both halves of that rule are load-bearing on a real listing
    page measured during development:

      div.container.px-3   556 links  (540 chapters + site nav)
      div.chaplist         552 links  (the 540 + a 'latest 12' box, dupes)
      ul.all               540 links  (exactly the chapter list)   <- want this

    Raw count would pick the nav wrapper. Same-shaped count still picks
    div.chaplist, whose first links are the NEWEST chapters (540, 539, 538) --
    which would number the whole novel backwards. Counting DISTINCT hrefs
    drops both to 540, and the tightest-container tiebreak then lands on
    ul.all."""
    rows = []
    for node in soup.find_all(_CONTAINER_TAGS):
        links = node.find_all("a", href=True)
        if len(links) < MIN_CHAPTERS:
            continue
        shapes = {}
        for a in links:
            shapes.setdefault(_href_shape(a.get("href")), []).append(a)
        shape, group = max(shapes.items(), key=lambda kv: len(set(a.get("href") for a in kv[1])))
        distinct = len(set(a.get("href") for a in group))
        if distinct < MIN_CHAPTERS:
            continue
        rows.append({"node": node, "total": len(links), "homogeneous": distinct,
                     "shape": shape, "links": group})
    rows.sort(key=lambda r: (-r["homogeneous"], r["total"]))
    out, seen = [], set()
    for r in rows:
        if r["homogeneous"] in seen:      # keep only the tightest per group size
            continue
        seen.add(r["homogeneous"])
        out.append(r)
        if len(out) >= limit:
            break
    return out


def best_cluster_size(soup) -> int:
    c = link_clusters(soup, limit=1)
    return c[0]["homogeneous"] if c else 0


def digest_listing(soup, url: str) -> dict:
    """Compact structural description of a chapter-listing page."""
    clusters = []
    for r in link_clusters(soup):
        clusters.append({
            "selector": _sel_for(r["node"]),
            "links": r["homogeneous"],
            "links_total_in_container": r["total"],
            "href_shape": r["shape"],
            "sample_hrefs": [a.get("href") for a in r["links"][:3]],
            "sample_texts": [a.get_text(strip=True)[:40] for a in r["links"][:3]],
        })
    metas = {}
    for m in soup.find_all("meta"):
        key = m.get("property") or m.get("name")
        if key and m.get("content") and len(metas) < 25:
            metas[key] = str(m["content"])[:100]
    heads = [h.get_text(strip=True)[:80] for h in soup.select("h1, h2")[:5]]
    # anchors that look like pagination
    pager = []
    for a in soup.find_all("a", href=True)[:400]:
        href, txt = a["href"], a.get_text(strip=True)[:12]
        if re.search(r"[?&](p|page|pn)=\d+", href) or a.get("rel") == ["next"]:
            pager.append({"href": href, "text": txt, "selector": _sel_for(a)})
        if len(pager) >= 6:
            break
    return {"url": url, "link_clusters": clusters, "meta_tags": metas,
            "headings": heads, "pagination_candidates": pager}


def digest_chapter(soup, url: str) -> dict:
    """Compact structural description of a chapter page."""
    blocks = []
    for node in soup.find_all(_CONTAINER_TAGS + ["pre"]):
        text = node.get_text(" ", strip=True)
        if len(text) < 100:
            continue
        link_len = sum(len(a.get_text(" ", strip=True)) for a in node.find_all("a"))
        blocks.append({
            "selector": _sel_for(node),
            "text_len": len(text),
            "link_text_share": round(link_len / max(len(text), 1), 2),
            "starts": text[:90],
        })
    blocks.sort(key=lambda b: -b["text_len"])
    heads = [h.get_text(strip=True)[:80] for h in soup.select("h1, h2, h3")[:5]]
    return {"url": url, "text_blocks": blocks[:10], "headings": heads}


# ---------------------------------------------------------------------------
# Validation -- deterministic, no LLM
# ---------------------------------------------------------------------------
def validate_listing(scraper: SiteScraper, soup, url: str) -> Tuple[bool, str, int]:
    """Does this spec actually find a chapter list on this page?"""
    if not scraper.chapter_links:
        return False, "no chapter_links selector", 0
    links = scraper._links_on(soup, url)
    n = len(links)
    if n < MIN_CHAPTERS:
        return False, "only %d chapter links (need %d)" % (n, MIN_CHAPTERS), n
    urls = [u for _, u in links]
    if len(set(urls)) < n * 0.9:
        return False, "chapter links are mostly duplicates", n
    host = _domain(url)
    offsite = [u for u in urls if _domain(u) != host]
    if len(offsite) > n * 0.2:
        return False, "%d/%d links point off-site" % (len(offsite), n), n
    best = best_cluster_size(soup)
    if best and n < best * MIN_CLUSTER_SHARE:
        return False, ("found %d links but the biggest cluster on the page has %d "
                       "-- probably matched a 'latest chapters' box" % (n, best)), n
    return True, "ok (%d chapters)" % n, n


def validate_content(scraper: SiteScraper, soup) -> Tuple[bool, str, int]:
    """Does this spec pull prose (not navigation) off a chapter page?"""
    if not scraper.content:
        return False, "no content selector", 0
    node = scraper._body_node(soup)
    if node is None:
        return False, "content selector matched nothing", 0
    text = node.get_text(" ", strip=True)
    if len(text) < MIN_CONTENT_CHARS:
        return False, "content is only %d chars" % len(text), len(text)
    link_len = sum(len(a.get_text(" ", strip=True)) for a in node.find_all("a"))
    share = link_len / max(len(text), 1)
    if share > MAX_LINK_TEXT_SHARE:
        return False, "%.0f%% of the text is links -- looks like navigation" % (share * 100), len(text)
    return True, "ok (%d chars)" % len(text), len(text)


# ---------------------------------------------------------------------------
# Spec <-> scraper
# ---------------------------------------------------------------------------
def _paginate_from(p) -> object:
    if not p or not isinstance(p, dict):
        return SinglePage()
    kind = (p.get("type") or "").lower()
    if kind in ("query", "querypage"):
        return QueryPage(p.get("param") or "p", int(p.get("start", 2) or 2))
    if kind in ("next", "next_link", "nextlink"):
        return NextLink(p.get("selector") or "", tuple(p.get("texts") or ()))
    return SinglePage()


SPEC_FIELDS = ("chapter_links", "content", "chapter_title", "chapter_number_re",
               "chapter_title_strip", "novel_id_re", "index_url", "listing_url",
               "language", "meta", "drop", "drop_classes", "drop_text_prefixes")


def build_scraper(spec: dict, **kwargs) -> SiteScraper:
    """Turn a spec dict into a live, ordinary SiteScraper."""
    attrs = {"name": "learned", "source_site": spec.get("source_site") or "",
             "domains": [], "paginate": _paginate_from(spec.get("paginate"))}
    for f in SPEC_FIELDS:
        if spec.get(f) not in (None, "", [], {}):
            attrs[f] = spec[f]
    attrs.setdefault("language", "zh")
    attrs["meta"] = spec.get("meta") or {}
    cls = type("LearnedScraper", (SiteScraper,), attrs)
    return cls(**kwargs)


# ---------------------------------------------------------------------------
# Persistence -- one readable JSON per domain, under DATA_DIR/site_specs/
# ---------------------------------------------------------------------------
def spec_path(url_or_domain: str) -> str:
    dom = url_or_domain if "/" not in url_or_domain else _domain(url_or_domain)
    safe = re.sub(r"[^a-z0-9.-]", "_", dom.lower()) or "unknown"
    return os.path.join(_spec_dir(), safe + ".json")


def load_spec(url: str) -> Optional[dict]:
    p = spec_path(url)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        logger.warning("could not read site spec %s: %s" % (p, e))
        return None


def save_spec(url: str, spec: dict) -> str:
    p = spec_path(url)
    spec = dict(spec)
    spec["domain"] = _domain(url)
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False, indent=2, sort_keys=True)
        logger.info("learned site spec saved: %s" % p)
    except OSError as e:
        logger.warning("could not save site spec %s: %s" % (p, e))
    return p


def forget_spec(url: str) -> bool:
    p = spec_path(url)
    try:
        os.remove(p)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
LISTING_PROMPT = """You are configuring a web-novel scraper for a site it has never seen.

Below is a STRUCTURAL DIGEST of a novel's chapter-listing page: the containers
that hold links (with how many links each holds and sample hrefs), the page's
meta tags, and any pagination-looking anchors.

Return ONLY a JSON object:
{"chapter_links": "<CSS selector matching the chapter <a> elements>",
 "paginate": null | {"type":"query","param":"p"} | {"type":"next_link","selector":"<css>"},
 "meta": {"title":"<css or css@attr>","author":"...","desc":"...","cover":"..."},
 "language": "zh" | "ja" | "ko" | "en"}

Rules:
- chapter_links MUST select the FULL chapter list, not a "latest chapters" box.
  When several clusters exist, prefer the one with the MOST links.
- Write selectors that match the <a> elements themselves, e.g. "ul.all li a".
- Use "selector@attr" to read an attribute, e.g. "meta[name='og:image']@content".
- paginate: use "query" when sample pagination hrefs carry ?p= / ?page=; use
  "next_link" when there is a next-page anchor; otherwise null.
- Omit any meta field you cannot determine. No prose, no markdown fences.

DIGEST:
%s"""

CHAPTER_PROMPT = """You are configuring a web-novel scraper for a site it has never seen.

Below is a STRUCTURAL DIGEST of one CHAPTER page: its biggest text blocks, each
with a CSS selector, its text length, what share of that text is inside links,
and how it starts.

Return ONLY a JSON object:
{"content": "<CSS selector for the element holding the chapter body>",
 "chapter_title": "<CSS selector for the chapter title>",
 "drop": ["<css of junk inside the body>", "..."]}

Rules:
- content must be the PROSE body: prefer a block with a large text_len and a LOW
  link_text_share. Never pick navigation, comments or recommendation blocks.
- Prefer the most specific block that still holds the whole body.
- drop may be empty. No prose, no markdown fences.

DIGEST:
%s"""


def parse_spec_reply(raw: str) -> Optional[dict]:
    """Pull the JSON object out of a model reply (it may add fences or prose)."""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None
