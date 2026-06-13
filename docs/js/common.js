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
