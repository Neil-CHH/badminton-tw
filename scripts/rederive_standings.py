# -*- coding: utf-8 -*-
"""用現有比分重跑 derive_standings,把推導名次更新到最新規則。

為什麼需要:抓取是增量的 —— 內容沒變就不寫檔,所以修好 derive_standings 之後,
既有賽事檔裡的舊推導名次不會自己更新,得跑 `--full` 全部重抓(慢又打擾來源)
或跑這支(純本機計算,不連網)。

只動 source=derived 的組別:merge_standings 同優先度時採用 incoming,
所以 pdf/official/ocr 的組別一律保留不動。

用法:
    python -X utf8 scripts/rederive_standings.py            # 只看差異
    python -X utf8 scripts/rederive_standings.py --apply    # 寫入
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOURN_DIR = ROOT / "docs" / "data" / "tournaments"
sys.path.insert(0, str(ROOT / "scripts"))
from scrape import derive_standings  # noqa: E402
from sources_common import merge_standings  # noqa: E402


def key(s):
    return (s.get("group", ""), s.get("rank", 0),
            s.get("unit", ""), tuple(s.get("members") or []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    changed, rows_before, rows_after = [], 0, 0
    for p in sorted(TOURN_DIR.glob("*.json")):
        t = json.loads(p.read_text(encoding="utf-8"))
        matches = t.get("matches") or []
        if not matches:
            continue
        old = t.get("standings") or []
        new = merge_standings(old, derive_standings(matches))
        if {key(s) for s in old} == {key(s) for s in new}:
            continue
        gained = {s.get("group") for s in new} - {s.get("group") for s in old}
        lost = {s.get("group") for s in old} - {s.get("group") for s in new}
        changed.append((p.stem, t.get("name", ""), len(old), len(new), gained, lost))
        rows_before += len(old)
        rows_after += len(new)
        if args.apply:
            t["standings"] = new
            p.write_text(json.dumps(t, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")

    print(f"{'openid':<14}{'名次筆數':>12}  賽事名")
    for oid, name, a, b, gained, lost in changed:
        note = ""
        if gained:
            note += f"  +組別 {sorted(gained)[:2]}"
        if lost:
            note += f"  -組別 {sorted(lost)[:2]}"
        print(f"{oid:<14}{a:>5} → {b:<5}  {name[:30]}{note}")
    print(f"\n{len(changed)} 場賽事的推導名次有變動({rows_before} → {rows_after} 筆)")
    if args.apply:
        print("已寫入 → 接著跑 python scripts/rebuild_index.py")
    else:
        print("(未寫檔;加 --apply 才會寫入)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
