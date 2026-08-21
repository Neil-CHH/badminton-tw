# 羽球賽事資料庫(PWA + 本地留底)

台灣羽球賽事資料庫:賽事資訊、逐場比分、各組名次。核心查詢是「以學校/單位為中心」與
「以選手為中心」的跨賽事查詢。每月以 `/badminton-update` 手動更新,歷史 PDF 以
`/badminton-import` 匯入。

## 三個資料來源

賽事 JSON 與 index.json 都有頂層 `source` 欄位;`sources_common.source_of()` 對舊資料
(沒有該欄位)會由 openid 前綴回推。openid 命名空間:mylivescore 用純數字、
其餘用 `lapgo-{cid}` / `tsba-{年}-{系列}` / `manual-{年}-{slug}`。

| 來源 | openid | 賽事清單 | 逐場比分 | 名次 |
|---|---|---|---|---|
| mylivescore.tw | `250115` | API | API | 由比分推導 `derived`;PDF 匯入 `pdf` 覆蓋 |
| lapgo.com.tw | `lapgo-122` | API | API | 官方成績總表 API → `official`(可到第 5 名) |
| tsbadminton.url.tw | `tsba-2024-會長盃` | 分類頁 HTML | ❌ 無 | 成績圖片視覺解析 + 名冊校對 → `ocr` |

**名次優先序**(`sources_common.merge_standings`,以「組別」為單位取最高者,同組不混用):
`pdf` ≧ `official` > `ocr` > `derived`。

**參賽名單 `entries[]`**:沒有逐場比分的賽事只有得名者查得到(tsba 實測 2,213 人參賽只有
221 人入庫)。`entries[]` 登錄「有出賽」這件事,讓沒得名的選手也搜尋得到:

```json
"entries": [{"group":"U11男單","unit":"北市民權","members":["王綨褘"],"source":"draw"}]
```

`source`:`draw`(賽程表籤表)。另有 `entriesCoverage`(對照籤表標題宣告人數的覆蓋率)。
`rebuild_index` 只登錄出賽事實、不動勝負;該選手在該賽事既無比分也無名次時,
分片紀錄加 `"e": 1`,前端顯示「僅參賽」。目前只有 tsba 有資料,欄位本身是通用的。

**跨來源去重**:同一場真實賽事會被兩個平台各收一次(實測 9 場 mylivescore × lapgo)。
`rebuild_index` 以 openid 為唯一鍵逐檔累加,不去重就會出現兩張卡、選手/單位的場次與
勝負獲獎全部翻倍(實測影響 5,944 位選手)。`scripts/dedupe.py` 自動判定,四項條件全中才算:

1. **來源不同** —— 同來源一律不自動處理。同來源的近似賽名有 130 組候選、**全部是誤判**
   (大專資格賽分區、群岳盃分站、運動 i 臺灣各鄉鎮、小樹苗兩梯…),真重複一組都沒有。
2. 賽名正規化後相似度 ≥ 0.90,且**年份 token 沒有互斥**(「114年…資格賽」與「115年…」
   相似度 0.95、組別全同、公告日期還重疊,只有年份能區分)。
3. **實際比賽日期區間有交集** —— 區間取自 `matches[].date`,沒有比分才退回 `dateStart`。
   不能只看 `dateStart`:357718 的 `dateStart` 是錯的(標 6/14、實際 12/27)。
4. 組別集合 Jaccard ≥ 0.85(組名要正規化:LAPGO 常把組名截斷少了右括號)。

保留 `SOURCE_PRIORITY` 高的那份(mylivescore > manual > lapgo > tsba),另一份(shadow)
**刪檔**並登錄進 `scripts/duplicates.json`,爬蟲之後跳過不再抓。刪除前還要通過
**支配性檢查**:shadow 不能有任何 canonical 缺少的東西(場次更多、獨有組別、名次來源更優先、
獨有 `entries[]`)—— 任一項不過就只警告不動檔,交給人判斷。另有**斷路器**:單次要刪超過
3% 就整批中止。**支配性檢查不可用名次逐列比對** —— canonical 有 pdf 名次時兩邊逐列本來就
不同(307030 的名次列 Jaccard 只有 0.78),那正是 canonical 較優的證據。

**正規化契約(最重要)**:`rebuild_index.py` 與 `docs/tournament.html` 都直接讀 mylivescore
原始 match 形狀。新來源必須輸出同樣 14 個 key(全為字串):`groupName / match / date /
time / teamA / teamB / matchtype / stadium / winner / Asidescore / Bsidescore / abstain /
HeadGroup / scoreinfo[]`。沒對上不會報錯,而是**靜默產生空的選手/單位統計**——
改動抓取程式後,務必抽一位該賽事的選手確認勝負統計不是空的。

