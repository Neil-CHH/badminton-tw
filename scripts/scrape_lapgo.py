# -*- coding: utf-8 -*-
"""lapgo.com.tw 羽球賽事資料抓取。

用法:
    python scripts/scrape_lapgo.py             # 增量
    python scripts/scrape_lapgo.py --full      # 全量重抓
    python scripts/scrape_lapgo.py --only 122  # 只抓指定 cid(可逗號分隔)
    python scripts/scrape_lapgo.py --dry-run   # 不寫檔,只印出會做什麼
    python scripts/scrape_lapgo.py --no-index  # 不重建索引

API(免登入,只要帶從任一頁面抓到的 csrf-token + cookie):
  POST /getCompetitionByStatus  body status=all
       → {now, sign_up, coming_soon, finish, notyet},每筆含 id(=cid)/name/url/start_date/
         end_date/place/type。type=='羽球比賽' 才收。
  POST /web/getSessionScoreGrouped  body cid=  → {table:[...]} 逐場比分
  POST /eventinfo/getResultsSummary body cid=  → 官方成績總表(名次,可到第 5 名)
  POST /web/getWebContent           body id=   → 賽事自訂頁面(含最新消息清單)
  POST /getNewsContent              body id=   → 單篇公告內文(名單/籤表的連結在這裡)
  POST /web/getSessionGroup         body cid=  → 組別定義(id 即籤表的 sid)
  POST /web/getSessionMapData       body sid=  → 抽籤結果(籤位 → 「單位,姓名 姓名」)

注意:`show_livescore` 旗標不可靠(實測 20 場 show_livescore=0 的已結束賽事有 17 場仍回傳
完整比分),故比照 scrape.py 對 mylivescore 的作法:一律試抓,不看旗標。

比分正規化契約見 CLAUDE.md:必須輸出與 mylivescore schedule 相同的 14 個 key,
否則 rebuild_index 會靜默產生空的選手/單位統計。
"""
import difflib
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote

from fetch_docs import classify
from scrape import base_group, derive_category, derive_standings, parse_group_tags
from sources_common import (NON_BADMINTON, SRC_LAPGO, Http, blocked_openids,
                            city_from_text, find_csrf, loads_lenient,
                            merge_standings, write_if_changed)

ROOT = Path(__file__).resolve().parent.parent
TOURN_DIR = ROOT / "docs" / "data" / "tournaments"

BASE = "https://lapgo.com.tw"
LIST_PAGE = BASE + "/activity"
BADMINTON_TYPE = "羽球比賽"

# LAPGO 的 type 由主辦自填,實測 12 場約 13,000 筆共 15 種寫法。
# 已是專案代號的直接沿用,其餘轉換;未知值保留原樣並警告(不可靜默丟棄)。
MATCHTYPE_MAP = {
    "決賽": "R2", "冠軍賽": "R2", "季軍賽": "R34",
    "1/2決賽": "R4", "1/4決賽": "R8", "1/8決賽": "R16",
    "1/16決賽": "R32", "1/32決賽": "R64",
}
MATCHTYPE_OK = {"預賽", "R34", "F2", "F3", "F4"} | {f"R{n}" for n in
                                                   (2, 3, 4, 8, 16, 32, 64, 128, 256)}
# 不屬於賽制的場次:表演/交流性質,勝負不該計入選手戰績,整場不收錄
# (lapgo-153 律師盃邀請賽的 9 場團體「友誼賽」)。
MATCHTYPE_DROP = {"友誼賽", "表演賽", "熱身賽", "交流賽"}
HEAD_MAP = {"single": "單打", "double": "雙打", "group": "團體"}

RANK_WORD = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}
_RESULT_LINE = re.compile(r'<div class="result_line">(.*?)</div>', re.S)
_TAG = re.compile(r"<[^>]+>")

# LAPGO 的 name 是「組別+場次代號」,代號寫法各主辦不同(實測 (一)/(四四)、A1-A3、[9]),
# 真正的組別是 session_group_id。以同一 sgid 底下所有名稱的共同前綴回推組別名。
_TRAIL = re.compile(r"[\s\-–—_/()()\[\]【】]+$")
_SUFFIX = re.compile(r"[((\[【][〇零一二三四五六七八九十百千\dA-Za-z\-]+[))\]】]\s*$")
# 循環賽的分組配對代號(A1-A2),共同前綴會殘留池代號字母
_POOL_PAIR = re.compile(r"[A-Za-z]\d+\s*[-–]\s*[A-Za-z]?\d+\s*$")
_POOL_TAIL = re.compile(r"[A-Za-z]{1,2}\d*$")

