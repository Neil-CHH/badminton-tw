# -*- coding: utf-8 -*-
"""抓取各賽事的官方文件連結(競賽規程/總成績紀錄/賽程表等),寫進 tournament JSON
的 documents 欄位。只記連結不下載(使用者決定不留底,app 直接外連原始 PDF)。

資料來源:go.mylivescore.link 的 news API(每場賽事的「最新消息/賽事文件」)。

用法:
    python scripts/fetch_docs.py            # 增量:已結束逾 60 天且已有成績連結的賽事跳過
    python scripts/fetch_docs.py --force    # 重新檢查所有賽事
    python scripts/fetch_docs.py --relink   # 不打 API,只由既有 documents 重新同步
                                            # regulation.pdf / resultPdf 指向最新一份
"""
import json
import re
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
TOURN_DIR = DATA_DIR / "tournaments"

LINK_API = "https://go.mylivescore.link/links.php"
LINK_ORIGIN = "https://go.mylivescore.link"

# 官方常在賽後數週把總成績紀錄換成新版(新檔名=新 URL),這段期間即使已有成績連結仍重查,
# 否則舊連結會永遠停在失效的版本。
RECHECK_DAYS = 60

_token = ""


def api(payload, retries=2):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(LINK_API, data=body, method="POST")
    req.add_header("Origin", LINK_ORIGIN)
    req.add_header("User-Agent", "Mozilla/5.0 (badminton-db scraper)")
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read().decode("utf-8", errors="replace")
            return json.JSONDecoder(strict=False).decode(raw)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"API 失敗 {payload.get('api')}: {last}")


def get_token():
    global _token
    d = api({"Account": "linktree", "api": "gtoken"})
    if d.get("success") != "1":
        raise RuntimeError(f"token 失敗: {d}")
    _token = d["result"]


def news(openid):
    if not _token:
        get_token()
    d = api({"OpenID": openid, "api": "news", "token": _token})
    if d.get("result") in ("Time out.", "no token."):
        get_token()
        d = api({"OpenID": openid, "api": "news", "token": _token})
    if d.get("success") != "1":
        return []
    return (d.get("result") or {}).get("info", []) or []


def classify(title, link):
    text = (title or "") + " " + (link or "")
    if re.search(r"規程|簡章|章程|競賽辦法", text):
        return "規程"
    if re.search(r"成績", text):
        return "成績"
    return "其他"


def sync_links(t):
    """由 documents 同步 regulation.pdf 與 resultPdf,回傳是否有變動。

    documents 由 API 依日期新→舊排序,故各類型取第一份即最新。既有值若仍在 documents
    中就保留(同賽事可能有多份規程,如學生組/成人組,人工挑過的不要蓋掉);官方換新版
    導致舊 URL 消失時才改指最新一份。
    """
    docs = t.get("documents") or []
    cur_urls = {d["url"] for d in docs}
    changed = False

    reg_urls = [d["url"] for d in docs if d["type"] == "規程"]
    if reg_urls:
        if not t.get("regulation"):
            t["regulation"] = {"organizer": None, "ball": None, "fee": None,
                               "souvenir": None, "awards": None, "notes": None,
                               "pdf": reg_urls[0]}
            changed = True
        elif t["regulation"].get("pdf") not in cur_urls:
            t["regulation"]["pdf"] = reg_urls[0]
            changed = True

    res_urls = [d["url"] for d in docs if d["type"] == "成績"]
    if res_urls and t.get("resultPdf") not in cur_urls:
        t["resultPdf"] = res_urls[0]
        changed = True

    return changed


def process(t):
    openid = t["openid"]
    items = news(openid)
    docs = []
    for it in items:
        link = (it.get("Link") or "").strip()
        title = (it.get("Cont") or "").strip().replace("\n", " ")
        if not link:
            continue
        docs.append({"title": title, "url": link,
                     "date": it.get("Stime", ""), "type": classify(title, link)})

    changed = docs != t.get("documents")
    t["documents"] = docs
    changed = sync_links(t) or changed

    return changed, len(docs)


def relink():
    """只重跑連結同步(不打 API),用於修正指向舊版 PDF 的 regulation.pdf / resultPdf。"""
    n = 0
    for p in sorted(TOURN_DIR.glob("*.json")):
        t = json.loads(p.read_text(encoding="utf-8"))
        if t["openid"].startswith("manual-"):
            continue
        if sync_links(t):
            n += 1
            p.write_text(json.dumps(t, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
            print(f"[同步] {t['openid']} {t['name'][:25]}")
    print(f"\n完成:同步 {n} 場")


def main():
    if "--relink" in sys.argv:
        relink()
        return
    force = "--force" in sys.argv
    recheck_since = (date.today() - timedelta(days=RECHECK_DAYS)).isoformat()
    total_docs = total_changed = 0
    files = sorted(TOURN_DIR.glob("*.json"))
    for i, p in enumerate(files, 1):
        t = json.loads(p.read_text(encoding="utf-8"))
        if t["openid"].startswith("manual-"):
            continue
        # 增量:已結束且已有成績連結的就不重查,但賽後 RECHECK_DAYS 天內仍追蹤改版
        if not force and (t.get("dateEnd") or "") < recheck_since \
                and t.get("status") == "finished" and t.get("documents") \
                and any(d["type"] == "成績" for d in t["documents"]):
            continue
        try:
            changed, ndocs = process(t)
        except Exception as e:  # noqa: BLE001
            print(f"[錯誤] {t['openid']} {t['name']}: {e}")
            continue
        total_docs += ndocs
        if changed:
            total_changed += 1
            p.write_text(json.dumps(t, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
            print(f"[{i}/{len(files)}] {t['openid']} {t['name'][:25]} 文件 {ndocs} 筆")
        time.sleep(0.3)
    print(f"\n完成:更新 {total_changed} 場、文件連結 {total_docs} 筆")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
