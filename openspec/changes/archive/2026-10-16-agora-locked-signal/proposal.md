## Why

sidecar（MyLinuxPool#114）用 `agora continue --headless` 接續 Session。那個 Session 正被別的行程接續著時（有人手動跑了 `agora continue`，或兩個 sidecar id 同時接續同一個 Session），agora 現在回的是一般的輸入錯誤（exit 1，「正在接續中」），sidecar 分不出這是「被鎖」，只能回 500，而不是 spec 規定的 409 `session_locked`（MyAiStorage#54，來源：MyLinuxPool sidecar v5 審查）。

工單寫「專用的 exit code 3，目前沒有被用掉」，但 3 已經是 `EXIT_IN_OUTBOX`（還在 outbox）。PM 決定改用 **4**（2026-10-13）。

## What Changes

- `agora continue`（不含 `--fork`）發現同一個 Session 正在接續中時：以專用的 `EXIT_LOCKED = 4` 結束，並在中文說明之前先印一行固定格式 `agora-locked <agora id>`（呼叫方靠它回報 409）。其他 exit 1／2／3 的用法不變。
- 兩條路徑都涵蓋：
  - 開頭的提早檢查（`store.continuing`）；
  - 拿 `pending/<ULID>.json` 的鎖時的競合窗口（兩個行程幾乎同時接續）。拿 `*.json.tmp` 的鎖當同時偵測（開啟時不 truncate），輸的那個不覆蓋贏的 pending 記錄、不啟動 agent，給同一個訊號。
- `--fork` 不受影響（它寫的是新的 Session）。
- README 的 exit code 表、`docs/design.md` 5.4 與 `session-continue` spec 跟著更新。

## Capabilities

### New Capabilities

（無）

### Modified Capabilities

- `session-continue`：同一台機器上不能同時接續同一個 Session——被鎖的訊號從 exit 1 改成 exit 4 加固定一行。

## Impact

- 程式：`src/agora/cli.py`（`EXIT_LOCKED`、`LockedError`、`main` 的出口、`cmd_continue` 的提早檢查、`_write_pending` 的鎖）。
- 測試：`tests/unit/test_cli_sync.py`（提早檢查、另一個行程持有的真鎖、兩條競合窗口、`--fork`、其他輸入錯誤仍是 1）。
- 文件：README（exit code 表）、`docs/design.md`（5.4）。
- 相關：MyLinuxPool#114、#51。
