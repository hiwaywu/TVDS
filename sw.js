// tvapp service worker - caches only the shell so the app opens instantly.
// Channel data and video always go to the network.
const SHELL = ['/', '/index.html', '/hls.min.js', '/manifest.json', '/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open('tvapp-v1').then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== 'tvapp-v1').map(k => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;          // streams handled by the proxy
  if (url.pathname.startsWith('/proxy') || url.pathname.startsWith('/api/')) return;
  e.respondWith(
    caches.match(e.request).then(hit => hit || fetch(e.request).then(res => {
      const copy = res.clone();
      caches.open('tvapp-v1').then(c => c.put(e.request, copy)).catch(() => {});
      return res;
    }).catch(() => hit))
  );
});
