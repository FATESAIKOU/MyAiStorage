**沒有 High。** 但歸檔前有 4 件事要先處理：
- **(1)** Esc 的升級時間不對：spec 要求 SIGINT →（5 秒）SIGTERM →（再 5 秒）SIGKILL，程式卻是在 **5 秒時同時送出 SIGTERM 和 SIGKILL**，agent 沒有任何收尾的時間（W1，Medium，已實測）。
- **(2)** 4.1b（用**真的子程序**測一次 Esc）還沒做；現在「整個 process group 都會停下來」只用假的物件測過。
- **(3)** 有兩條 MUST 拿掉之後沒有任何測試會失敗：「`a` 全選時不動看不到的勾選」，以及「確認視窗裡，直接按 Enter（停在『取消』）就是取消」。
- **(4)** 程式碼行數 **3,413**，超過 2,900，要使用者決定。

# Review：change tui-batch-actions 歸檔前的規格對照（4.1，`405de48`）

2026-10-03，review。對照 `specs/interactive-mode/spec.md` 的 5 個 Requirement、8 個 Scenario。HEAD `8691478` 的程式和測試，在 `405de48` 之後都沒有改過（工作目錄裡有別人還沒 commit 的改動，不在這次的範圍）。

在 `git archive HEAD` 取出的副本跑**全部的**單元測試：**424 passed**。對 spec 的關鍵 MUST 做了 16 個 mutation，每次只在 `tui.py` 改一處，跑 `test_tui.py`；另外加了一個量測升級時間的探測測試。都只在副本裡做。沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。

## Scenario 對照

| Requirement／Scenario | 測試（用按鍵驅動） | 結果 |
|---|---|---|
| 動作作用在勾選的列／**刪除勾選的** | `test_deleting_three_marked_rows_is_one_command`：勾三列，`d`，確定 → argv 是 `delete session <三個> --yes` | ✅ |
| ／**篩選掉的勾選不算** | `test_actions_take_the_marked_rows_that_are_on_screen`、`test_the_header_says_how_many_marked_rows_are_hidden` | ✅（mutation 抓得到）；斷言的是 `chosen_rows()`，不是按 `d` 之後的 argv，不過 `test_a_success_clears_only_the_rows_it_acted_on` 有真的按 `d` |
| ／**沒勾選時匯入游標那一列** | `test_import_uses_the_row_under_the_cursor_when_nothing_is_marked` | ✅（就是 T2-sec2 L2 要的那一個） |
| 全選切換／**切換兩次** | `test_a_marks_every_visible_row_and_toggles_them_off_again` | ✅（mutation 抓得到） |
| 進度與中斷／**看得到進度** | `test_the_progress_bar_walks_from_one_to_five` | ✅\*：只斷言最後是 5/5（假程序一次就吐完五行，看不到中間的狀態），沒有證明是「走」到 5/5 的 |
| ／**中斷 merge** | `test_interrupting_a_merge_returns_to_the_list_and_says_it_carries_on` | ✅ 前半段（停下來、回到清單、說「重跑會接著做」）；後半段「再 merge 時沿用第一個來源的要約」是指令模式的行為，由 `test_cli.py` 的 `test_merge_reuses_a_summary_it_already_paid_for` 負責，互動模式這邊只要送出同樣的 argv 就好 |
| 雲端欄／**看出被別台刪掉的** | `test_the_cloud_column_says_where_each_session_is` | ✅（mutation 抓得到）；但它是直接寫標記，沒有走過「Drive 上刪掉 → 同步 → 重讀」（T2-sec3 R2） |
| ／**對雲端沒有的接續** | `test_continuing_a_session_the_cloud_lost_is_refused_on_screen` | ✅（mutation 抓得到） |
| 按鍵／**未匯入頁的按鍵列** | `test_the_key_bar_shows_only_what_works_on_this_tab` | ✅ |

**8 個 Scenario 都有用按鍵驅動的測試。**

## 關鍵 MUST 的 mutation

