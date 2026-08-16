# -*- coding: utf-8 -*-
"""tsbadminton.url.tw(中華民國全民羽球發展協會)賽事文件抓取。

主辦全民會長盃 / 世界清晨盃 / 羽您有約,三個系列每年一屆,歷史紀錄都留在站上。

用法:
    python scripts/scrape_tsba.py                # 增量:更新文件清單
    python scripts/scrape_tsba.py --full         # 重建所有賽事(含重抓日期)
    python scripts/scrape_tsba.py --dry-run      # 不寫檔
    python scripts/scrape_tsba.py --stage-results  # 下載成績圖片+名冊 xlsx 供視覺解析
    python scripts/scrape_tsba.py --no-index

這個站的兩個硬限制(實測):
1. **必須帶瀏覽器 User-Agent**,否則整站回 HTTP 500。
2. **附件有防盜連**:/upload_attach/ 只認同站 Referer,外站連過去一律 403。
   所以 documents[].url 一律指向明細頁 hot_xxx.html,不能直接放附件網址,
   否則使用者在 PWA 上點了必定失敗。

成績只有 JPG 圖片、賽程是空間排版的籤表 xlsx,都無法直接轉成逐場比分,
故本檔只負責「文件清單 + 賽事基本資料 + 把待解析素材備妥」;
名次由 /badminton-update 流程中的視覺解析 + tsba_reconcile.py 產生。
"""
import datetime
import json
import re
import sys
from datetime import date
from pathlib import Path

from fetch_docs import classify
from sources_common import SRC_TSBA, Http, city_from_text, write_if_changed

ROOT = Path(__file__).resolve().parent.parent
TOURN_DIR = ROOT / "docs" / "data" / "tournaments"
INBOX = ROOT / "inbox" / "tsba"

BASE = "https://www.tsbadminton.url.tw"
# (分類頁, 系列預設值)。hot_cg120086 是協會公告,沒有賽事文件,不收。
CATEGORIES = [
    ("hot_cg105163", "會長盃"),
    ("hot_cg100948", "清晨盃"),
    ("hot_cg118656", "羽您有約"),
    ("custom_cg45241", "會長盃"),
    ("custom_cg45240", "清晨盃"),
]
SERIES = [("會長盃", "會長盃"), ("清晨盃", "清晨盃"), ("羽您有約", "羽您有約")]
VENUE = "臺北市"  # 協會位於臺北市;實際場館各屆不同,由規程/秩序冊補

_LINK = re.compile(r'<a[^>]+href="((?:hot|custom)_\d+)\.html"[^>]*>(.*?)</a>', re.S)
_TAG = re.compile(r"<[^>]+>")
_ATTACH = re.compile(r"upload_attach/(\d+)\.(\w+)")
_IMAGE = re.compile(r"editor_images/([0-9a-f]+\.(?:jpe?g|png))", re.I)
_SCHEDULE_DOC = re.compile(r"賽程|秩序冊|分類表|名單")
_YEAR = re.compile(r"(20\d\d)\s*年")
_YEAR_BARE = re.compile(r"(20[0-3]\d)")   # 標題常寫「2026清晨盃」不帶「年」
_ROC = re.compile(r"(?:民國)?(1[01]\d)\s*年")
_EDITION = re.compile(r"第\s*([0-9〇零一二三四五六七八九十百]+)\s*屆")
_CN_NUM = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s):
    s = s.strip()
    if s.isdigit():
        return int(s)
    if "十" not in s:
        n = 0
        for ch in s:
            if ch not in _CN_NUM:
                return None
            n = n * 10 + _CN_NUM[ch]
        return n or None
    left, _, right = s.partition("十")
    tens = _CN_NUM.get(left, 1) if left else 1
    ones = _CN_NUM.get(right, 0) if right else 0
    return tens * 10 + ones


def parse_year(title):
    for pat, off in ((_YEAR, 0), (_ROC, 1911), (_YEAR_BARE, 0)):
        m = pat.search(title)
        if m:
            return int(m.group(1)) + off
    return None