# ---- 賽事公告(最新消息)----
# 賽後仍追公告的天數(同 fetch_docs.RECHECK_DAYS 的理由:主辦常在賽後補貼文件)
NEWS_RECHECK_DAYS = 60
# 公告內文裡「算是文件」的連結。官方名單一律掛 Google Drive,少數掛 lapgo 自己的
# storage;LINE/Facebook/賽事頁本身不是文件,不能收進 documents。
_DOC_LINK = re.compile(
    r"https?://(?:drive\.google\.com/file/d/[\w-]+"
    r"|docs\.google\.com/[^\s\"'<>]+"
    r"|lapgo\.com\.tw/storage/[^\s\"'<>]+"
    r"|[^\s\"'<>]+\.(?:pdf|xlsx|xls))", re.I)
_DRIVE_FILE = re.compile(r"^https?://drive\.google\.com/file/d/([\w-]+)", re.I)


class LapgoApi:
    def __init__(self):
        self.http = Http()
        self.token = find_csrf(self.http.get_text(LIST_PAGE))

    def post(self, path, data):
        raw = self.http.post_form(BASE + path, data, referer=LIST_PAGE,
                                  headers={"X-CSRF-TOKEN": self.token})
        return loads_lenient(raw)

    def competitions(self):
        """回傳 [(bucket, info), ...],只留羽球比賽。"""
        d = self.post("/getCompetitionByStatus", {"status": "all"})
        out = []
        for bucket, rows in (d or {}).items():
            for r in rows or []:
                # type 由主辦自選,實測有籃球/排球/樂樂棒球被標成「羽球比賽」,再用賽名濾一次
                if r.get("type") != BADMINTON_TYPE:
                    continue
                if NON_BADMINTON.search(r.get("name") or ""):
                    print(f"  [警告] 排除非羽球賽事: {r.get('name')}")
                    continue
                out.append((bucket, r))
        return out

    def scores(self, cid):
        d = self.post("/web/getSessionScoreGrouped", {"cid": cid})
        return (d or {}).get("table") or []

    def results_summary(self, cid):
        d = self.post("/eventinfo/getResultsSummary", {"cid": cid}) or {}
        if d.get("error"):
            return None
        return d

    def web_content(self, cid):
        """賽事自訂頁面。type=='news' 那筆帶 news[](id/title/updated_at)。"""
        d = self.post("/web/getWebContent", {"id": str(cid)})
        return d if isinstance(d, list) else []

    def session_groups(self, cid):
        """組別定義:id(= 籤表的 sid)、name、type(single/double/three/group)、has_preliminary。"""
        d = self.post("/web/getSessionGroup", {"cid": str(cid)})
        return d if isinstance(d, list) else []

    def session_map(self, sid):
        """單一組別的籤表。有預賽的在 teamData(numA1…),純淘汰的在 final_schedule_map(num1…)。"""
        d = self.post("/web/getSessionMapData", {"sid": str(sid)})
        return d if isinstance(d, dict) else {}

    def news_content(self, nid, referer):
        """單篇公告內文(HTML,整份是 URL-encode 過的)。"""
        raw = self.http.post_form(BASE + "/getNewsContent", {"id": str(nid)},
                                  referer=referer,
                                  headers={"X-CSRF-TOKEN": self.token})
        return unquote(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)


# ---------- 比分正規化 ----------

def _cmp_sides(a, b):
    """由雙方數值決定 (winner, abstain)。'棄' 代表棄權。"""
    a, b = str(a), str(b)
    if a == "棄" or b == "棄":
        if a == "棄" and b == "棄":
            return "", "1"
        return ("B", "1") if a == "棄" else ("A", "1")
    try:
        na, nb = float(a), float(b)
    except ValueError:
        return "", ""
    if na > nb:
        return "A", ""
    if nb > na:
        return "B", ""
    return "", ""