| MUST（spec 的原文） | 拿掉的實作 | 結果 |
|---|---|---|
| 勾選只算看得到的 | `chosen_rows` 改成取全部的列 | **被抓到** |
| 標題列顯示「另有 N 個勾選被篩選掉」 | 不顯示 | **被抓到** |
| `a`：已經全部勾選時，全部取消 | 永遠只會全選 | **被抓到** |
| `a`：篩選掉的列的勾選 MUST NOT 被改變（**全選**的方向） | 全選時改成 `marked = set(看得到的)`，把看不到的勾選蓋掉 | **沒被抓到** ⚠️（W2） |
| `a`：（全部取消的方向） | 全部取消時連看不到的也清掉 | 被抓到（T2-sec2 修正確認時做過） |
| `a` 不移動游標 | 重建時不保留游標 | **被抓到** |
| Esc：先送 SIGINT | 第一步改成 SIGTERM | **被抓到** |
| Esc：沒停就升級 | `ESCALATION = ()`（兩個定義都改） | **被抓到** |
| Esc：5 秒後 SIGTERM，**再 5 秒** SIGKILL | — | ❌ **程式本身就不符合**（W1） |
| 子程序讀不到鍵盤 | 拿掉 `stdin=DEVNULL` | **被抓到** |
| 進度只讀 `[agora] … k/N` 的行 | 改成 `[agora]` 那一行裡任何位置的 `k/N` 都算 | **沒被抓到** ⚠️（W3） |
| 雲端欄 ✗ | ✗ 一律顯示成 ✓ | **被抓到** |
| 雲端欄「未上傳」優先 | 不先看 outbox | **被抓到** |
| 確認視窗的選項預設不勾 | 預設打勾 | **被抓到** |
| 確認視窗：Enter 一律是確定，不會勾選 | 拿掉 Enter 的綁定 | 被抓到（U1 修正確認時做過） |
| 確認視窗：Enter 用的是**目前停的那個選項**（預設是「取消」） | 改成 Enter 一律當成選了「確定」 | **沒被抓到** ⚠️（W2） |
| 對雲端沒有的不接續、不改標頭 | `_refuse` 永遠放行 | **被抓到** |
| 未匯入頁不能 push | 拿掉 `check_action` 的 push 判斷 | **被抓到** |
| 成功只清掉送出去的那些列的勾選 | 成功時清掉全部 | **被抓到** |

16 個 mutation 裡，被抓到 13 個，沒被抓到 3 個（W2 兩個、W3 一個）。

## W1（Medium，擋歸檔）：SIGTERM 和 SIGKILL 是同時送出的

```python
for sig in ESCALATION:                      # (SIGTERM, SIGKILL)
    self.set_timer(ESCALATE_AFTER, lambda sig=sig: self._step(pgid, sig))
```

兩個計時器用的都是 `ESCALATE_AFTER`（5 秒），所以 SIGTERM 和 SIGKILL 會在**同一個時間點**觸發。

實測：探測測試把 `ESCALATE_AFTER` 設成 0.5 秒，group 一直不結束，記下每一次 `killpg` 的時間：`SIGINT 0.0`、`SIGTERM 0.5`、**`SIGKILL 0.5`**；正確的時間應該是 1.0。

spec 寫的是「5 秒內沒停就送 SIGTERM，**再 5 秒**就 SIGKILL」，T2.md V2 要這個階梯，就是為了讓收到 SIGTERM 的 agent（例如寫要約的 opencode）有時間自己收尾。現在等於是：5 秒沒停，就直接 SIGKILL。

現有的測試（`test_the_escalation_goes_on_after_agora_itself_is_gone`）斷言的是送出的**順序** `[SIGINT, SIGTERM, SIGKILL]`，沒有看**時間**，所以抓不到。

**修法**：`for n, sig in enumerate(ESCALATION, 1): self.set_timer(ESCALATE_AFTER * n, …)`。補一個測試：記下 `killpg` 的時間，斷言 SIGKILL 比 SIGTERM 晚了大約 `ESCALATE_AFTER`。順便：`ESCALATION` 在 `tui.py` 裡**定義了兩次**（第 304、308 行），刪掉一個，否則以後只改其中一個就會沒有作用（我第一次做 mutation 時就是這樣被騙的）。

