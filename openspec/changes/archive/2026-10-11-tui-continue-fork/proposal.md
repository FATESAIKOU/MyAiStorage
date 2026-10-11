## Why

指令模式有 `agora continue session <id> --agent … --fork`（#29），互動模式沒有對應的操作：在 Agora 頁按 Enter 只能接續原本那個 Session，想分岔出一條新的，就得離開 TUI 改打指令。原本那個 Session 正在接續中時，TUI 的接續會被擋下，而這正是指令模式允許 `--fork` 的情況（issue #36）。

本人 10-11 指定：**不要自動 fork**，要在接續的視窗裡可以指定要不要 fork。

## What Changes

- Agora 頁按 Enter 接續時，選 agent 的視窗（「用哪個 agent 接續？」）多一個**預設不勾**的「fork」選項。
  - 不勾：照現在的接續。
  - 勾了：等同指令模式的 `continue --fork`（先複製成新的 Session，title 加 `-fork`，再接續；原本的不動）。之後的工作目錄、同步間隔照常問。
- 原本那個 Session 正在接續中時：不勾 fork 就在問工作目錄之前說明它正在接續中、可以勾 fork，然後停下，不開 agent；勾了照常接續。
- 按鍵不變（Enter 仍是接續）。文件與 acceptance 跟著更新。

## Capabilities

### New Capabilities

（無）

### Modified Capabilities

- `interactive-mode`：新增「接續時選擇要不要 fork」。

## Impact

- 程式：`src/agora/tui.py`（`action_primary` 的接續流程、`argv_for("continue", …)` 加上 `--fork`）。指令模式不變。
- 測試：`tests/unit/test_tui.py`（`run_test` 按鍵驅動）。
- 文件：`docs/design.md` 5.9（互動模式）、README 的互動模式、`docs/acceptance.md` 互動模式那一段。
- 相關：#29（`continue --fork`）、spec `session-continue`。