def _score(v):
    """未打的局 LAPGO 給 '--',mylivescore 給空字串;統一成空字串以免前端顯示兩套寫法。"""
    s = str(v if v is not None else "").strip()
    return "" if s in ("--", "-", "—", "None") else s


def _players(tp, idx):
    if len(tp) <= idx:
        return ""
    return "/".join(str(x) for x in (tp[idx].get("player") or []) if x)


def _team(tp, idx):
    if len(tp) <= idx:
        return ""
    return unquote(str(tp[idx].get("team") or ""))


def _load_tp(row):
    try:
        tp = json.loads(row.get("team_players") or "[]")
    except (TypeError, ValueError):
        return []
    return tp if isinstance(tp, list) else []


def _lcp(strings):
    lo, hi = min(strings), max(strings)
    i = 0
    while i < len(lo) and i < len(hi) and lo[i] == hi[i]:
        i += 1
    return lo[:i]


def _clean_group(name, pooled):
    """剝掉尾綴符號;pooled 表示該組用了 A1-A2 這類分組配對代號,需再剝掉殘留的池代號。"""
    name = _TRAIL.sub("", _SUFFIX.sub("", name)).strip()
    if pooled:
        stripped = _POOL_TAIL.sub("", name).strip()
        if len(stripped) >= 2:
            name = stripped
    return name


def canonical_group_names(table):
    """{session_group_id: 組別名}。取同組所有 name 的共同前綴再剝掉尾綴符號。

    單場成組時共同前綴就是完整名稱,改用尾綴 regex 剝除;剝過頭(剩不到 2 字)則退回最短名稱。
    """
    by_sg = {}
    for r in table:
        by_sg.setdefault(r.get("session_group_id"), []).append(
            base_group(str(r.get("name") or "")).strip())
    out = {}
    for sg, names in by_sg.items():
        uniq = sorted({n for n in names if n})
        if not uniq:
            out[sg] = ""
            continue
        pooled = any(_POOL_PAIR.search(n) for n in uniq)
        name = _clean_group(_lcp(uniq) if len(uniq) > 1 else uniq[0], pooled)
        out[sg] = name if len(name) >= 2 else _clean_group(min(uniq, key=len), pooled)
    return out


def align_groups(names, known):
    """把官方成績總表的組別名對回比分資料的組別名,回傳 {總表名: 比分名}。

    兩個端點的組別標法不一致(實測 'U10女單' vs 'U10歲組女單'、'專業校隊女單' vs
    '專業校隊組女單'、'不減當年混雙' vs '不減當年混雙(二人合計80歲以上)'),
    不對齊的話名次會掛在 groups[] 裡不存在的組別上。

    採一對一貪婪配對:先相似度、再前綴。若不強制一對一,'專業校隊組女單' 會因為前綴
    命中另一個真實存在的組別 '專業校隊' 而把四個組別併成一組(實測 cid=108)。
    """
    mapping = {n: n for n in names}
    if not known:
        return mapping
    taken = {n for n in names if n in known}
    todo = [n for n in names if n not in known]

    def assign(pairs):
        pairs.sort(key=lambda x: (-x[0], x[1], x[2]))
        for _, n, k in pairs:
            if n not in todo or k in taken:
                continue
            mapping[n] = k
            taken.add(k)
            todo.remove(n)

    # 1) 相似度(處理插字/改字,如 'U10女單' vs 'U10歲組女單')
    assign([(r, n, k) for n in list(todo) for k in known
            if (r := difflib.SequenceMatcher(None, n, k).ratio()) >= 0.7])
    # 2) 前綴(處理總表多帶括號說明,相似度會被長括號拉低)
    assign([(len(k), n, k) for n in list(todo) for k in known
            if n.startswith(k) or k.startswith(n)])
    return mapping


