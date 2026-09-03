/* Shared pure text helpers, used by both reader.js and novel.js — and by
   tests/frontend/*.test.mjs, which `require()` this file directly. Keeping
   this logic in one place (instead of copy-pasted per page) means a test
   exercises the ACTUAL shipped code, not a duplicate that can drift out of
   sync with it — which is exactly how the paragraph-splitting bug (reader.js,
   2026-08-29) went unnoticed: the reader's copy and the "original text"
   copy nearby used two different splitting rules, and only one got fixed. */
(function (root, factory) {
  var api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;               // Node / tests
  }
  if (root) {
    root.NyaaText = api;                // browser <script> tag
  }
})(typeof window !== "undefined" ? window : this, function () {

  // Split chapter text into paragraphs.
  //
  // Two conventions are in play and both must be handled: CJK web novels put
  // one paragraph per LINE (single \n), while a translation may come back
  // blank-line separated instead — and a model's formatting is not consistent
  // enough to rely on one convention. Splitting only on blank lines turned a
  // single-newline chapter into one wall of text (measured: 12 of 125
  // translated chapters in the live library, across two different models).
  function splitParagraphs(txt) {
    var s = txt || "";
    var byBlank = s.split(/\n{2,}/).map(function (x) { return x.trim(); }).filter(Boolean);
    var byLine = s.split(/\n+/).map(function (x) { return x.trim(); }).filter(Boolean);
    var list = byLine.length > byBlank.length * 2 ? byLine : byBlank;
    return list
      .map(function (x) { return x.replace(/\s*\n\s*/g, " ").trim(); })
      .map(function (x) { return x.replace(/^#{1,6}\s*/, "").trim(); })   // stray markdown headings
      .filter(Boolean);
  }

  // Drop a first paragraph that duplicates the chapter title, WITHOUT
  // deleting real prose that happens to start with (or contain) a short
  // title word. Requires the block to be title-length, and either an exact
  // match or a long-enough title (>=12 chars) for a prefix test to carry
  // real signal. A prior version used `first.includes(h1.slice(0,24))`,
  // which deleted "The fireplace crackled..." for a chapter titled "Fire".
  function stripTitleEcho(list, titleRaw) {
    var h1 = ((titleRaw || "") + "")
      .replace(/^Chapter\s+\d+[:\s-]*/i, "").trim().toLowerCase();
    if (!h1 || h1.length < 3 || list.length <= 1) return list;
    var first = list[0].toLowerCase();
    var titleLike = first.length <= Math.max(60, h1.length + 20);
    var strongMatch = first === h1 ||
      (h1.length >= 12 && (first.indexOf(h1) === 0 || h1.indexOf(first) === 0));
    return (titleLike && strongMatch) ? list.slice(1) : list;
  }

  // A chapter's "has content" flag arrives in TWO shapes depending on where
  // the data came from: the server-rendered page (window.__NOVEL__, from
  // main.py) exposes a boolean `has_content`; GET /api/novels/:id/chapters
  // returns the full `original_content` string instead. Reading only one of
  // them is silently wrong on the other code path rather than an error —
  // "fetch next 10" always restarted at chapter 1 because it read
  // `c.original_content` on server-rendered rows that never carry that key.
  function hasContent(c) {
    return (c && c.has_content !== undefined) ? !!c.has_content : !!(c && c.original_content);
  }

  // Best-effort parse of free-text memory into glossary rows (legacy / no
  // structured data). Single copy — novel.js and reader.js both use these.
  function parseCharLines(text) {
    return (text || "").split("\n").map(function (l) { return l.trim(); }).filter(Boolean).map(function (l) {
      var m = l.match(/^(.*?)\s*\(([^)]+)\)\s*[-–:]\s*(.*)$/);
      if (m) return { type: "character", translated: m[1].trim(), source: m[2].trim(), note: m[3].trim(), locked: false };
      return { type: "character", translated: l, source: "", note: "", locked: false };
    });
  }
  function parseTermLines(text) {
    return (text || "").split("\n").map(function (l) { return l.trim(); }).filter(Boolean).map(function (l) {
      var m = l.match(/^(.*?)\s*(?:=|->|→)\s*(.*)$/);
      if (m) return { type: "term", source: m[1].trim(), translated: m[2].trim(), note: "", locked: false };
      return { type: "term", source: l, translated: "", note: "", locked: false };
    });
  }

  // Cover URLs come from scraped og:image — untrusted. Allow only http(s),
  // site-relative, and data:image; drop javascript:/data:text/html and CSS
  // breakouts like "');background:evil". Returns "" when unsafe.
  function safeCoverUrl(u) {
    var s = String(u == null ? "" : u).trim();
    if (!s) return "";
    if (/^javascript:/i.test(s) || /^data:(?!image\/)/i.test(s)) return "";
    if (/^https?:\/\//i.test(s) || s.charAt(0) === "/" || /^data:image\//i.test(s)) {
      if (/["'();]/.test(s)) return "";
      return s;
    }
    return "";
  }

  return { splitParagraphs: splitParagraphs, stripTitleEcho: stripTitleEcho, hasContent: hasContent, parseCharLines: parseCharLines, parseTermLines: parseTermLines, safeCoverUrl: safeCoverUrl };
});
