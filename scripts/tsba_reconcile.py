# -*- coding: utf-8 -*-
"""把「成績圖片視覺解析出來的名次」用同屆賽程表的參賽名冊校對後寫入賽事 JSON。

tsbadminton 的成績只有 JPG,直接採信辨識結果風險高。但同屆賽程表 xlsx 內有全部
參賽者的真文字(單位+姓名),可以把「自由辨識」降級成「從已知名單挑一個」:

    完全命中          → 直接採用
    唯一近似解        → 自動更正成名冊寫法,原文留在 ocrRaw
    無命中/多個同分   → 不寫入,列進「需人工確認」

名冊會先依組別縮小候選範圍(U9男單只有 100 多人,而非全賽事 2000 多人),
這是精確度的主要來源。

用法:
    python scripts/tsba_reconcile.py --openid tsba-2024-會長盃 --input raw.json
    python scripts/tsba_reconcile.py --openid tsba-2024-會長盃 --input raw.json --apply

raw.json 由 /badminton-update 流程中視覺讀圖產生,格式:
    [{"group":"U9男子組單打","rank":1,"unit":"南屯國小","members":["莊以新"]}, ...]
"""
import argparse
import difflib
import json
import re
import sys
from pathlib import Path

from sources_common import merge_standings

ROOT = Path(__file__).resolve().parent.parent
TOURN_DIR = ROOT / "docs" / "data" / "tournaments"
INBOX = ROOT / "inbox" / "tsba"

# 成績圖與賽程表的組別寫法不同:「U9男子組單打」vs「U9男單」
_DROP = re.compile(r"[組\s()()]|歲|級|打$")
_PAIRS = ((("男子",), "男"), (("女子",), "女"), (("混合",), "混"),
          (("單打",), "單"), (("雙打",), "雙"), (("團體賽",), "團體"))
_PAREN = re.compile(r"\s*[（(][^）)]*[）)]\s*$")   # 名冊會加「(興隆)」之類的同名區別註記
# 姓名相似度 + 同單位加分。中文名字短,兩字名差一字相似度只有 0.5,
# 光看字面會漏掉(實測「涂皓」vs 官方「凃皓」);但同單位就足以確認。
UNIT_BONUS = 0.35
ACCEPT = 0.85
MARGIN = 0.08


def clean_name(name):
    """去掉籤表用來對齊日文姓名的全形空格(「串間　太政」),ASCII 空格保留(英文名有意義)。"""
    return re.sub(r"\s{2,}", " ", str(name or "").replace("　", "")).strip()


def norm_group(name):
    s = str(name or "")
    for olds, new in _PAIRS:
        for o in olds:
            s = s.replace(o, new)
    return _DROP.sub("", s).strip()


def _best(target, candidates, cutoff=0.6):
    """回傳唯一最佳解;並列或都太低則回 None。"""
    if not candidates:
        return None
    scored = sorted(((difflib.SequenceMatcher(None, target, c).ratio(), c)
                     for c in candidates), key=lambda x: (-x[0], x[1]))
    if scored[0][0] < cutoff:
        return None
    if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 1e-9:
        return None
    return scored[0][1]


def load_roster(openid):
    p = INBOX / openid / "roster.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for g, v in raw.items():
        pool = []
        for unit, name in v:
            nm = _PAREN.sub("", clean_name(name)).strip()
            if nm and (unit, nm) not in pool:
                pool.append((str(unit).strip(), nm))
        out[norm_group(g)] = pool
    return out


def scope(roster, group):
    """把 OCR 的組別對到名冊的組別,取該組候選。"""
    g = norm_group(group)
    if g in roster:
        return roster[g], g
    key = _best(g, list(roster), cutoff=0.75)
    return (roster.get(key, []), key) if key else ([], None)


def match_member(name, unit, pool):
    """在名冊中找這位選手。回傳 (姓名, 是否為更正) 或 (None, False)。"""
    scored = []
    for u, n in pool:
        s = difflib.SequenceMatcher(None, name, n).ratio()
        if u == unit:
            s += UNIT_BONUS
        scored.append((s, n))
    if not scored:
        return None, False
    scored.sort(key=lambda x: (-x[0], x[1]))
    best_score, best_name = scored[0]
    if best_score < ACCEPT:
        return None, False
    # 有並列的同分候選就不猜
    rival = next((s for s, n in scored[1:] if n != best_name), 0)
    if best_score - rival < MARGIN and best_name != name:
        return None, False
    return best_name, best_name != name


