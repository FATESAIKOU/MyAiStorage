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
