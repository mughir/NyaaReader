/* Login page — single-password auth. Loaded on /login. */
(function () {
  const { createApp, ref, onMounted } = Vue;
  createApp({
    setup() {
      const password = ref("");
      const busy = ref(false);
      const error = ref("");
      const checking = ref(true);

      function submit() {
        if (busy.value || !password.value) return;
        busy.value = true;
        error.value = "";
        fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: password.value }),
        })
          .then(async (r) => {
            if (r.ok) {
              window.location.href = "/";
            } else {
              const d = await r.json().catch(() => ({}));
              error.value = d.detail || "Wrong password";
            }
          })
          .catch(() => { error.value = "Login failed"; })
          .finally(() => { busy.value = false; });
      }

      // If no password is set, auth is disabled — skip the login form entirely.
      onMounted(() => {
        fetch("/api/auth/status")
          .then((r) => r.json())
          .then((s) => { if (!s.enabled) window.location.href = "/"; })
          .catch(() => {})
          .finally(() => { checking.value = false; });
      });

      return { password, busy, error, checking, submit };
    },
    template: `
<div class="login-wrap" v-if="!checking">
  <form class="login-card" @submit.prevent="submit">
    <div class="login-logo">
      <svg class="ic" style="width:46px;height:46px;color:#fff"><use href="#i-cat"/></svg>
    </div>
    <h1 class="login-title">NyaaReader</h1>
    <p class="login-sub">Sign in to your library</p>
    <input class="cfg-input" type="password" v-model="password" placeholder="Password"
           autofocus autocomplete="current-password">
    <div v-if="error" class="banner err" style="margin-top:8px;font-size:13px">⚠ {{ error }}</div>
    <button class="btn" type="submit" :disabled="busy">
      {{ busy ? 'Signing in…' : 'Sign in' }}
    </button>
  </form>
</div>`,
  }).mount("#login-app");
})();
