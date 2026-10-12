#!/usr/bin/env python3
"""ETF 觀察表資料更新程式。

每次執行會抓取官方公開資料，輸出 data/etf.json，並把當日快照附加到 data/history.csv。
只用 Python 標準函式庫，不需要安裝任何套件。

資料來源：
  - 證交所 基本市況報導 MIS：市價、預估淨值、已發行單位數（上市＋上櫃 ETF）
  - 證交所 每日收盤行情（ETF）：成交量（上市）
  - 櫃買中心 OpenAPI 上櫃股票行情：成交量（上櫃，例如 00937B）
  - 集保結算所 集保戶股權分散表：受益人數（每週）
  - 證交所 / 櫃買中心 除權除息計算結果表：近一年配息
  - 證交所 / 櫃買中心 除權除息預告表：下次除息日
  - config/etfs.json：名稱、類型、配息頻率、費率級距（手動維護）

某個來源失敗時，沿用上一次成功的資料並在輸出中標記，程式最後以結束碼 2 通知排程。
本機測試可設定環境變數 ETF_FIXTURES=<資料夾>，改讀資料夾內的範例檔而不連網。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "etfs.json"
OUT = ROOT / "data" / "etf.json"
HISTORY = ROOT / "data" / "history.csv"
FIXTURES = os.environ.get("ETF_FIXTURES")
UA = "Mozilla/5.0 (compatible; etf-dashboard/1.0; +https://github.com/jey2204-creator/etf)"

URLS = {
    "mis": "https://mis.twse.com.tw/stock/data/all_etf.txt",
    "twse_quotes": "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?type=0099P&response=json",
    "tpex_quotes": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
    "tdcc": "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5",
    "twse_div": "https://www.twse.com.tw/rwd/zh/exRight/TWT49U?response=json&startDate={start}&endDate={end}",
    "tpex_div": "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ?startDate={start}&endDate={end}&response=json",
    "twse_pre": "https://www.twse.com.tw/rwd/zh/exRight/TWT48U?response=json",
    "tpex_pre": "https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost",
}
SOURCE_NAMES = {
    "mis": "證交所 MIS（市價、淨值、單位數）",
    "twse_quotes": "證交所 每日收盤行情（上市成交量）",
    "tpex_quotes": "櫃買中心 上櫃行情（上櫃成交量）",
    "tdcc": "集保 股權分散表（受益人數）",
    "twse_div": "證交所 除權息結果（配息）",
    "tpex_div": "櫃買 除權息結果（配息）",
    "twse_pre": "證交所 除權息預告",
    "tpex_pre": "櫃買 除權息預告",
}

# ---------------------------------------------------------------- 工具函式

def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # 部分政府網站憑證缺少 Subject Key Identifier，新版 Python 的嚴格模式會拒絕；仍保留憑證驗證。
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


_CTX = ssl_context()
_last_host_hit: dict[str, float] = {}


def fetch(key: str, url: str, *, tries: int = 3, timeout: int = 40) -> bytes:
    """下載資料；同一主機兩次請求至少間隔 2 秒，避免被交易所限流。"""
    if FIXTURES:
        return (Path(FIXTURES) / key).read_bytes()
    host = url.split("/")[2]
    last_err: Exception | None = None
    for attempt in range(tries):
        wait = 2.0 - (time.time() - _last_host_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        try:
            headers = {"User-Agent": UA, "Accept": "*/*"}
            if "mis.twse" in host:
                headers["Referer"] = "https://mis.twse.com.tw/stock/etf_nav.jsp?ex=tse"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                body = r.read()
            _last_host_hit[host] = time.time()
            if not body:
                raise ValueError("回應是空的")
            return body
        except Exception as e:  # noqa: BLE001
            _last_host_hit[host] = time.time()
            last_err = e
            time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"{type(last_err).__name__}: {last_err}")


def fetch_json(key: str, url: str):
    raw = fetch(key, url)
    text = raw.decode("utf-8-sig", errors="replace").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"不是 JSON（開頭：{text[:80]!r}）") from e


def num(v) -> float | None:
    """把 '1,234.50'、1234、'12327844000.000000'、'-'、'' 轉成數字或 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"<[^>]+>", "", str(v)).replace(",", "").strip()
    if s in ("", "-", "--", "N/A", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_date(s: str) -> date | None:
    """支援 '1151012'、'115/10/12'、'115年10月12日'、'20261012'。"""
    if s is None:
        return None
    s = re.sub(r"<[^>]+>", "", str(s)).strip()
    m = re.fullmatch(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", s) or re.fullmatch(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return date(y + 1911, mo, d)
    if re.fullmatch(r"\d{8}", s):
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    if re.fullmatch(r"\d{7}", s):
        return date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:]))
    return None


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def field_index(fields: list[str], *names: str) -> int:
    for i, f in enumerate(fields):
        clean = re.sub(r"<[^>]+>", "", f).strip()
        if clean in names:
            return i
    raise KeyError(f"找不到欄位 {names}，現有欄位：{fields}")


