# -*- coding: utf-8 -*-
"""docs/data 健檢:只讀本地檔、不打 API,可隨時重跑。每月更新與 PDF 匯入後都該跑。

用法:
    python scripts/verify_data.py                          # 健檢
    python scripts/verify_data.py --summary                # 加印本次新增賽事摘要表
    python scripts/verify_data.py --summary 341170 333043  # 摘要表指定 openid

輸出分兩級:
  [錯誤] 結構/索引不一致,是我們自己造成的 → exit 1,修好再部署
  [提醒] 來源資料本身的問題或已知缺口 → 不影響 exit code,照抄進月報即可
"""
import json
import subprocess
import sys
from pathlib import Path

import rebuild_index  # 共用 shard_of,確保與前端 common.js 的分片演算法一致

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
TOURN_DIR = DATA_DIR / "tournaments"
SHARDS = rebuild_index.SHARDS

errors = []
warns = []


def err(msg):
    errors.append(msg)


def warn(msg):
    warns.append(msg)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------- 各項檢查 ----------

def check_doc_links(tours):
    """regulation.pdf / resultPdf 必須仍存在於 documents 中。

    官方換新版 PDF 會換檔名(=換 URL),舊連結會失效。fetch_docs.sync_links 負責跟上,
    這裡是防止該機制再度失效(參考 commit fc380dd)。
    """
    bad = 0
    for oid, t in tours.items():
        urls = {d["url"] for d in (t.get("documents") or [])}
        if not urls:
            continue
        for field, val in (("regulation.pdf", (t.get("regulation") or {}).get("pdf")),
                           ("resultPdf", t.get("resultPdf"))):
            if val and val not in urls:
                bad += 1
                err(f"{oid} {t['name'][:24]} 的 {field} 已不在 documents 中(官方換版?)\n"
                    f"       現值 {val}")
    if not bad:
        print("[OK] 連結一致性:regulation.pdf / resultPdf 都指向 documents 內的檔案")


def check_index(tours):
    """index.json 的賽事集合與新鮮度。"""
    idx_path = DATA_DIR / "index.json"
    if not idx_path.exists():
        err("index.json 不存在 → 跑 python scripts/rebuild_index.py")
        return
    index = load(idx_path)
    idx_ids = {t["openid"] for t in index}
    file_ids = set(tours)
    if idx_ids != file_ids:
        for oid in sorted(file_ids - idx_ids):
            err(f"{oid} 有 tournament 檔但不在 index.json")
        for oid in sorted(idx_ids - file_ids):
            err(f"{oid} 在 index.json 但 tournament 檔已不存在")
    else:
        print(f"[OK] index.json 收錄 {len(index)} 場,與 tournaments/ 檔案一致")

    newest = max((p.stat().st_mtime for p in TOURN_DIR.glob("*.json")), default=0)
    if idx_path.stat().st_mtime < newest:
        err("index.json 比 tournaments/ 舊 → 有人改過賽事檔卻沒重建索引,"
            "跑 python scripts/rebuild_index.py")


def check_shards():
    """分片落點、search-index 與分片的 key 集合是否一致。"""
    si_path = DATA_DIR / "search-index.json"
    if not si_path.exists():
        err("search-index.json 不存在 → 跑 python scripts/rebuild_index.py")
        return
    si = load(si_path)

    for kind, si_key in (("players", "players"), ("units", "units")):
        names = set()
        misplaced = 0
        for i in range(SHARDS):
            p = DATA_DIR / kind / f"{i}.json"
            if not p.exists():
                err(f"{kind}/{i}.json 不存在 → 跑 python scripts/rebuild_index.py")
                return
            shard = load(p)
            keys = shard["players"].keys() if kind == "players" else shard.keys()
            for name in keys:
                names.add(name)
                if rebuild_index.shard_of(name) != i:
                    misplaced += 1
                    if misplaced <= 3:
                        err(f"{kind}「{name}」放在分片 {i},"
                            f"但 shard_of 算出 {rebuild_index.shard_of(name)}")
            if kind == "players":
                orphan = set(shard["ranks"]) - set(shard["players"])
                if orphan:
                    err(f"players/{i}.json 有 {len(orphan)} 筆 ranks 找不到對應選手,"
                        f"例:{sorted(orphan)[:3]}")
        si_names = set(si.get(si_key, {}))
        if names != si_names:
            err(f"{kind}:search-index 有 {len(si_names)} 筆、分片合計 {len(names)} 筆,"
                f"兩邊不一致(缺 {len(si_names - names)} / 多 {len(names - si_names)})")
        elif not misplaced:
            print(f"[OK] {kind} 分片:{len(names)} 筆,落點與 search-index 皆一致")


def check_mojibake(tours):
    """U+FFFD 置換字元 = 抓取時編碼壞掉。選手/單位名以 search-index 的 key 一次涵蓋。"""
    hits = []
    for oid, t in tours.items():
        for field in ("name", "venue"):
            if "�" in (t.get(field) or ""):
                hits.append(f"{oid} {field}={t.get(field)[:40]}")
    si_path = DATA_DIR / "search-index.json"
    if si_path.exists():
        si = load(si_path)
        for kind in ("players", "units"):
            for name in si.get(kind, {}):
                if "�" in name:
                    hits.append(f"{kind}「{name}」")
    for h in hits[:10]:
        err(f"名稱含亂碼: {h}")
    if not hits:
        print("[OK] 名稱編碼:未發現置換字元")


