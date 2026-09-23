# -*- coding: utf-8 -*-
"""解析官方「報名結果」PDF → entries[](source=signup)。

為什麼要這支:賽事在「抽籤完、還沒打完」這段期間,API 既沒有比分也沒有名次,
整場一位選手都查不到 —— 但官方的報名結果 PDF 早就掛在 documents[] 裡了
(264311 羽霸盃就是 867 組 755 人躺在那裡沒人解析)。entries[] 這個機制本來就存在
(tsba 靠它讓 15,725 位沒得名的選手查得到),只是 mylivescore 這邊一直沒有產生器。

版面(全庫 61 份實測):

    [單號][繳費狀況][組別][項目][選手1][隊名1][選手2][隊名2][種子序]   ← 表頭
    [國小二年級女單]                                      [共7組]      ← 組別標頭
    [1151…][已付款][單打][國小二年級女單][柯寶豔][臺中市忠明國小]        ← 資料列

**不能用 page.find_tables()** —— 報名結果 PDF 沒有格線,實測回 0 張表(成績總表 PDF
有格線,所以 parse_result_pdf 那套在這裡不適用)。改用 words 的座標:依 y 併列、依 x 切欄。

表頭有 44 種寫法,但每一種都以「單號」開頭、欄名自我描述,所以一律**照欄名判角色**、
不寫死欄位順序 —— 尤其「領隊」「管理」「管理員」「教練N」欄裡放的是真人名,
但那不是選手,收進去會讓幹部憑空多出參賽紀錄。

第二種版面是 **LAPGO 的「選手名單」**(2026-09 加,見下方 parse_lapgo 的說明):
兩欄並排的 `編號｜隊名｜姓名`,組別寫在表格外的標題列並自帶「【共N組】」。
兩種版面共用同一套守門與寫入,只差讀法,`build_entries` 依表頭自動判別。

用法:
    python -X utf8 scripts/parse_entry_pdf.py --openid 264311            # 只看報告
    python -X utf8 scripts/parse_entry_pdf.py --openid 264311 --apply    # 寫入
    python -X utf8 scripts/parse_entry_pdf.py --all                      # 掃描報告
    python -X utf8 scripts/parse_entry_pdf.py --all --apply              # 套用
"""
import argparse
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
TOURN_DIR = ROOT / "docs" / "data" / "tournaments"
sys.path.insert(0, str(ROOT / "scripts"))
from parse_result_pdf import clean_name, match_roster, resolve_group  # noqa: E402
from sources_common import SRC_LAPGO, source_of, write_if_changed     # noqa: E402

CACHE = ROOT / "inbox" / "entrypdf"          # inbox 已 gitignore
# mylivescore 叫「報名結果」、LAPGO 的公告叫「選手名單」,兩邊都是同一件事:誰報了名。
SIGNUP_KEYWORDS = ("報名結果", "選手名單")
DROPOUT_KEYWORDS = ("不成組",)

# 同一列的 y 容差。不能用「四捨五入到固定格線」分列:704036 的組別標頭「35歲組男單」
# 在 y=62.5、右側的「共2組」在 y=63.0,固定格線會把同一列切成兩列,宣告組數就讀不到
# (實測 3 場的宣告數因此變成 0,而那是唯一的驗證基準)。
ROW_TOL = 3.0
# 判定「上下兩行是同一個欄名」的 x 容差。堆疊的欄名不會對齊到 pt(734564 的「衣服」在
# x=201、「尺寸1」在 x=199),但相鄰欄至少差 25pt 以上,所以這個值很寬鬆也不會誤併。
HEAD_FRAG_X = 8.0


_COUNT_RE = re.compile(r"^共(\d+)[組籤隊人]$")
_PLAYER_RE = re.compile(r"^選手(\d+)$")
_UNIT_RE = re.compile(r"^(隊名|縣市別)(\d*)$")
_ORDER_RE = re.compile(r"^\d{6,}$")          # 單號:六位以上的報名序號
_DRIVE_RE = re.compile(r"^https?://drive\.google\.com/file/d/([\w-]+)", re.I)

# ---- LAPGO 選手名單版面 ----
# 組別標題:「學生個人組-國小低年級男單【共33組】」。後面可能還接「比賽日期:10/22」,
# 而收尾的「】」偶爾會被排版擠到下一列(實測 lapgo-76),所以右括號與尾巴都放寬。
_LAPGO_TITLE = re.compile(r"^(.*?)【共\s*(\d+)\s*[組隊人]】?")
_LAPGO_SEAT = re.compile(r"^(\d+)[-–](\d+)$")   # 編號:{組序}-{席次}
# 欄名 → 角色。判角色前先剝掉括號註記(2024 版寫「隊名(單位)」,半形全形都出現過)。
_LAPGO_PAREN = re.compile(r"[((].*?[))]")
_LAPGO_ROLES = {
    "編號": "no", "參加編號": "no",
    "隊名": "unit",
    "姓名": "name", "選手": "name",
    # 2024 年的版面每列還重印一次組別。組別一律以標題為準(「【共N組】」是守門的
    # 唯一分母,兩邊寫法不同會讓抽到數對不上宣告數),這欄只要認得出來、不讀進去。
    "項目": "ignore",
}
# 隊名裡的「A/B」在雙打代表兩位搭檔分屬兩校。只認半形/全形斜線、且段數與人數都是 2
# 才拆 —— 隊名本來就很自由,「臨打+2」「間諜8+9」「雨一直下、NO NO」都是真隊名,
# 把 +、、 也當分隔就會拆爛(全庫 11,781 席裡只有 20 席帶分隔符)。
_LAPGO_UNIT_SPLIT = re.compile(r"[/／]")