## W2（Low～Medium）：兩條「安全的預設」沒有測試守著

- **`a` 全選時不動看不到的勾選。** 現有的兩個測試，一個是「篩選前沒有任何勾選」，另一個是「全部取消」的方向，所以全選時把看不到的勾選蓋掉，沒有任何測試會失敗（在 2.2 review 時，這個 mutation 看起來被抓到了，那是 P1 那個不穩定的測試造成的假象）。**補**：先勾「乙」，篩選到只剩「甲」，按 `a`，取消篩選，斷言「乙」還是勾著的。
- **確認視窗：直接按 Enter 就是取消。** `Confirm` 打開時停在「取消」，Enter 用的是停的那一個，所以直接按 Enter 是安全的。但沒有測試：改成 Enter 一律「確定」之後，測試全過。對 pull 的 `--not-exist-delete` 來說，這就是「多按一次 Enter」和「刪掉本機副本」之間的距離。**補**：按 `p` → 直接 Enter → 沒有啟動子程序。

## W3（Low）：進度只讀自己那一行，這個測試抓不到

`test_a_failure_line_with_a_timestamp_in_it_is_not_progress` 的假程序輸出的是：先一行含 `2026/10/03` 的失敗訊息，**之後**才是 `[agora] pull 1/2`。進度條取的是最後一個符合的行，所以就算把格式放寬到「行裡任何位置的 `k/N` 都算」，最後一行還是 `1/2`，測試照樣會過。**補**：把順序反過來，先 `[agora] pull 1/2`，再那一行失敗訊息，斷言進度還是 (1, 2)。

## 其他還沒處理的（不擋歸檔，記下來）

| 來源 | 項目 |
|---|---|
| tasks 4.1b | 用真的子程序測一次 Esc（T2.md V7 的寫法），外加一個「leader 先結束、孫程序不理 SIGINT」的版本（T2-sec1 M1）。**建議歸檔前做完**，它是 W1 這一類問題唯一的保證 |
| tasks 4.3 | PM 在 pane 裡操作一遍（可以用 `acceptance-draft.md` 的第二段） |
| 結果視窗的說明 | spec 要求結果視窗依照 exit code 說明「成功、部分失敗、已存進 outbox、已中斷」，但只有「已中斷」有測試；0、2、3 的說法沒有測試 |
| T2-sec2 | L1（merge 順序的測試分不出勾選的順序和畫面的順序）、L3、L4、P3（merge 的 exit 3 留著勾選，重跑會多做一份）、P4（動作完成後游標回到第一列）、Q2（按鍵列不會跟著焦點變）、Q3（沒顯示 Tab、shift+tab） |
| T2-sec3 | R1（`agora_rows` 的 `paths` 應該必填）、R2（✗ 的那個 Scenario 沒有走完整條路）、S2（未匯入頁的 pull 選項）、**S3（「雲端沒的」少了一個字，還在）**、S4（傳回去之後 ✗ 要等到下一次完整同步）、S6、U2（其他視窗裡 Tab 會切換底下的頁面）、U3 |
| design 5.9 | 版面示意圖的按鍵列寫著 `↑↓ … Tab shift+tab …`，可是實際的按鍵列沒有這些（Q3）；`d` 寫的是「確認 y／n」，實際是「取消／確定」的清單，預設停在取消 |
| 行數 | HEAD 是 **3,413**（tui 816、cli 727、opencode 538、store 476、claude 442、cache 182、header 169、base 63），超過 2,900 共 513 行，要使用者決定（T1-size、adapters-size、T1-final D10、T2-sec3） |

## 建議的歸檔條件

1. 修 W1（升級的時間），補一個看時間的測試，並刪掉重複的 `ESCALATION`。
2. 做完 4.1b（真的子程序）。
3. 補 W2 的兩個測試、W3 的那一個。
4. 順手修 S3 的錯字和 design 5.9 那兩處文字。
5. 行數由使用者決定，PM 做完 4.3 之後再打勾歸檔。