def check_dates(tours):
    for oid, t in sorted(tours.items()):
        ds, de = t.get("dateStart") or "", t.get("dateEnd") or ""
        if ds and de and ds > de:
            warn(f"{oid} {t['name'][:26]} 日期反轉 {ds} ~ {de}(來源資料如此,列表排序會錯位)")
        if not ds:
            warn(f"{oid} {t['name'][:26]} 缺 dateStart")


# 官方公布的名次(PDF/主辦平台總表)本來就會並列與跳號(依報名組數取 N 名,
# 第三名兩位、第五名四位),不該報。derived 由決賽勝負推導,重複或跳號代表推導有問題。
AUTHORITATIVE = {"pdf", "official"}


def check_standings(tours):
    """查 derived 名次的重複/跳號,以及 ocr 名次(機器讀圖)的合理性。"""
    bad = ocr_bad = 0
    for oid, t in sorted(tours.items()):
        groups = {}
        for s in t.get("standings", []):
            if s.get("source") in AUTHORITATIVE:
                continue
            groups.setdefault(s.get("group", ""), []).append((s.get("rank"), s.get("source")))
        for g, rows in groups.items():
            src = "ocr" if any(x[1] == "ocr" for x in rows) else "derived"
            ranks = [r for r, _ in rows]
            rs = [r for r in ranks if r is not None]
            if len(rs) != len(ranks):
                warn(f"{oid} 組別「{g}」有名次為空的 {src} 紀錄")
            elif rs and sorted(rs) != list(range(1, max(rs) + 1)):
                # ocr 允許並列(官方成績圖同樣有並列名次),只查跳號與重複的第一名
                dup_first = rs.count(1) > 1
                gap = sorted(set(rs)) != list(range(1, max(rs) + 1))
                if src == "ocr" and not (dup_first or gap):
                    continue
                warn(f"{oid} 組別「{g}」{src} 名次異常:{sorted(rs)}")
            else:
                continue
            bad += 1
            ocr_bad += src == "ocr"
    if not bad:
        print("[OK] derived / ocr 名次:無重複或跳號")
    elif ocr_bad:
        print(f"[提醒] 其中 {ocr_bad} 個組別來自圖片解析(ocr),建議對照原圖抽查")


def report_ocr(tours):
    """圖片解析的名次中,名冊查無而未能校對的筆數(補報名/換人居多,建議抽查)。"""
    rows = [(oid, s) for oid, t in tours.items()
            for s in t.get("standings", []) if s.get("ocrUnverified")]
    if not rows:
        return
    by_t = {}
    for oid, _ in rows:
        by_t[oid] = by_t.get(oid, 0) + 1
    print(f"\n== 圖片解析待確認({len(rows)} 筆,名冊查無,已收錄但標記 ocrUnverified)==")
    for oid, n in sorted(by_t.items()):
        print(f"  {oid}  {n} 筆")


def report_gaps(tours):
    """已結束、API 無比分、也還沒用 PDF 補名次 → 真正的資料缺口。"""
    gaps = [(t.get("dateEnd") or "", oid, t["name"])
            for oid, t in tours.items()
            if t.get("status") == "finished" and not t.get("matches")
            and not t.get("standings")]
    print(f"\n== 資料缺口({len(gaps)} 場已結束但無比分也無名次,需 PDF 補)==")
    for de, oid, name in sorted(gaps, reverse=True):
        print(f"  {oid}  {de}  {name}")


# ---------- 新增賽事摘要 ----------

def new_openids():
    """本次新增 = git 尚未追蹤的 tournament 檔。"""
    out = subprocess.run(["git", "status", "--porcelain", "--", "docs/data/tournaments"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout
    ids = []
    for line in out.splitlines():
        # porcelain v1:前兩碼是狀態、第 4 碼起是路徑。?? 未追蹤、A 已 add 但未 commit
        path = line[3:].strip()
        if line[:2] in ("??", "A ", "AM") and path.endswith(".json"):
            ids.append(Path(path).stem)
    return ids


def print_summary(tours, ids):
    if not ids:
        print("\n== 新增賽事:無 ==")
        return
    print(f"\n== 新增賽事({len(ids)} 場)==")
    print("  openid | 日期 | 縣市 | 組數 | 比分 | 規程 | 賽事名")
    rows = [(tours[i].get("dateStart") or "", i) for i in ids if i in tours]
    for _, oid in sorted(rows, reverse=True):
        t = tours[oid]
        print(f"  {oid} | {t.get('dateStart')}~{t.get('dateEnd')} | {t.get('city')} | "
              f"{len(t.get('groups', []))} 組 | {len(t.get('matches', []))} 場 | "
              f"{'有' if t.get('regulation') else '無'} | {t['name']}")
    for oid in ids:
        if oid not in tours:
            print(f"  {oid} (檔案讀不到)")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    want_summary = "--summary" in sys.argv

    tours = {p.stem: load(p) for p in sorted(TOURN_DIR.glob("*.json"))}
    print(f"檢查 {len(tours)} 場賽事…\n")

    check_doc_links(tours)
    check_index(tours)
    check_shards()
    check_mojibake(tours)
    check_dates(tours)
    check_standings(tours)

    if errors:
        print(f"\n== 錯誤({len(errors)})— 修好再部署 ==")
        for m in errors:
            print(f"  [錯誤] {m}")
    if warns:
        print(f"\n== 提醒({len(warns)})— 不影響部署 ==")
        for m in warns:
            print(f"  [提醒] {m}")

    report_ocr(tours)
    report_gaps(tours)

    if want_summary:
        print_summary(tours, args or new_openids())

    print(f"\n結果:錯誤 {len(errors)}、提醒 {len(warns)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
