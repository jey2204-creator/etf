#!/usr/bin/env python3
"""從 GitHub Issue 新增或刪除追蹤的 ETF。

用法：python3 scripts/manage.py "<Issue 標題>" "<Issue 內文>"
標題格式：「新增 ETF 00713」或「刪除 ETF 0056」；內文可寫「類型：高股息」，沒寫就自動判斷。
執行結果（給 Issue 留言用）印在標準輸出；設定檔有變動時結束碼為 0，沒變動為 3，格式錯誤為 1。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import update as U  # noqa: E402

TYPES = ["市值型", "高股息", "主動式", "槓桿", "債券"]
ISSUERS = [("主動統一", "uni"), ("統一", "uni"), ("元大", "yuanta"), ("國泰", "cathay"),
           ("群益", "capital"), ("富邦", "fubon"), ("凱基", "kgi")]
NO_ID_NEEDED = {"yuanta", "fubon"}  # 這兩家只要 ETF 代號就查得到成份股


def guess_type(code: str, name: str) -> str:
    if code[-1] in "LR" or "正2" in name or "反1" in name or "槓桿" in name:
        return "槓桿"
    if code.endswith("B") or "債" in name:
        return "債券"
    if code.endswith("A") or name.startswith("主動"):
        return "主動式"
    if re.search(r"高股息|高息|股息|收益|息", name):
        return "高股息"
    return "市值型"


def div_count(code: str, market: str) -> int:
    """近一年除息次數，用來推算每年配息次數。"""
    end = datetime.now(U.TZ).date()
    start = end - timedelta(days=365)
    fn = U.src_tpex_div if market == "TPEX" else U.src_twse_div
    events, _ = fn({code}, start, end)
    return len({e["date"] for e in events if e["date"]})


def backfill_weekly(code: str, weeks: int = 13) -> int:
    """盡量回補近幾週的集保受益人數與單位數（集保網站擋住就跳過，之後每週會自動累積）。"""
    base = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"
    page = U.fetch_text("tdcc_page", base, tries=1)
    dates = re.findall(r'<option value="(\d{8})"', page)[:weeks + 1]
    rows = []
    for d in dates:
        page = U.fetch_text("tdcc_page", base, tries=1)
        m = re.search(r'name="SYNCHRONIZER_TOKEN" value="([^"]+)"', page)
        if not m:
            break
        body = (f"SYNCHRONIZER_TOKEN={m.group(1)}&SYNCHRONIZER_URI=%2Fportal%2Fzh%2FsmWeb%2FqryStock&method=submit"
                f"&firDate={dates[0]}&scaDate={d}&sqlMethod=StockNo&stockNo={code}&stockName=")
        html = U.fetch_text("tdcc_q", base, data=body, tries=1,
                            headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": base})
        r = re.search(r"合\s*計</td>\s*<td[^>]*>([\d,]+)</td>\s*<td[^>]*>([\d,]+)<", html)
        if r:
            rows.append({"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "code": code,
                         "holders": int(r.group(1).replace(",", "")), "units": int(r.group(2).replace(",", "")), "nav": ""})
    if not rows:
        return 0
    import csv
    cols = ["date", "code", "holders", "units", "nav"]
    existing = {}
    if U.WEEKLY.exists():
        with U.WEEKLY.open(encoding="utf-8") as fh:
            for x in csv.DictReader(fh):
                existing[(x["date"], x["code"])] = x
    for x in rows:
        existing.setdefault((x["date"], x["code"]), x)
    with U.WEEKLY.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for k in sorted(existing):
            w.writerow({c: existing[k].get(c, "") for c in cols})
    return len(rows)


def save(cfg):
    U.CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def add(cfg, code: str, want_type: str | None) -> tuple[int, str]:
    if any(e["code"] == code for e in cfg["etfs"]):
        return 3, f"ℹ️ {code} 已經在清單裡，沒有變動。"
    mis = U.fetch_json("mis", U.URLS["mis"])
    rows = [m for g in mis.get("a1", []) for m in (g.get("msgArray") or [])]
    hit = next((m for m in rows if str(m.get("a", "")).strip() == code), None)
    if not hit:
        return 3, f"❌ 證交所 ETF 清單裡找不到 {code}，請確認代號（只支援國內上市、上櫃 ETF）。"
    name = str(hit.get("b", "")).strip()
    try:
        tpex = {str(r.get("SecuritiesCompanyCode", "")).strip() for r in U.fetch_json("tpex_quotes", U.URLS["tpex_quotes"])}
    except Exception:  # noqa: BLE001
        tpex = set()
    market = "TPEX" if code in tpex else "TWSE"
    etype = want_type if want_type in TYPES else guess_type(code, name)
    div_failed = False
    try:
        n_div = div_count(code, market)
    except Exception:  # noqa: BLE001
        n_div, div_failed = 0, True
    div_note = {0: "不配息", 1: "年配", 2: "半年配", 4: "季配", 12: "月配"}.get(n_div, f"每年 {n_div} 次")
    issuer = next((k for p, k in ISSUERS if name.startswith(p)), None)
    entry = {
        "code": code, "name": name, "type": etype, "market": market,
        "div_per_year": n_div, "div_note": div_note,
        "fee": {"mgmt": [], "custody": [], "note": "費率待查（由網頁新增，請 Claude 補上）", "source": None, "checked": None},
        "added": datetime.now(U.TZ).date().isoformat(),
    }
    if issuer in NO_ID_NEEDED:
        entry["pcf"] = {"issuer": issuer}
    cfg["etfs"].append(entry)
    save(cfg)
    try:
        n_weeks = backfill_weekly(code)
    except Exception as e:  # noqa: BLE001
        n_weeks = 0
        print(f"[INFO] 集保回補失敗：{str(e)[:120]}", file=sys.stderr)
    lines = [
        f"✅ 已新增 **{code} {name}**",
        f"- 市場：{'上櫃' if market == 'TPEX' else '上市'}　類型：{etype}{'' if want_type in TYPES else '（自動判斷）'}",
        "- 配息：⚠️ 證交所查詢失敗，暫設不配息，請 Claude 協助修正" if div_failed else f"- 近一年配息 {n_div} 次 → {div_note}",
        f"- 成份股：{'已接上（' + U.ISSUER_NAMES[issuer] + '）' if issuer in NO_ID_NEEDED else '⚠️ 暫無，這家投信需要另外設定，請 Claude 協助'}",
        "- 管理費：⚠️ 待查，網頁先顯示「—」",
        f"- 資金流向：{'已回補近 ' + str(n_weeks) + ' 週' if n_weeks else '從下週起開始累積'}",
    ]
    return 0, "\n".join(lines)


def delete(cfg, code: str) -> tuple[int, str]:
    left = [e for e in cfg["etfs"] if e["code"] != code]
    if len(left) == len(cfg["etfs"]):
        return 3, f"ℹ️ 清單裡沒有 {code}，沒有變動。"
    if not left:
        return 3, "❌ 清單至少要保留一檔。"
    name = next(e["name"] for e in cfg["etfs"] if e["code"] == code)
    cfg["etfs"] = left
    save(cfg)
    return 0, f"🗑️ 已從清單移除 **{code} {name}**（歷史資料保留在 data/ 裡，之後重新加入可以接續使用）。"


def main() -> int:
    title = sys.argv[1] if len(sys.argv) > 1 else ""
    body = sys.argv[2] if len(sys.argv) > 2 else ""
    m = re.search(r"(新增|刪除|移除)\s*ETF\s*([0-9]{4,6}[A-Z]?)", title.upper().replace("ＥＴＦ", "ETF"))
    if not m:
        print("❌ 標題格式要是「新增 ETF 代號」或「刪除 ETF 代號」，例如：新增 ETF 00713")
        return 1
    action, code = m.group(1), m.group(2)
    t = re.search(r"類型[：:]\s*(\S+)", body or "")
    want = t.group(1) if t and t.group(1) in TYPES else None
    cfg = U.load_json(U.CONFIG, None)
    rc, msg = add(cfg, code, want) if action == "新增" else delete(cfg, code)
    print(msg)
    return rc


if __name__ == "__main__":
    sys.exit(main())