## 架構

- `docs/` — PWA(GitHub Pages 站台根目錄;vanilla JS、無 build step、繁中)
  - 頁面:index(賽事列表)、search(選手+單位搜尋)、unit、player、tournament、about
  - `data/index.json` 賽事摘要|`data/search-index.json` 輕量搜尋索引(搜尋頁唯一載入)
  - `data/players/{0-15}.json`、`data/units/{0-15}.json` 選手/單位分片(名稱雜湊分 16 片;
    Python `rebuild_index.shard_of` 與 JS `common.js shardOf` 演算法必須一致)
  - `data/tournaments/{openid}.json` 單場賽事完整資料(含逐場比分與官方文件連結)
  - 官方 PDF **不留底**(使用者決定):documents/regulation.pdf/resultPdf 都是外部 URL
- `scripts/update_all.py` — **三來源總入口**,依序跑各 scraper 後只重建一次索引;
  任一來源失敗不中斷其他來源(`--only`、`--full`、`--stage-results`)
- `scripts/sources_common.py` — 跨來源共用:`source_of` / `merge_standings` /
  `write_if_changed` / `city_from_text` / `NON_BADMINTON` 排除規則 / 帶 cookie 的 `Http`
- `scripts/scrape.py` — mylivescore:API 抓取+組別標籤+名次推導(`--full`、`--no-index`)
- `scripts/scrape_lapgo.py` — lapgo:比分正規化 + 官方成績總表 → standings
- `scripts/scrape_tsba.py` — tsba:文件清單 +(`--stage-results`)備妥成績圖與名冊
- `scripts/tsba_xlsx.py` — 由籤表 xlsx 抽參賽名冊與比賽日期
- `scripts/tsba_reconcile.py` — 成績圖解析結果 × 名冊校對 → `source:"ocr"` 的 standings
- `scripts/dedupe.py` — 跨來源重複賽事偵測與刪檔(重建索引前跑);
  登錄檔 `scripts/duplicates.json` 同時是爬蟲的阻擋清單
