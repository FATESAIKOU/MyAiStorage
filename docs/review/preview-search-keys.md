# Review：preview-search-keys（issue #22），實作之前

審的是 `openspec/changes/preview-search-keys` 的 proposal、design、specs、tasks（03311c9）。
對照了 `src/agora/tui.py`、`openspec/specs/interactive-mode/spec.md`、`docs/design.md` 5.9、`docs/review/T6.md`，
以及 Textual 8.2.8 的原始碼（`widgets/_text_area.py`、`document/_edit.py`、`_tree_sitter.py`、`app.py`）。

驗證方式：在 scratchpad 另開一個 venv，裝 `textual[syntax]==8.2.8`，用自編的 Markdown 跑 `run_test`。
沒有碰 agora 的程式、真實的 Session，也沒有跑整合測試。

## 結論

**方向可行，但有 3 個 High 要在動手前改 design／spec。**

- 唯讀 `TextArea` 能做到大部分要求，下面這些都已在 8.2.8 實測：
  - 游標整行高亮；
  - `j`、`q` 這類字母鍵會走到 binding（`read_only` 時 `check_consume_key` 回 False，不會吃掉按鍵）；
  - 在 `(0,0)` 插入時，游標留在同一行文字上；
  - 沒裝 tree-sitter 時退回不上色。
- 三個 High：
  - **W1**：位移換算成行的算法會算錯；
  - **W2**：插入之後游標的行沒變，但**畫面會跳**；
  - **W3**：`g` 或跳到很前面時，一段一段 insert 在 3 MB 的檔案上要十幾秒。
- spec 漏掉的情況集中在兩處：
  - 視窗、輸入框開著時按 Tab／Esc 會怎樣（W4）；
  - 未匯入頁的預覽被「整份」換掉時，搜尋狀態要怎麼處理（W5）。

## 實測數字（120×40，`language="markdown"`、`soft_wrap=True`、`read_only=True`）

| 文件大小 | `load_text` | 按一次 `k` | 在最前面 insert 30 KB |
|---|---|---|---|
| 30 KB | 0.07 s | 0.09 s | 0.03 s |
| 300 KB | 0.11 s | 0.08 s | 0.05 s |
| 1 MB | 0.31 s | 0.09 s | 0.11 s |
| 3 MB | 1.03 s | 0.15 s | 0.30 s |

（按鍵時間包含 `pilot.pause()` 的開銷，大約 0.08 s。）

- 在 (0,0) insert 時，游標的行號正確：從 (0,0) 變成 (638,0)，剛好是插入的行數。
- 但是游標在畫面上的位置**從第 1 列跳到第 38 列**，`scroll_y` 從 0 變成 748。游標原本在第 15 行時也一樣跳到第 38 列。

---

## High

### W1 位移→行的換算會錯（design「搜尋在檔案上做」）

design 的算法是：「數 `at` 到位移之間的換行數就是第幾行；`at == 0` 時扣掉標頭」。這樣算會錯，原因有四個：

1. **每一段接起來時會少一個空行。**
   - `Preview.step()` 對每一段做 `.strip("\n")`，再用一個 `\n` 接起來。
   - `read_tail` 切在 `\n\n` 時，原文是 `A\n\nB`，接起來變成 `A\nB`。
   - 所以每接一段，行號就偏一行，在 2 MB 的檔案上會偏幾十行。
2. **casefold 會改變長度。**
   - 例如 `ß`→`ss`、`İ`→`i̇`。在 casefold 之後的位元組上找到的位移，換不回原文的位置。
3. **TextArea 的欄位以字元為單位，不是位元組。**
   - 同一行裡要先把前綴 decode 再算長度。中文一個字就差 3 倍。
4. **檔頭（front matter）沒有排除。**
   - 整份檔案的位元組包含 YAML 檔頭，裡面的符合也會被算進「共 N 個」。
   - 但這些符合永遠不會顯示，跳過去時也換算不出位置。

**建議：不要做位移換算。**
- 在檔案上找，只用來回答兩件事：
  - 一共有幾個；
  - `at` 之前有幾個（也就是還沒載入的部分有幾個）。
- 已經載入的部分，直接在 TextArea 的文字裡找。
- 要跳到第 k 個時：
  - 如果 k 在還沒載入的範圍，就一直 `step()`，直到 `at 之前的個數 < k`；
  - 然後它就是已載入文字裡的第 `k − at 之前的個數` 個。
- 兩邊要用**同一個比對函式**：在 decode 後的字串上，逐行 `re.finditer(re.escape(q), line, re.I)`。
- 檔案那一邊要從檔頭結束的地方開始找，檔頭的範圍用和 `_no_header` 一樣的規則算。
- 要加一個不變量測試：對每一個 `at`，`at 之前的個數 + 已載入的個數 == 總數`。
  測試內文要涵蓋：切在 `\n\n`、切在 `\n## `、中文、`ß`、檔頭裡也有這個字。

### W2 在 (0,0) 插入後畫面會跳（spec「游標停在原本那一行，不跳」）

- `insert(..., maintain_selection_offset=True)` 會把游標的行號加上插入的行數，這部分是對的。
- 但是 `selection` 改了以後，`_watch_selection` 會呼叫 `scroll_cursor_visible()`，把游標捲到畫面最下緣（實測從第 1 列跳到第 38 列）。
- 這就是 T6 的 Y2 在 TextArea 上又出現一次。

**建議：**
- design 寫明：insert 之前記下 `scroll_y` 和 `wrapped_document.height`；insert 之後把 `scroll_y` 設成「原值 + 高度增加的量」，用 `call_after_refresh`。要用折行後的高度，不是原文的行數。
- 插入的文字要是 `f"{step}\n"`，結尾要有換行，游標的欄位才不會跟著位移。
- 2.3 的測試要驗 `cursor_screen_offset.y` 插入前後一樣，只驗 `cursor_location` 不夠。T6 的 Y2 就是測試名稱寫了「keeps the line」，卻沒有驗這件事。

### W3 `g` 和跳到很前面時，一段一段 insert 太慢；也和 Goals 的「不讀整份、不排整份」衝突

- 每一次 `edit()` 都會跑 `_build_highlight_map()`，它對**整棵語法樹**做查詢，所以成本和整份文件一樣大。
- 在 3 MB 的檔案上，每 insert 30 KB 要 0.30 s。照 design 的做法「一直補到 `more()` 為假」，大約 100 段，總共要 **15～30 秒**。用 `load_text` 一次放進去只要 1.0 s。
- `history.record(edit)` 會把每一段插入的文字都留在 undo 紀錄裡，記憶體多佔一份。

**建議：**
- `g`，以及要跳過多段的搜尋：先只對 `Preview` 連續 `step()`（只讀檔、不碰畫面），最後**一次** insert 接好的文字，或直接 `load_text` 再還原游標。
- 每次 insert 之後呼叫 `history.clear()`。
- Goals 要改寫成：「平常只讀、只排尾端；`g` 與跳到還沒載入的符合是使用者明確要求的例外」。
- 4.2 要量 3 MB 的 `g` 和跳到第一個符合，並訂一個上限（例如 ≤ 1.5 s）。
- 附帶一點：`Preview.text` 每一步都把整串字串重組一次，步數多時是平方級的成本，可以改成先收集成 list，最後再接起來。

---

## Medium

### W4 Tab、Esc 在視窗與輸入框裡的行為，spec 沒有寫（spec「按鍵」）

- **Confirm 視窗裡的 Tab 照舊**
  - 現在的做法：Tab 由 `action_next_tab` 轉成 `focus_next`，shift+tab 由 `action_toggle_focus` 轉成 `focus_previous`。
  - 改成「Tab 和 shift+tab 都是 `toggle_focus`」之後，如果只留一個 action，Confirm 視窗裡的 Tab 會變成往回走。
  - 兩個鍵要各自轉交，1.3 要分別測。
- **其他視窗**（Choose、AskText、Tell、Busy、Run）
  - `App.query_one` 查的是 `default_screen`（`app.py:941`），所以在 AskText 裡按 Tab，會把**底下那個畫面**的焦點換掉，使用者看不到。
  - 現在的程式在這些視窗裡按 Tab 會換掉底下的分頁，是同一類的問題。
  - spec 應該寫：視窗開著時，Tab／shift+tab 只在視窗裡移動焦點，或什麼都不做。
- **輸入框有焦點時的 Tab**
  - 現在的邏輯是「焦點不是清單就到清單」，所以從篩選框或搜尋框按 Tab 都會到清單，篩選框和搜尋框也會一直開著。
  - 要在 spec 定義下面兩種情況，並寫進測試：
    - 篩選框有焦點時按 Tab：焦點到預覽區，篩選照樣生效；
    - 搜尋框有焦點時按 Tab：關掉搜尋框，焦點到清單，標亮保留或清掉（二選一）。
- **Esc 的優先順序**
  - `AgoraApp.on_key` 只要篩選列是開的，不管焦點在哪裡，按 Esc 都會清掉篩選並把焦點拉回清單。
  - 情境：使用者打開篩選後切到預覽區，預覽區裡也有搜尋，這時按 Esc，可能會同時清掉搜尋標亮和清單的篩選，焦點還會被拉走。
  - spec 要定義：預覽區裡的 Esc 依序「關搜尋框 → 清標亮」，**不動清單的篩選**。`on_key` 要改成只在焦點是篩選框時處理 Esc，並加測試。
- **`ctrl+t`**
  - 它是 priority 的 binding，`check_action` 現在一律回 True，所以在預覽區也會生效。
  - spec 把它列為清單的鍵，`check_action` 要擋掉。
- **`q`**
  - 實測：唯讀的 TextArea 不吃 `q`，會走到 App 的 binding；`Input` 會把 `q` 當成字。照 design 做就對了，不用改。

### W5 搜尋狀態的生命週期沒有定義（「未匯入頁只有最後一則時的搜尋」）

- 未匯入頁的預覽一開始是 `Preview(None)`，只有最後一則；背景讀完整份之後，`loaded()` 會呼叫 `put_preview` 換上整份。
- 這時使用者可能正在預覽區裡搜尋。換上之後，游標會跳到最後一行，搜尋結果（「第 k 個／共 N 個」、標亮）全部失效。
- 而且只有最後一則時，「共 2 個」換上整份之後可能變成「共 40 個」，對使用者是誤導。

**spec 要補三種情況：**
1. 換到另一個 Session：清掉搜尋框、標亮、計數。
2. 同一個 Session 的預覽被換掉（最後一則 → 整份；動作完成後 `reload()` 清掉快取）：
   - 建議用同一個字重新搜一次；
   - 游標停在換上之前的那一個符合，或回到最後一行（擇一寫明）；
   - 狀態行提示「已換成整份對話」。
3. 只有最後一則時搜尋：計數加上「（只有最後一則）」。
   如果整份讀取失敗，`loaded()` 不會再來，要確認畫面不會一直停在「載入中」。

Agora 頁 `session.md` 讀不到時，預覽也是 `Preview(None)`，搜尋同樣在記憶體裡找，行為一樣即可。

### W6 標亮的做法：覆寫 `get_line()`，不要覆寫 render line；快取要清