def normalize_matches(table, warn_unknown=None, dropped=None):
    """LAPGO 一列 = 一局(單雙打)或一點(團體);依 (session_group_id, session_num) 併成一場。"""
    gnames = canonical_group_names(table)
    grouped = {}
    for r in table:
        grouped.setdefault((r.get("session_group_id"), r.get("session_num")), []).append(r)

    out = []
    for rows in grouped.values():
        rows.sort(key=lambda r: r.get("point_index") or 0)
        first = rows[0]
        tp = _load_tp(first)
        if len(tp) < 2:
            continue

        raw_type = (first.get("type") or "").strip()
        if raw_type in MATCHTYPE_DROP:
            if dropped is not None:
                dropped.append(raw_type)
            continue
        mt = MATCHTYPE_MAP.get(raw_type, raw_type)
        if mt and mt not in MATCHTYPE_OK and warn_unknown is not None:
            warn_unknown.add(raw_type)

        point_count = first.get("point_count") or 1
        score = first.get("score") or ["", ""]
        psum = first.get("point_sum") or ["", ""]

        # point_count>=10 是團體賽彙總列(官方前端也只顯示第一列並改用 score 判勝負);
        # point_index==1 但實際只有一列時同樣視為單點,用 score 而非 point_sum。
        aggregated = point_count >= 10 or (len(rows) == 1 and point_count > 1)
        if aggregated:
            rows = rows[:1]
            side_a, side_b = _score(score[0]), _score(score[1])
        else:
            side_a, side_b = _score(psum[0]), _score(psum[1])
        winner, abstain = _cmp_sides(side_a, side_b)

        scoreinfo = []
        for r in rows:
            rtp = _load_tp(r) or tp
            sc = r.get("score") or ["", ""]
            scoreinfo.append({
                "round": str(r.get("point_index") or ""),
                "memberA": _players(rtp, 0),
                "memberB": _players(rtp, 1),
                "scoreA": _score(sc[0]),
                "scoreB": _score(sc[1]),
            })

        dt = str(first.get("start_datetime") or "")
        out.append({
            "groupName": gnames.get(first.get("session_group_id")) or str(first.get("name") or ""),
            "match": str(first.get("session_num") or ""),
            "date": dt[:10],
            "time": dt[11:16],
            "teamA": _team(tp, 0),
            "teamB": _team(tp, 1),
            "matchtype": mt,
            "stadium": "",
            "winner": winner,
            "Asidescore": side_a,
            "Bsidescore": side_b,
            "abstain": abstain,
            "HeadGroup": HEAD_MAP.get(first.get("group_type"), ""),
            "scoreinfo": scoreinfo,
        })

    out.sort(key=lambda m: (m["groupName"], m["date"], m["time"], m["match"]))
    return out


# ---------- 官方成績總表 → standings ----------

def _cell_entry(cell, is_team=False):
    """一格是 <div class="result_line">單位</div><div class="result_line">選手/選手</div>。

    偶爾只有一行(實測 118 格中 8 格):團體賽那行是隊名,個人賽那行是沒填單位的選手名。
    """
    lines = [_TAG.sub("", x).strip() for x in _RESULT_LINE.findall(cell or "")]
    lines = [x for x in lines if x]
    if not lines:
        return None
    if len(lines) == 1:
        return ({"unit": lines[0], "members": []} if is_team
                else {"unit": "", "members": [p.strip() for p in lines[0].split("/") if p.strip()]})
    members = []
    for nm in lines[1:]:
        members.extend(p.strip() for p in nm.split("/") if p.strip())
    return {"unit": lines[0], "members": members}


def parse_results_summary(data, known_groups=None, head_by_group=None):
    """回傳 standings(source=official)。田徑格式(columns/rows)不處理。

    known_groups 給比分資料的組別名,用來把總表的組別名對齊過去(見 align_groups)。
    head_by_group 給各組的單打/雙打/團體,用來判讀只有一行的格子(見 _cell_entry)。
    """
    if not data or data.get("is_athletics"):
        return []
    blocks = [(grp, b) for grp in (data.get("groups") or [])
              for b in (grp.get("blocks") or []) if b.get("row")]
    raw_names = []
    for _, b in blocks:
        n = _TAG.sub("", str(b["row"][0] or "")).strip()
        if n and n not in raw_names:
            raw_names.append(n)
    alias = align_groups(raw_names, set(known_groups or ()))

    out = []
    for _, block in blocks:
        headers = block.get("headers") or []
        row = block.get("row") or []
        group_name = _TAG.sub("", str(row[0] or "")).strip()
        if not group_name:
            continue
        group_name = alias.get(group_name, group_name)
        is_team = ((head_by_group or {}).get(group_name) == "團體"
                   or "團體" in group_name or group_name.endswith("團"))
        for i, head in enumerate(headers[1:], start=1):
            if i >= len(row):
                break
            m = re.search(r"第\s*([一二三四五六七八])\s*名", str(head))
            if not m:
                continue
            entry = _cell_entry(row[i], is_team)
            if not entry:
                continue
            out.append({
                "group": group_name,
                "rank": RANK_WORD[m.group(1)],
                "unit": entry["unit"],
                "members": entry["members"],
                "source": "official",
            })
    out.sort(key=lambda s: (s["group"], s["rank"]))
    return out


