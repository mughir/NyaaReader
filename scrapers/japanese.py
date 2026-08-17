"""
Japanese novel scrapers (syosetu, kakuyomu, alphapolis, etc.)
"""
import re
from typing import Optional, List
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, NovelInfo, ChapterData


class KakuyomuScraper(BaseScraper):
    domains = ["kakuyomu.jp"]
    """Kakuyomu (カクヨム) scraper"""

    BASE_URL = "https://kakuyomu.jp"

    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        title_elem = soup.select_one(".widget-work-title, .work-title, h1")
        title = title_elem.get_text(strip=True) if title_elem else "Unknown"

        author_elem = soup.select_one(".widget-work-author a, .author a")
        author = author_elem.get_text(strip=True) if author_elem else None

        desc_elem = soup.select_one(".widget-work-description, .description, .synopsis")
        description = desc_elem.get_text(strip=True) if desc_elem else None

        cover_elem = soup.select_one(".widget-work-image img, .work-image img")
        cover_url = cover_elem.get("src") if cover_elem else None

        chapters = []
        chapter_links = soup.select(".widget-toc-episode a, .episode-list a, .toc-episode a")

        for i, link in enumerate(chapter_links, 1):
            href = link.get("href")
            title_text = link.get_text(strip=True)
            if href and title_text:
                chapter_url = urljoin(self.BASE_URL, href)
                chapters.append(ChapterData(
                    number=i,
                    title=title_text,
                    content="",
                    url=chapter_url,
                    word_count=0
                ))

        return NovelInfo(
            title=title,
            author=author,
            description=description,
            cover_url=cover_url,
            chapters=chapters,
            original_language="ja",
            total_chapters=len(chapters)
        )

    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        content_elem = soup.select_one(".widget-episode-body, .episode-body, .episode-content, .p-episode-body")
        if not content_elem:
            return None

        for elem in content_elem.select("script, style, .ad"):
            elem.decompose()

        content = content_elem.get_text("\n", strip=True)
        content = self._clean_content(content)

        title_elem = soup.select_one(".widget-episode-title, .episode-title, h1")
        title = title_elem.get_text(strip=True) if title_elem else "Chapter"

        match = re.search(r"/episodes/(\d+)", url)
        number = int(match.group(1)) if match else 0

        word_count = len(re.findall(r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]", content))

        return ChapterData(
            number=number,
            title=title,
            content=content,
            url=url,
            word_count=word_count
        )


class AlphaPolisScraper(BaseScraper):
    domains = ["alphapolis.co.jp"]
    """AlphaPolis (アルファポリス) scraper"""

    BASE_URL = "https://www.alphapolis.co.jp"

    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        title_elem = soup.select_one(".novel-title, .work-title h1")
        title = title_elem.get_text(strip=True) if title_elem else "Unknown"

        author_elem = soup.select_one(".author-name a, .author a")
        author = author_elem.get_text(strip=True) if author_elem else None

        desc_elem = soup.select_one(".synopsis, .description")
        description = desc_elem.get_text(strip=True) if desc_elem else None

        cover_elem = soup.select_one(".novel-image img, .cover-image img")
        cover_url = cover_elem.get("src") if cover_elem else None
        if cover_url and cover_url.startswith("//"):
            cover_url = "https:" + cover_url

        chapters = []
        chapter_links = soup.select(".episode-list a, .toc a")

        for i, link in enumerate(chapter_links, 1):
            href = link.get("href")
            title_text = link.get_text(strip=True)
            if href and title_text:
                chapter_url = urljoin(self.BASE_URL, href)
                chapters.append(ChapterData(
                    number=i,
                    title=title_text,
                    content="",
                    url=chapter_url,
                    word_count=0
                ))

        return NovelInfo(
            title=title,
            author=author,
            description=description,
            cover_url=cover_url,
            chapters=chapters,
            original_language="ja",
            total_chapters=len(chapters)
        )

    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        content_elem = soup.select_one(".episode-body, .novel-body, .content-body")
        if not content_elem:
            return None

        for elem in content_elem.select("script, style, .ad"):
            elem.decompose()

        content = content_elem.get_text("\n", strip=True)
        content = self._clean_content(content)

        title_elem = soup.select_one(".episode-title, .chapter-title, h1")
        title = title_elem.get_text(strip=True) if title_elem else "Chapter"

        match = re.search(r"/(\d+)/?$", url)
        number = int(match.group(1)) if match else 0

        word_count = len(re.findall(r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]", content))

        return ChapterData(
            number=number,
            title=title,
            content=content,
            url=url,
            word_count=word_count
        )