- `TextArea.get_line()` 的 docstring 寫「You can stylize the Text object returned here」，這是官方給的接點。逐行繪製（`_render_line`）會先呼叫它，再疊上游標行、選取、語法上色，所以不需要自己算折行和捲動位移。
- **快取**
  - `render_line` 的快取鍵（`_line_cache`，`_text_area.py:1366`）不包含搜尋的符合。符合改變（Enter、`n`、`Esc`）之後，畫面上已經畫過的行不會重畫。
  - 要清掉 `_line_cache`（私有屬性，要寫註解說明為什麼要碰它）再 `refresh()`。
- **游標行上的符合會看不到**
  - 游標行的樣式是在 `get_line()` 之後才疊上去的，會蓋掉符合的底色。
  - 符合的樣式要用前景色或粗體／底線，不要只靠底色，或接受「游標那一行看不到其他符合」並寫進 design。
- **目前那一個用 selection 標**
  - 按 `j`／`k` 時 selection 會變回單點游標，「目前那一個」的標亮就消失了。這可以接受，但 spec 要寫明。
  - `n` 從游標的位置往下找，不是從上一個符合往下找。

### W7 TextArea 自帶的按鍵，以及「一行」的定義

- **自帶的按鍵**
  - 唯讀的 TextArea 仍然有這些 binding：
    - `shift+方向鍵` 選取，`f6`／`f7` 選一行／全選，`ctrl+c` 複製；
    - `ctrl+a`／`ctrl+e`、`home`／`end` 到行首／行尾；
    - `pageup`／`pagedown`，`←`／`→`，`ctrl+←`／`ctrl+→`。
  - spec 寫「焦點在預覽區時，按鍵 MUST 是規定的那些」，Non-goal 寫「沒有選取、複製」，兩者都和這些自帶的鍵矛盾。
  - `docs/design.md` 5.9 已經寫了預覽區可以用 PgUp／PgDn。
  - **建議：** spec 加上 PgUp／PgDn 與 ←／→；選取和複製的鍵要嘛在 `PreviewText.BINDINGS` 裡蓋掉，要嘛寫明「保留 TextArea 預設」。
- **載入前一段的觸發條件不完整**
  - 現在只寫了「第 0 行再按 `k`／`↑`」。在頂端按 PgUp、或用滑鼠滾輪捲到頂，也要能觸發。
  - `up` 是 TextArea 自己的 binding，要在子類別裡覆寫 `action_cursor_up`，或重新綁 `up`。
- **「一行」的定義**
  - `soft_wrap=True` 時，`cursor_up` 是按**折行後的行**移動，但游標行高亮的是**整個原文行**。
  - 結果是在一段很長的折行段落裡按 `k`，高亮的那一行不會變。
  - spec「按 `k` 三次 → 倒數第四行」要寫明是哪一種行，測試內文用不會折行的短行。

### W8 按鍵列不是 Footer；`check_action` 判斷焦點的方式會失效

- **按鍵列**
  - design 寫「`Footer` 依焦點顯示，切焦點時呼叫 `refresh_bindings()`」，但程式用的是自己的 `paint_keys()` 和固定的 `KEYS[tab]`（`tui.py:53`）。
  - 要改成依（分頁 × 哪一邊）查表。
  - 而且要在**任何**焦點改變時重畫：滑鼠點、打開篩選框或搜尋框都算，用 `on_descendant_focus` 之類的事件。只在 `toggle_focus` 裡重畫不夠。
- **`check_action`**
  - 現在用 `in_preview = isinstance(self.focused, VerticalScroll)` 判斷焦點在不在預覽區。
  - `TextArea` 不是 `VerticalScroll`，換掉之後這個判斷永遠是 False，清單的鍵就會在預覽區生效，W4 的 `ctrl+t` 也一樣。
  - 搜尋框有焦點時也要算作「預覽那一邊」。
  - **建議：** 第 1 節先加一個 `side()`，依焦點所在 widget 的 id 判斷：`#table`、`#filter` 算清單；`#right` 和搜尋框算預覽。第 2 節換 widget 時只要保留 `id="right"`。

### W9 `textual[syntax]` 的退路與上色範圍

- **退路**
  - 沒有 `tree_sitter`：Textual 只會 log 一個警告，然後用純文字，沒問題。
  - 有 `tree_sitter`、沒有 `tree_sitter_markdown`（例如輪子壞掉）：`_set_document` 會**丟出 `LanguageDoesNotExist`**（`_text_area.py:1147`），預覽區就壞了。
  - 要 try/except 退回 `language=None`，2.1 的測試兩種情況都要 monkeypatch 驗一次。
- **上色範圍**
  - `tree-sitter/highlights/markdown.scm` 只有區塊層級：標題、清單符號、引用、code fence。沒有粗體、斜體、行內 code。
  - 現在 `## user`／`## assistant` 各有自己的顏色（design 5.9「顏色」），改用 TextArea 之後會變成主題的 heading 色。
  - 這兩點要讓使用者知道，寫進 proposal。
- **依賴**
  - `textual[syntax]` 會裝 15 個語言的 grammar。只需要 `tree-sitter` 加 `tree-sitter-markdown` 也可以，但要自己確認版本相容。
  - 用哪一種都可以，`uv.lock` 都會變，這個變更要記在 commit 裡。

---

## Low

- **W10**：`_no_header` 對**每一段**都會套用。如果某一段的 30 KB 裡沒有 `\n## ` 也沒有 `\n\n`，切點就沒有往後移，這一段剛好以 `---` 開頭時，會被當成檔頭吃掉一截。這會讓 W1 的計數對不上。建議只在 `start == 0` 時套用。
- **W11**：搜尋在按 Enter 時讀檔，但已經載入的文字可能是更早讀的；中間如果有背景同步或 `local_reading` 改寫了檔案，兩邊會對不上。建議在 `Preview` 記下第一次讀時的（大小、mtime），不一樣就重新載入預覽。
- **W12**：T6 的 Y3（切點落在 code block 裡）在原文加上色時，會變成「之後全部被染成程式碼的顏色」。不影響內容，可以接受，寫進 design 的風險即可。
- **W13**：提示文字「↑ 往上捲載入更早的內容」要改成也提到 `k`／`g`。另外，游標行高亮不會因為失去焦點而消失（只看 `_has_cursor`），焦點在清單時預覽區也會有一條亮行，要確認這是想要的樣子。
- **W14**：issue #23（輸入法）的影響已經寫在 proposal 裡，沒有其他意見。

---

## 關掉 kitty 協定（ime-kitty-keyboard，#23）之後，按鍵還分得出來嗎

都分得出來。逐鍵的對照表在 `docs/review/ime-kitty-keyboard.md`，這裡只列重點：

- `Tab` 是 `\t`，`shift+tab` 是 `\x1b[Z`，`[`、`]` 是字元本身，`G`、`N` 是大寫字元，`ctrl+t` 是 `\x14`，互相不會混淆。
- 傳統編碼下，`Tab` 和 `ctrl+i` 送出的是同一個序列。這個 change 沒有用到 `ctrl+i`，沒有影響。
- **Esc 會等 100 ms（`ESCDELAY`）**才確定是 Esc。這段時間內又按了 `[`，兩個鍵會被當成 CSI 序列的開頭，Esc 和 `[` 可能都收不到。
  - 在預覽區按 Esc 清掉標亮之後，要先按 Tab 切回清單，才會按 `[`，很難在 100 ms 內做完。
  - 列為 Low，不用改設計。
- **單元測試抓不到這一點。** `run_test` 是直接把按鍵名稱送進 app，不經過終端機的編碼。所以 4.2 PM 在 pane 裡實際按一遍時，要在**關掉 kitty 的狀態**（#23 合進來之後的預設）按 Tab、shift+tab、`[`、`]`、`G`、`N`。

## 和 interactive-mode 主 spec、T6 的關係

- **主 spec**
  - 主 spec 只有「按鍵」一條和這次衝突，delta 用 MODIFIED 整條取代，沒問題。
  - 但 delta 把「等待視窗裡 Esc 中斷」寫成「照舊」。依 OpenSpec 的規則，MODIFIED 會整條取代，所以要把原文的 Esc 中斷、確認視窗的 Tab（S1）**照抄進去**，不然歸檔之後這兩點就從 spec 消失了。
- **`docs/design.md` 5.9**
  - 按鍵表、「預覽：整份對話用 Markdown 顯示」、「完整對話……shift+tab 切到預覽時」這三處都要改，4.1 已經列了。
  - 「完整對話」那段說未匯入頁「按 shift+tab 切到預覽時才讀整份」，但現在的程式是游標停下 150 ms 就在背景讀，文件本來就和程式不一樣，這次一起改掉。
- **T6**
  - 分段讀取（`read_tail`、`Preview.step`、150 ms 防抖、背景只讀檔）可以照用。
  - 要注意的只有 W1（接段時的 strip）、W2（Y2 再次出現）、W3（`g` 與跳躍是例外）、W10。

## tasks 的分工

- 第 1 節（impl3）和第 2、3 節（impl2）是**先後**做的（「第 1 節 commit 之後」），不會同時改 `tui.py`。但有兩個交接點要先講好：
  1. **`check_action` 和 `paint_keys` 會被兩個人改到。**
     - 第 1 節寫的時候，預覽區還是 `VerticalScroll`；第 2 節換成 TextArea 時，又得回頭改這兩個函式。
     - 照 W8 的建議，第 1 節做 `side()`，並把 `KEYS` 拆成清單／預覽兩張表。預覽那張表第 1 節先空著或放佔位，第 2、3 節只在表裡加列。
  2. **`AgoraApp.on_key`（Esc）**
     - 第 1 節要照 W4 改成只處理篩選框的 Esc，第 3 節的預覽區 Esc 才能放在 `PreviewText`，不會互相打架。
- 1.3 要寫明：`test_tui.py` 現在有 **17 處**用 `"tab"` 換頁，都要改成 `]`。另外有 3 處引用 `#history`／`#right`／`load_earlier`，第 2 節要改寫（T6 的 Y1、Y2 測試也在裡面，要保留它們驗的事）。
- 3.3「搜尋框裡打 `q` 是字」和 1.3 的鍵測試有重疊；`q` 的通用規則由 1.3 負責，3.3 只測搜尋框。
- 第 4.1 節（impl4，文件）要等這份 review 的 W4、W5、W7 在 spec 裡定案再寫，不然按鍵表會寫兩次。

## 動手前要改的（給 PM 的清單）

1. design「搜尋在檔案上做」改成 W1 的計數做法，加上不變量測試。
2. design「分段載入」加上 W2 的捲動還原，以及 W3 的一次 insert；Goals 加上例外條款；4.2 加上量測上限。
3. spec「按鍵」補 W4（視窗裡、輸入框裡的 Tab；Esc 的優先順序；`ctrl+t`），並照抄原本的 Esc 中斷與 S1。
4. spec「預覽區搜尋」補 W5 的三種情況；「游標與捲動」補 W7（PgUp／PgDn、TextArea 自帶的鍵、行的定義）。
5. tasks 1.1、1.2 寫明 `side()` 與拆開的 `KEYS`（W8）；2.1 寫明兩種退回不上色的情況（W9）。

---

## 複審（e163d5c）

這次只看規劃文件：proposal、design、spec、tasks。沒有看程式，也沒有碰 impl1、impl3 還沒 commit 的檔。

