"""
Base scraper class and utilities
"""
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from typing import Optional, List, Dict, Any
from urllib.parse import urljoin, urlparse
import logging
import re
from dataclasses import dataclass
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


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
        connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
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
                        return await response.text()
                    elif response.status == 404:
                        logger.warning(f"404 Not Found: {url}")
                        return None
                    elif response.status == 429:
                        # Rate limited - wait longer
                        retry_after = int(response.headers.get("Retry-After", 60))
                        logger.warning(f"Rate limited (429), waiting {retry_after}s: {url}")
                        await asyncio.sleep(min(retry_after, 120))
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
        # Remove common noise
        content = re.sub(r"(?i)(chapter \d+|第.+?章|第.+?話|제.+?화)", "", content)
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