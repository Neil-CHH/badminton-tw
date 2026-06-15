# -*- coding: utf-8 -*-
"""mylivescore.tw 羽球賽事資料抓取。

用法:
    python scripts/scrape.py          # 增量:只抓新賽事 / 狀態變更 / 進行中賽事
    python scripts/scrape.py --full   # 全量:所有賽事重抓(含逐場比分)

資料寫入 docs/data/tournaments/{openid}.json,結尾自動呼叫 rebuild_index.py。
API 細節見 CLAUDE.md。
"""
import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
TOURN_DIR = DATA_DIR / "tournaments"

MAIN_API = "https://mylivescore.tw/proxytoc.php"
MAIN_ORIGIN = "https://mylivescore.tw"
GOLIVE_URL = "https://livescore.efsoft.net/golivequery.php?openid=252119"

CITY = {
    "1": "臺北市", "2": "新北市", "3": "基隆市", "4": "桃園市", "5": "新竹縣",
    "6": "新竹市", "7": "苗栗縣", "8": "臺中市", "9": "彰化縣", "10": "南投縣",
    "11": "雲林縣", "12": "嘉義縣", "13": "嘉義市", "14": "臺南市", "15": "高雄市",
    "16": "屏東縣", "17": "宜蘭縣", "18": "花蓮縣", "19": "臺東縣", "20": "澎湖縣",
    "21": "金門縣", "22": "連江縣",
}
STATUS_NAME = {"1": "registering", "2": "ongoing", "3": "finished"}
EXCLUDE_PAT = re.compile(r"匹克球|桌球|研習|裁判|柔道")


def derive_category(name):
    """由賽名推導賽事類別(排名賽攸關晉升甲組,前端會特別標示)。"""
    if "排名賽" in name:
        return "排名賽"
    if "錦標賽" in name:
        return "錦標賽"
    return None


def post_json(url, payload, origin, retries=2):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Origin", origin)
    req.add_header("Referer", origin + "/")
    req.add_header("User-Agent", "Mozilla/5.0 (badminton-db scraper)")
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read().decode("utf-8", errors="replace")
            # 官方前端同款處理:資料內含未跳脫的換行/tab
            cleaned = raw.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
            return json.loads(cleaned)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"API 失敗 {url} {payload.get('api')}: {last_err}")


def resolve_liveresult_base():
    """golivequery.php 會 redirect 到 liveresult 實際 host(目前是 IP,可能變動)。"""
    req = urllib.request.Request(GOLIVE_URL, method="GET")
    req.add_header("User-Agent", "Mozilla/5.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            final = res.geturl()
        m = re.match(r"(https?://[^/]+/liveresult)/", final + "/")
        if m:
            return m.group(1)
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] liveresult host 解析失敗,改用預設 IP: {e}")
    return "http://172.105.210.232/liveresult"


class Api:
    def __init__(self):
        self.token = ""
        self.live_base = resolve_liveresult_base()
        self.live_origin = re.match(r"(https?://[^/]+)", self.live_base).group(1)

    def get_token(self):
        d = post_json(MAIN_API, {"Account": "officialc", "api": "gtoken"}, MAIN_ORIGIN)
        if d.get("success") != "1":
            raise RuntimeError(f"取得 token 失敗: {d}")
        self.token = d["result"]

    def mlist(self, status):
        if not self.token:
            self.get_token()
        payload = {
            "M_Type": "1", "M_Status": status, "M_Area": "0",
            "Year": "", "Month": "", "SContent": "",
            "api": "mlist", "token": self.token,
        }
        d = post_json(MAIN_API, payload, MAIN_ORIGIN)
        if d.get("result") in ("Time out.", "no token."):
            self.get_token()
            payload["token"] = self.token
            d = post_json(MAIN_API, payload, MAIN_ORIGIN)
        if d.get("success") != "1":
            return []
        return d["result"]["info"]

    def live(self, api, openid, **extra):
        payload = {"api": api, "Matchno": openid}
        payload.update(extra)
        return post_json(self.live_base + "/proxy.php", payload, self.live_origin)


# ---------- 組別標籤解析 ----------