**結論：W1～W13 與「動手前要改的」五點都已經寫進去了，可以照這版開工。** 另外發現 2 個 Medium、5 個 Low，是這次改寫新出現的，或原本就沒寫清楚的；都是改幾句文字就能解決，不影響第 1 節（impl3）現在在做的東西。唯一和第 1 節有關的是 R1，請 PM 轉告 impl3。

### 逐項對照

| 項目 | 寫在哪裡 | 判定 |
|---|---|---|
| W1 計數取代位移換算、同一個比對函式、不變量測試 | design「搜尋：計數」、tasks 3.1、3.3 | ✓（但 proposal 的 Impact 還是舊說法，見 R3） |
| W2 還原捲動、結尾加 `\n`、驗畫面位置 | design「分段載入」、spec「補前一段不跳」、tasks 2.2、2.3 | ✓（Scenario 與測試的說法對不上，見 R4） |
| W3 一次 insert、`history.clear()`、list、Goals 的例外、1.5 秒上限 | design、spec、tasks 2.2、4.2 | ✓ |
| W4 視窗裡的 Tab、輸入框的 Tab、Esc 的優先順序、ctrl+t、照抄原 spec 的 Esc 與 S1 | spec「按鍵」與三個新 Scenario、design「按鍵與視窗」、tasks 1.2、1.3 | ✓（Enter 的部分見 R1） |
| W5 搜尋狀態的生命週期 | spec「預覽區搜尋」、design「搜尋框與狀態」、tasks 3.2、3.3 | ✓（重讀時的提示見 R5） |
| W6 `get_line()`、清 `_line_cache`、符合不用底色、選取會消失 | design、spec、tasks 3.2 | ✓ |
| W7 TextArea 自帶的鍵、PgUp 和捲到頂也會補、「一行」的定義 | spec「游標與捲動」、design、Non-goals | ✓ |
| W8 `side()`、拆開的 `KEYS`、`on_descendant_focus` | design、tasks 1.1 | ✓ |
| W9 兩種退回不上色、只有區塊層級的色、`uv.lock` | proposal、design、tasks 2.1 | ✓ |
| W10 `_no_header` 只在 `start == 0` 時套用 | design、tasks 2.2 | ✓ |
| W11 記下檔案大小與 mtime | design、tasks 2.2 | ✓ |
| W12 code fence 被切在中間時的染色 | design 的 Risks | ✓ |
| W13 焦點離開時不高亮、提示文字 | spec、design、tasks 2.2、2.3 | ✓ |
| 五點清單 | 同上 | 五點都有做到 |

### 要 PM 再改的

**R1（Medium）：Enter 的 `primary` 不可以改成用 `side() == "list"` 判斷。**
- design 寫「`[`、`]`、ctrl+t 以及其他清單的鍵，`check_action` 只在 `side() == "list"` 時放行」，而篩選框也算在 `"list"` 那一邊。
- `enter` 是 **priority** 的 binding，會比 Input 先拿到按鍵。如果 `primary` 也照這條規則判斷，在篩選框裡按 Enter（包括輸入法選字的 Enter）就會直接接續或匯入游標那一列，而不是把篩選送出。
- 其他清單的鍵都不是 priority，篩選框會先把它們當成字吃掉，所以沒問題。`ctrl+t` 在篩選框裡生效，正是想要的。
- **要改：**
  - design 與 tasks 1.2 寫明：`primary` 維持「焦點是清單的表格本身」；`[`、`]` 不設成 priority；
  - 1.3 加一個測試：在篩選框按 Enter，不會開接續或匯入。
- 這一點和 impl3 現在做的程式直接相關。

**R2（Medium）：第一次按 Enter 搜尋，很可能就把整份檔案載入。**
- spec 寫「游標 MUST 跳到從游標位置往下最近的符合」，到頭時繞回另一端。
- 但預覽一打開，游標就在最後一行（spec「換到另一個 Session 時，游標 MUST 在最後一行」）。所以第一次搜尋時，下面一定沒有符合，會繞回**整份檔案的第 1 個**。如果它在檔案開頭，就要把 3 MB 全部載入。
- 使用者通常想看的是**離尾端最近**的那一個。
- **建議：**
  - Enter 時，往下沒有符合，就跳到**往上最近**的那一個，不要繞回開頭；`n`／`N` 的繞回照舊；
  - 或者至少在 spec 寫明「第一次搜尋可能載入整份」是可以接受的。
- 這是使用者體驗上的選擇，需要 PM 決定（必要時問本人）。
- 另外，Scenario「跳到還沒載入的地方」寫的是「只出現在最前面的字，按 `n`」。照現在的規則，按 Enter 時就已經跳過去了。要把 Scenario 改成「按 Enter（或 `n`）」，不然測試照字面寫，會測到「按 Enter 之後再按 `n`」，繞了一圈又回到同一個。

**R3（Low）：proposal 的 Impact 還寫著舊的做法。**
- 現在寫的是「新增在檔案上搜尋（不經過已載入的文字）」，和 W1 的新做法相反。
- 改成：「在檔案上計數（共幾個、還沒載入的有幾個），已載入的部分在預覽區的文字裡找」。

**R4（Low）：「不跳」的 Scenario 和測試說法不一致。**
- Scenario 寫「原本那一行仍在第 1 列**附近**，游標在它的上一行」，tasks 2.3 寫「`cursor_screen_offset.y` 不變」。
- 用 `k` 觸發時，游標本來就會往上移一行；如果原本那一行在畫面最上面，畫面還得往下捲一列，游標那一行才看得到。所以「游標的螢幕位置不變」不成立，「附近」又沒辦法驗。
- **建議寫成兩條：**
  - 捲到頂觸發時（游標沒動）：原本那一行在畫面上的位置**完全不變**；
  - 用 `k`／`↑`／PgUp 觸發時：原本那一行**最多往下移一頁**，游標所在的那一行要露出來。用 `k` 時剛好是一列。
- 2.3 的測試照這兩條驗的是「原本那一行」的螢幕位置，不是游標的。

**R5（Low）：重讀時的提示文字不對。**
- 生命週期規定：「同一個 Session 的預覽被換掉（換成整份，**或動作完成後重讀**）時，提示『已換成整份對話』」。
- 但重讀的時候並不是「換成整份」。而且這條規則沒有限定「有搜尋時」才適用，照字面讀，沒有在搜尋時也要提示。
- **建議：**
  - 只在有搜尋時才重搜並提示；
  - 提示文字分成兩種：「已換成整份對話」與「內容已更新」；
  - 「同一個 Session」以列的 key 判斷；重讀之後這一列不在了（例如被刪掉），就當成換 Session，全部清掉。

**R6（Low）：從篩選框按 Tab 切出去、再按 Tab 回來之後，焦點落在哪裡沒有寫。**
- spec 說切出去時篩選框留著；再按 Tab 回來時，焦點照現在的寫法會到清單的表格。
- 可是 design 也把 Esc 改成「只在焦點是篩選框時處理」。結果是：篩選框開著，焦點卻在表格上，按 Esc 沒有反應，只能按 `/` 回到篩選框，再按 Esc 才能關掉。
- **建議寫明其中一種：**
  - 篩選框還開著時，Tab 回到篩選框；
  - 或者表格有焦點、篩選框開著時，Esc 也能關掉篩選框。
- 另外，從搜尋框按 Tab 切出去時，打到一半還沒送出的字是丟掉，還是當成送出，也要寫一句。建議丟掉。

**R7（Low）：4.2 沒有寫要在關掉 kitty 協定的狀態下實際按一遍。**
- 本 review 的 kitty 那一節建議：4.2 在 pane 裡按一遍時，要在 #23 合進來之後的預設狀態（kitty 協定關閉）下按 Tab、shift+tab、`[`、`]`、`G`、`N`。這一點 tasks 還沒寫進去。
- 單元測試是直接把按鍵名稱送進 app，不經過終端機的編碼，測不出這一點，所以只能在 4.2 實際按。

### 沒有發現的問題

- **spec 內部：** 三個 Requirement 之間、和 design、tasks 之間，除了上面幾項，沒有其他矛盾。
- **主 spec：** 原本的 Esc 中斷、確認視窗的 Tab（S1）、「未匯入頁的按鍵列」都照抄進 MODIFIED 了，歸檔之後不會消失。
- **分工：**
  - 第 1 節負責 `side()`、`KEYS`、`on_key`；第 2、3 節只在預覽那張表加列，並新增 `#right`、`#search` 兩個 widget；
  - 交接的地方已經寫清楚，兩個人不會改到 `tui.py` 的同一段。

這次只改了這份 review（文件）。從 90f27f0 到 e163d5c 只動了 openspec 底下的文件，程式和上一次 commit 時完全一樣（當時 compileall 與完整 unit 560 passed）。工作區裡有 impl1、impl3 還沒 commit 的程式，現在跑 unit 會把它們一起跑進去，所以這次沒有重跑。

---

## 第 1 節程式審查（d52c591、4170b1a）

審的是 impl3 第 1 節「兩套按鍵」的兩個 commit，改到 `tui.py`、`test_tui.py`、`tasks.md`。對照的是 cc1891a 的 spec「按鍵」、design「先有 side()」「按鍵與視窗」（含 R1、R6 的決定），以及 tasks 1.1～1.3。

**結論：可以進第 2 節。行為都符合 spec，沒有 High。** 有 2 個 Medium，都是測試沒有守住的地方：程式現在是對的，但改壞了測試不會紅。另外有 1 個 Medium 是易用性（按鍵列沒有顯示換頁鍵），還有幾個 Low。這些都可以在第 2 節開工時一起補，不用另開一輪。

### PM 指定的五點

**(1) 拿掉 ctrl+t 的 priority 之後，篩選框裡還能切換標題／內文嗎？可以。**
- Textual 的 `Input` 沒有綁 ctrl+t（`_input.py` 的 BINDINGS 裡沒有這個鍵）。
- `Input` 只吃「可印出的字元」，ctrl+t 不是，所以按鍵會往上走到 App 的 binding。
- `check_action` 判斷 `side() == "list"`，篩選框算清單那一邊，所以會放行。
- 實測（探針測試，見下）：打開篩選框打了 `x`，按 ctrl+t，`app.content` 有切換，篩選框裡的字還是 `x`。
- 現有的測試都是在**表格**上按 ctrl+t 之後才按 `/`，沒有一個是在篩選框裡按。建議把這個探針加進 `test_tui.py`，因為 placeholder 寫的正是這個用法。

**(2) `_KeysTable`：不需要，建議拿掉（Medium，可以在第 2 節開工時順手做）。**
- 整個 repo 讀 `KEYS` 的地方只有一處：`paint_keys` 裡的 `KEYS.get((self.tab, side))`。
- 它特別寫的這些「相容」分支都沒有人用到：
  - 用裸的 `"agora"`／`"import"` 查；
  - tuple 的順序反過來（`(side, tab)`）也能查到；
  - 覆寫 `get`。
- 而且 `KEYS.get(...) or []` 會把打錯字變成「按鍵列一片空白」，不會報錯。
- **對第 2、3 節的影響：**
  - 預覽那一邊的鍵和分頁無關，現在卻要在 `"agora"`、`"import"` 兩份清單裡各加一次，容易漏掉其中一份。
  - 讀程式的人也得先看懂這個 dict 子類別，才知道要怎麼加列。
