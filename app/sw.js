// Cache-first for the shell, stale-while-revalidate for content.
// Bump SHELL_V whenever index.html changes, or clients keep the old shell.
const SHELL_V = "shell-v2";
const DATA_V  = "data-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest",
               "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL_V).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== SHELL_V && k !== DATA_V).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;

  // Content: serve cache immediately, refresh in the background.
  if (req.url.includes("/data/posts.json")) {
    e.respondWith(caches.open(DATA_V).then(async cache => {
      const hit = await cache.match(req);
      const net = fetch(req).then(res => {
        if (res && res.ok) cache.put(req, res.clone());
        return res;
      }).catch(() => hit);
      return hit || net;
    }));
    return;
  }

  // Shell: cache first, network as fallback.
  if (new URL(req.url).origin === location.origin) {
    e.respondWith(caches.match(req).then(hit => hit || fetch(req)));
  }
});
