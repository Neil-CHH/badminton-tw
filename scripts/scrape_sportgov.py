# -*- coding: utf-8 -*-
"""全國運動會 / 全國中等學校運動會 官方競賽資訊系統 → 逐場比分 + 官方名次。

為什麼需要這支:全運會/全中運「會內賽」在 mylivescore 只有一張賽事卡片,
`api:matches` 回空(scrape.py 抓不到 schedule),官方掛的「成績紀錄」PDF 又是
空間排版的籤表,parse_result_pdf 找不到成績總表 —— 這兩場在庫裡長年 0 組 0 場 0 名次。
但主辦(縣市政府)自己的競賽資訊系統把同一份資料以 HTML 表格公開,而且比 PDF 更全:
逐場比分、團體賽逐點明細、官方頒獎名單都有。

**這支不建新賽事**:兩場都已經以 mylivescore 的 openid 在庫裡,只是沒有內容。
新建會變成同一場賽事兩張卡片(dedupe 的老問題),所以一律「就地補進既有賽事檔」,
賽事的 `source` 維持 mylivescore,名次標 `official`,官方頁面寫進 documents。

兩屆用的是同一套 CMS(頁面路徑、欄位、參數名完全一樣),只有 host 不同。
新一屆就在 SITES 加一筆(host + openid + 項目對照)再跑一次。

端點(LID 是運動種類代碼,羽球 = 202,兩站相同):
  Module/Score/Instant_List.php?LID=202      場次清單 → FID/PID、日期、項目標題
  Module/Score/InstantScore.php?FID=&PID=    該場次逐場成績(單位+選手、勝隊、比數)
  Module/Score/Instant_ListDetail.php?SSID=  團體賽逐點明細(排點、選手、各局比數)
  Module/Score/Finals_Score.php?FID=         頒獎名單(名次+單位+選手)→ standings

  * PID 才是「項目」的穩定鍵(FID 是「項目 × 比賽日」);同一個 PID 的場次要合成一組。
  * 只有最後一天的 FID 才有 Finals_Score 連結,但 Finals_Score.php 對同項目其他
    FID 也回得出同一份名單 —— 仍只打有連結的那個,少一次請求也少一次誤判。

輸出必須對上 mylivescore 的 14 個 match key(見 CLAUDE.md 正規化契約),
其中兩個容易寫錯的:
  * 團體賽 Asidescore/Bsidescore = 該場「點數」(3:1),scoreinfo 一列 = 一點,
    scoreA/scoreB = 該點的局數(2:1)。rebuild_index 以 scoreA>scoreB 判該點勝方,
    若改放各局分數會讓一點灌水成三場勝負。
  * 個人賽 Asidescore/Bsidescore = 局數(2:1),scoreinfo 一列 = 一局。

用法:
    python -X utf8 scripts/scrape_sportgov.py --dry-run     # 只報告,不寫檔
    python -X utf8 scripts/scrape_sportgov.py --apply       # 寫入賽事檔
    python -X utf8 scripts/scrape_sportgov.py --openid 322738 --apply
"""
import argparse
import html
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOURN_DIR = ROOT / "docs" / "data" / "tournaments"
sys.path.insert(0, str(ROOT / "scripts"))
from scrape import parse_group_tags                      # noqa: E402
from sources_common import Http, merge_standings, write_if_changed   # noqa: E402