- **建議改成：**
  - `KEYS = {"list": {"agora": [...], "import": [...]}, "preview": [...]}`；
  - `paint_keys` 寫成 `KEYS["list"][self.tab] if side == "list" else KEYS["preview"]`，用中括號查，打錯字會直接報錯。

**(3) Enter 的 `primary` 只在 `#table`：✓**
- 寫的是 `side == "list" and focused.id == "table"`，符合 R1。
- 在副本裡改成只判斷 `side == "list"`，有 8 個測試會紅（包括篩選框裡按 Enter 的測試）。

**(4) 視窗開著時的 Tab／shift+tab：行為正確，但測試沒有守住（Medium）。**
- **程式的行為：**
  - Confirm 視窗裡，Tab 和 shift+tab 分別轉成 `focus_next`、`focus_previous`；
  - 其他視窗裡，`screen is not default_screen` 時直接 return；
  - 符合 spec 與 W4。
- **問題一：AskText 的測試抓不到錯。**
  - 測試只斷言 `app.tab` 沒有變，但 Tab 現在本來就不會換頁，所以這個斷言永遠成立，等於沒驗。
  - 在副本裡把「其他視窗 return」那一行拿掉，現有測試**全綠**。但實際行為已經壞了：關掉視窗之後，焦點從表格跑到 `PreviewArea(id='right')`。原因是 Tab 在畫面背後偷偷切換了底下畫面的焦點。
  - 我寫的探針抓得到：斷言「Tab 之後，焦點還在視窗裡的那個 widget 上」，以及「關掉視窗之後，焦點在 `#table`」。請把這兩個斷言補進 `test_ask_text_modal_tab_does_not_affect_underlying_screen`。
- **問題二（Low）：Confirm 的測試分不出方向。**
  - 把 Tab 改成往回走（`focus_previous`），測試也不會紅。
  - 因為 Confirm 視窗只有 OptionList 和 Checkbox 兩個可以拿焦點的 widget，往前一個和往後一個是同一個。
  - 這個改壞在實際使用上沒有差別，不用補。但如果以後 Confirm 多了按鈕，測試要改成用三個以上的 widget 來驗。

**(5) 改壞之後測試會不會紅？**

在 `git archive 4170b1a` 的副本裡，一次改壞一處，跑 `test_tui.py`：

| 改壞的地方 | `test_tui.py` | 探針 |
|---|---|---|
| `primary` 改成用 `side()` 判斷（R1 退回去） | 8 failed ✓ | |
| `d`、`m`、`e`、`P` 拿掉 `side` 的判斷 | 2 failed ✓ | |
| ctrl+t 拿掉「只在清單」 | 1 failed ✓ | |
| Tab 從篩選框切出去時不收起篩選框 | 1 failed ✓ | |
| `on_descendant_focus` 改成什麼都不做 | 1 failed ✓（滑鼠點的那個測試） | |
| 拿掉「其他視窗時 return」 | **全綠 ✗** | 1 failed ✓ |
| `on_key` 的 Esc 拿掉 `side() == "list"`（R6） | **全綠 ✗** | 1 failed ✓ |
| Confirm 裡的 Tab 改成往回走 | 全綠（只有兩個 widget，改了也沒差） | — |
| `]` 設成 priority | 全綠 | 全綠：Textual 讓 Input 先吃可印出的字元，就算設成 priority，在篩選框裡打 `]` 還是打字，所以這樣改其實無害 |

- **R6 的 Esc 為什麼抓不到：**
  - 現有測試只在「用 Tab 切到預覽區之後」按 Esc，但 Tab 切出去時篩選框已經收起來了，Esc 本來就不會動到篩選。
  - 篩選框還開著、焦點卻在預覽區的情況，只有**用滑鼠點**預覽區才會出現。我的探針就是這樣測的：點 `#right` 之後按 Esc，焦點仍在預覽區，篩選也還在。
  - 請把這個情況補進 `test_tab_from_filter_preserves_filter_and_esc_in_preview_does_not_clear_it`，或另寫一個測試。

### 其他

- **Medium：清單那一邊的按鍵列沒有換頁和切焦點的鍵。**
  - 這次是 BREAKING：Tab 不再換頁，換頁改成 `[`、`]`。但清單那張按鍵表裡沒有 `[ ]` 換頁，也沒有 `Tab` 切焦點。
  - 使用者只能從 Agora 頁是空的時候那句提示「按 ] 到未匯入」才知道。
  - 建議清單兩張表都加上 `("[ ]", "換頁")` 和 `("Tab", "切焦點")`。
- **Low：沒有用到的程式。**
  - `action_toggle_focus = action_shift_tab` 這個別名，現在沒有任何 binding 用到；
  - `action_next_tab` 和 `action_prev_tab` 幾乎一模一樣，而且 `check_action` 已經擋過的條件，在函式裡又檢查了一次。可以合成一個 `_change_tab(step)`。
- **Low：用滑鼠點離開篩選框時，篩選框不會收起來。**
  - R6 只規定了 Tab：Tab 切出去時篩選框收起來，滑鼠點不會。
  - 不違反 spec，焦點在預覽區時 Esc 也不會誤清篩選（探針驗過）。但兩種切法的結果不一樣，第 2 節或 4.1 寫文件時要知道這一點。
- `side()` 會沿著 parent 往上找 id。所以第 2 節把 `#right` 換成 TextArea、或在裡面放子 widget 時，都不用再改 `side()`。✓
- `test_tui.py` 原本 17 處用 Tab 換頁的地方，都已經改成 `]`。✓

### 給 PM 的清單（請轉告 impl3，可以在第 2 節的第一個 commit 一起做）

1. `KEYS` 改成普通的 dict，預覽那一邊只有一張表，`paint_keys` 用中括號查；拿掉 `_KeysTable`。
2. `test_ask_text_modal_tab_does_not_affect_underlying_screen` 補上兩個焦點的斷言：Tab 之後焦點仍在視窗裡的 widget；關掉視窗之後焦點在 `#table`。
3. 補一個 R6 的測試：篩選框開著時用滑鼠點預覽區，按 Esc，焦點仍在預覽區、篩選也還在。
4. 補一個測試：在篩選框裡按 ctrl+t 會切換標題／內文，篩選框的字不變。
5. 清單的按鍵列加上 `[ ]` 換頁、`Tab` 切焦點。
6. （Low）拿掉 `action_toggle_focus` 這個別名，把兩個換頁的 action 合成一個。

### 測試執行

- 探針與改壞的副本都放在 scratchpad，沒有加進 repo：
  - 探針一共四個：篩選框裡按 ctrl+t、篩選框裡打 `[` 和 `]`、用滑鼠點離開篩選框之後按 Esc、AskText 裡按 Tab；
  - 在沒改壞的 4170b1a 上，四個都通過。
- 完整 unit 是在 `git archive HEAD` 的乾淨副本裡跑的：
  - 那時的 HEAD 已經是 09f6a6b，包含 impl1 對 C1～C4 的修正；
  - compileall 通過，571 passed；
  - worktree 裡沒有 commit 的檔案沒有混進來。
- 沒有跑整合測試。

---

## 第 2 節程式審查（5e61693、0e75284）

這次審查的範圍：
- **5e61693**：第 2 節「預覽區的游標」，也包含第 1 節清單的 1（拿掉 `_KeysTable`）與 5（清單的按鍵列加上 `[ ]`、`Tab`）；
- **0e75284**：補第 1 節清單的 2、3、4、6。

對照的是：
- spec「預覽區的游標與捲動」；
- design「預覽區改用唯讀的 TextArea」「分段載入接在 TextArea 上」（含 R4）；
- tasks 2.1～2.3；
- 前面的 W2、W3、W6、W7、W9～W13。

驗證方式：
- 都在 `git archive` 的副本裡做；
- 探針、計時、故意改壞的程式都放在 scratchpad，沒有加進 repo；
- 沒有碰工作區。

**結論：先不要進第 3 節，先修 1 個 High（會讓程式整個掛掉）和 2 個 Medium。** 其他大致符合規劃。

### High

