---
name: badminton-import
description: 匯入競賽規程或總成績紀錄 PDF 到羽球賽事資料庫 — 解析 inbox/ 內的 PDF、更新賽事 JSON、歸檔 PDF、重建索引並部署。觸發詞:「匯入規程」「匯入成績」「badminton import」「/badminton-import」,或使用者丟 PDF 進 inbox/ 時。
---

# 羽球賽事資料庫 — PDF 匯入

專案根目錄:`c:\Users\ssken\Desktop\VS code\6. 羽球比賽紀錄資訊app\`
資料模型見專案 CLAUDE.md。

## 步驟

> **PDF 讀取**:這些 PDF 字型無 Unicode 對應,`pdftotext` 抽不到中文。一律用
> `scripts/pdf_render.py` 渲染成圖片再用 Read 視覺辨識(需 `pip install pymupdf`):
> - 定位:`python -X utf8 scripts/pdf_render.py <PDF或URL> --pages 1-2,N-1,N --out DIR`
>   先看頭尾頁找「成績總表」所在頁(總成績紀錄通常在最後 1~3 頁)。
> - 逐列判讀:`... --pages 88-89 --rows --scale 3 --out DIR` 依表格橫線切成單列 PNG
>   (scale 3 約 1785px,可多圖一次讀;太密的儲存格再用更高倍率單獨裁切)。
>   外連 URL 含空格沒關係,腳本會自行處理。

1. **掃描 `inbox/*.pdf`**(使用者也可能直接給檔案路徑)。判斷類型:
   - **競賽規程**:含報名日期、競賽組別、報名費、獎勵辦法等章節。
   - **總成績紀錄**:籤表/晉級圖 + 通常最後幾頁有「成績總表」(各組名次)。

2. **比對賽事**:從 PDF 標題取賽名,在 `docs/data/index.json` 以名稱關鍵字+日期找 openid。
   - 民國紀年要換算(民國 114 年 = 2025)。
   - 找不到 → 屬於網站未收錄的歷史賽事,新建 `docs/data/tournaments/manual-{YYYY}-{slug}.json`,
     依資料模型填基本欄位(openid 用檔名同字串、status=finished、isSystem=false)。

3. **規程 PDF → `regulation` 欄位**:
   ```json
   {"organizer": "主辦單位", "ball": "比賽用球(無則 null)", "fee": "報名費",
    "souvenir": "紀念品", "awards": "獎勵辦法(含取名次規則)", "notes": "賽制等重點",
    "pdf": "archive/規程/{year}/{openid}_競賽規程.pdf"}
   ```
   只填 PDF 明確寫的內容,不要腦補。

4. **成績 PDF → `standings`**:以最後的「成績總表」為準(各組依報名組數取 N 名,
   決賽敗者不一定有名次)。每筆 `{"group","rank","unit","members":[…],"source":"pdf"}`。
   - group 名稱須與該賽事 `groups[].name` 一致;`groups[]` 為空時(如排名賽)由成績總表
     的組別補建(`scripts/import_pdf_standings.py` 已示範)。
   - 合併規則:同組別 PDF 名次**取代**原 derived 名次;其他組別的 derived 保留。
   - **雙打跨單位**:兩位選手不同單位時加 `"memberUnits":[單位A,單位B]`(與 members 對齊),
     `unit` 填顯示用的「A／B」。`rebuild_index.py` 會用 memberUnits 把每位選手歸到正確單位。
   - 若該賽事 documents 中有官方成績連結,`resultPdf` 填該 URL;沒有就留 null。

4b. **排名賽(category=排名賽)專屬**:全國羽球排名賽是晉升甲組的唯一管道,需額外:
   - `category`:由賽名推導(scrape.py 已自動帶,排名賽/錦標賽);匯入時確認存在。
   - `promotion`:晉升甲組規則(取自成績 PDF 末頁備註),例:
     「乙組單打前 4 名、乙組雙打前 3 名晉升為中華民國羽球協會甲組球員。」
   - `promoted`:依備註對乙組名次標 `true`(單打 rank≤4、雙打 rank≤3)。
   - 大量資料可比照 `scripts/import_pdf_standings.py` 把辨識結果寫成資料後一次匯入。

5. **PDF 不留底**(使用者決定不存檔):`regulation.pdf` 同樣只填 documents 中的官方
   URL(無則 null)。解析完成後告知使用者該 PDF 已處理完畢,可自行移走 inbox 檔案
   (inbox/*.pdf 已在 .gitignore,不會被上傳)。

6. **重建索引 + 部署**:
   ```
   python scripts/rebuild_index.py
   git add -A && git commit -m "import: {賽事名}" && git push
   ```

7. **回報**:賽事名、匯入類型、寫入的名次筆數、規程欄位摘要;有對不上的組名或缺漏要列出。
