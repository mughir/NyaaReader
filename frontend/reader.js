/* Reader page — Vue-powered reading experience.
   Loaded on /novel/:id/chapter/:n. Uses window.__READER__ = {
     novel_id, chapter_number, title, novel_title, total_chapters,
     original, translated, is_translated, word_count }
   Translate/fetch run as background jobs with live polling — no page reloads. */
(function () {
  const DATA = window.__READER__;
  if (!DATA) return;

  const { createApp, ref, computed, onMounted, onUnmounted } = Vue;

  // -------- persisted reader prefs (localStorage) --------
  // Load is sanitized: a corrupted/out-of-range value can never take the
  // reader down or render it broken (e.g. font: 999, width: -50, or a
  // non-object from a previous buggy write).
  const PREFS_KEY = "novelreader.prefs";
  const PREFS_DEFAULTS = { theme: "light", font: 18, line: 1.9, showOrig: false, fontFamily: "serif", width: 700, focus: false, autoFetch: true };
  const THEMES = ["light", "sepia", "dark"];
  const FONT_FAMILIES = ["serif", "sans"];
  function clampNum(v, lo, hi, dflt) { return (typeof v === "number" && isFinite(v)) ? Math.min(hi, Math.max(lo, v)) : dflt; }
  function sanitizePrefs(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return { ...PREFS_DEFAULTS };
    return {
      theme: THEMES.includes(raw.theme) ? raw.theme : PREFS_DEFAULTS.theme,
      font: clampNum(raw.font, 13, 26, PREFS_DEFAULTS.font),
      line: clampNum(raw.line, 1.3, 2.4, PREFS_DEFAULTS.line),
      showOrig: raw.showOrig === true,
      fontFamily: FONT_FAMILIES.includes(raw.fontFamily) ? raw.fontFamily : PREFS_DEFAULTS.fontFamily,
      width: clampNum(raw.width, 320, 1000, PREFS_DEFAULTS.width),
      focus: raw.focus === true,
      autoFetch: raw.autoFetch !== false,
    };
  }
  let prefs = sanitizePrefs((() => { try { return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); } catch (e) { return null; } })());

  function savePrefs() {
    prefs = sanitizePrefs(prefs);  // clamp before persisting so bad values self-heal
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) { /* storage full/blocked — keep session values */ }
  }

  // -------- lightweight toast notifications --------
  function toast(msg, isErr) {
    let el = document.querySelector(".toast-stack");
    if (!el) { el = document.createElement("div"); el.className = "toast-stack"; document.body.appendChild(el); }
    const t = document.createElement("div");
    t.className = "toast" + (isErr ? " err" : "");
    t.textContent = msg;
    el.appendChild(t);
    setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, 3500);
  }

  // -------- app state --------
  const app = createApp({
    setup() {
      const novelId = DATA.novel_id;
      const chapterNumber = DATA.chapter_number;
      const total = DATA.total_chapters;

      const state = ref({
        original: DATA.original || "",
        translated: DATA.translated || "",
        is_translated: !!DATA.is_translated,
        has_original: !!(DATA.original || ""),
      });
      const titleTranslated = ref(DATA.title_translated || "");
      const novelTitleTranslated = ref(DATA.novel_title_translated || "");

      const theme = ref(prefs.theme);
      const fontSize = ref(prefs.font);
      const lineHeight = ref(prefs.line);
      const showOrig = ref(prefs.showOrig);
      const fontFamily = ref(prefs.fontFamily || "serif");
      const readerWidth = ref(prefs.width || 780);
      const focusMode = ref(prefs.focus || false);
      const autoFetch = ref(prefs.autoFetch !== false);
      const settingsOpen = ref(false);
      const busy = ref("");          // '', 'fetching', 'translating'
      const error = ref("");
      const pollTimer = ref(null);
      const ahead = ref({ running: false, done: 0, total: 0, label: "", kind: null });
      const stoppingAhead = ref(false);
      async function stopAhead() {
        if (stoppingAhead.value) return;
        stoppingAhead.value = true;
        try {
          await fetch(`/api/novels/${novelId}/batch-stop`, { method: "POST" });
          ahead.value.running = false;  // hide the bar immediately; poll will confirm
        } catch (e) {}
        stoppingAhead.value = false;
      }
      const tocOpen = ref(false);
      const tocQuery = ref("");
      const tocList = ref(DATA.toc || []);
      const tocFiltered = computed(() => {
        const q = tocQuery.value.trim().toLowerCase();
        if (!q) return tocList.value;
        return tocList.value.filter(c =>
          c.t.toLowerCase().includes(q) || String(c.n).includes(q));
      });
      function tocJump() {
        // "Jump to chapter…" search: exact number navigates directly, else first match
        const q = tocQuery.value.trim();
        if (!q) return;
        const n = parseInt(q, 10);
        const max = total;
        const target = (!isNaN(n) && n >= 1 && n <= max) ? n
          : (tocFiltered.value[0] ? tocFiltered.value[0].n : null);
        if (target) {
          tocOpen.value = false;
          window.location.href = `/novel/${novelId}/chapter/${target}`;
        }
      }
      // In-reader glossary/memory editor (same API as the novel page)
      const memOpen = ref(false);
      const memLoading = ref(false);
      const memSaving = ref(false);
      const memSaved = ref(false);
      const memError = ref("");
      const gloss = ref({ characters: [], terms: [] });
      // -------- bookmarks / highlights (UI 2) --------
      const bmOpen = ref(false);
      const bmList = ref([]);
      const bmSel = ref("");          // currently selected text
      const bmNote = ref("");
      const bmSaving = ref(false);
      const selPop = ref({ show: false, x: 0, y: 0 });

      const translated = computed(() => state.value.translated);
      const original = computed(() => state.value.original);
      const isTranslated = computed(() => state.value.is_translated);
      const hasOriginal = computed(() => state.value.has_original);

      // Display text: translation (fallback to original), plus optional original block
      const displayText = computed(() => {
        if (isTranslated.value) return translated.value;
        return original.value;
      });

      // Split content into paragraphs (blank-line separated) for proper spacing
      const paragraphs = computed(() => {
        let list = (displayText.value || "")
          .split(/\n{2,}/)
          .map(s => s.replace(/\s*\n\s*/g, " ").trim())
          // strip leaked markdown artifacts the translator sometimes emits
          .map(s => s.replace(/^#{1,6}\s*/, "").trim())
          .filter(Boolean);
        // The translator often emits the chapter title as the first line —
        // drop it when it duplicates the page heading (or just starts with it).
        const h1 = ((DATA.title_translated || DATA.title || "") + "")
          .replace(/^Chapter\s+\d+[:\s-]*/i, "").trim().toLowerCase();
        if (h1 && list.length > 1) {
          const first = list[0].toLowerCase();
          if (first === h1 || first.includes(h1.slice(0, 24)) || h1.includes(first)) {
            list = list.slice(1);
          }
        }
        return list;
      });

      // Original text split into paragraphs (for the 雙語 original block).
      // Chinese webnovel source uses one line per paragraph (single \n).
      const originalParas = computed(() => {
        return (original.value || "")
          .split(/\n+/)
          .map(s => s.trim())
          .filter(Boolean);
      });

      // ---- per-paragraph bilingual alignment (UI 1) ----
      const hoverPara = ref(-1);       // index of translated paragraph being hovered
      const hoverPos = ref({ x: 0, y: 0 });
      // Character-position proportional mapping: translated and original paragraphs
      // differ in count (translation may add a title, merge/split lines), so map by
      // each paragraph's cumulative character position within its text — the original
      // paragraph whose center is at the same text fraction as the hovered one.
      const origCenters = computed(() => {
        const paras = originalParas.value;
        if (!paras.length) return [];
        const total = paras.reduce((s, p) => s + p.length, 0) || 1;
        let acc = 0;
        return paras.map(p => {
          const center = (acc + p.length / 2) / total;
          acc += p.length;
          return center;
        });
      });
      const hoverOriginal = computed(() => {
        if (hoverPara.value < 0) return "";
        const m = paragraphs.value.length;
        if (!m) return "";
        const total = paragraphs.value.reduce((s, p) => s + p.length, 0) || 1;
        let acc = 0;
        for (let i = 0; i < hoverPara.value; i++) acc += paragraphs.value[i].length;
        const target = (acc + paragraphs.value[hoverPara.value].length / 2) / total;
        // nearest original paragraph center
        const centers = origCenters.value;
        let best = 0, bestDist = Infinity;
        for (let j = 0; j < centers.length; j++) {
          const d = Math.abs(centers[j] - target);
          if (d < bestDist) { bestDist = d; best = j; }
        }
        return originalParas.value[best] || "";
      });
      function showHover(i, ev) {
        hoverPara.value = i;
        const rect = ev.currentTarget.getBoundingClientRect();
        hoverPos.value = { x: rect.left, y: rect.top - 8 };
      }
      function hideHover() { hoverPara.value = -1; }
      // ---- delegated paragraph hover (perf): one listener on the container
      // instead of @mouseenter/@mouseleave closures on every <p> (a 500-para
      // chapter would otherwise mount 1000 handlers). Also skip entirely on
      // devices that can't hover (phones/tablets): the bilingual popup is
      // mouse-only, so touch users shouldn't pay the overhead at all.
      let paraEl = null;
      const CAN_HOVER = window.matchMedia
        ? window.matchMedia("(hover: hover) and (pointer: fine)").matches
        : true;
      function onParaOver(ev) {
        if (!CAN_HOVER) return;
        // walk up from the event target to its paragraph (handles <em>, <strong>
        // and other inline children inside the <p>)
        const el = ev.target && ev.target.closest ? ev.target.closest(".para") : null;
        if (!el || el === paraEl) return;
        paraEl = el;
        const idx = Array.prototype.indexOf.call(el.parentNode.children, el);
        showHover(idx, { currentTarget: el });
      }
      function onParaOut() { paraEl = null; hideHover(); }

      function setTheme(t) {
        theme.value = t; prefs.theme = t; savePrefs();
        document.documentElement.setAttribute("data-theme", t);
      }
      function bumpFont(d) {
        fontSize.value = Math.min(26, Math.max(13, fontSize.value + d));
        prefs.font = fontSize.value; savePrefs();
      }
      function bumpLine(d) {
        lineHeight.value = Math.min(2.4, Math.max(1.3, +(lineHeight.value + d).toFixed(2)));
        prefs.line = lineHeight.value; savePrefs();
      }
      function toggleOrig() {
        showOrig.value = !showOrig.value; prefs.showOrig = showOrig.value; savePrefs();
      }
      function setFontFamily(f) {
        fontFamily.value = f; prefs.fontFamily = f; savePrefs();
        document.documentElement.style.setProperty("--font-read", f === "serif" ? "Georgia, 'Times New Roman', serif" : "'Segoe UI', system-ui, sans-serif");
      }
      function setWidth(w) {
        readerWidth.value = w; prefs.width = w; savePrefs();
      }
      function toggleFocus() {
        focusMode.value = !focusMode.value; prefs.focus = focusMode.value; savePrefs();
        document.body.classList.toggle("reader-focus", focusMode.value);
      }
      function toggleAutoFetch() {
        autoFetch.value = !autoFetch.value;
        prefs.autoFetch = autoFetch.value;
        savePrefs();
        // Turning it OFF must also stop a translate-ahead batch that is
        // already running server-side — flipping the flag alone left the
        // background job preparing chapters forever.
        if (!autoFetch.value && ahead.value.kind === "translate-ahead") {
          stopAhead();
          ahead.value = { running: false, done: 0, total: 0, label: "", kind: null };
        }
        // Re-trigger immediately when turned on and the next chapter is raw.
        if (autoFetch.value) {
          const next = (DATA.toc || []).find(c => c.n === chapterNumber + 1);
          if (next && !next.done) startTranslateAhead();
        }
      }

      // -------- live polling of chapter state --------
      async function pollChapter() {
        const res = await fetch(`/api/novels/${novelId}/chapters/${chapterNumber}`);
        if (!res.ok) return null;
        return res.json();
      }

      function startPolling(everyMs, done) {
        clearInterval(pollTimer.value);
        // Hard timeout so "Translating…" never spins forever if the bg job dies
        const deadline = Date.now() + 5 * 60 * 1000;
        pollTimer.value = setInterval(async () => {
          if (Date.now() > deadline) {
            clearInterval(pollTimer.value);
            busy.value = "";
            error.value = "Translation timed out — try again.";
            return;
          }
          const ch = await pollChapter();
          if (!ch) return;
          state.value = {
            original: ch.original_content || "",
            translated: ch.translated_content || "",
            is_translated: !!ch.is_translated,
            has_original: !!(ch.original_content || ""),
          };
          if (ch.title_translated) titleTranslated.value = ch.title_translated;
          if (ch.is_translated) { clearInterval(pollTimer.value); busy.value = ""; }
          else if (done && done(ch)) { clearInterval(pollTimer.value); busy.value = ""; }
        }, everyMs);
      }

      // -------- actions --------
      async function fetchContent() {
        if (busy.value || hasOriginal.value) return;
        busy.value = "fetching"; error.value = "";
        try {
          const res = await fetch(`/api/novels/${novelId}/chapters/${chapterNumber}/fetch`, { method: "POST" });
          if (!res.ok) throw new Error("fetch failed");
          const data = await res.json();
          if (data.status !== "ok") { busy.value = ""; error.value = "Could not fetch chapter content."; return; }
          // refresh chapter state (content now present)
          const ch = await pollChapter();
          if (ch) {
            state.value = {
              original: ch.original_content || "",
              translated: ch.translated_content || "",
              is_translated: !!ch.is_translated,
              has_original: !!(ch.original_content || ""),
            };
          }
          busy.value = "";
          toast("Chapter content fetched ✓");
        } catch (e) {
          busy.value = ""; error.value = "Fetch failed: " + e.message;
          toast("Fetch failed: " + e.message, true);
        }
      }

      async function translate() {
        if (busy.value || isTranslated.value) return;
        busy.value = "translating"; error.value = "";
        try {
          const res = await fetch(`/api/novels/${novelId}/chapters/${chapterNumber}/translate`, { method: "POST" });
          if (!res.ok) throw new Error("translate start failed");
          const data = await res.json();
          if (data.status === "already_translated") { busy.value = ""; await refreshState(); return; }
          startPolling(4000);
        } catch (e) {
          busy.value = ""; error.value = "Translate failed: " + e.message;
          toast("Translate failed: " + e.message, true);
        }
      }

      async function refreshState() {
        const ch = await pollChapter();
        if (ch) {
          state.value = {
            original: ch.original_content || "",
            translated: ch.translated_content || "",
            is_translated: !!ch.is_translated,
            has_original: !!(ch.original_content || ""),
          };
        }
      }

      // -------- reading progress --------
      let progressTimer = null;
      function saveProgress() {
        const el = document.querySelector(".reader-main");
        if (!el) return;
        const max = el.scrollHeight - window.innerHeight;
        const pct = max > 0 ? Math.min(100, Math.max(0, (window.scrollY / max) * 100)) : 100;
        const bar = document.querySelector(".reader-progress > div");
        if (bar) bar.style.width = pct + "%";
        if (progressTimer) clearTimeout(progressTimer);
        progressTimer = setTimeout(() => {
          fetch(`/api/novels/${novelId}/progress`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ chapter_id: DATA.chapter_id || chapterNumber, scroll_position: Math.round(window.scrollY), percentage: +pct.toFixed(1) }),
          }).catch(() => {});
        }, 600);
      }

      async function restoreProgress() {
        try {
          const res = await fetch(`/api/novels/${novelId}/progress`);
          if (!res.ok) return;
          const p = await res.json();
          // Only restore if the saved progress is for THIS chapter
          if (p.chapter_id === (DATA.chapter_id || chapterNumber) && p.scroll_position > 0) {
            window.scrollTo(0, p.scroll_position);
          }
        } catch (e) {}
      }

      // -------- translate-ahead: queue next chapters so they're ready when you get there --------
      // Prefetch ONLY when (a) the auto-fetch toggle is on AND (b) the *immediately next*
      // chapter is still raw (not translated) — never jump ahead across a gap of already-
      // translated chapters (that would fetch Ch 23+ right after reading Ch 1).
      let aheadTimer = null;
      async function startTranslateAhead() {
        if (prefs.autoFetch === false) {
          // auto-fetch OFF: if a translate-ahead batch is still running
          // server-side (started before the pref changed, or left over from a
          // previous session), stop it — but ONLY translate-ahead. A
          // user-initiated to-end/retranslate/epub job must keep running.
          try {
            const r = await fetch(`/api/novels/${novelId}/batch-status`);
            const b = await r.json();
            if (b.kind === "translate-ahead" && b.running) stopAhead();
          } catch (e) {}
          return;
        }
        const next = (DATA.toc || []).find(c => c.n === chapterNumber + 1);
        if (next && next.done) return;                 // next chapter already translated — nothing to prefetch
        if (chapterNumber + 1 > (DATA.total || total)) return; // no next chapter
        try {
          await fetch(`/api/novels/${novelId}/translate-ahead?after_chapter=${chapterNumber}&count=5`, { method: "POST" });
        } catch (e) {}
        // poll batch status while running
        clearInterval(aheadTimer);
        aheadTimer = setInterval(async () => {
          try {
            const res = await fetch(`/api/novels/${novelId}/batch-status`);
            const b = await res.json();
            // If the user turned auto-fetch OFF while a translate-ahead job was
            // already running (e.g. before onMounted fired), stop it instead of
            // showing a spinner for something they explicitly disabled.
            if (!autoFetch.value && b.kind === "translate-ahead" && b.running) {
              stopAhead();
              ahead.value = { running: false, done: 0, total: 0, label: "", kind: null };
              return;
            }
            ahead.value = { running: !!b.running, done: b.done || 0, total: b.total || 0, label: b.current_label || "", kind: b.kind || null };
            if (!b.running) { clearInterval(aheadTimer); aheadTimer = null; }
          } catch (e) {}
        }, 8000);
      }

      function onKey(e) {
        // ignore when typing in an input/textarea
        const tag = (e.target.tagName || "").toLowerCase();
        if (tag === "input" || tag === "textarea" || tag === "select") return;
        if (e.key === "ArrowLeft" && chapterNumber > 1) { window.location.href = `/novel/${novelId}/chapter/${chapterNumber - 1}`; }
        else if (e.key === "ArrowRight" && chapterNumber < total) { window.location.href = `/novel/${novelId}/chapter/${chapterNumber + 1}`; }
        else if (e.key.toLowerCase() === "t" && !isTranslated.value && hasOriginal.value) { translate(); }
        else if (e.key === "Escape") { tocOpen.value = false; settingsOpen.value = false; focusMode.value = false; prefs.focus = false; savePrefs(); document.body.classList.remove("reader-focus"); }
        else if (e.key === "f") { toggleFocus(); }
      }

      // -------- immersive toolbar: hide on scroll down, show on scroll up --------
      // No auto-show timer: the toolbar stays hidden while you read.
      // Scroll up a little (or reach the top) to bring it back.
      let lastScrollY = window.scrollY;
      function onScrollImmersive() {
        if (focusMode.value) { document.body.classList.remove("toolbar-hidden"); return; }
        const y = window.scrollY;
        const delta = y - lastScrollY;
        lastScrollY = y;
        if (Math.abs(delta) < 8) return;
        if (delta > 0 && y > 220) document.body.classList.add("toolbar-hidden");
        else document.body.classList.remove("toolbar-hidden");
      }

      // -------- mobile swipe: left = next chapter, right = prev --------
      let touchX = null, touchY = null;
      function onTouchStart(e) {
        const t = e.changedTouches[0];
        touchX = t.clientX; touchY = t.clientY;
      }
      function onTouchEnd(e) {
        if (touchX === null) return;
        const t = e.changedTouches[0];
        const dx = t.clientX - touchX, dy = t.clientY - touchY;
        touchX = null; touchY = null;
        if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy) * 1.5) return; // horizontal swipe only
        if (dx < 0 && chapterNumber < total) window.location.href = `/novel/${novelId}/chapter/${chapterNumber + 1}`;
        else if (dx > 0 && chapterNumber > 1) window.location.href = `/novel/${novelId}/chapter/${chapterNumber - 1}`;
      }

      // -------- in-reader glossary/memory editor --------
      function parseCharLines(text) {
        return (text || "").split("\n").map(l => l.trim()).filter(Boolean).map(l => {
          const m = l.match(/^(.*?)\s*\(([^)]+)\)\s*[-–:]\s*(.*)$/);
          if (m) return { type: "character", translated: m[1].trim(), source: m[2].trim(), note: m[3].trim(), locked: false };
          return { type: "character", translated: l, source: "", note: "", locked: false };
        });
      }
      function parseTermLines(text) {
        return (text || "").split("\n").map(l => l.trim()).filter(Boolean).map(l => {
          const m = l.match(/^(.*?)\s*(?:=|->|→)\s*(.*)$/);
          if (m) return { type: "term", source: m[1].trim(), translated: m[2].trim(), note: "", locked: false };
          return { type: "term", source: l, translated: "", note: "", locked: false };
        });
      }
      async function toggleMem() {
        memOpen.value = !memOpen.value;
        if (memOpen.value && gloss.value.characters.length === 0 && gloss.value.terms.length === 0 && !memLoading.value) {
          memLoading.value = true;
          try {
            const res = await fetch(`/api/novels/${novelId}/memory`);
            if (res.ok) {
              const memory = await res.json();
              const entries = memory.glossary_entries || [];
              gloss.value = {
                characters: entries.filter(e => e.type === "character"),
                terms: entries.filter(e => e.type === "term"),
              };
              if (entries.length === 0) {
                gloss.value.characters = parseCharLines(memory.characters);
                gloss.value.terms = parseTermLines(memory.terms);
              }
            }
          } catch (e) {}
          memLoading.value = false;
        }
      }
      function addChar() { gloss.value.characters.push({ type: "character", source: "", translated: "", note: "", locked: false }); }
      function addTerm() { gloss.value.terms.push({ type: "term", source: "", translated: "", note: "", locked: false }); }
      function removeEntry(list, idx) { list.splice(idx, 1); }
      async function saveMemory() {
        if (memSaving.value) return;
        memSaving.value = true; memSaved.value = false; memError.value = "";
        try {
          const entries = [...gloss.value.characters, ...gloss.value.terms];
          const res = await fetch(`/api/novels/${novelId}/memory`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ glossary_entries: entries }),
          });
          if (!res.ok) throw new Error("save failed");
          memSaved.value = true;
          setTimeout(() => { memSaved.value = false; }, 3000);
        } catch (e) {
          memError.value = e.message;
        } finally {
          memSaving.value = false;
        }
      }

      // -------- bookmarks / highlights --------
      async function loadBookmarks() {
        try {
          const res = await fetch(`/api/novels/${novelId}/bookmarks`);
          if (res.ok) bmList.value = await res.json();
        } catch (e) {}
      }
      function onTextSelect() {
        const sel = window.getSelection();
        if (!sel || sel.isCollapsed || sel.rangeCount === 0) { selPop.value.show = false; return; }
        const text = sel.toString().trim();
        if (text.length < 3 || text.length > 2000) { selPop.value.show = false; return; }
        const rect = sel.getRangeAt(0).getBoundingClientRect();
        selPop.value = { show: true, x: rect.left, y: rect.bottom + 6 };
        bmSel.value = text;
      }
      function hideSelPop() { selPop.value.show = false; }
      async function saveBookmark() {
        if (bmSaving.value || !bmSel.value) return;
        bmSaving.value = true;
        try {
          const res = await fetch(`/api/chapters/${DATA.chapter_id}/bookmarks`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ quote: bmSel.value, note: bmNote.value }),
          });
          if (!res.ok) throw new Error("bookmark save failed");
          bmNote.value = "";
          selPop.value.show = false;
          toast("🔖 Bookmark saved");
          loadBookmarks();
        } catch (e) {
          toast("Bookmark failed: " + e.message, true);
        } finally {
          bmSaving.value = false;
        }
      }
      async function removeBookmark(id) {
        try {
          const res = await fetch(`/api/bookmarks/${id}`, { method: "DELETE" });
          if (!res.ok) throw new Error("delete failed");
          bmList.value = bmList.value.filter(b => b.id !== id);
          toast("Bookmark removed");
        } catch (e) {
          toast("Delete failed: " + e.message, true);
        }
      }
      function toggleBookmarks() {
        bmOpen.value = !bmOpen.value;
        if (bmOpen.value) loadBookmarks();
      }

      // -------- "Previously on…" recap (from AI memory) --------
      const recapOpen = ref(false);
      const recap = computed(() => DATA.recap || {});
      const hasRecap = computed(() => !!(recap.value.arc || recap.value.chapter));
      // -------- personal diary ("My thoughts") --------
      const diaryText = ref("");
      const diaryLoaded = ref(false);
      const diarySaving = ref(false);
      const diarySaved = ref(false);
      const diaryOpen = ref(false);

      async function loadDiary() {
        try {
          const res = await fetch(`/api/chapters/${DATA.chapter_id}/diary`);
          if (res.ok) {
            const d = await res.json();
            diaryText.value = d.content || "";
          }
        } catch (e) {}
        diaryLoaded.value = true;
      }
      async function saveDiary() {
        diarySaving.value = true;
        try {
          const res = await fetch(`/api/chapters/${DATA.chapter_id}/diary`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content: diaryText.value }),
          });
          if (!res.ok) throw new Error("save failed");
          diarySaved.value = true;
          setTimeout(() => (diarySaved.value = false), 2000);
          toast(diaryText.value.trim() ? "✍️ Diary saved" : "Diary entry removed");
        } catch (e) {
          toast("Diary save failed: " + e.message, true);
        } finally {
          diarySaving.value = false;
        }
      }
      function toggleDiary() {
        diaryOpen.value = !diaryOpen.value;
        if (diaryOpen.value && !diaryLoaded.value) loadDiary();
      }

      // -------- scroll progress bar (UI: #1) --------
      const scrollPct = ref(0);
      function onScrollProgress() {
        const h = document.documentElement;
        const max = h.scrollHeight - h.clientHeight;
        scrollPct.value = max > 0 ? Math.min(100, Math.round((h.scrollTop / max) * 100)) : 0;
      }
      // next chapter title for the end-of-chapter card (from TOC data; keys: n, t)
      const nextChapterTitle = computed(() => {
        const toc = DATA.toc || [];
        const next = toc.find(c => c.n === chapterNumber + 1);
        return next ? (next.t || "") : "";
      });
      const prevChapterTitle = computed(() => {
        const toc = DATA.toc || [];
        const prev = toc.find(c => c.n === chapterNumber - 1);
        return prev ? (prev.t || "") : "";
      });
      function jumpDiary() { toggleDiary(); }

      // Human label for the running batch — never claim "Preparing next
      // chapters…" when the job is actually a user-initiated to-end /
      // retranslate / check-updates run.
      const KIND_LABEL = {
        "translate-ahead": "Preparing next chapters…",
        "to-end": "Translating entire novel…",
        retranslate: "Re-translating…",
        titles: "Translating titles…",
        match: "Re-translating matches…",
        updates: "Checking for new chapters…",
        "retry-failed": "Retrying failed chapters…",
        epub: "Building EPUB…",
        "retranslate-drift": "Fixing glossary drift…",
      };
      const aheadLabel = computed(() => KIND_LABEL[ahead.value.kind] || "Working…");

      onMounted(() => {
        document.documentElement.setAttribute("data-theme", theme.value);
        setFontFamily(fontFamily.value);
        document.body.classList.toggle("reader-focus", focusMode.value);
        window.addEventListener("scroll", saveProgress, { passive: true });
        window.addEventListener("scroll", onScrollImmersive, { passive: true });
        window.addEventListener("scroll", onScrollProgress, { passive: true });
        window.addEventListener("keydown", onKey);
        onScrollProgress();
        document.addEventListener("mouseup", onTextSelect);
        document.addEventListener("mousedown", (e) => {
          if (!e.target.closest(".sel-pop")) hideSelPop();
        });
        document.querySelector(".reader-main")?.addEventListener("touchstart", onTouchStart, { passive: true });
        document.querySelector(".reader-main")?.addEventListener("touchend", onTouchEnd, { passive: true });
        restoreProgress();
        saveProgress();
        startTranslateAhead();
      });
      onUnmounted(() => {
        clearInterval(pollTimer.value);
        if (aheadTimer) clearInterval(aheadTimer);
        window.removeEventListener("scroll", saveProgress);
        window.removeEventListener("scroll", onScrollImmersive);
        window.removeEventListener("keydown", onKey);
        document.querySelector(".reader-main")?.removeEventListener("touchstart", onTouchStart);
        document.querySelector(".reader-main")?.removeEventListener("touchend", onTouchEnd);
      });

      return {
        state, theme, fontSize, lineHeight, showOrig, busy, error,
        translated, original, isTranslated, hasOriginal, displayText, paragraphs, originalParas,
        hoverPara, hoverPos, hoverOriginal, showHover, hideHover, onParaOver, onParaOut,
        bmOpen, bmList, bmSel, bmNote, bmSaving, selPop,
        titleTranslated, novelTitleTranslated, ahead, stoppingAhead, stopAhead, aheadLabel, KIND_LABEL,
        tocOpen, tocQuery, tocList, tocFiltered, tocJump,
        memOpen, memLoading, memSaving, memSaved, memError, gloss,
        fontFamily, readerWidth, focusMode, settingsOpen, autoFetch,
        chapterNumber, total, novelId, DATA,
        setTheme, bumpFont, bumpLine, toggleOrig, fetchContent, translate,
        setFontFamily, setWidth, toggleFocus, toggleAutoFetch,
        toggleMem, addChar, addTerm, removeEntry, saveMemory,
        toggleBookmarks, saveBookmark, removeBookmark, hideSelPop,
        recapOpen, recap, hasRecap,
        diaryText, diaryLoaded, diarySaving, diarySaved, diaryOpen,
        loadDiary, saveDiary, toggleDiary,
        scrollPct, nextChapterTitle, prevChapterTitle, jumpDiary,
      };
    },
    template: `
<div>
  <!-- progress bar -->
  <div class="reader-progress"><div></div></div>

  <!-- focus-mode exit (toolbar is hidden; mobile has no Esc) -->
  <button v-if="focusMode" class="focus-exit" @click="toggleFocus" title="Exit focus mode (Esc)">✕ Exit focus</button>

  <!-- toolbar -->
  <div class="reader-toolbar">
    <div class="tool-group tg-nav">
      <button @click="tocOpen = true" title="Chapter list"><svg class="ic"><use href="#i-menu"/></svg></button>
      <a :href="'/novel/' + novelId" title="Back to chapter list"><svg class="ic"><use href="#i-book"/></svg><span class="nav-label">Chapters</span></a>
      <a v-if="chapterNumber > 1" :href="'/novel/' + novelId + '/chapter/' + (chapterNumber-1)" title="Previous chapter (←)">←</a>
      <a v-if="chapterNumber < total" :href="'/novel/' + novelId + '/chapter/' + (chapterNumber+1)" title="Next chapter (→)">→</a>
    </div>
    <div class="ttl"><strong>{{ novelTitleTranslated || DATA.novel_title }}</strong> · Ch {{ chapterNumber }}/{{ total }}</div>
    <button v-if="!isTranslated && hasOriginal" class="btn small" @click="translate"
            :disabled="!!busy" :title="busy ? 'Translating…' : 'Translate chapter'">
      <span v-if="busy==='translating'" class="spinner"></span>
      {{ busy==='translating' ? 'Translating…' : '🌐 Translate' }}
    </button>
    <span v-else-if="isTranslated" class="badge ok" title="Translated">✓</span>
    <div class="tool-group">
      <button @click="bumpFont(-1)" title="Smaller font">A−</button>
      <button @click="bumpFont(1)" title="Bigger font">A+</button>
    </div>
    <div class="tool-group tg-theme">
      <button @click="setTheme('light')" :class="{on: theme==='light'}" title="Light theme"><svg class="ic"><use href="#i-sun"/></svg><span class="tg-label">Light</span></button>
      <button @click="setTheme('sepia')" :class="{on: theme==='sepia'}" title="Sepia theme"><svg class="ic"><use href="#i-book"/></svg><span class="tg-label">Sepia</span></button>
      <button @click="setTheme('dark')" :class="{on: theme==='dark'}" title="Dark theme"><svg class="ic"><use href="#i-moon"/></svg><span class="tg-label">Dark</span></button>
    </div>
    <div class="tool-group">
      <button @click="toggleBookmarks" :class="{on: bmOpen}" title="Bookmarks & highlights"><svg class="ic"><use href="#i-bookmark"/></svg></button>
      <button @click="toggleMem" :class="{on: memOpen}" title="Edit glossary / memory"><svg class="ic"><use href="#i-chip"/></svg></button>
      <button @click="toggleOrig" :class="{on: showOrig}" title="Show original text under translation">雙語</button>
    </div>
    <div class="tool-group">
      <button @click="settingsOpen = !settingsOpen" :class="{on: settingsOpen}" title="Reading settings"><svg class="ic"><use href="#i-settings"/></svg></button>
      <button @click="toggleFocus" :class="{on: focusMode}" title="Focus mode (F)"><svg class="ic"><use href="#i-expand"/></svg></button>
    </div>
  </div>

  <!-- scroll progress bar -->
  <div class="progress-track"><div class="progress-fill" :style="{width: scrollPct + '%'}"></div></div>

  <!-- settings popover -->
  <div v-if="settingsOpen" class="settings-pop">
    <div class="sp-row"><label>Font</label>
      <div class="tool-group">
        <button :class="{on: fontFamily==='serif'}" @click="setFontFamily('serif')">Serif</button>
        <button :class="{on: fontFamily==='sans'}" @click="setFontFamily('sans')">Sans</button>
      </div>
    </div>
    <div class="sp-row"><label>Width</label>
      <input type="range" min="560" max="1000" step="20" :value="readerWidth" @input="setWidth(+$event.target.value)">
      <span class="muted" style="font-size:11px">{{ readerWidth }}px</span>
    </div>
    <div class="sp-row"><label>Auto-fetch next</label>
      <button class="toggle" :class="{on: autoFetch}" @click="toggleAutoFetch" title="Prefetch & translate the next raw chapters while you read">{{ autoFetch ? 'On' : 'Off' }}</button>
      <span class="muted" style="font-size:11px">only when next chapter is raw</span>
    </div>
    <div class="sp-row"><label>Shortcuts</label><span class="muted" style="font-size:11px">← → chapter · T translate · F focus · Esc close</span></div>
  </div>

  <main class="reader-main" :style="{ fontSize: fontSize + 'px', lineHeight: lineHeight, maxWidth: readerWidth + 'px' }">
    <h1 class="chapter-title">{{ titleTranslated || DATA.title }}</h1>

    <!-- translate-ahead progress -->
    <div v-if="ahead.running" class="banner ahead-bar">
      <span class="spinner"></span>
      <span><strong>{{ aheadLabel }}</strong> {{ ahead.done }}/{{ ahead.total }} · {{ ahead.label }}</span>
      <div class="mini-bar"><div :style="{width: (ahead.total ? ahead.done/ahead.total*100 : 0) + '%'}"></div></div>
      <button class="btn danger small" @click="stopAhead" :disabled="stoppingAhead">{{ stoppingAhead ? 'Stopping…' : '⏹ Stop' }}</button>
    </div>
    <div v-else-if="!ahead.running && ahead.total > 0 && ahead.kind !== 'translate-ahead'" class="banner" style="opacity:.75">
      ✓ {{ ahead.kind ? KIND_LABEL[ahead.kind] || ahead.kind : 'Batch' }} finished — ready when you are.
    </div>
    <div v-else-if="!ahead.running && ahead.kind === 'translate-ahead' && ahead.total > 0" class="banner" style="opacity:.75">
      ✓ Next chapters translated in the background — they're ready when you are.
    </div>

    <!-- "Previously on…" recap (AI memory) -->
    <div v-if="hasRecap" class="recap-card" :class="{open: recapOpen}">
      <button class="recap-toggle" @click="recapOpen = !recapOpen">
        <span class="recap-caret">{{ recapOpen ? '▾' : '▸' }}</span>
        <span class="recap-title">📖 Previously on…</span>
        <span class="recap-hint">arc recap from AI memory</span>
      </button>
      <div v-if="recapOpen" class="recap-body">
        <p v-if="recap.arc" class="recap-line"><strong>Arc:</strong> {{ recap.arc }}</p>
        <p v-if="recap.chapter" class="recap-line"><strong>Last chapter:</strong> {{ recap.chapter }}</p>
      </div>
    </div>

    <div v-if="error" class="banner err">{{ error }}</div>

    <!-- not fetched yet -->
    <div v-if="!hasOriginal && !busy" class="empty-state">
      <div class="empty-emoji">⛁</div>
      <div class="empty-title">This chapter hasn't been downloaded yet</div>
      <div class="empty-sub">Fetch it from the source — it will be AI-translated automatically.</div>
      <div style="margin-top:12px"><button class="btn" @click="fetchContent">⬇ Fetch content</button></div>
    </div>

    <!-- busy fetch -->
    <div v-if="busy==='fetching'" class="banner"><span class="spinner"></span>Fetching chapter content…</div>

    <!-- busy translate -->
    <div v-if="busy==='translating'" class="banner"><span class="spinner"></span><strong>Translating…</strong> can take up to a minute — this page updates automatically.</div>

    <!-- content -->
    <div v-if="hasOriginal">
      <div class="reader-content" :class="{translating: busy==='translating'}"
           @mouseover="onParaOver" @mouseleave="onParaOut">
        <p v-for="(p, j) in paragraphs" :key="j" class="para">{{ p }}</p>
      </div>
      <!-- per-paragraph original overlay: only when 雙語 (showOrig) is on -->
      <div v-if="showOrig && hoverPara >= 0 && hoverOriginal && isTranslated" class="para-orig-pop"
           :style="{left: hoverPos.x + 'px', top: hoverPos.y + 'px'}">
        <div class="pop-head">ORIGINAL</div>
        <div>{{ hoverOriginal }}</div>
      </div>

      <div style="margin-top:22px;text-align:center">
        <span v-if="isTranslated" class="badge ok">✓ Translated</span>
      </div>

      <!-- personal diary: "My thoughts" -->
      <div class="diary-box">
        <button class="recap-toggle" @click="toggleDiary">
          <span>{{ diaryOpen ? '▾' : '▸' }}</span> ✍️ My thoughts
          <span class="muted" style="font-size:11px;font-weight:normal">(personal notes — visible on your Story so far page)</span>
        </button>
        <div v-if="diaryOpen" class="diary-body">
          <textarea v-model="diaryText" rows="4" placeholder="What did you think of this chapter? Feelings, predictions, favorite lines…"
                    class="diary-input"></textarea>
          <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
            <button class="btn small" @click="saveDiary" :disabled="diarySaving">
              {{ diarySaving ? 'Saving…' : '💾 Save' }}
            </button>
            <span v-if="diarySaved" class="badge ok">Saved</span>
            <a class="btn ghost small" :href="'/novel/' + novelId + '/review'">📖 View story so far</a>
          </div>
        </div>
      </div>
    </div>

    <!-- end-of-chapter card -->
    <div class="chapter-end">
      <div class="ce-divider"><span>— End of Chapter {{ chapterNumber }} —</span></div>
      <div v-if="chapterNumber < total" class="ce-next">
        <div class="ce-label">UP NEXT</div>
        <a class="ce-next-link" :href="'/novel/' + novelId + '/chapter/' + (chapterNumber+1)">
          <span class="ce-next-num">Ch {{ chapterNumber + 1 }}</span>
          <span class="ce-next-title">{{ nextChapterTitle || 'Next chapter' }}</span>
          <span class="ce-arrow">→</span>
        </a>
      </div>
      <div class="ce-actions">
        <a v-if="chapterNumber > 1" class="btn ghost small" :href="'/novel/' + novelId + '/chapter/' + (chapterNumber-1)">← Prev{{ prevChapterTitle ? '' : '' }}</a>
        <span v-else></span>
        <button class="btn ghost small" @click="jumpDiary">✍️ My thoughts</button>
        <a class="btn ghost small" :href="'/novel/' + novelId">📚 Chapters</a>
        <a v-if="chapterNumber < total" class="btn small" :href="'/novel/' + novelId + '/chapter/' + (chapterNumber+1)">Next →</a>
        <span v-else></span>
      </div>
      <div class="ce-jump">
        <span class="muted" style="font-size:12px">Jump to:</span>
        <input type="number" :value="chapterNumber" @change="jumpTo($event)" min="1" :max="total" style="width:70px">
        <button class="btn ghost small" @click="jumpBtn">Go</button>
      </div>
    </div>
  </main>

  <!-- TOC drawer -->
  <div v-if="tocOpen" class="toc-overlay" @click.self="tocOpen = false"></div>
  <aside class="toc-drawer" :class="{open: tocOpen}">
    <div class="toc-head">
      <strong>{{ novelTitleTranslated || DATA.novel_title }}</strong>
      <button class="btn ghost small" @click="tocOpen = false">✕</button>
    </div>
    <input type="search" v-model="tocQuery" placeholder="Jump to chapter… (type a number, Enter to go)" class="toc-search" @keyup.enter="tocJump">
    <div class="toc-list">
      <a v-for="c in tocFiltered" :key="c.n" class="toc-item" :class="{cur: c.n === chapterNumber, undone: !c.done}"
         :href="'/novel/' + novelId + '/chapter/' + c.n" @click="tocOpen = false">
        <span class="toc-num">{{ c.n }}</span>
        <span class="toc-t">{{ c.t }}</span>
        <span v-if="c.done" class="st done">✓</span>
      </a>
      <div v-if="tocFiltered.length === 0" class="muted" style="padding:12px">No chapters match.</div>
    </div>
  </aside>

  <!-- text-selection bookmark popup -->
  <div v-if="selPop.show" class="sel-pop" :style="{left: selPop.x + 'px', top: selPop.y + 'px'}">
    <textarea v-model="bmNote" rows="2" placeholder="Note (optional)…" @keydown.stop></textarea>
    <button class="btn small" @click="saveBookmark" :disabled="bmSaving">{{ bmSaving ? 'Saving…' : '🔖 Save bookmark' }}</button>
    <button class="btn ghost small" @click="hideSelPop">✕</button>
  </div>

  <!-- bookmarks drawer -->
  <div v-if="bmOpen" class="toc-overlay" @click.self="bmOpen = false"></div>
  <aside class="toc-drawer" :class="{open: bmOpen}">
    <div class="toc-head">
      <strong>🔖 Bookmarks</strong>
      <button class="btn ghost small" @click="bmOpen = false">✕</button>
    </div>
    <div class="toc-list">
      <div v-if="bmList.length === 0" class="muted" style="padding:12px">No bookmarks yet — select text in the chapter to highlight & save.</div>
      <div v-for="b in bmList" :key="b.id" class="bm-item">
        <a :href="'/novel/' + novelId + '/chapter/' + b.chapter_number" class="bm-chap">Ch {{ b.chapter_number }}</a>
        <div class="bm-quote">“{{ b.quote.length > 160 ? b.quote.slice(0,160) + '…' : b.quote }}”</div>
        <div v-if="b.note" class="bm-note">📝 {{ b.note }}</div>
        <button class="btn danger tiny" @click="removeBookmark(b.id)">✕</button>
      </div>
    </div>
  </aside>

  <!-- Glossary / memory editor drawer (same as novel page) -->
  <div v-if="memOpen" class="toc-overlay" @click.self="memOpen = false"></div>
  <aside class="toc-drawer gloss-drawer" :class="{open: memOpen}">
    <div class="toc-head">
      <strong>🧠 Glossary & AI Memory</strong>
      <button class="btn ghost small" @click="memOpen = false">✕</button>
    </div>
    <div class="toc-list" style="padding-bottom:80px">
      <div v-if="memLoading" class="muted" style="padding:12px">Loading…</div>
      <template v-else>
        <h4 class="mem-sec-title">👥 Characters</h4>
        <div v-for="(e, i) in gloss.characters" :key="'c'+i" class="gloss-row">
          <input v-model="e.translated" placeholder="EN name" class="g-in g-name">
          <input v-model="e.source" placeholder="Original" class="g-in g-src">
          <input v-model="e.note" placeholder="Note" class="g-in g-note">
          <label class="lock" title="Lock: AI must keep this translation">
            🔒<input type="checkbox" v-model="e.locked">
          </label>
          <button class="btn danger tiny" @click="removeEntry(gloss.characters, i)">✕</button>
        </div>
        <button class="btn small" style="margin-bottom:10px" @click="addChar">+ Add character</button>

        <h4 class="mem-sec-title">📖 Terms</h4>
        <div v-for="(e, i) in gloss.terms" :key="'t'+i" class="gloss-row">
          <input v-model="e.source" placeholder="Original term" class="g-in g-src">
          <input v-model="e.translated" placeholder="EN term" class="g-in g-name">
          <input v-model="e.note" placeholder="Note" class="g-in g-note">
          <label class="lock" title="Lock: AI must keep this translation">
            🔒<input type="checkbox" v-model="e.locked">
          </label>
          <button class="btn danger tiny" @click="removeEntry(gloss.terms, i)">✕</button>
        </div>
        <button class="btn small" @click="addTerm">+ Add term</button>

        <div style="margin-top:14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="btn" @click="saveMemory" :disabled="memSaving">{{ memSaving ? 'Saving…' : '💾 Save' }}</button>
          <span v-if="memSaved" class="badge ok">✓ Saved</span>
          <span v-if="memError" class="banner err" style="margin:0">{{ memError }}</span>
        </div>
        <p class="muted" style="margin-top:10px;font-size:11px">🔒 Locked names are fed to the translator as mandatory — it will never change them. Save applies to future translations.</p>
      </template>
    </div>
  </aside>
</div>`,
    methods: {
      jumpTo(e) {
        let n = parseInt(e.target.value, 10);
        if (!n || n < 1) n = 1;
        if (n > this.total) n = this.total;
        window.location.href = `/novel/${this.novelId}/chapter/${n}`;
      },
      jumpBtn() {
        const inp = document.querySelector(".ce-jump input");
        if (inp) this.jumpTo({ target: inp });
      },
    },
  });

  app.mount("#reader-app");
})();
