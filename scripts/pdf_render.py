# -*- coding: utf-8 -*-
"""把 PDF(本機路徑或 URL)渲染成 PNG 圖片,供視覺辨識。

賽事成績/規程 PDF 多用 CID 字型,pdftotext 抽不到中文,只能渲染成圖再用視覺讀。

用法:
    python scripts/pdf_render.py <path-or-url> [--pages 1-5,8] [--dpi 200] [--out DIR]
    python scripts/pdf_render.py <path-or-url> --pages 88-89 --rows   # 依表格橫線切成單列

整頁模式輸出 page-001.png …;--rows 模式把每頁表格依偵測到的橫格線切成單列
(pNN-rowMM.png),適合逐列辨識密集的成績總表。每個路徑印到 stdout。
不指定 --out 時用系統暫存資料夾下的隨機目錄。
"""
import argparse
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import fitz  # PyMuPDF


def fetch(src):
    """回傳 (pdf_bytes, 顯示用名稱)。src 可為本機路徑或 http(s) URL。"""
    if src.startswith("http://") or src.startswith("https://"):
        safe = urllib.parse.quote(src, safe=":/%?=&")
        req = urllib.request.Request(safe, method="GET")
        req.add_header("User-Agent", "Mozilla/5.0 (badminton-db pdf_render)")
        with urllib.request.urlopen(req, timeout=60) as res:
            return res.read(), src.rsplit("/", 1)[-1]
    p = Path(src)
    return p.read_bytes(), p.name


def parse_pages(spec, total):
    """'1-5,8' → [0,1,2,3,4,7](0-based,過濾超界)。None → 全部。"""
    if not spec:
        return list(range(total))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a) - 1, int(b)))
        elif part:
            out.append(int(part) - 1)
    return [i for i in out if 0 <= i < total]


def render(src, pages=None, dpi=200, out_dir=None):
    data, name = fetch(src)
    doc = fitz.open(stream=data, filetype="pdf")
    idxs = parse_pages(pages, doc.page_count)
    out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="pdfrender_"))
    out.mkdir(parents=True, exist_ok=True)
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    paths = []
    for i in idxs:
        pix = doc.load_page(i).get_pixmap(matrix=mat)
        fp = out / f"page-{i + 1:03d}.png"
        pix.save(fp)
        paths.append(fp)
    doc.close()
    return name, doc_page_count(data), paths


def doc_page_count(data):
    d = fitz.open(stream=data, filetype="pdf")
    n = d.page_count
    d.close()
    return n


def _hlines(pg):
    """偵測整頁的水平格線 y 座標(已群聚相近線)。"""
    ys = []
    for d in pg.get_drawings():
        for it in d["items"]:
            if it[0] == "l" and abs(it[1].y - it[2].y) < 1 and abs(it[2].x - it[1].x) > pg.rect.width * 0.5:
                ys.append(it[1].y)
            elif it[0] == "re":
                ys.append(it[1].y0)
                ys.append(it[1].y1)
    ys.sort()
    clusters = []
    for y in ys:
        if not clusters or y - clusters[-1][-1] > 4:
            clusters.append([y])
        else:
            clusters[-1].append(y)
    return [sum(c) / len(c) for c in clusters]


def render_rows(src, pages=None, scale=5, out_dir=None, min_h=12):
    """把指定頁的表格依水平格線切成單列 PNG,回傳路徑清單。"""
    data, name = fetch(src)
    doc = fitz.open(stream=data, filetype="pdf")
    idxs = parse_pages(pages, doc.page_count)
    out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="pdfrows_"))
    out.mkdir(parents=True, exist_ok=True)
    mat = fitz.Matrix(scale, scale)
    paths = []
    for pno in idxs:
        pg = doc.load_page(pno)
        ys = _hlines(pg)
        for i in range(len(ys) - 1):
            if ys[i + 1] - ys[i] < min_h:
                continue
            clip = fitz.Rect(pg.rect.x0, ys[i] - 1, pg.rect.x1, ys[i + 1] + 1)
            pix = pg.get_pixmap(matrix=mat, clip=clip)
            fp = out / f"p{pno + 1:03d}-row{i:02d}.png"
            pix.save(fp)
            paths.append(fp)
    doc.close()
    return name, paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="PDF 本機路徑或 URL")
    ap.add_argument("--pages", default=None, help="頁碼,如 1-5,8(1-based);省略=全部")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--rows", action="store_true", help="依表格橫線切成單列(--dpi 改用倍率)")
    ap.add_argument("--scale", type=int, default=5, help="--rows 模式的渲染倍率")
    ap.add_argument("--out", default=None, help="輸出資料夾;省略用暫存目錄")
    args = ap.parse_args()
    if args.rows:
        name, paths = render_rows(args.src, args.pages, args.scale, args.out)
        print(f"# {name} — 切出 {len(paths)} 列 @ {args.scale}x", file=sys.stderr)
    else:
        name, total, paths = render(args.src, args.pages, args.dpi, args.out)
        print(f"# {name} — {total} 頁,渲染 {len(paths)} 張 @ {args.dpi}dpi", file=sys.stderr)
    for p in paths:
        print(p)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
