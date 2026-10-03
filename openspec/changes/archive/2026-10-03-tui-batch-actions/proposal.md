## Why

使用者試用互動模式後要：merge、delete、import、pull、push 都能一次選多個、能全選、看得到進度、執行很久時能中斷，並且畫面上看得出哪些 Session 雲端已經沒有了。指令模式的對應已在 change `command-batch-actions` 做好；這個 change 讓互動模式用上它們。原本的需求單是 `docs/tickets/T2-tui-batch.md`。

## What Changes

- 所有不需要整個終端機的動作（import、merge、delete、pull、push）改成在子程序裡跑同一個 `agora` 指令，等待視窗有進度條（讀 `k/N`），按 Esc 中斷。
- 動作作用在勾選的列；沒有勾選時，delete、import、pull、push 作用在游標那一列；continue、edit 一次一個。
- `a` 切換全選與全不選（只算目前篩選後看得到的列）。
- Agora 頁加「雲端」欄；pull、push 的確認視窗可以勾選「雲端沒有的就刪掉本機的」與「雲端沒有的就傳回去」。
- 按鍵整理：拿掉 `r`、`s`，改成 `p`（pull）、`P`（push）。

## Capabilities

### New Capabilities
- `interactive-mode`: 互動模式的多選、全選、進度、中斷、雲端欄與按鍵

### Modified Capabilities

（`openspec/specs/` 歸檔 `command-batch-actions` 之後會有 `session-sync`、`batch-commands`；這個 change 只呼叫它們，不改它們的要求。）

## Impact

- 程式：`src/agora/tui.py`。
- 文件：`docs/design.md` 5.9。
- 測試：`tests/unit/test_tui.py`（Textual `run_test`）。
- 依賴 change `command-batch-actions` 的 pull／push、批次指令與「雲端沒有」的標記。
