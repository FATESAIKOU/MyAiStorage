# 健康檢查 runbook（6.3）

## 執行

- Mac 上每 6 小時一次（launchd；PM 決定 8）。
  安裝腳本 `scripts/install-health-launchd.sh` 已寫好，
  **沒有使用者同意不得執行（目前未啟用）**。
- plist 的指令是 `python -m aistorage.admin health`（不帶 `--data-json`）：
  它會自己 collect 資料再判定，退出碼 0＝沒有 fail，1＝有 fail。
- 出現 `fail` 時 macOS 通知（`--notify`）。
- 手動執行：`python -m aistorage.admin health --config config/committer.json`；
  除錯時可加 `--dump-data /tmp/h.json` 存下 collect 結果，再用
  `--data-json /tmp/h.json` 重跑判定；只印 plist：`health --plist`。

## 判定原則

**查不到（None）一律是 `warn`，不是 `ok`**（review M7）。沒有憑證、讀不到 pin、
拿不到 Actions 用量都屬於「沒在監控」，不能當成「一切正常」。

## 檢查項目與閾值（改數字即是改政策，見 `health.py` 頂部）

- 憑證可刷新（committer／worker 任一失敗 → fail；查不到 → warn）。
- 維護旗標存在 → warn（正常會在管理操作結束後解除）。
- workflow 被停用 → fail（有維護旗標則屬正常；查不到 → warn）。
- 被取消的 run → warn（對照 pin 的 `promoted_at` 交叉檢查；住民可刪 run）。
- 連續中止 ≥3 輪 → fail。
- 距上次成功提交（pin 的 `promoted_at`）：>12h warn、>30h fail。
- 兩週 Actions 分鐘數 >300 → warn（連續兩週就調提交間隔）。
- 隔離區增長中 → warn。
- 家庭共用配額：使用率 >85% warn、>95% fail（配額由 `--config` 的 conf 抓，
  抓不到 → warn）。
- 讀取視圖：索引 >50MB 或檔案 >5,000 → warn（D5）。
- manifest 落後 pin 的 main → warn（等下一個非空輪次補發）。
- **前綴裡有名同但內容不等於正式釘選值的主 manifest → warn；而且已經存在超過一輪
  → fail**（`manifest_conflict`，review-5d4dd52 M2）。這一項是「持續注入」的警示：
  住民在每一輪的第 4 步（sweep 列舉）之後放一份位元組不同的同名 manifest，
  `verify_clone`／`precheck` 就每一輪中止。`git clone` 是用**檔名**找檔案的，
  git-remote-annex 與 rclone 都沒辦法用 file id 做 clone，所以釘選值（連同
  `expected_manifest_sha256`）**救不了這條**——它屬於 ADR 0008 已接受的殘餘風險
  (1)：偵測得到、不會遺失或竄改內容，但提交會持續暫停。**處置只有一條路：先停掉
  注入**（撤銷那個 profile 對真本前綴的寫入：收件匣權限或簽章金鑰），**再**手動
  刪掉前綴裡那份；順序反了下一輪又會長出來。完整步驟見 `docs/runbooks/recovery.md`
  的「模式 4」。
- 同步器等待／拒收 → warn。
- opencode 升級時重跑 `resident/verify-prune.sh`（prune 只加標記）。

## 資料來源（`collect_health`）

刻意挑住民無法偽造的：pin 的 `promoted_at` 與 main ref、Drive 的隔離區與
讀取視圖大小、GitHub 的 workflow 狀態／runs／用量。每一項取不到都留 `None`。
