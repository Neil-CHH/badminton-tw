/* 羽球賽事資料庫 Service Worker
   - App shell:stale-while-revalidate(先回快取秒開,背景抓新版,下次打開即更新;
     不再依賴手動升版本號)
   - data/*.json:network-first,3.5 秒沒回應或離線即退回快取 */
const VERSION = "v5";
const SHELL_CACHE = `shell-${VERSION}`;
const DATA_CACHE = `data-${VERSION}`;
const SHELL = [
  "./", "./index.html", "./search.html", "./unit.html", "./player.html",
  "./tournament.html", "./about.html",
  "./css/style.css", "./js/common.js", "./manifest.json",
  "./icons/icon-192.png", "./icons/icon-512.png",
];
const DATA_TIMEOUT_MS = 3500;

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL_CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== SHELL_CACHE && k !== DATA_CACHE).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

async function dataFetch(req) {
  const cache = await caches.open(DATA_CACHE);
  const net = fetch(req).then(res => {
    if (res.ok) cache.put(req, res.clone());
    return res;
  }).catch(() => null);
  // 網路 3.5 秒內沒結果就先用快取(訊號差時不用乾等)
  const first = await Promise.race([
    net,
    new Promise(r => setTimeout(r, DATA_TIMEOUT_MS, "timeout")),
  ]);
  if (first && first !== "timeout") return first;
  const hit = await cache.match(req);
  if (hit) return hit;
  const late = await net;               // 無快取:只好等網路
  return late || Response.error();
}

async function shellFetch(req) {
  const cache = await caches.open(SHELL_CACHE);
  const hit = await cache.match(req);
  const net = fetch(req).then(res => {
    if (res.ok) cache.put(req, res.clone());
    return res;
  }).catch(() => null);
  if (hit) return hit;                  // 背景的 net 仍會更新快取
  const res = await net;
  return res || Response.error();
}

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(url.pathname.includes("/data/") ? dataFetch(e.request) : shellFetch(e.request));
});
