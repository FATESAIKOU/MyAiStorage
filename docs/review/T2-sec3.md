**沒有 High。** 3.1 的雲端欄是對的：它讀的是 sync 留在索引裡的標記和 outbox，列表本身不會去問 Drive；「未上傳」也是獨立的一種狀態，不會被當成 ✗。只有幾個 Low（R1～R4）。

# Review：change tui-batch-actions 第 3 節

## 3.1 雲端欄（`416647a`）

2026-10-03，review。對照 `specs/interactive-mode/spec.md` 的「雲端欄與 pull／push 的選項」，以及 `docs/review/T2.md` V6 的「雲端」欄那一列。只提意見，沒有改程式。

在 `git archive 416647a` 取出的副本跑 `test_tui.py`：**48 passed**。沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。

| spec | 實作 | 測試 | 結果 |
|---|---|---|---|
| Agora 頁有「雲端」欄 | `COLUMNS["agora"]` 加了「雲端」 | 斷言了欄位名稱 | ✅ |
| 雲端有 → ✓，沒有 → ✗，還沒上傳 → 「未上傳」 | `agora_rows`：在 outbox → 未上傳；否則 `index.cloud_has()` → ✓／✗ | `test_the_cloud_column_says_where_each_session_is`（三種都有，而且也查了表格裡實際畫出來的那一格） | ✅ |
| 「未上傳」優先於 ✗ | 先判斷是不是在 outbox | 同上 | ✅（和 session-sync 的「還在 outbox 的不算雲端沒有」一致） |
| 列表不去問 Drive（T6） | 只讀索引和 outbox | — | ✅ |
| Scenario「被另一台機器刪掉，這台同步之後 → ✗」 | 標記由 sync 寫入；TUI 啟動時做一次節流的同步；import、delete 這些子程序也會同步，做完之後 `reload` 會讀到新的標記 | 測試是直接呼叫 `mark_missing`，**沒有**走過「Drive 上刪掉 → 同步 → 重讀」 | ✅\*（R2） |

### 其他

| # | 嚴重度 | 問題 | 建議 |
|---|---|---|---|
| R1 | Low | `agora_rows(index, filters, paths=None)`：沒有傳 `paths` 的時候，`staged` 就是空的集合，outbox 裡的 Session 會**默默地**顯示成 ✓（因為它們沒有標記）。現在唯一的呼叫者（`reload`）有傳，但測試 `tui.agora_rows(index, [])` 沒有傳，以後的呼叫者也很容易漏掉 | 把 `paths` 改成必填 |
| R2 | Low | spec 的 Scenario 是「同步之後」顯示 ✗，但測試是直接寫標記。互動模式裡已經沒有手動同步的鍵了（`r`、`s` 都拿掉了），啟動時的同步又是節流的（5 分鐘內同步過就跳過），所以「別台剛刪掉、這台馬上打開互動模式」的時候，✗ 可能要等到下一次 import／delete 或重新啟動才會出現 | 補一個走完整條路的測試：fake remote 上 `rmtree` → `store.sync` → `reload` → ✗。另外在 design 5.9 寫明「雲端欄反映的是上一次完整同步的結果」（V6 的建議） |
| R3 | Low（給 3.2） | push `--not-exist-upload` 成功之後，標記要等**下一次完整同步**才會清掉（T1-sec3 L5），所以 3.2 做完之後，`P` 加上「雲端沒有的就傳回去」，那一列還是會顯示 ✗，一直到下次同步 | 3.2 的時候一起處理：push 成功的那些 id，直接從 `cloud_missing` 拿掉；或者讓 TUI 在 push 之後做一次不節流的同步 |
| R4 | Low | 每一次 `reload` 都會呼叫 `outbox_ulids()`，而它有一個副作用：會把 `.old-*` 還原回去。有 `.tmp-*` 在的時候它會跳過（stage 正在進行中），所以和同時在跑的子程序不會衝突。只是要知道：互動模式現在也會碰到 outbox 的這個復原邏輯了 | 不用改；在註解裡記一句就好 |

## 3.2 pull／push 的確認視窗與選項（`d468ac0`）

2026-10-03，review。對照 spec「雲端欄與 pull／push 的選項」的後半段。在 `git archive d468ac0` 取出的副本跑 `test_tui.py`：**51 passed**。探測測試只在副本裡做。規則同上。

