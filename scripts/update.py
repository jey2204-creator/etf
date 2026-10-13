#!/usr/bin/env python3
"""ETF 觀察表資料更新程式。

每次執行會抓取官方公開資料，輸出 data/etf.json、data/holdings.json，並把當日快照附加到 data/history.csv。
只用 Python 標準函式庫，不需要安裝任何套件。

資料來源：
  - 證交所 基本市況報導 MIS：市價、預估淨值、已發行單位數（上市＋上櫃 ETF）
  - 證交所 每日收盤行情（ETF）：成交量（上市）
  - 櫃買中心 OpenAPI 上櫃股票行情：成交量（上櫃，例如 00937B）
  - 集保結算所 集保戶股權分散表：受益人數（每週）
  - 證交所 / 櫃買中心 除權除息計算結果表：近一年配息
  - 證交所 / 櫃買中心 除權除息預告表：下次除息日
  - 各投信每日申購買回清單：成份股股數與權重（元大、國泰、群益、富邦、統一、凱基）
  - config/etfs.json：名稱、類型、配息頻率、費率級距、成份股來源（手動維護）

某個來源失敗時，沿用上一次成功的資料並在輸出中標記，程式最後以結束碼 2 通知排程。
例外：設定為 client 的成份股來源（國泰會擋海外主機）由網頁端直接讀取，伺服器端失敗不算失敗。
本機測試可設定環境變數 ETF_FIXTURES=<資料夾>，改讀資料夾內的範例檔而不連網。
"""
from __future__ import annotations

import csv
import html as htmllib
import http.cookiejar
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "etfs.json"
OUT = ROOT / "data" / "etf.json"
HOLDINGS = ROOT / "data" / "holdings.json"
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
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    urllib.request.HTTPSHandler(context=_CTX),
)
_last_host_hit: dict[str, float] = {}
_aia_tried: set[str] = set()


def _add_intermediate(host: str) -> bool:
    """有些網站（例如大華銀）只送出自己的憑證、漏了中繼憑證，瀏覽器會自動補，Python 不會。
    這裡照憑證裡的 caIssuers 網址把中繼憑證下載回來加進信任清單，仍然完整驗證憑證鏈。"""
    if host in _aia_tried:
        return False
    _aia_tried.add(host)
    import tempfile
    pem = ssl.get_server_certificate((host, 443), timeout=20)
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as fh:
        fh.write(pem)
    info = ssl._ssl._test_decode_cert(fh.name)  # noqa: SLF001（標準函式庫沒有公開的憑證解析）
    os.unlink(fh.name)
    added = False
    for url in info.get("caIssuers") or ():
        der = urllib.request.urlopen(url, timeout=20).read()  # noqa: S310（憑證機構的公開網址）
        cadata = der.decode() if der.lstrip().startswith(b"-----") else ssl.DER_cert_to_PEM_cert(der)
        _CTX.load_verify_locations(cadata=cadata)
        added = True
    print(f"[INFO] {host} 憑證缺中繼憑證，已自動補上：{added}")
    return added


def fetch(key: str, url: str, *, data: bytes | str | None = None, headers: dict | None = None,
          tries: int = 3, timeout: int = 40) -> bytes:
    """下載資料（有 data 時用 POST）；同一主機兩次請求至少間隔 2 秒，避免被限流。會保留 cookie。"""
    if FIXTURES:
        return (Path(FIXTURES) / key).read_bytes()
    host = url.split("/")[2]
    body_out = data.encode("utf-8") if isinstance(data, str) else data
    last_err: Exception | None = None
    for attempt in range(tries):
        wait = 2.0 - (time.time() - _last_host_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        try:
            h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-TW,zh;q=0.9"}
            if "mis.twse" in host:
                h["Referer"] = "https://mis.twse.com.tw/stock/etf_nav.jsp?ex=tse"
            if headers:
                h.update(headers)
            req = urllib.request.Request(url, data=body_out, headers=h, method="POST" if body_out is not None else "GET")
            with _OPENER.open(req, timeout=timeout) as r:
                body = r.read()
            _last_host_hit[host] = time.time()
            if not body:
                raise ValueError("回應是空的")
            return body
        except Exception as e:  # noqa: BLE001
            _last_host_hit[host] = time.time()
            last_err = e
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLCertVerificationError) and "local issuer" in str(reason):
                try:
                    if _add_intermediate(host):
                        continue
                except Exception as e2:  # noqa: BLE001
                    print(f"[INFO] {host} 補中繼憑證失敗：{e2}", file=sys.stderr)
            if isinstance(e, urllib.error.HTTPError):  # 記下伺服器回應的開頭，方便判斷是否被防火牆擋
                try:
                    snippet = re.sub(r"\s+", " ", e.read(300).decode("utf-8", "replace"))
                    last_err = RuntimeError(f"HTTP {e.code}（{e.headers.get('Server', '')}）{snippet[:160]}")
                except Exception:  # noqa: BLE001
                    pass
                if e.code in (401, 403, 404, 410):  # 伺服器明確拒絕，重試也沒用
                    break
            time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"{type(last_err).__name__}: {last_err}")