# 抽到/宣告 落在這個區間外就只報告不寫檔。報名結果 PDF 自己宣告了每組的組數,
# 那是唯一的驗證基準;對不上就是版面沒讀對,寧可留空號讓 verify 報出來。
MIN_RATIO, MAX_RATIO = 0.9, 1.1
MIN_TOTAL = 10       # 全場抽到的筆數低於此值,視為這份檔沒有可用文字


def download_url(url):
    """Google Drive 的分享連結(/file/d/{id}/view)不會回 PDF,要換成直接下載端點。
    LAPGO 的名單一律掛在 Drive,不換就只會拿到一頁 HTML。"""
    m = _DRIVE_RE.match(url or "")
    return (f"https://drive.google.com/uc?export=download&id={m.group(1)}" if m
            else urllib.parse.quote(url, safe=":/%?=&"))


def fetch_pdf(openid, url, kind):
    CACHE.mkdir(parents=True, exist_ok=True)
    # 快取檔名帶 URL 雜湊:主辦換新版名單時 URL 會變,沿用舊檔名會一直讀到過期的那份
    tag = hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:8]
    p = CACHE / f"{openid}-{kind}-{tag}.pdf"
    if p.exists() and p.stat().st_size > 1024:
        return p.read_bytes()
    req = urllib.request.Request(download_url(url))
    req.add_header("User-Agent", "Mozilla/5.0 (badminton-db parse_entry_pdf)")
    with urllib.request.urlopen(req, timeout=90) as res:
        data = res.read()
    p.write_bytes(data)
    return data


def doc_url(t, keywords):
    for d in t.get("documents") or []:
        if any(k in (d.get("title") or "") for k in keywords):
            return d.get("url"), d.get("title")
    return None, None


def signup_docs(t):
    """要解析的名單文件 [(url, title), ...]。

    mylivescore 只取**最新的一份**「報名結果」(舊版還留在 documents 裡,全解會把
    退掉的人又收回來);LAPGO 的名單常拆成個人組/團體組好幾個檔貼在同一篇公告,
    少收任何一個就是整批選手查不到,所以全部都要。documents 已依日期排序。
    """
    docs = [(d.get("url"), d.get("title") or "") for d in t.get("documents") or []
            if any(k in (d.get("title") or "") for k in SIGNUP_KEYWORDS) and d.get("url")]
    if not docs:
        return []
    return docs if source_of(t) == SRC_LAPGO else docs[:1]


def page_rows(page):
    """一頁的文字依 y 併成列 → [[(x0, text), ...], ...],列內依 x 遞增。"""
    return [row for _y, row in rows_with_y(page)]


def rows_with_y(page):
    """同 page_rows,但保留每列的 y —— LAPGO 版面把姓名配給「y 最近的編號」要用。"""
    words = sorted(page.get_text("words"), key=lambda w: (w[1], w[0]))
    rows, cur, top = [], [], None
    for x0, y0, _x1, _y1, txt, *_ in words:
        if top is None or y0 - top > ROW_TOL:
            if cur:
                rows.append((top, sorted(cur)))
            cur, top = [], y0
        cur.append((x0, txt))
    if cur:
        rows.append((top, sorted(cur)))
    return rows


def bounds(cols):
    """表頭 → [(右界, 欄名), ...]。界線取相鄰兩個表頭起點的中點。

    不能用「表頭起點以右就算這一欄」:儲存格是置中排版的,文字比表頭寬時起點會**跑到
    表頭左邊**(704036 的團體分頁,單號欄表頭在 x=54、報名序號卻從 x=45 開始),
    那樣整列都會被判成沒有單號而整段丟掉(實測 8 個團體組共 66 隊憑空消失)。
    """
    xs = [c[0] for c in cols]
    edges = [(xs[i] + xs[i + 1]) / 2 for i in range(len(xs) - 1)] + [float("inf")]
    return list(zip(edges, (c[1] for c in cols)))


def assign(row, cols):
    """一列文字依欄位界線切欄 → {欄名: [字, ...]}。"""
    out = defaultdict(list)
    edges = bounds(cols)
    for x0, txt in row:
        for edge, cname in edges:
            if x0 < edge:
                out[cname].append(txt)
                break
    return out