**沒有 High。** 有一個 Medium（S1）：那個選項**用鍵盤勾不到**。

| spec | 實作 | 結果 |
|---|---|---|
| pull 的確認視窗有「雲端沒有的就刪掉本機的」，預設不勾，勾了等同 `--not-exist-delete` | 新的 `Confirm`：`OptionList`（預設停在「取消」）加上一個 `Checkbox(value=False)` | ✅ 邏輯對 |
| push 的確認視窗有「雲端沒有的就傳回去」，預設不勾，勾了等同 `--not-exist-upload` | 同上 | ✅ 邏輯對；文字少了一個字（S3） |
| 取消就什麼都不做 | Esc → `(None, False)` | ✅（`test_cancelling_pull_runs_nothing`） |
| 不勾就不帶 flag | | ✅（`test_pull_without_the_option_sends_no_flag`，測的其實是 push） |

### S1（Medium）：勾選框只能用滑鼠勾，鍵盤到不了

實測（探測測試）：按 `p` 打開確認視窗之後，焦點在 `OptionList` 上。

| 按鍵 | 結果 |
|---|---|
| Tab | 焦點**沒有**移到勾選框；App 的 `tab`（`priority=True`）先接走了，**底下的清單換成了未匯入頁**（`app.tab` 從 `agora` 變成 `import`），確認視窗還開著 |
| shift+tab | 同樣被 App 的 `toggle_focus` 接走，焦點還在 `OptionList` |
| ↓ ↓ ↓ | 只在「取消／確定」之間移動 |
| 空白 | `picked` 還是 False |
| 用滑鼠點勾選框 | `picked` 變成 True |

所以只用鍵盤的使用者（互動模式本來就是鍵盤操作）**沒有辦法**使用這兩個選項；而這兩個選項就是 spec 這一條的重點。測試 `test_pull_and_push_ask_before_doing_it_and_offer_the_flag` 是用 `window.query_one("#extra").focus()` 直接在程式裡設定焦點，所以抓不到這個問題。

另外，「確認視窗開著的時候按 Tab，會切換底下的頁面」，這在其他的視窗（delete 的確認、Choose、AskText）也一樣會發生。這是之前就有的問題：雖然這次要動作的列在開視窗之前就決定了，不會送錯，但動作做完之後，畫面會停在另一頁。

**修法**（二選一）：
- **(a)** 不要用勾選框，把選項做成 `OptionList` 的第三個選項，例如「確定，並刪掉雲端沒有的本機副本」。這樣用 ↑↓ 就選得到，預設還是停在「取消」，也比較不會誤按。
- **(b)** 保留勾選框，讓 `check_action` 在畫面上有 `ModalScreen` 時，把 `next_tab`／`toggle_focus` 這些 App 層級的鍵都停用，並且讓 `Confirm` 自己綁 Tab → `focus_next`。

不管選哪一個，測試都要改成**只用按鍵**操作（不要直接呼叫 `.focus()`）。

### 其他

| # | 嚴重度 | 問題 | 建議 |
|---|---|---|---|
| S2 | Low | 在未匯入頁按 `p`，也會出現「雲端沒有的就刪掉本機的」。可是那一頁的列是 agent 的 session，這個 flag 對它們的意思是「agent 那邊已經沒有了，就刪掉全文快取」。而且那一頁的列都是從 agent 自己的清單來的，所以幾乎一定還在，勾了也不會有任何效果，說明的文字也不對 | 未匯入頁不要顯示這個選項，或者把文字改成「agent 那邊已經沒有的，就刪掉全文快取」 |
| S3 | Low | push 的選項文字是「雲端**沒的**就傳回去」，少了「有」；spec 寫的是「雲端沒有的就傳回去」。測試斷言的也是少了字的版本 | 改成「雲端沒有的就傳回去」，測試一起改 |
| S4 | Low（就是 3.1 的 R3） | 勾了「傳回去」、push 也成功了之後，那一列在雲端欄還是 ✗，要等下一次完整同步才會變回 ✓；這次的改動沒有處理 | 照 R3 的建議：push 成功的那些 id，直接從 `cloud_missing` 拿掉 |
| S5 | Low（就是 2.3 的 Q1） | 未匯入頁的 `P` 現在會打開這個新的確認視窗，按「確定」之後送出的是沒有任何 id 的 `push session` | 照 Q1 修 |
| S6 | Low | `test_pull_without_the_option_sends_no_flag` 的名字說的是 pull，按的卻是 `P`（push）；pull 不勾選項的那個情況，其實沒有測試 | 改名，或者補一個 pull 的版本 |

