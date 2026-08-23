# -*- coding: utf-8 -*-
"""掃 docs/data/tournaments/*.json,重建前端索引:

- index.json          賽事摘要(列表頁)
- search-index.json   輕量搜尋索引(選手/單位名稱 → 摘要數字,搜尋頁唯一需要載的檔)
- players/{0..15}.json 選手分片:姓名 → 參賽紀錄(含勝負統計)與名次
- units/{0..15}.json   單位分片:單位 → 出賽賽事、選手、得獎

分片依名稱雜湊(shard_of,前端 common.js 有相同實作,兩邊必須一致)。
爬蟲與 PDF 匯入後都要跑。"""
import json
import re
import sys
from pathlib import Path

from sources_common import blocked_openids, effective_dates, norm_group_key, source_of

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
TOURN_DIR = DATA_DIR / "tournaments"

NAME_SPLIT = re.compile(r"[-/、,,]")
SHARDS = 16


def shard_of(name, n=SHARDS):
    """與 docs/js/common.js 的 shardOf 完全一致:h = (h*31 + codepoint) mod 2^32。"""
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) % 4294967296
    return h % n


def split_members(raw):
    """'馬瀚/許蓁樺' 或 '林姝華-林紓嫻' → 個別姓名。"""
    return [n.strip() for n in NAME_SPLIT.split(raw or "") if n.strip()]


def base_group(group_name):
    return re.sub(r"\[[^\]]*\]\s*$", "", group_name or "").strip()


def _to_int(v):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