def parse_series(title, default):
    for key, name in SERIES:
        if key in title:
            return name
    return default


def parse_edition(title):
    m = _EDITION.search(title)
    return _cn_to_int(m.group(1)) if m else None


def fetch_entries(http):
    """回傳 [{id, title, series}],掃所有分類頁的可見連結。"""
    seen, out = set(), []
    for page, series_default in CATEGORIES:
        url = f"{BASE}/{page}.html"
        try:
            html = http.get_text(url, referer=BASE + "/")
        except Exception as e:  # noqa: BLE001
            print(f"  [錯誤] 讀取 {page} 失敗: {e}")
            continue
        for pid, raw in _LINK.findall(html):
            title = _TAG.sub("", raw).strip()
            title = re.sub(r"\s+", " ", title)
            if not title or len(title) < 6 or pid in seen:
                continue
            # 排除導覽列(最新消息/會長盃羽球賽 等短標題已被長度濾掉)
            seen.add(pid)
            out.append({"id": pid, "title": title,
                        "series": parse_series(title, series_default)})
    return out


def fetch_detail(http, pid):
    """讀明細頁,取出附件/成績圖與發佈日期。

    /upload_attach/ 的檔名是 unix epoch(實測 1786090663 → 2026-08-07,與標題的
    0805 版本相符),拿來當文件日期,也用來替標題沒寫年份的文件推年份。
    """
    html = http.get_text(f"{BASE}/{pid}.html", referer=f"{BASE}/")
    att = _ATTACH.findall(html)
    imgs = _IMAGE.findall(html)
    when = ""
    if att:
        try:
            when = datetime.date.fromtimestamp(min(int(a) for a, _ in att)).isoformat()
        except (ValueError, OSError, OverflowError):
            when = ""
    return {
        "date": when,
        "attachments": [f"{BASE}/upload_attach/{a}.{b}" for a, b in att],
        "images": [f"{BASE}/editor_images/{i}" for i in imgs],
    }


def group_tournaments(entries, warn_unresolved=True):
    """把文件依 (年份, 系列) 歸戶成賽事。標題推不出年份的文件會被列出但不收錄。"""
    tours = {}
    unresolved = []
    for e in entries:
        # enrich() 可能已由附件 epoch 推定年份,優先採用
        year = e.get("year") or parse_year(e["title"])
        if not year:
            unresolved.append(e)
            continue
        openid = f"tsba-{year}-{e['series']}"
        t = tours.setdefault(openid, {"year": year, "series": e["series"],
                                      "edition": None, "docs": []})
        ed = parse_edition(e["title"])
        if ed and not t["edition"]:
            t["edition"] = ed
        t["docs"].append(e)
    if unresolved and warn_unresolved:
        for e in unresolved:
            print(f"  [提醒] 無法判定年份,未收錄: {e['title'][:56]}")
    return tours


