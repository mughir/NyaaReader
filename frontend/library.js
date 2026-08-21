/* Library page — Vue-powered: cover cards, shelf tabs, inline add-novel form.
   Loaded on /. Uses window.__LIBRARY__ = [{id,title,author,cover_url,total_chapters,
   translated_chapters,read_chapters,reading_status,last_read}], window.__SHELF__ */
(function () {
  const DATA = window.__LIBRARY__ || [];
  const { createApp, ref } = Vue;

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
      const url = ref("");
      const lang = ref("en");
      const adding = ref(false);
      const error = ref("");
      const notice = ref("");

      const shown = ref(novels.value); // server already filtered by shelf

      function pct(n) {
        return n.total_chapters > 0 ? Math.round((n.translated_chapters / n.total_chapters) * 100) : 0;
      }
      function readPct(n) {
        return n.total_chapters > 0 ? Math.round(((n.read_chapters || 0) / n.total_chapters) * 100) : 0;
      }
      // Cover fallback: gradient + initial when no image
      function coverStyle(n) {
        if (n.cover_url) {
        // Sanitize: a crafted cover_url with ')' or ';' could inject CSS.
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
        window.location.href = key === "all" ? "/" : "/?shelf=" + key;
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
          // Don't re-fetch /api/novels here — its rows lack translated_chapters/
          // read_chapters/last_read, so replacing the cards wipes progress bars
          // and drops the active shelf filter. We redirect in ~1s anyway.
          setTimeout(() => { window.location.href = "/novel/" + novel.id; }, 900);
        } catch (e) {
          error.value = e.message;
        } finally {
          adding.value = false;
        }
      }

      function openNovel(id) { window.location.href = "/novel/" + id; }
      function openChapter(id, n) { window.location.href = `/novel/${id}/chapter/${n}`; }

      return { novels, shown, shelf, SHELVES, url, lang, adding, error, notice,
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
        <a class="nav-link active" href="/"> <svg class="ic"><use href="#i-home"/></svg><span class="nav-label">Library ({{ novels.length }})</span></a>
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

    <!-- shelf tabs -->
    <div class="shelf-tabs">
      <button v-for="s in SHELVES" :key="s.key" class="btn ghost small"
              :class="{on: shelf === s.key}" @click="goShelf(s.key)">
        <svg class="ic"><use :href="'#' + s.icon"/></svg> {{ s.label }}
      </button>
    </div>

    <div v-if="shown.length" class="library-grid">
      <a v-for="n in shown" :key="n.id" class="novel-card" :href="'/novel/' + n.id">
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
          <div class="card-actions">
            <span class="btn small" @click.prevent="openNovel(n.id)"><svg class="ic"><use href="#i-book-open"/></svg> Open</span>
            <span v-if="n.last_read" class="btn small accent" @click.prevent="openChapter(n.id, n.last_read.chapter_number)" title="Continue reading">Continue · Ch {{ n.last_read.chapter_number }}</span>
          </div>
        </div>
      </a>
    </div>
    <div v-else class="empty-state">
      <div class="empty-emoji">📚</div>
      <div class="empty-title">{{ shelf === 'all' ? 'Your library is empty' : 'Nothing on this shelf yet' }}</div>
      <div class="empty-sub" v-if="shelf === 'all'">Paste a novel URL above to add your first book — it will be scraped and AI-translated automatically.</div>
      <div class="empty-sub" v-else>Move novels here from their page (📖 Ongoing / 🔖 Read Later / ✅ Done / 🗑 Dropped), or switch to <button class="btn ghost small" @click="goShelf('all')">All</button>.</div>
    </div>
  </div>
</div>`,
  });

  app.mount("#library-app");
})();