## 3.3 雲端沒有時的拒絕（`920bd6b`），以及 Q1（`9262a6d`）、S1（`5a6f585`）的修正確認

2026-10-03，review。在 `git archive 920bd6b`（包含這三個 commit）取出的副本跑 `test_tui.py`：**56 passed**。探測測試只在副本裡做。規則同上。

**沒有 High。** 三個都修對了。另外有一個 Low～Medium（U1）：勾選框有焦點的時候按 Enter，會**勾上**那個危險的選項，而不是確定。

### 3.3：對 ✗ 的列接續、改標頭 ✅

- `action_primary`（接續）和 `action_edit` 在問 agent、問目錄、或打開編輯器**之前**，就先呼叫 `_refuse(row, …)`。它用 `store.cloud_gone(self.index, key)` 看標記，有標記就用 `Tell` 顯示拒絕的訊息，然後停下來。agent 和編輯器都不會被打開。
- 訊息是 `store.cloud_lost()`，指令模式的 `_cloud_lost` 也改成呼叫它，所以兩邊的文字只有一份，不會各說各的（T1-sec3 L2）✅。
- 測試：接續被拒絕、而且沒有開 agent；改標頭被拒絕；兩邊的文字一樣 ✅。
- 說明：畫面上的預先檢查**只看標記**。指令模式的 `_refuse_if_gone` 還會再問一次 Drive，也會排除 outbox（M1、F1、F3）。所以「標記還沒有、但 Drive 上已經沒有了」的情況，會照舊進入 suspend，再由指令模式拒絕（訊息印在 suspend 之後的終端機上）。這是可以接受的分工，因為互動模式的清單本來就不去問 Drive（T6）。如果想讓這種情況也在畫面上顯示，可以在 suspend 之前另外開一個子程序做檢查，但我不建議為了這個多花幾秒鐘。

### Q1 的修正 ✅

`check_action` 讓 `push` 只在 Agora 頁、而且不是預覽區有焦點的時候才能用；沒有列可以送的時候，`d`／`p`／`P`／Enter 都會在狀態列提示，不會啟動子程序。測試 `test_push_is_not_bound_on_the_import_tab`、`test_no_action_starts_a_process_without_rows_to_send` ✅。

### S1 的修正 ✅（但有 U1）

`Confirm` 開著的時候，App 會把 Tab／shift+tab 交給它（`action_next_tab`／`action_toggle_focus` 裡判斷 `isinstance(self.screen, Confirm)`），在選項清單和勾選框之間切換；空白鍵（priority）會勾選。我實測過：按 `p` → Tab 之後，焦點就在 `Checkbox` 上。

### U1（Low～Medium）：勾選框有焦點時，Enter 是「勾選」，不是「確定」

實測：按 `p` → Tab（焦點到了勾選框）→ Enter → 視窗**還開著**，`picked` 變成 **True**，沒有任何指令被送出。Textual 的 `Checkbox` 本來就把 Enter 當成切換，可是視窗最下面的提示寫的是「空白 勾選　Enter 確定　Esc 取消」。

使用者用 Tab 移到勾選框上看一下說明，然後照提示按 Enter 想確定，結果是**勾上了**「雲端沒有的就刪掉本機的」或「雲端沒有的就傳回去」。接著再 shift+tab、Enter，就會帶著這個 flag 執行。這兩個選項正是 3.2 刻意預設不勾的那兩個決定。

**建議**：`Confirm` 的勾選框有焦點時，Enter 就當成**確定**，用清單目前停的那一個選項（預設是「取消」，所以是安全的）；或者讓 Enter 只把焦點移回清單，不切換勾選框。並補一個測試：Tab → Enter，`picked` 仍然是 False。

### 其他