def build_record(openid, info, existing):
    year, series = info["year"], info["series"]
    ed = info["edition"] or (existing or {}).get("_edition")
    name = f"{year}年" + (f"第{ed}屆" if ed else "") + {
        "會長盃": "全民會長盃羽球錦標賽",
        "清晨盃": "世界清晨盃暨吳文達紀念盃羽球錦標賽",
        "羽您有約": "「羽您有約」全國羽球公益賽",
    }[series]

    docs = [{"title": d["title"], "url": f"{BASE}/{d['id']}.html",
             "date": d.get("date", ""), "type": classify(d["title"], "")}
            for d in sorted(info["docs"], key=lambda x: x.get("date") or "",
                            reverse=True)]

    old = existing or {}
    start = info.get("dateStart") or old.get("dateStart", "")
    end = info.get("dateEnd") or old.get("dateEnd", "") or start
    today = date.today().isoformat()
    if end:
        status = "finished" if today > end else ("ongoing" if today >= start else "registering")
    else:
        # 沒抓到賽期就只能用年份判斷(往年的一律視為已結束)
        status = "finished" if year < date.today().year else "registering"
    return {
        "openid": openid,
        "source": SRC_TSBA,
        "name": name,
        "city": old.get("city") or city_from_text(VENUE),
        "status": status,
        "registerStart": old.get("registerStart", ""),
        "registerEnd": old.get("registerEnd", ""),
        "dateStart": info.get("dateStart") or old.get("dateStart", ""),
        "dateEnd": info.get("dateEnd") or old.get("dateEnd", ""),
        "venue": old.get("venue", ""),
        "image": old.get("image", ""),
        "isSystem": False,
        "sourceUrl": f"{BASE}/{CATEGORIES[0][0]}.html" if series == "會長盃" else
                     f"{BASE}/{'hot_cg100948' if series == '清晨盃' else 'hot_cg118656'}.html",
        "category": "錦標賽",
        "promotion": old.get("promotion"),
        "regulation": old.get("regulation"),
        "groups": old.get("groups", []),
        "matches": old.get("matches", []),
        "standings": old.get("standings", []),
        # 參賽名單:讓沒得名的選手也查得到(來源是賽前籤表,見 build_entries)
        "entries": info.get("entries") if info.get("entries") is not None
                   else old.get("entries", []),
        "entriesCoverage": (info.get("entriesCoverage")
                            if "entriesCoverage" in info
                            else old.get("entriesCoverage")),
        "resultPdf": old.get("resultPdf"),
        "documents": docs,
        "lastUpdated": date.today().isoformat(),
    }


def load_existing(openid):
    p = TOURN_DIR / f"{openid}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def known_doc_dates():
    """{明細頁 url: 日期} — 已抓過的文件日期,避免每月重抓所有明細頁。"""
    out = {}
    for p in TOURN_DIR.glob("tsba-*.json"):
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for d in t.get("documents") or []:
            if d.get("date"):
                out[d["url"]] = d["date"]
    return out


def enrich(http, entries, use_detail=True):
    """補上每份文件的日期,並替標題沒年份的文件推年份(都靠明細頁的附件 epoch)。"""
    cache, known = {}, known_doc_dates()

    def detail(pid):
        if pid not in cache:
            try:
                cache[pid] = fetch_detail(http, pid)
            except Exception as e:  # noqa: BLE001
                print(f"  [警告] 讀取明細頁 {pid} 失敗: {e}")
                cache[pid] = {"date": "", "attachments": [], "images": []}
        return cache[pid]

    fetched = 0
    for e in entries:
        e["year"] = parse_year(e["title"])
        e["date"] = known.get(f"{BASE}/{e['id']}.html", "")
        if not use_detail or (e["date"] and e["year"]):
            continue
        d = detail(e["id"])
        fetched += 1
        e["date"] = e["date"] or d["date"]
        if not e["year"] and d["date"]:
            e["year"] = int(d["date"][:4])
            print(f"  [提醒] 由附件日期推定年份 {e['year']}: {e['title'][:44]}")
    if fetched:
        print(f"  (讀取 {fetched} 個明細頁補日期)")
    return cache


def get_schedule_xlsx(http, openid, info, cache):
    """下載該屆的賽程表/秩序冊 xlsx 並存到 inbox/tsba/{openid}/roster.xlsx。

    賽期與參賽名冊都只能從這份檔案取得,所以抓一次存起來共用。
    只收 .xlsx;早年的 .xls 是舊二進位格式,openpyxl 讀不了。
    """
    dest = INBOX / openid / "roster.xlsx"
    if dest.exists():
        return dest
    for d in sorted(info["docs"], key=lambda x: x.get("date") or "", reverse=True):
        if not _SCHEDULE_DOC.search(d["title"]):
            continue
        det = cache.get(d["id"])
        if det is None:
            try:
                det = cache[d["id"]] = fetch_detail(http, d["id"])
            except Exception:  # noqa: BLE001
                continue
        xlsx = [u for u in det["attachments"] if u.lower().endswith(".xlsx")]
        if not xlsx:
            continue
        try:
            raw = http.get(xlsx[0], referer=f"{BASE}/{d['id']}.html")
        except Exception as e:  # noqa: BLE001
            print(f"  [警告] {openid} 下載賽程 xlsx 失敗: {e}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        return dest
    return None


def fill_dates_from_schedule(http, openid, info, cache, existing):
    """賽事日期只有賽程表 xlsx 裡才有(列表頁的日期是公告有效期,不是賽期)。

    只在還沒有日期時抓一次,抓到就寫進 JSON,之後不再下載。
    """
    if (existing or {}).get("dateStart"):
        return
    from tsba_xlsx import extract_dates  # noqa: PLC0415
    path = get_schedule_xlsx(http, openid, info, cache)
    if not path:
        return
    try:
        start, end = extract_dates(path, info["year"])
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] {openid} 讀取賽程 xlsx 失敗: {e}")
        return
    if start:
        info["dateStart"], info["dateEnd"] = start, end
        print(f"  [日期] {openid} {start} ~ {end}(取自賽程表)")


