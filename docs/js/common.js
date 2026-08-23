/* 共用工具:資料載入、格式化、導覽列 */
const DATA = "./data";
const _cache = {};

async function loadJSON(path) {
  if (_cache[path]) return _cache[path];
  const res = await fetch(`${DATA}/${path}`);
  if (!res.ok) throw new Error(`載入失敗 ${path} (${res.status})`);
  const d = await res.json();
  _cache[path] = d;
  return d;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function fmtDate(s) {
  if (!s || s === "0000-00-00") return "";
  return s.replace(/-/g, "/");
}
function fmtRange(a, b) {
  const fa = fmtDate(a), fb = fmtDate(b);
  if (!fa && !fb) return "—";
  if (!fb || fa === fb) return fa;
  return `${fa} ~ ${fb}`;
}

/* 少數賽事的 dateStart 是錯的(複製到別場、日期反轉、0000-00-00)。index.json 由
   rebuild_index 修正過,但本頁的賽事詳情是直接讀賽事檔,得在這裡套同一條規則,
   否則列表頁與詳情頁的日期會不一致。
   與 scripts/sources_common.py 的 effective_dates() 必須完全一致。 */
const BAD_DATES = new Set(["", "0000-00-00"]);
/* 來源日期錯得離譜、又沒有比分可以回推的賽事,明列覆寫。
   與 sources_common.py 的 DATE_OVERRIDES 是同一份表。 */
const DATE_OVERRIDES = {
  "531555": ["2025-03-02", "2025-03-09"],  // 114 年全中運資格賽誤標為 2026 年
};
function matchDateRange(t) {
  const ds = [...new Set((t.matches || []).map(m => m.date).filter(d => d && !BAD_DATES.has(d)))].sort();
  return ds.length ? [ds[0], ds[ds.length - 1]] : null;
}
function effectiveDates(t) {
  const over = DATE_OVERRIDES[String(t.openid)];
  if (over) return over;
  const ds = t.dateStart, de = t.dateEnd || ds;
  const real = matchDateRange(t);
  const clean = v => (!v || BAD_DATES.has(v)) ? "" : v;
  if (!real) return [clean(ds), clean(de)];
  if (!ds || BAD_DATES.has(ds)) return real;
  const end = (!de || BAD_DATES.has(de)) ? ds : de;
  return (ds <= real[1] && real[0] <= end) ? [ds, end] : real;
}

const STATUS_LABEL = { registering: "報名中", ongoing: "進行中", finished: "已結束" };
function statusBadge(st) {
  const label = STATUS_LABEL[st] || st;
  return `<span class="badge ${esc(st)}">${esc(label)}</span>`;
}

function rankLabel(r) {
  return { 1: "冠軍", 2: "亞軍", 3: "季軍", 4: "殿軍" }[r] || `第${r}名`;
}
function rankClass(r) { return r <= 3 ? `rank-${r}` : ""; }

function baseGroup(g) { return String(g || "").replace(/\[[^\]]*\]\s*$/, "").trim(); }
function splitMembers(raw) {
  return String(raw || "").split(/[-/、,，]/).map(s => s.trim()).filter(Boolean);
}

function qs(name) { return new URLSearchParams(location.search).get(name) || ""; }

/* ---------- 資料來源 ---------- */
/* 舊資料沒有 source 欄位,一律視為 mylivescore(與 sources_common.source_of 一致) */
const SOURCES = {
  mylivescore: { label: "MY Livescore", site: "https://mylivescore.tw/matches.html" },
  lapgo: { label: "LAPGO", site: "https://lapgo.com.tw/activity" },
  tsba: { label: "全民羽球發展協會", site: "https://www.tsbadminton.url.tw/" },
  manual: { label: "PDF 匯入", site: null },
};
function sourceOf(t) { return (t && t.source) || "mylivescore"; }
function sourceLabel(t) { return (SOURCES[sourceOf(t)] || SOURCES.mylivescore).label; }
/* 官方賽事頁:非 mylivescore 的來源把網址存在 sourceUrl,不能套 mylivescore 的樣板 */
function officialUrl(t) {
  const src = sourceOf(t);
  if (t.sourceUrl) return t.sourceUrl;
  if (src === "mylivescore") {
    return `https://livescore.efsoft.net/golivequery.php?openid=${encodeURIComponent(t.openid)}`;
  }
  return (SOURCES[src] || {}).site || null;
}