# ---------- 賽事記錄 ----------

def derive_status(info, today=None):
    """LAPGO 的 status 欄位全是 'normal',改由比賽日期判定。"""
    today = today or date.today().isoformat()
    start = (info.get("start_date") or "")[:10]
    end = (info.get("end_date") or "")[:10] or start
    if end and today > end:
        return "finished"
    if start and today >= start:
        return "ongoing"
    return "registering"


def _canon_link(url):
    """Google Drive 的分享連結會帶各式 ?usp= 尾巴,同一個檔在不同公告寫法不同。
    正規化成 /view,documents 才不會同一份名單重複兩筆、每月比對也才穩定。"""
    m = _DRIVE_FILE.match(url)
    return f"https://drive.google.com/file/d/{m.group(1)}/view" if m else url


def news_window(info, today=None):
    """這場賽事還會不會出公告:尚未結束,或結束未滿 NEWS_RECHECK_DAYS 天。

    **賽前公告正好落在舊增量條件會跳過的那段**:選手名單是「報名截止、還沒開打」時
    貼出來的,那段期間 status 不變、比分也還沒有,`need` 一路判 False。實測 lapgo-128
    大佛盃的 1,018 席名單公布了 6 天,月更完全沒碰到那場。賽後仍追 60 天,理由同
    fetch_docs 的 RECHECK_DAYS:主辦常在賽後補貼成績與完整名單。
    """
    today = today or date.today().isoformat()
    end = (info.get("end_date") or "")[:10] or (info.get("start_date") or "")[:10]
    if not end:
        return True
    return end >= (date.fromisoformat(today) - timedelta(days=NEWS_RECHECK_DAYS)).isoformat()


def news_documents(api, info):
    """賽事公告 → documents[]。

    LAPGO 的 API 沒有報名名單端點(2026-09 查證,見 CLAUDE.md),但主辦會把
    **選手名單／抽籤結果／賽程**貼成「最新消息」,檔案掛在 Google Drive。
    實測 62 場羽球賽事有 30 場貼了選手名單,而我們一直沒讀 —— 那些賽事在開打前
    一位選手都查不到,即使答案早就公開了(lapgo-128 大佛盃:39 組 1,018 席)。

    公告清單在 /web/getWebContent,但**清單只有標題與時間**,連結在內文裡,
    要逐篇打 /getNewsContent 才拿得到。一篇公告可以掛好幾個檔(個人組/團體組分開),
    所以 documents 是一對多,parse_entry_pdf 那邊要全部解析而不是只取第一個。
    """
    referer = (info.get("url") or LIST_PAGE) + "/news"
    out, seen = [], set()
    for page in api.web_content(info["id"]):
        if page.get("type") != "news":
            continue
        for n in page.get("news") or []:
            title = (n.get("title") or "").strip()
            try:
                html = api.news_content(n.get("id"), referer)
            except Exception as e:                            # noqa: BLE001
                print(f"  [提醒] lapgo-{info['id']} 公告 {n.get('id')} 讀取失敗:{e}")
                continue
            time.sleep(0.2)
            urls = list(dict.fromkeys(_canon_link(u) for u in _DOC_LINK.findall(html)))
            for i, u in enumerate(urls):
                if u in seen:
                    continue
                seen.add(u)
                out.append({
                    "title": title if len(urls) == 1 else f"{title}({i + 1})",
                    "url": u,
                    "date": (n.get("updated_at") or "")[:10],
                    "type": classify(title, u),
                    "source": "lapgo-news",
                })
    out.sort(key=lambda d: d.get("date") or "", reverse=True)
    return out


