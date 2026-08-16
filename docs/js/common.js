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