# 抽取數低於宣告數的這個比例,視為解析失敗(排版不對),整組不收錄。
# 門檻不設高:低於宣告數但內容正確的組別仍然有價值——名單的用途是
# 「讓沒得名的選手查得到」,整組丟掉只會製造更多查不到的人。
ENTRY_MIN_RATIO = 0.5


def build_entries(path, warn=None):
    """由賽程表 xlsx 產生 entries[],並回傳 (entries, 覆蓋率, 被淘汰的組別)。"""
    from tsba_xlsx import declared_counts, extract_roster  # noqa: PLC0415
    roster = extract_roster(path)
    declared = declared_counts(path)

    entries, dropped = [], []
    exp_total = got_total = 0
    for group, rows in sorted(roster.items()):
        dec = declared.get(group)
        expected = 0
        if dec:
            expected = dec[0] * 2 if dec[1] in ("組", "隊") else dec[0]
        if not rows or (expected and len(rows) < expected * ENTRY_MIN_RATIO):
            dropped.append((group, len(rows), expected))
            continue
        if expected:
            exp_total += expected
            got_total += min(len(rows), expected)
        for unit, name in rows:
            entries.append({"group": group, "unit": unit,
                            "members": [name], "source": "draw"})
    # 宣告數為 0 的組別無從驗證,覆蓋率只反映有宣告數的部分
    coverage = round(got_total / exp_total, 3) if exp_total else None
    if dropped and warn is not None:
        warn.extend(dropped)
    return entries, coverage, dropped


