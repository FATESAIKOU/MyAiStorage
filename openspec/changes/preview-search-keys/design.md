## Context

互動模式在 `src/agora/tui.py`（Textual 8.2.8）。

現在的預覽區：
- 是 `PreviewArea(VerticalScroll)`，裡面放 `Static(Markdown(...))`；
- T6 讓它只讀、只排檔案的最後一段（`Preview.step`、`read_tail`），捲到頂再補前一段；
- 游標移動有 150 ms 的防抖。

現在的按鍵：
- `AgoraApp.BINDINGS` 一套給全部，`check_action` 用 `isinstance(self.focused, VerticalScroll)` 判斷焦點在不在預覽區；
- 按鍵列是自己的 `paint_keys()` 加上固定的 `KEYS[tab]`，不是 Footer；
- `Tab` 換頁、`shift+tab` 切焦點；確認視窗的 Tab 由 `action_next_tab` 轉交（review S1）。

這份 design 已經依 review `docs/review/preview-search-keys.md` 的 W1～W13 改過。

## Goals / Non-Goals

**Goals：**
- 預覽區有游標與整行高亮，可以搜尋整份 Session；
- 左右兩套按鍵，只有 `Tab`／`shift+tab` 與 `q` 共用；
- 平常只讀、只排尾端一段（T6）。例外只有使用者明確要求的兩種：`g`，以及跳到還沒載入的搜尋符合。

**Non-Goals：**
- 修中文輸入法選字（issue #23，change `ime-kitty-keyboard`）；
- 預覽區的編輯；
- 跨 Session 搜尋（清單的 `/` 篩選照舊）。

## Decisions

### 先有 `side()`，按鍵表依「分頁 × 哪一邊」查（W8）
- `side()` 依焦點所在 widget 的 id 判斷：
  - `#table`、`#filter` 是 `"list"`；
  - `#right`（預覽區，第 2 節換成 TextArea 時保留這個 id）與預覽區的搜尋框是 `"preview"`；
  - 有視窗開著時另外處理。
- `check_action` 改用 `side()`，不再用 `isinstance`。
- `KEYS` 拆成清單、預覽兩張表，`paint_keys()` 依（分頁, 邊）查表。第 1 節先把預覽那張放上 `Tab`、`q`，第 2、3 節只在表裡加列。
- 在 `on_descendant_focus` 重畫按鍵列，滑鼠點、打開輸入框都算。

### 按鍵與視窗（W4）
- App 層 BINDINGS 只留 `tab`、`shift+tab`（priority）與 `q`（非 priority）。
- `tab`、`shift+tab` 各自一個 action：
  - 有 `Confirm` 時，分別轉成 `focus_next`／`focus_previous`（照 S1）；
  - 有其他視窗時什麼都不做（不用 `App.query_one`，它查的是底下的畫面）；
  - 沒有視窗時切換兩邊：從篩選框切出去，篩選框留著；從搜尋框切出去，關掉搜尋框，標亮留著。
- `[`、`]`、ctrl+t 以及其他清單的鍵，`check_action` 只在 `side() == "list"` 時放行。
- `AgoraApp.on_key` 的 Esc 只在焦點是篩選框時處理；預覽區的 Esc 放在 `PreviewText`。

### 預覽區改用唯讀的 TextArea
- 用 `PreviewText(TextArea)`，設定如下：
  - `id="right"`、`read_only=True`、`soft_wrap=True`、`show_line_numbers=False`；
  - 焦點進來時打開 `highlight_cursor_line`，離開時關掉（W13）。
- TextArea 自帶的鍵保留：PgUp／PgDn、←／→、Home／End、選取、複製（W7）。
- `up`、PgUp 在第 0 行時要觸發載入前一段：覆寫 `action_cursor_up`、`action_cursor_page_up`。捲到頂沿用 `watch_scroll_y`。
- 上色：
  - `language="markdown"`，依賴改成 `textual[syntax]`，`uv.lock` 跟著變；
  - 沒有 tree-sitter 時 Textual 會退回純文字；有 tree-sitter 但沒有 markdown 的語法時會丟 `LanguageDoesNotExist`，要 try/except 退回 `language=None`（W9）；
  - tree-sitter 的 markdown 只上區塊層級的色（標題、清單、引用、code fence）。