/* ---------- 選手/單位分片 ---------- */
/* 與 scripts/rebuild_index.py 的 shard_of 完全一致:h = (h*31 + codepoint) mod 2^32 */
function shardOf(name, n = 16) {
  let h = 0;
  for (const ch of String(name)) h = ((Math.imul(h, 31) >>> 0) + ch.codePointAt(0)) >>> 0;
  return h % n;
}
function loadPlayerShard(name) { return loadJSON(`players/${shardOf(name)}.json`); }
function loadUnitShard(name) { return loadJSON(`units/${shardOf(name)}.json`); }

/* 輕量搜尋索引拆成兩檔:單位頁只需要 units 那半(gzip 171KB),
   不必連 players 那半(gzip 669KB)一起載。搜尋頁兩檔都要,並行載入總量不變。 */
function loadSearchPlayers() { return loadJSON("search-index-players.json"); }
function loadSearchUnits() { return loadJSON("search-index-units.json"); }

/* ---------- 單位名稱的縣市前綴(用於判斷同名不同校) ---------- */
const COUNTY_PREFIXES = [
  ["臺北市", "北市"], ["台北市", "北市"], ["北市", "北市"],
  ["新北市", "新北"], ["新北", "新北"],
  ["桃園市", "桃園"], ["桃市", "桃園"],
  ["臺中市", "中市"], ["台中市", "中市"], ["中市", "中市"],
  ["臺南市", "南市"], ["台南市", "南市"], ["南市", "南市"],
  ["高雄市", "高市"], ["高市", "高市"],
  ["基隆市", "基隆"], ["基市", "基隆"],
  ["新竹市", "竹市"], ["竹市", "竹市"], ["新竹縣", "竹縣"], ["竹縣", "竹縣"],
  ["苗栗縣", "苗栗"], ["苗縣", "苗栗"],
  ["彰化縣", "彰化"], ["彰縣", "彰化"],
  ["南投縣", "南投"], ["投縣", "南投"],
  ["雲林縣", "雲林"], ["雲縣", "雲林"],
  ["嘉義市", "嘉市"], ["嘉市", "嘉市"], ["嘉義縣", "嘉縣"], ["嘉縣", "嘉縣"],
  ["屏東縣", "屏東"], ["屏縣", "屏東"],
  ["宜蘭縣", "宜蘭"], ["宜縣", "宜蘭"],
  ["花蓮縣", "花蓮"], ["花縣", "花蓮"],
  ["臺東縣", "臺東"], ["台東縣", "臺東"], ["東縣", "臺東"],
  ["澎湖縣", "澎湖"], ["澎縣", "澎湖"],
  ["金門縣", "金門"], ["金縣", "金門"],
  ["連江縣", "連江"],
].sort((a, b) => b[0].length - a[0].length);
function countyOf(unitName) {
  const s = String(unitName || "");
  for (const [prefix, canon] of COUNTY_PREFIXES) if (s.startsWith(prefix)) return canon;
  return "";
}

function playerLink(name) {
  return `<a href="./player.html?name=${encodeURIComponent(name)}">${esc(name)}</a>`;
}
function unitLink(name) {
  return `<a href="./unit.html?name=${encodeURIComponent(name)}">${esc(name)}</a>`;
}
function tournLink(openid, text) {
  return `<a href="./tournament.html?id=${encodeURIComponent(openid)}">${esc(text)}</a>`;
}

/* 底部導覽。active: matches | search | about */
function renderNav(active) {
  const items = [
    ["index.html", "matches", "🏸", "賽事"],
    ["search.html", "search", "🔍", "搜尋"],
    ["about.html", "about", "ℹ️", "關於"],
  ];
  const nav = document.createElement("nav");
  nav.className = "bottomnav";
  nav.innerHTML = items.map(([href, key, ico, label]) =>
    `<a href="./${href}" class="${key === active ? "active" : ""}">
       <span class="ico">${ico}</span><span>${label}</span></a>`).join("");
  document.body.appendChild(nav);
}

/* 賽事索引轉 map */
async function tournMap() {
  const idx = await loadJSON("index.json");
  const m = {};
  idx.forEach(t => { m[t.openid] = t; });
  return m;
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("./sw.js").catch(() => {});
  });
}