- `scripts/rebuild_index.py` — 由 tournaments/*.json 重建索引與分片(匯入後必跑)
- `scripts/verify_data.py` — 資料健檢(連結一致性/索引新鮮度/分片落點/亂碼/缺口),只讀不寫
- `inbox/` — 待匯入 PDF 與 tsba 待解析素材暫放(整個目錄 gitignore)

## mylivescore.tw API(2026-06 偵察,細節勿憑記憶,以 scrape.py 為準)

- 所有請求都要帶 `Origin` header,否則回 `Forbidden origin`。
- token:`POST https://mylivescore.tw/proxytoc.php` body `{"Account":"officialc","api":"gtoken"}`。
- 賽事清單 `api:mlist`:M_Type=1(羽球)、M_Status 1報名中/2進行中/3已結束;CityName 縣市代碼
  1-22(對照表在 scrape.py)。回應含未跳脫換行,需 `replace(\n→\\n)` 後再 JSON parse。
- 選手賽事 API host 是 IP(`http://172.105.210.232/liveresult/`),由
  `https://livescore.efsoft.net/golivequery.php?openid=X` redirect 動態解析。
  `api:items` 組別、`api:matches` 逐場比分。**IsSystem 旗標不可靠**:許多 IsSystem=0
  的地方賽/休閒賽 API 仍回傳完整逐場比分,故 scrape.py 對已結束/進行中賽事一律試抓
  (2026-06 起);少數賽事(全中運會內賽、全運資格賽、部分選拔賽)才真的回空,需 PDF 補。
- 籤表:`http://livescore.mylivescore.tw/draws/{openid}/{groupid}.html`(直接外連)。
- **賽事文件 PDF**(規程/總成績紀錄/賽程表):`POST https://go.mylivescore.link/links.php`
  body `{"OpenID":openid,"api":"news","token":...}`(token 用 `{"Account":"linktree","api":"gtoken"}`,
  Origin 帶 `https://go.mylivescore.link`)。回傳 info[] 含 Cont 標題與 Link PDF 直接下載連結。
  `scripts/fetch_docs.py` 把連結寫進 documents 欄位(只記連結不下載)。
  注意回應 JSON 含原始換行,要用 `json.JSONDecoder(strict=False)`。
- 網站改版時:抓 `matches.html` 找 `matchesmain.js`,看 fetch 端點與 payload。

## lapgo.com.tw API(2026-08 偵察,以 scrape_lapgo.py 為準)

- 免登入。從任一頁面抓 `<meta name="csrf-token">`,之後 POST 都帶 `X-CSRF-TOKEN` + cookie。
- 賽事清單:`POST /getCompetitionByStatus` body `status=all`
  → `{now, sign_up, coming_soon, finish, notyet}`;`type=='羽球比賽'` 才收,
  但**主辦常把籃球/排球/樂樂棒球標成羽球比賽**,要再用賽名過濾(`NON_BADMINTON`)。
- 逐場比分:`POST /web/getSessionScoreGrouped` body `cid={id}` → `{table:[...]}`。
- 官方成績總表:`POST /eventinfo/getResultsSummary` body `cid={id}`(名次到第 5 名)。
- **`show_livescore` 旗標不可靠**(同 mylivescore 的 IsSystem):實測 20 場
  `show_livescore=0` 的已結束賽事有 17 場仍回傳完整比分 → 一律試抓,不看旗標。
- **一列 = 一局(單雙打)或一點(團體)**,依 `(session_group_id, session_num)` 併成一場;
  `point_sum` 是全場局/點數(→ Asidescore/Bsidescore),`score` 是該局分數(→ scoreinfo)。
  `point_count>=10` 是團體賽彙總列,只取第一列並改用 `score` 判勝負。
- **`name` 是「組別+場次代號」不是組別**,代號寫法各主辦不同(`(一)`、`A1-A3`、`[9]`);
  真正的組別是 `session_group_id`,組別名取同 sgid 底下所有 name 的共同前綴。
- 比分與成績總表的**組別名寫法不一致**(`U10女單` vs `U10歲組女單`),
  `align_groups()` 做一對一貪婪配對;不強制一對一會把多組併成一組。
- 網站改版時:抓賽事頁的 `js/web.js`,搜 `url:` 看端點;成績總表在 `js/resultsSummary.js`。

## tsbadminton.url.tw(2026-08 偵察,以 scrape_tsba.py 為準)

中華民國全民羽球發展協會,主辦全民會長盃 / 世界清晨盃 / 羽您有約,歷屆紀錄留在站上。

- **必須帶瀏覽器 User-Agent,否則整站回 HTTP 500。**
- **附件有防盜連**:`/upload_attach/` 只認同站 Referer,外站(含本 PWA)一律 403。
  → `documents[].url` 一律指向明細頁 `hot_{id}.html`,絕不可直接放附件網址。
- 分類頁:`hot_cg105163`(會長盃)、`hot_cg100948`(清晨盃)、`hot_cg118656`(羽您有約);
  歷屆成績:`custom_cg45241`(會長盃)、`custom_cg45240`(清晨盃)。
  解析可見的 `<a href="(hot|custom)_\d+.html">標題</a>` 即可,不必靠 schema.org 區塊。
- **`/upload_attach/{epoch}.{xlsx|pdf}` 的檔名是 unix epoch**,用來當文件日期,
  也用來替標題沒寫年份的文件推年份。
- **成績只有 JPG 圖片**(`/editor_images/`);賽程 xlsx 是空間排版的籤表樹,無法轉逐場比分。
  但籤表裡「單位｜姓名」是真文字,`tsba_xlsx.extract_roster` 抽成名冊,一份資料兩個用途:
  OCR 校對字典 + 賽事的 `entries[]` 參賽名單。
- **籤表有四種排版都要處理**(`extract_roster` 的 A/B/C/D):
  A `序號｜單位｜姓名`(2023 雙打是同列並排、2024 是搭檔在上一列且該列沒有序號)、
  B `序號｜(空)｜姓名`(沒填單位的個人參賽者)、
  C `隊名：｜X隊` + `隊員：｜甲｜乙…`(團體隊伍名單)、
  D **直式**:單位／姓名1／姓名2 疊在同一欄的連續列(2022 的雙打分頁)。
  單位詞彙要**整本活頁簿一起建**,因為直式分頁自己建不出來。
- 組別標題兩種寫法都要認:會長盃短寫法「U9男單」、清晨盃長寫法「30歲男子甲組單打」;
  說明常和標題擠在同一格(「…男單 ：108 人,107 場」),長度門檻要在剝掉說明之後才判斷。
- 宣告人數(`declared_counts`)用來驗證抽取完整度,但**統計表分頁要跳過** ——
  同名標題旁邊的數字是場數之類的別的東西,會蓋掉籤表標題裡的正確答案。
- 賽事日期只有賽程表 xlsx 的日賽程分頁才有(列表頁的日期是公告有效期,不是賽期)。
- 早年附件是 `.xls` 舊二進位格式,openpyxl 讀不了,只收 `.xlsx`。

## 資料模型重點

- `matches[]` 為 API schedule 原樣:teamA/B=單位名(自由填寫,同校多種寫法,**不做正規化**,
  前端用子字串模糊比對 + 變體合併);scoreinfo[].memberA/B=選手名(雙打以 - 或 / 連接)。
- `matchtype` 代號:`預賽`、`R{n}` 淘汰賽(R2=決賽、R34=三四名戰)、`F2/F3/F4` 循環決賽。
- 名次推導(scrape.py derive_standings):R2 勝負=1/2 名、R34=3/4 名、F3/F4 依勝場數。
  官方規則是「依報名組數取 N 名」,決賽敗者不一定有名次 → **PDF 匯入的名次(source=pdf)
  永遠優先**,同組別覆蓋 derived。
- 手動匯入的歷史賽事 openid 格式:`manual-{YYYY}-{slug}`。

## 常用指令

```
python scripts/update_all.py             # 每月增量更新(三來源 + fetch_docs + 重建索引)
python scripts/update_all.py --full      # 全量重抓(對 mylivescore/lapgo 生效)
python scripts/update_all.py --only lapgo        # 只跑單一來源
python scripts/scrape.py          # 只跑 mylivescore(--full / --no-index)
python scripts/scrape_lapgo.py    # 只跑 lapgo(--full / --only {cid} / --dry-run)
python scripts/scrape_tsba.py     # 只跑 tsba(--dry-run / --no-detail)
python scripts/scrape_tsba.py --build-entries   # 由賽程表 xlsx 建參賽名單(缺才抓)
python scripts/scrape_tsba.py --stage-results    # 備妥 tsba 成績圖與名冊供視覺解析
python scripts/tsba_reconcile.py --openid X --input raw.json [--apply]  # 名冊校對
python scripts/fetch_docs.py      # 更新官方文件連結(增量,不下載;只對 mylivescore)
python scripts/fetch_docs.py --relink   # 不打 API,只重新同步 regulation.pdf/resultPdf
python scripts/dedupe.py --dry-run  # 看跨來源重複判定,不動檔
python scripts/dedupe.py          # 刪除重複賽事檔並登錄(update_all 已內含)
python scripts/rebuild_index.py   # 重建索引(匯入後)
python scripts/verify_data.py --summary  # 健檢+新增賽事摘要(部署前跑,exit 1 表示要修)
python -m http.server 8765 -d docs   # 本地預覽
```

部署:GitHub Pages(main branch /docs)。更新資料後 `git add -A && git commit && git push` 即上線。

## 注意

- Windows console 編碼:python 一律 `-X utf8`,stdout 需 reconfigure(腳本已內建)。
- **跨語言契約有三個,改一邊就要改另一邊**:`rebuild_index.shard_of` ↔ `common.js shardOf`、
  mylivescore 的 14 個 match key、`sources_common.effective_dates` ↔ `common.js effectiveDates`。
- 賽事日期(2026-08):少數賽事的 `dateStart` 是錯的(整串複製到別場、日期反轉、
  `0000-00-00`),會讓列表排序與年份篩選錯位。`effective_dates` 只在**與實際比分日期完全
  沒有交集時**才改用 `matches[].date` 推得的區間(目前 5 場),「公告 2/28 起、實際 3/1
  開打」這種正常落差一律保留原值。**賽事 JSON 本體不改寫**(保持來源忠實,也避免爬蟲每月
  覆寫→rebuild 再補寫的 git 噪音),所以直接讀賽事檔的 tournament.html 必須自己套同一條規則。
- 選手/單位索引已分片(2026-07):搜尋頁只載 search-index.json(~1.5MB),選手/單位頁依
  名稱雜湊載對應分片(最大單片 <1MB)。選手勝負統計(w/l)由 rebuild_index 預算進分片。
- sw.js shell 快取為 stale-while-revalidate,改前端後不需手動升 VERSION;data 為
  network-first + 3.5s timeout 退回快取。
- 名次與比分為自動推導/抓取,about 頁已標注「以官方公告為準」。
- 增量規則(2026-08 調整):scrape 抓回的內容若與現有檔案相同(除 lastUpdated)就不寫檔,
  故「更新 N」是真實變動數;「已結束但無比分」的賽事每月重試,但**名次已由 PDF 補齊者
  不再重試**,需要時用 `--full`。fetch_docs 對結束 60 天內的賽事仍重查,以追上官方換版的
  總成績紀錄;`regulation.pdf`/`resultPdf` 由 `sync_links()` 保持指向 documents 中的最新一份。