- `## user`、`## assistant` 的不同顏色，以及搜尋標亮，都在覆寫的 `get_line()` 上加樣式（W6，官方的接點）。
  - 符合改變時要清 `_line_cache`（私有屬性，附註解）再 `refresh()`；
  - 游標行的底色會蓋掉符合的底色，所以符合用前景色加粗體或底線。
- pinned 行（dir、tags）與「↑ 往上捲…」提示維持在 TextArea 之外的 `Static`，提示改成「↑ 往上捲、按 k 或 g 載入更早的內容（還有約 N KB）」（W13）。

### 分段載入接在 TextArea 上（W2、W3、W10）
- 補一段：
  1. 先記下 `scroll_y` 與 `wrapped_document.height`；
  2. 在 `(0, 0)` 插入 `f"{step}\n"`（結尾要有換行，游標的欄位才不會位移），`maintain_selection_offset=True`；
  3. 再用 `call_after_refresh` 把 `scroll_y` 設成「原值＋高度增加的量」；
  4. 呼叫 `history.clear()`。
- 跳很遠（`g`、跳到還沒載入的符合）：
  - 先只對 `Preview` 連續 `step()`（只讀檔，不碰畫面）；
  - 最後**一次** insert 接好的文字，或 `load_text` 再還原游標與捲動；
  - 不可以一段一段 insert（每次 insert 都會重算整棵語法樹，3 MB 要 15～30 秒）。
- `Preview.text` 改成先收集成 list，最後再接起來，避免平方級的成本。
- `_no_header` 只在讀到檔案開頭（`start == 0`）時套用（W10）。
- `Preview` 記下第一次讀時檔案的大小與 mtime；之後讀時不一樣，就重新建預覽（W11）。

### 搜尋：計數，不做位移換算（W1）
- 一個比對函式，兩邊都用：在 decode 後的字串上逐行 `re.finditer(re.escape(q), line, re.IGNORECASE)`。
- 在檔案上找（Enter 時讀整個檔，從檔頭結束的地方開始，檔頭的範圍和 `_no_header` 用同一個規則），只回答兩件事：
  - 一共有幾個（N）；
  - `at` 之前有幾個（還沒載入的部分有幾個，記為 B）。
- 已載入的部分，直接在 TextArea 的文字裡找，得到每個符合的位置。
- 跳到第 k 個（從 1 數）：
  - 如果 k ≤ B（還沒載入），先依「跳很遠」載入到 B < k；
  - 然後它就是已載入文字裡的第 k − B 個。
- 不變量：對每一個 `at`，B＋已載入的個數＝N。測試內文要涵蓋：
  - 切在 `\n\n`、切在 `\n## `；
  - 中文、`ß`；
  - 檔頭裡也有這個字。
- 沒有檔案的預覽（`Preview(None)`）：只在記憶體的文字裡找，B＝0。
- 目前那一個用 selection 標。`n`、`N` 從游標的位置往下、往上找，不是從上一個符合。

### 搜尋框與狀態（W5）
- 預覽區下方一行 `Input`（`id="search"`）：
  - `/` 打開並取得焦點；
  - Enter 送出，搜尋框關掉，焦點回到預覽區；
  - Esc 關掉；
  - 和清單的篩選框是兩個不同的 Input。
- 計數顯示在搜尋框那一行的位置（搜尋框關掉後改顯示計數）。
- 預覽被換掉：
  - 換到另一個 Session：清掉；
  - 同一個 Session 換成整份（`loaded()`）或重讀：用同一個字重新搜，游標回到最後一行，提示「已換成整份對話」；
  - 只有最後一則時，計數加「（只有最後一則）」；
  - `loaded()` 失敗時把「載入中」換成「讀不到整份對話」。

## Risks / Trade-offs

- [`textual[syntax]` 多裝 15 個語言的 grammar] → 只在安裝時多幾 MB；markdown 語法裝不起來就不上色。
- [原文的 code fence 被切在中間（T6 的 Y3）時，之後全部被染成程式碼的顏色] → 內容不受影響，接受。
- [`g` 在很大的檔案上仍要一次 `load_text`（3 MB 約 1 秒）] → 是使用者明確要求的動作；4.2 量 3 MB 的 `g` 與跳到第一個符合，上限 1.5 秒。
- [使用者習慣 `Tab` 換頁] → 這是使用者自己選的改法；README、`docs/acceptance.md`、按鍵列都要更新。
- [搜尋框打中文] → 由 change `ime-kitty-keyboard` 修。