# ---- 抽籤結果(籤表)----
# 籤位鍵:有預賽的「numA1」= A 組第 1 位;純淘汰的「num12」= 第 12 籤位
_SEAT_KEY = re.compile(r"^num([A-Za-z]*)(\d+)$")
_CJK = re.compile(r"[㐀-鿿\U00020000-\U0003ffff]")
# 籤位數 ÷ 官方隊數低於這個比例就整場不收(抽到一半的籤不能蓋掉完整的 PDF 名單)
DRAW_MIN_COVERAGE = 0.9
# 還沒抽籤的賽事每組都回空的;連續這麼多組都空就不再往下問
DRAW_EMPTY_STOP = 5


def split_names(text):
    """「邱彥勛 盧品安」→ 兩人。外文姓名本身含空白(「uyen Dang Huy Nhat」),
    連續的非中文 token 要併回同一人,不能一律按空白拆。"""
    out = []
    for tok in text.split():
        if out and not _CJK.search(tok) and not _CJK.search(out[-1]):
            out[-1] += " " + tok
        else:
            out.append(tok)
    return out


def parse_seat(value, gtype):
    """籤位字串 → (unit, members);空籤位(輪空)回 None。
    個人/雙打/三人組:「單位,姓名 姓名」,沒填單位時只有姓名;團體組(group)只有隊名。"""
    v = (value or "").strip()
    if not v:
        return None
    if gtype == "group":
        return v, []
    unit, _, names = v.rpartition(",")
    names = names.strip()
    members = [names] if gtype == "single" else split_names(names)
    return unit.strip(), [m for m in members if m]


def _draw_entries(group, unit, members):
    """一個籤位 → entries。搭檔分屬兩校時單位寫成「甲校/乙校」,要拆成每人各自的單位,
    否則單位頁會多出「甲校/乙校」這種假單位(同 standings 的 memberUnits 問題)。
    團體組籤位只有隊名、沒有隊員,登錄不了任何選手,不產生 entry(籤表 draws 仍保留)。"""
    if not members:
        return []
    units = [u.strip() for u in re.split(r"[/／]", unit)] if unit else []
    if len(members) > 1 and len(units) == len(members):
        return [{"group": group, "unit": u, "members": [m], "source": "draw"}
                for u, m in zip(units, members)]
    return [{"group": group, "unit": unit, "members": members, "source": "draw"}]


def draw_data(api, info):
    """抽籤結果 → (entries, draws, coverage)。

    主辦在「最新消息」公告「抽籤結果出爐」時**不附檔**,只給賽事頁的 /score 連結 ——
    籤表是頁面用 /web/getSessionMapData 即時畫出來的,news_documents 永遠收不到
    (lapgo-128 大佛盃 2026-09-15 的公告就是這樣)。但這支 API 本身就公開,而且不必等
    主辦貼公告:只要抽了籤就拿得到,已結束的舊賽事(2024 lapgo-34)也照樣回傳。

    抽籤結果比選手名單 PDF 新(名單確認期更正後才抽),組名也跟之後的比分一致,
    所以拿到完整籤表時整份取代 PDF 名單。draws[] 另外保存分組:預賽是小組循環,
    同組互為對手,這是開打前唯一能給使用者的「對手」資訊(時間/場地要等賽程公告)。
    """
    entries, draws = [], []
    filled = declared = empty_run = 0
    for g in api.session_groups(info["id"]):
        gname = (g.get("name") or "").strip()
        gtype = g.get("type") or ""
        try:
            d = api.session_map(g["id"])
        except Exception as e:                                # noqa: BLE001
            # 一組失敗就整場不收:半套名單會蓋掉完整的 PDF 名單
            print(f"  [提醒] lapgo-{info['id']} 籤表 {gname} 讀取失敗,整場略過:{e}")
            return [], [], None
        time.sleep(0.15)
        declared += int(d.get("total_team_count") or 0)
        prelim = bool(d.get("has_preliminary"))
        # 有預賽的組,final_schedule_map 是預賽打完才排的決賽籤,開打前是 null 或晉級代號
        seat_map = (d.get("teamData") if prelim else d.get("final_schedule_map")) or {}
        pools = {}
        for key, val in seat_map.items():
            m = _SEAT_KEY.match(key)
            seat = parse_seat(val, gtype) if m else None
            if seat:
                pools.setdefault(m.group(1).upper(), []).append((int(m.group(2)), *seat))
        if not pools:
            empty_run += 1
            if not filled and empty_run >= DRAW_EMPTY_STOP:
                return [], [], None                           # 還沒抽籤
            continue
        empty_run = 0
        dr = {"group": gname, "type": gtype, "format": "pool" if prelim else "bracket",
              "pools": []}
        for pname in sorted(pools, key=lambda x: (len(x), x)):   # A…Z、AA…
            seats = sorted(pools[pname], key=lambda x: x[0])
            dr["pools"].append({"name": pname, "seats": [
                {"pos": pos, "unit": unit, "members": members} for pos, unit, members in seats]})
            for _, unit, members in seats:
                filled += 1
                entries.extend(_draw_entries(gname, unit, members))
        draws.append(dr)
    coverage = round(filled / declared, 3) if declared else None
    return entries, draws, coverage


