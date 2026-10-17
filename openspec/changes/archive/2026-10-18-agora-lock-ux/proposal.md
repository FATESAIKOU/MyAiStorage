## Why

接續鎖（`store.continuing`：本機 `pending/<ULID>.json` 上的 flock）是對的，要保留；但現在用起來很煩，而且差點造成誤刪（MyAiStorage#90）：

- `agora delete session A B C D --yes` 只要其中一個正在接續，整批都不刪（`cmd_delete` 在第一個鎖住的 id 就 `raise InputError`）。
- 訊息只說「正在接續，等它結束再刪」，看不出是誰在用。
- `agora search` 清單的日期是建立日／對話日，看起來很舊的 Session 其實正在跑，整理時差點被當成舊的刪掉（剛好被鎖擋住才沒事）。

本人 2026-10-18 決定：鎖保留，但改好用。

## What Changes

- **`delete` 遇到鎖住的就跳過、其他照刪**：被跳過的每一個在 stderr 寫明原因，最後摘要說跳過了幾個。**全部都被鎖住**時什麼都不刪、stderr 明確說明，exit 1（與現行單一個被鎖的 exit 1 相同；不新增 exit code，4／5 的語意不動）。
- **鎖住時寫明是誰在接續**：`agent`、`dir`、`started_at`（pending 記錄本來就有）。`continue` 的固定行 `agora-locked <agora id>`、exit 4、以及「等它結束再接續（要分岔請用 --fork）」的說明都保留，只在說明裡補上這些欄位；`delete` 的跳過訊息用同一套說法。不印這些以外的私人資訊。
- **`search` 清單顯示最後更新時間與「使用中」標記**：日期欄沿用對話日期（清單的排序不變），不同時補上 `（更新：YYYY-MM-DD）`（`agora.updated_at`）；本機有人拿著 `pending/<ULID>.json` 的鎖的那一行標 `(使用中)`。「使用中」只判斷本機的鎖——別台機器的接續這裡看不到，寫進文件與 spec。
- **`agora delete … --force`**：連正在接續的 Session 一起刪；刪之前逐個在 stderr 警告後果（那個 agent 之後的寫回會另存成新的 Session）。`--force` 不能取代 `--yes`；沒有 `--yes` 什麼都不刪。沒加 `--force` 時行為如第一點。
- 文件同步：`session-continue`／`batch-commands`／`session-sync` 主 spec、README 的 delete／search 段與 exit code 表、`docs/design.md` 5.1／5.6。

## Capabilities

### New Capabilities

（無）

### Modified Capabilities

- `batch-commands`：delete 跳過正在接續的、全部被鎖住的結果與 exit code、`--force` 的警告與確認。
- `session-continue`：被鎖的說明寫出誰在接續（固定行 `agora-locked <agora id>` 與 exit 4 不變）。
- `session-sync`：search 每一行的最後更新時間與「使用中」標記（只判斷本機的鎖）。

## Impact

- 程式：`src/agora/store.py`（`continuing_details`）、`src/agora/cli.py`（`_locked`、`cmd_delete`、`cmd_search`、`--force` 的旗標與警告）。`store.continuing`、`cache`、TUI 的接續檢查不動。
- 測試：`tests/unit/test_cli.py`（被鎖的 delete 測試改寫、`--force`、search 格式）、`tests/unit/test_cli_sync.py`（固定行與 exit 4 不變）、`tests/unit/test_cli_more.py`（search 輸出的既有斷言）。只碰假資料與隔離的 `HOME`／`AGORA_*`。
- 文件：README（delete、search、exit code 表）、`docs/design.md`（5.1、5.6）。
- 相關：MyAiStorage#54（`agora-locked` exit 4）、#76（agent 死掉的 exit 5）、#79（介面改動要同步主 spec）。
