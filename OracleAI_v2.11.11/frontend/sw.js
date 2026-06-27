/* OracleAI service worker — deliberately minimal.
 *
 * The UI is served LIVE by the FastAPI backend (and intentionally no-cached
 * server-side), so this SW does NOT cache the app shell — caching it would
 * resurrect the stale-frontend bug. Its only jobs:
 *   1. Make the app installable (a SW with a fetch handler is required).
 *   2. When the backend/Ollama isn't running, show a friendly offline page
 *      instead of the browser's raw connection error.
 */
const CACHE = 'oracleai-shell-v2';
const OFFLINE_URL = '/static/offline.html';

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((c) => c.add(OFFLINE_URL))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  // Only intercept top-level navigations: network-first, with the friendly
  // offline page as the fallback when the backend can't be reached.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match(OFFLINE_URL))
    );
  }
  // All other requests fall through to the network (no shell caching).
});