def load_existing(openid):
    p = TOURN_DIR / f"{openid}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def build_record(api, info, existing, with_news=True):
    cid = info["id"]
    openid = f"lapgo-{cid}"
    name = info.get("name") or ""
    place = info.get("place") or ""
    img = info.get("index_img")
    record = {
        "openid": openid,
        "source": SRC_LAPGO,
        "name": name,
        "city": city_from_text(place) if city_from_text(place) != "其他"
                else city_from_text(name),
        "status": derive_status(info),
        "registerStart": (info.get("registration_start_date") or "")[:10],
        "registerEnd": (info.get("registration_end_date") or "")[:10],
        "dateStart": (info.get("start_date") or "")[:10],
        "dateEnd": (info.get("end_date") or "")[:10],
        "venue": place,
        "image": f"{BASE}/storage/competition{cid}/{img}" if img else "",
        "isSystem": bool(info.get("has_livescore")),
        "sourceUrl": info.get("url") or LIST_PAGE,
        "category": derive_category(name) or (existing or {}).get("category"),
        "promotion": (existing or {}).get("promotion"),
        "regulation": (existing or {}).get("regulation"),
        "groups": (existing or {}).get("groups", []),
        "matches": (existing or {}).get("matches", []),
        "standings": (existing or {}).get("standings", []),
        "resultPdf": (existing or {}).get("resultPdf"),
        "documents": (existing or {}).get("documents"),
        "lastUpdated": date.today().isoformat(),
    }
    # entries/entriesCoverage 是 parse_entry_pdf 解析名單寫進去的、API 沒有這兩個欄位。
    # 不從 existing 帶過去,每月重抓就會洗掉、下個月再解析一次,無限循環
    # (目標賽事多半還沒打完,每月都會重抓)。同 scrape.py 的既有規則。
    # draws 只在開打前抓(見下方),比分上線後也要帶著,不然籤表會在開打那個月消失。
    for k in ("entries", "entriesCoverage", "draws"):
        if (existing or {}).get(k) is not None:
            record[k] = existing[k]

    if with_news:
        docs = news_documents(api, info)
        if docs:                        # 抓空時保留既有的,免得一次讀取失敗就清光
            record["documents"] = docs

    unknown = set()
    dropped = []
    table = api.scores(cid)
    time.sleep(0.4)
    matches = normalize_matches(table, unknown, dropped) if table else []
    if unknown:
        print(f"  [警告] {openid} 未知 matchtype: {sorted(unknown)}")
    if dropped:
        kinds = ", ".join(sorted(set(dropped)))
        print(f"  [略過] {openid} 非賽制場次 {len(dropped)} 場({kinds})")

    if matches:
        record["matches"] = matches
        head_by_group = {}
        for m in matches:
            head_by_group.setdefault(base_group(m["groupName"]), m["HeadGroup"])
        record["groups"] = [
            {"id": "", "name": g, "tags": parse_group_tags(g, head_by_group.get(g, "")),
             "drawUrl": None}
            for g in sorted({base_group(m["groupName"]) for m in matches if m["groupName"]})
        ]

    # 抽籤結果:比分上線前唯一拿得到「誰在哪一組、跟誰同組」的地方(見 draw_data)。
    # 比分上線後就不再問 —— 那時選手已由比分登錄,籤表沿用開打前抓到的那份。
    # 不限公告期:已結束卻始終沒有比分的舊賽事(lapgo-27 捷豹盃、lapgo-32 花蓮市長盃,
    # 資料缺口清單上的常客)籤表 API 照樣回傳,這是它們唯一查得到選手的來源。
    # 沒抽籤的賽事連問 DRAW_EMPTY_STOP 組就停,每月成本很小。
    if not matches:
        d_entries, draws, cov = draw_data(api, info)
        if draws and cov is not None and cov < DRAW_MIN_COVERAGE:
            print(f"  [籤表] {openid} 籤位只有官方隊數的 {cov:.0%},疑似還沒抽完,不收")
        elif draws:
            record["draws"] = draws
            record["entries"] = d_entries
            record["entriesCoverage"] = cov
            score_url = f"{info['url']}/score" if info.get("url") else None
            record["groups"] = [
                {"id": "", "name": dr["group"],
                 "tags": parse_group_tags(dr["group"], HEAD_MAP.get(dr["type"], "")),
                 "drawUrl": score_url}
                for dr in draws
            ]

    # 官方成績總表優先於推導名次;總表沒涵蓋的組別再用 derive_standings 補
    incoming = []
    summary = api.results_summary(cid)
    time.sleep(0.4)
    if summary:
        known = {m["groupName"] for m in matches if m["groupName"]}
        heads = {m["groupName"]: m["HeadGroup"] for m in matches if m["groupName"]}
        incoming.extend(parse_results_summary(summary, known, heads))
    if matches:
        incoming.extend(derive_standings(matches))
    if incoming:
        record["standings"] = merge_standings(record["standings"], incoming)

    return record


