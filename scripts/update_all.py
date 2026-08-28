# -*- coding: utf-8 -*-
"""一次跑完三個來源的更新,最後只重建一次索引。

用法:
    python scripts/update_all.py            # 每月增量更新
    python scripts/update_all.py --full     # 三個來源都全量重抓
    python scripts/update_all.py --only lapgo,tsba
    python scripts/update_all.py --stage-results   # 另外備妥 tsba 成績圖與名冊

任一來源失敗不會中斷其他來源(某站改版時仍能完成其餘更新),最後統一回報。
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = [sys.executable, "-X", "utf8"]

# (代號, 說明, 腳本, 支援 --full)
SOURCES = [
    ("mylivescore", "mylivescore.tw", "scrape.py", True),
    ("lapgo", "lapgo.com.tw", "scrape_lapgo.py", True),
    ("tsba", "tsbadminton.url.tw", "scrape_tsba.py", False),
]


def run(script, args, label):
    cmd = PY + [str(ROOT / "scripts" / script)] + args
    # 先 flush,否則輸出被導向檔案/pipe 時,父行程的標題會跟子行程的輸出錯位
    print(f"\n{'=' * 60}\n▶ {label}\n{'=' * 60}", flush=True)
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=str(ROOT / "scripts"), check=False)
    except Exception as e:  # noqa: BLE001
        print(f"[錯誤] {label} 無法啟動: {e}")
        return False, 0
    ok = r.returncode == 0
    dt = time.time() - t0
    if not ok:
        print(f"[錯誤] {label} 失敗(exit {r.returncode}),繼續跑其他來源")
    return ok, dt


def main():
    argv = sys.argv[1:]
    full = "--full" in argv
    only = None
    if "--only" in argv:
        only = {x.strip() for x in argv[argv.index("--only") + 1].split(",") if x.strip()}

    results = []
    for key, label, script, supports_full in SOURCES:
        if only and key not in only:
            continue
        args = ["--no-index"] + (["--full"] if full and supports_full else [])
        ok, dt = run(script, args, label)
        results.append((label, ok, dt))

    if not only or "mylivescore" in only:
        ok, dt = run("fetch_docs.py", [], "官方文件連結 (fetch_docs)")
        results.append(("fetch_docs", ok, dt))

        # 官方總成績 PDF → 名次。fetch_docs 只是把 PDF 連結收進 documents,
        # 不解析就等於「答案掛在那裡沒人看」——名次會一直是推導的,而「該賽事取幾名」
        # 推導永遠猜不到。2026-08 查出積了 22 場、596 筆沒補,就是因為這步以前
        # 不在月更裡、要等人想到才手動跑。
        # 增量:名次已全是 pdf 的賽事會自己跳過;解不動的組別只會列出來交還人工。
        ok, dt = run("parse_result_pdf.py", ["--all", "--apply"],
                     "官方成績 PDF → 名次 (parse_result_pdf)")
        results.append(("官方成績PDF", ok, dt))

    if "--stage-results" in argv:
        ok, dt = run("scrape_tsba.py", ["--stage-results"], "tsba 成績圖/名冊備料")
        results.append(("stage-results", ok, dt))

    # 先去重再重建索引:同一場賽事被兩個平台各收一次時只留一份,
    # 否則列表會出現兩張卡、選手/單位的場次與勝負獲獎全部翻倍。
    ok, dt = run("dedupe.py", [], "跨來源去重 (dedupe)")
    results.append(("跨來源去重", ok, dt))

    ok, dt = run("rebuild_index.py", [], "重建索引 (rebuild_index)")
    results.append(("重建索引", ok, dt))

    print(f"\n{'=' * 60}\n▶ 總結\n{'=' * 60}")
    for label, ok, dt in results:
        print(f"  {'✔' if ok else '✘'} {label:<28} {dt:5.1f}s")
    failed = [x[0] for x in results if not x[1]]
    if failed:
        print(f"\n[注意] 失敗:{', '.join(failed)}。修好後可單獨重跑該來源。")
    print("\n接著跑 python scripts/verify_data.py --summary 再部署。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
