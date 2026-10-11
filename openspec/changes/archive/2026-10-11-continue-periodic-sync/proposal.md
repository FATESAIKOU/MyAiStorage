## Why

`agora continue` 要等 agent 結束才把對話寫回 Session（`_finish`）。agent 跑很久時，Drive 上的版本一直停在開始的那一刻：別台機器或互動模式看不到進度；機器當掉或被強制中斷時，要等下一個指令補存（`recover_pending`）才救得回來（issue #28）。

## What Changes

- `agora continue` 啟動 agent 之後，agent 執行期間**每隔 `--sync-period` 分鐘（沒給就是 5；0 ＝ 不自動同步）**把最新的對話寫回這個 Session，並走 local-first 的上傳（本機先存、背景上傳到 Google Drive）。
- 互動模式接續時，多一個輸入框確認同步間隔（預設 5）。
- 這 5 分鐘內沒有新內容就不寫。
- agent 結束時照現在的流程做最後一次寫回；和最後一次定期寫回相同就不再寫。
- 定期寫回期間 agora 不在終端機印任何東西（終端機是 agent 的），訊息記到 log 檔。
- 適用 `--fork` 出來的 Session（#29）。
- 「寫回目標」由一個解析函式決定，定期、最後一次、補存共用：途中被刪（不論前景或背景上傳發現）時，同一段工作只另存一次。
- 同一台機器上，同一個 Session 不能同時接續兩次（現在會互相蓋掉 pending，是既有的 bug）。

## Capabilities

### New Capabilities

（無；`session-continue` 由 change `continue-fork` 新增，這張在它之後歸檔）

### Modified Capabilities

- `session-continue`：新增「接續期間定期寫回」「同一台機器上不能同時接續同一個 Session」。
- `interactive-mode`：新增「接續前確認同步間隔」。

## Impact

- 程式：`src/agora/tui.py`（接續前的同步間隔輸入框）；`src/agora/cli.py`（`--sync-period`、`cmd_continue` 的等待迴圈、寫回目標的解析、可重複呼叫的寫回、pending 的改名）；`src/agora/store.py`（`stage` 先寫好 `.update`、fsync、`Drive._run` 的 stdin）；`src/agora/agents/opencode.py`（`opencode export` 的 stdin）；`tests/fakes/`（scripted 模式）。依賴 change `continue-fork` 的 `store.never_confirmed`。
- 測試：單元測試用假 agent 與很短的間隔；整合測試補一個（只用 `agora-test`）。
- 文件：`docs/design.md` 5.4、README 的 continue。
