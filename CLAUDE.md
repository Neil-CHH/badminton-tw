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

**第四個來源不建賽事,只補內容**:全運會/全中運的官方競賽資訊系統
(`scrape_sportgov.py`,見下文)。那兩場賽事本來就以 mylivescore 的 openid 在庫裡、
只是 API 回空,所以一律**就地補進既有賽事檔**(`source` 維持 mylivescore)。
另建一筆會變成同一場賽事兩張卡片,正是 dedupe 在處理的老問題。

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

**以獲獎內容比對(2026-08 補)**:上面那套只看賽名,有兩個結構性盲點 —— 同來源一律跳過、
主辦把同一場賽事取兩個完全不同的名字時永遠配不上。`award_overlap_candidates()` 改看實質內容:
日期區間有交集,且 `(選手, 正規化組別, 名次)` 三元組交集 ≥ 4 筆、÷ 較小一方 ≥ 0.15。
**這條不看賽名也不看來源**,實測全庫只命中 1 組、零誤判(門檻放寬到 0.15/4 仍只有那 1 組):

> `628963`「114年度菁英盃全國羽球錦標賽」(342 場比分、名次全 derived)與
> `630550`「114年高雄市羽球社區聯誼賽第5站、第6站」(0 場比分、名次全 pdf)——
> 同場館同日期、組別是子集、22 筆獲獎完全相同,但**賽名相似度只有 0.40**。

命中**只報告不自動刪**(同來源有可能是同場館同週末的兩個賽事,要人工判斷)。shadow 帶著
更權威名次時不能直接刪,用 `dedupe.py --merge <shadow> <canonical>` 先把名次
`merge_standings` 進 canonical 再刪檔登錄。

**正規化契約(最重要)**:`rebuild_index.py` 與 `docs/tournament.html` 都直接讀 mylivescore
原始 match 形狀。新來源必須輸出同樣 14 個 key(全為字串):`groupName / match / date /
time / teamA / teamB / matchtype / stadium / winner / Asidescore / Bsidescore / abstain /
HeadGroup / scoreinfo[]`。沒對上不會報錯,而是**靜默產生空的選手/單位統計**——
改動抓取程式後,務必抽一位該賽事的選手確認勝負統計不是空的。

## 架構

- `docs/` — PWA(GitHub Pages 站台根目錄;vanilla JS、無 build step、繁中)
  - 頁面:index(賽事列表)、search(選手+單位搜尋)、unit、player、tournament、about
  - `data/index.json` 賽事摘要|`data/search-index-players.json` +
    `data/search-index-units.json` 輕量搜尋索引(搜尋頁載兩檔、單位頁只載 units 那檔)
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
- `scripts/scrape_sportgov.py` — 全運會/全中運官方競賽資訊系統 → 逐場比分 + 官方頒獎名單
  (`official`);資格賽只有籤表 PDF,收 `entries[]`。**不在 update_all 裡**,新一屆才手動跑
- `scripts/tsba_xlsx.py` — 由籤表 xlsx 抽參賽名冊與比賽日期
- `scripts/tsba_reconcile.py` — 成績圖解析結果 × 名冊校對 → `source:"ocr"` 的 standings
- `scripts/dedupe.py` — 跨來源重複賽事偵測與刪檔(重建索引前跑);
  登錄檔 `scripts/duplicates.json` 同時是爬蟲的阻擋清單
- `scripts/parse_result_pdf.py` — **官方總成績紀錄 PDF → standings(source=pdf)**,
  自動解析成績總表(mylivescore 的 PDF 是可抽文字的向量表格,不必視覺判讀);
  成績總表不在 mylivescore、只能人工取得的少數賽事登錄在 `LOCAL_SUMMARY` → 讀 `Ref/`
  的本地檔(pdf 或 xlsx 版面完全一樣,共用同一支 `scan_table`)
