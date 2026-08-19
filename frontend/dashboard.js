/* Dashboard page — library stats, shelves, recent activity. Loaded on /dashboard. */
(function () {
  const { createApp, ref, onMounted } = Vue;
  createApp({
    setup() {
      const stats = ref(null);
      const error = ref("");
      const SHELF_ICON = { ongoing: "i-book-open", read_later: "i-bookmark", done: "i-check", dropped: "i-trash" };
      const SHELF_LABEL = { ongoing: "Ongoing", read_later: "Read later", done: "Done", dropped: "Dropped" };

      async function load() {
        try {
          const r = await fetch("/api/stats");
          if (r.ok) stats.value = await r.json();
          else error.value = "Failed to load stats";
        } catch (e) { error.value = "Failed to load stats"; }
      }
      function fmtDate(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      }

      onMounted(load);
      return { stats, error, SHELF_ICON, SHELF_LABEL, fmtDate };
    },
    template: `
<div>
  <header class="topbar">
    <div class="container">
      <a class="brand" href="/"><span class="logo-mark"><svg class="ic ic-lg"><use href="#i-cat"/></svg></span> NyaaReader</a>
      <span class="flex-spacer"></span>
      <nav class="topnav">
        <a class="nav-link" href="/"><svg class="ic"><use href="#i-home"/></svg><span class="nav-label">Library</span></a>
        <a class="nav-link active" href="/dashboard"><svg class="ic"><use href="#i-sparkle"/></svg><span class="nav-label">Dashboard</span></a>
        <a class="nav-link" href="/config"><svg class="ic"><use href="#i-settings"/></svg><span class="nav-label">Settings</span></a>
      </nav>
    </div>
  </header>

  <div class="container">
    <div v-if="error" class="banner err">⚠ {{ error }}</div>

    <template v-if="stats">
      <h1 class="page-title"><svg class="ic ic-lg"><use href="#i-sparkle"/></svg> Your reading dashboard</h1>

      <div class="dash-grid">
        <div class="dash-card">
          <div class="dash-num">{{ stats.total_novels }}</div>
          <div class="dash-label">Novels</div>
        </div>
        <div class="dash-card">
          <div class="dash-num">{{ stats.total_chapters }}</div>
          <div class="dash-label">Chapters</div>
        </div>
        <div class="dash-card">
          <div class="dash-num">{{ stats.translated_chapters }} <span class="dash-badge">{{ stats.translation_rate }}</span></div>
          <div class="dash-label">Translated</div>
        </div>
        <div class="dash-card">
          <div class="dash-num">{{ stats.read_chapters }}</div>
          <div class="dash-label">Chapters read</div>
        </div>
        <div class="dash-card">
          <div class="dash-num">{{ stats.bookmarks }}</div>
          <div class="dash-label">Bookmarks</div>
        </div>
        <div class="dash-card">
          <div class="dash-num">{{ stats.diary_entries }}</div>
          <div class="dash-label">Diary entries</div>
        </div>
      </div>

      <h2 class="section-title"><svg class="ic"><use href="#i-layers"/></svg> Shelves</h2>
      <div class="dash-shelves">
        <div v-for="(count, key) in stats.shelves" :key="key" class="shelf-chip">
          <svg class="ic"><use :href="'#' + (SHELF_ICON[key] || 'i-book')"/></svg>
          {{ SHELF_LABEL[key] || key }} <strong class="chip-count">{{ count }}</strong>
        </div>
        <div v-if="Object.keys(stats.shelves).length === 0" class="muted">No novels yet.</div>
      </div>

      <h2 class="section-title"><svg class="ic"><use href="#i-clock"/></svg> Recent activity</h2>
      <div v-if="stats.recent.length" class="dash-recent">
        <div v-for="(r, i) in stats.recent" :key="i" class="dash-row">
          <span class="dash-row-novel" :title="r.novel">{{ r.novel }}</span>
          <span class="dash-row-ch" :title="r.chapter_title">Ch {{ r.chapter_number }} · {{ r.chapter_title }}</span>
          <span class="dash-row-time">{{ fmtDate(r.read_at) }}</span>
        </div>
      </div>
      <div v-else class="muted">No reading activity yet — open a chapter to start.</div>
    </template>
  </div>
</div>`,
  }).mount("#dashboard-app");
})();