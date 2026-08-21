# -*- coding: utf-8 -*-
"""跨來源重複賽事去重:同一場真實賽事被兩個平台各收一次時,只留一份。

用法:
    python scripts/dedupe.py --dry-run   # 只看判定結果,不動檔(建議先跑)
    python scripts/dedupe.py             # 刪除 shadow 檔並寫入 duplicates.json

為什麼需要:rebuild_index 以 openid 為唯一鍵逐檔累加,重複賽事會讓列表出現兩張卡、
選手/單位的參賽場次與勝負獲獎全部翻倍。

判定規則(四項全部成立才算重複):
  1. 來源不同 —— 同來源一律不自動處理。實測同來源有 130 組近似賽名候選,
     全部是誤判(大專資格賽分區、群岳盃分站、運動 i 臺灣各鄉鎮、小樹苗兩梯…),
     真重複則一組都沒有。這條是整套規則的安全基礎。
  2. 賽名正規化後相似度 >= NAME_RATIO
  3. 實際比賽日期區間有交集 —— 區間取自 matches[].date,沒有比分才退回 dateStart。
     不能只看 dateStart:357718 的 dateStart 是錯的(標 6/14、實際 12/27),
     照著配會配錯對象並漏掉真正的 lapgo-74。任一側算不出區間 → 判定為不重複。
  4. 組別集合 Jaccard >= GROUP_JACCARD

保留 SOURCE_PRIORITY 高的那份(mylivescore 最優先);刪除前還要通過支配性檢查
(dominates):shadow 不能有任何 canonical 缺少的東西,否則只警告不動檔。
"""
import json
import sys
from datetime import date
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path

from sources_common import (blocked_openids, best_standing_source, effective_dates,
                            group_keys, load_duplicates, match_date_range,
                            norm_tourn_name, save_duplicates, source_of, source_priority,
                            year_tokens)

ROOT = Path(__file__).resolve().parent.parent
TOURN_DIR = ROOT / "docs" / "data" / "tournaments"

NAME_RATIO = 0.90
GROUP_JACCARD = 0.85
# 斷路器:單次刪除上限。規則若因來源改版而退化,寧可整批中止也不要無聲掃掉整站。
BREAKER_MIN = 3
BREAKER_PCT = 0.03


def load_all():
    out = []
    for p in sorted(TOURN_DIR.glob("*.json")):
        t = json.loads(p.read_text(encoding="utf-8"))
        t["_path"] = p
        t["_src"] = source_of(t)
        t["_name"] = norm_tourn_name(t.get("name", ""))
        t["_years"] = year_tokens(t.get("name", ""))
        t["_groups"] = group_keys(t)
        t["_range"] = match_date_range(t) or _declared_range(t)
        t["_matches"] = len(t.get("matches") or [])
        out.append(t)
    return out


def _declared_range(t):
    ds, de = effective_dates(t)
    return (ds, de or ds) if ds else None


def _jaccard(a, b):
    return len(a & b) / len(a | b) if (a or b) else 0.0


def is_pair(a, b, cross_source_only=True):
    """回傳 (是否重複, 相似度, 組別 Jaccard)。"""
    if cross_source_only and a["_src"] == b["_src"]:
        return False, 0.0, 0.0
    ratio = 1.0 if (a["_name"] and a["_name"] == b["_name"]) else \
        SequenceMatcher(None, a["_name"], b["_name"]).ratio()
    if ratio < NAME_RATIO:
        return False, ratio, 0.0
    # 年份寫得清楚且不同 → 是不同屆,不可能是同一場
    if a["_years"] and b["_years"] and not (a["_years"] & b["_years"]):
        return False, ratio, 0.0
    ra, rb = a["_range"], b["_range"]
    if not ra or not rb or not (ra[0] <= rb[1] and rb[0] <= ra[1]):
        return False, ratio, 0.0
    gj = _jaccard(a["_groups"], b["_groups"])
    return gj >= GROUP_JACCARD, ratio, gj