| # | 嚴重度 | 問題 | 建議 |
|---|---|---|---|
| U2 | Low | Tab 只有在 `Confirm` 開著的時候才會交出去。其他的視窗（delete 的確認 `Choose`、接續時選 agent 和目錄的 `Choose`、`AskText`、`Tell`）開著的時候，按 Tab **還是會切換底下的頁面**。實測：按 `d` 打開刪除確認、按 Tab 之後，`app.tab` 變成了 `import`。要處理的列在開視窗之前就決定了，不會送錯，但動作做完之後，畫面會停在另一頁 | `action_next_tab`／`action_toggle_focus` 改成判斷 `isinstance(self.screen, ModalScreen)`：是 `Confirm` 就交給它，其他的視窗就什麼都不做 |
| U3 | Low（潛在） | 沒有帶 `extra` 的 `Confirm`，不會產生 `#extra`，這時候按 Tab 或空白，`query_one("#extra")` 會丟出 `NoMatches`。現在所有的呼叫者都有帶 `extra`，所以不會發生 | 沒有 `#extra` 的時候，`action_toggle`／`action_focus_next` 直接 return |
| U4 | Low | 3.2 的 S2（未匯入頁的 pull 也出現「雲端沒有的就刪掉本機的」）、S3（「雲端沒的」少了一個字）、S4（傳回去之後 ✗ 要等下一次完整同步才會變回 ✓）、S6（測試名稱說 pull，按的是 push）這次都沒有處理 | 照 3.2 的建議 |

## U1 的修正確認（`472d683`）

2026-10-03，review。在 `git archive 472d683` 取出的副本跑 `test_tui.py`，連跑兩次：**58 passed**。mutation 在副本裡做。規則同上。

**修對了。** `Confirm` 加了一個 priority 的 `enter` → `action_confirm`，不管焦點在哪裡，都用清單目前停的那一個選項來確定（停在 None 時當成 0，也就是「取消」），並帶上目前勾選框的狀態。勾選框只有空白鍵能切換。

- App 的 `enter`（`primary`）雖然也是 priority，但它的 `check_action` 只在 `DataTable` 有焦點的時候才成立，所以在視窗裡會讓給 `Confirm` ✅。
- 新的測試：
  - `test_enter_on_the_checkbox_confirms_and_does_not_tick_it`：Tab 到勾選框之後按 Enter，送出去的指令**沒有** flag；
  - `test_space_is_what_ticks_it`：空白鍵勾選、shift+tab 回到清單、Enter，送出去的指令**有** flag。
- **mutation**：拿掉那一行 `Binding("enter", …)` 之後，`test_enter_on_the_checkbox_confirms_and_does_not_tick_it` 就會失敗 ✅。
- 同一個 commit 也順便修了 T2-sec2 的 P2：「另一頁的勾選」那個測試，現在真的有在未匯入頁勾一列。

**還沒處理的**（不擋這次的確認）：U2（其他視窗開著的時候，Tab 還是會切換底下的頁面）、U3（沒有 extra 的 `Confirm`）、S2、S3（「雲端沒的」少了一個字，連這次的 commit 訊息裡也是這樣寫的）、S4、S6。

### HEAD 的程式碼行數（`472d683`，算法同 T1-size：不含空行、註解、docstring）

| 檔案 | 程式碼行 | T1-final（`35ab469`）時 | 差 |
|---|---:|---:|---:|
| tui.py | 816 | 692 | **+124** |
| cli.py | 727 | 729 | −2 |
| agents/opencode.py | 538 | 538 | 0 |
| store.py | 476 | 470 | +6 |
| agents/claude.py | 442 | 442 | 0 |
| cache.py | 182 | 182 | 0 |
| header.py | 169 | 169 | 0 |
| agents/base.py | 63 | 63 | 0 |
| **合計** | **3,413** | 3,285 | **+128** |

比 2,900 多了 **513 行**。這段期間增加的幾乎全部都是 T2 第 2、3 節的 tui.py：選取規則、`a`、自己畫的按鍵列、雲端欄、`Confirm`、拒絕的畫面、還有幾個修正。store 的 +6 是 `cloud_lost`／`cloud_gone`。額度怎麼處理，還是要使用者決定（見 T1-size、adapters-size、T1-final 的 D10）。
