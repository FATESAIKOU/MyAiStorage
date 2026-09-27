# 健康檢查 runbook（6.3）

## 執行

- Mac 上每 6 小時一次（launchd；PM 決定 8）。
  安裝腳本 `scripts/install-health-launchd.sh` 已寫好，
  **沒有使用者同意不得執行（目前未啟用）**。
- 出現 `fail` 時 macOS 通知＋非 0 exit code。
- 手動執行：`python -m aistorage.admin health --data-json <檔>`；
  只印 plist：`python -m aistorage.admin health --plist`。

## 檢查項目與閾值（改數字即是改政策，見 `health.py` 頂部）

- 憑證可刷新（committer／worker 任一失敗 → fail）。
- workflow 被停用 → fail（有維護旗標則屬正常）。
- 被取消的 run → warn（對照 pin 的 `promoted_at` 交叉檢查；住民可刪 run）。
- 連續中止 ≥3 輪 → fail。
- 距上次成功提交：>12h warn、>30h fail（附「提交流程可能被停用或遭到注入」）。
- 兩週 Actions 分鐘數 >300 → warn（連續兩週就調提交間隔）。
- 隔離資料夾增長中 → warn。
- 家庭共用配額：使用率 >85% warn、>95% fail。
- 讀取視圖：索引 >50MB 或檔案 >5,000 → warn（D5）。
- manifest 落後 pin 的 main → warn（等下一個非空輪次補發）。
- 同步器等待／拒收 → warn。
- opencode 升級時重跑 `resident/verify-prune.sh`（prune 只加標記）。

## 資料來源

優先使用住民無法偽造的：pin 的 `promoted_at`（上次成功提交）、
Drive 的隔離區與配額、GitHub runs／billing API。
`--data-json` 的組裝方式見 `health.py` 的 `collect` 註記（整合後補）。
