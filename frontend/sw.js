/* NyaaReader service worker — network-first with static-app-shell cache.
   Keeps the UI shell available offline; content always hits the network
   (translations are live data, never stale-cached). */
const CACHE = "nyaa-reader-v5";   /* NOTE: manual version bumps are OBSOLETE for CONTENT —
   backend main.py _asset_stamp() appends ?v=<hash> to every asset URL, and the
   fetch handler below is network-first, so any frontend edit is picked up
   automatically. Keep this name stable; bump it only when the SW's own caching
   strategy changes (v4 -> v5 did: entries are now keyed by bare pathname, so
   the old per-stamp entries need purging by activate()). */
const SHELL = [
  "/static/styles.css",
  "/static/favicon.svg",
  "/static/favicon.ico",
  "/static/icons.svg",
  "/static/vendor/vue.global.prod.js",
  "/static/lib/text.js",
  "/static/library.js",
  "/static/novel.js",
  "/static/reader.js",
  "/static/review.js",
  "/static/config.js",
  "/static/dashboard.js",
  "/static/login.js",
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
  if (!SHELL.includes(url.pathname)) {
    return;
  }
  /* Shell assets are requested as /static/x.js?v=<stamp> (see _asset_stamp in
     backend/main.py). Two consequences drive the strategy below:

     1. NETWORK-FIRST. The stamp must always win — serving a cached copy for a
        changed stamp would mask every frontend edit. The cache is purely the
        offline fallback.
     2. Cache under the BARE pathname, and match with ignoreSearch. Keying by
        the full stamped URL meant the install-time precache (bare paths) never
        matched a real request, so the shell was never actually available
        offline, and every edit added one more permanent entry to the cache. */
  const key = new Request(url.origin + url.pathname);
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(key, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(key, { ignoreSearch: true }))
  );
});
