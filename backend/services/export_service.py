"""
EPUB export and Cover image services for NyaaReader.
"""
import asyncio
import logging
import os
from pathlib import Path
import re

from models import Chapter, Novel

logger = logging.getLogger("novel-reader.export")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data")) if not os.name == "nt" else Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _safe_filename(name: str) -> str:
    return re.sub(r'[^\w\- ]', '', name)[:80].strip() or "novel"


def _epub_path(novel) -> Path:
    d = Path(DATA_DIR) / "epub"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"novel_{novel.id}.epub"


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _split_paragraphs(text: str) -> list:
    """Split text into clean paragraphs respecting both line breaks and blank-line separated texts."""
    text = text or ""
    by_blank = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    by_line = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chosen = by_line if len(by_line) > len(by_blank) * 2 else by_blank
    return [re.sub(r"\s*\n\s*", " ", p).strip() for p in chosen if p.strip()]


def _export_epub_bg(novel_id: int):
    """Background: assemble the EPUB from translated chapters."""
    from ebooklib import epub
    from database import SessionLocal
    from services.job_service import _set_batch, _finish_batch, _bump_batch

    db = SessionLocal()
    try:
        novel = db.query(Novel).filter(Novel.id == novel_id).first()
        if not novel:
            return
        chapters = (db.query(Chapter).filter(
            Chapter.novel_id == novel_id,
            Chapter.is_translated == True,
            Chapter.translated_content.isnot(None),
        ).order_by(Chapter.chapter_number).all())
        if not _set_batch(novel_id, "epub", len(chapters)):
            return
        if not chapters:
            _finish_batch(novel_id, "No translated chapters to export yet")
            return

        book = epub.EpubBook()
        book.set_identifier(f"nyaa-{novel_id}")
        book.set_title(novel.title_translated or novel.title)
        if novel.author:
            book.add_author(novel.author)
        book.set_language(novel.target_language or "en")
        if novel.description_translated or novel.description:
            book.add_metadata("DC", "description", novel.description_translated or novel.description)

        book_items = []
        for i, ch in enumerate(chapters):
            title = ch.title_translated or ch.title or f"Chapter {ch.chapter_number}"
            body = ch.translated_content or ""
            paras = _split_paragraphs(body)
            html_body = "".join(f"<p>{_html_escape(p)}</p>" for p in paras) or "<p></p>"
            item = epub.EpubHtml(
                title=title,
                file_name=f"ch_{ch.chapter_number:04d}.xhtml",
                lang=novel.target_language or "en",
                content=f"<h1>{_html_escape(title)}</h1>{html_body}",
            )
            book.add_item(item)
            book_items.append(item)
            _bump_batch(novel_id, label=f"Ch {ch.chapter_number} {title[:40]}")
            if (i + 1) % 25 == 0:
                db.close()
                db = SessionLocal()

        book.toc = tuple(book_items)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav"] + book_items
        out = _epub_path(novel)
        epub.write_epub(str(out), book)
        _finish_batch(novel_id, f"EPUB built — {len(chapters)} chapters")
        logger.info(f"EPUB for novel {novel_id}: {out} ({len(chapters)} chapters)")
    except Exception as e:
        logger.error(f"EPUB export failed for {novel_id}: {e}")
        from services.job_service import _finish_batch
        _finish_batch(novel_id, f"EPUB build failed: {str(e)[:120]}")
    finally:
        db.close()