**S1：最後一則以 code block 結尾的 Session，一打開預覽，整個互動模式就掛掉。**
- **怎麼發生的：**
  - `PreviewText._build_highlight_map` 用 `self._highlights.keys()` 逐一取行號，再呼叫 `self.document.get_line(line_index)`（`tui.py:357` 起）；
  - tree-sitter 的 `fenced_code_block` 節點結束點的行號，會等於 `line_count`，也就是最後一行的下一行；
  - `Preview` 又會把結尾的換行 `strip` 掉，所以文件只要以收尾的 ```` ``` ```` 結束，就會查到不存在的那一行，丟出 `IndexError`。
- **實測：**
  - impl1 做的 3 MB 假 session，轉成閱讀版之後一選到那一列，app 就整個掛掉，traceback 是 `settled → put_preview → load_text → _build_highlight_map → IndexError`；
  - 用最短的內文重現：`"## assistant\n```python\nprint(1)\n```"` 會掛；以清單、引用、純文字結尾都不會。
  - assistant 的回答以 code block 結尾很常見，所以這是 High。
- **修法：**
  - 跳過 `line_index >= self.document.line_count` 的行；
  - 或者不覆寫 `_build_highlight_map`，改在 `get_line()` 裡處理標題的顏色（見下面 S6）。
- **要補的測試：** `PreviewText.load_text("## assistant\n```\nx\n```")` 不能丟例外，而且要在 `run_test` 裡實際選到這一列。

### Medium

**S2：換 Session 時會多讀一段，T6 的「平常只讀尾端」退回去了。**
- **實測（同一份內文，都在副本裡跑）：**

  | 版本 | 打開時讀的量 | 換到另一個 Session 時讀的量 |
  |---|---|---|
  | 改之前（cab2ba7） | 30,698 bytes | 30,698 bytes |
  | 現在 | 30,698 bytes | **61,402 bytes** |

  回到原本那一列也一樣是兩段。
- **原因：**
  - `put_preview` 呼叫 `load_text` 時，捲動位置會從大於 0 變成 0；
  - `watch_scroll_y` 就把它當成「捲到頂」，呼叫 `load_earlier`，又補了一段，畫面也多做一次插入和捲動還原。
- **修法：**
  - `_suppress_scroll_load` 這個旗標本來就在，但正式程式從來沒有設過它，只有測試在設。
  - 在 `put_preview` 和 `load_all_earlier` 換內容、捲動的期間，把它設起來，等 `call_after_refresh` 之後再放開。
- **要補的測試：** 換 Session 之後，`len(preview._chunks) == 1`（或者讀的位元組約等於一段）。

**S3：用 `k`／PgUp 補前一段時，如果上一行很長、會折行，游標會跑到畫面外。**
- **實測（3000 字一行的內文，和測試用的 `_big_body` 一樣）：**
  - 原本那一行確實往下移了一列，這部分 ✓；
  - 但游標落在 `(7, 0)`，位置在**畫面第 −124 列**，完全看不到。
- **原因：**
  - `restore()` 用 `move_cursor((row - 1, 0))`，把游標放到上一行的**第一段**；
  - 捲動卻只往上退一列（`delta - 1`）。
  - 上一行如果折成 h 列，游標就在畫面上面 h − 1 列的地方。
- **spec 怎麼說：** 「k 一次移動畫面上的一列」。所以游標應該停在上一行的**最後一段**，而不是第一段。
- **修法：** 用 `pane.wrapped_document` 把「原本那一行的上一列」換回文件位置（例如 `offset_to_location`），游標放在那裡；捲動照樣退一列就對了。PgUp 也是同樣的問題：它拿文件的行數去減頁高，而不是折行後的列數。
- **為什麼現有測試沒抓到：**
  - 測試假設每一行只佔一列，用 `(grew - 1) - scroll_y` 算游標在畫面上的位置；
  - 而且允許 `approx(..., abs=1)` 的誤差。
- **要改的測試：**
  - 用 `wrapped_document.location_to_offset(cursor_location)` 算游標的實際位置，斷言它在畫面內；
  - 原本那一行「剛好往下一列」要斷言 `== 1`，不要用 `abs=1`。因為 `abs=1` 連「完全沒動」（0）都會通過。
- 短行的內文是對的：原本那一行在第 1 列，游標在第 0 列。

### PM 指定的六點

**(1) 補前一段時，畫面真的不跳嗎？**
- **捲到頂觸發（游標沒動）：✓**
  - 探針用短行內文、滑鼠捲到頂，補完之後原本第一行在第 0 列，**完全不變**；
  - 這個情況由 T6 原本的測試 `test_scrolling_to_the_top_adds_the_step_above_and_keeps_the_line` 守著：把捲動還原拿掉，它會紅。
- **用 `k` 觸發：**
  - 短行：剛好往下一列 ✓；
  - 長行：見 S3。
  - 測試**有**驗「原本那一行」在畫面上的位置（`grew - scroll_y`），方向是對的，但容許誤差太寬（S3）。
- **用 PgUp 觸發：** 測試只驗「有補到」，沒有驗畫面位置。可以接受，但 S3 修好後要補一個斷言：游標在畫面內。

**(2) `g` 的做法對，時間在邊緣。**
- **做法：** `load_all_earlier` 是先只對 `Preview` 連續 `step()`，再 `load_text` 一次 ✓。
  - 在副本裡改成一段一段 insert，`test_benchmark_3mb_g_press` 會紅（量到 4.5 秒）。
- **實測（120×30，探針在 pytest 的隔離環境裡跑；只讀了 impl1 那份假 jsonl，是用 claude 轉接器加 `AGORA_CLAUDE_HOME` 讀的）：**

  | 內文 | 第一次按 `g` | 全部載入之後再按 `g` |
  |---|---|---|
  | 假 session 的閱讀版：2.11 MB，82,698 行 | **1.02 秒** | 0.09～0.10 秒 |
  | 同一份接長到 3.17 MB，124,050 行 | **1.55 秒**（目標 1.5，剛好超過） | 0.10 秒 |
  | 測試用的 `_big_body`：3 MB，每行 3000 字 | 0.51 秒 | — |

  - 第一份 jsonl 是 3.0 MB，但閱讀版只有 2.1 MB。
  - 測試裡用的是「很少行、每行很長」的內文，比真實內容快得多，所以測試量到的時間太樂觀。
  - 全部載入之後，在 3 MB 上按 `j` 約 0.22 秒（含一個 frame）。
  - 這些量測都是在 S1 先修好的狀態下做的：我在探針裡換了一個會檢查行號範圍的 `_build_highlight_map`，否則一打開就掛了。
- **建議：**
  - 4.2 由 PM 在 pane 裡用 3 MB 真實格式的內容再量一次；
  - 如果超過 1.5 秒，可以考慮讓 `_build_highlight_map` 的覆寫不要每一行都去讀文字（S6），或接受 1.5 秒左右。這是 PM 的判斷。
- **`test_benchmark_3mb_g_press` 會因為機器忙而失敗（Medium／Low）：**
  - 我同時跑 12 個改壞的副本時，**和 `g` 無關的改壞**也讓它失敗了（量到 1.95～2.3 秒）。
  - 單元測試裡放絕對時間的門檻，在機器忙的時候（例如同時有幾個隊員在跑測試）會偶爾失敗。
  - **建議：** 單元測試只抓「平方級」的退化，門檻放寬到 5 秒左右；1.5 秒的目標留給 4.2 的人工量測。或者把它標成 integration／benchmark，平常不跑。

**(3) 兩種退回不上色、焦點離開時不高亮、`## user`／`## assistant` 的顏色**
- **兩種退回不上色：✓**
  - 只 catch `LanguageDoesNotExist`；沒有 tree-sitter 時由 Textual 自己退回；兩種情況都有測試；
  - 把 except 改掉，對應的測試會紅。
- **焦點離開時不高亮：✓** `on_focus`／`on_blur` 切換高亮；改壞之後有兩個測試會紅。
- **user／assistant 的顏色：** `get_line()` 給 `## user` 加 bold cyan、`## assistant` 加 bold green ✓，測試會抓到拿掉顏色的改壞。但有兩點：
  - 覆寫 `_build_highlight_map`，就是為了拿掉這兩行的 tree-sitter `heading` 樣式，讓 `get_line()` 的顏色不被蓋掉。
    - 把這個覆寫整個拿掉，**現有測試全綠**：測試只檢查 `get_line()` 的回傳值，沒有檢查畫出來的樣子。
    - 而且這個覆寫就是 S1 掛掉的地方。
  - 「沿用原本的不同顏色」其實沒有可以沿用的東西：改之前（cab2ba7）的 `tui.py` 沒有替這兩種標題設顏色，rich 的 Markdown 是用同一種樣式畫所有標題。
    - 這不是錯，但 cyan 是清單裡 opencode 的顏色，綠色是 merge 的顏色，意思會混在一起。
    - 建議由 PM 決定顏色，順便改 spec 的字面，例如改成「`## user`／`## assistant` 用不同顏色」。

**(4) 給第 3 節搜尋標亮的接點：有，但要注意順序。**
- 接點是 `get_line()` 的最後一行會呼叫 `_stylize_search_matches(text, line_index, line_string)` ✓。
- 第 3 節要注意兩件事：
  - **快取：** 符合改變時，要清 `_line_cache` 再 `refresh()`（W6）。
  - **樣式會被蓋掉：** TextArea 會在 `get_line()` **之後**才套上語法上色（前景色）和游標行（底色），兩者都會蓋掉 `get_line()` 加的樣式。
    - 所以標亮如果只改前景色，在標題、code block 裡會被語法上色蓋掉；只改底色，在游標那一行會被蓋掉。
    - 建議用不會被這兩者蓋掉的屬性，例如 `reverse` 或 `underline` 加 `bold`。第 3 節的測試要檢查畫出來的 Strip，不能只看 `get_line()` 的回傳值。

**(5) 改壞之後測試會不會紅？**

在 `git archive HEAD` 的副本裡，一次改壞一處，跑 `test_tui.py`。表裡不算 benchmark 那個測試，因為它在機器忙時本來就會失敗。

| 改壞的地方 | 結果 |
|---|---|
| `k` 觸發時不還原捲動 | ✓ 1 failed |
| 捲到頂時不還原捲動 | ✓ T6 的測試紅 |
| `g` 改成一段一段 insert | ✓ 只有 benchmark 抓到（4.5 秒） |
| 有焦點時也不打開游標行高亮 | ✓ 2 failed |
| 拿掉 `LanguageDoesNotExist` 的退路 | ✓ 1 failed |
| 換 Session 時游標放在最前面 | ✓ 2 failed |
| 拿掉 `## user` 的顏色 | ✓ 1 failed |
| `_no_header` 改回每一段都套用（W10 退回去） | **✗ 全綠** |
| `settled()` 不檢查 `is_stale()`（W11 退回去） | **✗ 全綠**（`test_preview_staleness_detection` 只測 `is_stale()` 這個函式本身） |
| 拿掉 `_build_highlight_map` 的覆寫 | **✗ 全綠**（見 (3)） |
| 提示改回舊的文字（沒提到 k、g） | **✗ 全綠** |
| 拿掉 `history.clear()` | ✗ 全綠（Low，只影響記憶體） |

- **W10、W11 沒有任何測試守著。** W10 會影響第 3 節的計數不變量，第 3 節本來就要寫那個不變量測試，但建議現在先補兩個簡單的：
  - 30 KB 內沒有切點、而且這一段以 `---` 開頭時，內容不會被吃掉；
  - 檔案改了（大小或 mtime 變了）之後再選到這一列，會重新讀。
- **提示文字：** 「提到 `k`、`g`」是 spec 的 MUST，要加一行斷言。

**(6) 0e75284 有補齊第 1 節清單的 2、3、4、6 嗎？有。**
- **2：** AskText 的測試加了兩個斷言：Tab／shift+tab 之後焦點還在視窗裡的 widget，關掉視窗之後焦點在 `#table`。
  - 在副本裡拿掉「其他視窗時 return」，這個測試現在會紅。
- **3：** 新增 `test_filter_open_mouse_click_preview_esc_keeps_focus_and_filter`。
  - 在副本裡拿掉 Esc 的 `side() == "list"` 判斷，這個測試現在會紅。
- **4：** 新增 `test_ctrl_t_in_filter_toggles_mode_without_changing_filter_text`。
- **6：** 拿掉了 `action_toggle_focus` 這個別名，兩個換頁的 action 合成 `_change_tab(step)`。
- **1、5：** 已在 5e61693 做了：
  - `KEYS` 改成普通的 dict，預覽只有一張表，`paint_keys` 用中括號查；
  - 清單兩張表都加上了 `[ ]` 換頁和 `Tab` 切焦點。

### Low

- `PreviewArea = PreviewText` 這個別名留著，沒有任何地方用到，可以拿掉。
- `PreviewText.BINDINGS = [*TextArea.BINDINGS, ...]` 不需要展開：子類別本來就會繼承父類別的 BINDINGS。
- `_suppress_scroll_load` 現在只有測試在設，等於是在正式程式裡留了一個給測試用的後門。照 S2 改成正式程式自己用，就不是後門了。
- `Preview.step()` 會回傳 `" "`，表示「有讀到東西，但 strip 之後是空的」。這種用一個空格當旗標的寫法，日後容易被誤用。可以改成回傳 `bool`，或者回傳 `(text, read_any)`。

### 給 PM 的清單（請轉告 impl3，要在第 3 節之前修）

1. **S1（High）：** `_build_highlight_map` 跳過超出文件範圍的行，或者整個改用 `get_line()` 處理；補「以 code block 結尾」的測試。
2. **S2：** `put_preview`、`load_all_earlier` 換內容的期間，把 `_suppress_scroll_load` 設起來；補「換 Session 只讀一段」的測試。
3. **S3：** `k`／PgUp 補完之後，游標要放在折行後的上一列；測試用 `wrapped_document` 算出的實際位置來斷言，原本那一行的位置要 `== 1`。
4. **測試：**
   - W10、W11、提示文字各補一個斷言；
   - 讓 `_build_highlight_map` 的覆寫有測試守著（檢查畫出來的樣子，或確認這兩行沒有 `heading` 樣式）；
   - benchmark 的門檻放寬，或者移出 unit。
5. **PM 決定：** user／assistant 用什麼顏色，以及 spec「沿用原本的顏色」的字面要不要改；3 MB 的 `g` 1.55 秒算不算合格（4.2 再量一次）。

### 測試執行

