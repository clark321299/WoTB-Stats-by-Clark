const CACHE_NAME = "clstats-v8";
const ASSETS = [
    "./index.html",
    "./manifest.json",
    "./icons/favicon.png",
    "./icons/icon-192.png",
    "./icons/icon-512.png",
    "https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"
];

self.addEventListener("install", e => {
    e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(ASSETS)));
    self.skipWaiting();
});

self.addEventListener("activate", e => {
    e.waitUntil(
        caches.keys().then(keys => Promise.all(
            keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
        ))
    );
    self.clients.claim();
});

self.addEventListener("fetch", e => {
    const url = new URL(e.request.url);

    if (url.hostname.includes("wotblitz")) return;

    // Локальний API — завжди з мережі (без кешу SW).
    if (url.pathname.startsWith("/api/")) {
        e.respondWith(fetch(e.request));
        return;
    }

    // Головна сторінка — мережа першою, інакше після оновлення коду кнопки «живуть» у старому JS.
    const isMainDoc =
        e.request.mode === "navigate" ||
        url.pathname === "/" ||
        url.pathname.endsWith("/index.html") ||
        url.pathname.endsWith("index.html");
    if (isMainDoc) {
        e.respondWith(
            fetch(e.request)
                .then(response => {
                    if (response.ok) {
                        const clone = response.clone();
                        caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
                    }
                    return response;
                })
                .catch(() => caches.match(e.request))
        );
        return;
    }

    e.respondWith(
        caches.match(e.request).then(cached => {
            const fetchPromise = fetch(e.request).then(response => {
                if (response.ok) {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
                }
                return response;
            }).catch(() => cached);

            return cached || fetchPromise;
        })
    );
});
