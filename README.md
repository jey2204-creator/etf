# ETF 觀察表（PWA）

手機優先的台股 ETF 觀察網頁。目前為**示意數據**。

## 檔案
- `index.html`：網頁本體
- `manifest.webmanifest`：加入主畫面的名稱、圖示、顏色
- `sw.js`：離線快取（更新網頁時把 `CACHE` 版本號 +1）
- `icons/`：主畫面圖示

## 部署
GitHub → 新建公開 repo `etf` → 上傳全部檔案 → Settings → Pages → Branch 選 `main` / `(root)` → Save。
網址：`https://<你的帳號>.github.io/etf/`
