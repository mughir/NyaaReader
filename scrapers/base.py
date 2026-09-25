"""
Base scraper class and utilities
"""
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from typing import Optional, List, Dict, Any
from urllib.parse import urljoin, urlparse
import ipaddress
import logging
import os
import re
import socket
from dataclasses import dataclass
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# Cap per-response downloads: a hostile/misbehaving source (or an SSRF'd
# internal endpoint) could otherwise stream gigabytes into memory.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _allow_private_network() -> bool:
    """Opt-out for deployments that legitimately scrape LAN-hosted sources."""
    return os.getenv("ALLOW_PRIVATE_NETWORK", "").strip().lower() in ("1", "true", "yes")


def _assert_public_address(host: str, addr: str) -> None:
    """Raise if `addr` (an IP resolved for `host`) is not public Internet
    address space: blocks loopback, RFC1918, link-local (cloud metadata),
    shared/reserved ranges. Scraped pages carry attacker-controllable URLs
    (chapter links, LLM-extracted ones included), so the server must never
    blindly fetch them into the internal network."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return
    if not ip.is_global:
        raise aiohttp.ClientError(
            f"blocked non-public address {addr} (host {host!r}) — SSRF guard")


class _PublicNetworkResolver(aiohttp.ThreadedResolver):
    """DNS resolver that refuses to resolve hostnames pointing at
    non-public address space. Enforced at the connector level so redirect
    targets are validated too (aiohttp re-resolves every hop)."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await super().resolve(host, port, family)
        if _allow_private_network():
            return results
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and not ip.is_global:
            raise aiohttp.ClientError(
                f"blocked non-public address {host!r} — SSRF guard")
        for r in results:
            addr = r["host"] if isinstance(r, dict) else getattr(r, "host", "")
            _assert_public_address(host, addr)
        return results


@dataclass
class ChapterData:
    number: int
    title: str
    content: str
    url: str
    word_count: int = 0


@dataclass
class NovelInfo:
    title: str
    author: Optional[str] = None
    description: Optional[str] = None
    cover_url: Optional[str] = None
    chapters: List[ChapterData] = None
    original_language: str = "zh"
    total_chapters: int = 0


class BaseScraper(ABC):
    """Base class for novel scrapers"""

    def __init__(self, delay: float = 1.0, timeout: int = 30, max_retries: int = 3):
        self.delay = delay
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_request_time = 0

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(limit=10, limit_per_host=5,
                                         resolver=_PublicNetworkResolver())
        self.session = aiohttp.ClientSession(timeout=self.timeout, connector=connector)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _rate_limit(self):
        """Enforce rate limiting between requests"""
        import time
        elapsed = time.time() - self.last_request_time
        if elapsed < self.delay:
            await asyncio.sleep(self.delay - elapsed)
        self.last_request_time = time.time()

    async def _fetch(self, url: str, headers: Optional[Dict] = None) -> Optional[str]:
        """Fetch a URL with retries and rate limiting"""
        if not self.session:
            raise RuntimeError("Session not initialized. Use async with.")

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            logger.warning(f"Blocked fetch of non-http(s) URL: {url}")
            return None
        if not _allow_private_network():
            try:
                ip = ipaddress.ip_address(parsed.hostname or "")
            except ValueError:
                ip = None
            if ip is not None and not ip.is_global:
                logger.warning(f"Blocked fetch of non-public host: {url}")
                return None

        default_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7,ja;q=0.6,ko;q=0.5",
        }
        if headers:
            default_headers.update(headers)

        last_exception = None
        for attempt in range(self.max_retries):
            try:
                await self._rate_limit()
                async with self.session.get(url, headers=default_headers) as response:
                    if response.status == 200:
                        return await self._read_capped(response, url)
                    elif response.status == 404:
                        logger.warning(f"404 Not Found: {url}")
                        return None
                    elif response.status == 429:
                        # Rate limited - wait longer
                        raw = response.headers.get("Retry-After", "60")
                        try:
                            retry_after = int(raw)
                        except ValueError:
                            # HTTP-date form — parse it, else fall back to 60s
                            from email.utils import parsedate_to_datetime
                            from datetime import datetime as _dt, timezone as _tz
                            try:
                                retry_after = max(0, (parsedate_to_datetime(raw) - _dt.now(_tz.utc)).total_seconds())
                            except Exception:
                                retry_after = 60
                        logger.warning(f"Rate limited (429), waiting {retry_after}s: {url}")
                        await asyncio.sleep(min(retry_after, 120))
                        continue  # already waited — skip the extra backoff below
                    elif response.status >= 500:
                        logger.warning(f"Server error {response.status}: {url}")
                    else:
                        logger.warning(f"HTTP {response.status}: {url}")
            except asyncio.TimeoutError:
                logger.warning(f"Timeout fetching {url} (attempt {attempt + 1}/{self.max_retries})")
            except aiohttp.ClientError as e:
                logger.warning(f"Client error fetching {url}: {e} (attempt {attempt + 1}/{self.max_retries})")
            except Exception as e:
                logger.warning(f"Error fetching {url}: {e} (attempt {attempt + 1}/{self.max_retries})")

            if attempt < self.max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff

        logger.error(f"Failed to fetch {url} after {self.max_retries} attempts")
        return None

    async def _read_capped(self, response: "aiohttp.ClientResponse", url: str) -> str:
        """Read the response body, aborting past MAX_RESPONSE_BYTES instead of
        buffering an unbounded body in memory."""
        raw = bytearray()
        async for chunk in response.content.iter_chunked(1 << 16):
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                logger.warning(
                    f"Response body exceeded {MAX_RESPONSE_BYTES // (1024 * 1024)} MB — truncated: {url}")
                break
        charset = response.charset or "utf-8"
        try:
            return bytes(raw).decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return bytes(raw).decode("utf-8", errors="replace")

    def _parse_html(self, html: str) -> BeautifulSoup:
        """Parse HTML with lxml parser"""
        return BeautifulSoup(html, "lxml")

    @abstractmethod
    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        """Get novel metadata and chapter list"""
        pass

    @abstractmethod
    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        """Fetch and parse a single chapter"""
        pass

    def _clean_content(self, content: str) -> str:
        """Clean and normalize chapter content"""
        # Remove excessive whitespace
        content = re.sub(r"\n{3,}", "\n\n", content)
        content = re.sub(r"[ \t]{2,}", " ", content)
        # Remove chapter-heading lines ONLY when the line is a heading — the old
        # unanchored pattern deleted every in-sentence occurrence of "chapter N".
        content = re.sub(
            r"(?im)^[ \t]*(?:chapter\s*\d+[:.\s–-]{0,3}[^\n]{0,60}|第[^\n]{1,20}章[：:\s]*|제[^\n]{1,20}화[：:\s]*)[ \t]*$",
            "", content)
        return content.strip()

    def _detect_language(self, text: str) -> str:
        """Simple language detection"""
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        japanese_chars = len(re.findall(r"[\u3040-\u309f\u30a0-\u30ff]", text))
        korean_chars = len(re.findall(r"[\uac00-\ud7af]", text))

        if japanese_chars > chinese_chars and japanese_chars > korean_chars:
            return "ja"
        elif korean_chars > chinese_chars and korean_chars > japanese_chars:
            return "ko"
        return "zh"