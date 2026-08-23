# -*- coding: utf-8 -*-
"""列出「有官方總成績紀錄 PDF、但名次仍是推導/從缺」的賽事,供 PDF 匯入排程。

名次優先序 pdf ≧ official > ocr > derived(見 CLAUDE.md)。推導名次推不出
「該組取幾名」—— 官方實測同一場賽事裡各組取 1~4 名都有(257164 新羽盃),
所以有 PDF 的一律該以 PDF 覆蓋。沒有 PDF 的就維持推導,並在此列成清單。

用法:python -X utf8 scripts/pdf_backfill_list.py [--json out.json] [--all]
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOURN_DIR = ROOT / "docs" / "data" / "tournaments"

# 官方總成績類文件的標題關鍵字(排除賽程表/報名結果/競賽規程等)
RESULT_KEYWORDS = ("總成績", "成績紀錄", "成績記錄", "成績總表", "成績冊", "名次表")


def result_docs(t):
    out = []
    for d in t.get("documents") or []:
        title = d.get("title") or ""
        if any(k in title for k in RESULT_KEYWORDS):
            out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="同時輸出 JSON 清單到指定路徑")
    ap.add_argument("--all", action="store_true", help="連沒有 PDF 的缺口也一併列出")
    args = ap.parse_args()

    have_pdf, no_pdf = [], []
    for p in sorted(TOURN_DIR.glob("*.json")):
        t = json.loads(p.read_text(encoding="utf-8"))
        srcs = Counter(s.get("source") for s in t.get("standings") or [])
        # 已經有 pdf 名次的組別不算缺口;全場皆 pdf 就完全不用處理
        weak = sum(n for s, n in srcs.items() if s != "pdf")
        if srcs and not weak:
            continue
        docs = result_docs(t)
        row = {
            "openid": t.get("openid"),
            "name": t.get("name"),
            "dateStart": t.get("dateStart"),
            "source": t.get("source"),
            "status": t.get("status"),
            "matches": len(t.get("matches") or []),
            "standings": dict(srcs),
            "weakCount": weak,
            "resultPdf": (docs[0].get("url") if docs else None),
            "resultDocTitle": (docs[0].get("title") if docs else None),
        }
        # 還沒打完的賽事不算缺口:籤表先發、比分全是空格子,本來就推不出名次
        played = any(m.get("winner") for m in t.get("matches") or [])
        row["played"] = played
        if docs:
            have_pdf.append(row)
        elif played or t.get("status") == "finished":
            no_pdf.append(row)
        elif args.all:
            row["note"] = "尚未開打"
            no_pdf.append(row)

    have_pdf.sort(key=lambda r: -r["weakCount"])
    no_pdf.sort(key=lambda r: (r["dateStart"] or ""), reverse=True)

    print(f"== 有官方總成績 PDF、名次卻仍是推導/OCR/官方總表({len(have_pdf)} 場,"
          f"共 {sum(r['weakCount'] for r in have_pdf)} 筆可覆蓋)==")
    print(f"{'openid':<12}{'日期':<12}{'筆數':>5}  {'現有來源':<22}賽事名")
    for r in have_pdf[:40]:
        src = ",".join(f"{k}{v}" for k, v in sorted(r["standings"].items()))
        print(f"{r['openid']:<12}{(r['dateStart'] or ''):<12}{r['weakCount']:>5}  {src:<22}{(r['name'] or '')[:30]}")
    if len(have_pdf) > 40:
        print(f"  …另有 {len(have_pdf) - 40} 場")

    print()
    print(f"== 沒有官方總成績 PDF,只能維持推導({len(no_pdf)} 場;不含尚未開打的)==")
    for r in no_pdf[:40]:
        st = "無名次" if not r["standings"] else ",".join(
            f"{k}{v}" for k, v in sorted(r["standings"].items()))
        print(f"{r['openid']:<12}{(r['dateStart'] or ''):<12}{r['matches']:>6} 場比分  "
              f"{st:<16}{(r['name'] or '')[:30]}")
    if len(no_pdf) > 40:
        print(f"  …另有 {len(no_pdf) - 40} 場")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"hasResultPdf": have_pdf, "noResultPdf": no_pdf},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n清單已寫入 {args.json}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
