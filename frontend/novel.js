/* Novel page — Vue-powered: hero, searchable chapter list, fetch-more, delete.
   Loaded on /novel/:id. Uses window.__NOVEL__ = { novel, chapters } */
(function () {
  const DATA = window.__NOVEL__;
  if (!DATA) return;

  const { createApp, ref, computed, watch, onMounted } = Vue;

  const app = createApp({
    setup() {
      const novel = ref(DATA.novel);
      const chapters = ref(DATA.chapters);
      const memory = ref(null);
      const memOpen = ref(false);
      const memLoading = ref(false);
      const memSaving = ref(false);
      const memSaved = ref(false);
      const retranslating = ref(false);
      const translatingTitles = ref(false);
      const translatingAll = ref(false);
      const checking = ref(false);
      const shelfStatus = ref(novel.value.reading_status || "ongoing");
      const failedCount = ref(0);
      const retryingFailed = ref(false);
      const translatingMeta = ref(false);
      const stoppingBatch = ref(false);

      const SHELF_LABEL = { ongoing: "Ongoing", read_later: "Read Later", done: "Done", dropped: "Dropped" };
      const SHELF_ICON = { ongoing: "i-book-open", read_later: "i-bookmark", done: "i-check", dropped: "i-trash" };
      function shelfLabel(k) { return SHELF_LABEL[k] || k; }
      function shelfIcon(k) { return SHELF_ICON[k] || "i-book"; }

      async function stopBatch() {
        if (stoppingBatch.value) return;
        stoppingBatch.value = true;
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/batch-stop`, { method: "POST" });
          if (res.ok) {
            const d = await res.json();
            if (d.status === "stopped") note.value = "Stopping after the current chapter…";
            else note.value = "No batch is running.";
          }
        } catch (e) { error.value = "Stop failed: " + e.message; }
        stoppingBatch.value = false;
      }
      const exportingEpub = ref(false);
      const epubReady = ref(false);
      const generatingCover = ref(false);
      const uploadingCover = ref(false);
      const coverInput = ref(null);
      const driftCount = ref(0);
      const fixingDrift = ref(false);
      const moreOpen = ref(false);
      const moreMenu = ref(null);
      const showOrig = ref(false);
      const descOpen = ref(false);
      // is the synopsis long enough to warrant a Read-more toggle?
      const descLong = computed(() =>
        (novel.value.description_translated || novel.value.description || "").length > 400
      );
      // "Read" target: next unread chapter or latest translated
      const readTarget = computed(() => {
        const readChaps = chapters.value.filter(c => c.is_read).map(c => c.chapter_number);
        const maxRead = readChaps.length ? Math.max(...readChaps) : 0;
        const nextToRead = chapters.value.find(c => c.chapter_number > maxRead && c.is_translated);
        if (nextToRead) return nextToRead.chapter_number;
        const tr = chapters.value.filter(c => c.is_translated).map(c => c.chapter_number);
        if (tr.length) return Math.max(...tr);
        return 1;
      });
      const readButtonText = computed(() => {
        const readCount = chapters.value.filter(c => c.is_read).length;
        if (readCount === 0) return `Read Ch ${readTarget.value}`;
        return `Continue Ch ${readTarget.value}`;
      });
      const parsedDesc = computed(() => {
        const desc = novel.value.description_translated || novel.value.description || "";
        const tagRegex = /\[([^\]]+)\]/g;
        const tags = [];
        let m;
        while ((m = tagRegex.exec(desc)) !== null) {
          tags.push(m[1].trim());
        }
        const textWithoutTags = desc.replace(/\[[^\]]+\]/g, "").trim();
        return { tags, text: textWithoutTags || desc };
      });
      const progressPct = computed(() => {
        if (!novel.value.total_chapters) return 0;
        return Math.round((translatedCount.value / novel.value.total_chapters) * 100);
      });
      // Editable copy: {characters: [], terms: []}
      const gloss = ref({ characters: [], terms: [] });
      const q = ref("");
      const filter = ref("all"); // all | translated | pending
      const searchMode = ref("titles"); // titles | content
      const contentResults = ref([]);
      const contentSearching = ref(false);
      const contentSearched = ref(false);
      // chapter-list pagination — init from ?page=N URL param so refresh /
      // browser-back keeps your place (server-rendered page, no SPA routing)
      const INIT_PAGE = Math.max(1, parseInt(new URLSearchParams(location.search).get("page") || "1", 10) || 1);
      const listPage = ref(INIT_PAGE);
      const PER_PAGE = 40;
      const fetching = ref(false);
      const deleting = ref(false);
      const error = ref("");
      const note = ref("");
      const batch = ref({ kind: null, total: 0, done: 0, current_label: "", running: false });
      let batchTimer = null;

      const translatedCount = computed(() => chapters.value.filter(c => c.is_translated).length);

      // Defined in frontend/lib/text.js (loaded before this script — see
      // backend/main.py _page()), so the same logic is covered by
      // tests/frontend/*.test.mjs instead of a copy that can drift.
      const { hasContent, parseCharLines, parseTermLines, safeCoverUrl } = window.NyaaText;

      // Full filtered list (search + status filter)
      const filtered = computed(() => {
        let list = chapters.value;
        const query = q.value.trim().toLowerCase();
        if (query) {
          list = list.filter(c =>
            (c.title || "").toLowerCase().includes(query) ||
            (c.title_translated || "").toLowerCase().includes(query) ||
            String(c.chapter_number).includes(query));
        }
        if (filter.value === "translated") list = list.filter(c => c.is_translated);
        if (filter.value === "pending") list = list.filter(c => !c.is_translated);
        return list;
      });

      // Current page slice of the filtered list
      const visible = computed(() => {
        const start = (listPage.value - 1) * PER_PAGE;
        return filtered.value.slice(start, start + PER_PAGE);
      });
      const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / PER_PAGE)));

      function syncPageUrl(p) {
        // keep ?page=N in the URL without reloading (replaceState, not pushState)
        try {
          const u = new URL(location.href);
          const clamped = Math.max(1, p);
          if (clamped === 1) u.searchParams.delete("page");
          else u.searchParams.set("page", String(clamped));
          history.replaceState(null, "", u.toString());
        } catch (e) { /* noop */ }
      }
      function goListPage(delta) {
        const n = Math.min(totalPages.value, Math.max(1, listPage.value + delta));
        listPage.value = n;
        syncPageUrl(n);
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
      function goToPage(n) {
        const p = Math.min(totalPages.value, Math.max(1, parseInt(n) || 1));
        listPage.value = p;
        syncPageUrl(p);
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
      // Page-number buttons with ellipsis: [1] … [4] [5] [6] … [12]
      const pageNumbers = computed(() => {
        const total = totalPages.value;
        const cur = listPage.value;
        const out = [];
        const push = (p) => { if (!out.includes(p)) out.push(p); };
        push(1);
        if (total <= 7) { for (let i = 2; i <= total; i++) push(i); return out; }
        for (let i = Math.max(2, cur - 1); i <= Math.min(total - 1, cur + 1); i++) push(i);
        push(total);
        // mark gaps with -1
        const withGaps = [];
        let prev = 0;
        for (const p of out.sort((a, b) => a - b)) {
          if (prev && p - prev > 1) withGaps.push(-1);
          withGaps.push(p);
          prev = p;
        }
        return withGaps;
      });
      function jumpToPageInput() {
        const el = document.getElementById("pageJumpInput");
        if (el) { const v = el.value; if (v) { goToPage(v); el.value = ""; } }
      }
      // Reset to page 1 when search/filter changes & debounce in-text search
      let searchDebounce = null;
      watch([q, filter], ([newQ]) => {
        listPage.value = 1;
        syncPageUrl(1);
        if (searchMode.value === "content") {
          clearTimeout(searchDebounce);
          if (!newQ || !newQ.trim()) {
            contentResults.value = [];
            contentSearched.value = false;
          } else {
            searchDebounce = setTimeout(searchContent, 300);
          }
        }
      });

      // If the URL asked for a page beyond the real range (stale link, fewer
      // chapters now), clamp to the last page and fix the URL once.
      // immediate: fires with the initial computed value too (a lazy watcher
      // would never run if totalPages settles without changing).
      watch(totalPages, (tp) => {
        if (listPage.value > tp) { listPage.value = tp; syncPageUrl(tp); }
      }, { immediate: true });

      // Full-text search inside translated content (UI 3)
      async function searchContent() {
        const query = q.value.trim();
        if (query.length < 2) { contentResults.value = []; contentSearched.value = false; return; }
        contentSearching.value = true; contentSearched.value = false;
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/search`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ q: query }),
          });
          if (res.ok) {
            const data = await res.json();
            contentResults.value = data.results || [];
            contentSearched.value = true;
          }
        } catch (e) {
          contentResults.value = []; contentSearched.value = true;
        } finally {
          contentSearching.value = false;
        }
      }
      function setSearchMode(mode) {
        searchMode.value = mode;
        if (mode === "content") searchContent();
      }
      async function loadDriftCount() {
        try {
          const r = await fetch(`/api/novels/${novel.value.id}/drift-count`);
          if (r.ok) driftCount.value = (await r.json()).drift || 0;
        } catch (e) {}
      }
      async function fixDrift() {
        if (fixingDrift.value || driftCount.value === 0) return;
        fixingDrift.value = true;
        try {
          const r = await fetch(`/api/novels/${novel.value.id}/retranslate-drift`, { method: "POST" });
          const d = await r.json();
          if (d.status === "started") {
            note.value = `Retranslating ${d.pending} drifted chapter(s) with locked names…`;
            pollBatch();
          } else if (d.status === "none") {
            note.value = "No drift found — locked names are consistent.";
            driftCount.value = 0;
          }
        } catch (e) {
          error.value = "Drift fix failed: " + e.message;
        } finally {
          fixingDrift.value = false;
        }
      }

      async function loadFailedCount() {
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/failed-count`);
          if (res.ok) failedCount.value = (await res.json()).failed || 0;
        } catch (e) {}
      }
      async function retryFailed() {
        if (retryingFailed.value || failedCount.value === 0) return;
        retryingFailed.value = true;
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/retry-failed`, { method: "POST" });
          const d = await res.json();
          if (d.status === "started") {
            note.value = `Retrying ${d.pending} failed chapter(s) in the background…`;
            pollBatch();
          } else if (d.status === "none") {
            note.value = "No failed chapters to retry.";
            failedCount.value = 0;
          }
        } catch (e) {
          error.value = "Retry failed: " + e.message;
        } finally {
          retryingFailed.value = false;
        }
      }
      async function exportEpub() {
        if (exportingEpub.value) return;
        exportingEpub.value = true;
        epubReady.value = false;
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/export-epub`, { method: "POST" });
          const d = await res.json();
          if (d.status === "started") {
            note.value = "Building EPUB in the background…";
            pollBatch();
          }
        } catch (e) {
          error.value = "EPUB export failed: " + e.message;
        } finally {
          exportingEpub.value = false;
        }
      }
      async function generateCover() {
        if (generatingCover.value) return;
        generatingCover.value = true;
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/generate-cover`, { method: "POST" });
          const d = await res.json();
          if (d.status === "ok") {
            novel.value.cover_url = d.cover_url + "?t=" + Date.now();
            note.value = "Cover generated ✨";
          } else {
            error.value = d.detail || "Cover generation failed";
          }
        } catch (e) {
          error.value = "Cover generation failed: " + e.message;
        } finally {
          generatingCover.value = false;
        }
      }
      async function onCoverFile(e) {        const file = e.target.files && e.target.files[0];
        e.target.value = "";
        if (!file || uploadingCover.value) return;
        uploadingCover.value = true;
        try {
          const fd = new FormData();
          fd.append("file", file);
          const res = await fetch(`/api/novels/${novel.value.id}/cover-upload`, { method: "POST", body: fd });
          const d = await res.json();
          if (d.status === "ok") {
            novel.value.cover_url = d.cover_url + "?t=" + Date.now();
            note.value = "Cover uploaded ✓";
          } else {
            error.value = d.detail || "Upload failed";
          }
        } catch (err) {
          error.value = "Upload failed: " + err.message;
        } finally {
          uploadingCover.value = false;
        }
      }

      async function fetchMore() {
        if (fetching.value) return;
        fetching.value = true; error.value = "";
        try {
          const firstMissing = chapters.value.find(c => !hasContent(c));
          const start = firstMissing ? firstMissing.chapter_number : (chapters.value.length + 1);
          const res = await fetch(`/api/novels/${novel.value.id}/fetch-chapters?start=${start}&count=10&translate=true`, { method: "POST" });
          if (!res.ok) throw new Error("fetch-more failed");
          note.value = "Fetching next 10 chapters in the background… refresh in a bit to see new ✓ marks.";
          // poll until the batch lands, then refresh
          pollRefresh();
        } catch (e) {
          error.value = e.message;
        } finally {
          fetching.value = false;
        }
      }

      async function pollRefresh() {
        for (let i = 0; i < 20; i++) {
          await new Promise(r => setTimeout(r, 5000));
          const res = await fetch(`/api/novels/${novel.value.id}/chapters`);
          if (!res.ok) continue;
          const list = await res.json();
          const prev = chapters.value;
          const changed = list.some(c => hasContent(c) !== hasContent(prev.find(x => x.id === c.id)));
          chapters.value = list;
          if (changed || list.every(c => hasContent(c))) { note.value = ""; return; }
        }
        note.value = "Still fetching — refresh manually if needed.";
      }

      async function deleteNovel() {
        if (deleting.value) return;
        if (!confirm("Delete this novel and all its chapters?")) return;
        deleting.value = true;
        try {
          const res = await fetch(`/api/novels/${novel.value.id}`, { method: "DELETE" });
          if (!res.ok) throw new Error("delete failed");
          window.location.href = "/";
        } catch (e) {
          error.value = e.message; deleting.value = false;
        }
      }

      async function toggleMemory() {
        memOpen.value = !memOpen.value;
        if (memOpen.value && !memory.value && !memLoading.value) {
          memLoading.value = true;
          try {
            const res = await fetch(`/api/novels/${novel.value.id}/memory`);
            if (res.ok) {
              memory.value = await res.json();
              // Populate editable copy from structured entries (fallback: parse text)
              const entries = memory.value.glossary_entries || [];
              gloss.value = {
                characters: entries.filter(e => e.type === "character"),
                terms: entries.filter(e => e.type === "term"),
              };
              if (entries.length === 0) {
                gloss.value.characters = parseCharLines(memory.value.characters);
                gloss.value.terms = parseTermLines(memory.value.terms);
              }
            }
          } catch (e) {
            // Silent failure here looked identical to "no glossary yet" —
            // the panel just opened empty either way.
            error.value = "Could not load the glossary: " + e.message;
          }
          memLoading.value = false;
        }
      }

      // Best-effort parse helpers live in lib/text.js (shared with reader.js)

      function addChar() { gloss.value.characters.push({ type: "character", source: "", translated: "", note: "", locked: false }); }
      function addTerm() { gloss.value.terms.push({ type: "term", source: "", translated: "", note: "", locked: false }); }
      function removeEntry(list, idx) { list.splice(idx, 1); }

      async function saveMemory() {
        if (memSaving.value) return;
        memSaving.value = true; memSaved.value = false; error.value = "";
        try {
          const entries = [...gloss.value.characters, ...gloss.value.terms];
          const res = await fetch(`/api/novels/${novel.value.id}/memory`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ glossary_entries: entries }),
          });
          if (!res.ok) throw new Error("save failed");
          memSaved.value = true;
          setTimeout(() => { memSaved.value = false; }, 3000);
        } catch (e) {
          error.value = e.message;
        } finally {
          memSaving.value = false;
        }
      }

      async function retranslate() {
        if (retranslating.value) return;
        if (!confirm("Re-translate ALL translated chapters with the current glossary? This takes a while (one chapter per minute).")) return;
        retranslating.value = true; error.value = "";
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/retranslate`, { method: "POST" });
          if (!res.ok) throw new Error("retranslate start failed");
          note.value = "Re-translating all chapters in the background — this runs chapter by chapter. Refresh later to see updated text.";
        } catch (e) {
          error.value = e.message;
        } finally {
          retranslating.value = false;
        }
        pollBatch();
      }

      async function translateTitles() {
        if (translatingTitles.value) return;
        if (!confirm("Translate the chapter titles that are still in the original language? (Fast — short text each.)")) return;
        translatingTitles.value = true; error.value = "";
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/translate-titles`, { method: "POST" });
          if (!res.ok) throw new Error("title translate start failed");
          note.value = "Translating chapter titles in the background — refresh to see them.";
        } catch (e) {
          error.value = e.message;
        } finally {
          translatingTitles.value = false;
        }
        pollBatch();
      }

      async function translateAll() {
        if (translatingAll.value) return;
        if (!confirm("Translate the ENTIRE remaining novel in the background? ~490 chapters, est. ~$1.50, runs several hours. You can keep reading meanwhile.")) return;
        translatingAll.value = true; error.value = "";
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/translate-to-end`, { method: "POST" });
          if (!res.ok) throw new Error("translate-to-end start failed");
          note.value = "Translating the whole novel in the background — watch the progress bar. Keep reading freely.";
        } catch (e) {
          error.value = e.message;
        } finally {
          translatingAll.value = false;
        }
        pollBatch();
      }

      async function checkUpdates() {
        if (checking.value) return;
        checking.value = true; error.value = "";
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/check-updates`, { method: "POST" });
          if (!res.ok) throw new Error("check failed");
          // Report what actually happened. This used to print "Checked the
          // source…" for EVERY 200, including already_running — so a check that
          // never ran looked identical to one that did.
          const d = await res.json();
          if (d.status === "already_running") {
            note.value = "Another background job is running for this novel — check for updates again once it finishes.";
          } else {
            note.value = "Checking the source for new chapters…";
          }
        } catch (e) {
          error.value = e.message;
        } finally {
          checking.value = false;
        }
        pollBatch();
      }

      async function setShelf() {
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/reading-status`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status: shelfStatus.value }),
          });
          if (!res.ok) throw new Error("shelf update failed");
          note.value = `Moved to ${shelfStatus.value.replace("_", " ")} shelf.`;
        } catch (e) {
          error.value = e.message;
        }
      }

      const memSections = computed(() => {
        const m = memory.value;
        if (!m) return [];
        return [
          { key: "characters", label: "👥 Characters", text: m.characters },
          { key: "terms", label: "📖 Glossary / Terms", text: m.terms },
          { key: "plot", label: "📜 Plot summary", text: m.plot },
          { key: "arc_plot", label: "🏛 Arc progress", text: m.arc_plot },
          { key: "chapter_plot", label: "📝 Recent chapters", text: m.chapter_plot },
        ].filter(s => s.text);
      });

      // If the novel title/desc aren't translated yet, kick off a background
      // translation and poll for the result (existing novels added pre-feature).
      async function pollBatch() {
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/batch-status`);
          const b = await res.json();
          batch.value = { kind: b.kind, total: b.total || 0, done: b.done || 0,
                          current_label: b.current_label || "", running: !!b.running };
          if (b.running) {
            if (!batchTimer) batchTimer = setInterval(pollBatch, 5000);
          } else if (batchTimer) {
            clearInterval(batchTimer); batchTimer = null;
            // when an epub job just finished, reveal the download link
            if (batch.value.kind === "epub" && batch.value.total > 0) epubReady.value = true;
            // Every batch kind now ends with a real outcome label ("Translated
            // 20/20 chapters", "Stopped by user — 5/20…", "No new chapters"…)
            // instead of just vanishing — this used to be special-cased for
            // "updates" only, so every OTHER kind's completion was invisible
            // once its progress panel disappeared (the panel itself is
            // v-if="batch.running", so it unmounts the instant the job ends).
            if (batch.value.kind && batch.value.current_label) {
              note.value = batch.value.current_label;
            }
            // The chapter list below (✓ / EN / ⛁ markers, titles) is part of
            // the server-rendered page data, so a job that changed it is
            // invisible until reload — the same reason "updates" already
            // reloaded. Generalize that to every kind that mutates chapters.
            // "updates" is special: its own done/total count TRANSLATION
            // attempts on the up-to-5 newly found chapters, which can be 0
            // even when chapters WERE added (they just didn't need
            // translating) — its "Added N" label is the real signal there.
            const kind = batch.value.kind;
            const changedChapters = kind === "updates"
              ? /^Added /.test(batch.value.current_label || "")
              : ["to-end", "retranslate", "titles", "retry-failed", "retranslate-drift", "match"].includes(kind)
                && batch.value.done > 0;
            if (changedChapters) {
              note.value = (note.value || "Done") + " — refreshing…";
              setTimeout(() => window.location.reload(), 1200);
            }
          }
        } catch (e) {}
      }

      async function translateMeta() {
        if (translatingMeta.value) return;
        translatingMeta.value = true; note.value = ""; error.value = "";
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/translate-meta`, { method: "POST" });
          if (!res.ok) throw new Error("start failed");
          const d = await res.json();
          if (d.status === "none") note.value = "Title & synopsis already translated.";
          else note.value = `Translating title & synopsis in the background… (${d.pending || 2} item(s))`;
          // poll until the meta batch finishes
          for (let i = 0; i < 24; i++) {
            await new Promise(r => setTimeout(r, 5000));
            const r2 = await fetch(`/api/novels/${novel.value.id}`);
            if (!r2.ok) continue;
            const n = await r2.json();
            novel.value.title_translated = n.title_translated;
            novel.value.description_translated = n.description_translated;
            if (novel.value.title_translated && novel.value.description_translated) { note.value = "Title & synopsis translated ✓"; break; }
          }
        } catch (e) { error.value = "Translate title & synopsis failed: " + e.message; }
        translatingMeta.value = false;
      }

      async function ensureMetaTranslated() {
        // Auto-fire on page load when meta is missing (best-effort, quiet).
        if (novel.value.title_translated && novel.value.description_translated) return;
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/translate-meta`, { method: "POST" });
          if (!res.ok) return;
          const d = await res.json();
          if (d.status === "started") note.value = "Translating title & synopsis…";
        } catch (e) { /* quiet — the visible button covers this case */ }
      }

      // -------- Chapter Range Multi-Select & Batch Actions --------
      const selectMode = ref(false);
      const selectedChapters = ref([]);
      const lastSelectedIdx = ref(null);

      function toggleSelectMode() {
        selectMode.value = !selectMode.value;
        if (!selectMode.value) selectedChapters.value = [];
        lastSelectedIdx.value = null;
      }

      function isSelected(chNum) {
        return selectedChapters.value.includes(chNum);
      }

      function toggleChapterSelect(chNum, idx, ev) {
        if (ev && ev.shiftKey && lastSelectedIdx.value !== null) {
          const start = Math.min(lastSelectedIdx.value, idx);
          const end = Math.max(lastSelectedIdx.value, idx);
          const range = visible.value.slice(start, end + 1).map(c => c.chapter_number);
          const set = new Set(selectedChapters.value);
          range.forEach(n => set.add(n));
          selectedChapters.value = Array.from(set);
        } else {
          const idxInSel = selectedChapters.value.indexOf(chNum);
          if (idxInSel >= 0) selectedChapters.value.splice(idxInSel, 1);
          else selectedChapters.value.push(chNum);
        }
        lastSelectedIdx.value = idx;
      }

      function selectAllVisible() {
        const set = new Set(selectedChapters.value);
        visible.value.forEach(c => set.add(c.chapter_number));
        selectedChapters.value = Array.from(set);
      }

      function clearSelection() {
        selectedChapters.value = [];
        lastSelectedIdx.value = null;
      }

      async function batchTranslateSelected() {
        if (!selectedChapters.value.length) return;
        const list = selectedChapters.value.slice().sort((a, b) => a - b);
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/batch-translate-selected`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ chapters: list }),
          });
          if (res.ok) {
            note.value = `Translating ${list.length} selected chapters in background…`;
            clearSelection();
            selectMode.value = false;
            pollBatch();
          }
        } catch (e) { error.value = "Batch translate failed: " + e.message; }
      }

      async function batchMarkRead(isRead) {
        if (!selectedChapters.value.length) return;
        const list = selectedChapters.value.slice();
        try {
          const res = await fetch(`/api/novels/${novel.value.id}/batch-mark-read`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ chapters: list, is_read: isRead }),
          });
          if (res.ok) {
            chapters.value.forEach(c => {
              if (list.includes(c.chapter_number)) c.is_read = isRead;
            });
            clearSelection();
          }
        } catch (e) { error.value = "Batch mark failed: " + e.message; }
      }

      return { novel, chapters, q, filter, fetching, deleting, error, note, hasContent, safeCoverUrl,
               translatedCount, visible, filtered, fetchMore, deleteNovel, ensureMetaTranslated,
               searchMode, contentResults, contentSearching, contentSearched,
               searchContent, setSearchMode,
               listPage, totalPages, pageNumbers, goListPage, goToPage, jumpToPageInput,
               failedCount, retryingFailed, loadFailedCount, retryFailed,
               translatingMeta, translateMeta,
               stoppingBatch, stopBatch,
               exportingEpub, epubReady, exportEpub, generatingCover, generateCover,
               uploadingCover, coverInput, onCoverFile,
               driftCount, fixingDrift, loadDriftCount, fixDrift,
               moreOpen, moreMenu, showOrig, readTarget, descOpen, descLong,
               memory, memOpen, memLoading, memSaving, memSaved, retranslating, translatingTitles,
               translatingAll, checking, shelfStatus, shelfLabel, shelfIcon,
               gloss, memSections, toggleMemory, saveMemory, retranslate, translateTitles,
               translateAll, checkUpdates, setShelf,
               addChar, addTerm, removeEntry, batch, pollBatch,
               selectMode, selectedChapters, toggleSelectMode, isSelected, toggleChapterSelect,
               selectAllVisible, clearSelection, batchTranslateSelected, batchMarkRead,
               parsedDesc, progressPct, readButtonText };
    },
    mounted() {
      this.ensureMetaTranslated();
      this.pollBatch();
      this.loadFailedCount();
      this.loadDriftCount();
      document.addEventListener("mousedown", (e) => {
        if (this.moreOpen && !e.target.closest(".more-menu")) {
          this.moreOpen = false;
        }
      });
    },
    template: `
<div>
  <header class="topbar">
    <div class="container">
      <a class="brand" href="/"><span class="logo-mark"><svg class="ic ic-lg"><use href="#i-cat"/></svg></span> NyaaReader</a>
      <span class="crumb">{{ novel.title_translated || novel.title }}</span>
      <span class="flex-spacer"></span>
      <nav class="topnav">
        <a class="nav-link" href="/"><svg class="ic"><use href="#i-home"/></svg><span class="nav-label">Library</span></a>
        <a class="nav-link" href="/dashboard"><svg class="ic"><use href="#i-sparkle"/></svg><span class="nav-label">Dashboard</span></a>
        <a class="nav-link" href="/config"><svg class="ic"><use href="#i-settings"/></svg><span class="nav-label">Settings</span></a>
      </nav>
    </div>
  </header>

  <div class="container">
    <div class="hero" :class="{ 'has-bg': !!safeCoverUrl(novel.cover_url) }">
      <!-- blurred cover backdrop -->
      <div class="hero-bg-clip" v-if="safeCoverUrl(novel.cover_url)">
        <div class="hero-bg" :style="safeCoverUrl(novel.cover_url) ? { backgroundImage: 'url(' + safeCoverUrl(novel.cover_url) + ')' } : {}"></div>
      </div>
      <div class="hero-left">
        <div class="hero-cover">
          <img v-if="safeCoverUrl(novel.cover_url)" class="cover" :src="safeCoverUrl(novel.cover_url)" :alt="novel.title">
          <div v-else class="cover" style="display:flex;align-items:center;justify-content:center;font-size:44px">📖</div>
        </div>
        <div class="hero-cover-btns" v-if="!generatingCover && !uploadingCover">
          <button class="hero-cover-btn" @click="generateCover" title="Generate AI cover">🎨 AI Cover</button>
          <button class="hero-cover-btn" @click="coverInput && coverInput.click()" title="Upload cover">⬆ Upload</button>
        </div>
        <div class="hero-cover-btns" v-else>
          <span class="hero-cover-btn spin" style="cursor:default">{{ generatingCover ? '🎨 Generating…' : '⬆ Uploading…' }}</span>
        </div>
      </div>
      <div class="info">
        <div class="hero-meta-row">
          <span class="badge" :class="'st-' + shelfStatus">● {{ shelfLabel(shelfStatus) }}</span>
          <span class="badge ok">✓ {{ translatedCount }} / {{ novel.total_chapters }} translated ({{ progressPct }}%)</span>
          <span class="badge">{{ novel.original_language ? novel.original_language.toUpperCase() : 'ZH' }} → {{ novel.target_language ? novel.target_language.toUpperCase() : 'EN' }}</span>
          <span class="badge" v-if="novel.source_site">{{ novel.source_site }}</span>
        </div>

        <h1>{{ novel.title_translated || novel.title }}</h1>
        <div v-if="novel.title_translated && novel.title !== novel.title_translated" class="byline orig-title">{{ novel.title }}</div>
        <div class="byline author-byline">{{ novel.author || 'Unknown author' }}</div>

        <!-- Tag Chips -->
        <div v-if="parsedDesc.tags.length" class="hero-tags">
          <span v-for="(t, i) in parsedDesc.tags" :key="i" class="tag-chip">{{ t }}</span>
        </div>

        <!-- Synopsis -->
        <div class="desc" :class="{collapsed: !descOpen && !showOrig}">{{ parsedDesc.text }}</div>
        <button v-if="descLong" class="orig-toggle" @click="descOpen = !descOpen">{{ descOpen ? '▾ Show less' : '▸ Read more' }}</button>
        
        <div v-if="novel.description_translated && novel.description !== novel.description_translated" style="margin-top:2px">
          <button class="orig-toggle" @click="showOrig = !showOrig">{{ showOrig ? '▾ Hide' : '▸ Show' }} original text</button>
          <div v-if="showOrig" class="desc desc-orig">{{ novel.description }}</div>
        </div>

        <!-- Action Toolbar -->
        <div class="hero-actions">
          <a class="btn hero-cta" :href="'/novel/' + novel.id + '/chapter/' + readTarget" title="Jump to next unread chapter">
            <svg class="ic"><use href="#i-book"/></svg> {{ readButtonText }}
          </a>
          <button class="btn soft" @click="translateAll" :disabled="translatingAll">
            <svg class="ic"><use href="#i-sparkle"/></svg> Translate to end
          </button>
          <a class="btn soft" :href="'/novel/' + novel.id + '/review'">
            <svg class="ic"><use href="#i-book-open"/></svg> Story so far
          </a>
          <label class="shelf-btn" :title="shelfLabel(shelfStatus)">
            <svg class="ic"><use :href="'#' + shelfIcon(shelfStatus)"/></svg>
            <span class="shelf-btn-label">{{ shelfLabel(shelfStatus) }}</span>
            <select v-model="shelfStatus" @change="setShelf" title="Reading shelf">
              <option value="ongoing">Ongoing</option>
              <option value="read_later">Read Later</option>
              <option value="done">Done</option>
              <option value="dropped">Dropped</option>
            </select>
          </label>
          <div class="more-menu" ref="moreMenu">
            <button class="icon-btn" @click="moreOpen = !moreOpen" title="More actions"><svg class="ic"><use href="#i-menu"/></svg></button>
            <div v-if="moreOpen" class="more-pop">
              <button v-if="novel.source_site !== 'manual'" class="more-item" @click="checkUpdates" :disabled="checking"><svg class="ic"><use href="#i-sparkle"/></svg> Check updates</button>
              <button class="more-item" @click="fetchMore" :disabled="fetching"><svg class="ic"><use href="#i-arrow-down"/></svg>{{ fetching ? 'Fetching…' : 'Fetch next 10' }}</button>
              <button v-if="failedCount > 0" class="more-item" @click="retryFailed" :disabled="retryingFailed"><svg class="ic"><use href="#i-clock"/></svg> Retry {{ failedCount }} failed</button>
              <button v-if="!novel.title_translated || !novel.description_translated" class="more-item" @click="translateMeta" :disabled="translatingMeta"><svg class="ic"><use href="#i-sparkle"/></svg>{{ translatingMeta ? 'Translating…' : 'Translate title & synopsis' }}</button>
              <button v-if="driftCount > 0" class="more-item" @click="fixDrift" :disabled="fixingDrift"><svg class="ic"><use href="#i-search"/></svg> Fix {{ driftCount }} drifted</button>
              <button class="more-item" @click="exportEpub" :disabled="exportingEpub"><svg class="ic"><use href="#i-arrow-down"/></svg>{{ exportingEpub ? 'Building…' : 'Export EPUB' }}</button>
              <a v-if="epubReady" class="more-item" :href="'/api/novels/' + novel.id + '/epub-download'"><svg class="ic"><use href="#i-arrow-down"/></svg> Download EPUB</a>
              <div class="more-divider"></div>
              <button class="more-item danger" @click="deleteNovel" :disabled="deleting"><svg class="ic"><use href="#i-trash"/></svg> {{ deleting ? 'Deleting…' : 'Delete novel' }}</button>
            </div>
          </div>
          <input ref="coverInput" type="file" accept="image/png,image/jpeg,image/webp,image/gif"
                 style="display:none" @change="onCoverFile">
        </div>
        <div v-if="note" class="banner" style="margin-top:10px">✓ {{ note }}</div>
        <div v-if="error" class="banner err" style="margin-top:10px">⚠ {{ error }}</div>
        <!-- background batch progress (retranslate / fetch-more / translate-ahead) -->
        <div v-if="batch.running" class="batch-panel" style="margin-top:12px">
          <div class="batch-head">
            <span class="spinner"></span>
            <span class="batch-title"><strong v-if="batch.kind==='retranslate'">Re-translating…</strong>
                <strong v-else-if="batch.kind==='translate-ahead'">Preparing next chapters…</strong>
                <strong v-else-if="batch.kind==='titles'">Translating titles…</strong>
                <strong v-else-if="batch.kind==='to-end'">Translating entire novel…</strong>
                <strong v-else-if="batch.kind==='match'">Re-translating matches…</strong>
                <strong v-else-if="batch.kind==='updates'">Fetching new chapters…</strong>
                <strong v-else-if="batch.kind==='retry-failed'">Retrying failed chapters…</strong>
                <strong v-else-if="batch.kind==='epub'">Building EPUB…</strong>
                <strong v-else-if="batch.kind==='retranslate-drift'">Fixing glossary drift…</strong>
                <strong v-else-if="batch.kind==='translate-selected'">Translating selected chapters…</strong>
                <strong v-else>Working…</strong></span>
            <span class="batch-count">{{ batch.done }}/{{ batch.total }}</span>
            <button class="btn ghost small batch-stop" @click="stopBatch" :disabled="stoppingBatch">{{ stoppingBatch ? 'Stopping…' : 'Stop' }}</button>
          </div>
          <div class="batch-track">
            <div class="batch-fill" :style="{width: (batch.total ? batch.done/batch.total*100 : 0) + '%'}"></div>
          </div>
          <div v-if="batch.current_label" class="batch-label">{{ batch.current_label }}</div>
        </div>
      </div>
    </div>

    <!-- AI memory / glossary editor -->
    <div class="memory-panel">
      <button class="mem-toggle" @click="toggleMemory">
        <span>{{ memOpen ? '🧠 AI Memory & Glossary ▾' : '🧠 AI Memory & Glossary' }}</span>
        <span class="mem-hint">edit names/terms · lock them so the AI never changes them</span>
      </button>
      <div v-if="memOpen" class="mem-body">
        <div v-if="memLoading" class="muted">Loading memory…</div>
        <div v-else>
          <div v-if="memSaved" class="banner" style="margin-top:0;margin-bottom:10px">✓ Saved — locked names will be enforced on the next translation.</div>

          <h4 class="mem-sec-title">👥 Characters</h4>
          <div v-if="gloss.characters.length === 0" class="muted" style="margin-bottom:8px">No characters tracked yet.</div>
          <div v-for="(c, i) in gloss.characters" :key="'c'+i" class="gloss-row">
            <input v-model="c.translated" placeholder="Translated name (e.g. Angelia)" class="g-in g-name">
            <input v-model="c.source" placeholder="Original (e.g. 安潔莉雅)" class="g-in g-src">
            <input v-model="c.note" placeholder="Role / note" class="g-in g-note">
            <label class="g-lock" :title="c.locked ? 'Locked — AI must use this name' : 'Unlocked — AI may adjust'">
              <input type="checkbox" v-model="c.locked"> 🔒
            </label>
            <button class="btn danger tiny" @click="removeEntry(gloss.characters, i)">✕</button>
          </div>
          <button class="btn small" @click="addChar">+ Add character</button>

          <h4 class="mem-sec-title" style="margin-top:16px">📖 Terms / Glossary</h4>
          <div v-if="gloss.terms.length === 0" class="muted" style="margin-bottom:8px">No terms tracked yet.</div>
          <div v-for="(t, i) in gloss.terms" :key="'t'+i" class="gloss-row">
            <input v-model="t.source" placeholder="Original term (e.g. 希果)" class="g-in g-src">
            <input v-model="t.translated" placeholder="Translation (e.g. Hopefruit)" class="g-in g-name">
            <input v-model="t.note" placeholder="Note" class="g-in g-note">
            <label class="g-lock" :title="t.locked ? 'Locked — AI must use this term' : 'Unlocked — AI may adjust'">
              <input type="checkbox" v-model="t.locked"> 🔒
            </label>
            <button class="btn danger tiny" @click="removeEntry(gloss.terms, i)">✕</button>
          </div>
          <button class="btn small" @click="addTerm">+ Add term</button>

          <div style="margin-top:16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <button class="btn" @click="saveMemory" :disabled="memSaving">{{ memSaving ? 'Saving…' : '💾 Save glossary' }}</button>
            <button class="btn" @click="retranslate" :disabled="retranslating">{{ retranslating ? 'Starting…' : '🔄 Re-translate all' }}</button>
            <button class="btn ghost" @click="translateTitles" :disabled="translatingTitles">{{ translatingTitles ? 'Starting…' : '🏷 Translate titles only' }}</button>
            <span class="muted" style="font-size:12px">Titles-only is fast (short text, no chapter re-run). Re-translate all is heavy — applies glossary to every translated chapter.</span>
          </div>

          <details class="mem-section" style="margin-top:14px">
            <summary>Plot / arc summaries (read-only, AI-maintained)</summary>
            <div v-if="memSections.length === 0" class="muted">No plot memory yet.</div>
            <div v-for="s in memSections" :key="s.key" style="margin:8px 0">
              <strong style="font-size:12px">{{ s.label }}</strong>
              <div class="mem-text">{{ s.text }}</div>
            </div>
          </details>
        </div>
      </div>
    </div>

    <div class="chapter-toolbar">
      <div class="tb-row">
        <input type="search" v-model="q" placeholder="Search chapters… title or number"
               @keyup.enter="searchMode==='content' && searchContent()">
        <div class="tool-group">
          <button :class="{on: searchMode==='titles'}" @click="setSearchMode('titles')">Titles</button>
          <button :class="{on: searchMode==='content'}" @click="setSearchMode('content')"><svg class="ic"><use href="#i-search"/></svg> In-text</button>
        </div>
        <button class="btn ghost small" :class="{on: selectMode}" @click="toggleSelectMode" title="Multi-select chapters for batch actions">☑ Select</button>
        <button v-if="selectMode" class="btn ghost small" @click="selectAllVisible">Select Page</button>
        <button v-if="selectMode && selectedChapters.length" class="btn ghost small" @click="clearSelection">Clear</button>
        <span class="tb-spacer"></span>
        <div class="tool-group">
          <button :class="{on: filter==='all'}" @click="filter='all'">All</button>
          <button :class="{on: filter==='translated'}" @click="filter='translated'">✓ Translated</button>
          <button :class="{on: filter==='pending'}" @click="filter='pending'">Pending</button>
        </div>
      </div>
      <div class="tb-row" v-if="contentSearched">
        <span class="muted" style="font-size:12px">{{ contentResults.length }} result{{ contentResults.length === 1 ? '' : 's' }} in-text</span>
      </div>
    </div>

    <!-- in-text search results -->
    <div v-if="searchMode==='content'" class="search-results">
      <div v-if="contentSearching" class="muted" style="padding:8px">Searching…</div>
      <div v-else-if="contentSearched && contentResults.length === 0" class="empty-state">
        <div class="empty-emoji">🔍</div>
        <div class="empty-title">Nothing found inside the translations</div>
        <div class="empty-sub">No translated chapter contains “{{ q.trim() }}”. Try a different name, or check chapters not yet translated.</div>
      </div>
      <a v-for="r in contentResults" :key="r.chapter_number" class="search-hit"
         :href="'/novel/' + novel.id + '/chapter/' + r.chapter_number">
        <span class="hit-title">Ch {{ r.chapter_number }} · {{ r.title }} <span class="hit-count">({{ r.count }}×)</span></span>
        <span class="hit-snippet" v-html="r.snippet"></span>
      </a>
    </div>

    <!-- chapter-list pager (TOP — no need to scroll to the bottom) -->
    <div v-if="totalPages > 1" class="pager top-pager">
      <span class="pg-ind">Page {{ listPage }} / {{ totalPages }} · {{ filtered.length }} chapters · showing {{ visible.length }}</span>
      <span class="pager-controls">
        <button class="btn ghost small" :disabled="listPage <= 1" @click="goListPage(-1)">← Prev</button>
        <template v-for="(p, i) in pageNumbers" :key="i">
          <button v-if="p === -1" class="btn ghost small pg-ellipsis" disabled>…</button>
          <button v-else class="btn ghost small pager-page" :class="{active: p === listPage}" @click="goToPage(p)">{{ p }}</button>
        </template>
        <button class="btn ghost small" :disabled="listPage >= totalPages" @click="goListPage(1)">Next →</button>
        <span class="pg-jump">
          <input id="pageJumpInput" type="number" min="1" :max="totalPages" placeholder="Page…" @keyup.enter="jumpToPageInput">
          <button class="btn ghost small" @click="jumpToPageInput" title="Go to page">Go</button>
        </span>
      </span>
    </div>

    <div v-if="visible.length" class="chapter-list">
      <div v-for="(c, i) in visible" :key="c.id" class="chapter-item"
           :class="{'item-selected': isSelected(c.chapter_number)}"
           :style="selectMode ? 'cursor:pointer' : ''"
           @click="selectMode ? toggleChapterSelect(c.chapter_number, i, $event) : null">
        <input v-if="selectMode" type="checkbox" :checked="isSelected(c.chapter_number)"
               @click.stop="toggleChapterSelect(c.chapter_number, i, $event)" class="ch-checkbox">
        <a :href="selectMode ? null : ('/novel/' + novel.id + '/chapter/' + c.chapter_number)"
           style="display:flex;align-items:center;gap:12px;flex:1;color:inherit;text-decoration:none">
          <span class="read-dot" :class="{read: c.is_read}" :title="c.is_read ? 'Read' : 'Not read yet'"></span>
          <span class="num">{{ c.chapter_number }}</span>
          <span class="ttl">
            <span v-if="c.title_translated">{{ c.title_translated }}</span>
            <span v-else>{{ c.title || ('Chapter ' + c.chapter_number) }}</span>
          </span>
          <span v-if="c.is_translated" class="st done" title="Translated">✓</span>
          <span v-else-if="hasContent(c)" class="st fetched" title="Fetched, not translated">EN</span>
          <span v-else class="st missing" title="Not fetched yet">⛁</span>
        </a>
      </div>
    </div>
    <div v-else class="empty-state">
      <div class="empty-emoji">{{ q ? '🔍' : '📄' }}</div>
      <div class="empty-title" v-if="q">No chapters match “{{ q }}”</div>
      <div class="empty-title" v-else>No chapters here yet</div>
      <div class="empty-sub" v-if="q">Try a different search, or switch to <button class="btn ghost small" @click="setSearchMode('content')"><svg class="ic"><use href="#i-search"/></svg> In-text</button> to search inside translations.</div>
      <div class="empty-sub" v-else>Use <button class="btn ghost small" @click="fetchMore" :disabled="fetching">⬇ Fetch next 10</button> to download chapters from the source.</div>
    </div>

    <!-- Floating Batch Action Bar -->
    <div v-if="selectedChapters.length" class="batch-bar">
      <div class="batch-bar-count">
        <span>Selected <strong>{{ selectedChapters.length }}</strong> ch</span>
        <span class="muted" style="font-size:11px">(Shift+Click for range)</span>
      </div>
      <div class="batch-bar-btns">
        <button class="btn small" @click="batchTranslateSelected">⚡ Translate ({{ selectedChapters.length }})</button>
        <button class="btn ghost small" @click="batchMarkRead(true)">📖 Read</button>
        <button class="btn ghost small" @click="batchMarkRead(false)">Unread</button>
        <button class="btn ghost small" @click="clearSelection">✕</button>
      </div>
    </div>

  </div>
</div>`,
  });

  app.mount("#novel-app");
})();
