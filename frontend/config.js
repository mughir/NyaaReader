/* Settings page — API keys, fallback models, backups. Loaded on /config. */
(function () {
  const { createApp, ref, onMounted } = Vue;

  const app = createApp({
    setup() {
      const cfg = ref({
        fallback_api_key: "", fallback_api_key_set: false,
        fallback_base_url: "", fallback_model: "", fallback_model_2: "",
        backup_enabled: true, backup_interval_hours: 24, backup_keep: 14,
        auth_password: "", auth_password_set: false,
      });
      const removeAuth = ref(false);
      const backups = ref([]);
      const saving = ref(false);
      const savedFlash = ref(false);
      const backingUp = ref(false);
      const msg = ref("");
      const err = ref("");

      async function load() {
        try {
          const r = await fetch("/api/config");
          if (r.ok) {
            const d = await r.json();
            // Keep masked key fragments OUT of the editable fields — they are
            // display-only. If they leak into the input, a save sends the 4-char
            // fragment back as the "real key" and clobbers it (the 401 bug).
            cfg.value = { ...cfg.value, ...d };
            cfg.value.gemini_api_key = "";
            cfg.value.fallback_api_key = "";
          }
        } catch (e) {}
        loadBackups();
      }
      async function loadBackups() {
        try {
          const r = await fetch("/api/backups");
          if (r.ok) backups.value = await r.json();
        } catch (e) {}
      }
      async function deleteBackup(name) {
        if (!confirm(`Delete backup ${name}?`)) return;
        try {
          const r = await fetch(`/api/backups/${encodeURIComponent(name)}`, { method: "DELETE" });
          if (!r.ok) throw new Error("delete failed");
          msg.value = `Deleted backup: ${name}`;
          await loadBackups();
        } catch (e) { err.value = e.message; }
      }
      async function restoreBackup(event) {
        const file = (event.target.files || [])[0];
        if (!file) return;
        if (!file.name.endsWith(".db")) { err.value = "Please pick a .db backup file"; return; }
        if (!confirm(`Restore library from "${file.name}"? This REPLACES your current data.`)) { event.target.value = ""; return; }
        const fd = new FormData();
        fd.append("file", file);
        const btn = document.getElementById("restoreBtn");
        if (btn) btn.disabled = true;
        try {
          const r = await fetch("/api/backups/restore", { method: "POST", body: fd });
          const d = await r.json();
          if (!r.ok) { err.value = d.detail || "restore failed"; }
          else {
            msg.value = `✓ Restored (${(d.size/1024).toFixed(0)} KB). Reloading…`;
            setTimeout(() => { window.location.reload(); }, 1200);
          }
        } catch (e) { err.value = e.message; }
        finally {
          if (btn) btn.disabled = false;
          event.target.value = "";
        }
      }
      async function save() {
        saving.value = true; msg.value = ""; err.value = "";
        // ---- Health-check BEFORE saving ----
        // Step 1: key + base URL reach the relay. Step 2: model names exist.
        // If key/base fail, abort (nothing saved). If a model is wrong, clear it
        // and save the rest.
        try {
          const hc = await fetch("/api/config/health-check", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              api_key: cfg.value.fallback_api_key || undefined,
              base_url: cfg.value.fallback_base_url,
              model: cfg.value.fallback_model,
              model_2: cfg.value.fallback_model_2,
            }),
          }).then(r => r.json()).catch(() => null);
          if (hc && hc.key_ok === false) {
            err.value = hc.message || "Relay health check failed";
            saving.value = false;
            return;
          }
          if (hc && hc.models) {
            if (!hc.models.model && cfg.value.fallback_model) {
              err.value = `Model "${cfg.value.fallback_model}" was not found on the relay — cleared. Re-check the name.`;
              cfg.value.fallback_model = "";
              cfg.fallback_model = "";
            } else if (cfg.value.fallback_model_2 && hc.models.model_2 === false) {
              err.value = `Model 2 "${cfg.value.fallback_model_2}" was not found on the relay — cleared.`;
              cfg.value.fallback_model_2 = "";
              cfg.fallback_model_2 = "";
            }
          }
        } catch (e) { /* health-check non-fatal on network error */ }

        const body = {
          fallback_base_url: cfg.value.fallback_base_url,
          fallback_model: cfg.value.fallback_model,
          fallback_model_2: cfg.value.fallback_model_2,
          backup_enabled: cfg.value.backup_enabled,
          backup_interval_hours: +cfg.value.backup_interval_hours || 24,
          backup_keep: +cfg.value.backup_keep || 14,
        };
        // Keys: send only when the user typed a new value
        if (cfg.value.fallback_api_key) body.fallback_api_key = cfg.value.fallback_api_key;
        // Auth: only update when the user typed something (blank = keep current)
        if (cfg.value.auth_password) body.auth_password = cfg.value.auth_password;
        // Explicit removal: user clicked "Remove password" -> send __clear
        if (removeAuth.value) body.auth_password__clear = true;
        try {
          const r = await fetch("/api/config", {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          if (!r.ok) throw new Error("save failed");
          msg.value = "Settings saved — new keys/models apply immediately.";
          savedFlash.value = true;
          setTimeout(() => { savedFlash.value = false; }, 1500);
          await load();
        } catch (e) { err.value = e.message; }
        finally { saving.value = false; }
      }
      async function backupNow() {
        backingUp.value = true; msg.value = ""; err.value = "";
        try {
          const r = await fetch("/api/backup", { method: "POST" });
          const d = await r.json();
          if (!r.ok || d.status !== "ok") throw new Error(d.message || "backup failed");
          msg.value = `Backup created: ${d.file} (${(d.size / 1024).toFixed(0)} KB)`;
          await loadBackups();
        } catch (e) { err.value = e.message; }
        finally { backingUp.value = false; }
      }
      function fmtDate(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        return d.toLocaleString();
      }

      async function logout() {
        try { await fetch("/api/auth/logout", { method: "POST" }); } catch (e) {}
        window.location.href = "/login";
      }

      onMounted(load);

      return { cfg, removeAuth, backups, saving, savedFlash, backingUp, msg, err,
               save, backupNow, loadBackups, deleteBackup, restoreBackup, fmtDate, logout };
    },
    template: `
<div>
  <header class="topbar">
    <div class="container">
      <a class="brand" href="/"><span class="logo-mark"><svg class="ic ic-lg"><use href="#i-cat"/></svg></span> NyaaReader</a>
      <nav class="topnav">
        <a class="nav-link" href="/"><svg class="ic"><use href="#i-home"/></svg><span class="nav-label">Library</span></a>
        <a class="nav-link" href="/dashboard"><svg class="ic"><use href="#i-sparkle"/></svg><span class="nav-label">Dashboard</span></a>
        <a class="nav-link active" href="/config"><svg class="ic"><use href="#i-settings"/></svg><span class="nav-label">Settings</span></a>
      </nav>
      <span class="flex-spacer"></span>
      <button v-if="cfg.auth_password_set" class="icon-btn" @click="logout" title="Sign out"><svg class="ic ic-lg"><use href="#i-logout"/></svg></button>
    </div>
  </header>

  <div class="container container-narrow">
    <div v-if="msg" class="banner">✓ {{ msg }}</div>
    <div v-if="err" class="banner err">⚠ {{ err }}</div>

    <h1 class="page-title"><svg class="ic ic-lg"><use href="#i-settings"/></svg> Settings</h1>

    <h2 class="section-title"><svg class="ic"><use href="#i-key"/></svg> API Key</h2>
    <div class="cfg-card">
      <label class="cfg-label">
        <span class="cfg-label-text">Relay API key</span>
        <span class="status-badge" :class="cfg.fallback_api_key_set ? 'st-set' : 'st-unset'">{{ cfg.fallback_api_key_set ? 'Set ✓' : 'Not set' }}</span>
      </label>
      <p class="cfg-hint">Single key for the whole chain — deepseek-v4-flash &amp; gpt-5.6-luna.</p>
      <input type="password" v-model="cfg.fallback_api_key" placeholder="Leave empty to keep current key"
             class="cfg-input long" autocomplete="off">
      <label class="cfg-label"><span class="cfg-label-text">Relay base URL</span>
        <span class="status-badge" :class="cfg.fallback_base_url ? 'st-set' : 'st-unset'">{{ cfg.fallback_base_url ? 'Set ✓' : 'Not set' }}</span>
      </label>
      <input type="text" v-model="cfg.fallback_base_url" class="cfg-input long" placeholder="https://opencode.ai/zen/go/v1">
      <label class="cfg-label"><span class="cfg-label-text">Model (tier 1 — best value)</span>
        <span class="status-badge" :class="cfg.fallback_model ? 'st-set' : 'st-unset'">{{ cfg.fallback_model ? 'Set ✓' : 'Required' }}</span>
      </label>
      <input type="text" v-model="cfg.fallback_model" class="cfg-input short" placeholder="deepseek-v4-flash">
      <label class="cfg-label"><span class="cfg-label-text">Model 2 (tier 2 — optional)</span>
        <span class="status-badge" :class="cfg.fallback_model_2 ? 'st-set' : 'st-unset'">{{ cfg.fallback_model_2 ? 'Set ✓' : 'Optional' }}</span>
      </label>
      <input type="text" v-model="cfg.fallback_model_2" class="cfg-input short" placeholder="gpt-5.6-luna">
      <p class="cfg-hint">On save, Nyaa pings the relay with the key + base URL, then checks the model names. If the key/base fail, nothing is saved. If only a model name is wrong, just that field is cleared.</p>
    </div>

    <h2 class="section-title"><svg class="ic"><use href="#i-lock"/></svg> Access</h2>
    <div class="cfg-card">
      <label class="cfg-label">
        <span class="cfg-label-text">Password</span>
        <span class="status-badge" :class="cfg.auth_password_set ? 'st-set' : 'st-unset'">{{ cfg.auth_password_set ? 'Set ✓' : 'Not set — login not required' }}</span>
      </label>
      <input type="password" v-model="cfg.auth_password" class="cfg-input short" placeholder="Leave empty to keep current"
             autocomplete="new-password">
      <label v-if="cfg.auth_password_set" class="cfg-check">
        <input type="checkbox" v-model="removeAuth"> Remove password (disable login requirement)
      </label>
      <p class="cfg-hint">Set a password to protect the whole app (recommended before exposing it on a network/VPS).</p>
    </div>

    <h2 class="section-title"><svg class="ic"><use href="#i-layers"/></svg> Backups</h2>
    <div class="cfg-card">
      <label class="cfg-check" style="font-weight:600">
        <input type="checkbox" v-model="cfg.backup_enabled"> Automatic backups
      </label>
      <div class="cfg-pair">
        <label class="cfg-label" style="flex:1"><span class="cfg-label-text">Interval (hours)</span>
          <input type="number" v-model="cfg.backup_interval_hours" min="1" class="cfg-input short">
        </label>
        <label class="cfg-label" style="flex:1"><span class="cfg-label-text">Keep last N</span>
          <input type="number" v-model="cfg.backup_keep" min="1" class="cfg-input short">
        </label>
      </div>
      <button class="btn" @click="backupNow" :disabled="backingUp" style="margin:6px 0 12px">
        {{ backingUp ? 'Backing up…' : '💾 Backup now' }}
      </button>
      <div style="margin-bottom:12px">
        <button id="restoreBtn" class="btn ghost" @click="$refs.restoreInput.click()">📥 Restore from backup…</button>
        <input ref="restoreInput" type="file" accept=".db" style="display:none" @change="restoreBackup">
        <span class="muted" style="font-size:11px;margin-left:8px">choose a .db to replace the library</span>
      </div>
      <div v-if="backups.length" class="bm-item">
        <div v-for="b in backups" :key="b.name" class="backup-row">
          <span style="font-size:12px;flex:1;overflow:hidden;text-overflow:ellipsis">{{ b.name }} · {{ (b.size/1024).toFixed(0) }} KB · {{ fmtDate(b.date) }}</span>
          <a :href="'/api/backups/' + b.name + '/download'" class="btn ghost tiny" title="Download">⬇</a>
          <button class="btn ghost tiny danger" @click="deleteBackup(b.name)" title="Delete backup">🗑</button>
        </div>
      </div>
      <div v-else class="muted" style="font-size:12px">No backups yet — click Backup now.</div>
    </div>

    <button class="btn save-btn" :class="{saved: savedFlash}" @click="save" :disabled="saving" style="margin-top:6px">
      {{ saving ? 'Saving…' : (savedFlash ? 'Saved ✓' : '💾 Save settings') }}
    </button>
  </div>
</div>`,
  });

  app.mount("#config-app");
})();