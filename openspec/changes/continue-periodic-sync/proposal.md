## Why

`agora continue` 要等 agent 結束才把對話寫回 Session（`_finish`）。agent 跑很久時，Drive 上的版本一直停在開始的那一刻：別台機器或互動模式看不到進度；機器當掉或被強制中斷時，要等下一個指令補存（`recover_pending`）才救得回來（issue #28）。

## What Changes

- `agora continue` 啟動 agent 之後，agent 執行期間**每 5 分鐘**把最新的對話寫回這個 Session，並走 local-first 的上傳（本機先存、背景上傳到 Google Drive）。
- 這 5 分鐘內沒有新內容就不寫。
- agent 結束時照現在的流程做最後一次寫回；和最後一次定期寫回相同就不再寫。
- 定期寫回期間 agora 不在終端機印任何東西（終端機是 agent 的），訊息記到 log 檔。
- 適用 `--fork` 出來的 Session（#29）。

## Capabilities

### New Capabilities

（無；`session-continue` 由 change `continue-fork` 新增，這張在它之後歸檔）

### Modified Capabilities

- `session-continue`：新增「接續期間定期寫回」。

## Impact

- 程式：`src/agora/cli.py`（`cmd_continue` 的等待迴圈、抽出可重複呼叫的寫回、pending 記錄的更新）；可能動到 `src/agora/agents/claude.py`（警告改成可以靜音）。
- 測試：單元測試用假 agent 與很短的間隔；整合測試補一個（只用 `agora-test`）。
- 文件：`docs/design.md` 5.4、README 的 continue。
