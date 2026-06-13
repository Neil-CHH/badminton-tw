---
name: badminton-update
description: 每月更新羽球賽事資料庫 — 從 mylivescore.tw API 增量抓取新賽事與比分、重建索引、commit+push 部署到 GitHub Pages。觸發詞:「更新羽球賽事」「badminton update」「/badminton-update」。
---

# 羽球賽事資料庫 — 每月更新

專案根目錄:`c:\Users\ssken\Desktop\VS code\6. 羽球比賽紀錄資訊app\`
API 與資料模型細節見專案 CLAUDE.md。

## 步驟

1. **增量抓取**(專案根目錄執行):
   ```
   python scripts/scrape.py
   ```
   - 自動抓三種狀態(報名中/進行中/已結束)的羽球賽事(M_Type=1)。
   - 新賽事、狀態變更、進行中賽事會重抓比分與名次;結尾自動重建索引。
   - 留意輸出中的 `[錯誤]` 與 `[警告] 排除非羽球賽事`。
   - 若大量失敗,先檢查 liveresult host 是否變更(腳本會從 golivequery.php redirect 自動解析,但 mylivescore.tw 本身改版時需重新偵察,方法見 CLAUDE.md)。

2. **更新官方文件連結**(競賽規程/總成績紀錄/賽程表,只記連結不下載):
   ```
   python scripts/fetch_docs.py
   ```
   增量模式只查新賽事與尚未有成績連結的賽事。跑完再執行一次
   `python scripts/rebuild_index.py`(讓 hasRegulation 旗標生效)。

3. **規程欄位解析(可選)**:對本次新增且使用者關注的賽事,可從 documents 中的
   規程 URL 下載暫存檔後 Read,把用球/費用/紀念品/獎勵解析進 `regulation` 欄位
   (格式見 badminton-import skill),解析完刪除暫存檔。量大時先問使用者要解析哪些。

4. **驗證**:抽查 1 場新增賽事的 JSON(名稱無亂碼、matches 非空、standings 合理)。

5. **部署**:
   ```
   git add -A
   git commit -m "monthly update YYYY-MM"
   git push
   ```

6. **摘要回報**:新增 X 場、更新 Y 場、略過 Z 場;新增賽事清單(名稱+日期+縣市);缺規程賽事清單。