async def _generate_cover_svg(novel) -> str:
    """Build a cover SVG via the relay: ask for a color scheme + motif based on
    title/synopsis, then render a styled 2:3 cover locally (deterministic, safe)."""
    from translator import OpenAIRelayTranslator
    import json as _json

    title = novel.title_translated or novel.title or "NyaaReader"
    synopsis = (novel.description_translated or novel.description or "")[:600]

    prompt = (
        "You design book covers. For the novel below, output ONLY a JSON object:\n"
        '{"bg1":"#hex","bg2":"#hex","accent":"#hex","motif":"one of: mountain|ocean|moon|sword|flower|dragon|stars|tree|flame|city|mask|gate"}'
        "\nRules: bg1/bg2 = a moody vertical gradient pair matching the novel's vibe; "
        "accent = a readable highlight color; motif = a single symbolic element fitting the story. "
        f"\n\nTITLE: {title}\nSYNOPSIS: {synopsis}"
    )
    llm = OpenAIRelayTranslator(model="deepseek-v4-flash")
    raw = await asyncio.to_thread(llm._generate, prompt)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise RuntimeError("relay returned no JSON")
    d = _json.loads(m.group(0))
    bg1 = d.get("bg1", "#1b1b2f")
    bg2 = d.get("bg2", "#162447")
    accent = d.get("accent", "#e43f5a")
    motif = d.get("motif", "stars")
    if not re.match(r"^#[0-9a-fA-F]{6}$", str(bg1)): bg1 = "#1b1b2f"
    if not re.match(r"^#[0-9a-fA-F]{6}$", str(bg2)): bg2 = "#162447"
    if not re.match(r"^#[0-9a-fA-F]{6}$", str(accent)): accent = "#e43f5a"

    motifs = {
        "mountain": '<path d="M0 330 L90 200 L150 290 L210 220 L300 340 L300 450 L0 450 Z" fill="rgba(255,255,255,.10)"/>'
                    '<path d="M0 380 L120 260 L200 340 L300 290 L300 450 L0 450 Z" fill="rgba(255,255,255,.07)"/>',
        "ocean":   '<path d="M0 300 C60 280 120 320 180 300 C240 280 280 310 300 295 L300 450 L0 450 Z" fill="rgba(255,255,255,.12)"/>'
                   '<path d="M0 350 C70 330 140 370 210 350 C250 338 280 355 300 345 L300 450 L0 450 Z" fill="rgba(255,255,255,.08)"/>',
        "moon":    '<circle cx="225" cy="110" r="46" fill="none" stroke="rgba(255,255,255,.55)" stroke-width="3"/>'
                   '<circle cx="238" cy="98" r="42" fill="none" stroke="rgba(255,255,255,.35)" stroke-width="3"/>',
        "sword":   '<path d="M150 90 L240 330 L150 380 L60 330 Z" fill="rgba(255,255,255,.10)"/>'
                   '<path d="M143 100 L157 100 L152 90 Z" fill="rgba(255,255,255,.25)"/>',
        "flower":  '<g fill="rgba(255,255,255,.18)"><circle cx="150" cy="140" r="34"/><circle cx="132" cy="118" r="26"/><circle cx="168" cy="118" r="26"/><circle cx="132" cy="162" r="26"/><circle cx="168" cy="162" r="26"/></g>'
                   '<circle cx="150" cy="140" r="14" fill="rgba(255,255,255,.4)"/>',
        "dragon":  '<path d="M90 160 C130 120 200 130 210 180 C220 230 170 240 180 280 L150 270 C150 230 180 210 170 180 C160 150 120 150 105 175 Z" fill="rgba(255,255,255,.14)"/>',
        "stars":   '<g fill="rgba(255,255,255,.6)"><circle cx="70" cy="80" r="3"/><circle cx="240" cy="60" r="2.5"/><circle cx="200" cy="160" r="2"/><circle cx="110" cy="200" r="2.5"/><circle cx="260" cy="240" r="2"/><circle cx="50" cy="160" r="2"/></g>',
        "tree":    '<path d="M150 120 C120 180 110 220 115 300 L185 300 C190 220 180 180 150 120 Z" fill="rgba(255,255,255,.12)"/>'
                   '<path d="M150 120 C130 90 170 90 150 60 C140 90 160 90 150 120 Z" fill="rgba(255,255,255,.14)"/>',
        "flame":   '<path d="M150 100 C110 170 100 200 150 260 C200 200 190 170 150 100 Z" fill="rgba(255,255,255,.15)"/>',
        "city":    '<g fill="rgba(255,255,255,.10)"><rect x="40" y="250" width="40" height="140"/><rect x="90" y="210" width="34" height="180"/><rect x="135" y="260" width="42" height="130"/><rect x="190" y="200" width="36" height="190"/><rect x="238" y="250" width="40" height="140"/></g>',
        "mask":    '<path d="M110 170 C110 140 190 140 190 170 L205 250 C205 290 95 290 95 250 Z" fill="rgba(255,255,255,.12)"/>',
        "gate":    '<path d="M120 180 L120 340 L180 340 L180 180 L150 140 Z" fill="none" stroke="rgba(255,255,255,.25)" stroke-width="4"/>'
                   '<path d="M120 180 Q150 200 180 180" fill="none" stroke="rgba(255,255,255,.25)" stroke-width="4"/>',
    }
    glyph = motifs.get(motif, motifs["stars"])

    title_short = title[:80]
    words = title_short.split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > 17:
            if cur: lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur: lines.append(cur)
    lines = lines[:3]
    if len(lines) == 3 or max(len(l) for l in lines) > 16:
        font_size = 19
    else:
        font_size = 23
    tspans = "".join(
        f'<tspan x="150" dy="{30 if i else 0}">{_xml_escape(l)}</tspan>' for i, l in enumerate(lines)
    )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 450">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0.4" y2="1">
      <stop offset="0" stop-color="{bg1}"/>
      <stop offset="1" stop-color="{bg2}"/>
    </linearGradient>
    <linearGradient id="sh" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="rgba(0,0,0,0)"/>
      <stop offset="1" stop-color="rgba(0,0,0,.55)"/>
    </linearGradient>
  </defs>
  <rect width="300" height="450" fill="url(#g)"/>
  {glyph}
  <rect width="300" height="450" fill="url(#sh)"/>
  <rect x="10" y="10" width="280" height="430" fill="none" stroke="rgba(255,255,255,.28)" stroke-width="2" rx="8"/>
  <text font-family="Georgia, serif" font-size="{font_size}" font-weight="bold" fill="#ffffff"
        text-anchor="middle" x="150" y="270" letter-spacing="1">{tspans}</text>
  <text font-family="Georgia, serif" font-size="13" fill="rgba(255,255,255,.7)"
        text-anchor="middle" x="150" y="360">NyaaReader</text>
  <rect x="120" y="374" width="60" height="3" rx="1.5" fill="{accent}"/>
</svg>'''
