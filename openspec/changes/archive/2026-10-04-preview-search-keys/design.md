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
  - 沒有視窗時切換兩邊：從篩選框切出去，和按 Enter 一樣（篩選框收起來，篩選照樣生效）；從搜尋框切出去，關掉搜尋框，還沒送出的字丟掉，標亮留著。
- `[`、`]`、ctrl+t 以及其他清單的鍵，`check_action` 只在 `side() == "list"` 時放行。這些鍵都不是 priority，篩選框會先把它們當成字吃掉；`[`、`]` 也不要設成 priority。
- 例外是 Enter 的 `primary`（review R1）：它是 priority，會比篩選框先拿到 Enter，所以 MUST 維持「焦點是 `#table` 本身」才放行，不可以用 `side()`。
- `AgoraApp.on_key` 的 Esc：只在 `#filterbar` 開著而且 `side() == "list"` 時清篩選（review R6）。因為 Tab 切出去時篩選框已經收起來，預覽區的 Esc 不會動到篩選；預覽區的 Esc 放在 `PreviewText`。

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
  - 顏色（PM 10-04 決定）：`## user` 用 bold `#87afff`，`## assistant` 用 bold `#d787ff`。改之前其實沒有替這兩種標題設顏色；cyan、green、`#ff8700` 是清單裡 agent 與合併的顏色，`#ffd75f` 是標記，紅色是錯誤，都避開。
  - 若要讓這兩行不被 tree-sitter 的 heading 樣式蓋掉而覆寫 `_build_highlight_map`，MUST 跳過 `line_index >= document.line_count` 的行（code fence 的結束點會落在最後一行的下一行，review S1）。
  - 符合改變時要清 `_line_cache`（私有屬性，附註解）再 `refresh()`；
  - 游標行的底色會蓋掉符合的底色，所以符合用前景色加粗體或底線。
- pinned 行（dir、tags）與「↑ 往上捲…」提示維持在 TextArea 之外的 `Static`，提示改成「↑ 往上捲、按 k 或 g 載入更早的內容（還有約 N KB）」（W13）。

### 分段載入接在 TextArea 上（W2、W3、W10）
- 補一段：
  1. 先記下 `scroll_y` 與 `wrapped_document.height`；
  2. 在 `(0, 0)` 插入 `f"{step}\n"`（結尾要有換行，游標的欄位才不會位移），`maintain_selection_offset=True`；
  3. 再用 `call_after_refresh` 把 `scroll_y` 設成「原值＋高度增加的量」；
  4. 呼叫 `history.clear()`。
- 畫面不跳的兩種情況（review R4）：
  - 捲到頂觸發（游標沒動）：原本最上面那一行的螢幕位置完全不變；
  - `k`／`↑`／PgUp 觸發：還原捲動之後，游標移到前一段的最後，再照 TextArea 平常的規則捲到游標看得到為止。原本那一行最多往下移一頁，`k`／`↑` 時剛好一列。
  - 測試驗的是「原本那一行」的螢幕位置，不是游標的。
- `put_preview`、`load_all_earlier` 換內容與捲動的期間，要設 `_suppress_scroll_load`，到 `call_after_refresh` 之後才放開；否則 `load_text` 把 `scroll_y` 變成 0，會被當成「捲到頂」而多讀一段（review S2）。
- `k`／`↑`／PgUp 補完之後，游標放在折行後「原本那一行的上一列」，用 `wrapped_document` 換回文件位置；不是上一行的第一段（review S3）。
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
- 目前那一個用 selection 標。`n`、`N` 從游標的位置往下、往上找，不是從上一個符合，到頭時繞回另一端。
- Enter 送出時（review R2）：從游標往下找最近的；往下沒有時，跳到往上最近的那一個，不繞回最前面。預覽一打開游標在最後一行，這樣第一次搜尋通常只在已載入的尾端裡找，不會為了檔頭附近的一個符合載入整份。

### 搜尋框與狀態（W5）
- 預覽區下方一行 `Input`（`id="search"`）：
  - `/` 打開並取得焦點；
  - Enter 送出，搜尋框關掉，焦點回到預覽區；
  - Esc 關掉；
  - Tab／shift+tab 切出去時關掉，還沒送出的字丟掉；
  - 和清單的篩選框是兩個不同的 Input。
- 計數顯示在搜尋框那一行的位置（搜尋框關掉後改顯示計數）。
- 預覽被換掉（review R5）：
  - 「同一個 Session」以清單那一列的 key 判斷；重讀後那一列不在了，當成換 Session；
  - 換到另一個 Session：清掉；
  - 有搜尋時，同一個 Session 換成整份（`loaded()`）：用同一個字重新搜，游標回到最後一行，提示「已換成整份對話」；
  - 有搜尋時，動作完成後重讀：同樣重搜，提示「內容已更新」；
  - 沒有搜尋時不提示；
  - 只有最後一則時，計數加「（只有最後一則）」；
  - `loaded()` 失敗時把「載入中」換成「讀不到整份對話」。

## Risks / Trade-offs

- [`textual[syntax]` 多裝 15 個語言的 grammar] → 只在安裝時多幾 MB；markdown 語法裝不起來就不上色。
- [原文的 code fence 被切在中間（T6 的 Y3）時，之後全部被染成程式碼的顏色] → 內容不受影響，接受。
- [`g` 在很大的檔案上仍要一次 `load_text`] → 是使用者明確要求的動作。review 實測：3 MB 的 jsonl（閱讀版 2.1 MB）第一次按 `g` 1.02 秒；閱讀版本身 3.17 MB 時 1.55 秒。PM 10-04 決定：以「3 MB 的 Session 檔 ≤ 1.5 秒」為目標，閱讀版 3 MB 約 1.5 秒可以接受；4.2 在 pane 裡再量。
  - 4.2（PM 10-04 18:00 在 herdr pane 實測，kitty 協定關掉的預設狀態）：3 MB 的 Session 檔（閱讀版約 2.1 MB）按 `g` 1.40 秒，從只讀尾端的狀態 Enter 搜只在最前面的字並跳過去 1.33 秒，`n`／`N` 約 0.2 秒。目標達成。
  - review 的剖析（7ef3ff6）：`load_text` 的成本約 65～70% 是 TextArea 本身的折行，跟著**行數**走；tree-sitter 上色多 25～35%；我們覆寫的 `_build_highlight_map` 可以忽略。閱讀版本身就 3 MB、十幾萬到三十萬行的極端情況，`g` 與遠距跳轉約 2～3.5 秒。PM 決定接受：這兩個是使用者明確要求的動作，平常的操作（移動、`n`、補前一段）都很快。真的需要時，再考慮超過某個行數就不上色或不折行。單元測試的門檻放寬到 5 秒，只抓平方級的退化（機器忙時絕對時間會不穩）。
- [使用者習慣 `Tab` 換頁] → 這是使用者自己選的改法；README、`docs/acceptance.md`、按鍵列都要更新。
- [搜尋框打中文] → 由 change `ime-kitty-keyboard` 修。