def dominates(canonical, shadow):
    """canonical 是否完全涵蓋 shadow。回傳 (可安全刪除, 不能刪的理由)。

    刻意不比對名次「逐列」內容:canonical 有 pdf 名次而 shadow 只有 derived 時,
    兩邊逐列本來就不同(307030 的名次列 Jaccard 只有 0.78),
    那正是 canonical 較優的證據,拿來當門檻反而會擋掉正確判定。
    """
    if shadow["_matches"] > canonical["_matches"]:
        return False, f"shadow 場次較多({shadow['_matches']} > {canonical['_matches']})"
    extra = shadow["_groups"] - canonical["_groups"]
    if extra:
        return False, f"shadow 獨有組別 {sorted(extra)[:3]}"
    can_best = best_standing_source(canonical)
    for g, prio in best_standing_source(shadow).items():
        if prio > can_best.get(g, 0):
            return False, f"shadow 組別「{g}」的名次來源優先度較高"
    if (shadow.get("entries") or []) and not (canonical.get("entries") or []):
        return False, "shadow 有參賽名單而 canonical 沒有"
    return True, ""


def detect(tours):
    """回傳 (可刪除的 pair, 需人工確認的 pair)。"""
    deletable, review = [], []
    for a, b in combinations(tours, 2):
        ok, ratio, gj = is_pair(a, b)
        if not ok:
            continue
        canonical, shadow = (a, b) if source_priority(a) >= source_priority(b) else (b, a)
        safe, why = dominates(canonical, shadow)
        (deletable if safe else review).append((canonical, shadow, ratio, gj, why))
    return deletable, review


def same_source_candidates(tours):
    """同來源內符合全部條件的近似賽事,只報告不處理(verify_data 用)。

    同來源的重複幾乎都是誤判(分區賽、分站賽、同名不同梯),所以絕不自動刪;
    但真的出現同來源重複時仍該讓人看見,故保留這條報告線。
    """
    out = []
    for a, b in combinations(tours, 2):
        if a["_src"] != b["_src"]:
            continue
        ok, ratio, gj = is_pair(a, b, cross_source_only=False)
        if ok:
            out.append((a, b, ratio, gj))
    return out


def main():
    dry = "--dry-run" in sys.argv
    tours = load_all()
    known = blocked_openids()
    deletable, review = detect(tours)

    print(f"掃描 {len(tours)} 場賽事,已登錄重複 {len(known)} 組")

    if review:
        print(f"\n== 需人工確認({len(review)} 組,不會自動刪除)==")
        for can, sh, ratio, gj, why in review:
            print(f"  {can['openid']} ← {sh['openid']}  相似度 {ratio:.2f}/組別 {gj:.2f}"
                  f"\n      不刪的理由:{why}  ({can['name'][:34]})")

    if not deletable:
        print("\n沒有新的重複賽事。")
        return 0

    print(f"\n== 判定為重複({len(deletable)} 組)==")
    print("  保留 openid  來源         場次  ←  刪除 openid  場次   相似度 組別  賽名")
    for can, sh, ratio, gj, _ in sorted(deletable, key=lambda d: d[0]["openid"]):
        print(f"  {can['openid']:<12} {can['_src']:<12} {can['_matches']:>5}  ←  "
              f"{sh['openid']:<12} {sh['_matches']:>5}   {ratio:.2f}  {gj:.2f}  "
              f"{can['name'][:34]}")

    cap = max(BREAKER_MIN, int(len(tours) * BREAKER_PCT))
    if len(deletable) > cap:
        print(f"\n[錯誤] 單次要刪 {len(deletable)} 場,超過上限 {cap} 場 → 全部中止,未刪任何檔案。"
              "\n       請先確認判定規則是否因來源改版而失準(scripts/dedupe.py)。")
        return 1

    if dry:
        print(f"\n(dry-run)未刪檔。確認無誤後跑 python scripts/dedupe.py")
        return 0

    pairs = load_duplicates()
    today = date.today().isoformat()
    for can, sh, _, _, _ in deletable:
        # 已登錄的不重複寫入(檔案被 git checkout 救回時會再走到這裡)
        if sh["openid"] not in known:
            pairs.append({
                "canonical": can["openid"],
                "shadow": sh["openid"],
                "name": can.get("name", ""),
                "shadowUrl": sh.get("sourceUrl") or "",
                "detected": today,
            })
        sh["_path"].unlink(missing_ok=True)
    save_duplicates(pairs)
    print(f"\n已刪除 {len(deletable)} 個重複賽事檔,並登錄至 scripts/duplicates.json。")
    print("接著跑 python scripts/rebuild_index.py 重建索引。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
