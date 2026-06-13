---
name: badminton-import
description: 匯入競賽規程或總成績紀錄 PDF 到羽球賽事資料庫 — 解析 inbox/ 內的 PDF、更新賽事 JSON、歸檔 PDF、重建索引並部署。觸發詞:「匯入規程」「匯入成績」「badminton import」「/badminton-import」,或使用者丟 PDF 進 inbox/ 時。
---

# 羽球賽事資料庫 — PDF 匯入

專案根目錄:`c:\Users\ssken\Desktop\VS code\6. 羽球比賽紀錄資訊app\`
資料模型見專案 CLAUDE.md。

## 步驟

1. **掃描 `inbox/*.pdf`**(使用者也可能直接給檔案路徑)。逐檔 Read 並判斷類型:
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
   - group 名稱須與該賽事 `groups[].name` 一致(對不上時用最接近者並回報)。
   - 合併規則:同組別 PDF 名次**取代**原 derived 名次;其他組別的 derived 保留。
   - 若該賽事 documents 中有官方成績連結,`resultPdf` 填該 URL;沒有就留 null。

5. **PDF 不留底**(使用者決定不存檔):`regulation.pdf` 同樣只填 documents 中的官方
   URL(無則 null)。解析完成後告知使用者該 PDF 已處理完畢,可自行移走 inbox 檔案
   (inbox/*.pdf 已在 .gitignore,不會被上傳)。

6. **重建索引 + 部署**:
   ```
   python scripts/rebuild_index.py
   git add -A && git commit -m "import: {賽事名}" && git push
   ```

7. **回報**:賽事名、匯入類型、寫入的名次筆數、規程欄位摘要;有對不上的組名或缺漏要列出。
