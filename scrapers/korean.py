"""
Korean novel scrapers (novelpia, munpia, naver, ridibooks, etc.)
"""
import re
from typing import Optional, List
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, NovelInfo, ChapterData


class NovelpiaScraper(BaseScraper):
    domains = ["novelpia.com"]
    """Novelpia (노벨피아) scraper"""

    BASE_URL = "https://novelpia.com"

    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        title_elem = soup.select_one(".novel-title, .title-area h1, .work-title")
        title = title_elem.get_text(strip=True) if title_elem else "Unknown"

        author_elem = soup.select_one(".author-name, .writer a, .author a")
        author = author_elem.get_text(strip=True) if author_elem else None

        desc_elem = soup.select_one(".synopsis, .description, .intro")
        description = desc_elem.get_text(strip=True) if desc_elem else None

        cover_elem = soup.select_one(".thumbnail img, .cover-img img, .novel-cover img")
        cover_url = cover_elem.get("src") if cover_elem else None
        if cover_url and cover_url.startswith("//"):
            cover_url = "https:" + cover_url

        chapters = []
        chapter_links = soup.select(".episode-list a, .chapter-list a, .toc a")

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
            original_language="ko",
            total_chapters=len(chapters)
        )

    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        content_elem = soup.select_one(".viewer-content, .episode-content, .chapter-content, .content-body")
        if not content_elem:
            return None

        for elem in content_elem.select("script, style, .ad, .advertisement"):
            elem.decompose()

        content = content_elem.get_text("\n", strip=True)
        content = self._clean_content(content)

        title_elem = soup.select_one(".episode-title, .chapter-title, .viewer-title h1")
        title = title_elem.get_text(strip=True) if title_elem else "Chapter"

        match = re.search(r"/(\d+)/?$", url)
        number = int(match.group(1)) if match else 0

        # Count Korean characters
        word_count = len(re.findall(r"[\uac00-\ud7af]", content))

        return ChapterData(
            number=number,
            title=title,
            content=content,
            url=url,
            word_count=word_count
        )


class MunpiaScraper(BaseScraper):
    domains = ["munpia.com"]
    """Munpia (문피아) scraper"""

    BASE_URL = "https://www.munpia.com"

    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        title_elem = soup.select_one(".novel-title, .title h1, .work-title")
        title = title_elem.get_text(strip=True) if title_elem else "Unknown"

        author_elem = soup.select_one(".author a, .writer a")
        author = author_elem.get_text(strip=True) if author_elem else None

        desc_elem = soup.select_one(".synopsis, .description, .intro")
        description = desc_elem.get_text(strip=True) if desc_elem else None

        cover_elem = soup.select_one(".cover img, .thumbnail img")
        cover_url = cover_elem.get("src") if cover_elem else None
        if cover_url and cover_url.startswith("//"):
            cover_url = "https:" + cover_url

        chapters = []
        chapter_links = soup.select(".episode-list a, .list-item a, .toc a")

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
            original_language="ko",
            total_chapters=len(chapters)
        )

    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        content_elem = soup.select_one(".viewer, .content-body, .episode-body, .chapter-content")
        if not content_elem:
            return None

        for elem in content_elem.select("script, style, .ad"):
            elem.decompose()

        content = content_elem.get_text("\n", strip=True)
        content = self._clean_content(content)

        title_elem = soup.select_one(".title, .episode-title, h1")
        title = title_elem.get_text(strip=True) if title_elem else "Chapter"

        match = re.search(r"/(\d+)/?$", url)
        number = int(match.group(1)) if match else 0

        word_count = len(re.findall(r"[\uac00-\ud7af]", content))

        return ChapterData(
            number=number,
            title=title,
            content=content,
            url=url,
            word_count=word_count
        )


class NaverSeriesScraper(BaseScraper):
    domains = ["series.naver.com", "novel.naver.com"]
    """Naver Series (네이버 시리즈) scraper"""

    BASE_URL = "https://series.naver.com"

    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        title_elem = soup.select_one(".novel_title, .end_title h2, .work_title")
        title = title_elem.get_text(strip=True) if title_elem else "Unknown"

        author_elem = soup.select_one(".author a, .writer a")
        author = author_elem.get_text(strip=True) if author_elem else None

        desc_elem = soup.select_one(".synopsis, .desc_area, .description")
        description = desc_elem.get_text(strip=True) if desc_elem else None

        cover_elem = soup.select_one(".novel_thumb img, .thumbnail img")
        cover_url = cover_elem.get("src") if cover_elem else None

        chapters = []
        chapter_links = soup.select(".lst_episode a, .episode_list a, .volume_list a")

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
            original_language="ko",
            total_chapters=len(chapters)
        )

    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        content_elem = soup.select_one(".viewer, .content_view, .episode_view, .chapter_view")
        if not content_elem:
            return None

        for elem in content_elem.select("script, style, .ad"):
            elem.decompose()

        content = content_elem.get_text("\n", strip=True)
        content = self._clean_content(content)

        title_elem = soup.select_one(".episode_tit, .chapter_tit, h1")
        title = title_elem.get_text(strip=True) if title_elem else "Chapter"

        match = re.search(r"no=(\d+)", url)
        number = int(match.group(1)) if match else 0

        word_count = len(re.findall(r"[\uac00-\ud7af]", content))

        return ChapterData(
            number=number,
            title=title,
            content=content,
            url=url,
            word_count=word_count
        )


class RidibooksScraper(BaseScraper):
    domains = ["ridibooks.com"]
    """Ridibooks (리디북스) scraper"""

    BASE_URL = "https://ridibooks.com"

    async def get_novel_info(self, url: str) -> Optional[NovelInfo]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        title_elem = soup.select_one(".book-title, .title_area h1")
        title = title_elem.get_text(strip=True) if title_elem else "Unknown"

        author_elem = soup.select_one(".author a, .writer a")
        author = author_elem.get_text(strip=True) if author_elem else None

        desc_elem = soup.select_one(".description, .book_desc")
        description = desc_elem.get_text(strip=True) if desc_elem else None

        cover_elem = soup.select_one(".cover_image img, .book_cover img")
        cover_url = cover_elem.get("src") if cover_elem else None

        chapters = []
        chapter_links = soup.select(".chapter_list a, .toc_list a")

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
            original_language="ko",
            total_chapters=len(chapters)
        )

    async def get_chapter_content(self, url: str) -> Optional[ChapterData]:
        html = await self._fetch(url)
        if not html:
            return None

        soup = self._parse_html(html)

        content_elem = soup.select_one(".viewer_content, .content_view, .chapter_view")
        if not content_elem:
            return None

        for elem in content_elem.select("script, style, .ad"):
            elem.decompose()

        content = content_elem.get_text("\n", strip=True)
        content = self._clean_content(content)

        title_elem = soup.select_one(".chapter_title, .episode_title, h1")
        title = title_elem.get_text(strip=True) if title_elem else "Chapter"

        match = re.search(r"/(\d+)/?$", url)
        number = int(match.group(1)) if match else 0

        word_count = len(re.findall(r"[\uac00-\ud7af]", content))

        return ChapterData(
            number=number,
            title=title,
            content=content,
            url=url,
            word_count=word_count
        )