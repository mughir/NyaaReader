/* Dashboard page — translation progress management, active jobs, library stats, shelves. Loaded on /dashboard. */
(function () {
  const { createApp, ref, computed, onMounted, onUnmounted } = Vue;

  createApp({
    setup() {
      const stats = ref(null);
      const tasksData = ref({ active_jobs: [], recent_jobs: [], novels: [], total_active: 0 });
      const error = ref("");
      const notice = ref("");
      const searchQuery = ref("");
      const actionBusy = ref({});
      let pollTimer = null;

      const SHELF_ICON = { ongoing: "i-book-open", read_later: "i-bookmark", done: "i-check", dropped: "i-trash" };
      const SHELF_LABEL = { ongoing: "Ongoing", read_later: "Read later", done: "Done", dropped: "Dropped" };

      const activeJobs = computed(() => (tasksData.value && tasksData.value.active_jobs) || []);
      const recentJobs = computed(() => (tasksData.value && tasksData.value.recent_jobs) || []);
      const novels = computed(() => {
        const list = (tasksData.value && tasksData.value.novels) || [];
        const q = (searchQuery.value || "").trim().toLowerCase();
        if (!q) return list;
        return list.filter(n =>
          (n.title || "").toLowerCase().includes(q) ||
          (n.title_translated || "").toLowerCase().includes(q) ||
          (n.author || "").toLowerCase().includes(q)
        );
      });

      async function loadData() {
        try {
          const [resStats, resTasks] = await Promise.all([
            fetch("/api/stats"),
            fetch("/api/dashboard/tasks"),
          ]);
          if (resStats.ok) stats.value = await resStats.json();
          if (resTasks.ok) tasksData.value = await resTasks.json();
        } catch (e) {
          if (!stats.value && !tasksData.value.novels.length) {
            error.value = "Failed to load dashboard data: " + e.message;
          }
        }
      }

      function schedulePoll() {
        if (pollTimer) clearTimeout(pollTimer);
        const hasActive = activeJobs.value.length > 0;
        const interval = hasActive ? 3000 : 10000;
        pollTimer = setTimeout(async () => {
          await loadData();
          schedulePoll();
        }, interval);
      }

      function setBusy(key, val) {
        actionBusy.value = { ...actionBusy.value, [key]: val };
      }

      function isBusy(key) {
        return !!actionBusy.value[key];
      }

      async function cancelJob(novelId) {
        const key = `cancel-${novelId}`;
        setBusy(key, true);
        try {
          const res = await fetch(`/api/novels/${novelId}/batch-stop`, { method: "POST" });
          if (res.ok) {
            notice.value = "Cancellation requested — task will stop after the current item.";
            await loadData();
          } else {
            error.value = "Failed to cancel task.";
          }
        } catch (e) {
          error.value = "Error cancelling task: " + e.message;
        } finally {
          setBusy(key, false);
          setTimeout(() => { if (notice.value.startsWith("Cancellation")) notice.value = ""; }, 4000);
        }
      }

      async function translateNovelTitle(novelId) {
        const key = `meta-${novelId}`;
        setBusy(key, true);
        try {
          const res = await fetch(`/api/novels/${novelId}/translate-meta`, { method: "POST" });
          const d = await res.json();
          if (d.status === "started") {
            notice.value = `Started translating novel title & synopsis (${d.pending || 2} items).`;
          } else if (d.status === "none") {
            notice.value = "Novel title & synopsis are already translated.";
          } else if (d.status === "already_running") {
            notice.value = "A task is already running for this novel.";
          }
          await loadData();
          schedulePoll();
        } catch (e) {
          error.value = "Failed to start title translation: " + e.message;
        } finally {
          setBusy(key, false);
          setTimeout(() => { notice.value = ""; }, 4000);
        }
      }

      async function translateChapterTitles(novelId) {
        const key = `titles-${novelId}`;
        setBusy(key, true);
        try {
          const res = await fetch(`/api/novels/${novelId}/translate-titles`, { method: "POST" });
          const d = await res.json();
          if (d.status === "started") {
            notice.value = `Started translating ${d.pending || ""} chapter title(s).`;
          } else if (d.status === "none") {
            notice.value = "All chapter titles are already translated.";
          } else if (d.status === "already_running") {
            notice.value = "A task is already running for this novel.";
          }
          await loadData();
          schedulePoll();
        } catch (e) {
          error.value = "Failed to start chapter titles translation: " + e.message;
        } finally {
          setBusy(key, false);
          setTimeout(() => { notice.value = ""; }, 4000);
        }
      }

      async function translateMemory(novelId) {
        const key = `memory-${novelId}`;
        setBusy(key, true);
        try {
          const res = await fetch(`/api/novels/${novelId}/translate-memory`, { method: "POST" });
          const d = await res.json();
          if (d.status === "started") {
            notice.value = `Started translating AI memory & glossary (${d.pending || 1} items).`;
          } else if (d.status === "none") {
            notice.value = "AI memory & glossary are already up to date.";
          } else if (d.status === "already_running") {
            notice.value = "A task is already running for this novel.";
          }
          await loadData();
          schedulePoll();
        } catch (e) {
          error.value = "Failed to start memory translation: " + e.message;
        } finally {
          setBusy(key, false);
          setTimeout(() => { notice.value = ""; }, 4000);
        }
      }

      async function retryFailedChapters(novelId) {
        const key = `retry-${novelId}`;
        setBusy(key, true);
        try {
          const res = await fetch(`/api/novels/${novelId}/retry-failed`, { method: "POST" });
          const d = await res.json();
          if (d.status === "started") {
            notice.value = `Retrying ${d.pending || ""} failed chapter(s).`;
          } else if (d.status === "none") {
            notice.value = "No failed chapters found to retry.";
          } else if (d.status === "already_running") {
            notice.value = "A task is already running for this novel.";
          }
          await loadData();
          schedulePoll();
        } catch (e) {
          error.value = "Failed to retry failed chapters: " + e.message;
        } finally {
          setBusy(key, false);
          setTimeout(() => { notice.value = ""; }, 4000);
        }
      }

      async function translateToEnd(novelId) {
        const key = `toend-${novelId}`;
        setBusy(key, true);
        try {
          const res = await fetch(`/api/novels/${novelId}/translate-to-end`, { method: "POST" });
          const d = await res.json();
          if (d.status === "started") {
            notice.value = `Translating remaining chapters in background (${d.pending || ""} pending).`;
          } else if (d.status === "none") {
            notice.value = "All chapters are already translated.";
          } else if (d.status === "already_running") {
            notice.value = "A task is already running for this novel.";
          }
          await loadData();
          schedulePoll();
        } catch (e) {
          error.value = "Failed to start chapters translation: " + e.message;
        } finally {
          setBusy(key, false);
          setTimeout(() => { notice.value = ""; }, 4000);
        }
      }

      function getKindBadgeClass(kind) {
        if (kind === "meta") return "badge-kind-meta";
        if (kind === "titles") return "badge-kind-titles";
        if (kind === "memory") return "badge-kind-memory";
        if (kind === "retry-failed") return "badge-kind-danger";
        return "badge-kind-default";
      }

      function getKindIcon(kind) {
        if (kind === "meta") return "i-sparkle";
        if (kind === "titles") return "i-bookmark";
        if (kind === "memory") return "i-chip";
        if (kind === "to-end") return "i-book-open";
        if (kind === "retranslate") return "i-layers";
        return "i-clock";
      }

      function fmtDate(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      }

      onMounted(async () => {
        await loadData();
        schedulePoll();
      });

      onUnmounted(() => {
        if (pollTimer) clearTimeout(pollTimer);
      });

      return {
        stats,
        tasksData,
        error,
        notice,
        searchQuery,
        activeJobs,
        recentJobs,
        novels,
        SHELF_ICON,
        SHELF_LABEL,
        isBusy,
        cancelJob,
        translateNovelTitle,
        translateChapterTitles,
        translateMemory,
        retryFailedChapters,
        translateToEnd,
        getKindBadgeClass,
        getKindIcon,
        fmtDate,
      };
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
    <div v-if="notice" class="banner ok">✓ {{ notice }}</div>

    <h1 class="page-title"><svg class="ic ic-lg"><use href="#i-sparkle"/></svg> Translation & Reading Dashboard</h1>

    <!-- 1. Active Translation Tasks Panel -->
    <section class="dash-section">
      <div class="dash-section-header">
        <h2 class="section-title" style="margin:0">
          <svg class="ic"><use href="#i-clock"/></svg> Active Translation Tasks
          <span v-if="activeJobs.length" class="dash-live-badge"><span class="live-dot"></span> {{ activeJobs.length }} running</span>
        </h2>
      </div>

      <div v-if="activeJobs.length" class="dash-tasks-grid">
        <div v-for="job in activeJobs" :key="job.id" class="dash-task-card">
          <div class="dash-task-top">
            <span class="dash-task-kind" :class="getKindBadgeClass(job.kind)">
              <svg class="ic"><use :href="'#' + getKindIcon(job.kind)"/></svg>
              {{ job.kind_label }}
            </span>
            <span class="dash-task-pct num">{{ job.percent }}%</span>
          </div>

          <div class="dash-task-title">
            <a :href="'/novel/' + job.novel_id" :title="job.novel_title">{{ job.novel_title }}</a>
          </div>

          <div class="dash-task-label muted">
            {{ job.current_label || 'Processing…' }} ({{ job.done }} / {{ job.total }})
          </div>

          <div class="dash-task-progress-track">
            <div class="dash-task-progress-fill" :style="{ width: Math.max(3, job.percent) + '%' }"></div>
          </div>

          <div class="dash-task-actions">
            <button
              class="btn small ghost danger"
              @click="cancelJob(job.novel_id)"
              :disabled="job.stop_requested || isBusy('cancel-' + job.novel_id)">
              <svg class="ic"><use href="#i-trash"/></svg>
              {{ job.stop_requested ? 'Stopping…' : 'Cancel task' }}
            </button>
            <a :href="'/novel/' + job.novel_id" class="btn small ghost">
              View novel →
            </a>
          </div>
        </div>
      </div>
      <div v-else class="dash-empty-tasks">
        <svg class="ic ic-lg" style="color:var(--ok)"><use href="#i-check"/></svg>
        <span>No background translation tasks running right now. All queues are idle.</span>
      </div>
    </section>

    <!-- 2. Library Overview Stats -->
    <template v-if="stats">
      <h2 class="section-title"><svg class="ic"><use href="#i-layers"/></svg> Library Overview</h2>
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
    </template>

    <!-- 3. Translation Management by Novel -->
    <section class="dash-section" style="margin-top: 32px">
      <div class="dash-section-header" style="flex-wrap: wrap; gap: 12px; justify-content: space-between; align-items: center">
        <h2 class="section-title" style="margin:0">
          <svg class="ic"><use href="#i-settings"/></svg> Manage Translations & Progress
        </h2>
        <div class="dash-search-box">
          <input
            type="search"
            v-model="searchQuery"
            placeholder="Filter novels…"
            class="dash-search-input"
          />
        </div>
      </div>

      <div v-if="novels.length" class="dash-novel-cards">
        <div v-for="n in novels" :key="n.id" class="dash-novel-manage-card">
          <div class="dash-novel-header">
            <div class="dash-novel-info">
              <h3 class="dash-novel-name">
                <a :href="'/novel/' + n.id">{{ n.title_translated || n.title }}</a>
              </h3>
              <div v-if="n.title_translated && n.title_translated !== n.title" class="dash-novel-orig muted">
                {{ n.title }}
              </div>
            </div>
            <div class="dash-novel-header-actions">
              <a :href="'/novel/' + n.id" class="btn small soft">
                Open Novel
              </a>
            </div>
          </div>

          <!-- Active job on this novel banner -->
          <div v-if="n.active_job" class="dash-novel-active-banner">
            <span class="dash-live-badge"><span class="live-dot"></span> Active</span>
            <strong>{{ n.active_job.kind_label }}:</strong>
            <span>{{ n.active_job.current_label || 'Processing…' }} ({{ n.active_job.done }}/{{ n.active_job.total }})</span>
            <button
              class="btn small ghost danger"
              style="margin-left: auto; padding: 2px 8px; font-size: 11.5px"
              @click="cancelJob(n.id)"
              :disabled="n.active_job.stop_requested || isBusy('cancel-' + n.id)">
              {{ n.active_job.stop_requested ? 'Stopping…' : 'Cancel' }}
            </button>
          </div>

          <!-- Progress and Action Grid -->
          <div class="dash-progress-columns">
            <!-- Col 1: Novel Title & Synopsis -->
            <div class="dash-col-item">
              <div class="dash-col-title">
                <svg class="ic"><use href="#i-sparkle"/></svg> Novel Title & Synopsis
              </div>
              <div class="dash-col-status">
                <span v-if="n.novel_title_status.is_running" class="badge running">⏳ Translating…</span>
                <span v-else-if="n.novel_title_status.is_translated" class="badge ok">✓ Translated</span>
                <span v-else class="badge warn">⚠ Missing</span>
              </div>
              <div class="dash-col-act">
                <button
                  v-if="n.novel_title_status.is_running"
                  class="btn small ghost danger"
                  @click="cancelJob(n.id)"
                  :disabled="isBusy('cancel-' + n.id)">
                  Cancel
                </button>
                <button
                  v-else
                  class="btn small ghost"
                  @click="translateNovelTitle(n.id)"
                  :disabled="isBusy('meta-' + n.id)">
                  {{ n.novel_title_status.is_translated ? 'Re-translate' : 'Translate' }}
                </button>
              </div>
            </div>

            <!-- Col 2: Chapter Titles -->
            <div class="dash-col-item">
              <div class="dash-col-title">
                <svg class="ic"><use href="#i-bookmark"/></svg> Chapter Titles
              </div>
              <div class="dash-col-status">
                <span v-if="n.chapter_title_status.is_running" class="badge running">⏳ Translating…</span>
                <span v-else-if="n.chapter_title_status.pending === 0 && n.chapter_title_status.total > 0" class="badge ok">✓ All {{ n.chapter_title_status.translated }}</span>
                <span v-else class="badge warn">{{ n.chapter_title_status.pending }} missing</span>
              </div>
              <div class="dash-col-act">
                <button
                  v-if="n.chapter_title_status.is_running"
                  class="btn small ghost danger"
                  @click="cancelJob(n.id)"
                  :disabled="isBusy('cancel-' + n.id)">
                  Cancel
                </button>
                <button
                  v-else
                  class="btn small ghost"
                  @click="translateChapterTitles(n.id)"
                  :disabled="isBusy('titles-' + n.id)">
                  {{ n.chapter_title_status.pending > 0 ? 'Translate Titles' : 'Retry Titles' }}
                </button>
              </div>
            </div>

            <!-- Col 3: AI Memory & Glossary -->
            <div class="dash-col-item">
              <div class="dash-col-title">
                <svg class="ic"><use href="#i-chip"/></svg> AI Memory & Glossary
              </div>
              <div class="dash-col-status">
                <span v-if="n.memory_status.is_running" class="badge running">⏳ Translating…</span>
                <span v-else-if="n.memory_status.total_entries > 0 && n.memory_status.pending_entries === 0" class="badge ok">✓ {{ n.memory_status.translated_entries }} terms</span>
                <span v-else-if="n.memory_status.pending_entries > 0" class="badge warn">{{ n.memory_status.pending_entries }} untranslated</span>
                <span v-else-if="n.memory_status.has_memory" class="badge ok">✓ Active</span>
                <span v-else class="badge muted">Empty</span>
              </div>
              <div class="dash-col-act">
                <button
                  v-if="n.memory_status.is_running"
                  class="btn small ghost danger"
                  @click="cancelJob(n.id)"
                  :disabled="isBusy('cancel-' + n.id)">
                  Cancel
                </button>
                <button
                  v-else
                  class="btn small ghost"
                  @click="translateMemory(n.id)"
                  :disabled="isBusy('memory-' + n.id)">
                  {{ n.memory_status.pending_entries > 0 ? 'Translate Memory' : 'Translate/Sync' }}
                </button>
              </div>
            </div>

            <!-- Col 4: Chapter Content -->
            <div class="dash-col-item">
              <div class="dash-col-title">
                <svg class="ic"><use href="#i-book-open"/></svg> Chapter Content
              </div>
              <div class="dash-col-status">
                <span v-if="n.chapter_content_status.is_running" class="badge running">⏳ Translating…</span>
                <span v-else-if="n.chapter_content_status.failed > 0" class="badge danger">⚠ {{ n.chapter_content_status.failed }} failed</span>
                <span v-else-if="n.chapter_content_status.translated === n.chapter_content_status.total && n.chapter_content_status.total > 0" class="badge ok">✓ {{ n.chapter_content_status.translated }} / {{ n.chapter_content_status.total }}</span>
                <span v-else class="badge">{{ n.chapter_content_status.translated }} / {{ n.chapter_content_status.total }}</span>
              </div>
              <div class="dash-col-act">
                <button
                  v-if="n.chapter_content_status.is_running"
                  class="btn small ghost danger"
                  @click="cancelJob(n.id)"
                  :disabled="isBusy('cancel-' + n.id)">
                  Cancel
                </button>
                <div v-else style="display:flex; gap:4px">
                  <button
                    v-if="n.chapter_content_status.failed > 0"
                    class="btn small ghost danger"
                    @click="retryFailedChapters(n.id)"
                    :disabled="isBusy('retry-' + n.id)">
                    Retry Failed
                  </button>
                  <button
                    v-if="n.chapter_content_status.pending > 0"
                    class="btn small ghost"
                    @click="translateToEnd(n.id)"
                    :disabled="isBusy('toend-' + n.id)">
                    Translate All
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div v-else class="muted" style="padding: 16px 0">
        {{ searchQuery ? 'No novels match your filter.' : 'No novels in library yet.' }}
      </div>
    </section>

    <!-- 4. Shelves & Recent Reading Activity -->
    <template v-if="stats">
      <h2 class="section-title"><svg class="ic"><use href="#i-layers"/></svg> Shelves</h2>
      <div class="dash-shelves">
        <a v-for="(count, key) in stats.shelves" :key="key" class="shelf-chip" :href="'/?shelf=' + key" style="text-decoration:none">
          <svg class="ic"><use :href="'#' + (SHELF_ICON[key] || 'i-book')"/></svg>
          {{ SHELF_LABEL[key] || key }} <strong class="chip-count">{{ count }}</strong>
        </a>
        <div v-if="Object.keys(stats.shelves).length === 0" class="muted">No novels yet.</div>
      </div>

      <h2 class="section-title"><svg class="ic"><use href="#i-clock"/></svg> Recent activity</h2>
      <div v-if="stats.recent.length" class="dash-recent">
        <a v-for="(r, i) in stats.recent" :key="i" class="dash-row" :href="'/novel/' + r.novel_id + '/chapter/' + r.chapter_number" :title="'Continue reading ' + r.novel + ' Ch ' + r.chapter_number">
          <span class="dash-row-novel" :title="r.novel">{{ r.novel }}</span>
          <span class="dash-row-ch" :title="r.chapter_title">Ch {{ r.chapter_number }} · {{ r.chapter_title }}</span>
          <span class="dash-row-time">{{ fmtDate(r.read_at) }}</span>
        </a>
      </div>
      <div v-else class="muted">No reading activity yet — open a chapter to start.</div>
    </template>
  </div>
</div>`,
  }).mount("#dashboard-app");
})();