def cell_text(parts):
    """同一格內的字接回去。get_text("words") 依空白切字,英文姓名「Cheng Da Dang」
    會被拆成三個字,不接回去會變成三位不存在的選手;中文之間則不補空格。
    純數字的碎片丟掉 —— 姓名格裡混進來的是序號(實測「180 陳俞安」「0 黃柏宇」),
    不是名字的一部分。

    **接到一半已經出現過的尾巴就跳過**(2026-09 加):有些 PDF 把同一段文字疊印兩次
    來做假粗體,偏移幾個 pt,words 就回兩份 —— 285271 竹南鎮長盃的
    「張祐榮」+「祐榮」會接成「張祐榮祐榮」這個不存在的人(實測 10 筆已寫進庫裡)。
    正常的姓名碎片是不同的字(「Bekti」+「Saputra」),不會是已接內容的尾巴。
    """
    out = ""
    for x in parts:
        if x.isdigit() or (out and out.endswith(x)):
            continue
        if out and re.search(r"[A-Za-z0-9]$", out) and re.match(r"[A-Za-z0-9]", x):
            out += " "
        out += x
    return out.strip()


def column_roles(cols):
    """表頭 → (選手欄, 每位選手的單位欄, 團體隊名欄)。

    「領隊」「管理」「管理員」「教練N」不符合「選手N」,自然不會被讀進來 ——
    那些欄位放的是真人名,收進去會讓幹部憑空多出參賽紀錄。
    """
    players, units, team_col = [], {}, None
    for _x, n in cols:
        m = _PLAYER_RE.match(n)
        if m:
            players.append((int(m.group(1)), n))
            continue
        m = _UNIT_RE.match(n)
        if not m:
            continue
        kind, idx = m.group(1), m.group(2)
        if not idx:
            team_col = team_col or n
        elif kind == "隊名" or int(idx) not in units:
            # 同一個號碼同時有「縣市別N」與「隊名N」時取隊名(704036):
            # 本庫的單位是社團/學校名,不是縣市
            units[int(idx)] = n
    players.sort()
    # 只有一個帶號碼的隊名欄、卻有三個以上選手欄 → 那個隊名是團體隊伍名
    if team_col is None and len(units) == 1 and len(players) > 2:
        team_col = next(iter(units.values()))
        units = {}
    use_team = team_col is not None and len(players) > 2
    return players, units, (team_col if use_team else None)


def is_header(texts):
    return "單號" in texts and ("組別" in texts or "項目" in texts)


