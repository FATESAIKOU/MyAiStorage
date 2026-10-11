## Context

`AgoraApp.action_primary`（`src/agora/tui.py`）在 Agora 頁的接續流程：
1. `_refuse(row, "接續")`：雲端沒有的 Session 不接續；
2. `Choose("用哪個 agent 接續？", ["opencode", "claude"])`；
3. `ask_dir(row)`：工作目錄；
4. `ask_sync_period()`：同步間隔；
5. `outside(argv_for("continue", …))`：交出終端機給指令模式。

「正在接續中」現在只由指令模式擋（`store.continuing(paths, ulid)`，exit 1，訊息叫人用 `--fork`），TUI 要等問完目錄與間隔、交出終端機之後才看得到。

## Goals / Non-Goals

**Goals：** 在選 agent 的同一個視窗裡選要不要 fork，預設不 fork；正在接續中時，不勾 fork 就早一點說明並停下。

**Non-Goals：** 改指令模式；fork 的 title 自訂（照指令模式預設加 `-fork`）；未匯入頁的接續（那一頁的 Enter 是匯入）。

## Decisions

- **視窗沿用 `Confirm`**（不用改它）：它本來就是「幾個選項＋一個預設不勾的額外選項」，Tab／shift+tab 在視窗裡移動（S1）、空白勾選、Enter 選擇都已經有了。
  - `Confirm("用哪個 agent 接續？", ["opencode", "claude"], note=..., extra="fork：複製成新的 Session 再接續", note_on=...)`；
  - `note`（沒勾時）：「接續原本這個 Session」；`note_on`（勾了）：「會多一個 <原 title>-fork 的新 Session，原本的不動」；
  - 回傳 `(index, fork)`。**注意：Esc 回傳的是 `(None, False)`**（binding 是 `dismiss((None, False))`，docstring 寫的「None」不準），所以要用 `answer is None or answer[0] is None` 判斷取消。
- `argv_for("continue", rows, agent, workdir, sync_period, fork=False)`：`fork` 為真時在最後加 `--fork`。其他動作不受影響。
- **正在接續中的提早檢查**：選完 agent、沒勾 fork 時，用和指令模式同一個判斷（`store.continuing(self.paths, <ulid>)`，ulid 的取法照 `cli._ulid_of`）；是的話 `Tell("不能接續", "<id> 正在接續中，等它結束再接續；要分岔請在選 agent 的視窗勾 fork", ok=False)`，然後停下。指令模式的檢查照舊保留（兩台機器的情況、競態都還是它擋）。
- 雲端沒有的照舊在一開始由 `_refuse` 擋，不開視窗（fork 也不例外，和 spec「雲端欄」一致）。
- 按鍵列不變：Enter 仍是「接續」。

## Risks / Trade-offs

- [TUI 與指令模式各判斷一次「正在接續中」] → TUI 只是提早說明；真正擋的還是指令模式，兩者用同一個函式，不會說法不一。
- [`Confirm` 的額外選項原本用在「預設什麼都不做」的危險選項] → fork 也是「預設不做、要明確選」的決定，語意相符。