def parse_group_tags(name, head):
    """從組名解析結構化標籤,解析不出的欄位為 None。"""
    tags = {"level": None, "grade": None, "event": None, "head": head or None}
    m = re.search(r"國小|國中|高中|大專|高國中|幼兒園|幼稚園", name)
    if m:
        tags["level"] = m.group(0)
    m = re.search(r"低年級|中年級|高年級|[一二三四五六]年級|幼幼|大班|中班", name)
    if m:
        tags["grade"] = m.group(0)
    if re.search(r"親子", name):
        tags["event"] = "親子"
    elif re.search(r"夫妻|夫婦", name):
        tags["event"] = "夫妻"
    elif re.search(r"混合雙打|混雙|男女混雙|混合", name):
        tags["event"] = "混雙"
    elif re.search(r"男(子)?雙|男子雙打", name):
        tags["event"] = "男雙"
    elif re.search(r"女(子)?雙|女子雙打", name):
        tags["event"] = "女雙"
    elif re.search(r"男(子)?單|男子單打", name):
        tags["event"] = "男單"
    elif re.search(r"女(子)?單|女子單打", name):
        tags["event"] = "女單"
    elif head == "團體" or re.search(r"團體", name):
        tags["event"] = "團體"
    elif re.search(r"雙打", name):
        tags["event"] = "雙打"
    elif re.search(r"單打", name):
        tags["event"] = "單打"
    elif head in ("單打", "雙打"):
        tags["event"] = head
    if tags["event"] != "團體" and (head == "團體"):
        tags["event"] = "團體"
    if not tags["level"] and re.search(r"歲|社會|公開|常青|長青", name):
        tags["level"] = "社會"
    return tags


# ---------- 名次推導 ----------

def base_group(group_name):
    """'國小低年級男單[1]' → '國小低年級男單'(去掉場地/分組尾碼)。"""
    return re.sub(r"\[[^\]]*\]\s*$", "", group_name).strip()


def side_entry(match, side):
    unit = match.get("team" + side, "") or ""
    members = []
    for si in match.get("scoreinfo", []):
        nm = (si.get("member" + side) or "").strip()
        if nm:
            members.append(nm)
    # 雙打同一人名重複出現時去重、保持順序
    seen, uniq = set(), []
    for nm in members:
        if nm not in seen:
            seen.add(nm)
            uniq.append(nm)
    return {"unit": unit, "members": uniq}


def _entry_key(e):
    return e["unit"] + "|" + "/".join(e["members"])


def derive_standings(matches):
    """由決賽比分推導各組名次。matchtype 代號:
    R2=決賽、R34=三四名戰、F2/F3/F4=循環決賽(2~4 隊互打)、R4=四強、預賽=分組賽。
    推不出的組別不產生資料(待 PDF 匯入覆蓋)。"""
    standings = []
    groups = {}
    for m in matches:
        groups.setdefault(base_group(m.get("groupName", "")), []).append(m)
    for gname, ms in groups.items():
        if not gname:
            continue
        mt = lambda m: (m.get("matchtype") or "").strip()  # noqa: E731
        finals = [m for m in ms if mt(m) in ("R2", "F2") and m.get("winner") in ("A", "B")]
        thirds = [m for m in ms if mt(m) == "R34" and m.get("winner") in ("A", "B")]
        rrobin = [m for m in ms if mt(m) in ("F3", "F4")]

        if len(finals) == 1:
            f = finals[0]
            win, lose = ("A", "B") if f["winner"] == "A" else ("B", "A")
            for rank, side in ((1, win), (2, lose)):
                e = side_entry(f, side)
                if e["unit"] or e["members"]:
                    standings.append({"group": gname, "rank": rank,
                                      "unit": e["unit"], "members": e["members"],
                                      "source": "derived"})
            if len(thirds) == 1:
                t = thirds[0]
                win3, lose3 = ("A", "B") if t["winner"] == "A" else ("B", "A")
                for rank, side in ((3, win3), (4, lose3)):
                    e = side_entry(t, side)
                    if e["unit"] or e["members"]:
                        standings.append({"group": gname, "rank": rank,
                                          "unit": e["unit"], "members": e["members"],
                                          "source": "derived"})
        elif rrobin and all(m.get("winner") in ("A", "B") for m in rrobin):
            # 循環決賽:依勝場數排名(同勝場以總得失分差排序)
            stats = {}
            for m in rrobin:
                for side in ("A", "B"):
                    e = side_entry(m, side)
                    if not (e["unit"] or e["members"]):
                        continue
                    k = _entry_key(e)
                    st = stats.setdefault(k, {"entry": e, "wins": 0, "diff": 0})
                    try:
                        own = int(m.get(f"{side}sidescore") or 0)
                        opp = int(m.get("Bsidescore" if side == "A" else "Asidescore") or 0)
                        st["diff"] += own - opp
                    except (TypeError, ValueError):
                        pass
                    if m["winner"] == side:
                        st["wins"] += 1
            ranked = sorted(stats.values(), key=lambda s: (-s["wins"], -s["diff"]))
            for rank, st in enumerate(ranked, start=1):
                standings.append({"group": gname, "rank": rank,
                                  "unit": st["entry"]["unit"],
                                  "members": st["entry"]["members"],
                                  "source": "derived"})
    standings.sort(key=lambda s: (s["group"], s["rank"]))
    return standings


# ---------- 主流程 ----------

