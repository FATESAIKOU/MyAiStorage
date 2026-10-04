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
