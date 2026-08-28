---
name: badminton-update
description: 每月更新羽球賽事資料庫 — 從 mylivescore.tw / lapgo.com.tw / tsbadminton.url.tw 三個來源增量抓取新賽事與比分、重建索引、commit+push 部署到 GitHub Pages。觸發詞:「更新羽球賽事」「badminton update」「/badminton-update」。
---

# 羽球賽事資料庫 — 每月更新

專案根目錄:`c:\Users\ssken\Desktop\VS code\6. 羽球比賽紀錄資訊app\`
API 與資料模型細節見專案 CLAUDE.md。所有 python 指令都在專案根目錄執行。

資料有三個來源,各自的自動化程度不同:

| 來源 | 賽事清單 | 逐場比分 | 名次 |
|---|---|---|---|
| mylivescore.tw | API | API | 由比分推導(`derived`),官方 PDF 可覆蓋(`pdf`) |
| lapgo.com.tw | API | API | 官方成績總表 API(`official`,可到第 5 名) |
| tsbadminton.url.tw | 分類頁 HTML | ❌ 無 | 成績圖片視覺解析 + 名冊校對(`ocr`) |

## 步驟

1. **三來源增量抓取**(最後只重建一次索引):
   ```
   python scripts/update_all.py
   ```
   任一來源失敗不會中斷其他來源,最後會印出各來源的成功/失敗與耗時。
   單獨重跑某個來源用 `--only mylivescore` / `--only lapgo` / `--only tsba`。

   **各來源的判讀基準**(抓回的內容與現有檔案相同就不寫檔,所以「更新 N」都是真實變動):
   - **mylivescore**:更新正常是個位數到十幾場,「無變化」常有三、四十場。
     突然變成 50+ 場多半是比對邏輯失效或官網改版,先查清楚再部署。
     大量失敗時先檢查 liveresult host 是否變更(見 CLAUDE.md)。
   - **lapgo**:羽球賽事約 60~70 場,每月新增 1~5 場。
     出現 `[警告] 未知 matchtype` 要看一眼——代表主辦用了新的輪次寫法,
     若是常見寫法就加進 `scrape_lapgo.MATCHTYPE_MAP`(未知值會原樣保留,不會遺失資料)。
     出現 `[警告] 排除非羽球賽事` 屬正常(主辦常把籃球/排球標成「羽球比賽」)。
   - **tsba**:文件約 80 餘筆、賽事 19 場。`[提醒] 無法判定年份,未收錄` 屬正常
     (例如「第29~33屆冠軍表」這種跨屆彙整)。
   - **官方成績 PDF → 名次**(2026-08 起納入 update_all,在 fetch_docs 之後跑):
     印「N 組 M 筆」。**這行要看一眼** —— 官方名次是靜默失敗最容易發生的地方:
     解析器讀錯版面時不會報錯,只會少收,而且 source=pdf 優先度最高,會直接蓋掉
     推導名次。判讀基準是「組數 × 每組名次數」對不對得上該賽事的規模;
     一場九個項目的賽事只解出兩組,就是版面沒讀對(2026-08 的 259196 排名賽實例)。
     `[版面不支援] N 組需人工` 屬正常,那是程式判不出來、主動交還人工的。

   `--full` 全量重抓的時機:懷疑漏抓、官網改版後、或某場已用 PDF 補過名次但官方事後
   開放了逐場比分(增量模式對「已有名次的無比分賽事」不再重試)。
   `update_all.py --full` 只會對 mylivescore 與 lapgo 生效。

2. **參賽名單(tsba,缺才抓)**:
   ```
   python scripts/scrape_tsba.py --build-entries
   ```
   由賽程表 xlsx 抽出參賽名冊寫進 `entries[]`,讓沒得名的選手也搜尋得到
   (tsba 賽事沒有逐場比分,不做這步就只有得名者查得到)。
   已有名單的賽事會跳過,所以每月只會處理新賽事。判讀:
   - `[名單] {openid} N 筆 / 覆蓋 X%` —— 覆蓋率是對照籤表標題宣告人數算的。
     會長盃版面單純通常 98~100%;清晨盃排版雜,85~90% 屬正常。
   - `[淘汰] 組別:抽到 N 筆,宣告 M` —— 抽取數不到宣告的一半,視為解析失敗、整組不收。
     偶爾幾組正常(多為團體組);若大量淘汰代表籤表換版面了,要看 `tsba_xlsx.py` 的四種排版。
   - `[提醒] 沒有可用的 .xlsx 賽程表` —— 早年賽事只有 .xls 舊格式,無解,略過即可。

3. **規程欄位解析(可選)**:對本次新增且使用者關注的賽事,可從 documents 中的
   規程 URL 下載暫存檔後 Read,把用球/費用/紀念品/獎勵解析進 `regulation` 欄位
   (格式見 badminton-import skill),解析完刪除暫存檔。量大時先問使用者要解析哪些。

4. **tsba 成績圖解析(有新成績公告時才做)**:
   tsbadminton 的成績只有 JPG 圖片,賽程是空間排版的籤表 xlsx,兩者都無法直接轉成
   逐場比分。名次流程是「腳本備料 → 你讀圖 → 腳本校對」:

   ```
   python scripts/scrape_tsba.py --stage-results            # 全部
   python scripts/scrape_tsba.py --stage-results --only tsba-2026-會長盃
   ```
   會把成績圖下載到 `inbox/tsba/{openid}/`,並從同屆賽程表 xlsx 抽出參賽名冊
   (`roster.json`,單位+姓名的真文字),清單寫在 `inbox/tsba/worklist.json`。

   接著 **Read 那些 JPG**,輸出成 raw JSON(一列一個名次):
   ```json
   [{"group":"U9男子組單打","rank":1,"unit":"南屯國小","members":["莊以新"]}]
   ```
   冠軍=1、亞軍=2、季軍=3、殿軍=4、第五名=5(並列名次就重複同一個 rank);
   團體組只填 `unit`,`members` 留空陣列;儲存格畫斜線代表從缺,不要產生資料。

   再跑校對(**務必先不加 `--apply` 看報告**):
   ```
   python scripts/tsba_reconcile.py --openid tsba-2026-會長盃 --input raw.json
   python scripts/tsba_reconcile.py --openid tsba-2026-會長盃 --input raw.json --apply
   ```
   校對會把每個姓名對回名冊:完全命中直接採用、唯一近似解自動更正(原文留在 `ocrRaw`)、
   無法判定的不寫入而列成「需人工確認」。判讀基準:
   - **自動命中應該佔多數**。若大量落入「需人工確認」,通常是組別沒對上或名冊沒抽好,
     先修再匯入,不要硬寫進資料庫。
   - `[更正]` 逐條看一眼,這些是辨識錯字被名冊糾正回來的(例:`廖成昊→廖宬昊`)。
   - `[需確認]` 要回報給使用者,並說明是哪一組哪個名次。
   - `[未校對]` 代表名冊沒有該組別(團體賽名單不在籤表內),照原文收錄。

   匯入後跑 `python scripts/rebuild_index.py`。

5. **健檢**:
   ```
   python scripts/verify_data.py --summary
   ```
   - `[錯誤]`(exit 1):連結指向已失效的 PDF、索引與賽事檔不同步、分片落點錯亂、名稱亂碼。
     **一定要修好再部署**,多數情況跑 `python scripts/rebuild_index.py` 就解決。
   - `[提醒]`:來源資料本身的問題(如日期反轉、tsba 早年賽事缺 dateStart),
     照抄進月報即可,不用擋部署。
   - 若出現「其中 N 個組別來自圖片解析(ocr)」,要對照原圖抽查再部署。
   - `[錯誤] … pdf 名次有 N 個第一名`:成績總表被讀成同一組(分組標籤常寫在表格外面)。
     用 `python scripts/parse_result_pdf.py --openid X` 對一下原始 PDF,不要直接部署。
   - 另外會印「資料缺口」(已結束但無比分也無名次,需 PDF 補)與「新增賽事摘要表」,
     步驟 7 直接用這兩段輸出。摘要表以 git 未追蹤的檔案判定新增,所以要在 commit 前跑。

6. **部署**:
   ```
   git status                    # 先確認變動範圍合理
   git log --oneline -8          # 看本月已經跑過幾次
   git add -A
   git commit -m "monthly update YYYY-MM 第N次 (新增X場+更新Y場+文件連結Z筆)"
   git push
   ```
   同月第一次不加「第N次」,第二次起加(`第2次`、`第3次`…)。X/Y 取三個來源的合計。
   `inbox/` 已被 gitignore,備料的圖片與 xlsx 不會進版控。

7. **摘要回報**:
   - 一行總計:新增 X 場、更新 Y 場、無變化 Z 場、略過 W 場、文件連結 N 筆。
   - **依來源分列**新增/更新場數(mylivescore / lapgo / tsba),使用者要看得出各站的貢獻。
   - 新增賽事表格:直接用步驟 5 的摘要表(openid / 日期 / 縣市 / 組數 / 比分 / 規程 / 賽事名)。
   - 資料缺口:用步驟 5 的缺口清單,並點出本次新增之中哪幾場是空的。
   - 若這次有跑步驟 4,列出「自動命中 / 自動更正 / 需人工確認」三個數字與待確認清單。
   - **本月由官方 PDF 覆蓋名次:N 場 / M 筆**(步驟 1 的 parse_result_pdf 那段)。
     這行是給數字異常一眼看得出來用的,即使是 0 場也要寫。
   - 若有新增參賽名單,列出場次與覆蓋率(健檢的「參賽名單」段落)。
   - 健檢的 `[提醒]` 逐條列出。

   要自己查 index.json 時,欄位名是 `source` / `dateStart` / `dateEnd` / `city` / `status` /
   `hasMatches` / `hasRegulation` / `hasEntries` / `groupCount`(不是 date / matchCount / hasResult)。