- `scripts/rederive_standings.py` — 用現有比分重跑 derive_standings(改過推導規則後跑)
- `scripts/pdf_backfill_list.py` — 列出「有官方 PDF 但名次仍非 pdf」的待補清單
- `scripts/rebuild_index.py` — 由 tournaments/*.json 重建索引與分片(匯入後必跑)
- `scripts/verify_data.py` — 資料健檢(連結一致性/索引新鮮度/分片落點/亂碼/缺口),只讀不寫
- `inbox/` — 待匯入 PDF 與 tsba 待解析素材暫放(整個目錄 gitignore)
- `Ref/` — **進版控**的少數官方文件:網路上沒有穩定連結、只能人工取得,而程式又要靠它
  重跑(`parse_result_pdf.LOCAL_SUMMARY`)。這是「官方 PDF 不留底」的唯一例外

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

## 全運會 / 全中運官方競賽資訊系統(2026-08 偵察,以 scrape_sportgov.py 為準)

主辦縣市自架的「運動賽會競賽資訊系統」,兩屆用**同一套 CMS**(路徑、參數、欄位全同),
只有 host 不同:114 全運會 `sport114.yunlin.gov.tw`、115 全中運 `sport115.cyc.edu.tw`。
新一屆就在 `SITES` 加一筆(host + openid + 項目標題對照)再跑一次。

- 端點(`LID` 是運動種類,羽球固定 202):`Score/Instant_List.php?LID=202` 場次清單 →
  `Score/InstantScore.php?FID=&PID=` 逐場成績 → `Score/Instant_ListDetail.php?SSID=`
  團體賽逐點明細 → `Score/Finals_Score.php?FID=` 頒獎名單(名次+單位+選手)。
- **PID 才是「項目」的穩定鍵**,FID 是「項目 × 比賽日」;同 PID 的場次要合成一組。
  只有最後一天的 FID 有頒獎名單連結。
- 項目標題用**整串對照表**換成組別名(對不上就報錯停下,不用拆字規則猜)。全中運沿用
  570139(114 年會內賽)的簡寫「高男團 / 國女單」,兩屆的組別名才對得起來。
- **輪次不可以自己往前推**:全運團體是兩組循環賽再交叉、全運個人是 16 籤全程排名賽
  (輸的人繼續打到定出 1~8 名)、全中運是乾淨的 8 籤淘汰 —— 同樣是「決賽的前一場」,
  前者是循環賽、後者是八強。只認兩件事:雲林站備註欄寫的「第1.2名/第3.4名/第5.6名/
  第7.8名」,以及嘉義站(備註欄全空)由**官方頒獎名單**認出的第 1/2 名與第 3/4 名交手場
  (還要是雙方各自的最後一場)。再往前只補一層「決賽雙方的上一場 = R4」。
- **個人賽同一縣市可以派兩組人**(114 全運男單澎湖縣的王柏崴與劉韋奇同時打準決賽),
  比對「哪一方」時只認單位會撞在一起,要連姓名一起認。
- 局分格是「上行甲方各局、下行乙方各局」,但實測 3/252 列被主辦填成**轉置**的
  「一行 = 一局」(會讀出 21:21 這種平局)。兩種讀法都算一次,拿官方「成績」欄裁決。
- 單位格偶爾接上出賽狀態(「臺南市 請假」)要剝掉;而且狀態字**有時掛在贏的那一方**
  (114 全運男雙第 5 場高雄市標傷退卻 2:0 勝),`abstain` 只登錄輸的那一方。
- W/O 一分未打也**要留一列 scoreinfo** —— 選手名只存在於 scoreinfo,整列略過會讓
  那兩位選手在這場憑空消失;棄權方的分數沿用庫裡既有寫法「棄」。
- **資格賽不進這套系統**:成績只有掛在賽事消息頁附件的籤表 PDF,而且官方「成績總表」
  列的是**晉級名單、不是 1~N 名** → 只收 `entries[]`,不產生 standings。籤表的
  「序號 縣市」+ 下一行姓名可以穩定抽出席位,雙打的搭檔寫在席位行**之前**、中間常插
  進賽程節點文字,所以往回找「同頁、同名的裸縣市行」而不是固定往回數幾行。抽到的
  席位數要對得上籤表標題宣告的人數/組數才收(實測 5 個項目 41/35/40/33/38 全中)。
- 補進來的官方頁面連結在 `documents[]` 帶 `source: "sportgov"`,`fetch_docs` 看到這個
  標記就不會在重抓 links.php 時把它整包洗掉。

## 資料模型重點

- `matches[]` 為 API schedule 原樣:teamA/B=單位名(自由填寫,同校多種寫法,**不做正規化**,
  前端用子字串模糊比對 + 變體合併);scoreinfo[].memberA/B=選手名(雙打以 - 或 / 連接)。
- `matchtype` 代號:`預賽`、`R{n}` 淘汰賽(R2=決賽、R34=三四名戰)、`F2/F3/F4` 循環決賽。
  **非賽制場次不收錄**(`scrape_lapgo.MATCHTYPE_DROP`:友誼賽/表演賽/熱身賽/交流賽)——
  表演性質的勝負不該計進選手戰績(lapgo-153 律師盃邀請賽 9 場團體友誼賽)。整場略過,
  組別與名次不受影響(名次來自官方成績總表)。略過時會印 `[略過] … 非賽制場次 N 場`,
  這樣「整場都是友誼賽 → 0 場比賽」才不會被誤判成 API 抓取失敗。
- 名次推導(scrape.py derive_standings):R2 勝負=1/2 名、R34=3/4 名、F3/F4 依勝場數。
  **一組同時有 R2 與 F2/F3/F4 = 主籤 + 5~8 名安慰賽,不是循環決賽**(2026-08 修,實測 52 組
  中招):舊寫法把 R2 和 F2 混成 finals 而落進循環賽分支,結果 1~4 名全發給安慰賽選手、
  真正的冠軍一個名次都沒有(277843 國小中年級女生組單打:決賽高紫芸勝,derived 卻給了
  安慰賽冠軍賴品喬)。有 R2 就以 R2 為決賽,F 系列整組忽略。
  官方規則是「依報名組數取 N 名」,決賽敗者不一定有名次 → **PDF 匯入的名次(source=pdf)
  永遠優先**,同組別覆蓋 derived。
- **一組一隊只能有一個名次**(2026-08 修,原本會讓選手的獲獎數翻倍):
  - 循環決賽的 `_entry_key` **團體賽只能以單位為鍵** —— `side_entry` 的 members 取自
    「那一輪」的 scoreinfo,團體賽每輪先發不同,把 members 放進鍵裡會讓同一隊裂成好幾筆、
    各拿一個名次(672515 的田中高中B 同時有第 2 與第 4 名)。個人賽反過來不能只用單位,
    LAPGO 的 unit 常是空字串。
  - **決賽兩隊之一又出現在三四名戰 → 整場 R34 略過**:來源籤表自相矛盾(619483、494323
    的 R4 敗者跑去打決賽),硬給名次會讓同一隊同時是冠軍和季軍。寧可留空號讓 verify 報出來。
  - `rebuild_index` 還有一道保險:同賽事、同正規化組別、同選手只留**最佳名次**。爬蟲修好
    只對重抓後的資料生效,LAPGO 主辦成績總表自己填錯的(lapgo-100 一般男雙同時有第 1、
    第 2 名)只有這層擋得住。同一列 `members[]` 內同名出現兩次(團體賽列出多組配對)也在
    這裡去重。**單位名次不可以 `(單位, 組別)` 去重** —— 同校在同組拿第 1 和第 2 是正常的。
- 手動匯入的歷史賽事 openid 格式:`manual-{YYYY}-{slug}`。
- **「取 N 名」只存在於官方文件,推導永遠猜不到**(2026-08 實測):同一場賽事裡各組取的
  名次數不一樣 —— 257164 新羽盃 11 組中,取 1 名 2 組、取 2 名 4 組、取 4 名(1/2/並列 3)
  5 組,而籤表形狀完全一樣。全庫對照官方名次也證實:同樣有八強籤的組別,官方取到第 3 名
  的 85 組、取到第 5 名的 59 組,五五波。→ **有總成績 PDF 就一律以 PDF 為準**,
  沒有的才退回推導,清單用 `pdf_backfill_list.py` 追蹤。
- **並列名次是主流**:官方資料裡 191 組 official + 62 組 pdf 是 `[1,2,3,3]`(兩個並列
  第三、沒有殿軍),`[1,2,3,3,5,5,5,5]` 也很常見。同組同名次出現多筆是正常的。
- **成績總表的儲存格有換行時,有兩種完全不同的意思**,只能靠比分資料分辨:
  (A) 單位名太長被排版折行 ——「臺中市北屯區四維國民小」+「學」,接回去才是單位名;
  (B) 單位與選手擠在同一格 ——「合庫北市大」+「王子維」,最後一行是選手。
  判準是「最後一行是不是這場出現過的選手」。**不能反過來拿「接起來是不是已知單位」判斷**:
  官方 PDF 寫全稱、比分寫簡稱(「四維國小」),對不上會讓團體組全數落空。完全沒有比分可
  對照時(排名賽/全大運 API 回空)才退而用組別名判斷賽制(單打取 1 名、雙打 2 名、團體 0 名)。
- **防線不可以用「字數上限」或「賽制字眼」判斷人名是否合理** —— 實測會誤殺真資料:
  原住民姓名「伊斯瑪哈撒嗯拉嘎夫」9 個字,而本庫隊名極自由、本來就常含賽制字眼
  (「可以靠553拿名次嗎」「單打手的驕傲」「臺中羽球單打團」)。可用的判準是
  「姓名裡包住了單位名」。人工逐列判讀過的賽事(兩支 import_*_standings.py)一律整場跳過。
- **成績總表的表格結構有四個陷阱**(2026-08 修,全庫 226 份 PDF 中 19 場中招,
  失敗方式全是**靜默少收**、不會報錯 —— 改動這支程式後務必比對「組數 × 每組名次數」):
  1. **一個名次表頭底下可以接連好幾個組別**,每組各佔一列(排名賽甲組五個項目共用一個
     「第一名…第八名」表頭)。舊寫法讀完第一組就跳兩三列,其餘整批消失
     (259196 只收到甲組男單與乙組男雙,17 筆;實際是 9 組 72 筆)。
  2. **水平合併的名次表頭格 = 並列名次**,抽文字只會拿到 `['第三名','']`,後面那欄
     看起來沒名次就被丟掉。要看儲存格框線的 bbox 橫跨幾欄(乙組男單 `[1,2,3,3,5,5,5,5]`)。
  3. **分組標籤在表格外面**:「(一)甲組:」「(二)乙組:」是段落標題不是表格內容,
     不撈進來兩張表的項目欄都寫「男子組單打」而撞名互相蓋掉。全大運的「公開組/一般組」
     同理但寫法不同,目前不認 → 靠「同名組別在兩個區塊名次相交就交還人工」擋住。
  4. **「項目」可能佔兩欄**:左欄年齡組(U19/高中組,用 rowspan 併格)、右欄項目
     (男子單打)。只讀左欄會讓單位與姓名整串錯位(788659 曾寫進 13 筆亂碼)。
     判準要兩個條件同時成立:第 1 欄沒有自己的名次表頭,且併進來之後組別名才判得出賽制。
  另:**雙打搭檔分屬兩校時,儲存格上半是兩個單位、下半是兩個姓名**,接起來會黏成
  「國體大彰師大」這種不存在的單位;既有寫法是用全形斜線並列「國體大／彰師大」。
  重跑時**舊的 pdf 名次要整批清掉再寫**(不能只換這次解析到的組別)—— 規則修好後組別名
  常跟著變,舊名的錯誤名次會留著變成兩套並存、選手獲獎翻倍。

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
python scripts/scrape_sportgov.py --dry-run   # 全運會/全中運官方系統(--apply 才寫檔)
python scripts/fetch_docs.py      # 更新官方文件連結(增量,不下載;只對 mylivescore)
python scripts/fetch_docs.py --relink   # 不打 API,只重新同步 regulation.pdf/resultPdf
python scripts/dedupe.py --dry-run  # 看重複判定(含獲獎內容比對),不動檔
python scripts/dedupe.py          # 刪除重複賽事檔並登錄(update_all 已內含)
python scripts/dedupe.py --merge 630550 628963   # 先併名次再刪 shadow(人工確認後才跑)
python scripts/parse_result_pdf.py --openid X          # 解析官方總成績 PDF(只看報告)
python scripts/parse_result_pdf.py --openid X --file Ref/成績總表.xlsx --apply  # 讀本地檔
python scripts/parse_result_pdf.py --all --apply       # 全庫套用(--force 連已是 pdf 的也重跑)
python scripts/pdf_backfill_list.py                    # 待補 PDF 清單
python scripts/rederive_standings.py --apply           # 改過推導規則後重推(不連網)
python scripts/rebuild_index.py   # 重建索引(匯入後)
python scripts/verify_data.py --summary  # 健檢+新增賽事摘要(部署前跑,exit 1 表示要修)
python -m http.server 8765 -d docs   # 本地預覽
```

部署:GitHub Pages(main branch /docs)。更新資料後 `git add -A && git commit && git push` 即上線。

## 注意

- Windows console 編碼:python 一律 `-X utf8`,stdout 需 reconfigure(腳本已內建)。
- **跨語言契約有三個,改一邊就要改另一邊**:`rebuild_index.shard_of` ↔ `common.js shardOf`、
  mylivescore 的 14 個 match key、`sources_common.effective_dates` ↔ `common.js effectiveDates`
  (含 `DATE_OVERRIDES` 那張表)。
- 賽事日期(2026-08):少數賽事的 `dateStart` 是錯的(整串複製到別場、日期反轉、
  `0000-00-00`),會讓列表排序與年份篩選錯位。`effective_dates` 只在**與實際比分日期完全
  沒有交集時**才改用 `matches[].date` 推得的區間(目前 5 場),「公告 2/28 起、實際 3/1
  開打」這種正常落差一律保留原值。**賽事 JSON 本體不改寫**(保持來源忠實,也避免爬蟲每月
  覆寫→rebuild 再補寫的 git 噪音),所以直接讀賽事檔的 tournament.html 必須自己套同一條規則。
  沒有比分可以回推的錯誤日期只能明列在 `DATE_OVERRIDES`(目前 1 場:531555「114年全中運
  資格賽」標成 2026 年,害它排在「115年…資格賽」前 6 天、看起來像同一場收了兩次)。
  **不可用「賽名年份 vs dateStart 年份」的通則** —— 4 筆不一致裡有 3 筆(342339、507058、
  648140)是「賽事叫 2026 年但前一年 12 月就開打」的正常情形。
- **一場賽事在多個組別得名不是重複**:單打+雙打、個人+團體本來就是不同獎項(廖宥睿在
  746611 拿男雙冠軍、單打團體亞軍、雙打團體亞軍,官方規程確認是三個獨立報名的項目)。
  但 player.html 的 🏆 成績總覽每列都印賽名,同一個賽名連著出現好幾次很容易被當成資料重複,
  所以賽事欄用 `rowspan` 併成一格,標題也寫成「N 個獎項・M 場賽事」。
- **檔案大小一律看 gzip,不要看磁碟上的 raw** —— GitHub Pages 對 .json 有 gzip,
  實際傳輸約是 raw 的 1/3(search-index-players 2043KB→669KB、players 分片 1198KB→196KB)。
  `rebuild_index` 印的是 raw,拿它評估使用者流量會高估三倍。
- 選手/單位索引已分片(2026-07):選手/單位頁依名稱雜湊載對應分片(16 片,gzip 最大
  players 196KB / units 85KB)。選手勝負統計(w/l)由 rebuild_index 預算進分片。
- **搜尋索引拆成 players / units 兩檔(2026-08)**:`unit.html` 的 `variantsOf()` 要掃過
  全部單位名找變體(「崑山國小」vs「南市崑山國小」),所以整份 units 索引都要,但
  **players 那半用不到** —— 合成一檔時單位頁得白載 669KB。拆檔後單位頁首載
  960KB→274KB(gzip);搜尋頁兩檔並行載入,總量不變。
  各頁面實際載入(gzip):index 18KB、選手頁 ~215KB、搜尋頁 ~840KB、單位頁 ~274KB。
- 成長速度(2026-08 量測):例行更新讓 search-index 每月增約 40KB raw;
  2026-08-16 從 1734→2415KB 那一跳是加 tsba 來源與 `entries[]` 的一次性結構增量,不是失控。
  下一個要動的門檻是分片數 16→32(選手頁 196→100KB),但那要同步改
  `rebuild_index.shard_of` 與 `common.js shardOf`,選手數再翻倍時再說。
- sw.js shell 快取為 stale-while-revalidate,改前端後不需手動升 VERSION —— **例外是
  「刪掉或改名資料檔」**(如 2026-08 拆 search-index):舊 HTML 還在 shell 快取裡,會去要
  一個已 404 的檔,要升 VERSION 強制換掉 shell+data 快取(代價是全體重載一次)。data 為
  network-first + 3.5s timeout 退回快取。**兩種策略的 fetch 都必須帶
  `{ cache: "no-cache" }`**(2026-08 修):GitHub Pages 對所有檔案都回
  `Cache-Control: max-age=600`,不指定的話 fetch 會直接吃瀏覽器的 HTTP 快取而不碰網路,
  network-first 會退化成「更新後 10 分鐘內都還是舊資料」。實測帶 ETag 驗證回 304、
  下載 0 bytes(完整下載是 199KB),所以幾乎不花流量。
- 名次與比分為自動推導/抓取,about 頁已標注「以官方公告為準」。
- 增量規則(2026-08 調整):scrape 抓回的內容若與現有檔案相同(除 lastUpdated)就不寫檔,
  故「更新 N」是真實變動數;「已結束但無比分」的賽事每月重試,但**名次已由 PDF 補齊者
  不再重試**,需要時用 `--full`。fetch_docs 對結束 60 天內的賽事仍重查,以追上官方換版的
  總成績紀錄;`regulation.pdf`/`resultPdf` 由 `sync_links()` 保持指向 documents 中的最新一份。
