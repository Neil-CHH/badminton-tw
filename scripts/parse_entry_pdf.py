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

用法:
    python -X utf8 scripts/parse_entry_pdf.py --openid 264311            # 只看報告
    python -X utf8 scripts/parse_entry_pdf.py --openid 264311 --apply    # 寫入
    python -X utf8 scripts/parse_entry_pdf.py --all                      # 掃描報告
    python -X utf8 scripts/parse_entry_pdf.py --all --apply              # 套用
"""
import argparse
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
from sources_common import write_if_changed                          # noqa: E402

CACHE = ROOT / "inbox" / "entrypdf"          # inbox 已 gitignore
SIGNUP_KEYWORDS = ("報名結果",)
DROPOUT_KEYWORDS = ("不成組",)

# 同一列的 y 容差。不能用「四捨五入到固定格線」分列:704036 的組別標頭「35歲組男單」
# 在 y=62.5、右側的「共2組」在 y=63.0,固定格線會把同一列切成兩列,宣告組數就讀不到
# (實測 3 場的宣告數因此變成 0,而那是唯一的驗證基準)。
ROW_TOL = 3.0


_COUNT_RE = re.compile(r"^共(\d+)[組籤隊人]$")
_PLAYER_RE = re.compile(r"^選手(\d+)$")
_UNIT_RE = re.compile(r"^(隊名|縣市別)(\d*)$")
_ORDER_RE = re.compile(r"^\d{6,}$")          # 單號:六位以上的報名序號

# 抽到/宣告 落在這個區間外就只報告不寫檔。報名結果 PDF 自己宣告了每組的組數,
# 那是唯一的驗證基準;對不上就是版面沒讀對,寧可留空號讓 verify 報出來。
MIN_RATIO, MAX_RATIO = 0.9, 1.1
MIN_TOTAL = 10       # 全場抽到的筆數低於此值,視為這份檔沒有可用文字


def fetch_pdf(openid, url, kind):
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"{openid}-{kind}.pdf"
    if p.exists() and p.stat().st_size > 1024:
        return p.read_bytes()
    req = urllib.request.Request(urllib.parse.quote(url, safe=":/%?=&"))
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


def page_rows(page):
    """一頁的文字依 y 併成列 → [[(x0, text), ...], ...],列內依 x 遞增。"""
    words = sorted(page.get_text("words"), key=lambda w: (w[1], w[0]))
    rows, cur, top = [], [], None
    for x0, y0, _x1, _y1, txt, *_ in words:
        if top is None or y0 - top > ROW_TOL:
            if cur:
                rows.append(sorted(cur))
            cur, top = [], y0
        cur.append((x0, txt))
    if cur:
        rows.append(sorted(cur))
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
    """
    out = ""
    for x in parts:
        if x.isdigit():
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


def find_header(doc):
    for page in doc:
        for row in page_rows(page):
            texts = [t for _x, t in row]
            if "單號" in texts and ("組別" in texts or "項目" in texts):
                return list(row)
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
        for row in page_rows(page):
            texts = [t for _x, t in row]
            if "單號" in texts and ("組別" in texts or "項目" in texts):
                cols = list(row)
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


def build_entries(doc, drop, known, roster):
    """報名結果 PDF → (entries, coverage, 每組抽到/宣告, 扣掉的不成組筆數, 不收的理由)。"""
    cols = find_header(doc)
    if not cols:
        return [], None, {}, 0, "讀不到表頭"
    declared, rows = parse_pdf(doc)
    if not declared:
        return [], None, {}, 0, "讀不到組別宣告數,沒有可驗證的基準"

    got = defaultdict(int)
    for g, _cells, _cols in rows:
        got[g] += 1

    entries, seen, dropped = [], set(), 0
    for g, unit, name, _raw in rows_to_people(rows):
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

    url, title = doc_url(t, SIGNUP_KEYWORDS)
    if not local and not url:
        return {"openid": openid, "status": "無報名結果PDF"}

    try:
        if local:
            src = Path(local) if Path(local).is_absolute() else ROOT / local
            if not src.exists():
                return {"openid": openid, "status": f"找不到本地檔:{local}"}
            doc = fitz.open(src)
        else:
            doc = fitz.open(stream=fetch_pdf(openid, url, "signup"), filetype="pdf")
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
    entries, coverage, per_group, dropped, why = build_entries(doc, drop, known, roster)

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
        if doc_url(t, SIGNUP_KEYWORDS)[0]:
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