def stage_results(http, tours, cache, only=None):
    """把成績圖片與同屆賽程 xlsx 的名冊備妥,供 /badminton-update 視覺解析。

    成績只有 JPG。但同屆賽程表 xlsx 裡有全部參賽者的真文字(單位+姓名),
    先把名冊抽出來當校對字典,讀圖時就從已知名單挑,而不是自由辨識。
    """
    from tsba_xlsx import extract_roster  # noqa: PLC0415
    INBOX.mkdir(parents=True, exist_ok=True)
    worklist = []
    for openid in sorted(tours):
        if only and openid not in only:
            continue
        info = tours[openid]
        result_docs = [d for d in info["docs"] if "成績" in d["title"]]
        if not result_docs:
            continue
        out_dir = INBOX / openid
        images, roster_path = [], None

        for d in result_docs:
            det = cache.get(d["id"]) or fetch_detail(http, d["id"])
            cache[d["id"]] = det
            for n, url in enumerate(det["images"], 1):
                dest = out_dir / f"{d['id']}_{n:02d}{Path(url).suffix.lower()}"
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(http.get(url, referer=f"{BASE}/{d['id']}.html"))
                images.append(str(dest.relative_to(ROOT)).replace("\\", "/"))

        # 名冊:找同屆的賽程/秩序冊 xlsx
        for d in sorted(info["docs"], key=lambda x: x.get("date") or "", reverse=True):
            if not _SCHEDULE_DOC.search(d["title"]):
                continue
            det = cache.get(d["id"]) or fetch_detail(http, d["id"])
            cache[d["id"]] = det
            xl = [u for u in det["attachments"] if u.lower().endswith(".xlsx")]
            if not xl:
                continue
            dest = out_dir / "roster.xlsx"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                dest.write_bytes(http.get(xl[0], referer=f"{BASE}/{d['id']}.html"))
            try:
                roster = extract_roster(dest)
            except Exception as e:  # noqa: BLE001
                print(f"  [警告] {openid} 名冊抽取失敗: {e}")
                break
            roster_path = out_dir / "roster.json"
            roster_path.write_text(json.dumps(
                {g: [list(x) for x in v] for g, v in roster.items()},
                ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  [名冊] {openid} {len(roster)} 組 / "
                  f"{sum(len(v) for v in roster.values())} 筆")
            break

        if images:
            worklist.append({
                "openid": openid, "name": info.get("name", openid),
                "images": images,
                "roster": (str(roster_path.relative_to(ROOT)).replace("\\", "/")
                           if roster_path else None),
            })
            print(f"  [成績圖] {openid} {len(images)} 張"
                  f"{'(無名冊可校對)' if not roster_path else ''}")

    (INBOX / "worklist.json").write_text(
        json.dumps(worklist, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n待解析:{len(worklist)} 場,清單寫入 inbox/tsba/worklist.json")


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    TOURN_DIR.mkdir(parents=True, exist_ok=True)

    http = Http()
    entries = fetch_entries(http)
    cache = enrich(http, entries, use_detail="--no-detail" not in argv)
    tours = group_tournaments(entries)
    print(f"tsbadminton:文件 {len(entries)} 筆 → 賽事 {len(tours)} 場")

    if "--stage-results" in argv:
        only = None
        if "--only" in argv:
            only = {x.strip() for x in argv[argv.index("--only") + 1].split(",") if x.strip()}
        for openid, info in tours.items():
            info["name"] = build_record(openid, info, load_existing(openid))["name"]
        stage_results(http, tours, cache, only)
        return

    want_entries = "--build-entries" in argv
    new_count = updated = unchanged = 0
    for openid in sorted(tours):
        existing = load_existing(openid)
        info = tours[openid]
        if "--no-detail" not in argv:
            fill_dates_from_schedule(http, openid, info, cache, existing)
        # 參賽名單:只在還沒有時抓一次(比照 dateStart 的作法)
        if want_entries and not (existing or {}).get("entries"):
            path = get_schedule_xlsx(http, openid, info, cache)
            if path:
                try:
                    ents, cov, dropped = build_entries(path)
                except Exception as e:  # noqa: BLE001
                    print(f"  [警告] {openid} 名單抽取失敗: {e}")
                else:
                    info["entries"], info["entriesCoverage"] = ents, cov
                    covtxt = f"{cov:.0%}" if cov is not None else "無宣告數可驗證"
                    print(f"  [名單] {openid} {len(ents)} 筆 / 覆蓋 {covtxt}"
                          f"{f',淘汰 {len(dropped)} 組' if dropped else ''}")
                    for g, got, exp in dropped[:5]:
                        print(f"      [淘汰] {g}:抽到 {got} 筆,宣告 {exp}")
            elif not (existing or {}).get("entries"):
                print(f"  [提醒] {openid} 沒有可用的 .xlsx 賽程表,無法建立參賽名單")
        record = build_record(openid, info, existing)
        label = "新增" if existing is None else "更新"
        detail = f"{len(record['documents'])} 份文件"
        if dry:
            print(f"  [{label}(dry)] {openid} {record['name']} ({detail})")
            continue
        if not write_if_changed(TOURN_DIR / f"{openid}.json", record, existing):
            unchanged += 1
            continue
        if existing is None:
            new_count += 1
        else:
            updated += 1
        print(f"  [{label}] {openid} {record['name']} ({detail})")

    print(f"\ntsbadminton 完成:新增 {new_count}、更新 {updated}、無變化 {unchanged}")
    if dry or "--no-index" in argv:
        return
    print("重建索引…")
    import rebuild_index  # noqa: PLC0415
    rebuild_index.main()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
