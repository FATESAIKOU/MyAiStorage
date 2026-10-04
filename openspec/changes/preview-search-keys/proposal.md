## Why

使用者 10-04 用新版（b174024）時提出兩件事（issue #22）：

- 預覽整份 Session 時沒辦法在裡面找東西；
- 清單（左）和預覽（右）共用同一套按鍵，焦點在預覽區時很多鍵沒有意義。

他要的是：預覽區像 `less` 一樣能搜尋、有游標，而且兩邊的按鍵分成兩套，只有切焦點和離開兩邊共用。

## What Changes

- **預覽區有游標**：預覽區改成唯讀的文字區，顯示原始的 Markdown 並加上語法上色（使用者選的，取代現在排過版的 Markdown）。上色只到區塊層級（標題、清單、引用、code fence），沒有粗體、斜體、行內 code；`## user`／`## assistant` 沿用原本的不同顏色。
  - 游標那一行整行高亮；
  - `j`／`k`、`↑`／`↓` 移一行，`g`／`G` 到最前／最後。
- **預覽區搜尋**：
  - `/` 輸入要找的字，`n`／`N` 跳到下一個／上一個符合；
  - 搜尋範圍是**整份 Session**：還沒載入的前面部分也算，跳到那裡時再載入那一段；
  - 符合的字標亮，顯示「第 k 個／共 N 個」；
  - `Esc` 先關搜尋框、再清標亮。
- **BREAKING** 按鍵分成兩套：
  - `Tab`（以及 `shift+tab`）切換焦點，`q` 離開，這兩個兩邊共用；
  - 換頁從 `Tab` 改成 `[`、`]`，只在清單有效；
  - 其他鍵只在焦點所在的那一邊有效；
  - 按鍵列跟著焦點換。

## Capabilities

### New Capabilities

（無）

### Modified Capabilities

- `interactive-mode`：
  - 修改「按鍵」：切焦點改用 `Tab`、換頁改用 `[`／`]`，兩邊分開；
  - 新增「預覽區的游標與捲動」「預覽區搜尋」。

## Impact

- 程式：`src/agora/tui.py`。
  - 預覽區從 `VerticalScroll`＋`Static(Markdown)` 改成唯讀的 `TextArea`；
  - 沿用 T6 的分段讀取（`Preview.step`、`read_tail`）；
  - 新增在檔案上搜尋（不經過已載入的文字）。
- 測試：`tests/unit/test_tui.py`（用 `run_test` 按鍵驅動）。
- 文件：`docs/design.md` 5.9、`docs/acceptance.md` 互動模式那一段、README 的按鍵表。
- 相關：issue #23（中文輸入法選字）會影響預覽區搜尋框打中文。那張另外處理，這個 change 不依賴它。