# 項目標題 → 組別名。刻意用「整串對照」而不是拆字規則:
# 對不上就報錯停下來,比默默寫進一個猜出來的組別名安全。
# 全中運沿用 570139(114 年會內賽,人工由官方 PDF 匯入)的簡寫,兩屆才對得起來。
SITES = {
    "322738": {
        "name": "中華民國114年全國運動會羽球會內賽",
        "host": "https://sport114.yunlin.gov.tw",
        "lid": "202",
        "events": {
            "羽球男子組團體賽": "男子團體",
            "羽球女子組團體賽": "女子團體",
            "羽球男子組單打賽": "男子單打",
            "羽球女子組單打賽": "女子單打",
            "羽球男子組雙打賽": "男子雙打",
            "羽球女子組雙打賽": "女子雙打",
            "羽球混合組混合雙打賽": "混合雙打",
        },
    },
    "732851": {
        "name": "115年全國中等學校運動會羽球賽會內賽",
        "host": "https://sport115.cyc.edu.tw",
        "lid": "202",
        "events": {
            "高男組羽球團體賽": "高男團",
            "高女組羽球團體賽": "高女團",
            "國男組羽球團體賽": "國男團",
            "國女組羽球團體賽": "國女團",
            "高男組羽球單打賽": "高男單",
            "高女組羽球單打賽": "高女單",
            "國男組羽球單打賽": "國男單",
            "國女組羽球單打賽": "國女單",
            "高男組羽球雙打賽": "高男雙",
            "高女組羽球雙打賽": "高女雙",
            "國男組羽球雙打賽": "國男雙",
            "國女組羽球雙打賽": "國女雙",
            "高中組羽球男女混合雙打賽": "高混雙",
            "國中組羽球男女混合雙打賽": "國混雙",
        },
    },
}

# 單位格偶爾會被主辦接上出賽狀態(「臺南市 請假」),不剝掉會多出一個假單位。
STATUS_WORDS = ("請假", "棄權", "退賽", "已退賽", "未到", "傷退")
# 備註欄的名次場註記 →  matchtype 代號(見 CLAUDE.md:R2=決賽、R34=三四名戰)
_PLACE_RE = re.compile(r"第\s*(\d+)\s*[.·、]\s*(\d+)\s*名")


def _cells(tr):
    """一列 <tr> → 各格文字;<br> 保留成換行(單位/選手/各局比數都靠它分行)。"""
    out = []
    for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", tr):
        c = re.sub(r"(?is)<br\s*/?>", "\n", c)
        c = html.unescape(re.sub(r"(?s)<[^>]+>", "", c))
        out.append("\n".join(" ".join(x.split()) for x in c.split("\n") if x.strip()))
    return out


