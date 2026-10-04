## Why

互動模式裡用中文輸入法打字時，選好字一按 Enter，字就消失（issue #23）。

原因是 Textual 8.2.7 起，啟動時會對終端機開啟 kitty keyboard protocol。開啟之後，輸入法選字用的 Enter 被當成一個獨立的按鍵事件送進 app，蓋掉輸入法剛送出的字。

本人 10-04 在 herdr 的 pane 裡試過兩支最小的 Textual 程式：
- 預設的那支：選字後按 Enter，字消失；
- 設了 `TEXTUAL_DISABLE_KITTY_KEY=1`（Textual 內建的開關）的那支：正常。

## What Changes

- 互動模式啟動、載入 Textual 之前，預設設定 `TEXTUAL_DISABLE_KITTY_KEY=1`。使用者自己的環境變數已經有設定（不論值是什麼）時，照他的設定。
- 不改任何按鍵。`Tab`、`shift+tab`、`[`、`]`、`ctrl+t`、`ctrl+q` 在不用 kitty 協定時都送得出來；change `preview-search-keys` 的按鍵設計不受影響。

## Capabilities

### New Capabilities

（無）

### Modified Capabilities

- `interactive-mode`：新增「輸入法」——篩選框與搜尋框可以用中文輸入法打字選字。

## Impact

- 程式：`src/agora/cli.py`，在 `from agora import tui` 之前設定環境變數（`tui.py` 一 import 就載入 Textual，Textual 在 import 時讀這個設定）。
- 測試：
  - 單元測試：用子程序，在乾淨的環境裡 import 互動模式之後，`textual.constants.DISABLE_KITTY_KEY` 為真；使用者自己設成 `0` 時為假。
  - 人工：在 herdr 的 pane 裡用中文輸入法在篩選框選字，無法自動測。
- 文件：README 加一句。
