/* Library page — Vue-powered: cover cards, shelf tabs, inline add-novel form.
   Loaded on /. Uses window.__LIBRARY__ = [{id,title,author,cover_url,total_chapters,
   translated_chapters,read_chapters,reading_status,last_read}], window.__SHELF__ */
(function () {
  const DATA = window.__LIBRARY__ || [];
  const { createApp, ref, computed, watch } = Vue;

  const SHELVES = [
    { key: "all", label: "All", icon: "i-book" },
    { key: "ongoing", label: "Ongoing", icon: "i-book-open" },
    { key: "read_later", label: "Read Later", icon: "i-bookmark" },
    { key: "done", label: "Done", icon: "i-check" },
    { key: "dropped", label: "Dropped", icon: "i-trash" },
  ];

  const app = createApp({
    setup() {
      const novels = ref(DATA);
      const shelf = ref(window.__SHELF__ || "all");
      const searchQuery = ref("");
      const sortMode = ref(localStorage.getItem("novelreader.lib_sort") || "recent");
      const viewMode = ref(localStorage.getItem("novelreader.lib_view") || "grid");
      const url = ref("");
      const lang = ref("en");
      const adding = ref(false);
      const error = ref("");
      const notice = ref("");

      watch(sortMode, (v) => localStorage.setItem("novelreader.lib_sort", v));
      watch(viewMode, (v) => localStorage.setItem("novelreader.lib_view", v));

      const filteredNovels = computed(() => {
        let list = novels.value.slice();
        if (shelf.value !== "all") {
          list = list.filter(n => (n.reading_status || "ongoing") === shelf.value);
        }
        const q = searchQuery.value.trim().toLowerCase();
        if (q) {
          list = list.filter(n =>
            (n.title_translated || "").toLowerCase().includes(q) ||
            (n.title || "").toLowerCase().includes(q) ||
            (n.author || "").toLowerCase().includes(q) ||
            (n.source_site || "").toLowerCase().includes(q)
          );
        }

        if (sortMode.value === "progress") {
          list.sort((a, b) => pct(b) - pct(a));
        } else if (sortMode.value === "chapters") {
          list.sort((a, b) => (b.total_chapters || 0) - (a.total_chapters || 0));
        } else if (sortMode.value === "title") {
          list.sort((a, b) => (a.title_translated || a.title || "").localeCompare(b.title_translated || b.title || ""));
        } else {
          // "recent" (default): last_read first, then by id descending
          list.sort((a, b) => {
            const aTime = a.last_read && a.last_read.read_at ? new Date(a.last_read.read_at).getTime() : 0;
            const bTime = b.last_read && b.last_read.read_at ? new Date(b.last_read.read_at).getTime() : 0;
            if (bTime !== aTime) return bTime - aTime;
            return b.id - a.id;
          });
        }
        return list;
      });

      function pct(n) {
        return n.total_chapters > 0 ? Math.round((n.translated_chapters / n.total_chapters) * 100) : 0;
      }
      function readPct(n) {
        return n.total_chapters > 0 ? Math.round(((n.read_chapters || 0) / n.total_chapters) * 100) : 0;
      }
      // Cover fallback: gradient + initial when no image
      function coverStyle(n) {
        if (n.cover_url) {
          const safe = String(n.cover_url).replace(/[\s'"();]/g, "");
          return { backgroundImage: `url(${safe})`, backgroundSize: "cover", backgroundPosition: "center" };
        }
        const hue = (n.id * 47) % 360;
        return {
          background: `linear-gradient(150deg, hsl(${hue},55%,42%), hsl(${(hue + 45) % 360},62%,26%) 65%, hsl(${(hue + 90) % 360},65%,18%))`,
        };
      }
      function coverText(n) {
        if (n.cover_url) return "";
        const t = (n.title_translated || n.title || "?").trim();
        return t ? t[0].toUpperCase() : "?";
      }
      function shelfLabel(key) {
        const s = SHELVES.find(x => x.key === key);
        return s ? s.label : key;
      }
      function shelfIcon(key) {
        const s = SHELVES.find(x => x.key === key);
        return s ? s.icon : "i-book";
      }
      function goShelf(key) {
        shelf.value = key;
        const newUrl = key === "all" ? "/" : "/?shelf=" + key;
        window.history.pushState({}, "", newUrl);
      }

      async function addNovel() {
        if (!url.value || adding.value) return;
        adding.value = true; error.value = ""; notice.value = "";
        try {
          const res = await fetch("/api/novels", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ source_url: url.value.trim(), target_language: lang.value, auto_translate: true }),
          });
          if (!res.ok) {
            let msg = "HTTP " + res.status;
            try { const d = await res.json(); msg = d.detail || msg; } catch (e) {}
            throw new Error(msg);
          }
          const novel = await res.json();
          notice.value = `Added "${novel.title_translated || novel.title}" — fetching first chapters in background.`;
          url.value = "";
          setTimeout(() => { window.location.href = "/novel/" + novel.id; }, 900);
        } catch (e) {
          error.value = e.message;
        } finally {
          adding.value = false;
        }
      }

      function openNovel(id) { window.location.href = "/novel/" + id; }
      function openChapter(id, n) { window.location.href = `/novel/${id}/chapter/${n}`; }

      return { novels, filteredNovels, shelf, searchQuery, sortMode, viewMode, SHELVES, url, lang, adding, error, notice,
               pct, readPct, coverStyle, coverText, shelfLabel, shelfIcon, goShelf,
               addNovel, openNovel, openChapter };
    },
    template: `
<div>
  <header class="topbar">
    <div class="container">
      <a class="brand" href="/"><span class="logo-mark"><svg class="ic ic-lg"><use href="#i-cat"/></svg></span> NyaaReader</a>
      <span class="flex-spacer"></span>
      <nav class="topnav">
        <a class="nav-link active" href="/"><svg class="ic"><use href="#i-home"/></svg><span class="nav-label">Library</span></a>
        <a class="nav-link" href="/dashboard"><svg class="ic"><use href="#i-sparkle"/></svg><span class="nav-label">Dashboard</span></a>
        <a class="nav-link" href="/config"><svg class="ic"><use href="#i-settings"/></svg><span class="nav-label">Settings</span></a>
      </nav>
    </div>
  </header>

  <div class="container">
    <form class="add-form" @submit.prevent="addNovel">
      <input type="url" v-model="url" placeholder="Paste novel URL — syosetu, jjwxc, qidian…" required>
      <select v-model="lang">
        <option value="en">→ English</option>
        <option value="id">→ Indonesian</option>
        <option value="ja">→ Japanese</option>
        <option value="ko">→ Korean</option>
      </select>
      <button class="btn" type="submit" :disabled="adding">{{ adding ? 'Adding…' : '+ Add' }}</button>
      <div class="hint">AI translation is on by default (Gemini with DeepSeek fallback). First 5 chapters auto-fetch in the background.</div>
    </form>

    <div v-if="error" class="banner err" style="margin-top:12px">⚠ {{ error }}</div>
    <div v-if="notice" class="banner" style="margin-top:12px">✓ {{ notice }}</div>

    <!-- shelf tabs & collection controls -->
    <div class="library-toolbar">
      <div class="shelf-tabs">
        <button v-for="s in SHELVES" :key="s.key" class="btn ghost small"
                :class="{on: shelf === s.key}" @click="goShelf(s.key)">
          <svg class="ic"><use :href="'#' + s.icon"/></svg> {{ s.label }}
        </button>
      </div>
      <div class="lib-filter-bar">
        <div class="search-wrap">
          <input type="search" v-model="searchQuery" placeholder="Search library…" class="lib-search">
        </div>
        <select v-model="sortMode" class="lib-sort" title="Sort library">
          <option value="recent">🕒 Recently Read</option>
          <option value="progress">📈 % Translated</option>
          <option value="chapters">📚 Total Chapters</option>
          <option value="title">🔤 Title (A-Z)</option>
        </select>
        <div class="view-toggle">
          <button class="icon-btn-sm" :class="{active: viewMode === 'grid'}" @click="viewMode = 'grid'" title="Grid view">⊞</button>
          <button class="icon-btn-sm" :class="{active: viewMode === 'list'}" @click="viewMode = 'list'" title="List view">≡</button>
        </div>
      </div>
    </div>

    <!-- Grid view -->
    <div v-if="filteredNovels.length && viewMode === 'grid'" class="library-grid">
      <a v-for="n in filteredNovels" :key="n.id" class="novel-card" :href="'/novel/' + n.id">
        <div class="cover" :style="coverStyle(n)">
          <span v-if="!n.cover_url" class="cover-initial">{{ coverText(n) }}</span>
          <span class="cover-badge" :class="'st-' + (n.reading_status||'ongoing')">{{ shelfLabel(n.reading_status || 'ongoing') }}</span>
        </div>
        <div class="card-body">
          <h3>{{ n.title_translated || n.title }}</h3>
          <div class="meta">{{ n.author || 'Unknown' }} · {{ n.total_chapters }} ch</div>
          <div class="meta" v-if="n.source_site">{{ n.source_site }}</div>
          <div class="meta" v-if="n.translated_chapters > 0">✓ {{ n.translated_chapters }}/{{ n.total_chapters }} translated</div>
          <div class="progress-mini"><div :style="{width: pct(n) + '%'}"></div></div>
          <div class="meta" v-if="n.read_chapters > 0">📖 read {{ n.read_chapters }}/{{ n.total_chapters }} ({{ readPct(n) }}%)</div>
          <div class="card-actions" v-if="n.last_read">
            <span class="btn small accent" @click.prevent="openChapter(n.id, n.last_read.chapter_number)" title="Continue reading">Continue · Ch {{ n.last_read.chapter_number }}</span>
          </div>
        </div>
      </a>
    </div>

    <!-- Compact List view -->
    <div v-else-if="filteredNovels.length && viewMode === 'list'" class="library-list">
      <a v-for="n in filteredNovels" :key="n.id" class="list-card" :href="'/novel/' + n.id">
        <div class="list-cover" :style="coverStyle(n)">
          <span v-if="!n.cover_url" class="list-cover-initial">{{ coverText(n) }}</span>
        </div>
        <div class="list-main">
          <div class="list-header">
            <h4>{{ n.title_translated || n.title }}</h4>
            <span class="cover-badge" :class="'st-' + (n.reading_status||'ongoing')">{{ shelfLabel(n.reading_status || 'ongoing') }}</span>
          </div>
          <div class="list-meta">{{ n.author || 'Unknown' }} · {{ n.total_chapters }} chapters · {{ n.source_site }}</div>
          <div class="list-progress-bar">
            <div class="list-progress-fill" :style="{width: pct(n) + '%'}"></div>
          </div>
          <div class="list-stats">
            <span>✓ {{ n.translated_chapters }}/{{ n.total_chapters }} translated ({{ pct(n) }}%)</span>
            <span v-if="n.read_chapters > 0">📖 read {{ n.read_chapters }} ch ({{ readPct(n) }}%)</span>
          </div>
        </div>
        <div class="list-actions">
          <button v-if="n.last_read" class="btn small accent" @click.prevent="openChapter(n.id, n.last_read.chapter_number)">Continue Ch {{ n.last_read.chapter_number }}</button>
          <span v-else class="btn small ghost">Open</span>
        </div>
      </a>
    </div>

    <div v-else class="empty-state">
      <div class="empty-emoji">📚</div>
      <div class="empty-title">{{ searchQuery ? 'No novels match your search' : (shelf === 'all' ? 'Your library is empty' : 'Nothing on this shelf yet') }}</div>
      <div class="empty-sub" v-if="searchQuery">Try a different title, author, or keyword.</div>
      <div class="empty-sub" v-else-if="shelf === 'all'">Paste a novel URL above to add your first book — it will be scraped and AI-translated automatically.</div>
      <div class="empty-sub" v-else>Move novels here from their page (📖 Ongoing / 🔖 Read Later / ✅ Done / 🗑 Dropped), or switch to <button class="btn ghost small" @click="goShelf('all')">All</button>.</div>
    </div>
  </div>
</div>`,
  });

  app.mount("#library-app");
})();