def tiered_rate(aum_e8: float | None, tiers: list) -> float | None:
    """級距採超額分段計算，回傳加權後的年費率（%）。"""
    if not tiers:
        return None
    if not aum_e8 or aum_e8 <= 0 or len(tiers) == 1:
        return float(tiers[0][1])
    remaining, lower, cost = aum_e8, 0.0, 0.0
    for upper, rate in tiers:
        cap = float("inf") if upper is None else upper - lower
        part = min(remaining, cap)
        cost += part * rate
        remaining -= part
        if upper is not None:
            lower = upper
        if remaining <= 0:
            break
    return cost / aum_e8


# ---------------------------------------------------------------- 各資料來源

def src_mis(codes):
    j = fetch_json("mis", URLS["mis"])
    rows = [m for g in j.get("a1", []) for m in (g.get("msgArray") or [])]
    if not rows:
        raise ValueError("MIS 回傳沒有任何 ETF")
    out, dates = {}, []
    for m in rows:
        code = str(m.get("a", "")).strip()
        if code not in codes:
            continue
        d = roc_date(m.get("i"))
        dates.append(d)
        out[code] = {
            "mis_name": str(m.get("b", "")).strip(),
            "price": num(m.get("e")),
            "nav": num(m.get("f")),
            "prem_src": num(m.get("g")),
            "prev_nav": num(m.get("h")),
            "units": num(m.get("c")),
            "units_chg": num(m.get("d")),
            "quote_date": iso(d),
            "quote_time": str(m.get("j", "")).strip() or None,
        }
    as_of = max((d for d in dates if d), default=None)
    return out, iso(as_of)


def src_twse_quotes(codes):
    j = fetch_json("twse_quotes", URLS["twse_quotes"])
    if str(j.get("stat", "")).upper() != "OK":
        raise ValueError(f"證交所回應 stat={j.get('stat')}")
    tables = j.get("tables") or []
    table = next((t for t in tables if t.get("fields") and "證券代號" in t["fields"] and "成交股數" in t["fields"]), None)
    if not table:
        raise ValueError("找不到 ETF 收盤行情表")
    f = table["fields"]
    ic, iv, ip = field_index(f, "證券代號"), field_index(f, "成交股數"), field_index(f, "收盤價")
    d = roc_date(j.get("date"))
    out = {}
    for r in table.get("data", []):
        code = str(r[ic]).strip()
        if code in codes:
            shares = num(r[iv])
            out[code] = {"volume": round(shares / 1000) if shares is not None else None,
                         "close": num(r[ip]), "volume_date": iso(d)}
    return out, iso(d)


def src_tpex_quotes(codes):
    j = fetch_json("tpex_quotes", URLS["tpex_quotes"])
    out, dates = {}, []
    for r in j:
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        if code in codes:
            d = roc_date(r.get("Date"))
            dates.append(d)
            shares = num(r.get("TradingShares"))
            out[code] = {"volume": round(shares / 1000) if shares is not None else None,
                         "close": num(r.get("Close")), "volume_date": iso(d)}
    return out, iso(max((d for d in dates if d), default=None))


def src_tdcc(codes):
    raw = fetch("tdcc", URLS["tdcc"]).decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(raw))
    header = [h.strip() for h in next(reader)]
    i_date, i_code = header.index("資料日期"), header.index("證券代號")
    i_lvl, i_ppl, i_sh = header.index("持股分級"), header.index("人數"), header.index("股數")
    out, as_of = {}, None
    for r in reader:
        if len(r) <= max(i_date, i_code, i_lvl, i_ppl, i_sh):
            continue
        code = r[i_code].strip()
        if code in codes and r[i_lvl].strip() == "17":  # 17 = 合計
            d = roc_date(r[i_date])
            as_of = max(filter(None, [as_of, d]), default=None)
            out[code] = {"holders": int(num(r[i_ppl]) or 0), "holders_units": num(r[i_sh]), "holders_date": iso(d)}
    if not out:
        raise ValueError("股權分散表內找不到任何追蹤中的 ETF")
    return out, iso(as_of)


