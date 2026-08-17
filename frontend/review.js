/* Review page — "Story so far" (AI memory) + personal diary. Loaded on /novel/{id}/review. */
(function () {
  const { createApp, ref, onMounted } = Vue;

  const app = createApp({
    setup() {
      const DATA = window.__REVIEW__ || { novel: {}, memory: {}, diary: [] };
      const memory = ref(DATA.memory || {});
      const diary = ref(DATA.diary || []);
      const glossary = ref((DATA.memory && DATA.memory.glossary_entries) || []);
      const novel = DATA.novel || {};

      // Characters: one per line ("Name (CN) - description")
      const characterLines = ref((memory.value.characters || "").split("\n").map(l => l.trim()).filter(Boolean));
      const memoryNotes = ref((memory.value.memory || "").split("\n").map(l => l.trim()).filter(Boolean));

      const totalDiary = ref(diary.value.length);

      function fmtChapter(num) { return `Ch ${num}`; }

      return { memory, diary, glossary, characterLines, memoryNotes, novel, totalDiary, fmtChapter };
    },
    template: `
<div>
  <header class="topbar">
    <a class="brand" href="/">📚 NyaaReader</a>
    <a class="btn ghost small" :href="'/novel/' + novel.id">← Chapters</a>
    <span class="crumb">Story so far · {{ novel.title_translated || novel.title }}</span>
  </header>

  <div class="container" style="max-width:760px">
    <h2 style="margin:18px 0 4px">📖 Story so far</h2>
    <p class="muted" style="margin-top:0">An auto-maintained review of your copy of the novel — the AI updates it as chapters are translated.</p>

    <!-- Plot summary -->
    <div v-if="memory.plot" class="rv-card">
      <h3>Plot</h3>
      <p class="rv-text">{{ memory.plot }}</p>
    </div>

    <!-- Current arc -->
    <div v-if="memory.arc_plot" class="rv-card">
      <h3>📍 Current arc</h3>
      <p class="rv-text">{{ memory.arc_plot }}</p>
    </div>

    <!-- Recent chapter -->
    <div v-if="memory.chapter_plot" class="rv-card">
      <h3>🕐 Last translated chapter</h3>
      <p class="rv-text">{{ memory.chapter_plot }}</p>
    </div>

    <!-- Characters -->
    <div v-if="characterLines.length" class="rv-card">
      <h3>👥 Characters</h3>
      <div v-for="(c, i) in characterLines" :key="i" class="rv-char">{{ c }}</div>
    </div>

    <!-- Glossary terms -->
    <div v-if="glossary.length" class="rv-card">
      <h3>🔒 Glossary</h3>
      <div v-for="g in glossary" :key="g.source" class="rv-char">
        <strong>{{ g.translated || g.source }}</strong>
        <span v-if="g.source && g.source !== g.translated" class="muted"> ← {{ g.source }}</span>
        <span v-if="g.locked" title="Locked"> 🔒</span>
      </div>
    </div>

    <!-- Running memory notes -->
    <div v-if="memoryNotes.length" class="rv-card">
      <h3>🧠 Notes</h3>
      <div v-for="(n, i) in memoryNotes" :key="i" class="rv-char">{{ n }}</div>
    </div>

    <div v-if="!memory.plot && !characterLines.length && !memory.arc_plot" class="rv-card muted">
      No AI memory yet — translate a few chapters and the review appears here automatically.
    </div>

    <!-- Diary -->
    <h2 style="margin:28px 0 4px">✍️ My reading diary <span class="muted" style="font-size:13px">({{ totalDiary }} entries)</span></h2>
    <p class="muted" style="margin-top:0">Your personal reflections — add them from the <em>My thoughts</em> box under any chapter.</p>
    <div v-if="diary.length">
      <div v-for="e in diary" :key="e.chapter_number" class="rv-card rv-diary">
        <a class="rv-ch" :href="'/novel/' + novel.id + '/chapter/' + e.chapter_number">Ch {{ e.chapter_number }}</a>
        <p class="rv-text" style="margin-top:4px">{{ e.content }}</p>
      </div>
    </div>
    <div v-else class="empty-state">
      <div class="empty-emoji">✍️</div>
      <div class="empty-title">No diary entries yet</div>
      <div class="empty-sub">Open any chapter and write your thoughts in the <em>My thoughts</em> box at the bottom — they'll collect here as your personal reading diary.</div>
    </div>
  </div>
</div>`,
  });

  app.mount("#review-app");
})();