def main():
    argv = sys.argv[1:]
    full = "--full" in argv
    dry = "--dry-run" in argv
    only = None
    if "--only" in argv:
        only = {x.strip() for x in argv[argv.index("--only") + 1].split(",") if x.strip()}

    TOURN_DIR.mkdir(parents=True, exist_ok=True)
    api = LapgoApi()
    comps = api.competitions()
    print(f"LAPGO 羽球賽事:{len(comps)} 場")

    blocked = blocked_openids()
    new_count = updated = unchanged = skipped = blocked_hits = 0
    for bucket, info in sorted(comps, key=lambda x: str(x[1].get("start_date") or "")):
        cid = info["id"]
        if only and str(cid) not in only:
            continue
        openid = f"lapgo-{cid}"
        # 已判定與 mylivescore 重複並刪檔的賽事,不再抓也不再建檔(見 dedupe.py)
        if openid in blocked:
            blocked_hits += 1
            continue
        existing = load_existing(openid)
        status = derive_status(info)
        fresh = news_window(info)
        need = (
            full or only or existing is None
            or existing.get("status") != status
            or status == "ongoing"
            or fresh                    # 還在出公告的賽事每月都要回頭看(見 news_window)
            or (status == "finished" and not existing.get("matches")
                and not existing.get("standings"))
        )
        if not need:
            skipped += 1
            continue
        try:
            record = build_record(api, info, existing,
                                  with_news=fresh or bool(full or only))
        except Exception as e:  # noqa: BLE001
            print(f"  [錯誤] {openid} {info.get('name')}: {e}")
            continue

        label = "新增" if existing is None else "更新"
        detail = f"{len(record['matches'])} 場比賽、{len(record['standings'])} 筆名次"
        if record.get("documents"):
            detail += f"、{len(record['documents'])} 筆公告文件"
        if record.get("draws"):
            detail += f"、籤表 {len(record['draws'])} 組 {len(record.get('entries') or [])} 人次"
        if dry:
            print(f"  [{label}(dry)] {openid} {record['name'][:30]} ({detail})")
            continue
        if not write_if_changed(TOURN_DIR / f"{openid}.json", record, existing):
            unchanged += 1
            continue
        if existing is None:
            new_count += 1
        else:
            updated += 1
        print(f"  [{label}] {openid} {record['name'][:30]} ({detail})")

    print(f"\nLAPGO 完成:新增 {new_count}、更新 {updated}、無變化 {unchanged}、略過 {skipped}"
          + (f"、重複已排除 {blocked_hits}" if blocked_hits else ""))
    if dry or "--no-index" in argv:
        return
    print("重建索引…")
    import rebuild_index  # noqa: PLC0415
    rebuild_index.main()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