def reconcile_entry(unit, members, pool):
    """回傳 (unit, members, status, notes)。status: verified|corrected|unmatched。"""
    if not pool:
        return unit, members, "unverified", ["無名冊可校對"]
    names = {n for _, n in pool}
    by_name = {}
    for u, n in pool:
        by_name.setdefault(n, []).append(u)

    out, notes, status = [], [], "verified"
    for nm in members:
        nm = (nm or "").strip()
        if not nm:
            continue
        if nm in names:
            out.append(nm)
            continue
        hit, changed = match_member(nm, unit, pool)
        if hit:
            out.append(hit)
            if changed:
                notes.append(f"{nm}→{hit}")
                status = "corrected"
        else:
            out.append(nm)
            notes.append(f"{nm}?")
            status = "unmatched"

    # 單位:以名冊寫法為準(專案不對單位名做正規化,拼法一致直接影響聚合品質)
    cand = []
    for nm in out:
        cand.extend(by_name.get(nm, []))
    if cand:
        best_unit = max(set(cand), key=cand.count)
        if unit and best_unit != unit:
            notes.append(f"單位 {unit}→{best_unit}")
            if status == "verified":
                status = "corrected"
        unit = best_unit
    elif unit and unit not in {u for u, _ in pool}:
        hit = _best(unit, sorted({u for u, _ in pool}), cutoff=0.7)
        if hit:
            notes.append(f"單位 {unit}→{hit}")
            unit = hit
            if status == "verified":
                status = "corrected"
    return unit, out, status, notes


def reconcile(rows, roster):
    kept, review = [], []
    for r in rows:
        group = str(r.get("group") or "").strip()
        rank = r.get("rank")
        members = [m for m in (r.get("members") or []) if str(m).strip()]
        unit = str(r.get("unit") or "").strip()
        if not group or not rank:
            review.append({**r, "_why": "缺組別或名次"})
            continue
        pool, key = scope(roster, group)
        unit2, members2, status, notes = reconcile_entry(unit, members, pool)
        entry = {"group": group, "rank": int(rank), "unit": unit2,
                 "members": members2, "source": "ocr"}
        if status != "verified":
            entry["ocrRaw"] = {"unit": unit, "members": members}
        if status in ("unmatched", "unverified"):
            # 名冊查無此人多半是補報名/換人(籤表是賽前版本),不是辨識錯誤。
            # 直接丟掉會漏掉真實的獎牌紀錄,所以保留但標記,讓之後可以篩出來抽查。
            entry["ocrUnverified"] = True
            review.append({**entry, "_why": "、".join(notes) or "名冊無此組別",
                           "_pool": key})
        entry["_status"] = status
        entry["_notes"] = notes
        kept.append(entry)
    return kept, review


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--openid", required=True)
    ap.add_argument("--input", required=True, help="視覺解析產生的 raw JSON")
    ap.add_argument("--apply", action="store_true", help="寫入賽事 JSON")
    args = ap.parse_args()

    rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    roster = load_roster(args.openid)
    if not roster:
        print("[警告] 找不到名冊(inbox/tsba/{}/roster.json),".format(args.openid)
              + "所有名次會標為 unverified,務必人工確認")
    kept, review = reconcile(rows, roster)

    from collections import Counter
    stat = Counter(e["_status"] for e in kept)
    print(f"解析 {len(rows)} 筆 → 自動命中 {stat.get('verified', 0)}"
          f"、自動更正 {stat.get('corrected', 0)}"
          f"、名冊查無(仍收錄並標記 ocrUnverified) "
          f"{stat.get('unmatched', 0) + stat.get('unverified', 0)}")
    for e in kept:
        if e["_notes"] and e["_status"] == "corrected":
            print(f"  [更正] {e['group']} 第{e['rank']}名: {'、'.join(e['_notes'])}")
    unver = sorted({e["group"] for e in kept if e["_status"] == "unverified"})
    if unver:
        print(f"  [未校對] 名冊沒有這些組別(團體賽名單不在籤表內),照原文收錄: "
              f"{'、'.join(unver)}")
    for r in review:
        print(f"  [需確認] {r.get('group')} 第{r.get('rank')}名 "
              f"{r.get('unit')} {r.get('members')} — {r.get('_why')}")

    if not args.apply:
        print("\n(未加 --apply,沒有寫入)")
        return
    p = TOURN_DIR / f"{args.openid}.json"
    if not p.exists():
        sys.exit(f"找不到 {p}")
    t = json.loads(p.read_text(encoding="utf-8"))
    clean = [{k: v for k, v in e.items() if not k.startswith("_")} for e in kept]
    t["standings"] = merge_standings(t.get("standings", []), clean)
    known = {g["name"] for g in t.get("groups", [])}
    for g in sorted({e["group"] for e in clean} - known):
        t.setdefault("groups", []).append(
            {"id": "", "name": g, "tags": None, "drawUrl": None})
    p.write_text(json.dumps(t, ensure_ascii=False, separators=(",", ":")),
                 encoding="utf-8")
    print(f"\n已寫入 {p.name}:{len(clean)} 筆名次(source=ocr)")
    print("記得跑 python scripts/rebuild_index.py")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
