# -*- coding: utf-8 -*-
"""多來源共用工具(mylivescore / LAPGO / tsbadminton)。

賽事 JSON 的頂層 `source` 欄位標示資料來源,前端據此顯示來源標籤與正確的官方外連。
名次(standings)的 `source` 則標示該筆名次怎麼來的,優先序見 STANDINGS_PRIORITY。
"""
import http.cookiejar
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

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


# 同一場真實賽事被兩個平台各收一次時,留哪一邊(數字大者為 canonical)。
# mylivescore 最完整(逐場比分最多、有 resultPdf/documents),故最高;
# manual 是人工由官方 PDF 匯入,可信度次之。
SOURCE_PRIORITY = {SRC_MYLIVESCORE: 40, SRC_MANUAL: 30, SRC_LAPGO: 20, SRC_TSBA: 10}


def source_priority(t):
    return SOURCE_PRIORITY.get(source_of(t), 0)


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


# ---------- 日期 ----------
# 來源的 dateStart 偶爾是錯的(實測 5 場):有整串複製到別場的、有日期反轉的、
# 也有 0000-00-00。列表排序與年份篩選都吃這個欄位,錯了就會排到不相干的位置。

BAD_DATES = {"", "0000-00-00", None}


def match_date_range(t):
    """由 matches[].date 取實際比賽日期區間;沒有比分回 None。"""
    ds = sorted({m.get("date") for m in (t.get("matches") or [])
                 if m.get("date") not in BAD_DATES})
    return (ds[0], ds[-1]) if ds else None


def effective_dates(t):
    """列表排序/篩選要用的日期區間。

    只有在 dateStart 無效、或與實際比分日期「完全沒有交集」時才改用比分推得的區間;
    「公告 2/28 起、實際 3/1 才開打」這種正常落差一律保留原值。
    日期反轉(dateStart > dateEnd)會讓交集判斷自然失敗,因而一併被修正 —— 故意不先排序。

    docs/js/common.js 的 effectiveDates() 是同一條規則的 JS 版,兩邊必須同步修改。
    """
    ds = t.get("dateStart")
    de = t.get("dateEnd") or ds
    real = match_date_range(t)
    if not real:
        return ("" if ds in BAD_DATES else ds, "" if de in BAD_DATES else de)
    if ds in BAD_DATES:
        return real
    if de in BAD_DATES:
        de = ds
    return (ds, de) if (ds <= real[1] and real[0] <= de) else real


# ---------- 跨來源去重 ----------
# 同一場真實賽事被兩個平台各收一次時,只留 SOURCE_PRIORITY 高的那一份,
# 另一份(shadow)刪檔並記進 duplicates.json,爬蟲之後不再抓。判定規則見 dedupe.py。

DUPLICATES_PATH = Path(__file__).resolve().parent / "duplicates.json"

_PUNCT_RE = re.compile(r"[\s\-_·．・,，、.。/\\:：;；!！?？+*&\"'’]")
_BRACKET_RE = re.compile(r"[「」『』（）()\[\]【】〈〉<>{}]")
_SUBDRAW_RE = re.compile(r"\[[^\]]*\]\s*$")


def norm_tourn_name(name):
    """賽名正規化:全半形、臺/台、括號與標點差異都抹平後再比相似度。"""
    s = unicodedata.normalize("NFKC", name or "").replace("臺", "台")
    return _PUNCT_RE.sub("", _BRACKET_RE.sub("", s))


_YEAR_RE = re.compile(r"20\d{2}|1\d{2}年")


def year_tokens(name):
    """賽名裡的年份(西元 20xx 或民國 1xx年)。

    「114年全國中等學校運動會羽球賽資格賽」與「115年…」相似度高達 0.95、組別完全相同、
    連公告日期都重疊(兩場都還沒有比分),只有年份能區分 —— 少了這道就會誤判成同一場。
    """
    return set(_YEAR_RE.findall(unicodedata.normalize("NFKC", name or "")))


def norm_group_key(group):
    """組名正規化。LAPGO 的組名常被截斷少了右括號(「夫妻雙打(合計70歲以上」),
    去掉括號類字元後才對得上 mylivescore 的完整寫法。"""
    s = unicodedata.normalize("NFKC", group or "").replace("臺", "台")
    return _PUNCT_RE.sub("", _BRACKET_RE.sub("", _SUBDRAW_RE.sub("", s)))


def group_keys(t):
    """賽事的組別集合(名次與競賽組別合起來看,只有其一的賽事也能比對)。"""
    keys = {norm_group_key(s.get("group")) for s in (t.get("standings") or [])}
    keys |= {norm_group_key(g.get("name")) for g in (t.get("groups") or [])}
    return {k for k in keys if k}


def best_standing_source(t):
    """組別 → 該組名次的最高來源優先度。用於「shadow 是否有 canonical 沒有的好資料」。"""
    best = {}
    for s in t.get("standings") or []:
        g = norm_group_key(s.get("group"))
        best[g] = max(best.get(g, 0), standings_rank(s.get("source")))
    return best


def load_duplicates():
    """已登錄的重複賽事對。回傳 list[{canonical, shadow, ...}]。"""
    if not DUPLICATES_PATH.exists():
        return []
    data = json.loads(DUPLICATES_PATH.read_text(encoding="utf-8"))
    return data.get("pairs") or []


def blocked_openids():
    """爬蟲阻擋清單:這些 openid 已判定為重複並刪檔,不要再抓也不要進索引。"""
    return {str(p["shadow"]) for p in load_duplicates() if p.get("shadow")}


def save_duplicates(pairs):
    DUPLICATES_PATH.write_text(json.dumps({
        "_note": "自動偵測的跨來源重複賽事(見 scripts/dedupe.py)。"
                 "shadow 檔已刪除,爬蟲會跳過不再抓取;需要復原時可由 git 歷史取回。",
        "pairs": sorted(pairs, key=lambda p: str(p.get("shadow", ""))),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
