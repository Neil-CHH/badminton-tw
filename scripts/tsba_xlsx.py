# -*- coding: utf-8 -*-
"""tsbadminton 賽程表/秩序冊 xlsx 的解析工具。

這些 xlsx 是「空間排版的籤表樹」,無法還原成逐場比分,但兩件事很有價值:

1. **參賽名冊** — 每個籤位都是「序號｜單位｜姓名」的相鄰儲存格,是真文字。
   成績只有 JPG 圖片,視覺解析容易讀錯字;有名冊就能把「自由辨識」降成
   「從已知名單挑一個」,精確度差很多(見 tsba_reconcile.py)。
2. **比賽日期** — 日賽程分頁有真正的比賽日期,列表頁只有公告有效期(非賽期)。

需要 openpyxl。
"""
import datetime
import re

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None

# 組別標題(一張分頁內可能有多個組別,如 U11男單 / U11女單 / U11男雙)
EVENT_PAT = re.compile(r"(男單|女單|男雙|女雙|混雙|男團|女團|團體)")
_SEED = re.compile(r"\s*[\[［]\s*\d+\s*[\]］]\s*$")   # 姓名後的種子序號
_BYE = re.compile(r"^(bye|輪空)", re.I)
_TIME = re.compile(r"^\d{1,2}:\d{2}")
_NOISE = re.compile(r"^[\d\s:：/\-]+$")
# 表頭字樣,避免把「組別｜冠軍」這種標題列當成「單位｜姓名」
_HEADER = {"組別", "組  別", "編號", "姓名", "單位", "名次", "日期", "時間", "場次",
           "備註", "隊名", "選手", "冠軍", "亞軍", "季軍", "殿軍", "第五名", "場地",
           "序號", "隊伍", "學校", "球隊", "組別名稱"}


def _cells(row):
    return [("" if v is None else str(v).strip()) for v in row]


def _is_name(s):
    """姓名:2-8 字、無數字、非時間、非 Bye。"""
    if not (2 <= len(s) <= 8) or _BYE.match(s) or _TIME.match(s):
        return False
    if s.replace(" ", "") in _HEADER:
        return False
    return not any(ch.isdigit() for ch in s) and ":" not in s and "：" not in s


def _is_unit(s):
    if s.replace(" ", "") in _HEADER or EVENT_PAT.search(s):
        return False
    return 1 < len(s) <= 16 and not _BYE.match(s) and not _NOISE.match(s)


def extract_roster(path, sheets=None):
    """回傳 {組別: [(單位, 姓名)]}。組別取該列上方最近一個含賽別關鍵字的標題。

    找「序號｜單位｜姓名」三格相鄰的樣式;序號是純數字(籤位號)。
    """
    if openpyxl is None:
        raise RuntimeError("需要 openpyxl:pip install openpyxl")
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    roster = {}
    for sname in (sheets or wb.sheetnames):
        if sname not in wb.sheetnames:
            continue
        ws = wb[sname]
        current = sname
        for row in ws.iter_rows(values_only=True):
            cells = _cells(row)
            for c in cells:
                if c and EVENT_PAT.search(c) and len(c) <= 24:
                    current = EVENT_PAT.sub(lambda m: m.group(1), c).strip()
                    current = re.sub(r"\s*\d+[-–]\d+\s*$", "", current).strip()
                    break
            # 「單位｜姓名」相鄰即成立,不要求前面有籤位序號:雙打的一組佔兩列,
            # 第一位搭檔那列沒有序號(實測 U9男雙 r648 吳敏岳 / r649「1」顏弘寔),
            # 若強制要有序號會漏掉每一組的第一位。
            for i in range(len(cells) - 1):
                unit, name = cells[i], cells[i + 1]
                if not unit or not name:
                    continue
                nm = _SEED.sub("", name).strip()
                if _is_unit(unit) and _is_name(nm):
                    roster.setdefault(current, [])
                    if (unit, nm) not in roster[current]:
                        roster[current].append((unit, nm))
    wb.close()
    return roster


def extract_dates(path, year=None):
    """回傳 (dateStart, dateEnd)。掃所有分頁的日期型儲存格,取最小/最大。

    year 給定時只採該年度的日期,濾掉版本日期之類的雜訊。
    """
    if openpyxl is None:
        raise RuntimeError("需要 openpyxl:pip install openpyxl")
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    found = set()
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for v in row:
                d = None
                if isinstance(v, datetime.datetime):
                    d = v.date()
                elif isinstance(v, datetime.date):
                    d = v
                elif isinstance(v, str):
                    m = re.match(r"^(20\d\d)[-/](\d{1,2})[-/](\d{1,2})", v.strip())
                    if m:
                        try:
                            d = datetime.date(*(int(x) for x in m.groups()))
                        except ValueError:
                            d = None
                if d and (year is None or d.year == int(year)):
                    found.add(d)
    wb.close()
    if not found:
        return "", ""
    return min(found).isoformat(), max(found).isoformat()
