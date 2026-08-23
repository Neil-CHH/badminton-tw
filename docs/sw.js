/* 羽球賽事資料庫 Service Worker
   - App shell:stale-while-revalidate(先回快取秒開,背景抓新版,下次打開即更新;
     不再依賴手動升版本號)
   - data/*.json:network-first,3.5 秒沒回應或離線即退回快取

   兩種策略的 fetch 都要帶 { cache: "no-cache" }:GitHub Pages 對所有檔案都回
   Cache-Control: max-age=600,不指定的話 fetch 會直接吃瀏覽器那份 10 分鐘的 HTTP 快取、
   根本不碰網路 —— network-first 會退化成「10 分鐘內都是舊的」,背景更新也一樣被擋住。
   no-cache 不是不快取,是「一定帶 ETag 去問伺服器」,沒變就回 304,幾乎不花流量。 */
/* v7:search-index.json 拆成 search-index-players/units 兩檔並刪除舊檔。
   shell 平常是 stale-while-revalidate、不必手動升版,但這次「舊 HTML 會去要
   一個已經不存在的資料檔」,升版強制換掉 shell 與 data 快取才不會卡在 404。 */
const VERSION = "v7";
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
  const net = fetch(req, { cache: "no-cache" }).then(res => {
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
  const net = fetch(req, { cache: "no-cache" }).then(res => {
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