- **完整 unit：** 在 `git archive HEAD`（ba3a9e9，包含 5e61693、0e75284）的乾淨副本裡跑，**單獨跑**、不和別的測試搶 CPU：compileall 通過，**583 passed**。
- **故意改壞的副本：** 是 12 個平行跑的，所以 benchmark 那個測試的失敗不列入判斷。
- 沒有跑整合測試。

---

## 2.4 複查（2158aa8、717c468）

這次複查的是 impl2 的 tasks 2.4（2158aa8）和決定顏色的 717c468，對照上一節「第 2 節程式審查」給 PM 的清單 1～5 與 Low。

驗證方式：
- 都在 `git archive HEAD` 的副本裡做。那時的 HEAD 是 0e67141，已經包含 #25 的 guard。
- 沒有碰工作區。

**結論：清單 1～5 與 Low 都做到了，可以進第 3 節。**
- 只有一個沒有測試守著：PgUp 只往上一列（Low），可以順手補。
- impl2 回報的例外可以接受，理由見下面。

### 逐項

| 項目 | 結果 |
|---|---|
| S1 以 code block 結尾不再掛 | ✓ 上一節的六種最短內文都不再丟例外；用 3 MB 假 session 的閱讀版選到那一列也正常。新增兩個測試，其中一個在 `run_test` 裡實際選到那一列 |
| S2 換 Session 只讀一段 | ✓ 探針：兩個 Session 來回切換，每個都只讀一段（上一節是兩段）。`put_preview` 換內容期間設 `_suppress_scroll_load`，等 refresh 之後再放開 |
| S3 用 `k` 補前一段 | ✓ 用 `wrapped_document` 換算「畫面上的上一列」。3000 字一行時，游標停在 `(7, 2976)`，也就是上一行的**最後一段**，在畫面第 0 列；原本那一行在第 1 列。短行也一樣 |
| S3 用 PgUp 補前一段 | ✓ 往上一頁的列數，游標在畫面內 |
| 測試的斷言 | ✓ 改成 `_screen_row(...) == 1`、游標在畫面第 0 列，不再用 `abs=1`；長行、短行兩種內文都參數化測了 |
| W10、W11、提示文字、`_build_highlight_map` 的覆寫 | ✓ 各有一個新測試，故意改壞都會紅（見下表） |
| benchmark 的門檻 | ✓ 改成 5 秒，docstring 寫明「只抓一段一段 insert 的退化；1.5 秒的目標留給 4.2 人工量」 |
| 顏色 | ✓ `## user` 是 `bold #87afff`，`## assistant` 是 `bold #d787ff`。717c468 把 tasks 2.1 也寫上了；有一個測試會檢查畫出來的樣子，顏色改回 cyan 會紅 |
| Low：拿掉 `PreviewArea` 別名 | ✓ |
| Low：BINDINGS 不再展開父類別的 | ✓（並加了測試：只多 4 個鍵，其他是繼承的） |
| Low：`step()` 回傳的空格旗標 | ✓ 改成回傳 `None`，並加了測試 |
| Low：`_suppress_scroll_load` 只給測試用 | ✓ 現在是正式程式在用（類別屬性加上註解）；測試 helper 還在設它，用來把畫面擺到頂端而不觸發載入，這樣用是合理的 |

### 故意改壞（`test_tui.py`，一次 6 個平行跑）

| 改壞的地方 | 結果 |
|---|---|
| S1 拿掉行號範圍的檢查 | ✓ 2 failed（兩個 code block 結尾的測試） |
| S2 `put_preview` 不設 suppress | ✓ 換 Session 的測試紅 |
| S2 放開 suppress 的那一行拿掉（永遠不放開） | ✓ T6 的「捲到頂會補」測試紅 |
| S3 游標放回上一行的第一段 | ✓ 2 failed（長行的 `k` 與 PgUp） |
| W10 `_no_header` 改回每一段都套用 | ✓ 1 failed |
| W11 不檢查檔案有沒有改過 | ✓ 1 failed |
| 提示文字改回舊的 | ✓ 1 failed |
| 拿掉 `## user`／`## assistant` 樣式的覆寫 | ✓ 1 failed（檢查畫出來的樣子的那一個） |
| 顏色改回 cyan | ✓ 2 failed |
| PgUp 只往上一列 | **✗ 全綠**。單獨跑 PgUp 的測試也是綠的 |
| `load_all_earlier`（`g`）不設 suppress | ✗ 全綠（impl2 自己回報的例外，見下） |

- **PgUp（Low）：**
  - 測試只斷言「游標在畫面內、而且行號 > 0」，往上一列也滿足這兩個條件；
  - 建議加一個斷言：原本那一行在畫面上往下移了大約一頁，例如 `_screen_row(pane, (added, 0)) >= pane.content_size.height - 1`。
- **與這次無關的旁證：**
  - 平行跑的時候，`test_the_bar_really_reaches_n_n_before_the_window_goes` 在其中兩個副本失敗過；
  - 在沒改壞的 HEAD 上單獨跑 5 次都通過。這個測試來自較早的 commit（4581aa4），是機器忙時才會失敗的計時測試，不是這次改出來的；
  - 只是提醒：以後平行跑 mutation 時，它的失敗不要算進去。

### impl2 回報的例外：可以接受

- `load_all_earlier` 是先 `while preview.more(): preview.step()`，全部讀完才 `load_text`。
- 所以 `load_text` 把捲動位置歸零、觸發 `watch_scroll_y → load_earlier` 時，`load_earlier` 第一行就是 `if preview is None or not preview.more(): return`，什麼都不會做：不讀檔、不插入、也不會走到 `is_stale()` 的重建。
- 那裡的 suppress 只是保險，拿掉之後行為完全一樣，所以測試不可能分得出來。這是「等價的改壞」，不是測試漏掉。
- **建議：** 保留它並在旁邊加一句註解（「`more()` 已經是假，這裡只是保險」），或者乾脆拿掉讓程式更短。兩種都可以，不擋合併。

### `g` 的時間（再量一次）

- 用 3.17 MB 真實格式的內容（impl1 那份假 session 的閱讀版接長），第一次按 `g` 要 **1.66 秒**；上一節量的是 1.55 秒，差別在誤差範圍內。全部載入之後再按 `g` 約 0.1 秒。
- 只用 2.11 MB 的閱讀版時，上一節量的是 1.02 秒。
- 還是在 1.5 秒的邊緣，維持上一節的建議：由 PM 在 4.2 實際按一次決定。

### 探針與隔離（對照新規則「測試檔只能放在 repo 的 tests/」與 #25）

- 我的探針都放在 `git archive HEAD` 解開的**副本**裡的 `tests/unit/`，在副本的根目錄跑 pytest。所以會載入副本裡的 repo conftest 與 `_guard`；HEAD 已經包含 #25 的 guard，探針也都通過了它的檢查。
- 我用一個探針印出環境變數確認過：`HOME`、`AGORA_CONFIG`、`AGORA_CACHE_DIR`、`AGORA_STATE_DIR` 都在 pytest 的 `tmp_path` 底下。
- 探針只額外讀了 `/tmp/agora-trial/home` 裡 impl1 那份假 jsonl（透過 `AGORA_CLAUDE_HOME`），沒有在 repo 外、也沒有在沒有 conftest 的地方跑 pytest。
- **給 #25 的觀察（不是這張單的問題）：**
  - conftest 沒有設 `AGORA_RCLONE` 和 `AGORA_FOLDER_NAME`，印出來是 `None`；
  - 目前 unit 測試不會呼叫 rclone，所以沒有影響；
  - 但隔離要不要也把這兩個設到假的值，PM 可以順便判斷。

### 測試執行

- **完整 unit：** 在 `git archive HEAD`（0e67141）的乾淨副本裡單獨跑：compileall 通過，**593 passed**。
- 沒有跑整合測試。

---

## 第 3 節程式審查（9d49bca、ac7ea27）

審的是 impl2 的第 3 節「預覽區搜尋」（9d49bca），以及 2.4 複查的 Low：PgUp 的斷言（ac7ea27）。

對照的是：
- spec「預覽區搜尋」，含 R2（Enter 往下最近，沒有就往上最近，不繞回）與 R5（生命週期）；
- design「搜尋：計數，不做位移換算」「搜尋框與狀態」；
- tasks 3.1～3.3；
- W1、W5、W6，以及第 2 節審查 (4)；
- impl2 的回報 `/tmp/agora-handover/impl2-section3-report.md`。

驗證方式：
- 都在 `git archive HEAD` 的副本裡做；
- 探針放在副本的 `tests/unit/` 底下跑，會經過 repo 的 conftest 與 `_guard`；
- 沒有碰工作區：impl4 的 `README.md`、`docs/design.md`、`docs/acceptance.md` 還沒 commit。

**結論：計數的做法正確，不變量、R2、標亮、搜尋框的鍵都對，測試也守得住。**

有 2 個 Medium 建議在 4.2 之前修：
- 自動重搜會自己把整份檔案讀進來；
- 3 MB 真實內容上的速度，以及每按一次 `n` 都重讀整個檔案。

另外有 1 個 Low～Medium（整份讀取失敗時，最後一則從畫面上消失），以及幾個 Low。

### PM 指定的六點

**(1) 不變量 B＋已載入＝N：✓**
- `Preview.counts()` 讀整個檔案，從 `_header_end()` 開始算（和 `_no_header` 是同一份實作），算出：
  - 總數 N；
  - `at` 之前的個數 B（`at` 是位元組，先把 `raw[:at]` decode 再換算成字元）。
- 兩邊都用 `find_in_lines()`：在 decode 後的字串上逐行 `re.finditer(..., re.IGNORECASE)`，沒有任何位移從檔案帶到畫面。
- `test_the_count_agrees_with_what_is_on_screen_at_every_step` 每讀一段就驗一次，內文涵蓋：
  - 切在 `\n\n`、切在 `\n## `；
  - 中文；
  - `Straße` 與 `STRASSE` 並存；
  - 檔頭裡也有那個字。
- 故意改壞驗證：
  - 「檔頭也算進去」：3 個測試紅；
  - 「把 `at` 的位元組直接當字元用」：2 個測試紅（包括不變量）。
- **Low：** 這個測試的 `words` 裡有一個字串是 `"沒有���字"`，原始碼裡真的有 U+FFFD，應該是 `"沒有這個字"`。不影響正確性（後面的斷言用的是正確的字），但迴圈裡那一個並沒有測到想測的東西，請改回來。

**(2) 跳到還沒載入的符合：是一次載入 ✓，但真實內容上要 2.6 秒（Medium，T2）**
- **做法對：** `_load_to()` 只對 `Preview` 連續 `step()`（每段扣掉自己的符合個數），最後用 `show_read_text()` 一次 `load_text`。沒讀到東西時不重新載入（impl2 第 13 點）。
- **實測（120×30）：** 用 impl1 那份假 session 的閱讀版，接長到 3.17 MB、124,050 行，最前面放一個獨有的字：

  | 量測 | 時間 |
  |---|---|
  | Enter 搜這個字並跳過去 | **2.59 秒**（目標約 1.5） |
  | 同一份內容按 `g`（第 2.4 節量的） | 1.66 秒 |
  | 其中 `counts()` 一次 | 0.25 秒 |
  | 其中 108 段 `step()` 加上 `count_in` | 0.12 秒 |
  | 全部載入之後，再按一次 `n` | **0.47 秒** |
  | 搜一個找不到的字，再按 `n` | 0.31 秒 |

