## Context

互動模式在 `src/agora/tui.py`（Textual）。現在的動作在背景執行緒裡呼叫 `cli.main`，用 `redirect_stdout` 收輸出，所以沒辦法中斷，也會和其他執行緒的輸出混在一起（review `docs/review/cache.md` K3）。指令模式的批次、進度、續傳、雲端標記由 change `command-batch-actions` 提供。

## Goals / Non-Goals

**Goals:**
- 互動模式只呼叫指令模式，行為一致；能中斷、看得到進度。

**Non-Goals:**
- 不在互動模式裡另外實作續傳：重跑同一個指令就會接著做。
- 不做雲端與本機內容的比對（只看有沒有）。

## Decisions

- **子程序跑指令**：`[sys.executable, "-m", "agora.cli", …]`，stdout 與 stderr 合在一起逐行讀，`start_new_session=True`，Esc 時對整個 process group 送 SIGINT，讓指令啟動的 agent（例如寫要約的 opencode）一起停。替代方案「執行緒＋redirect_stdout」沒辦法安全中斷，而且會影響整個程式的 stdout。
- **進度**：從輸出裡最後一個 `k/N` 讀；沒有就顯示不確定的進度條。
- **continue、edit** 照舊用 `App.suspend()`，在本程序呼叫 `cli.main`（要把終端機交給 agent 或編輯器）。
- **測試**：`spawn` 可以替換成假的程序物件（跑假的 cli、提供 stdout），用 Textual 的 `run_test` 模擬按鍵。
- **「雲端」欄**：讀 `command-batch-actions` 在索引裡留下的標記與 outbox；不另外呼叫 Drive。

## Risks / Trade-offs

- [子程序每次都要重新載入 Python 與套件，多一兩百毫秒] → 動作本身都是秒級，可以接受。
- [中斷時 agent 的 session 可能留在寫要約的專案裡] → 那個專案的 session 本來就不列在未匯入頁（review T5）；之後補刪由既有的 pending 記錄處理。
