# -*- coding: utf-8 -*-
"""掃 docs/data/tournaments/*.json,重建三個索引:

- index.json   賽事摘要(列表頁)
- players.json 選手姓名 → 參賽紀錄(不含比分)
- units.json   單位名稱 → 出賽賽事與選手

爬蟲與 PDF 匯入後都要跑。"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
TOURN_DIR = DATA_DIR / "tournaments"

NAME_SPLIT = re.compile(r"[-/、,,]")


def split_members(raw):
    """'馬瀚/許蓁樺' 或 '林姝華-林紓嫻' → 個別姓名。"""
    return [n.strip() for n in NAME_SPLIT.split(raw or "") if n.strip()]


def base_group(group_name):
    return re.sub(r"\[[^\]]*\]\s*$", "", group_name or "").strip()


def main():
    tournaments = []
    for p in sorted(TOURN_DIR.glob("*.json")):
        tournaments.append(json.loads(p.read_text(encoding="utf-8")))

    index = []
    players = {}      # name -> {openid: {"units": set, "groups": set}}
    units = {}        # unit -> {openid: set(player names)}
    ranks = {}        # name -> list of rank refs(由 standings 來)
    unit_ranks = {}   # unit -> list of rank refs

    for t in tournaments:
        oid = t["openid"]
        index.append({
            "openid": oid,
            "name": t["name"],
            "city": t.get("city", ""),
            "status": t.get("status", ""),
            "registerStart": t.get("registerStart", ""),
            "registerEnd": t.get("registerEnd", ""),
            "dateStart": t.get("dateStart", ""),
            "dateEnd": t.get("dateEnd", ""),
            "venue": t.get("venue", ""),
            "image": t.get("image", ""),
            "category": t.get("category"),
            "hasMatches": bool(t.get("matches")),
            "hasRegulation": bool(t.get("regulation")),
            "groupCount": len(t.get("groups", [])),
        })

        def add_player(name, unit, group, openid=oid):
            rec = players.setdefault(name, {})
            ent = rec.setdefault(openid, {"units": set(), "groups": set()})
            if unit:
                ent["units"].add(unit)
            if group:
                ent["groups"].add(group)

        def add_unit(unit, name, openid=oid):
            if not unit:
                return
            units.setdefault(unit, {}).setdefault(openid, set())
            if name:
                units[unit][openid].add(name)

        for m in t.get("matches", []):
            g = base_group(m.get("groupName", ""))
            for side in ("A", "B"):
                unit = m.get("team" + side, "") or ""
                add_unit(unit, None)
                for si in m.get("scoreinfo", []):
                    for nm in split_members(si.get("member" + side)):
                        add_player(nm, unit, g)
                        add_unit(unit, nm)

        for s in t.get("standings", []):
            # 雙打跨單位時 memberUnits 與 members 對齊,逐位歸屬正確單位;
            # 否則全部歸屬 s.unit(單打或同單位雙打)。
            members = s.get("members", [])
            munits = s.get("memberUnits") or []
            all_members = []
            seen_units = []
            for i, nm in enumerate(members):
                unit_i = munits[i] if i < len(munits) else s.get("unit", "")
                for nm2 in split_members(nm):
                    all_members.append(nm2)
                    add_player(nm2, unit_i, s.get("group", ""))
                    add_unit(unit_i, nm2)
                    ranks.setdefault(nm2, []).append({
                        "openid": oid, "group": s.get("group", ""),
                        "rank": s.get("rank"), "unit": unit_i,
                    })
                if unit_i and unit_i not in seen_units:
                    seen_units.append(unit_i)
            # 團體賽:只有單位、無個別選手 → 仍登錄該單位名次
            if not members and s.get("unit"):
                add_unit(s["unit"], None)
                seen_units.append(s["unit"])
            for unit in seen_units:
                unit_ranks.setdefault(unit, []).append({
                    "openid": oid, "group": s.get("group", ""),
                    "rank": s.get("rank"), "members": all_members,
                })

    # 序列化(set → sorted list,壓縮格式)
    index.sort(key=lambda x: x.get("dateStart", ""), reverse=True)

    players_out = {}
    for name, recs in players.items():
        players_out[name] = [
            {"openid": oid, "units": sorted(e["units"]), "groups": sorted(e["groups"])}
            for oid, e in recs.items()
        ]
    ranks_out = ranks

    units_out = {}
    for unit, recs in units.items():
        units_out[unit] = {
            "t": [{"openid": oid, "players": sorted(names)} for oid, names in recs.items()],
            "ranks": unit_ranks.get(unit, []),
        }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DATA_DIR / "players.json").write_text(
        json.dumps({"players": players_out, "ranks": ranks_out},
                   ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DATA_DIR / "units.json").write_text(
        json.dumps(units_out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    sizes = {f: (DATA_DIR / f).stat().st_size // 1024 for f in
             ("index.json", "players.json", "units.json")}
    print(f"索引完成:{len(index)} 場賽事、{len(players_out)} 位選手、{len(units_out)} 個單位")
    print(f"檔案大小(KB):{sizes}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
