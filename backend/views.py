"""
HTML rendering helpers for NyaaReader multi-page Vue app.
"""
import hashlib
import html as html_mod
import json
import logging
import os
from typing import Optional

from fastapi.responses import HTMLResponse

logger = logging.getLogger("novel-reader.views")

_backend_dir = os.path.dirname(os.path.abspath(__file__))
frontend_path = next(
    (p for p in (
        os.path.join(_backend_dir, "..", "frontend"),
        os.path.join(_backend_dir, "frontend"),
    ) if os.path.isdir(p)),
    None,
)


def _asset_stamp() -> str:
    """Hash of frontend asset mtimes for cache-busting."""
    try:
        h = hashlib.md5()
        for name in ("styles.css", "lib/text.js", "library.js", "novel.js", "reader.js",
                     "config.js", "dashboard.js", "review.js", "login.js"):
            p = os.path.join(frontend_path or ".", name)
            if os.path.exists(p):
                h.update(str(os.path.getmtime(p)).encode())
        return h.hexdigest()[:10]
    except Exception as e:
        logger.warning(f"_asset_stamp: could not hash frontend files, cache-busting disabled: {e}")
        return "0"


try:
    with open(os.path.join(frontend_path or ".", "icons.svg"), encoding="utf-8") as _f:
        _icons_sprite_cache = _f.read()
except OSError:
    logger.warning("icons.svg not found — pages will render without the icon sprite")
    _icons_sprite_cache = ""


def _page(title: str, body: str, page_js: Optional[str] = None,
          data_js: Optional[str] = None, refresh: Optional[int] = None) -> HTMLResponse:
    refresh_tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    data_tag = f"<script>{data_js}</script>" if data_js else ""
    stamp = _asset_stamp()
    js_tags = ""
    if page_js:
        js_tags = (
            f'<script src="/static/vendor/vue.global.prod.js?v={stamp}"></script>\n'
            f'<script src="/static/lib/text.js?v={stamp}"></script>\n'
            f'<script src="/static/{page_js}?v={stamp}"></script>'
        )
    icons_sprite = _icons_sprite_cache
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_mod.escape(title)}</title>
{refresh_tag}
<link rel="icon" href="/static/favicon.ico" sizes="any">
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="apple-touch-icon" href="/static/favicon.svg">
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" content="#6d5ae0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<script>
(function() {{
  try {{
    var raw = localStorage.getItem("novelreader.prefs");
    if (raw) {{
      var p = JSON.parse(raw);
      if (p && p.theme) document.documentElement.setAttribute("data-theme", p.theme);
    }}
  }} catch(e) {{}}
}})();
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400&family=Literata:ital,opsz,wght@0,7..72,400;0,7..72,600;1,7..72,400&family=Lora:ital,wght@0,400;0,600;1,400&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/styles.css?v={stamp}">
</head>
<body>
{icons_sprite}
{body}
{data_tag}
{js_tags}
<script>
if ('serviceWorker' in navigator) {{
  navigator.serviceWorker.register('/static/sw.js').catch(function () {{}});
}}
</script>
</body>
</html>""")


def _json(data) -> str:
    """JSON for embedding inside <script> tags. Escapes "</" so a scraped
    title like `</script><img onerror=...>` cannot break out of the script
    block (classic JSON-in-HTML XSS)."""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def _get_recap(db, novel_id: int) -> dict:
    """Build the 'Previously on…' recap from AI memory: current arc plot + most
    recent chapter summary. Returns {} when memory is empty (reader hides the card)."""
    from models import NovelMemory
    mem = db.query(NovelMemory).filter(NovelMemory.novel_id == novel_id).first()
    if not mem:
        return {}
    recap = {}
    arc = (mem.arc_plot or "").strip()
    ch = (mem.chapter_plot or "").strip()
    if arc:
        recap["arc"] = arc
    if ch:
        recap["chapter"] = ch
    return recap