def _rows(page_html, marker):
    """抓出含 marker 欄位的那張表,回傳 [各列的格子]。"""
    tab = next((t for t in re.findall(r"(?is)<table.*?</table>", page_html)
                if marker in t), None)
    return [_cells(tr) for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", tab or "")]


def _strip_status(unit):
    for w in STATUS_WORDS:
        if unit.endswith(" " + w) or unit.endswith(w) and len(unit) > len(w):
            return unit[: -len(w)].strip(), w
    return unit, ""


def parse_side(cell):
    """'臺南市 請假\\n1074 盧煒璿' → ('臺南市', ['盧煒璿'], '請假')。

    選手一律是「編號 姓名」;沒有編號的行只可能是單位(團體賽的全中運頁面只給校名)。
    姓名不做任何切分 —— 原住民姓名「寶昕．達古拉外」中間就有分隔號長相的字。
    """
    lines = [x for x in (cell or "").split("\n") if x.strip()]
    unit, status, names = "", "", []
    for ln in lines:
        m = re.match(r"^(\d{3,6})\s+(.+)$", ln)
        if m:
            names.append(m.group(2).strip())
        elif not unit:
            unit, status = _strip_status(ln.strip())
    return unit, names, status


def parse_games(cell):
    """'21｜21\\n比\\n09｜18' → 兩種讀法的 [(A局分, B局分), ...];分不出來回 ([], [])。

    正常版面是「一行 = 一方的各局分數」(上行甲方、下行乙方),兩站都一樣;
    局分本身有的用全形｜分隔、有的用空白。

    但實測有極少數列被主辦填成**轉置**的「一行 = 一局」(114 全運女單第 17 場、
    女雙第 6 場、115 全中運國男雙第 2 場,共 3/252 列):照正常版面讀會讀出
    「21:21」這種不可能的平局,而且和官方「成績」欄對不起來。兩種讀法都回,
    由呼叫端拿官方成績欄裁決 —— 不靠猜,對得上才換。
    """
    lines = [x for x in (cell or "").split("\n") if x.strip() and x.strip() != "比"]
    if len(lines) != 2:
        return [], []
    a, b = ([x for x in re.split(r"[｜|,、\s]+", ln.strip()) if x] for ln in lines)
    if len(a) != len(b):
        return [], []
    return list(zip(a, b)), ([tuple(a), tuple(b)] if len(a) == 2 else [])


def _wins(games):
    """[(A局分, B局分), ...] → (A 贏幾局/點, B 贏幾局/點)。"""
    return (sum(1 for x, y in games if x > y), sum(1 for x, y in games if y > x))


def _same_side(cell_a, cell_win):
    """勝隊格是不是 A 方 —— 比單位名 + 選手集合,兩者都要對上才算。"""
    ua, na, _ = parse_side(cell_a)
    uw, nw, _ = parse_side(cell_win)
    return ua == uw and (not nw or set(nw) <= set(na))


def fetch_sessions(http, host, lid):
    """場次清單 → [{fid, pid, title, month, day, finals}]。"""
    url = f"{host}/Module/Score/Instant_List.php?LID={lid}"
    page = http.get_text(url)
    out = []
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", page):
        if "Pic.php" not in tr:
            continue
        fid = re.search(r"Pic\.php\?FID=(\d+)", tr).group(1)
        pid = re.search(r"PID=(\d+)", tr)
        cells = [c for c in _cells(tr) if c]
        title = next((c for c in cells if "羽球" in c), "")
        md = re.search(r"(\d+)月(\d+)日", " ".join(cells))
        out.append({"fid": fid, "pid": pid.group(1) if pid else fid,
                    "title": re.sub(r"\(\d+日\)\s*$", "", title).strip(),
                    "month": int(md.group(1)) if md else 0,
                    "day": int(md.group(2)) if md else 0,
                    "finals": "Finals_Score" in tr})
    return out


def fetch_matches(http, host, sess, group, year, stadium, warn):
    """一個場次的所有比賽 → 14-key match dict(團體賽再去抓逐點明細)。"""
    url = f"{host}/Module/Score/InstantScore.php?FID={sess['fid']}&PID={sess['pid']}"
    page = http.get_text(url)
    rows = _rows(page, "勝隊")
    if not rows:
        warn(f"{group} {sess['fid']}:找不到成績表")
        return []
    is_team = "各局比數" not in " ".join(rows[0])
    date = f"{year:04d}-{sess['month']:02d}-{sess['day']:02d}"
    out = []
    for r in rows[1:]:
        if len(r) < 10:
            continue
        no, time_, cell_a, cell_b, cell_w, score = r[2], r[1], r[3], r[5], r[6], r[7]
        tail = r[8:]
        unit_a, names_a, st_a = parse_side(cell_a)
        unit_b, names_b, st_b = parse_side(cell_b)
        if not unit_a or not unit_b:
            warn(f"{group} 場次{no}:單位讀不出來 {cell_a!r} / {cell_b!r}")
            continue
        # 團體賽的尾巴是 [備註, 詳細成績],個人賽是 [各局比數, 備註]
        remark = tail[0] if is_team else tail[-1]
        games = "" if is_team else tail[0]
        winner = "A" if _same_side(cell_a, cell_w) else (
            "B" if _same_side(cell_b, cell_w) else "")
        if cell_w and not winner:
            warn(f"{group} 場次{no}:勝隊對不上任何一方 {cell_w!r}")

        # abstain 記的是「棄權方」。單位格上的狀態字偶爾掛在贏的那一方(114 全運
        # 男雙第 5 場,高雄市標了傷退卻 2:0 勝 —— 應該是後來才退的),把贏家寫成
        # 棄權會反過來誤導,所以只有輸的那一方才登錄。
        abstain = ""
        flag_side = "A" if st_a else ("B" if st_b else "")
        if flag_side and flag_side != winner:
            abstain = f"{flag_side}:{st_a or st_b}"
        m = {
            "groupName": group, "match": str(no), "date": date, "time": time_,
            "teamA": unit_a, "teamB": unit_b,
            "matchtype": "預賽", "stadium": stadium, "winner": winner,
            "Asidescore": "", "Bsidescore": "", "abstain": abstain,
            "HeadGroup": "團體" if is_team else ("雙打" if len(names_a) > 1
                                                 or len(names_b) > 1 else "單打"),
            "scoreinfo": [],
            "_sideA": (unit_a, tuple(names_a)),
            "_sideB": (unit_b, tuple(names_b)),
        }
        sa, sb = (score.split(":") + ["", ""])[:2] if ":" in score else ("", "")
        m["Asidescore"], m["Bsidescore"] = sa.strip(), sb.strip()
        if is_team:
            ssid = re.search(r"Instant_ListDetail\.php\?SSID=(\d+)",
                             _row_html(page, no) or "")
            if ssid:
                m["scoreinfo"] = fetch_rubbers(http, host, ssid.group(1),
                                               unit_a, group, no, warn)
                time.sleep(0.2)
            elif score:
                warn(f"{group} 場次{no}:團體賽沒有逐點明細連結")
            if m["scoreinfo"] and _wins([(x["scoreA"], x["scoreB"])
                                         for x in m["scoreinfo"]]) != (_num(sa),
                                                                       _num(sb)):
                warn(f"{group} 場次{no}:逐點勝負加總與場上點數 {score!r} 對不上")
        else:
            normal, swapped = parse_games(games)
            want = (_num(sa), _num(sb))
            picked = normal
            if normal and _wins(normal) != want:
                if swapped and _wins(swapped) == want:
                    picked = swapped     # 這一列被主辦填成轉置版面,見 parse_games
                else:
                    warn(f"{group} 場次{no}:局分 {games!r} 與成績 {score!r} 對不上,"
                         f"照官方原樣收錄")
            member_a, member_b = "/".join(names_a), "/".join(names_b)
            for i, (x, y) in enumerate(picked, start=1):
                m["scoreinfo"].append({"round": str(i), "memberA": member_a,
                                       "memberB": member_b, "scoreA": x, "scoreB": y})
            if not picked and (names_a or names_b):
                # 不戰而勝(W/O)一分未打,官方沒有局分。**還是要留一列 scoreinfo**:
                # 選手名只存在於 scoreinfo,整列略過會讓這兩位選手在這場憑空消失。
                # 棄權方沿用庫裡的既有寫法「棄」,勝方留空(真的不知道名目比分)。
                m["scoreinfo"].append(
                    {"round": "1", "memberA": member_a, "memberB": member_b,
                     "scoreA": "棄" if st_a else "", "scoreB": "棄" if st_b else ""})
                m["Asidescore"] = "棄" if st_a else m["Asidescore"]
                m["Bsidescore"] = "棄" if st_b else m["Bsidescore"]
            if not normal and score and score.upper() not in ("W/O", "WO"):
                warn(f"{group} 場次{no}:有成績 {score!r} 卻讀不出局分 {games!r}")
        _set_matchtype(m, remark, warn, group)
        out.append(m)
    return out


def _row_html(page, no):
    """回傳含「場次 = no」那一列的原始 HTML(要在裡面找 SSID)。"""
    for tr in re.findall(r"(?is)<tr[^>]*>.*?</tr>", page):
        c = _cells(tr)
        if len(c) >= 10 and c[2] == str(no):
            return tr
    return None


def _set_matchtype(m, remark, warn, group):
    """名次場由官方備註認定(「第1.2名」= 決賽、「第3.4名」= 三四名戰)。

    **不要自己推算輪次** —— 兩屆的賽制不一樣:全運會團體是兩組循環賽再交叉、
    全運會個人是 16 籤全程排名賽(輸的人也繼續打到定出 1~8 名)、全中運是乾淨的
    8 籤淘汰。同樣是「決賽的前一場」,前者是循環賽、後者是八強,沒有共通的推法。
    全中運那站的備註欄根本是空的,改由官方頒獎名單認(見 label_from_standings)。
    """
    mm = _PLACE_RE.search(remark or "")
    if not mm:
        return
    a, b = mm.group(1), mm.group(2)
    m["matchtype"] = "R2" if (a, b) == ("1", "2") else f"R{a}{b}"


def _side_key(entry, is_team):
    """比賽的一方 → 可比對的身份。個人賽同一個縣市可以派兩組人,只認單位會撞在一起
    (澎湖縣的王柏崴與劉韋奇在 114 全運男單同時打準決賽),所以個人賽要連姓名一起認。"""
    unit, names = entry
    return unit if is_team or not names else (unit, frozenset(names))


def label_from_standings(matches, standings, warn, group):
    """備註欄沒寫名次場的站台(全中運),改由官方頒獎名單認出決賽與三四名戰。

    判準:第 1、2 名兩方交手、而且那一場是**雙方各自的最後一場** —— 循環賽制下
    同兩隊可能碰兩次,加上「最後一場」才不會把預賽誤認成決賽。認不出來就不標。
    """
    is_team = any(m.get("HeadGroup") == "團體" for m in matches)
    last = {}
    for m in matches:
        for side in ("A", "B"):
            k = _side_key(m["_side" + side], is_team)
            last[k] = max(last.get(k, 0), _num(m["match"]))
    by_rank = {}
    for s in standings:
        by_rank.setdefault(s["rank"], []).append(s)
    for (r1, r2), code in (((1, 2), "R2"), ((3, 4), "R34")):
        pair = [by_rank.get(r1) or [], by_rank.get(r2) or []]
        if any(len(x) != 1 for x in pair):
            continue                      # 並列名次 → 沒有一場專屬的名次戰
        keys = {_side_key((s[0]["unit"], tuple(s[0]["members"])), is_team)
                for s in pair}
        if len(keys) != 2:
            continue
        cands = [m for m in matches
                 if {_side_key(m["_sideA"], is_team),
                     _side_key(m["_sideB"], is_team)} == keys
                 and all(last.get(k) == _num(m["match"]) for k in keys)]
        if len(cands) == 1 and cands[0]["matchtype"] == "預賽":
            cands[0]["matchtype"] = code
        elif len(cands) > 1:
            warn(f"{group}:{code} 有 {len(cands)} 場候選,不標")


def mark_semis(matches):
    """決賽雙方各自的上一場 = 準決賽(R4)。只補這一層,理由見 _set_matchtype。"""
    finals = [m for m in matches if m["matchtype"] == "R2"]
    if len(finals) != 1:
        return
    f = finals[0]
    is_team = any(m.get("HeadGroup") == "團體" for m in matches)
    order = sorted(matches, key=lambda m: _num(m["match"]))
    for side in ("A", "B"):
        k = _side_key(f["_side" + side], is_team)
        prev = [m for m in order if _num(m["match"]) < _num(f["match"])
                and k in (_side_key(m["_sideA"], is_team),
                          _side_key(m["_sideB"], is_team))]
        if prev and prev[-1]["matchtype"] == "預賽":
            prev[-1]["matchtype"] = "R4"


def _num(s):
    try:
        return int(re.sub(r"\D", "", s or "") or 0)
    except ValueError:
        return 0


def fetch_rubbers(http, host, ssid, unit_a, group, no, warn):
    """團體賽逐點明細 → scoreinfo。一列 = 一點,scoreA/scoreB 放該點的局數。"""
    page = http.get_text(f"{host}/Module/Score/Instant_ListDetail.php?SSID={ssid}")
    rows = _rows(page, "排點")
    flip = False
    for r in rows:                       # 「甲隊 對 乙隊」那一列決定明細頁的左右
        if len(r) == 1 and " 對 " in r[0]:
            flip = r[0].split(" 對 ")[0].strip() != unit_a
            break
    out = []
    for r in rows:
        if len(r) < 9 or not re.fullmatch(r"\d+", r[0] or ""):
            continue
        point, names_a, names_b, score = r[1], r[3], r[5], r[6]
        if ":" not in score:
            continue                      # 已分出勝負就不打的點,官方留空
        ga, gb = (x.strip() for x in score.split(":", 1))
        _u, na, _s = parse_side("\n".join(["", *names_a.split("\n")]))
        _u, nb, _s = parse_side("\n".join(["", *names_b.split("\n")]))
        if not na or not nb:
            warn(f"{group} 場次{no} 第{point}點:選手讀不出來 {names_a!r}/{names_b!r}")
            continue
        a, b = ("/".join(na), "/".join(nb))
        if flip:
            a, b, ga, gb = b, a, gb, ga
        out.append({"round": str(point), "memberA": a, "memberB": b,
                    "scoreA": ga, "scoreB": gb})
    return out


def fetch_standings(http, host, fid, group, warn):
    """頒獎名單 → [{group, rank, unit, members, source:'official'}]。"""
    page = http.get_text(f"{host}/Module/Score/Finals_Score.php?FID={fid}")
    rows = _rows(page, "名次")
    out = []
    for r in rows:
        if len(r) < 3 or not re.fullmatch(r"\d+", (r[0] or "").strip()):
            continue
        unit, names, _st = parse_side("\n".join([r[1], *r[2].split("\n")]))
        if not unit:
            continue
        out.append({"group": group, "rank": int(r[0]), "unit": unit,
                    "members": names, "source": "official"})
    if not out:
        warn(f"{group}:頒獎名單讀不出任何一列")
    return out


def process(openid, apply=False):
    cfg = SITES[openid]
    path = TOURN_DIR / f"{openid}.json"
    t = json.loads(path.read_text(encoding="utf-8"))
    before = json.loads(path.read_text(encoding="utf-8"))
    year = int((t.get("dateStart") or "")[:4] or 0)
    stadium = t.get("venue") or ""
    warnings = []
    warn = warnings.append

    http = Http()
    sessions = fetch_sessions(http, cfg["host"], cfg["lid"])
    unknown = sorted({s["title"] for s in sessions if s["title"] not in cfg["events"]})
    if unknown:
        raise SystemExit(f"{openid}:未知項目 {unknown} —— 請先補進 SITES.events")

    matches, standings, by_group = [], [], {}
    for s in sorted(sessions, key=lambda s: (s["pid"], s["month"], s["day"])):
        group = cfg["events"][s["title"]]
        ms = fetch_matches(http, cfg["host"], s, group, year, stadium, warn)
        by_group.setdefault(group, []).extend(ms)
        time.sleep(0.3)
        if s["finals"]:
            standings.extend(fetch_standings(http, cfg["host"], s["fid"], group, warn))
            time.sleep(0.3)
    st_by_group = {}
    for s in standings:
        st_by_group.setdefault(s["group"], []).append(s)
    for group, ms in by_group.items():
        label_from_standings(ms, st_by_group.get(group, []), warn, group)
        mark_semis(ms)
        matches.extend(ms)
    for m in matches:
        m.pop("_sideA", None)
        m.pop("_sideB", None)
    matches.sort(key=lambda m: (m["groupName"], _num(m["match"])))

    # 日期落在賽期外 = 年份或月份讀錯,寧可報出來也不要靜靜寫進去
    for m in matches:
        if not (t.get("dateStart", "") <= m["date"] <= t.get("dateEnd", "9999")):
            warn(f"{m['groupName']} 場次{m['match']}:日期 {m['date']} 不在賽期內")
            break

    groups = [{"id": "", "name": g,
               "tags": parse_group_tags(g, next((m["HeadGroup"] for m in ms
                                                 if m.get("HeadGroup")), "")),
               "drawUrl": None}
              for g, ms in sorted(by_group.items())]
    res = {"openid": openid, "name": cfg["name"], "groups": len(groups),
           "matches": len(matches), "standings": len(standings),
           "players": len({n for m in matches for si in m["scoreinfo"]
                           for n in (si["memberA"] + "/" + si["memberB"]).split("/") if n}),
           "warnings": warnings}

    if apply and matches:
        t["groups"] = groups
        t["matches"] = matches
        t["standings"] = merge_standings(t.get("standings") or [], standings)
        _add_doc(t, cfg["host"], cfg["lid"], cfg["name"])
        res["written"] = write_if_changed(path, t, before)
    return res


def _add_doc(t, host, lid, name):
    """把官方賽程成績頁記進 documents(專案只記連結、不下載)。"""
    url = f"{host}/Module/Score/Instant_List.php?LID={lid}"
    docs = t.setdefault("documents", [])
    same = [d for d in docs if d.get("url") == url]
    if same:
        for d in same:
            d["source"] = "sportgov"     # 舊版寫進去的沒有標記,補上才不會被 fetch_docs 洗掉
        return
    # source 標記讓 fetch_docs 知道這筆不是 links.php 來的,重抓時不要洗掉
    docs.append({"title": f"{name}-官方賽程成績(逐場比分與頒獎名單)",
                 "url": url, "date": t.get("dateEnd") or "", "type": "成績",
                 "source": "sportgov"})


# ---------- 資格賽:只有籤表 PDF,收參賽名單 ----------
# 資格賽的成績不進競賽資訊系統(那套只跑會內賽),主辦把各項目的籤表 PDF 掛在賽事
# 消息頁的附件。籤表**不發名次** —— 官方「成績總表」列的是晉級名單而不是 1~N 名,
# 所以這裡只收 entries[](登錄出賽事實),不產生 standings,也不硬湊逐場比分:
# 籤表的比分要靠版面座標把「勝方節點」接回兩個來源席位,錯一個就是假戰績。
QUALIFIERS = {
    "255139": {
        "name": "中華民國114年全國運動會羽球資格賽",
        "host": "https://sport114.yunlin.gov.tw",
        "news": "/Module/CompNews/Detail.php?ID=239",
        "groups": ["男子團體", "女子團體", "男子單打", "女子單打",
                   "男子雙打", "女子雙打", "混合雙打"],
    },
}

_CITY_RE = "|".join(
    ["臺北市", "新北市", "基隆市", "桃園市", "新竹縣", "新竹市", "苗栗縣", "臺中市",
     "彰化縣", "南投縣", "雲林縣", "嘉義縣", "嘉義市", "臺南市", "高雄市", "屏東縣",
     "宜蘭縣", "花蓮縣", "臺東縣", "澎湖縣", "金門縣", "連江縣"])
_SEAT_RE = re.compile(rf"^(\d+)\s+({_CITY_RE})$")
_DECLARED_RE = re.compile(r"共\s*(\d+)\s*[人組隊]")


def _pdf_lines(data):
    """PDF → [(頁碼, 一行文字)];籤表是空間排版,但同一格的字仍在同一行。"""
    import fitz
    out = []
    for pi, page in enumerate(fitz.open(stream=data, filetype="pdf")):
        for ln in page.get_text().split("\n"):
            ln = " ".join(ln.split())
            if ln:
                out.append((pi, ln))
    return out


def extract_seats(data, pairs):
    """籤表 PDF → ([(單位, [選手,...]), ...], 標題宣告的席位數)。

    籤表把每個席位寫成「序號 縣市」+ 下一行姓名(姓名可能帶 [1]、[5/8] 種子註記)。
    雙打的搭檔寫在**席位行之前**的「縣市 / 姓名」兩行,中間常插進賽程節點文字
    (`#33`、`李/李勝出者進入會內賽`),所以往回找「同一頁、與席位同名的裸縣市行」,
    而不是固定往回數幾行。抽出的席位數要對得上標題宣告的人數/組數才算數。
    """
    lines = _pdf_lines(data)
    declared = next((int(m.group(1)) for _p, l in lines
                     for m in [_DECLARED_RE.search(l)] if m), 0)
    seats, used = [], set()
    for i, (pg, l) in enumerate(lines):
        m = _SEAT_RE.match(l)
        if not m or i + 1 >= len(lines):
            continue
        city = m.group(2)
        names = [_clean_name(lines[i + 1][1])]
        if pairs:
            j = next((k for k in range(i - 1, -1, -1)
                      if lines[k][0] == pg and lines[k][1] == city), None)
            if j is not None and j + 1 < i and j + 1 not in used:
                used.add(j + 1)
                names.insert(0, _clean_name(lines[j + 1][1]))
        seats.append((city, [n for n in names if n]))
    return seats, declared


def _clean_name(s):
    return re.sub(r"\[[^\]]*\]", "", s or "").strip()


def process_qualifier(openid, apply=False):
    cfg = QUALIFIERS[openid]
    path = TOURN_DIR / f"{openid}.json"
    t = json.loads(path.read_text(encoding="utf-8"))
    before = json.loads(path.read_text(encoding="utf-8"))
    warnings = []
    http = Http()
    news_url = cfg["host"] + cfg["news"]
    page = http.get_text(news_url)

    attach = {}
    for m in re.finditer(r'(?is)<a[^>]+href="([^"]*downloadfile\.php[^"]*)"[^>]*>(.*?)</a>',
                         page):
        title = html.unescape(re.sub(r"(?s)<[^>]+>", "", m.group(2))).strip()
        url = re.sub(r"^(\.\./)+", cfg["host"] + "/", m.group(1))
        g = next((x for x in cfg["groups"] if x in title), None)
        if g:
            attach.setdefault(g, url)

    entries, found, declared_total = [], 0, 0
    for g in cfg["groups"]:
        if g not in attach:
            warnings.append(f"{g}:賽事消息頁找不到對應附件")
            continue
        if "團體" in g:
            continue          # 團體籤表只有縣市名、沒有隊員名單,收了也查不到人
        seats, declared = extract_seats(http.get(attach[g], referer=news_url),
                                        pairs="雙" in g)
        if declared and len(seats) != declared:
            warnings.append(f"{g}:抽到 {len(seats)} 席、籤表標題宣告 {declared},不收")
            continue
        found += len(seats)
        declared_total += declared or len(seats)
        entries += [{"group": g, "unit": u, "members": n, "source": "draw"}
                    for u, n in seats if n]

    res = {"openid": openid, "name": cfg["name"], "groups": len(cfg["groups"]),
           "matches": 0, "standings": 0, "entries": len(entries),
           "players": len({n for e in entries for n in e["members"]}),
           "warnings": warnings}
    if apply and entries:
        t["groups"] = [{"id": "", "name": g, "tags": parse_group_tags(g, ""),
                        "drawUrl": None} for g in cfg["groups"]]
        t["entries"] = entries
        t["entriesCoverage"] = round(found / declared_total, 4) if declared_total else None
        docs = t.setdefault("documents", [])
        for d in docs:
            if d.get("url") == news_url:
                d["source"] = "sportgov"
        if not any(d.get("url") == news_url for d in docs):
            docs.append({"title": f"{cfg['name']}-各項目籤表與成績(官方賽事消息)",
                         "url": news_url, "date": t.get("dateEnd") or "",
                         "type": "成績", "source": "sportgov"})
        res["written"] = write_if_changed(path, t, before)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--openid", choices=sorted(set(SITES) | set(QUALIFIERS)))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    todo = ([args.openid] if args.openid
            else sorted(SITES) + sorted(QUALIFIERS))
    for oid in todo:
        run = process_qualifier if oid in QUALIFIERS else process
        r = run(oid, apply=args.apply and not args.dry_run)
        print(f"{oid} {r['name']}")
        print(f"  {r['groups']} 組・{r['matches']} 場比分・{r['standings']} 筆名次"
              + (f"・{r['entries']} 筆參賽名單" if "entries" in r else "")
              + f"・{r['players']} 位選手"
              + ("  → 已寫入" if r.get("written") else
                 ("  → 內容未變" if "written" in r else "  (未寫檔)")))
        for w in r["warnings"]:
            print(f"  [注意] {w}")
    if args.apply and not args.dry_run:
        print("\n接著跑 python scripts/rebuild_index.py")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
