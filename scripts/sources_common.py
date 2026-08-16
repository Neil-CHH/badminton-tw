# -*- coding: utf-8 -*-
"""多來源共用工具(mylivescore / LAPGO / tsbadminton)。

賽事 JSON 的頂層 `source` 欄位標示資料來源,前端據此顯示來源標籤與正確的官方外連。
名次(standings)的 `source` 則標示該筆名次怎麼來的,優先序見 STANDINGS_PRIORITY。
"""
import http.cookiejar
import json
import re
import time
import urllib.parse
import urllib.request

# ---------- 賽事來源 ----------

SRC_MYLIVESCORE = "mylivescore"
SRC_LAPGO = "lapgo"
SRC_TSBA = "tsba"
SRC_MANUAL = "manual"

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 非羽球賽事(各平台的分類旗標都會被主辦填錯:LAPGO 有籃球/排球/樂樂棒球被標成「羽球比賽」)
NON_BADMINTON = re.compile(
    r"匹克球|桌球|網球|籃球|棒球|壘球|排球|足球|樂樂|研習|裁判|柔道|游泳|田徑|體操")


def source_of(t):
    """取賽事來源;舊資料沒有 source 欄位時由 openid 格式回推。"""
    s = t.get("source")
    if s:
        return s
    openid = str(t.get("openid", ""))
    for prefix, src in (("lapgo-", SRC_LAPGO), ("tsba-", SRC_TSBA), ("manual-", SRC_MANUAL)):
        if openid.startswith(prefix):
            return src
    return SRC_MYLIVESCORE


# ---------- 名次來源優先序 ----------
# pdf   : 官方總成績紀錄 PDF(人工匯入,最權威)
# official: 主辦平台 API 提供的官方成績總表(LAPGO)
# ocr   : 成績圖片視覺解析(tsba),經名冊字典校對
# derived: 由決賽比分自動推導
STANDINGS_PRIORITY = {"pdf": 40, "official": 30, "ocr": 20, "derived": 10}


def standings_rank(source):
    return STANDINGS_PRIORITY.get(source, 0)


def _by_group(rows):
    d = {}
    for s in rows or []:
        d.setdefault(s.get("group", ""), []).append(s)
    return d


def _top_source(rows):
    """同一組別內只保留優先度最高的來源(呼叫端常把 official 與 derived 混在同一份清單)。"""
    if not rows:
        return []
    top = max(standings_rank(s.get("source")) for s in rows)
    return [s for s in rows if standings_rank(s.get("source")) == top]


def merge_standings(existing, incoming):
    """以「組別」為單位挑優先度最高的來源,同一組不混用兩種來源。

    同優先度時採用 incoming,讓重抓能更新 derived 名次;incoming 沒有的組別保留 existing
    (PDF 匯入的名次因此不會被之後的重抓洗掉)。
    """
    ex, inc = _by_group(existing), _by_group(incoming)
    out = []
    for g in set(ex) | set(inc):
        e_rows = _top_source(ex.get(g) or [])
        i_rows = _top_source(inc.get(g) or [])
        e_p = max((standings_rank(s.get("source")) for s in e_rows), default=-1)
        i_p = max((standings_rank(s.get("source")) for s in i_rows), default=-1)
        out.extend(i_rows if (i_rows and i_p >= e_p) else e_rows)
    out.sort(key=lambda s: (s.get("group", ""), s.get("rank", 0)))
    return out


# ---------- 寫檔 ----------

def same_content(a, b):
    """除 lastUpdated 外完全相同。用來避免每月把沒變的賽事整份重寫(製造 git 噪音)。"""
    if not a or not b:
        return False
    return ({k: v for k, v in a.items() if k != "lastUpdated"}
            == {k: v for k, v in b.items() if k != "lastUpdated"})


def write_if_changed(path, record, existing):
    """內容有變才寫檔。回傳 True 表示真的寫了。"""
    if same_content(record, existing):
        return False
    path.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    return True


# ---------- 縣市 ----------

_CITY_FULL = ["臺北市", "新北市", "基隆市", "桃園市", "新竹縣", "新竹市", "苗栗縣",
              "臺中市", "彰化縣", "南投縣", "雲林縣", "嘉義縣", "嘉義市", "臺南市",
              "高雄市", "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣", "澎湖縣", "金門縣", "連江縣"]
# 場地文字常見只寫縣市名不寫後綴(例:國立彰化師範大學)。新竹/嘉義有縣市同名,取市。
_CITY_BARE = {"臺北": "臺北市", "新北": "新北市", "基隆": "基隆市", "桃園": "桃園市",
              "新竹": "新竹市", "苗栗": "苗栗縣", "臺中": "臺中市", "彰化": "彰化縣",
              "南投": "南投縣", "雲林": "雲林縣", "嘉義": "嘉義市", "臺南": "臺南市",
              "高雄": "高雄市", "屏東": "屏東縣", "宜蘭": "宜蘭縣", "花蓮": "花蓮縣",
              "臺東": "臺東縣", "澎湖": "澎湖縣", "金門": "金門縣", "連江": "連江縣"}


def city_from_text(text, default="其他"):
    """由場地/賽名文字推出縣市(LAPGO 沒有縣市欄位)。台/臺 皆可。"""
    if not text:
        return default
    t = text.replace("台", "臺")
    for c in _CITY_FULL:
        if c in t:
            return c
    for bare, full in _CITY_BARE.items():
        if bare in t:
            return full
    return default


# ---------- HTTP ----------

class Http:
    """帶 cookie jar 的簡易 client。LAPGO 需要 CSRF token + cookie,tsba 需要瀏覽器 UA。"""

    def __init__(self, ua=BROWSER_UA, timeout=40):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.ua = ua
        self.timeout = timeout

    def _open(self, req, retries):
        last = None
        for attempt in range(retries + 1):
            try:
                with self.opener.open(req, timeout=self.timeout) as res:
                    return res.read()
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"HTTP 失敗 {req.full_url}: {last}")

    def get(self, url, referer=None, retries=2):
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", self.ua)
        if referer:
            req.add_header("Referer", referer)
        return self._open(req, retries)

    def get_text(self, url, referer=None, retries=2):
        return self.get(url, referer, retries).decode("utf-8", errors="replace")

    def post_form(self, url, data, referer=None, headers=None, retries=2):
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("User-Agent", self.ua)
        req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        req.add_header("X-Requested-With", "XMLHttpRequest")
        if referer:
            req.add_header("Referer", referer)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        return self._open(req, retries).decode("utf-8", errors="replace")


_CSRF_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')


def find_csrf(html):
    m = _CSRF_RE.search(html)
    if not m:
        raise RuntimeError("找不到 csrf-token")
    return m.group(1)


def loads_lenient(raw):
    """回應有時是被 JSON 字串包住的 JSON,或含未跳脫換行。"""
    d = json.loads(raw, strict=False)
    if isinstance(d, str):
        d = json.loads(d, strict=False)
    return d