def fetch_text(key: str, url: str, **kw) -> str:
    return fetch(key, url, **kw).decode("utf-8-sig", errors="replace")


def fetch_json(key: str, url: str, **kw):
    raw = fetch(key, url, **kw)
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

MIS_RETRY_WAIT = 20  # 秒；投信更新預估淨值時，該投信整組會短暫從清單消失


def src_mis(codes):
    rows, got = [], {}
    for attempt in range(3):  # 有缺檔就等一下再抓，最多共 3 次，各次結果合併
        if attempt:
            print(f"[INFO] MIS 缺 {sorted(codes - set(got))}，{MIS_RETRY_WAIT} 秒後重抓", file=sys.stderr)
            time.sleep(MIS_RETRY_WAIT)
        try:
            j = fetch_json("mis", URLS["mis"])
        except Exception:  # noqa: BLE001
            if not got:
                raise
            break
        for m in (m for g in j.get("a1", []) for m in (g.get("msgArray") or [])):
            code = str(m.get("a", "")).strip()
            if code in codes and code not in got:
                got[code] = m
        if codes <= set(got):
            break
    rows = list(got.values())
    if not rows:
        raise ValueError("MIS 回傳沒有任何 ETF")
    out, dates = {}, []
    for m in rows:
        code = str(m.get("a", "")).strip()
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


# ---------------------------------------------------------------- 成份股（各投信每日申購買回清單）
# 每筆持股統一成 [種類, 代號, 名稱, 數量, 權重%]；種類 s=股票（數量為股數）、f=期貨（口數）、
# b=債券（面額）、e=ETF（單位數）。

ISSUER_NAMES = {"yuanta": "元大投信", "cathay": "國泰投信", "capital": "群益投信",
                "fubon": "富邦投信", "uni": "統一投信", "kgi": "凱基投信",
                "fhtrust": "復華投信", "ctbc": "中國信託投信", "blackrock": "貝萊德投信",
                "tsit": "台新投信", "uobam": "大華銀投信"}


