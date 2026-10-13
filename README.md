# ETF 觀察表（PWA）

手機優先的台股 ETF 觀察網頁：市價、折溢價、資產規模、受益人數、管理費、殖利率、成份股。
網址：https://jey2204-creator.github.io/etf/

## 資料怎麼更新
GitHub Actions（`.github/workflows/update.yml`）每個交易日台灣時間 14:23、17:43 執行
`scripts/update.py`，抓官方公開資料後寫入 `data/etf.json`，並把當日快照附加到 `data/history.csv`。
也可以在 GitHub 的 Actions → 更新 ETF 資料 → Run workflow 手動執行。

| 欄位 | 來源 | 頻率 |
|---|---|---|
| 市價、預估淨值、已發行單位數 | 證交所 基本市況報導 MIS | 每日 |
| 折溢價、資產規模 | 由上列資料計算（規模 = 單位數 × 淨值） | 每日 |
| 成交量 | 證交所 每日收盤行情（上市）、櫃買中心 OpenAPI（上櫃） | 每日 |
| 受益人數 | 集保結算所 集保戶股權分散表 | 每週 |
| 近一年配息、殖利率、下次除息 | 證交所／櫃買 除權除息計算結果表與預告表 | 每日 |
| 管理費 | `config/etfs.json` 手動維護，依目前規模換算級距 | 費率異動時更新 |
| 成份股（股數、權重、期貨、債券） | 各投信每日申購買回清單：元大、群益、富邦、統一、凱基（GitHub 每日抓）；國泰（網頁開啟時由裝置直接讀取） | 每日 |

某個來源失敗時，程式沿用上一次成功的資料，網頁上方會顯示警告，該次 Actions 也會標成失敗（GitHub 會寄信通知）。

國泰投信的網站防火牆會拒絕 GitHub 的美國主機（HTTP 403 Access Denied），但它的 API 允許其他網站跨網域讀取，
所以 00878 的成份股改由網頁開啟時從使用者的裝置直接向國泰讀取（設定檔 `pcf.client: true`），伺服器端讀不到不算失敗。
在台灣以外的網路、或國泰網站維護時，網頁會顯示「暫時讀不到」。

成份股輸出在 `data/holdings.json`，每筆為「種類、代號、名稱、數量、權重%」，種類 s=股票（股數）、f=期貨（口數）、b=債券（面額）、e=ETF。

## 新增或修改追蹤的 ETF
編輯 `config/etfs.json`：代號、名稱、類型、市場（TWSE 上市／TPEX 上櫃）、每年配息次數、費率級距，以及 `pcf`（成份股來源投信與該投信的基金代碼）。目前支援的投信：yuanta、cathay、capital、fubon、uni、kgi；`client: true` 目前只支援 cathay。

## 檔案
- `index.html`：網頁本體；`sw.js`：離線快取（更新網頁時把 `CACHE` 版本號 +1）
- `manifest.webmanifest`、`icons/`：加入主畫面的名稱與圖示
- `scripts/update.py`：資料更新程式（只用 Python 標準函式庫）
- `config/etfs.json`：手動維護的欄位
- `data/`：自動產生的資料，不要手動修改
