/* NyaaReader service worker — network-first with static-app-shell cache.
   Keeps the UI shell available offline; content always hits the network
   (translations are live data, never stale-cached). */
const CACHE = "nyaa-reader-v2";   /* bumped 2026-08-17 — UI polish releases; bump on every frontend deploy */
const SHELL = [
  "/static/styles.css",
  "/static/favicon.svg",
  "/static/favicon.ico",
  "/static/icons.svg",
  "/static/vendor/vue.global.prod.js",
  "/static/library.js",
  "/static/novel.js",
  "/static/reader.js",
  "/static/review.js",
  "/static/config.js",
  "/static/manifest.json",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Never cache API calls or chapter pages — always fresh
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/novel/")) {
    return;
  }
  // App shell: cache-first for static assets
  if (SHELL.includes(url.pathname)) {
    e.respondWith(
      caches.match(e.request).then(
        (hit) => hit || fetch(e.request).then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
          return res;
        })
      )
    );
  }
});