def src_twse_div(codes, start: date, end: date):
    url = URLS["twse_div"].format(start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d"))
    j = fetch_json("twse_div", url)
    if str(j.get("stat", "")).upper() != "OK":
        raise ValueError(f"證交所回應 stat={j.get('stat')}")
    f = j["fields"]
    i_d, i_c, i_v, i_k = (field_index(f, "資料日期"), field_index(f, "股票代號"),
                          field_index(f, "權值+息值"), field_index(f, "權/息"))
    events = []
    for r in j.get("data", []):
        code = str(r[i_c]).strip()
        if code in codes and "息" in str(r[i_k]):
            events.append({"code": code, "date": roc_date(r[i_d]), "amount": num(r[i_v])})
    return events, iso(end)


def src_tpex_div(codes, start: date, end: date):
    url = URLS["tpex_div"].format(start=start.strftime("%Y/%m/%d"), end=end.strftime("%Y/%m/%d"))
    j = fetch_json("tpex_div", url)
    t = (j.get("tables") or [{}])[0]
    f = t.get("fields") or []
    i_d, i_c, i_v, i_k = (field_index(f, "除權息日期"), field_index(f, "代號"),
                          field_index(f, "息值"), field_index(f, "權/息"))
    events = []
    for r in t.get("data", []):
        code = str(r[i_c]).strip()
        if code in codes and "息" in str(r[i_k]):
            events.append({"code": code, "date": roc_date(r[i_d]), "amount": num(r[i_v])})
    return events, iso(end)


def src_twse_pre(codes):
    j = fetch_json("twse_pre", URLS["twse_pre"])
    if str(j.get("stat", "")).upper() != "OK":
        raise ValueError(f"證交所回應 stat={j.get('stat')}")
    f = j["fields"]
    i_d, i_c, i_k, i_v = (field_index(f, "除權除息日期"), field_index(f, "股票代號"),
                          field_index(f, "除權息"), field_index(f, "現金股利"))
    out = []
    for r in j.get("data", []):
        code = str(r[i_c]).strip()
        if code in codes and "息" in str(r[i_k]):
            out.append({"code": code, "date": roc_date(r[i_d]), "amount": num(r[i_v])})
    return out, None


def src_tpex_pre(codes):
    j = fetch_json("tpex_pre", URLS["tpex_pre"])
    out = []
    for r in j:
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        if code in codes and "息" in str(r.get("ExRrightsExDividend", "")):
            amt = num(r.get("CashDividend"))
            out.append({"code": code, "date": roc_date(r.get("ExRrightsExDividendDate")), "amount": amt if amt else None})
    return out, None


# ---------------------------------------------------------------- 主流程

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main() -> int:
    now = datetime.now(TZ)
    today = now.date()
    cfg = load_json(CONFIG, None)
    if not cfg:
        print("讀不到 config/etfs.json", file=sys.stderr)
        return 1
    etf_cfg = cfg["etfs"]
    codes = {e["code"] for e in etf_cfg}
    twse_codes = {e["code"] for e in etf_cfg if e.get("market", "TWSE") == "TWSE"}
    tpex_codes = codes - twse_codes

    prev = load_json(OUT, {})
    prev_etf = {e["code"]: e for e in prev.get("etfs", [])}
    prev_src = prev.get("sources", {})

    div_start = today - timedelta(days=400)
    jobs = {
        "mis": lambda: src_mis(codes),
        "twse_quotes": lambda: src_twse_quotes(twse_codes),
        "tpex_quotes": lambda: src_tpex_quotes(tpex_codes),
        "tdcc": lambda: src_tdcc(codes),
        "twse_div": lambda: src_twse_div(twse_codes, div_start, today),
        "tpex_div": lambda: src_tpex_div(tpex_codes, div_start, today),
        "twse_pre": lambda: src_twse_pre(twse_codes),
        "tpex_pre": lambda: src_tpex_pre(tpex_codes),
    }
    if not tpex_codes:
        for k in ("tpex_quotes", "tpex_div", "tpex_pre"):
            jobs.pop(k)

    results, sources = {}, {}
    for key, job in jobs.items():
        try:
            data, as_of = job()
            results[key] = data
            sources[key] = {"name": SOURCE_NAMES[key], "ok": True, "as_of": as_of, "fetched_at": now.isoformat(timespec="seconds")}
            print(f"[OK]   {key}: {as_of}")
        except Exception as e:  # noqa: BLE001
            old = prev_src.get(key, {})
            sources[key] = {"name": SOURCE_NAMES[key], "ok": False, "as_of": old.get("as_of"),
                            "fetched_at": old.get("fetched_at"), "error": str(e)[:300]}
            print(f"[FAIL] {key}: {e}", file=sys.stderr)

    warnings = []
    etfs = []
    div_ok = all(sources.get(k, {"ok": True})["ok"] for k in ("twse_div", "tpex_div"))
    events = results.get("twse_div", []) + results.get("tpex_div", [])
    upcoming = results.get("twse_pre", []) + results.get("tpex_pre", [])

    for c in etf_cfg:
        code = c["code"]
        rec = dict(prev_etf.get(code, {}))  # 先沿用上次資料，成功的來源再覆蓋
        rec.update({"code": code, "name": c["name"], "type": c["type"],
                    "market": "上櫃" if c.get("market") == "TPEX" else "上市"})

        m = results.get("mis", {}).get(code)
        if m:
            rec.update(m)
            price, nav = m["price"], m["nav"]
            rec["prem"] = round((price - nav) / nav * 100, 3) if price and nav else None
            base_nav = nav or m["prev_nav"]
            rec["aum"] = round(m["units"] * base_nav / 1e8, 2) if m["units"] and base_nav else None
        elif "mis" in results:
            warnings.append(f"{code}：MIS 沒有這檔的資料")

        q = results.get("twse_quotes", {}).get(code) or results.get("tpex_quotes", {}).get(code)
        if q:
            rec["volume"], rec["volume_date"] = q["volume"], q["volume_date"]
            if q["close"] and rec.get("price") and rec.get("quote_date") == q["volume_date"] \
                    and abs(q["close"] - rec["price"]) / q["close"] > 0.02:
                warnings.append(f"{code}：MIS 成交價 {rec['price']} 與收盤價 {q['close']} 差距超過 2%")

        t = results.get("tdcc", {}).get(code)
        if t:
            rec.update(t)

        # 費率依目前規模計算
        fee = c.get("fee", {})
        aum = rec.get("aum")
        mg, cu = tiered_rate(aum, fee.get("mgmt")), tiered_rate(aum, fee.get("custody"))
        rec["fee"] = {
            "mgmt": round(mg, 4) if mg is not None else None,
            "custody": round(cu, 4) if cu is not None else None,
            "total": round(mg + cu, 4) if mg is not None and cu is not None else None,
            "tiered": len(fee.get("mgmt") or []) > 1 or len(fee.get("custody") or []) > 1,
            "note": fee.get("note"), "source": fee.get("source"), "checked": fee.get("checked"),
        }

        # 配息：取最近 N 次（N = 每年配息次數）
        n = int(c.get("div_per_year", 0))
        div = {"per_year": n, "freq": c.get("div_note")}
        if div_ok:
            mine = sorted((e for e in events if e["code"] == code and e["date"] and e["amount"]),
                          key=lambda e: e["date"], reverse=True)
            take = mine[:n] if n else []
            ttm = round(sum(e["amount"] for e in take), 4) if take else 0.0
            px = rec.get("price") or rec.get("nav")
            div.update({
                "ttm": ttm if n else None,
                "count": len(take),
                "yield": round(ttm / px * 100, 2) if n and px and take else None,
                "last": {"date": iso(mine[0]["date"]), "amount": mine[0]["amount"]} if mine else None,
            })
        else:
            div.update({k: (rec.get("div") or {}).get(k) for k in ("ttm", "count", "yield", "last")})
        nxt = sorted((e for e in upcoming if e["code"] == code and e["date"] and e["date"] >= today), key=lambda e: e["date"])
        if nxt or "twse_pre" in results:
            div["next"] = {"date": iso(nxt[0]["date"]), "amount": nxt[0]["amount"]} if nxt else None
        else:
            div["next"] = (rec.get("div") or {}).get("next")
        rec["div"] = div

        # 基本檢查：數字明顯不合理就提出警告
        p = rec.get("prem")
        if p is not None and abs(p) > 10:
            warnings.append(f"{code}：折溢價 {p}% 異常，請人工確認")
        if rec.get("price") is None:
            warnings.append(f"{code}：沒有市價")
        etfs.append(rec)

    out = {
        "schema": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "sources": sources,
        "warnings": warnings,
        "holdings": "sample",
        "etfs": etfs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    append_history(etfs)

    failed = [k for k, s in sources.items() if not s["ok"]]
    for w in warnings:
        print(f"[WARN] {w}", file=sys.stderr)
    print(f"完成：{len(etfs)} 檔，失敗來源 {failed or '無'}，警告 {len(warnings)} 則")
    return 2 if failed else 0


def append_history(etfs):
    """每個交易日每檔一列，同一天重跑會覆蓋。"""
    cols = ["date", "code", "price", "nav", "prem", "aum", "units", "holders", "holders_date", "volume"]
    rows = {}
    if HISTORY.exists():
        with HISTORY.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows[(r["date"], r["code"])] = r
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return v

    for e in etfs:
        if e.get("quote_date") and e.get("price") is not None:
            rows[(e["quote_date"], e["code"])] = {k: cell(e.get("quote_date" if k == "date" else k)) for k in cols}
    with HISTORY.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for key in sorted(rows):
            w.writerow({k: rows[key].get(k, "") for k in cols})


if __name__ == "__main__":
    sys.exit(main())