- **為什麼 impl2 量的是 0.83 秒：** 它用的是產生出來的內文，每行 3000 字、行數很少，比真實內容快很多。真實的 Session 行數多、行短，`load_text` 和逐行處理都慢得多。
- **為什麼慢：**
  - Enter 比 `g` 多的約 0.9 秒，大致是：`counts()` 讀整個檔案並 decode、跑 regex；載入前後各對整份文件做一次 `find_in_lines()`（12 萬行）；最後一次 `load_text`。
  - `n`／`N` 每按一次就重新呼叫 `counts()`，又把 3 MB 讀一次、decode 一次、找一次；接著再對已載入的文件找一次。所以每按一下要 0.3～0.5 秒。
- **建議：**
  - 在 `Preview` 上快取 `(query, 檔案大小, mtime) → (N, 檔頭結束的位置)`；
  - B 不用每次重算：`_load_to` 已經會在讀每一段時扣掉該段的個數，照這個方式往下維護即可；
  - 已載入文件的符合清單，用 `(query, 文件版本)` 快取。
  - 這樣 Enter 應該接近 `g` 的時間，`n` 會降到幾十毫秒。
- **順帶確認 impl2 的發現：** `_big_body(3)` 是用字元算重複次數，實際約 9.4 MB。所以我在第 2 節審查寫的「`_big_body` 3 MB 按 `g` 0.51 秒」，其實是 9.4 MB、但只有很少的長行。真實格式的數字（1.66 秒），以及「測試的內文比真實內容樂觀」這個結論都不變。

**(3) 標亮在標題、code block、游標行裡都看得到：✓**
- `MATCH_STYLE = "bold underline"`。不用顏色，是因為語法上色會蓋掉前景色、游標行會蓋掉底色。
- 探針檢查畫出來的 Strip（`render_line`），以下三處的「表格」都帶有 underline 和 bold：
  - `# 表格的標題`（標題）；
  - code block 裡的 `x = '程式裡的表格'`；
  - 游標所在、而且有兩個符合的那一行（兩個都有）。
- 測試也是檢查 Strip，不只看 `get_line()`：一般的行和游標那一行都有斷言。把樣式改成底色，有 2 個測試會紅。
- 標題和 code block 的情況目前沒有測試，可以把我探針的內文加進去（Low）。

**(4) 生命週期**
- **換 Session 清掉：✓**
  - `track_row()` 在 `moved()` 與 `show()` 都會呼叫，換分頁、篩選後換列也算；
  - 改成不清，測試會紅。
- **換成整份或重讀時重搜並提示：✓**
  - 提示文字分「已換成整份對話」與「內容已更新」，沒有搜尋時不提示；
  - 拿掉重搜，有 2 個測試紅。
- **讀取失敗不卡在「載入中」：✓**
  - `load_full` 包了 try/except，`import_preview` 失敗時的提示也分得出是哪一種失敗；
  - 拿掉 try/except，測試會紅。
- **未匯入頁的最後一則不進 cache，搜尋時退回「最後放進畫面的那份預覽」：合理。**
  - 搜的正是畫面上那一份，計數和標亮一定一致；
  - 唯一的時間差：在清單換列後 150 ms 內就切到預覽區按 `/`，搜的是舊列的內容，但那時畫面上也還是舊列的內容，兩者仍然一致；
  - 等新列的內容放上來，`put_preview` 會重搜。可以接受。

**T1（Medium）：自動重搜會自己把整份檔案讀進來**
- **實測：**
  - 3 MB 內文，先搜一個只在最前面的字，跳過去之後 `at == 0`（全部讀了）；
  - 接著 `app.reload()`（就是動作完成後會做的事），**沒有按任何鍵**，重讀之後的預覽又被讀到 `at == 0`。
- **原因：**
  - 重搜時呼叫的是 `run_preview_search(query, note=note)`，`step=0`，等於使用者自己按了 Enter；
  - 新的預覽只有尾端一段，最近的符合在還沒讀的部分，於是 `_load_to` 把整份讀進來。在 3 MB 上，這是使用者沒有要求的、約 2.6 秒的卡頓。
- **未匯入頁也一樣：** 「最後一則換成整份」時，如果符合只在前面，也會自己讀到底。
- **和 spec 不一致：**
  - spec R5 寫的是「用同一個字重新搜；游標回到最後一行」；
  - Goals 寫的是「只有 `g` 與跳到還沒載入的搜尋符合（使用者要求的）才一次載入」；
  - 現在的行為是游標被拉到最近的符合，必要時讀整份。
- **建議：** 自動重搜只做「重新計數、標亮已載入的符合」，游標留在最後一行，計數顯示「共 N 個」（`which = 0`，`paint_count` 已經支援這種顯示），要等使用者按 `n`／`N` 才跳。
  - 寫法可以是 `run_preview_search(query, note=note, jump=False)`。
  - 測試：在 3 MB、符合只在前面的內文上 `reload()` 之後，`at` 不會變成 0。

**T3（Low～Medium）：整份讀取失敗時，原本顯示的最後一則消失**
- **實測：** 未匯入頁的 agent `export` 會丟例外時，pinned 正確顯示「讀不到整份對話」，但預覽區的內容變成空字串。
  - 原因：失敗時的 `Preview(None, "讀不到整份對話")` 沒有內容，它會取代畫面上原本的最後一則，也會被放進 cache。
- **影響：** 使用者剛才看得到的那一則不見了，用 `/` 搜也只會得到「找不到」；cache 裡留著失敗的那一份，要等 `reload()` 才會再試。
- **改之前就是這樣：** 原本失敗時說的是「讀不到這個 session」，內容也是空的，不是這次改出來的，但這次的規格正好在管這一段。
- **建議：** 失敗時保留最後一則的內容，只把 pinned 換成「讀不到整份對話」；而且不要放進 cache，下次選到這一列時再試一次。

**(5) 搜尋框裡的鍵：✓**
- 框裡打 `n N j k q /` 都是字（`Input` 會先吃可印字元），測試有涵蓋；
- Tab 切出去時會丟掉沒送出的字，搜尋框也收起來（`toggle_focus` 裡的 `hide_preview_search()`）；把這段拿掉，測試會紅；
- 清單那一邊按 `n`／`N` 沒有作用。

**(6) 改壞之後測試會不會紅？**

在 `git archive HEAD` 的副本裡，一次改 12 處，跑 `test_tui.py`，分兩批、每批 6 個平行跑：

| 改壞的地方 | 結果 |
|---|---|
| 檔頭也算進去 | ✓ 3 failed |
| `at` 的位元組直接當字元用 | ✓ 2 failed |
| 第一次搜尋繞回檔案最前面（R2 退回去） | ✓ 1 failed |
| 第一次搜尋不往上找 | ✗ 全綠，但這是**等價**的改壞：往下沒有符合時，備援路徑的 `target = passed` 剛好落在往上最近的那一個 |
| 符合改變時不清 `_line_cache` | ✗ 全綠，和 impl2 的說法一致。我另外試了「先找到、再找一個不存在的字」，也沒有殘留的底線：搜尋框收起或打開會改變預覽區的大小，而大小也在快取的 key 裡。所以這一行目前是保險，保留即可 |
| 標亮改成底色 | ✓ 2 failed |
| 換列時不清掉搜尋 | ✓ 1 failed |
| Tab 切出去時不收起搜尋框 | ✓ 1 failed |
| 預覽區的 Esc 不清標亮 | ✓ 1 failed |
| 內容被換掉時不重搜 | ✓ 2 failed |
| `load_full` 拿掉 try/except | ✓ 1 failed |
| 計數不加「（只有最後一則）」 | ✓ 1 failed |

- impl2 自己做了 22 處改壞，有 3 處不會紅（每一步重算 B、不清 `_line_cache`、`/` 也開清單的篩選），它給的理由我都同意。
- 「每一步重算 B」：如果照 T2 加上快取，它就不再是可能的退化，這一條自然消失。
- `test_the_bar_really_reaches_n_n_before_the_window_goes` 在平行跑時又失敗了一次，和這次的改動無關（2.4 複查時已記錄過）。

### ac7ea27（PgUp 的斷言）：✓

- 新增的斷言是「原本那一行大約往下移了一頁」。
- 在副本裡把 PgUp 改成只往上一列，現在會紅：`a page up pushes it down about a page (1)`。
- impl2 也驗了「用檔案的行數而不是折行後的列數」與「不還原捲動」兩種改壞，都會紅。
- 2.4 複查的 Low 已經補上。

### Low

- **測試原始碼有一個 U+FFFD：** 見 (1)，請改回 `"沒有這個字"`。
- **標題和 code block 裡的標亮沒有測試：** 見 (3)。
- **「找不到」時，上一個搜尋的選取還留在畫面上：** 游標不動符合 spec，但舊的「目前那一個」底色還在，看起來像是還選著。可以在找不到時把 selection 收成游標。
- **impl2 判斷的 13 點我都同意**，其中第 2 點（只有在內容物件換掉時才重搜）的前提，是 T1 改成「只計數、不跳」。

### 給 PM 的清單（請轉告 impl2）

1. **T1：** 自動重搜（`put_preview` 之後的 settle）只計數、只標亮，不跳也不載入；游標留在最後一行，計數顯示「共 N 個」。補「`reload()` 之後 `at` 不變」的測試。spec 的 R5 字面已經是這個意思，不用改。
2. **T2：** 快取 N（以 query 加上檔案的大小、mtime 為鍵）和已載入的符合清單，B 往下維護，不要每次重算。目標是 Enter 接近 `g` 的時間、`n` 在 0.1 秒內。4.2 用真實格式的 3 MB 再量一次。
3. **T3：** 整份讀取失敗時保留最後一則的內容，只換 pinned，而且不放進 cache。
4. **（Low）** 修掉 U+FFFD；補標題和 code block 的標亮測試；找不到時把 selection 收成游標。

### 測試執行

- 完整 unit 在 `git archive HEAD` 的乾淨副本裡跑：那時 HEAD 已經是 ac7ea27。compileall 通過，**611 passed**。
- 探針和改壞都在副本裡；探針另外只讀了 `/tmp/agora-trial` 那份假 jsonl。
- 沒有跑整合測試。

---

## 4.1 文件審查（46a287c）

審的是 impl4 的 4.1 文件：README、`docs/design.md` 5.9、`docs/acceptance.md` 互動模式的那幾段。

對照的是：
- spec 的三個 Requirement；
- design 的決定（含 R1～R7）；
- HEAD 的程式：`KEYS` 表、`PreviewText`、搜尋；
- 第 3 節審查的 T1，也就是「自動重搜只計數、不跳、游標留在最後一行」。impl2 正在照這個改，文件要寫成改完之後的行為。

這次只看文件。驗收步驟能不能照做，我是用讀程式來判斷的，例如 `action_filter`、pull 的 Confirm、`grow.py` 的內容。

**結論：README 與 design 5.9 的鍵和 spec、程式大致一致，BREAKING 也標得夠明顯。**

但 acceptance 有三處照著做會做不下去，另外 design 和 acceptance 還是寫「重搜」，要改成 T1 的行為：

| 等級 | 問題 |
|---|---|
| Medium | A1 搜尋的步驟照做會做不下去 |
| Medium | A2 第 12 節後半的步驟互相打架 |
| Medium | A3 16.34 按 Enter 會取消，而且 pull 有可能動到測試資料 |
| Medium | A4 文件還沒照 T1 改 |
| Low～Medium | A5 漏掉的 Scenario 與 R7 |
| Low | 其他 |

