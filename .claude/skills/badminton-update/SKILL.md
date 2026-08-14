---
name: badminton-update
description: 每月更新羽球賽事資料庫 — 從 mylivescore.tw API 增量抓取新賽事與比分、重建索引、commit+push 部署到 GitHub Pages。觸發詞:「更新羽球賽事」「badminton update」「/badminton-update」。
---

# 羽球賽事資料庫 — 每月更新

專案根目錄:`c:\Users\ssken\Desktop\VS code\6. 羽球比賽紀錄資訊app\`
API 與資料模型細節見專案 CLAUDE.md。所有 python 指令都在專案根目錄執行。

## 步驟

1. **增量抓取**:
   ```
   python scripts/scrape.py
   ```
   - 自動抓三種狀態(報名中/進行中/已結束)的羽球賽事(M_Type=1)。
   - 新賽事、狀態變更、進行中賽事會重抓比分與名次;結尾自動重建索引。
   - **判讀基準**:抓回來的內容與現有檔案相同時不寫檔,所以「更新 N」代表真的有變動,
     正常是個位數到十幾場,「無變化」則常有三、四十場。若「更新」突然變成 50+ 場,
     多半是比對邏輯失效或官網改版,先查清楚再部署。
   - 留意輸出中的 `[錯誤]` 與 `[警告] 排除非羽球賽事`。
   - 若大量失敗,先檢查 liveresult host 是否變更(腳本會從 golivequery.php redirect 自動解析,
     但 mylivescore.tw 本身改版時需重新偵察,方法見 CLAUDE.md)。
   - `--full` 全量重抓的時機:懷疑漏抓、官網改版後、或某場已用 PDF 補過名次但官方事後
     開放了逐場比分(增量模式對「已有名次的無比分賽事」不再重試)。

2. **更新官方文件連結**(競賽規程/總成績紀錄/賽程表,只記連結不下載):
   ```
   python scripts/fetch_docs.py
   python scripts/rebuild_index.py      # 讓 hasRegulation 旗標生效
   ```
   增量模式查新賽事、尚未有成績連結的賽事,以及結束 60 天內的賽事(官方常在賽後數週
   把總成績紀錄換成新版)。
   `--relink` 為不打 API 的維護模式,只用既有 documents 重新同步 regulation.pdf /
   resultPdf 指向最新一份;正常流程用不到。

3. **規程欄位解析(可選)**:對本次新增且使用者關注的賽事,可從 documents 中的
   規程 URL 下載暫存檔後 Read,把用球/費用/紀念品/獎勵解析進 `regulation` 欄位
   (格式見 badminton-import skill),解析完刪除暫存檔。量大時先問使用者要解析哪些。

4. **健檢**:
   ```
   python scripts/verify_data.py --summary
   ```
   - `[錯誤]`(exit 1):連結指向已失效的 PDF、索引與賽事檔不同步、分片落點錯亂、名稱亂碼。
     **一定要修好再部署**,多數情況跑 `python scripts/rebuild_index.py` 就解決。
   - `[提醒]`:來源資料本身的問題(如日期反轉),照抄進月報即可,不用擋部署。
   - 另外會印「資料缺口」(已結束但無比分也無名次,需 PDF 補)與「新增賽事摘要表」,
     步驟 6 直接用這兩段輸出。摘要表以 git 未追蹤的檔案判定新增,所以要在 commit 前跑。

5. **部署**:
   ```
   git status                    # 先確認變動範圍合理
   git log --oneline -8          # 看本月已經跑過幾次
   git add -A
   git commit -m "monthly update YYYY-MM 第N次 (新增X場+更新Y場+文件連結Z筆)"
   git push
   ```
   同月第一次不加「第N次」,第二次起加(`第2次`、`第3次`…)。X/Y 取自步驟 1 的
   「新增/更新」,Z 取自步驟 2 的「文件連結 N 筆」。

6. **摘要回報**:
   - 一行總計:新增 X 場、更新 Y 場、無變化 Z 場、略過 W 場、文件連結 N 筆。
   - 新增賽事表格:直接用步驟 4 的摘要表(openid / 日期 / 縣市 / 組數 / 比分 / 規程 / 賽事名)。
   - 資料缺口:用步驟 4 的缺口清單,並點出本次新增之中哪幾場是空的。
   - 健檢的 `[提醒]` 逐條列出。

   要自己查 index.json 時,欄位名是 `dateStart` / `dateEnd` / `city` / `status` /
   `hasMatches` / `hasRegulation` / `groupCount`(不是 date / matchCount / hasResult)。
