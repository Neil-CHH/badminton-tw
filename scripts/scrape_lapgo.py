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
from datetime import date
from pathlib import Path
from urllib.parse import unquote

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


def load_existing(openid):
    p = TOURN_DIR / f"{openid}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def build_record(api, info, existing):
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
        need = (
            full or only or existing is None
            or existing.get("status") != status
            or status == "ongoing"
            or (status == "finished" and not existing.get("matches")
                and not existing.get("standings"))
        )
        if not need:
            skipped += 1
            continue
        try:
            record = build_record(api, info, existing)
        except Exception as e:  # noqa: BLE001
            print(f"  [錯誤] {openid} {info.get('name')}: {e}")
            continue

        label = "新增" if existing is None else "更新"
        detail = f"{len(record['matches'])} 場比賽、{len(record['standings'])} 筆名次"
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
