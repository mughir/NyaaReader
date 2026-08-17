"""
Chinese novel scrapers (jjwxc, qidian, 17k, novel543, etc.)
"""
import asyncio
import re
from typing import Optional, List
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, NovelInfo, ChapterData


class JJWXCScraper(BaseScraper):
    domains = ["jjwxc.net", "jjwxc.com"]
    """JJWXC (晋江文学城) scraper"""

    BASE_URL = "https://www.jjwxc.net"

    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        """Parse JJWXC novel page"""
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        # Title
        title_elem = soup.select_one(".noveltitle, h1.novel-title, .book-title")
        title = title_elem.get_text(strip=True) if title_elem else "Unknown"

        # Author
        author_elem = soup.select_one(".author a, .writer a, .author-name")
        author = author_elem.get_text(strip=True) if author_elem else None

        # Description
        desc_elem = soup.select_one(".novelintro, .book-intro, .description")
        description = desc_elem.get_text(strip=True) if desc_elem else None

        # Cover
        cover_elem = soup.select_one(".novelcover img, .book-cover img")
        cover_url = cover_elem.get("src") if cover_elem else None
        if cover_url and not cover_url.startswith("http"):
            cover_url = urljoin(self.BASE_URL, cover_url)

        # Chapter list - JJWXC uses specific selectors
        chapters = []
        chapter_links = soup.select(".chapter-list a, .chapter-list2 a, #chapterList a, .catalog a")

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
            original_language="zh",
            total_chapters=len(chapters)
        )

    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        """Fetch chapter content from JJWXC"""
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        # Content area
        content_elem = soup.select_one(".novelcontent, .chapter-content, #content, .read-content")
        if not content_elem:
            return None

        # Remove ads/scripts
        for elem in content_elem.select("script, style, .ad, .advertisement, .chapter-ad"):
            elem.decompose()

        content = content_elem.get_text("\n", strip=True)
        content = self._clean_content(content)

        # Title
        title_elem = soup.select_one(".chapter-title, .chapter-title2, h1")
        title = title_elem.get_text(strip=True) if title_elem else "Chapter"

        # Chapter number from URL
        match = re.search(r"chapterid=(\d+)", url)
        number = int(match.group(1)) if match else 0

        word_count = len(re.findall(r"[\u4e00-\u9fff]", content))

        return ChapterData(
            number=number,
            title=title,
            content=content,
            url=url,
            word_count=word_count
        )


class QidianScraper(BaseScraper):
    domains = ["qidian.com", "qdmm.com"]
    """Qidian (起点中文网) scraper"""

    BASE_URL = "https://www.qidian.com"

    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        title_elem = soup.select_one(".book-info h1, .book-title")
        title = title_elem.get_text(strip=True) if title_elem else "Unknown"

        author_elem = soup.select_one(".author a, .writer-name")
        author = author_elem.get_text(strip=True) if author_elem else None

        desc_elem = soup.select_one(".book-intro, .book-desc")
        description = desc_elem.get_text(strip=True) if desc_elem else None

        cover_elem = soup.select_one(".book-img img, .book-cover img")
        cover_url = cover_elem.get("src") if cover_elem else None
        if cover_url and cover_url.startswith("//"):
            cover_url = "https:" + cover_url

        chapters = []
        chapter_links = soup.select(".volume-list .cf a, .chapter-list a, #chapterList a")

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
            original_language="zh",
            total_chapters=len(chapters)
        )

    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        content_elem = soup.select_one(".read-content, .chapter-content, .content")
        if not content_elem:
            return None

        for elem in content_elem.select("script, style, .ad"):
            elem.decompose()

        content = content_elem.get_text("\n", strip=True)
        content = self._clean_content(content)

        title_elem = soup.select_one(".chapter-title, h1, .title")
        title = title_elem.get_text(strip=True) if title_elem else "Chapter"

        match = re.search(r"/(\d+)\.html", url)
        number = int(match.group(1)) if match else 0

        word_count = len(re.findall(r"[\u4e00-\u9fff]", content))

        return ChapterData(
            number=number,
            title=title,
            content=content,
            url=url,
            word_count=word_count
        )


class SeventeenKScraper(BaseScraper):
    domains = ["17k.com"]
    """17K (17K小说网) scraper"""

    BASE_URL = "https://www.17k.com"

    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        title_elem = soup.select_one(".book-title, .book-name h1")
        title = title_elem.get_text(strip=True) if title_elem else "Unknown"

        author_elem = soup.select_one(".author a, .writer a")
        author = author_elem.get_text(strip=True) if author_elem else None

        desc_elem = soup.select_one(".intro, .book-intro")
        description = desc_elem.get_text(strip=True) if desc_elem else None

        cover_elem = soup.select_one(".book-cover img, .cover img")
        cover_url = cover_elem.get("src") if cover_elem else None
        if cover_url and cover_url.startswith("//"):
            cover_url = "https:" + cover_url

        chapters = []
        chapter_links = soup.select(".chapter-list a, .volume-chapter a")

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
            original_language="zh",
            total_chapters=len(chapters)
        )

    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        content_elem = soup.select_one(".p, .content, .read-content")
        if not content_elem:
            return None

        for elem in content_elem.select("script, style, .ad"):
            elem.decompose()

        content = content_elem.get_text("\n", strip=True)
        content = self._clean_content(content)

        title_elem = soup.select_one(".title, h1, .chapter-title")
        title = title_elem.get_text(strip=True) if title_elem else "Chapter"

        match = re.search(r"/(\d+)\.html", url)
        number = int(match.group(1)) if match else 0

        word_count = len(re.findall(r"[\u4e00-\u9fff]", content))

        return ChapterData(
            number=number,
            title=title,
            content=content,
            url=url,
            word_count=word_count
        )
