# 羽球賽事資料庫(PWA + 本地留底)

台灣羽球賽事資料庫:賽事資訊、逐場比分、各組名次。核心查詢是「以學校/單位為中心」與
「以選手為中心」的跨賽事查詢。資料來源 mylivescore.tw,每月以 `/badminton-update` 手動更新,
歷史 PDF 以 `/badminton-import` 匯入。

## 架構

- `docs/` — PWA(GitHub Pages 站台根目錄;vanilla JS、無 build step、繁中)
  - 頁面:index(賽事列表)、search(選手+單位搜尋)、unit、player、tournament、about
  - `data/index.json` 賽事摘要|`data/players.json` 選手索引|`data/units.json` 單位索引
  - `data/tournaments/{openid}.json` 單場賽事完整資料(含逐場比分與官方文件連結)
  - 官方 PDF **不留底**(使用者決定):documents/regulation.pdf/resultPdf 都是外部 URL
- `scripts/scrape.py` — API 抓取+組別標籤+名次推導(`--full` 全量)。結尾自動跑 rebuild_index
- `scripts/rebuild_index.py` — 由 tournaments/*.json 重建三個索引(匯入後必跑)
- `inbox/` — 待匯入 PDF 暫放

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
python scripts/scrape.py          # 每月增量更新
python scripts/scrape.py --full   # 全量重抓
python scripts/fetch_docs.py      # 更新官方文件連結(增量,不下載)
python scripts/rebuild_index.py   # 重建索引(匯入後)
python -m http.server 8765 -d docs   # 本地預覽
```

部署:GitHub Pages(main branch /docs)。更新資料後 `git add -A && git commit && git push` 即上線。

## 注意

- Windows console 編碼:python 一律 `-X utf8`,stdout 需 reconfigure(腳本已內建)。
- players.json ~7MB(gzip 後約 1.5MB),前端僅在搜尋頁 lazy-load;若持續成長可考慮分片。
- 名次與比分為自動推導/抓取,about 頁已標注「以官方公告為準」。
