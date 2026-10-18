## Why

M0（MyAiStorage#93，報告 `reports/M0-report.md`）量到：閒置的 `wait` 對每個註冊 channel 的第一次查詢是完整請求（計額度），之後帶 ETag 的輪詢回 304（量到不計額度）；真正的閒置成本是「每次重新開始 `wait` 時的那一輪完整查詢」。照期 2 規模（12 個 agent、6 個 channel）推估，`wait --timeout 100` 約吃主額度的 52%、`--timeout 600` 約 8.6%；若 `wait` 之間記得 ETag，推估可壓到接近 0（M0 §4，未實作）。

現在 ETag 只存在單次 `wait` 行程的記憶體（`src/angareion/github.py` 的 `_check_etags`），行程一結束就沒了。MyAiStorage#96（本人 2026-10-18 選「記住上次查到哪」）要求把它存進狀態檔，讓重新開始的 `wait` 對每個 channel 也只發條件式查詢；對 AI 來說用法不變。

同一個 M0 回合還暴露兩個小問題：`read`／`wait` 加了 `--channel` 會讀到寄給別人的訊息（M0 §3.1：審查員讀到寄給 impl 的指示並照做），要加警告；以及 `wait` 的預設逾時要不要改（M0 建議 harness 用 `--timeout 600`）。

## What Changes

- `wait` 之間把每個實體上一次「完整查過而且沒有新訊息」的條件式查詢狀態——完整的查詢 URL（含 `since`）、當時的游標、回應的 ETag——存進新的本機狀態檔 `wait-<agent 名>.json`（與游標檔同一個狀態目錄、分成兩個檔）。重新開始 `wait` 時，查詢 URL、游標、收件模式（整個 channel／寄給我的）三者完全相同才帶 `If-None-Match`；只有 304 才算「沒有新訊息」。
- 安全邊界比現行規則再加嚴：回應有下一頁、第 1 頁已滿（`per_page` 筆）、或這次查到新訊息時不留 ETag；狀態檔不存在／壞掉／版本不認得／記錄對不上時一律完整查詢（不掉訊息）。`wait` 仍然不動游標；`read` 不讀這個檔、仍不受 304 影響。
- `read`／`wait` 帶 `--channel` 時在 stderr 印一行警告（會包含寄給別人的訊息；不給 `--channel` 才是「寄給我的」）；exit code 與 stdout 不變。
- 重新評估 `wait` 的預設逾時：**結論是不改**（不給 `--timeout` 就一直等）。ETag 記住之後閒置成本已接近 0，綁住等待時間的額度理由消失；限時是 harness 的事，`SKILL.md` 繼續建議 `--timeout 600`，補一句「重新開始 wait 很便宜」。
- 驗收（PM 對真 GitHub 做）：照 M0 的量法（`sitecustomize` 請求計數器）連續重新啟動 `wait --timeout 60`，計額度的 200 要接近 0（只有第一次建立 ETag 與真正有新訊息時）。
- 文件同步：`docs/angareion.md` 的目標／游標／額度段、`skills/angareion/SKILL.md` 的 wait 說明、主 spec。

## Capabilities

### New Capabilities

（無）

### Modified Capabilities

- `angareion`：`確認有沒有新訊息`——`wait` 之間保留條件式查詢狀態（跨行程的狀態檔、可重用的條件、304 的安全邊界、壞檔退回完整查詢、預設逾時維持一直等）；`目標與游標`——`--channel` 的收訊警告（stderr、exit code 與 stdout 不變），以及 ETag 狀態不進游標檔、不影響 `read`。

## Impact

- 程式：`src/angareion/github.py`（`check` 的 ETag 記錄改成可注入的 cache：精確比對 URL／游標、找到新訊息或多頁／整頁滿時清掉、只記完整查過且沒有新訊息的那一次）、`src/angareion/backend.py`（`ETagRecord`、`ETagCache` 介面、記憶體版；`ChannelBackend` 的簽名不變）、`src/angareion/state.py`（`wait-<agent 名>.json` 的讀寫：版本、原子替換、壞檔當空）、`src/angareion/cli.py`（把檔案版 cache 接給 GitHub 後端；`read`／`wait` 的 `--channel` 警告）。
- 測試：新增 `tests/unit/test_angareion_etag.py`（假 transport 的 ETag／304 行為、重啟、失效、壞檔、多 channel、換 identity／狀態目錄、多頁／整頁、read 後游標）；`tests/unit/test_angareion_cli.py`（`--channel` 警告）；`tests/unit/test_angareion_state.py`（游標檔仍只有 position/at/seen）。
- 文件：`docs/angareion.md`（目標、`wait` 的等法、游標、額度段）、`skills/angareion/SKILL.md`（wait 與收訊規則）、主 spec（delta 套用時機由 PM 決定；`angareion-v5` 尚未歸檔、同兩個 requirement 也被它改過）。
- 流程：實測驗收由 PM 對真 GitHub 做（本 change 不改 `experiments/m0/`、不碰真後端）；commit 由 PM 做。