def load_existing(openid):
    p = TOURN_DIR / f"{openid}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def scrape_tournament(api, info, status_key, existing):
    openid = info["OpenID"]
    record = {
        "openid": openid,
        "name": info["MName"],
        "city": CITY.get(info.get("CityName", ""), "其他"),
        "status": STATUS_NAME[status_key],
        "registerStart": info.get("Registdate1", ""),
        "registerEnd": info.get("Registdate2", ""),
        "dateStart": info.get("Matchdate1", ""),
        "dateEnd": info.get("Matchdate2", ""),
        "venue": info.get("Venus", ""),
        "image": info.get("Site_Pic", ""),
        "isSystem": info.get("IsSystem") == "1",
        "category": derive_category(info["MName"]) or (existing or {}).get("category"),
        "promotion": (existing or {}).get("promotion"),
        "regulation": (existing or {}).get("regulation"),
        "groups": (existing or {}).get("groups", []),
        "matches": (existing or {}).get("matches", []),
        "standings": (existing or {}).get("standings", []),
        "resultPdf": (existing or {}).get("resultPdf"),
        "lastUpdated": date.today().isoformat(),
    }

    if record["isSystem"] and status_key in ("2", "3"):
        d = api.live("items", openid)
        time.sleep(0.5)
        groups = []
        for g in (d.get("result", {}) or {}).get("matchiteminfo", []) or []:
            gname = g.get("GroupName", "")
            head = None
            groups.append({
                "id": g.get("GroupID", ""), "name": gname,
                "tags": None,  # head 需要比分資料,先佔位
                "drawUrl": f"http://livescore.mylivescore.tw/draws/{openid}/{g.get('GroupID','')}.html",
            })
        d = api.live("matches", openid)
        time.sleep(0.5)
        schedule = (d.get("result", {}) or {}).get("schedule", []) or []

    # 部分 isSystem 賽事(如全國排名賽)API 不提供逐場比分 → schedule 為空。
    # 此時保留既有(多為 PDF 匯入的)groups / matches / standings,不覆蓋。
    if record["isSystem"] and status_key in ("2", "3") and schedule:
        record["matches"] = schedule

        # 由比分資料補組別 head(單打/雙打/團體)
        head_by_group = {}
        for m in schedule:
            head_by_group.setdefault(base_group(m.get("groupName", "")), m.get("HeadGroup", ""))
        for g in groups:
            head = head_by_group.get(base_group(g["name"]), "")
            g["tags"] = parse_group_tags(g["name"], head)
        # items 可能為空但 matches 有資料 → 由 matches 補組別清單
        if not groups and schedule:
            for gname in sorted({base_group(m.get("groupName", "")) for m in schedule if m.get("groupName")}):
                groups.append({"id": "", "name": gname,
                               "tags": parse_group_tags(gname, head_by_group.get(gname, "")),
                               "drawUrl": None})
        record["groups"] = groups

        # 名次:PDF 匯入的名次優先保留,只更新 derived 部分
        pdf_standings = [s for s in record["standings"] if s.get("source") == "pdf"]
        if pdf_standings:
            pdf_groups = {s["group"] for s in pdf_standings}
            derived = [s for s in derive_standings(schedule) if s["group"] not in pdf_groups]
            record["standings"] = sorted(pdf_standings + derived,
                                         key=lambda s: (s["group"], s["rank"]))
        else:
            record["standings"] = derive_standings(schedule)

    return record


def main():
    full = "--full" in sys.argv
    TOURN_DIR.mkdir(parents=True, exist_ok=True)
    api = Api()
    print(f"liveresult host: {api.live_base}")

    new_count = updated = skipped = 0
    seen_ids = set()
    for status_key in ("1", "2", "3"):
        infos = api.mlist(status_key)
        time.sleep(0.5)
        print(f"狀態 {STATUS_NAME[status_key]}: {len(infos)} 場")
        for info in infos:
            openid = info["OpenID"]
            if openid in seen_ids:
                continue
            seen_ids.add(openid)
            if EXCLUDE_PAT.search(info["MName"]):
                print(f"  [警告] 排除非羽球賽事: {info['MName']}")
                continue
            existing = load_existing(openid)
            need = (
                full
                or existing is None
                or existing.get("status") != STATUS_NAME[status_key]
                or status_key == "2"  # 進行中的每次都更新比分
                or (status_key == "3" and existing.get("isSystem") and not existing.get("matches"))
            )
            if not need:
                skipped += 1
                continue
            try:
                record = scrape_tournament(api, info, status_key, existing)
            except Exception as e:  # noqa: BLE001
                print(f"  [錯誤] {openid} {info['MName']}: {e}")
                continue
            out = TOURN_DIR / f"{openid}.json"
            out.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                           encoding="utf-8")
            if existing is None:
                new_count += 1
                print(f"  [新增] {openid} {record['name']} ({len(record['matches'])} 場比賽)")
            else:
                updated += 1
                print(f"  [更新] {openid} {record['name']} ({len(record['matches'])} 場比賽)")

    print(f"\n完成:新增 {new_count}、更新 {updated}、略過 {skipped}")
    print("重建索引…")
    import rebuild_index  # noqa: PLC0415
    rebuild_index.main()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