### (1) 和 spec、程式不一致、或寫錯的地方

**A4（Medium）：重搜的行為要照 T1 改**
- design 5.9「預覽區搜尋」寫的是：「同一個 Session 的預覽被換掉……則用同一個字重新搜，並提示……」。
- acceptance 16.34 寫的是：「預覽用同一個字重新搜（標亮、計數都還在）」。
- 照 T1 改完之後，行為應該是：
  - 只重新計數、只標亮已載入的符合；
  - **游標留在最後一行，不跳，也不載入前面的內容**；
  - 計數顯示「**共 N 個**」（不是「第 k 個／共 N 個」），後面加「已換成整份對話」或「內容已更新」；
  - 要等按 `n`／`N` 才跳。
- 這兩處都要照這個寫。README 沒有寫到重搜，不用改。

**鍵與按鍵列：一致 ✓**
- README 和 design 列的兩張鍵表，和 `KEYS["list"]`、`KEYS["preview"]`、`KEYS["search"]` 以及 `check_action` 一致：
  - `[ ]` 換頁只在清單；
  - Enter 只在清單本身；篩選框裡的 Enter 只收起篩選框；
  - 預覽區的 Esc 有兩段，而且不動清單的篩選；
  - 從篩選框按 Tab 等同 Enter；從搜尋框按 Tab 會丟掉沒送出的字。
- acceptance 12.2、12.4 列的按鍵列內容，和 `KEYS` 一字不差。
- 顏色 `#87afff`／`#d787ff`、提示文字「↑ 往上捲、按 k 或 g 載入更早的內容」，都和程式一致。

**Low：Tab／shift+tab 那一列的寫法容易誤會**
- README 和 design 的兩張表，把 Tab／shift+tab 寫成「切到預覽區／切回清單」（清單那張）和「切回清單／切到預覽區」（預覽那張）。
- 讀起來像是兩個鍵方向不同。但程式裡兩個鍵都是「切到另一邊」，只有在確認視窗裡才分往下、往上。
- 建議寫成「`Tab`、`shift+tab`：切到另一邊（預覽區／清單）」。

**Low：design 5.9 最後那段的 spec 連結**
- 現在連到 `openspec/changes/tui-batch-actions/...` 和 `openspec/changes/preview-search-keys/...`。
- 前者已經歸檔到 `openspec/changes/archive/2026-10-03-tui-batch-actions/`，這個連結原本就是壞的；後者在這個 change 歸檔之後也會失效。
- 建議都改成指向主 spec `openspec/specs/interactive-mode/spec.md`。

### (2) BREAKING 老使用者一眼看得懂嗎：看得懂 ✓

- README 在互動模式那段的第二句，就用 ⚠️ 寫了「`Tab` 不再換頁：換頁改成 `[`、`]`……」。
- acceptance 的開頭說明和第 12 節的最上面，也各有一句 ⚠️。
- design 5.9 在「兩個分頁」那一條寫明「這是 breaking：以前 `Tab` 換頁、`shift+tab` 切焦點」。
- 程式本身也有提示：清單的按鍵列第一、二格就是 `[ ] 換頁`、`Tab 切焦點`；Agora 頁是空的時候，提示寫「按 ] 到未匯入」。
- **建議（Low）：** README 的 ⚠️ 那句後面加半句「以前的 `shift+tab` 切焦點照舊，`Tab` 也一樣是切焦點」。習慣用 shift+tab 的人就知道，他們原本的按法不受影響。

### (3) acceptance 能不能照著做、有沒有漏

**A1（Medium）：16.24～16.26 用 `表格` 搜 T1，照做會做不下去**
- 16.3 的 `grow.py` 在**每一則**裡都寫了 `"表格" * 150`。2000 KB 的 T1 大約有一千多則，每一則一兩百個符合，`表格` 的總數在**十幾萬個**以上。
- 所以：
  - 16.24 的「第 k 個／共 N 個」裡 N 非常大，也看不出「離尾端最近」是哪一個（游標所在那一行就有一百五十個）；
  - 16.25 的 `n`、`N` 只會在同一行裡一格一格移動；
  - 16.26「一路按 `N`，直到『第 1 個』」要按十幾萬次，做不到。
- **建議：**
  - 讓 `grow.py` 在**第一則**（最早的那一則）多寫一個獨有的字，例如 `驗收的開頭字`；
  - 16.26 改成「`/` 搜 `驗收的開頭字`，`Enter`」：應該跳到最前面（一次載入、提示消失），計數是「第 1 個／共 1 個」；
  - 16.24、16.25 改搜一個每則只出現一次的字，例如 `的回覆`，再把 `"表格" * 150` 改掉，不然計數一樣會很大。
- 這一步順便量時間，見 A5。

**A2（Medium）：第 12 節後半（12.12～12.15）的步驟會互相打架**
- `action_filter` 打開篩選框時**不會清掉舊的字**。12.11 按 Enter 之後，篩選框裡還留著那個字。
- 所以 12.12 打開篩選框再打 `q`，篩選字會變成「原本的字 + q」，而且篩選是邊打邊生效的，清單多半就空了。結果：
  - 12.13 的「清單仍然照篩選框裡的字篩選（『另有 1 個勾選被篩選掉』還在）」不成立：篩選字已經變了，被篩選掉的勾選會變成 2 個；
  - 12.14 按 Esc 之後篩選清掉，但 12.11 勾的兩列**還勾著**；
  - 12.15「勾一列，`d` → 確認視窗只列出看得到的那一個」也不成立：沒有篩選了，會列出勾著的兩到三個。後面那句「`/` → 清空 → `Enter` 取消篩選」也已經沒有篩選可以取消。
- **建議的順序：**
  1. 12.11 篩選；
  2. 原本的 12.15（`d` 只列出看得到的那一個，`Enter` 取消）；
  3. 12.13（`/` 回到篩選框，按 `Tab`：篩選框收起、篩選還在）；
  4. 12.14（`Tab` 回清單，`Esc` 清掉篩選）；
  5. 12.12 放到最後，另外寫：「`/`，打 `q`，篩選框裡出現 `q`，沒有離開；`Esc`」。
- **另外：** 12.12 的「打开」是簡體字，請改成「打開」。

**A3（Medium）：16.34 按 Enter 會取消，而且按「確定」可能動到測試資料**
- pull 的確認視窗選項是 `["取消", "確定"]`，焦點預設在「取消」。所以「確認視窗直接 `Enter`」就是取消，不會執行，也就不會重讀，看不到「內容已更新」。
- 而如果改成選「確定」，對 T1 pull 就是拿 Drive 上那份小的去比對被 16.3 加長的本機鏡像。「T1 本身是『已經是新的』，本機那份不會被覆蓋」這個說法沒有被驗證過，有可能把加長的測試資料蓋掉。
- **建議：**
  - 勾 Q（12.8 匯入的那一列，不是 T1），游標放回 T1；
  - 按 `p`，在確認視窗**移到「確定」再按 Enter**；
  - 動作只會作用在 Q，完成之後 `reload()` 會重讀 T1 的預覽，就看得到「共 N 個  內容已更新」（照 A4 的 T1 行為），游標在最後一行。

**A5（Low～Medium）：漏掉的 Scenario 與 R7**
- **spec「確認視窗裡的 Tab」：** 沒有對應的步驟。可以接在 12.15 的確認視窗裡：按 `Tab`，焦點移到勾選框；按 `shift+tab`，回到選項；底下的頁面不變。
- **`shift+tab`：** 整份 acceptance 都只按 `Tab`，沒有按過 `shift+tab`。R7 要的是在真實的終端機裡（kitty 協定關掉的預設狀態）實際按過 Tab、shift+tab、`[`、`]`、`G`、`N`。
  - 建議在 12.4 後面加一步「`shift+tab` 回清單」；
  - 第 12 節開頭寫明「不要設 `TEXTUAL_DISABLE_KITTY_KEY`，用預設」。
- **R5 的「只有最後一則」與「已換成整份對話」：** 未匯入頁這兩種狀態閃得太快，人工很難抓到，單元測試已經有涵蓋，acceptance 可以不做。但建議在 16.13 註明：「整份換上來之後如果有搜尋，計數後面會出現『已換成整份對話』」。
- **在已載入內容的頂端按 PgUp 會補前一段：** 16.19 只寫了「PgUp 翻一頁」，沒有寫在頂端也會補，可以併進 16.20。
- **計時：**
  - 16.22 寫「2000 KB 應該是瞬間的」，期望太樂觀。我在 2.11 MB 的真實格式內容上量到 `g` 約 1.0 秒；`grow.py` 的內容行比較長，可能會快一點，但不會是「瞬間」。建議寫「約 1 秒以內」。
  - 結果表只記了 `g` 的秒數，搜尋只記「順／卡」。第 3 節審查 T2 的目標是「跳到最前面的符合 ≈ `g` 的時間」與「`n` 在 0.1 秒內」，建議把 A1 改過的「搜開頭字」也記秒數，`n` 記順／卡。
  - 目標是「3 MB ≤ 1.5 秒」，但驗收用的是 2000 KB，量出來的數字只能對照。建議讓 16.3 改成長 3000 KB，或者另外生一個 3000 KB 的 T4 專門計時。PM 決定即可。

**其他步驟：** 12.1～12.11、15.2～15.3、16.13、16.17～16.23、16.27～16.33 都照著程式的行為寫，能照做 ✓。
- 例如 16.29 用檔頭裡的 id 搜，結果是「找不到」，這和程式從 `_header_end()` 開始計數一致。
- 16.31、16.32 的搜尋框行為，和 `hide_preview_search()` 一致。

### 給 PM 的清單（請轉告 impl4）

1. **A4：** design 5.9 與 acceptance 16.34 照 T1 寫成「只計數、標亮，游標留在最後一行，計數是『共 N 個』加提示；按 `n`／`N` 才跳」。等 impl2 的 T1 commit 之後再對一次字。
2. **A1：** `grow.py` 的第一則加一個獨有的字，拿掉 `"表格" * 150`；16.24～16.26 改用獨有的字和每則只出現一次的字。
3. **A2：** 第 12 節後半照上面的順序重排；把簡體的「打开」改成「打開」。
4. **A3：** 16.34 改成勾 Q、游標在 T1、在確認視窗移到「確定」再 Enter。
5. **A5：** 補確認視窗的 Tab 步驟和一步 `shift+tab`；寫明用 kitty 關掉的預設狀態；PgUp 在頂端也會補；16.22 改成「約 1 秒以內」；搜尋的計時也記秒數；用 2000 KB 還是 3000 KB 計時由 PM 決定。
6. **（Low）** Tab／shift+tab 那一列改成「切到另一邊」；design 的 spec 連結改指主 spec；README 的 ⚠️ 補半句 shift+tab 的說明。

### 測試

- 這次只有文件改動：ac7ea27 到 46a287c 之間，`src`、`tests` 都沒變。所以沿用第 3 節審查在 ac7ea27 上的完整 unit 結果（**611 passed**），沒有重跑。
- 沒有跑整合測試，也沒有碰工作區：impl2 的 `tui.py`、`test_tui.py` 還沒 commit。
