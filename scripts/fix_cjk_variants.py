"""把賽事檔裡「看起來是中文、其實不是中文」的字還原成一般漢字。

主辦打字時輸入法選到康熙部首(⾧⾺⽟⽻⼩)或 CJK 筆畫(㇐㇠),使用者用正常字就
搜不到那個人 —— 這是「查得到人」這條準則底下的純損失。規則在
`sources_common.normalize_cjk`,爬蟲寫入時也套同一支,所以這支只需要在
發現既有資料有問題時跑一次(`--apply` 才寫檔)。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources_common import normalize_cjk          # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOURN_DIR = ROOT / "docs" / "data" / "tournaments"
BAD = re.compile(r"[\u2e80-\u2fdf\u31c0-\u31ef]")


def walk(obj, stats, path=""):
    if isinstance(obj, str):
        fixed = normalize_cjk(obj)
        if fixed != obj:
            stats.append((path, obj, fixed))
        return fixed
    if isinstance(obj, dict):
        return {k: walk(v, stats, f"{path}.{k}" if path else k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [walk(v, stats, path + "[]") for v in obj]
    return obj


def main():
    apply = "--apply" in sys.argv[1:]
    total = files = 0
    for path in sorted(TOURN_DIR.glob("*.json")):
        raw = path.read_text(encoding="utf-8")
        if not BAD.search(raw):
            continue
        stats = []
        fixed = walk(json.loads(raw), stats)
        if not stats:
            continue
        files += 1
        total += len(stats)
        print(f"{path.stem}  {len(stats)} 處")
        for p, before, after in stats[:4]:
            print(f"    [{p}] {before!r} → {after!r}")
        if len(stats) > 4:
            print(f"    …另外 {len(stats) - 4} 處")
        if apply:
            path.write_text(json.dumps(fixed, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    print(f"\n{files} 場、共 {total} 處" + ("  → 已寫入" if apply else "  (未寫檔,加 --apply)"))
    if apply and files:
        print("接著跑 python scripts/rebuild_index.py")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