def _row_pitch(rows):
    """一頁的主要行距(相鄰列 y 差的中位數)。堆疊的表頭列靠得比這個近得多。"""
    ys = [y for y, _r in rows]
    diffs = sorted(b - a for a, b in zip(ys, ys[1:]) if b > a)
    return diffs[len(diffs) // 2] if diffs else 0.0


def header_cols(rows, i):
    """主表頭那一列 + 疊在它上下的欄名碎片 → [(x, 欄名), ...]。

    **表頭可以疊成好幾列**(2026-09 修):734564 成大盃的「衣服尺寸1/2」「參照成績1/2」
    欄名被拆成上下兩行(「衣服」在主表頭上方、「尺寸1」在下方),主表頭那列裡根本
    沒有這幾欄。只認主表頭,選手1 與 隊名1 之間就少了一道界線,置中的尺寸值(XL/2L)
    會落進選手格,解出「陳士智XL」「2L侯靖思」這種假選手 —— 實測該場 2,234 個姓名
    只有 53 個對得上比分。欄名本身其實不重要(column_roles 只挑選手N/隊名N,認不得的
    欄自然被忽略),**要的是那道 x 界線**。

    判準是 y:堆疊的欄名離主表頭不到一般行距的 0.7 倍(實測 3.6pt vs 行距 6.9pt),
    而最近的資料列或組別標頭至少隔一整個行距。再排掉帶單號/「共N組」的列以策安全。
    """
    y0, main = rows[i]
    cols = {x: [t] for x, t in main}
    pitch = _row_pitch(rows)
    for j in (i - 1, i + 1):
        if not 0 <= j < len(rows):
            continue
        y, row = rows[j]
        if not pitch or abs(y - y0) >= pitch * 0.7:
            continue
        if any(_ORDER_RE.match(t) or _COUNT_RE.match(t) for _x, t in row):
            continue
        for x, t in row:
            near = min(cols, key=lambda c: abs(c - x))
            if abs(near - x) <= HEAD_FRAG_X:
                cols[near].insert(0 if j < i else len(cols[near]), t)
            else:
                cols[x] = [t]
    return sorted((x, "".join(v)) for x, v in cols.items())


def find_header(doc):
    for page in doc:
        rows = rows_with_y(page)
        for i, (_y, row) in enumerate(rows):
            if is_header([t for _x, t in row]):
                return header_cols(rows, i)
    return []


def parse_pdf(doc):
    """報名結果 / 不成組名單 PDF → (declared, rows)。

    declared = {組別: 宣告組數};rows = [(組別, {欄名: [字]}, 該列所屬的表頭), ...]。
    兩種 PDF 是同一套系統產的、版面相同,不成組名單只是多一行標題、少了種子序。

    **表頭要逐列跟著換,不能整份用第一個** —— 一份 PDF 可以有兩種表頭:704036 前 7 頁
    是個人賽(選手1｜隊名1｜選手2｜隊名2),第 8 頁是團體賽(隊名｜選手1…選手8)。
    整份沿用第一個表頭,團體隊伍名那一欄就對不到,66 隊的單位會全部變成空字串。
    """
    cols, group = [], None
    declared, rows = {}, []
    for page in doc:
        page_r = rows_with_y(page)
        for i, (_y, row) in enumerate(page_r):
            texts = [t for _x, t in row]
            if is_header(texts):
                cols = header_cols(page_r, i)
                continue
            counts = [t for t in texts if _COUNT_RE.match(t)]
            if counts and len(row) <= 3:
                rest = [t for t in texts if not _COUNT_RE.match(t)]
                if rest:
                    group = rest[0]
                    declared[group] = int(_COUNT_RE.match(counts[0]).group(1))
                continue
            if not cols:
                continue
            cells = assign(row, cols)
            # 資料列一定有報名序號。這道過濾同時擋掉頁首標題 —— 標題的字會落進
            # 某個欄位區間,不擋就會冒出「2026」這種選手(實測 264311)
            if not any(_ORDER_RE.match(x) for x in cells.get("單號", [])):
                continue
            rows.append((group, cells, tuple(cols)))
    return declared, rows


def rows_to_people(rows):
    """資料列 → [(組別, 單位, 姓名, 姓名原字串), ...]。"""
    roles = {}
    out = []
    for group, cells, cols in rows:
        if cols not in roles:
            roles[cols] = column_roles(cols)
        players, units, team_col = roles[cols]
        g = group or cell_text(cells.get("項目", []))
        if not g:
            continue
        team = cell_text(cells.get(team_col, [])) if team_col else ""
        for idx, col in players:
            raw = cell_text(cells.get(col, []))
            name = clean_name(raw)
            if not name:
                continue
            unit = team or cell_text(cells.get(units.get(idx, ""), []))
            out.append((g, unit, name, raw))
    return out


# ---------- 版面二:LAPGO 選手名單 ----------

def _lapgo_role(text):
    return _LAPGO_ROLES.get(_LAPGO_PAREN.sub("", text or "").strip())


def lapgo_header(row):
    """表頭列 → [(右界, 欄塊序, 角色), ...];角色 ∈ no/unit/name/group。不是表頭回 []。

    一頁常分左右兩個欄塊、欄序完全相同,所以先照欄名把每欄轉成角色,再確認整列是
    同一組角色重複 N 次。**照欄名判角色、不寫死順序**(同 column_roles 的理由):
    實測三種寫法 —— `編號｜隊名｜姓名`(37 份)、`編號｜隊名｜選手`(5 份)、
    `參加編號｜項目｜隊名(單位)｜姓名`(2 份,2024 年的舊版產生器)。

    界線一律取**相鄰兩個表頭起點的中點**(同 bounds() 的理由):儲存格置中排版,
    姓名比表頭寬時會往右壓過下一欄的起點,用「下一欄起點」當界就會把右半頁的編號
    判進左半頁的姓名格,讀出「鄧長恩23-10」這種姓名黏編號的字(實測 lapgo-122)。
    """
    roles = [_lapgo_role(t) for _x, t in row]
    if not roles or None in roles:
        return []
    n = roles.index(roles[0], 1) if roles.count(roles[0]) > 1 else len(roles)
    if len(roles) % n or roles != roles[:n] * (len(roles) // n):
        return []
    if "no" not in roles[:n] or "name" not in roles[:n]:
        return []
    xs = [x for x, _t in row]
    edges = [(xs[i] + xs[i + 1]) / 2 for i in range(len(xs) - 1)] + [float("inf")]
    return [(e, i // n, roles[i]) for i, e in enumerate(edges)]


def is_lapgo(doc):
    for page in doc:
        for row in page_rows(page):
            if lapgo_header(row):
                return True
    return False


def parse_lapgo(doc):
    """LAPGO 選手名單 PDF → (declared, people, got)。

    people = [(組別, 單位, 姓名, 姓名原字串), ...];got = {組別: 抽到的席數}。

    三件非讀座標不可的事(全庫 42 份實測):

    1. **編號的組序是整場跨檔連號的**,不是每份檔從 1 開始 —— 同一場的「社會組」那份
       從 27 起跳(個人組那份用掉 1~26)。所以組序要**依出現順序**綁到標題,不能拿
       序號當索引。
    2. **雙打/團體的編號格是垂直置中的,自己獨佔一列**,隊名與姓名在它的上下列。
       只讀編號那一列的隊名,團體組的隊名會全部變成空字串(實測 lapgo-101 青年混合
       團體 14 隊全空)。隊名與姓名都要配給「同頁同欄塊裡 y 最近的編號」。
    3. 續頁不重印表頭也不重印標題(只有編號前綴能認組),所以表頭要跨頁沿用;
       但一頁若有表頭,表頭以上那幾列是頁首大標,落進姓名欄會冒出假選手,要擋掉。
    """
    titles, anchors, names, units = [], [], [], []
    order, bound = [], {}
    cols = []
    for pno, page in enumerate(doc):
        head_y = None
        for y, row in rows_with_y(page):
            texts = [t for _x, t in row]
            if any(_lapgo_role(t) == "no" for t in texts):
                head = lapgo_header(row)
                if head:
                    cols, head_y = head, y
                    continue
            m = _LAPGO_TITLE.match("".join(texts))
            if m and m.group(1).strip():
                titles.append((m.group(1).strip(), int(m.group(2))))
                order.append(("T", len(titles) - 1))
                continue
            if not cols or (head_y is not None and y < head_y):
                continue
            buckets = defaultdict(list)
            for x, t in row:
                for edge, bi, role in cols:
                    if x < edge:
                        buckets[(bi, role)].append(t)
                        break
            for bi in {b for b, _r in buckets}:
                seat = next((t for t in buckets.get((bi, "no"), [])
                             if _LAPGO_SEAT.match(t)), None)
                if seat:
                    g, seq = _LAPGO_SEAT.match(seat).groups()
                    if int(g) not in bound:
                        bound[int(g)] = None
                        order.append(("G", int(g)))
                    anchors.append((pno, bi, y, int(g), int(seq)))
                unit = cell_text(buckets.get((bi, "unit"), []))
                if unit and _lapgo_role(unit) != "unit":
                    units.append((pno, bi, y, unit))
                name = cell_text(buckets.get((bi, "name"), []))
                # 單字的姓名格一定是碎片,不是人:罕用字遇到字型 fallback 會被排到
                # 上一列自成一格(lapgo-128「施珵𧙗」的𧙗),隊名太長溢出到姓名欄也會
                # 留下一個字(lapgo-135「國立清華附小TOS校隊」的「校」)。收進去會
                # 生出查得到的假選手;丟掉最多只是少一個字,不會無中生有。
                if len(name) > 1 and _lapgo_role(name) != "name":
                    names.append((pno, bi, y, name))

    by_block = defaultdict(list)
    for a in anchors:
        by_block[(a[0], a[1])].append(a)

    def seat_of(pno, bi, y):
        cands = by_block.get((pno, bi))
        if not cands:
            return None
        a = min(cands, key=lambda a: abs(a[2] - y))
        return (a[3], a[4])

    seat_names, seat_unit = defaultdict(list), {}
    for pno, bi, y, name in names:
        s = seat_of(pno, bi, y)
        if s:
            seat_names[s].append(name)
    for pno, bi, y, unit in units:
        s = seat_of(pno, bi, y)
        if s:
            seat_unit.setdefault(s, unit)       # 同一席的隊名會重複,取第一個

    pend = []
    for kind, v in order:
        if kind == "T":
            pend.append(v)
        elif pend:
            bound[v] = pend.pop(0)

    declared, got, people = {}, defaultdict(int), []
    for g, ti in bound.items():
        if ti is not None:
            declared[titles[ti][0]] = declared.get(titles[ti][0], 0) + titles[ti][1]
    for (g, seq) in sorted({(a[3], a[4]) for a in anchors}):
        ti = bound.get(g)
        if ti is None:                          # 組序配不到標題 → 這組沒有分母可驗
            continue
        group = titles[ti][0]
        got[group] += 1
        members = seat_names.get((g, seq)) or []
        unit = seat_unit.get((g, seq), "")
        parts = [p.strip() for p in _LAPGO_UNIT_SPLIT.split(unit) if p.strip()]
        pair = parts if len(parts) == 2 and len(members) == 2 else None
        for i, raw in enumerate(members):
            name = clean_name(raw)
            if name:
                people.append((group, pair[i] if pair else unit, name, raw))
    return declared, people, dict(got)


def dropout_names(doc):
    """不成組名單 → {(組別, 姓名原字串), ...}。

    姓名格常常把單位黏在姓名後面(267404 的「陳俞安惠文高中」在 PDF 裡是一個
    text run),所以留原字串,比對時用前綴而不是等值。

    **實測目前一筆都扣不到,這是對的,不是失效** —— 報名結果 PDF 本來就已經是
    「不成組剔除後」的版本:267404 的不成組 8 個組別在報名結果裡一個都沒有
    (「公開甲組女單」沒開成,報名結果只剩女雙/男單/男雙)。留著這道是保險,
    萬一哪個主辦先發報名結果、後才公告不成組才用得到。

    比對一定要連組別一起看,不能只比姓名:那 8 個組別裡有 15 個人在**別的組**
    照常出賽(同一人報好幾項,只有一項沒開成),只比姓名會把他們整個人刪掉。
    """
    cols = find_header(doc)
    if not cols:
        return set()
    _declared, rows = parse_pdf(doc)
    return {(g, raw) for g, _u, _n, raw in rows_to_people(rows)}


def is_dropped(group, name, drop):
    for dg, dname in drop:
        if dg == group and (dname == name or dname.startswith(name)):
            return True
    return False


# ---- LAPGO 2024 舊版面(無欄名表頭,但有格線) ----
# 2024 年的選手名單長這樣,和 2025 起的版面差在**完全沒有欄名那一列**:
#
#     國小中年級男單【共74組】          ← 組別標題(表格外的頁首)
#     ┌──────┬────────────┬────────┬───────┬──────────────┬────────┐
#     │ 1-1  │ 竹市舊社國小 │ 戴廷緯 │ 1-41  │ 新竹市龍山國小 │ 趙子亮 │
#
# `parse_lapgo` 靠欄名判角色、沒有表頭就整份跳過 —— lapgo-27 捷豹盃 3 份名單、
# 685 人次因此一直讀不到,那場是「零選手可查」清單上的常客。
#
# 這個版面**不能用座標切欄**:長隊名(「快羽黑武士羽球隊」x0=102)的 word 會一路
# 延伸進姓名欄的 x 範圍(248),以 x 分群會把單位和姓名黏成一群(實測名單(2) 那群
# 跨度 152pt)。但它**有格線**(報名結果 PDF 沒有,那才要走座標),所以改用
# `find_tables()`:實測 3 份共 16 頁,每頁都是乾乾淨淨的 1 張表 × 6 欄。
_LAPGO24_BLOCK = 3            # 一個欄塊 = 編號 | 隊名 | 姓名


def _lapgo24_title(page, table):
    """頁首的組別標題(在表格上方),回 (組別, 宣告組數);續頁沒有標題則回 None。"""
    top = table.bbox[1] if table else 1e9
    texts = [w[4] for w in sorted(page.get_text("words"), key=lambda w: (w[1], w[0]))
             if w[3] <= top]
    m = _LAPGO_TITLE.match("".join(texts))
    return (m.group(1).strip(), int(m.group(2))) if m and m.group(1).strip() else None


def is_lapgo_2024(doc):
    """無表頭、有格線、頁首帶「【共N組】」、格子裡是 {組序}-{席次} 的 2024 版面。"""
    for page in doc:
        tables = page.find_tables().tables
        if not tables:
            continue
        rows = tables[0].extract()
        if not rows or len(rows[0]) % _LAPGO24_BLOCK:
            continue
        if not _lapgo24_title(page, tables[0]):
            continue
        if any(_LAPGO_SEAT.match((c or "").strip()) for r in rows[:6] for c in r):
            return True
    return False


def parse_lapgo_2024(doc):
    """LAPGO 2024 選手名單 → (declared, people, got)。people=[(組別,單位,姓名,原字串)]。

    三件事:
    (a) **續頁不重印標題**(名單(3) 第 2 頁第一列就是資料),組別沿用前一頁;
        編號的組序可以交叉驗證 —— 對不上就不沿用,寧可漏收也不要把人算進別組。
    (b) **雙打/團體只有首列有編號**,其後幾列編號留白、隊名每列重印、姓名逐列不同。
        空編號沿用同欄塊上一個編號,一個編號 = 一隊(= 標題「共N組」的那個「組」)。
    (c) **一頁可以有好幾個組別**,第二個之後的標題在**表格內部**、獨佔一列
        (`['40-49歲女雙【共6組】', None, None, ...]`)。只認頁首標題會把後面幾組
        全算進第一組(實測「40-49歲男雙」抽到 26 vs 宣告 7)。同成績總表
        「一個名次表頭底下接連好幾個組別」那個坑。
    (d) 守門的分母是「隊數」不是「人數」,所以 got 算不重複的編號數。
    """
    declared, people, got = {}, [], defaultdict(set)
    group = gseq = None
    for page in doc:
        tables = page.find_tables().tables
        if not tables:
            continue
        rows = tables[0].extract()
        if not rows or len(rows[0]) % _LAPGO24_BLOCK:
            continue
        title = _lapgo24_title(page, tables[0])
        if title:
            group, cnt = title
            gseq = None                       # 新組別,等第一個編號來定組序
            declared[group] = declared.get(group, 0) + cnt
        elif group is None:
            continue                          # 還沒見過任何標題,無從歸組
        last = {}
        for row in rows:
            cells = [(c or "").strip() for c in row]
            # 表格內的組別標題:獨佔一列(第一格是標題,其餘都空)
            if cells[0] and not any(cells[1:]):
                m = _LAPGO_TITLE.match(cells[0])
                if m and m.group(1).strip():
                    group = m.group(1).strip()
                    declared[group] = declared.get(group, 0) + int(m.group(2))
                    gseq, last = None, {}
                    continue
            for bi in range(len(cells) // _LAPGO24_BLOCK):
                seat, unit, name = cells[bi * _LAPGO24_BLOCK:(bi + 1) * _LAPGO24_BLOCK]
                m = _LAPGO_SEAT.match(seat)
                if m:
                    if gseq is None:
                        gseq = m.group(1)
                    elif not title and m.group(1) != gseq:
                        # 續頁的組序和上一頁對不上 → 這頁不是同一組,別亂歸
                        group = None
                        break
                    last[bi] = seat
                seat = last.get(bi)
                if not seat or not group:
                    continue
                got[group].add(seat)
                # 單字的姓名格一定是碎片不是人(見 parse_lapgo);角色欄(領隊/教練)也不是選手
                if len(name) > 1 and _lapgo_role(name) != "name" and not _lapgo_role(unit) == "name":
                    people.append((group, unit, name, name))
            if group is None:
                break
    return declared, people, {g: len(v) for g, v in got.items()}


def read_doc(doc):
    """一份 PDF → (declared, people, got),自動判別版面。讀不出來回 None。

    兩種版面的分辨方式都是表頭:mylivescore 的報名結果以「單號」開頭,
    LAPGO 的選手名單是「編號｜隊名｜姓名」成組重複。
    """
    if find_header(doc):
        declared, rows = parse_pdf(doc)
        got = defaultdict(int)
        for g, _cells, _cols in rows:
            got[g] += 1
        return declared, rows_to_people(rows), dict(got)
    if is_lapgo(doc):
        return parse_lapgo(doc)
    if is_lapgo_2024(doc):
        return parse_lapgo_2024(doc)
    return None


def build_entries(docs, drop, known, roster):
    """名單 PDF(可多份)→ (entries, coverage, 每組抽到/宣告, 扣掉的不成組筆數, 不收的理由)。

    LAPGO 會把同一場的名單拆成個人組/團體組好幾個檔,合起來才是完整的一場,
    所以宣告數與抽到數都跨檔累加後再一起過守門。
    """
    declared, people, got = {}, [], defaultdict(int)
    read = 0
    for doc in docs:
        out = read_doc(doc)
        if out is None:
            continue
        read += 1
        d, p, g = out
        for k, v in d.items():
            declared[k] = declared.get(k, 0) + v
        people.extend(p)
        for k, v in g.items():
            got[k] += v
    if not read:
        return [], None, {}, 0, "讀不到表頭"
    if not declared:
        return [], None, {}, 0, "讀不到組別宣告數,沒有可驗證的基準"

    entries, seen, dropped = [], set(), 0
    for g, unit, name, _raw in people:
        if is_dropped(g, name, drop):
            dropped += 1
            continue
        group = resolve_group(g, known, roster, set())[0]
        key = (group, unit, name)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"group": group, "unit": unit,
                        "members": [name], "source": "signup"})

    exp_total = sum(declared.values())
    got_total = sum(min(got.get(g, 0), n) for g, n in declared.items())
    coverage = round(got_total / exp_total, 3) if exp_total else None
    ratio = sum(got.values()) / exp_total if exp_total else 0
    per_group = {g: (got.get(g, 0), n) for g, n in sorted(declared.items())}

    if len(entries) < MIN_TOTAL:
        return [], None, per_group, dropped, f"只抽到 {len(entries)} 筆,視為版面讀不到"
    if not MIN_RATIO <= ratio <= MAX_RATIO:
        return [], None, per_group, dropped, (
            f"抽到 {sum(got.values())} 列 vs 宣告 {exp_total} 組(比例 {ratio:.2f})"
            f",超出守門區間,交還人工")
    return entries, coverage, per_group, dropped, None


def process(openid, apply=False, local=None):
    path = TOURN_DIR / f"{openid}.json"
    if not path.exists():
        return {"openid": openid, "status": "查無此賽事"}
    t = json.loads(path.read_text(encoding="utf-8"))
    existing = json.loads(path.read_text(encoding="utf-8"))
    if has_lapgo_draw(t):
        return {"openid": openid, "status": "已有抽籤結果名單,不讀 PDF"}

    wanted = signup_docs(t)
    if not local and not wanted:
        return {"openid": openid, "status": "無報名名單PDF"}
    title = "、".join(w[1] for w in wanted)[:60]

    docs = []
    try:
        if local:
            src = Path(local) if Path(local).is_absolute() else ROOT / local
            if not src.exists():
                return {"openid": openid, "status": f"找不到本地檔:{local}"}
            docs.append(fitz.open(src))
        else:
            for i, (url, _tt) in enumerate(wanted):
                docs.append(fitz.open(stream=fetch_pdf(openid, url, f"signup{i}"),
                                      filetype="pdf"))
    except Exception as exc:                                  # noqa: BLE001
        return {"openid": openid, "status": f"PDF 讀取失敗:{exc}"}

    drop = set()
    durl, _dtitle = doc_url(t, DROPOUT_KEYWORDS)
    if durl and not local:
        try:
            drop = dropout_names(
                fitz.open(stream=fetch_pdf(openid, durl, "dropout"), filetype="pdf"))
        except Exception as exc:                              # noqa: BLE001
            print(f"        [注意] {openid} 不成組名單讀取失敗,未扣除:{exc}")

    roster = match_roster(t)
    known = {g.get("name") for g in t.get("groups") or [] if g.get("name")}
    known |= set(roster)
    entries, coverage, per_group, dropped, why = build_entries(docs, drop, known, roster)

    res = {"openid": openid, "name": t.get("name"), "title": title,
           "status": "OK" if entries else "不收", "why": why,
           "rows": len(entries), "dropped": dropped,
           "groups": len({e["group"] for e in entries}),
           "coverage": coverage, "perGroup": per_group}

    if apply and entries:
        # 只換自己寫的那批。tsba/sportgov 的 source:"draw" 名單是另一條產線,
        # 整包覆蓋會把它們洗掉。
        kept = [e for e in t.get("entries") or [] if e.get("source") != "signup"]
        t["entries"] = kept + entries
        if not kept:
            t["entriesCoverage"] = coverage
        alive = {e["group"] for e in t["entries"]}
        have = {g.get("name") for g in t.get("groups") or []}
        groups = t.get("groups") or []
        for g in sorted(alive - have):
            groups.append({"id": "", "name": g, "tags": [], "drawUrl": None})
        t["groups"] = groups
        res["applied"] = write_if_changed(path, t, existing)
    return res


def has_lapgo_draw(t):
    """LAPGO 賽事已由 scrape_lapgo.draw_data 寫入抽籤結果名單。
    那份比選手名單 PDF 新(名單確認期更正後才抽籤),而且組名不同(API 組名 vs PDF 標題),
    再把 PDF 名單加回去會讓同一批人以兩種組名各登錄一次。tsba/sportgov 的 draw 名單
    是另一回事(可以與 signup 並存),所以只認 lapgo。"""
    return t.get("source") == "lapgo" and any(
        e.get("source") == "draw" for e in t.get("entries") or [])


def targets_all(force=False):
    """使用者定的那條線:抽籤完就要查得到人。

    目標 = 沒有比分、沒有名次、卻有報名結果 PDF 的賽事 —— 也就是「整場一位選手都
    查不到,但答案就掛在 documents 裡」。寫成條件而不是一份 openid 清單,之後每個月
    新到這個狀態的賽事會自己被接住,不必等人想到。比分上線後該場自然退出目標集合,
    但已寫進去的 entries 保留:那仍然是「誰報了名」的事實,讓報名了卻沒出賽的人
    繼續查得到。要補已有比分的賽事就加 --force(或用 --openid 指定)。
    """
    out = []
    for p in sorted(TOURN_DIR.glob("*.json")):
        t = json.loads(p.read_text(encoding="utf-8"))
        if not force and (t.get("matches") or t.get("standings")):
            continue
        if has_lapgo_draw(t):
            continue
        if signup_docs(t):
            out.append(p.stem)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--openid")
    ap.add_argument("--file", help="改讀本地報名結果 PDF,不下載官方連結")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="連已經有比分/名次的賽事也解析(補「報名了卻沒出賽」的人)")
    ap.add_argument("--json")
    args = ap.parse_args()

    targets = ([args.openid] if args.openid
               else (targets_all(args.force) if args.all else None))
    if not targets:
        ap.error("需要 --openid 或 --all")
    if args.file and len(targets) != 1:
        ap.error("--file 只能配 --openid 用")

    results, ok, rej, fail, rows, wrote = [], 0, 0, 0, 0, 0
    for i, oid in enumerate(targets, 1):
        r = process(oid, apply=args.apply, local=args.file)
        results.append(r)
        head = f"[{i}/{len(targets)}]"
        if r["status"] == "OK":
            ok += 1
            rows += r["rows"]
            wrote += 1 if r.get("applied") else 0
            cov = f"{r['coverage']:.0%}" if r["coverage"] is not None else "無宣告數"
            drop = f" 扣不成組 {r['dropped']}" if r["dropped"] else ""
            print(f"{head}  {oid:<12} {r['groups']:>3} 組 {r['rows']:>5} 人 "
                  f"覆蓋 {cov}{drop}  {(r.get('name') or '')[:24]}")
            for g, (a, b) in r["perGroup"].items():
                if a != b:
                    print(f"        [組數對不上] {g}:抽到 {a} vs 宣告 {b}")
        elif r["status"] == "不收":
            rej += 1
            print(f"{head}! {oid:<12} 不收:{r['why']}  {(r.get('name') or '')[:24]}")
        else:
            fail += 1
            print(f"{head}  {oid:<12} {r['status']}")
    print(f"\n完成:收錄 {ok} 場、共 {rows} 筆參賽紀錄;不收 {rej} 場;無法處理 {fail} 場")
    if args.apply:
        print(f"已寫入 {wrote} 場 → 接著跑 python scripts/rebuild_index.py")
    if args.json:
        Path(args.json).write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=list),
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
