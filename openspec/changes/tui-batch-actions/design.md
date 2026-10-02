## Context

互動模式在 `src/agora/tui.py`（Textual）。現在的動作在背景執行緒裡呼叫 `cli.main`，用 `redirect_stdout` 收輸出，所以沒辦法中斷，也會和其他執行緒的輸出混在一起（review `docs/review/cache.md` K3）。指令模式的批次、進度、續傳、雲端標記由 change `command-batch-actions` 提供。

## Goals / Non-Goals

**Goals:**
- 互動模式只呼叫指令模式，行為一致；能中斷、看得到進度。

**Non-Goals:**
- 不在互動模式裡另外實作續傳：重跑同一個指令就會接著做。
- 不做雲端與本機內容的比對（只看有沒有）。

## Decisions

- **子程序跑指令**：`[sys.executable, "-m", "agora.cli", …]`，`stdin=DEVNULL`（V1），stdout 與 stderr 合在一起逐行讀，`start_new_session=True`，Esc 時對整個 process group 送 SIGINT，沒停再升級成 SIGTERM、SIGKILL（V2），讓指令啟動的 agent（例如寫要約的 opencode）一起停。替代方案「執行緒＋redirect_stdout」沒辦法安全中斷，而且會影響整個程式的 stdout。
- **進度**：從輸出裡最後一個 `k/N` 讀；沒有就顯示不確定的進度條。
- **continue、edit** 照舊用 `App.suspend()`，在本程序呼叫 `cli.main`（要把終端機交給 agent 或編輯器）。
- **測試**：`spawn` 可以替換成假的程序物件（跑假的 cli、提供 stdout），用 Textual 的 `run_test` 模擬按鍵。
- **「雲端」欄**：讀 `command-batch-actions` 在索引裡留下的標記與 outbox；不另外呼叫 Drive。

## Risks / Trade-offs

- [子程序每次都要重新載入 Python 與套件，多一兩百毫秒] → 動作本身都是秒級，可以接受。
- [中斷時寫要約的 opencode session 留下來] → opencode 的 summarize 現在要等 `run` 結束才寫 `pending-<id>`，中途被中斷就沒有記錄（review V5、v6 Z2）。改成從 `--format json` 的事件串流一讀到 session id 就先寫記錄（tasks 1.4）；在那之前留下的，只會在寫要約專用的專案裡，未匯入頁本來就不列。
- [delete 在「Drive 已刪」與「寫墓碑」之間被中斷] → 依賴 command-batch-actions 修好 review S1-4（Drive 上已不存在的視為已刪）；那之前 delete 的「重跑會接著做」不保證（V3）。
- [未匯入頁同時勾了 opencode 與 claude 的列] → 依 agent 分成兩個指令依序跑，等待視窗標示「第 i／2 段」（V6）。