class _TableParser(HTMLParser):
    """把 HTML 裡的每個 <table> 轉成「列 → 儲存格文字」的清單。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._stack: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            t: list[list[str]] = []
            self.tables.append(t)
            self._stack.append(t)
        elif tag == "tr" and self._stack:
            self._row = []
            self._stack[-1].append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr":
            self._row = None
        elif tag == "table" and self._stack:
            self._stack.pop()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_tables(html: str):
    p = _TableParser()
    p.feed(html)
    return p.tables


def table_rows(tables, *needed: str):
    """找出表頭含有所有指定欄位的表格，回傳 (欄位索引, 資料列)。找不到回傳 (None, [])。"""
    for t in tables:
        for i, row in enumerate(t):
            if all(n in row for n in needed):
                return {h: row.index(h) for h in row if h}, t[i + 1:]
    return None, []


def ms_date(s) -> date | None:
    """支援 '/Date(1791734400000)/'、'2026-10-12T00:00:00'、'2026/10/12' 等格式。"""
    if s is None:
        return None
    s = str(s).strip()
    m = re.search(r"Date\((\d+)", s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, TZ).date()
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return date(*(int(x) for x in m.groups()))
    return roc_date(s)


def _item(kind, code, name, qty, weight):
    q, w = num(qty), num(weight)
    if q is not None and float(q).is_integer():
        q = int(q)
    return [kind, str(code or "").strip(), re.sub(r"\s+", " ", str(name or "")).strip().rstrip("*"),
            q, round(w, 4) if w is not None else None]


def pcf_yuanta(code, _id):
    url = ("https://etfapi.yuantaetfs.com/ectranslation/api/bridge?APIType=ETFAPI&CompanyName=YUANTAFUNDS"
           f"&DeviceId=null&FuncId=PCF%2FDaily&AppName=ETF&Device=3&Platform=ETF&ticker={code}")
    j = fetch_json(f"pcf_{code}", url)
    if "FundWeights" not in j and isinstance(j.get("Data"), dict):
        j = j["Data"]
    fw = j.get("FundWeights") or {}
    items = []
    for kind, key in (("s", "StockWeights"), ("e", "ETFWeights"), ("b", "BondWeights"), ("f", "FutureWeights")):
        for x in fw.get(key) or []:
            name = x.get("name") or x.get("ename")
            if kind == "f" and x.get("ym"):
                name = f"{name} {x['ym']}"
            items.append(_item(kind, x.get("code"), name, x.get("qty"), x.get("weights")))
    return items, roc_date((j.get("PCF") or {}).get("trandate"))


def pcf_cathay(code, fund):
    # 國泰的網站防火牆會拒絕海外主機（GitHub 的伺服器在美國），在台灣執行才會成功；
    # 設定檔標 client: true，網頁開啟時會改由使用者的裝置直接讀取同一組 API。
    base = "https://cwapi.cathaysite.com.tw/api/ETF/"
    hdr = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.cathaysite.com.tw",
           "Referer": "https://www.cathaysite.com.tw/", "lang": "zh_TW"}
    a = fetch_json(f"pcf_{code}_assets", f"{base}GetETFAssets?FundCode={fund}&status=1", headers=hdr)
    d = ms_date((a.get("result") or {}).get("preDate"))
    if not d:
        raise ValueError("國泰沒有回傳資料日期")
    q = f"FundCode={fund}&SearchDate={d.isoformat()}&status=1"
    items = []
    for x in fetch_json(f"pcf_{code}_stocks", f"{base}GetETFDetailStockList?{q}", headers=hdr).get("result") or []:
        items.append(_item("s", x.get("stockCode"), x.get("stockName"), x.get("volumn"), x.get("weights")))
    for x in fetch_json(f"pcf_{code}_futures", f"{base}GetETFDetailFutureList?{q}", headers=hdr).get("result") or []:
        items.append(_item("f", x.get("ftNo"), f"{x.get('ftName', '')} {x.get('ftDate', '')}".strip(),
                           x.get("volumn"), x.get("ntMkval")))
    return items, d


def pcf_capital(code, fund):
    try:  # 先開一次網頁取得 cookie，網站有防機器人機制
        fetch(f"pcf_{code}_warmup", f"https://www.capitalfund.com.tw/etf/product/detail/{fund or 195}/portfolio", tries=1)
    except Exception:  # noqa: BLE001
        pass
    if not fund:  # 沒填基金代碼就從群益的 ETF 清單查（代號 → fundNo）
        lst = fetch_json(f"pcf_{code}_list", "https://www.capitalfund.com.tw/CFWeb/api/etf/list", data="{}",
                         headers={"Content-Type": "application/json"})
        fund = next((f.get("fundNo") for f in ((lst.get("data") or {}).get("funds") or [])
                     if str(f.get("stockNo", "")).strip() == code), None)
        if not fund:
            raise ValueError(f"群益 ETF 清單找不到 {code}")
    j = fetch_json(f"pcf_{code}", "https://www.capitalfund.com.tw/CFWeb/api/etf/buyback",
                   data=json.dumps({"fundId": str(fund), "date": None}),
                   headers={"Content-Type": "application/json", "Referer":
                            f"https://www.capitalfund.com.tw/etf/product/detail/{fund}/portfolio"})
    d = j.get("data") or {}
    items = []
    for x in d.get("stocks") or []:
        items.append(_item("s", x.get("stocNo"), x.get("stocName"), x.get("share"), x.get("weight")))
    for x in d.get("bonds") or []:
        items.append(_item("b", x.get("bondNo"), x.get("bondName"), x.get("faceValue"), x.get("weight")))
    for x in d.get("futures") or []:
        items.append(_item("f", x.get("txEname"), x.get("txDesc"), x.get("lot"), x.get("weight")))
    pcf = d.get("pcf") or {}
    return items, ms_date(pcf.get("date2") or pcf.get("date1"))


def pcf_fubon(code, _id):
    html = fetch_text(f"pcf_{code}", f"https://websys.fsit.com.tw/FubonETF/Fund/Assets.aspx?stkId={code}")
    m = re.search(r"資料日期[：:]\s*(\d{4}/\d{1,2}/\d{1,2})", html)
    tables = html_tables(html)
    items = []
    idx, rows = table_rows(tables, "股票代碼", "股票名稱", "股數", "權重(%)")
    for r in rows:
        # 國內股票是 4–6 碼；海外股票像「VIC VN」「AAPL US」，代號後面帶市場別
        if idx and len(r) > max(idx.values()) and re.fullmatch(r"[0-9A-Z][0-9A-Z.\-/]{0,11}( [A-Z]{2})?", r[idx["股票代碼"]]):
            items.append(_item("s", r[idx["股票代碼"]], r[idx["股票名稱"]], r[idx["股數"]], r[idx["權重(%)"]]))
    idx, rows = table_rows(tables, "期貨代碼", "期貨名稱", "口數", "權重(%)")
    for r in rows:
        if idx and len(r) > max(idx.values()) and r[idx["期貨代碼"]] and "合計" not in r[idx["期貨代碼"]]:
            items.append(_item("f", r[idx["期貨代碼"]], r[idx["期貨名稱"]], r[idx["口數"]], r[idx["權重(%)"]]))
    return items, ms_date(m.group(1)) if m else None


def pcf_uni(code, fund):
    today = datetime.now(TZ).date()
    roc = f"{today.year - 1911}/{today.month:02d}/{today.day:02d}"
    try:
        fetch(f"pcf_{code}_warmup", f"https://www.ezmoney.com.tw/ETF/Transaction/PCF?fundCode={fund}", tries=1)
    except Exception:  # noqa: BLE001
        pass
    j = fetch_json(f"pcf_{code}", "https://www.ezmoney.com.tw/ETF/Transaction/GetPCF",
                   data=json.dumps({"fundCode": fund, "date": roc, "specificDate": False}),
                   headers={"Content-Type": "application/json; charset=utf-8", "X-Requested-With": "XMLHttpRequest",
                            "Referer": f"https://www.ezmoney.com.tw/ETF/Transaction/PCF?fundCode={fund}"})
    kinds = {"ST": "s", "GD": "f", "BD": "b", "EF": "e", "ETF": "e"}
    items, d = [], None
    for a in j.get("asset") or []:
        kind = kinds.get(str(a.get("AssetCode", "")).strip())
        if not kind:
            continue
        for x in a.get("Details") or []:
            name = x.get("DetailName")
            if kind == "f" and x.get("MTH"):
                name = f"{name} {x['MTH']}"
            items.append(_item(kind, x.get("DetailCode"), name, x.get("Share"), x.get("NavRate")))
            d = d or ms_date(x.get("TranDate"))
    return items, d


def pcf_kgi(code, fund):
    try:
        fetch(f"pcf_{code}_warmup", "https://www.kgifund.com.tw/Fund/RedemptionList", tries=1)
    except Exception:  # noqa: BLE001
        pass
    html = fetch_text(f"pcf_{code}", "https://www.kgifund.com.tw/Fund/RedemptionVC", data=f"fundID={fund}",
                      headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                               "X-Requested-With": "XMLHttpRequest",
                               "Referer": "https://www.kgifund.com.tw/Fund/RedemptionList"})
    tables = html_tables(html)
    items = []
    idx, rows = table_rows(tables, "股票代號", "股票名稱", "股數", "權重(%)")
    for r in rows:
        if idx and len(r) > max(idx.values()) and re.fullmatch(r"[0-9A-Z]{4,6}", r[idx["股票代號"]]):
            items.append(_item("s", r[idx["股票代號"]], r[idx["股票名稱"]], r[idx["股數"]], r[idx["權重(%)"]]))
    idx, rows = table_rows(tables, "期貨代號", "期貨名稱", "口數", "權重(%)")
    for r in rows:
        if idx and len(r) > max(idx.values()) and r[idx["期貨代號"]] and "合計" not in r[idx["期貨代號"]]:
            name = r[idx["期貨名稱"]]
            if "契約年月" in idx and r[idx["契約年月"]]:
                name = f"{name} {r[idx['契約年月']]}"
            items.append(_item("f", r[idx["期貨代號"]], name, r[idx["口數"]], r[idx["權重(%)"]]))
    plain = htmllib.unescape(re.sub(r"<[^>]+>", " ", html))
    m = re.search(r"\((\d{4}/\d{1,2}/\d{1,2})\)\s*每受益權單位淨資產價值", plain) or \
        re.search(r'id="DataDate"[^>]*value="(\d{4}/\d{1,2}/\d{1,2})"', html)
    return items, ms_date(m.group(1)) if m else None


def fhtrust_fund_id(code):
    """復華投信網站的基金代碼（例如 00929 → ETF21）。"""
    j = fetch_json("fhtrust_list", "https://www.fhtrust.com.tw/api/fundList")
    for x in j.get("result") or []:
        if str(x.get("etf002", "")).strip() == code:
            return x.get("fundID")
    raise ValueError(f"復華基金清單找不到 {code}")


def pcf_fhtrust(code, fund):
    fund = fund or fhtrust_fund_id(code)
    day = datetime.now(TZ).date()
    for _ in range(8):  # 當天的清單晚上才出來，往前找最近有資料的一天
        j = fetch_json(f"pcf_{code}", f"https://www.fhtrust.com.tw/api/assets?fundID={fund}&qDate={day:%Y/%m/%d}",
                       headers={"Referer": f"https://www.fhtrust.com.tw/ETF/etf_detail/{fund}"})
        r = (j.get("result") or [{}])[0]
        if r.get("detail"):
            kinds = {"股票": "s", "期貨": "f", "債券": "b", "基金": "e"}
            items = [_item(kinds[x["ftype"]], x.get("stockid"), x.get("stockname"), x.get("qshare"),
                           str(x.get("prate_addaccint", "")).rstrip("%"))
                     for x in r["detail"] if x.get("ftype") in kinds]
            return items, ms_date(r.get("dDate"))
        if FIXTURES:
            break
        day -= timedelta(days=1)
    raise ValueError("復華最近 8 天都沒有資產明細")


INDUSTRY = {"01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織", "05": "電機機械", "06": "電器電纜",
            "08": "玻璃陶瓷", "09": "造紙", "10": "鋼鐵", "11": "橡膠", "12": "汽車", "14": "建材營造",
            "15": "航運", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨", "20": "其他", "21": "化學",
            "22": "生技醫療", "23": "油電燃氣", "24": "半導體", "25": "電腦週邊", "26": "光電", "27": "通信網路",
            "28": "電子零組件", "29": "電子通路", "30": "資訊服務", "31": "其他電子", "32": "文化創意",
            "33": "農業科技", "34": "電子商務", "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活"}


def src_caps():
    """上市櫃普通股市值（億元）與產業：收盤價 × 已發行股數。回傳 {代號: [市值億, 產業]}。"""
    out = {}
    co = {str(r.get("公司代號", "")).strip(): r for r in
          fetch_json("twse_co", "https://openapi.twse.com.tw/v1/opendata/t187ap03_L")}
    for r in fetch_json("twse_all", "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"):
        c = str(r.get("Code", "")).strip()
        px, info = num(r.get("ClosingPrice")), co.get(c)
        sh = num(info.get("已發行普通股數或TDR原股發行股數")) if info else None
        if re.fullmatch(r"\d{4}", c) and px and sh:
            out[c] = [round(px * sh / 1e8), INDUSTRY.get(str(info.get("產業別", "")).strip(), "")]
    ind = {}
    try:
        ind = {str(r.get("SecuritiesCompanyCode", "")).strip(): str(r.get("SecuritiesIndustryCode", "")).strip()
               for r in fetch_json("tpex_co", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O")}
    except Exception:  # noqa: BLE001
        pass
    for r in fetch_json("tpex_quotes", URLS["tpex_quotes"]):
        c = str(r.get("SecuritiesCompanyCode", "")).strip()
        px, sh = num(r.get("Close")), num(r.get("Capitals"))
        if re.fullmatch(r"\d{4}", c) and px and sh:
            out[c] = [round(px * sh / 1e8), INDUSTRY.get(ind.get(c, ""), "")]
    if len(out) < 500:
        raise ValueError(f"市值資料只有 {len(out)} 檔，不合理")
    return out


def pcf_ctbc(code, fund):
    """中國信託投信：先取通行碼，再用 ETF 清單把代號換成基金代碼（例如 009828 → E0039），最後查申購買回清單。"""
    api = "https://www.ctbcinvestments.com.tw/API/"
    hdr = {"Content-Type": "application/json; charset=utf-8", "Origin": "https://www.ctbcinvestments.com",
           "Referer": "https://www.ctbcinvestments.com/"}

    def post(path, token, body=None):
        j = fetch_json(f"pcf_{code}_{path.split('/')[-1]}", f"{api}{path}?token={urllib.parse.quote(token)}",
                       data=json.dumps(dict({"token": token}, **(body or {}))), headers=hdr)
        if j.get("ResultCode") != 0:
            raise ValueError(f"中信 {path}：{j.get('ResultMsg')}")
        return j.get("Data")

    token = post("home/AuthToken", "www.ctbcinvestments.com")["token"]
    if not fund:
        rows = (post("etf/ETFList", token, {"IsWithETF": "Y"}) or {}).get("Data") or []
        fund = next((r.get("FID") for r in rows if str(r.get("ETF_ID", "")).strip() == code), None)
        if not fund:
            raise ValueError(f"中信 ETF 清單找不到 {code}")
    kinds = {"STOCK": "s", "FUTURE": "f", "FUTURES": "f", "BOND": "b", "ETF": "e", "FUND": "e"}
    day = datetime.now(TZ).date()
    for _ in range(8):  # 公告日通常是下一個營業日，往前找最近有資料的一天
        d = post("etf/Buyback", token, {"FID": fund, "StartDate": f"{day:%Y/%m/%d}"}) or {}
        groups = d.get("Detail") or []
        items = [_item(kinds[g["Code"]], x.get("code_"), x.get("name_"), x.get("qty_"), x.get("weights_"))
                 for g in groups if g.get("Code") in kinds for x in (g.get("Data") or [])]
        if items:
            head = (d.get("Data") or [{}])[0]
            return items, ms_date(head.get("公告日")) or day
        if FIXTURES:
            break
        day -= timedelta(days=1)
    raise ValueError("中信最近 8 天都沒有申購買回清單")


# 貝萊德的持股只標國家，不帶市場代碼；轉成和其他投信一致的「代號 市場」格式（美股、台股不加）
BLACKROCK_MKT = {"日本": "JP", "韓國": "KS", "中國": "CH", "香港": "HK", "英國": "LN", "法國": "FP", "德國": "GY",
                 "加拿大": "CN", "越南": "VN", "印度": "IN", "瑞士": "SW", "荷蘭": "NA", "澳洲": "AU",
                 "瑞典": "SS", "丹麥": "DC", "義大利": "IM", "西班牙": "SM", "新加坡": "SP", "巴西": "BZ"}


def _csv_rows(text: str):
    return list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))


def pcf_blackrock(code, product):
    """貝萊德投信：官網持股 CSV（id 是官網產品編號，例如 009826 → 351824）。"""
    if not product:
        raise ValueError("貝萊德需要在設定檔填官網產品編號（pcf.id）")
    text = fetch_text(f"pcf_{code}", f"https://www.blackrock.com/tw/products/{product}/fund/1480664180144.ajax"
                                      f"?fileType=csv&fileName={code}_holdings&dataType=fund")
    rows = _csv_rows(text)
    d = None
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text[:200])
    if m:
        d = date(*(int(x) for x in m.groups()))
    idx, data = table_rows([rows], "Ticker", "Name", "Asset Class", "Weight (%)", "Shares")
    if not idx:
        raise ValueError("貝萊德持股檔格式變了，找不到表頭")
    items = []
    for r in data:
        if len(r) <= max(idx.values()):
            continue
        ac, tk = r[idx["Asset Class"]], r[idx["Ticker"]].strip()
        loc = r[idx["Location"]].strip() if "Location" in idx else ""
        if ac == "Futures":
            continue  # 期貨只占極小比例且無權重，略過
        if ac in ("Cash", "FX", "Cash Collateral and Margins") or not tk or tk == "-":
            continue
        if loc not in ("美國", "台灣", ""):
            tk = f"{tk} {BLACKROCK_MKT.get(loc, loc)}"
        items.append(_item("s", tk, r[idx["Name"]], r[idx["Shares"]], r[idx["Weight (%)"]]))
    return items, d


def pcf_tsit(code, _id):
    """台新投信（含原新光投信）：申購買回清單頁面，用表單 POST 查詢。"""
    url = "https://www.tsit.com.tw/ETF/Home/Pcf"
    page = fetch_text(f"pcf_{code}_page", url)
    m = re.search(r'id="MAX_DATE"[^>]*value="([\d-]+)"', page) or re.search(r'value="([\d-]+)"[^>]*id="MAX_DATE"', page)
    day = m.group(1) if m else datetime.now(TZ).date().isoformat()
    html = fetch_text(f"pcf_{code}", url, data=f"DATA_DATE={day}&ETF_ID={code}&FUND_TYPE=ALL",
                      headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": url})
    tables = html_tables(html)
    items = []
    idx, rows = table_rows(tables, "代號", "名稱", "股數", "持股權重")
    for r in rows:
        if idx and len(r) > max(idx.values()) and r[idx["代號"]] and "合計" not in r[idx["代號"]]:
            items.append(_item("s", r[idx["代號"]], r[idx["名稱"]], r[idx["股數"]], r[idx["持股權重"]].rstrip("%")))
    idx, rows = table_rows(tables, "期貨代號", "期貨名稱", "口數", "持股權重")
    for r in rows:
        if idx and len(r) > max(idx.values()) and r[idx["期貨代號"]] and "合計" not in r[idx["期貨代號"]]:
            items.append(_item("f", r[idx["期貨代號"]], r[idx["期貨名稱"]], r[idx["口數"]], r[idx["持股權重"]].rstrip("%")))
    return items, ms_date(day)


def pcf_uobam(code, fund):
    """大華銀投信：官網 JSON（id 是基金代碼，例如 009829 → 00719554）。"""
    if not fund:
        raise ValueError("大華銀需要在設定檔填基金代碼（pcf.id）")
    j = fetch_json(f"pcf_{code}", f"https://www.uobam.com.tw/json/reply/WebSitePcfRequest?fundID={fund}&pcfDate=",
                   headers={"Referer": "https://www.uobam.com.tw/fund/etf/pcf"})
    kinds = {"stock": "s", "futures": "f", "bond": "b", "fund": "e", "etf": "e"}
    items = []
    for x in j.get("result") or []:
        k = kinds.get(str(x.get("kind", "")).lower())
        if k:
            name = x.get("cName")
            if k == "f" and x.get("dym"):
                name = f"{name} {x['dym']}"
            items.append(_item(k, x.get("code"), name, x.get("qty"), x.get("weight")))
    return items, ms_date(j.get("announce")) or ms_date(j.get("datadate"))


PCF_FETCHERS = {"yuanta": pcf_yuanta, "cathay": pcf_cathay, "capital": pcf_capital,
                "fubon": pcf_fubon, "uni": pcf_uni, "kgi": pcf_kgi, "fhtrust": pcf_fhtrust,
                "ctbc": pcf_ctbc, "blackrock": pcf_blackrock, "tsit": pcf_tsit, "uobam": pcf_uobam}


def update_holdings(etf_cfg, now) -> tuple[dict, list[str]]:
    """抓各投信申購買回清單。回傳 (holdings, 需要通知的失敗投信)。

    設定檔 pcf 標 client: true 的來源，網頁開啟時會從使用者裝置直接讀取；
    這類來源在伺服器端失敗只記錄、不算排程失敗。"""
    prev = load_json(HOLDINGS, {})
    prev_etfs = prev.get("etfs", {})
    sources, etfs, warnings, hard_fail = {}, {}, [], set()
    for c in etf_cfg:
        code, pcf = c["code"], c.get("pcf") or {}
        issuer = pcf.get("issuer")
        fn = PCF_FETCHERS.get(issuer)
        if not fn:
            continue
        name = ISSUER_NAMES.get(issuer, issuer)
        client = {"issuer": issuer, "id": pcf.get("id")} if pcf.get("client") else None
        src = sources.setdefault(issuer, {"name": f"{name} 申購買回清單（成份股）",
                                          "ok": True, "codes": [], "as_of": None,
                                          "fetched_at": now.isoformat(timespec="seconds")})
        src["codes"].append(code)
        if client:
            src["client"] = True
        try:
            items, d = fn(code, pcf.get("id"))
            items = [x for x in items if x[1] and x[4] is not None]
            if not items:
                raise ValueError("沒有解析到任何持股")
            items.sort(key=lambda x: -(x[4] or 0))
            stock_w = sum(x[4] for x in items if x[0] == "s")
            if c.get("type") in ("市值型", "高股息", "主動式") and not 80 <= stock_w <= 105:
                warnings.append(f"{code}：股票權重合計 {stock_w:.1f}% 不合理，請人工確認")
            etfs[code] = {"issuer": name, "date": iso(d), "stale": False, "items": items}
            src["as_of"] = max(filter(None, [src["as_of"], iso(d)]), default=None)
            print(f"[OK]   成份股 {code}: {len(items)} 筆，{iso(d)}")
        except Exception as e:  # noqa: BLE001
            src["ok"] = False
            src["error"] = f"{code}: {str(e)[:250]}"
            old = prev_etfs.get(code)
            etfs[code] = dict({k: v for k, v in old.items() if k != "client"}, stale=True) \
                if old and old.get("items") else {"issuer": name, "date": None, "stale": True, "items": []}
            if client:
                print(f"[INFO] 成份股 {code}: 伺服器端讀不到，改由網頁端直接讀取（{str(e)[:120]}）")
            else:
                hard_fail.add(issuer)
                print(f"[FAIL] 成份股 {code}: {e}", file=sys.stderr)
        if client:
            etfs[code]["client"] = client
    caps = prev.get("stocks") or {}
    try:
        caps = src_caps()
        sources["caps"] = {"name": "證交所／櫃買 股價與股本（市值排行）", "ok": True,
                           "as_of": now.date().isoformat(), "fetched_at": now.isoformat(timespec="seconds")}
    except Exception as e:  # noqa: BLE001
        sources["caps"] = {"name": "證交所／櫃買 股價與股本（市值排行）", "ok": False, "client": False,
                           "as_of": prev.get("sources", {}).get("caps", {}).get("as_of"), "error": str(e)[:200],
                           "fetched_at": now.isoformat(timespec="seconds")}
        print(f"[FAIL] 市值：{e}", file=sys.stderr)
    out = {"schema": 1, "generated_at": now.isoformat(timespec="seconds"),
           "fields": ["種類", "代號", "名稱", "數量", "權重%"], "sources": sources,
           "warnings": warnings, "etfs": etfs, "stocks": caps}
    HOLDINGS.parent.mkdir(parents=True, exist_ok=True)
    HOLDINGS.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return out, sorted(hard_fail)


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

    try:
        flows = update_weekly(etfs)
        for rec in etfs:
            if rec["code"] in flows:
                rec["flow"] = flows[rec["code"]]
    except Exception as e:  # noqa: BLE001
        warnings.append(f"資金流向計算失敗：{e}")

    out = {
        "schema": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "sources": sources,
        "warnings": warnings,
        "etfs": etfs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    append_history(etfs)

    failed = [k for k, s in sources.items() if not s["ok"]]
    hold, hold_failed = update_holdings(etf_cfg, now)
    for w in warnings + hold["warnings"]:
        print(f"[WARN] {w}", file=sys.stderr)
    print(f"完成：{len(etfs)} 檔，失敗來源 {failed or '無'}，成份股失敗 {hold_failed or '無'}，"
          f"警告 {len(warnings) + len(hold['warnings'])} 則")
    return 2 if failed or hold_failed else 0


WEEKLY = ROOT / "data" / "weekly.csv"


def fill_weekly_nav(rows):
    """補上週資料缺的淨值：向證交所 ETF e添富 查每日淨值（只有上市 ETF），取當天或之前最近一天。"""
    missing: dict[str, list] = {}
    for (d, code), r in rows.items():
        if not r.get("nav"):
            missing.setdefault(code, []).append(d)
    for code, dates in missing.items():
        try:
            start = date.fromisoformat(min(dates)) - timedelta(days=10)
            end = date.fromisoformat(max(dates))
            j = fetch_json(f"enav_{code}", "https://www.twse.com.tw/zh/ETFortune/ajaxEtfInfoChart",
                           data=f"id={code}&startDate={start:%Y/%m/%d}&endDate={end:%Y/%m/%d}&type=fundPric",
                           headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                    "X-Requested-With": "XMLHttpRequest",
                                    "Referer": f"https://www.twse.com.tw/zh/ETFortune/etfInfo/{code}"}, tries=2)
            ser = sorted((iso(ms_date(x.get("date"))), x.get("count")) for x in j.get("netPrice") or []
                         if x.get("count") is not None)
            for d in dates:
                prior = [v for k, v in ser if k and k <= d]
                if prior:
                    rows[(d, code)]["nav"] = prior[-1]
        except Exception as e:  # noqa: BLE001
            print(f"[INFO] {code} 週淨值補值失敗：{str(e)[:120]}")


def update_weekly(etfs):
    """集保每週資料：受益人數、單位數、當週淨值。回傳 {代號: 資金流向摘要}。

    淨申購金額 ≈（本週單位數 − 上週單位數）× 本週淨值。單位數一週變動超過 1.8 倍視為分割，該週不計。"""
    cols = ["date", "code", "holders", "units", "nav"]
    rows = {}
    if WEEKLY.exists():
        with WEEKLY.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows[(r["date"], r["code"])] = r
    navs = {}  # 從每日紀錄找當天淨值
    if HISTORY.exists():
        with HISTORY.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("nav"):
                    navs[(r["date"], r["code"])] = r["nav"]
    fill_weekly_nav(rows)
    for e in etfs:
        d, code = e.get("holders_date"), e["code"]
        if not d or not e.get("holders") or not e.get("holders_units"):
            continue
        old = rows.get((d, code), {})
        nav = old.get("nav") or navs.get((d, code)) or e.get("nav") or ""
        rows[(d, code)] = {"date": d, "code": code, "holders": int(e["holders"]),
                           "units": int(e["holders_units"]), "nav": nav}
    WEEKLY.parent.mkdir(parents=True, exist_ok=True)
    with WEEKLY.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for key in sorted(rows):
            w.writerow({k: rows[key].get(k, "") for k in cols})

    by_code: dict[str, list] = {}
    for (d, code), r in sorted(rows.items()):
        by_code.setdefault(code, []).append(r)
    out = {}
    for e in etfs:
        code = e["code"]
        ser = by_code.get(code, [])
        if len(ser) < 2:
            continue
        last_nav = e.get("nav")
        weeks = []  # [日期, 受益人增減, 淨申購(億)]
        for a, b in zip(ser, ser[1:]):
            ua, ub = float(a["units"]), float(b["units"])
            nav = float(b["nav"]) if b.get("nav") else (float(a["nav"]) if a.get("nav") else last_nav)
            split = ua > 0 and not (1 / 1.8 < ub / ua < 1.8)
            flow = None if split or not nav else round((ub - ua) * nav / 1e8, 2)
            weeks.append([b["date"], int(b["holders"]) - int(a["holders"]), flow])

        def tot(n, i):
            xs = [w[i] for w in weeks[-n:]]
            return None if len(weeks) < n or any(x is None for x in xs) else round(sum(xs), 2)

        h0 = int(ser[-1]["holders"])
        out[code] = {
            "date": ser[-1]["date"],
            "holders_w1": tot(1, 1), "holders_w4": tot(4, 1), "holders_w13": tot(13, 1),
            "holders_w4_pct": round(tot(4, 1) / (h0 - tot(4, 1)) * 100, 2) if tot(4, 1) is not None and h0 - tot(4, 1) else None,
            "flow_w1": tot(1, 2), "flow_w4": tot(4, 2), "flow_w13": tot(13, 2),
            "weeks": weeks[-13:],
        }
    return out


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
