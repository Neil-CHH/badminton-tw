# -*- coding: utf-8 -*-
"""docs/data 健檢:只讀本地檔、不打 API,可隨時重跑。每月更新與 PDF 匯入後都該跑。

用法:
    python scripts/verify_data.py                          # 健檢
    python scripts/verify_data.py --summary                # 加印本次新增賽事摘要表
    python scripts/verify_data.py --summary 341170 333043  # 摘要表指定 openid

輸出分兩級:
  [錯誤] 結構/索引不一致,是我們自己造成的 → exit 1,修好再部署
  [提醒] 來源資料本身的問題或已知缺口 → 不影響 exit code,照抄進月報即可
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import dedupe  # 共用去重判定,確保健檢與 dedupe.py 用的是同一套規則
import rebuild_index  # 共用 shard_of,確保與前端 common.js 的分片演算法一致
import sources_common

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
TOURN_DIR = DATA_DIR / "tournaments"
SHARDS = rebuild_index.SHARDS

errors = []
warns = []


def err(msg):
    errors.append(msg)


def warn(msg):
    warns.append(msg)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------- 各項檢查 ----------

def check_doc_links(tours):
    """regulation.pdf / resultPdf 必須仍存在於 documents 中。

    官方換新版 PDF 會換檔名(=換 URL),舊連結會失效。fetch_docs.sync_links 負責跟上,
    這裡是防止該機制再度失效(參考 commit fc380dd)。
    """
    bad = 0
    for oid, t in tours.items():
        urls = {d["url"] for d in (t.get("documents") or [])}
        if not urls:
            continue
        for field, val in (("regulation.pdf", (t.get("regulation") or {}).get("pdf")),
                           ("resultPdf", t.get("resultPdf"))):
            if val and val not in urls:
                bad += 1
                err(f"{oid} {t['name'][:24]} 的 {field} 已不在 documents 中(官方換版?)\n"
                    f"       現值 {val}")
    if not bad:
        print("[OK] 連結一致性:regulation.pdf / resultPdf 都指向 documents 內的檔案")


def check_index(tours):
    """index.json 的賽事集合與新鮮度。"""
    idx_path = DATA_DIR / "index.json"
    if not idx_path.exists():
        err("index.json 不存在 → 跑 python scripts/rebuild_index.py")
        return
    index = load(idx_path)
    idx_ids = {t["openid"] for t in index}
    file_ids = set(tours)
    if idx_ids != file_ids:
        for oid in sorted(file_ids - idx_ids):
            err(f"{oid} 有 tournament 檔但不在 index.json")
        for oid in sorted(idx_ids - file_ids):
            err(f"{oid} 在 index.json 但 tournament 檔已不存在")
    else:
        print(f"[OK] index.json 收錄 {len(index)} 場,與 tournaments/ 檔案一致")

    newest = max((p.stat().st_mtime for p in TOURN_DIR.glob("*.json")), default=0)
    if idx_path.stat().st_mtime < newest:
        err("index.json 比 tournaments/ 舊 → 有人改過賽事檔卻沒重建索引,"
            "跑 python scripts/rebuild_index.py")


SEARCH_INDEX = {"players": "search-index-players.json", "units": "search-index-units.json"}


def load_search_index():
    """兩個搜尋索引檔 → {kind: {name: [...]}};缺檔回 None(呼叫端各自報錯)。"""
    out = {}
    for kind, fname in SEARCH_INDEX.items():
        p = DATA_DIR / fname
        if not p.exists():
            return None
        out[kind] = load(p)
    return out


def check_shards():
    """分片落點、search-index 與分片的 key 集合是否一致。"""
    si = load_search_index()
    if si is None:
        err(f"{' / '.join(SEARCH_INDEX.values())} 不齊 → 跑 python scripts/rebuild_index.py")
        return

    for kind, si_key in (("players", "players"), ("units", "units")):
        names = set()
        misplaced = 0
        for i in range(SHARDS):
            p = DATA_DIR / kind / f"{i}.json"
            if not p.exists():
                err(f"{kind}/{i}.json 不存在 → 跑 python scripts/rebuild_index.py")
                return
            shard = load(p)
            keys = shard["players"].keys() if kind == "players" else shard.keys()
            for name in keys:
                names.add(name)
                if rebuild_index.shard_of(name) != i:
                    misplaced += 1
                    if misplaced <= 3:
                        err(f"{kind}「{name}」放在分片 {i},"
                            f"但 shard_of 算出 {rebuild_index.shard_of(name)}")
            if kind == "players":
                orphan = set(shard["ranks"]) - set(shard["players"])
                if orphan:
                    err(f"players/{i}.json 有 {len(orphan)} 筆 ranks 找不到對應選手,"
                        f"例:{sorted(orphan)[:3]}")
        si_names = set(si.get(si_key, {}))
        if names != si_names:
            err(f"{kind}:search-index 有 {len(si_names)} 筆、分片合計 {len(names)} 筆,"
                f"兩邊不一致(缺 {len(si_names - names)} / 多 {len(names - si_names)})")
        elif not misplaced:
            print(f"[OK] {kind} 分片:{len(names)} 筆,落點與 search-index 皆一致")


def check_mojibake(tours):
    """U+FFFD 置換字元 = 抓取時編碼壞掉。選手/單位名以 search-index 的 key 一次涵蓋。"""
    hits = []
    for oid, t in tours.items():
        for field in ("name", "venue"):
            if "�" in (t.get(field) or ""):
                hits.append(f"{oid} {field}={t.get(field)[:40]}")
    si = load_search_index()
    if si is not None:
        for kind in ("players", "units"):
            for name in si.get(kind, {}):
                if "�" in name:
                    hits.append(f"{kind}「{name}」")
    for h in hits[:10]:
        err(f"名稱含亂碼: {h}")
    if not hits:
        print("[OK] 名稱編碼:未發現置換字元")


def check_dates(tours):
    """賽事日期。dateStart 錯掉(整串複製到別場、日期反轉、0000-00-00)會讓列表排序與
    年份篩選錯位。有比分的由 effective_dates 自動修正(rebuild_index 已套用到索引),
    沒有比分可推導的只能報出來。"""
    fixed = []
    for oid, t in sorted(tours.items()):
        ds, de = t.get("dateStart") or "", t.get("dateEnd") or ""
        eff = sources_common.effective_dates(t)
        if eff != (ds, de or ds):
            fixed.append((oid, t["name"], ds, de, eff))
            continue
        if ds and de and ds > de:
            warn(f"{oid} {t['name'][:26]} 日期反轉 {ds} ~ {de}"
                 f"(來源資料如此,且無比分可推導,列表排序會錯位)")
        if not ds:
            warn(f"{oid} {t['name'][:26]} 缺 dateStart")
    if fixed:
        print(f"[OK] 日期:{len(fixed)} 場的 dateStart 與實際比分日期不符,索引已自動改用比分日期")
        for oid, name, ds, de, eff in fixed:
            print(f"       {oid}  {ds or '(空)'}~{de or '(空)'}  →  {eff[0]}~{eff[1]}  {name[:30]}")


def check_duplicates(tours):
    """跨來源重複賽事:登錄檔一致性,以及還沒處理掉的重複。

    重複賽事會讓列表出現兩張卡,選手/單位的場次與勝負獲獎全部翻倍,所以列為錯誤。
    """
    pairs = sources_common.load_duplicates()
    for p in pairs:
        if p.get("canonical") not in tours:
            err(f"duplicates.json 的 canonical {p.get('canonical')} 找不到賽事檔 —— "
                f"shadow {p.get('shadow')} 已刪,這場等於整個消失")
        if p.get("shadow") in tours:
            err(f"{p.get('shadow')} 已登錄為重複但賽事檔仍在 → 跑 python scripts/dedupe.py")

    loaded = dedupe.load_all()
    deletable, review = dedupe.detect(loaded)
    for can, sh, ratio, _gj, _ in deletable:
        err(f"{sh['openid']} 與 {can['openid']} 判定為同一場賽事(相似度 {ratio:.2f})"
            f"但尚未去重 → 跑 python scripts/dedupe.py")
    for can, sh, ratio, _gj, why in review:
        warn(f"{can['openid']} 與 {sh['openid']} 疑似同一場(相似度 {ratio:.2f}),"
             f"但{why} → 不自動處理,請人工確認")
    for a, b, ratio, _gj in dedupe.same_source_candidates(loaded):
        warn(f"同來源疑似重複:{a['openid']} 與 {b['openid']}(相似度 {ratio:.2f})"
             f"「{a['name'][:24]}」→ 同來源一律不自動處理,請人工確認")
    # 賽名比對的兩個盲點(同來源不看、主辦取兩個不同名字)都由獲獎內容補上。
    # 這條命中就代表有選手的場次與獲獎被算了兩次,列為錯誤。
    aw = dedupe.award_overlap_candidates(loaded)
    for a, b, inter, ratio in aw:
        can, sh = sorted((a, b), key=lambda t: (t["_matches"], sources_common.source_priority(t)),
                         reverse=True)
        err(f"{a['openid']} 與 {b['openid']} 有 {inter} 筆完全相同的獲獎"
            f"(選手/組別/名次,重疊 {ratio:.2f})、比賽日期也重疊 → 極可能是同一場賽事收了兩次"
            f"「{a['name'][:20]}」/「{b['name'][:20]}」;確認後跑 "
            f"python scripts/dedupe.py --merge {sh['openid']} {can['openid']}")
    if not deletable and not review and not aw:
        print(f"[OK] 去重:已登錄 {len(pairs)} 組,賽名與獲獎內容都沒有新的重複候選")


# 官方公布的名次(PDF/主辦平台總表)本來就會並列與跳號(依報名組數取 N 名,
# 第三名兩位、第五名四位),不該報。derived 由決賽勝負推導,重複或跳號代表推導有問題。
AUTHORITATIVE = {"pdf", "official"}


def check_standings(tours):
    """查 derived 名次的重複/跳號,以及 ocr 名次(機器讀圖)的合理性。"""
    bad = ocr_bad = 0
    for oid, t in sorted(tours.items()):
        groups = {}
        for s in t.get("standings", []):
            if s.get("source") in AUTHORITATIVE:
                continue
            groups.setdefault(s.get("group", ""), []).append((s.get("rank"), s.get("source")))
        for g, rows in groups.items():
            src = "ocr" if any(x[1] == "ocr" for x in rows) else "derived"
            ranks = [r for r, _ in rows]
            rs = [r for r in ranks if r is not None]
            if len(rs) != len(ranks):
                warn(f"{oid} 組別「{g}」有名次為空的 {src} 紀錄")
            elif rs and sorted(rs) != list(range(1, max(rs) + 1)):
                # ocr 允許並列(官方成績圖同樣有並列名次),只查跳號與重複的第一名
                dup_first = rs.count(1) > 1
                gap = sorted(set(rs)) != list(range(1, max(rs) + 1))
                if src == "ocr" and not (dup_first or gap):
                    continue
                warn(f"{oid} 組別「{g}」{src} 名次異常:{sorted(rs)}")
            else:
                continue
            bad += 1
            ocr_bad += src == "ocr"
    if not bad:
        print("[OK] derived / ocr 名次:無重複或跳號")
    elif ocr_bad:
        print(f"[提醒] 其中 {ocr_bad} 個組別來自圖片解析(ocr),建議對照原圖抽查")


def check_pdf_standings(tours):
    """查「我們自己解析官方 PDF」產生的名次(source=pdf)有沒有讀錯版面。

    check_standings 對 pdf/official 是整組跳過的,背後假設是「官方資料一定對」——
    但風險從來不是官方資料,是**我們的解析**。2026-08 的 259196 排名賽把甲組與乙組
    讀成同一組,「男子組單打」同時有王子維(甲組冠軍)和蕭順(乙組冠軍)兩個第一名,
    而當時 update_all 說「更新 15 場」、verify_data 說「錯誤 0」,一路綠燈上線。

    **只查重複的第一名**:名次跳號不能查 —— `[1,2,3,3,5,5,5,5]` 是官方常態並列,
    實測全庫 939 組都長這樣,查了只會被雜訊淹沒。也不查 official(lapgo 的成績總表
    API):主辦自己就會給趣味組四個並列第一(lapgo-95 女俠組 [1,1,1,1])。
    """
    bad = 0
    for oid, t in sorted(tours.items()):
        groups = {}
        for s in t.get("standings", []):
            if s.get("source") != "pdf":
                continue
            groups.setdefault(s.get("group", ""), []).append(s.get("rank"))
        for g, ranks in sorted(groups.items()):
            if ranks.count(1) > 1:
                bad += 1
                err(f"{oid} 組別「{g}」的 pdf 名次有 {ranks.count(1)} 個第一名 "
                    f"{sorted(r for r in ranks if r)} —— 成績總表多半被讀成同一組"
                    f"(分組標籤寫在表格外面),用 parse_result_pdf.py --openid {oid} 對一下原始 PDF")
    if not bad:
        print("[OK] pdf 名次:沒有同組別出現兩個第一名")


def check_duplicate_awards(tours):
    """同一場賽事、同一組別,同一位選手拿到兩個名次 → 該選手的獲獎數會被算兩次。

    rebuild_index 建索引時已去重(只留最佳名次),所以前端數字是對的;但賽事頁
    tournament.html 直接讀賽事檔,還是會把兩列都畫出來,所以仍要報出來讓人回頭補 PDF。
    """
    bad = 0
    for oid, t in sorted(tours.items()):
        per = {}
        for s in t.get("standings", []):
            g = sources_common.norm_group_key(s.get("group"))
            for nm in s.get("members") or []:
                for nm2 in rebuild_index.split_members(nm):
                    per.setdefault((nm2, g), []).append(s.get("rank"))
        for (nm2, g), rks in sorted(per.items()):
            # 同一個名次出現兩次是正常的:團體賽的名單會把同一位選手的多組配對都列出來
            # (254131 的「三井安/定平」與「三井安/牡羊帥哥」都在冠軍那一列)。
            # 真正該報的是**名次不同**——那代表名次表自相矛盾。
            if len(set(rks)) > 1:
                bad += 1
                if bad <= 12:
                    warn(f"{oid} 「{g}」的 {nm2} 同時有第 {sorted(r for r in rks if r)} 名 —— "
                         f"索引只取最佳名次,但賽事檔的名次表自相矛盾,建議匯入官方成績 PDF")
    if bad > 12:
        warn(f"(同組別名次矛盾還有 {bad - 12} 筆未列出)")
    if not bad:
        print("[OK] 名次去重:沒有同組別、同選手拿到兩個不同名次的情形")


_ROSTER_DOC = re.compile(r"報名結果|抽籤結果|秩序冊|籤表")


def check_entry_gaps(tours):
    """查不到任何參賽者的賽事,以及「名冊掛在那裡卻沒解析」的賽事。

    這是「文件一進來就要查得到人」的守門。以前的漏法是靜默的:官方把報名結果 PDF
    掛進 documents,沒有人去讀,那場賽事就一直是零位選手可查(實測 264311 羽霸盃
    755 人躺了一整個月)。列出來才不用靠人記得。

    warn 級不擋部署 —— lapgo 沒有公開的報名名單端點、tsba 早年只有成績圖,
    那些場次是真的拿不到資料,不該讓健檢一直紅著。
    """
    blind, unparsed = [], []
    for oid, t in sorted(tours.items()):
        if not (t.get("matches") or t.get("standings") or t.get("entries")):
            blind.append((oid, t))
        if t.get("entries") or t.get("matches"):
            continue
        docs = [d for d in t.get("documents") or []
                if _ROSTER_DOC.search(d.get("title") or "")]
        if docs:
            # parse_entry_pdf 只吃「報名結果」PDF 直連。籤表樹、秩序冊、以及別的
            # scraper 補進來的官方消息頁(255139 的資格賽籤表)都要另外處理,
            # 給錯指令只會讓人跑一次空包彈
            auto = any("報名結果" in (d.get("title") or "")
                       and (d.get("url") or "").lower().endswith(".pdf") for d in docs)
            unparsed.append((oid, t, auto, docs[0].get("title") or ""))
    if blind:
        warn(f"{len(blind)} 場賽事查不到任何選手(無比分、無名次、無參賽名單):"
             + "、".join(oid for oid, _ in blind[:6])
             + ("…" if len(blind) > 6 else ""))
    for oid, t, auto, title in unparsed:
        how = (f"跑 python scripts/parse_entry_pdf.py --openid {oid} --apply" if auto
               else "不是報名結果 PDF,需人工判讀或另寫解析")
        warn(f"{oid}「{(t.get('name') or '')[:20]}」有名冊文件「{title[:26]}」"
             f"卻沒有參賽名單 —— {how}")
    if not blind and not unparsed:
        print("[OK] 參賽名單:沒有「名冊掛著沒解析」的賽事")


def report_entries(tours):
    """參賽名單的收錄狀況。名單讓沒得名的選手也查得到,覆蓋率偏低要留意。"""
    rows = [(oid, t) for oid, t in sorted(tours.items()) if t.get("entries")]
    if not rows:
        return
    total = sum(len(t["entries"]) for _, t in rows)
    print(f"\n== 參賽名單({len(rows)} 場、{total} 筆)==")
    for oid, t in rows:
        cov = t.get("entriesCoverage")
        covtxt = f"{cov:.0%}" if cov is not None else "無宣告數可驗證"
        flag = "  ← 覆蓋偏低,可查籤表排版" if cov is not None and cov < 0.8 else ""
        print(f"  {oid}  {len(t['entries']):5d} 筆  覆蓋 {covtxt}{flag}")


def report_ocr(tours):
    """圖片解析的名次中,名冊查無而未能校對的筆數(補報名/換人居多,建議抽查)。"""
    rows = [(oid, s) for oid, t in tours.items()
            for s in t.get("standings", []) if s.get("ocrUnverified")]
    if not rows:
        return
    by_t = {}
    for oid, _ in rows:
        by_t[oid] = by_t.get(oid, 0) + 1
    print(f"\n== 圖片解析待確認({len(rows)} 筆,名冊查無,已收錄但標記 ocrUnverified)==")
    for oid, n in sorted(by_t.items()):
        print(f"  {oid}  {n} 筆")


def report_gaps(tours):
    """已結束、API 無比分、也還沒用 PDF 補名次 → 真正的資料缺口。"""
    gaps = [(t.get("dateEnd") or "", oid, t["name"])
            for oid, t in tours.items()
            if t.get("status") == "finished" and not t.get("matches")
            and not t.get("standings")]
    print(f"\n== 資料缺口({len(gaps)} 場已結束但無比分也無名次,需 PDF 補)==")
    for de, oid, name in sorted(gaps, reverse=True):
        print(f"  {oid}  {de}  {name}")


# ---------- 新增賽事摘要 ----------

def new_openids():
    """本次新增 = git 尚未追蹤的 tournament 檔。"""
    out = subprocess.run(["git", "status", "--porcelain", "--", "docs/data/tournaments"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout
    ids = []
    for line in out.splitlines():
        # porcelain v1:前兩碼是狀態、第 4 碼起是路徑。?? 未追蹤、A 已 add 但未 commit
        path = line[3:].strip()
        if line[:2] in ("??", "A ", "AM") and path.endswith(".json"):
            ids.append(Path(path).stem)
    return ids


def print_summary(tours, ids):
    if not ids:
        print("\n== 新增賽事:無 ==")
        return
    print(f"\n== 新增賽事({len(ids)} 場)==")
    print("  openid | 日期 | 縣市 | 組數 | 比分 | 規程 | 賽事名")
    rows = [(tours[i].get("dateStart") or "", i) for i in ids if i in tours]
    for _, oid in sorted(rows, reverse=True):
        t = tours[oid]
        print(f"  {oid} | {t.get('dateStart')}~{t.get('dateEnd')} | {t.get('city')} | "
              f"{len(t.get('groups', []))} 組 | {len(t.get('matches', []))} 場 | "
              f"{'有' if t.get('regulation') else '無'} | {t['name']}")
    for oid in ids:
        if oid not in tours:
            print(f"  {oid} (檔案讀不到)")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    want_summary = "--summary" in sys.argv

    tours = {p.stem: load(p) for p in sorted(TOURN_DIR.glob("*.json"))}
    print(f"檢查 {len(tours)} 場賽事…\n")

    check_doc_links(tours)
    check_index(tours)
    check_shards()
    check_mojibake(tours)
    check_dates(tours)
    check_duplicates(tours)
    check_duplicate_awards(tours)
    check_standings(tours)
    check_pdf_standings(tours)
    check_entry_gaps(tours)

    if errors:
        print(f"\n== 錯誤({len(errors)})— 修好再部署 ==")
        for m in errors:
            print(f"  [錯誤] {m}")
    if warns:
        print(f"\n== 提醒({len(warns)})— 不影響部署 ==")
        for m in warns:
            print(f"  [提醒] {m}")

    report_entries(tours)
    report_ocr(tours)
    report_gaps(tours)

    if want_summary:
        print_summary(tours, args or new_openids())

    print(f"\n結果:錯誤 {len(errors)}、提醒 {len(warns)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