def main():
    # 跨來源重複的 shadow 賽事檔在 dedupe.py 就已刪除,這裡是防呆:
    # 單獨跑 rebuild(例如 PDF 匯入後)時,萬一檔案被救回也不會再度重複計入。
    blocked = blocked_openids()
    tournaments = []
    for p in sorted(TOURN_DIR.glob("*.json")):
        if p.stem in blocked:
            continue
        tournaments.append(json.loads(p.read_text(encoding="utf-8")))

    index = []
    players = {}      # name -> {openid: {"units": set, "groups": set, "w": int, "l": int}}
    units = {}        # unit -> {openid: set(player names)}
    ranks = {}        # name -> list of rank refs(由 standings 來)
    unit_ranks = {}   # unit -> list of rank refs

    for t in tournaments:
        oid = t["openid"]
        # 少數賽事的 dateStart 是錯的(複製到別場、日期反轉、0000-00-00),
        # 會讓列表排序與年份篩選錯位 → 與實際比分日期完全沒交集時改用比分推得的區間。
        # 賽事 JSON 本體不動(保持來源忠實);tournament.html 以 common.js 的
        # effectiveDates() 套用同一條規則,兩邊必須同步。
        date_start, date_end = effective_dates(t)
        index.append({
            "openid": oid,
            "source": source_of(t),
            "name": t["name"],
            "city": t.get("city", ""),
            "status": t.get("status", ""),
            "registerStart": t.get("registerStart", ""),
            "registerEnd": t.get("registerEnd", ""),
            "dateStart": date_start,
            "dateEnd": date_end,
            "venue": t.get("venue", ""),
            "image": t.get("image", ""),
            "category": t.get("category"),
            "hasMatches": bool(t.get("matches")),
            "hasRegulation": bool(t.get("regulation")),
            "hasEntries": bool(t.get("entries")),
            "groupCount": len(t.get("groups", [])),
        })

        # (姓名) 有比分或名次者。參賽名單登錄時據此判斷是不是「僅參賽」。
        scored = set()

        def add_player(name, unit, group, openid=oid):
            rec = players.setdefault(name, {})
            ent = rec.setdefault(openid, {"units": set(), "groups": set(), "w": 0, "l": 0})
            if unit:
                ent["units"].add(unit)
            if group:
                ent["groups"].add(group)
            return ent

        def add_unit(unit, name, openid=oid):
            if not unit:
                return
            units.setdefault(unit, {}).setdefault(openid, set())
            if name:
                units[unit][openid].add(name)

        for m in t.get("matches", []):
            g = base_group(m.get("groupName", ""))
            is_team = m.get("HeadGroup") == "團體"
            winner = m.get("winner")
            # 勝負統計:個人賽以整場勝負計(選手出現於任一局);
            # 團體賽以「點」計(該點比分高者勝),與 player.html 顯示邏輯一致。
            side_players = {"A": set(), "B": set()}
            for si in m.get("scoreinfo", []):
                sa, sb = _to_int(si.get("scoreA")), _to_int(si.get("scoreB"))
                rubber_win = None  # 團體賽單點勝方
                if is_team and sa is not None and sb is not None and sa != sb:
                    rubber_win = "A" if sa > sb else "B"
                for side in ("A", "B"):
                    unit = m.get("team" + side, "") or ""
                    for nm in split_members(si.get("member" + side)):
                        ent = add_player(nm, unit, g)
                        add_unit(unit, nm)
                        scored.add(nm)
                        side_players[side].add(nm)
                        if is_team and rubber_win:
                            ent["w" if rubber_win == side else "l"] += 1
            for side in ("A", "B"):
                add_unit(m.get("team" + side, "") or "", None)
                if not is_team and winner in ("A", "B"):
                    for nm in side_players[side]:
                        ent = add_player(nm, None, None)
                        ent["w" if winner == side else "l"] += 1

        # 獲獎去重:同一場賽事、同一組別,一位選手只登錄一筆名次(取最佳)。
        # 兩種來源都會製造重複,而且只有這一層擋得住已寫進賽事檔的資料:
        #   a) 同一列 members[] 內同名出現兩次 —— 團體賽名單列出多組配對時很常見
        #      (254131 的 ["三井安/定平","聖紘/牡羊帥哥","三井安/牡羊帥哥"])
        #   b) 同一組別有兩列都含這位選手 —— 主辦成績總表自己填錯(lapgo-100 一般男雙
        #      同時有第 1、第 2 名),或籤表矛盾讓推導產生兩個名次
        # standings 已由 merge_standings / derive_standings 依 (group, rank) 排序,
        # 仍明確比較 rank 以免將來排序改變。
        best_rank = {}      # (選手, 正規化組別) -> 已登錄的那一筆 rank ref
        unit_seen = set()   # (單位, 正規化組別, 名次) -> 單位名次列去重(完全相同才算重複)
        for s in sorted(t.get("standings", []),
                        key=lambda r: (r.get("group") or "", r.get("rank") or 99)):
            # 雙打跨單位時 memberUnits 與 members 對齊,逐位歸屬正確單位;
            # 否則全部歸屬 s.unit(單打或同單位雙打)。
            members = s.get("members", [])
            munits = s.get("memberUnits") or []
            gkey = norm_group_key(s.get("group", ""))
            all_members = []
            seen_units = []
            for i, nm in enumerate(members):
                unit_i = munits[i] if i < len(munits) else s.get("unit", "")
                for nm2 in split_members(nm):
                    if nm2 not in all_members:
                        all_members.append(nm2)
                    add_player(nm2, unit_i, s.get("group", ""))
                    add_unit(unit_i, nm2)
                    scored.add(nm2)
                    prev = best_rank.get((nm2, gkey))
                    if prev is None:
                        ref = {"openid": oid, "group": s.get("group", ""),
                               "rank": s.get("rank"), "unit": unit_i}
                        best_rank[(nm2, gkey)] = ref
                        ranks.setdefault(nm2, []).append(ref)
                    elif (s.get("rank") or 99) < (prev["rank"] or 99):
                        prev.update(group=s.get("group", ""), rank=s.get("rank"), unit=unit_i)
                if unit_i and unit_i not in seen_units:
                    seen_units.append(unit_i)
            # 團體賽:只有單位、無個別選手 → 仍登錄該單位名次
            if not members and s.get("unit"):
                add_unit(s["unit"], None)
                seen_units.append(s["unit"])
            for unit in seen_units:
                # 單位不可以 (單位, 組別) 去重 —— 同一所學校在同一組拿第 1 和第 2 是正常的
                key = (unit, gkey, s.get("rank"))
                if key in unit_seen:
                    continue
                unit_seen.add(key)
                unit_ranks.setdefault(unit, []).append({
                    "openid": oid, "group": s.get("group", ""),
                    "rank": s.get("rank"), "members": all_members,
                })

        # 參賽名單:讓「有參加但沒得名」的選手也查得到。
        # 只登錄出賽事實,不動勝負;沒有比分也沒有名次者標記 entry_only,
        # 前端據此顯示「僅參賽」,避免與有成績資料的紀錄混淆。
        for e in t.get("entries", []):
            unit = e.get("unit", "") or ""
            group = e.get("group", "") or ""
            for nm in e.get("members", []):
                for nm2 in split_members(nm):
                    ent = add_player(nm2, unit, group)
                    add_unit(unit, nm2)
                    if nm2 not in scored:
                        ent["entry_only"] = True

    # ---------- 序列化 ----------
    index.sort(key=lambda x: x.get("dateStart", ""), reverse=True)

    players_out = {}
    for name, recs in players.items():
        out = []
        for oid, e in recs.items():
            rec = {"openid": oid, "units": sorted(e["units"]), "groups": sorted(e["groups"])}
            if e["w"]:
                rec["w"] = e["w"]
            if e["l"]:
                rec["l"] = e["l"]
            if e.get("entry_only"):
                rec["e"] = 1   # 僅有報名/籤表紀錄,沒有比分也沒有名次
            out.append(rec)
        players_out[name] = out

    units_out = {}
    for unit, recs in units.items():
        units_out[unit] = {
            "t": [{"openid": oid, "players": sorted(names)} for oid, names in recs.items()],
            "ranks": unit_ranks.get(unit, []),
        }

    # 輕量搜尋索引:name → [場次數, 名次數, 單位預覽] / unit → [場次數, 選手數]
    search_index = {
        "players": {
            name: [
                len(recs),
                len(ranks.get(name, [])),
                "、".join(sorted({u for r in recs for u in r["units"]})[:3]),
            ]
            for name, recs in players_out.items()
        },
        "units": {
            unit: [len(rec["t"]), len({p for e in rec["t"] for p in e["players"]})]
            for unit, rec in units_out.items()
        },
    }

    # 分片
    player_shards = [{"players": {}, "ranks": {}} for _ in range(SHARDS)]
    for name, recs in players_out.items():
        s = shard_of(name)
        player_shards[s]["players"][name] = recs
        if name in ranks:
            player_shards[s]["ranks"][name] = ranks[name]
    unit_shards = [{} for _ in range(SHARDS)]
    for unit, rec in units_out.items():
        unit_shards[shard_of(unit)][unit] = rec

    # ---------- 寫檔 ----------
    def dump(obj):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "players").mkdir(exist_ok=True)
    (DATA_DIR / "units").mkdir(exist_ok=True)
    (DATA_DIR / "index.json").write_text(dump(index), encoding="utf-8")
    (DATA_DIR / "search-index.json").write_text(dump(search_index), encoding="utf-8")
    for i in range(SHARDS):
        (DATA_DIR / "players" / f"{i}.json").write_text(dump(player_shards[i]), encoding="utf-8")
        (DATA_DIR / "units" / f"{i}.json").write_text(dump(unit_shards[i]), encoding="utf-8")
    # 舊的整包索引(已由分片取代)
    (DATA_DIR / "players.json").unlink(missing_ok=True)
    (DATA_DIR / "units.json").unlink(missing_ok=True)

    kb = lambda p: p.stat().st_size // 1024  # noqa: E731
    p_sizes = [kb(DATA_DIR / "players" / f"{i}.json") for i in range(SHARDS)]
    u_sizes = [kb(DATA_DIR / "units" / f"{i}.json") for i in range(SHARDS)]
    print(f"索引完成:{len(index)} 場賽事、{len(players_out)} 位選手、{len(units_out)} 個單位")
    print(f"檔案大小(KB):index={kb(DATA_DIR / 'index.json')}"
          f" search-index={kb(DATA_DIR / 'search-index.json')}"
          f" players分片={sum(p_sizes)}(最大單片 {max(p_sizes)})"
          f" units分片={sum(u_sizes)}(最大單片 {max(u_sizes)})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
