**沒有 High。** 2.1 的主要目標做到了：看不到的勾選不會被送進動作（V4），標題列會提示被篩選掉的數量，merge 照畫面上的順序。

不過，「成功清掉、失敗保留」這一條在三種情況下不對（M1）：
- 未匯入頁同時有 opencode 和 claude 的列時，第一段成功，**第二段失敗的勾選也被清掉了**；
- 動作失敗時，**另一頁的勾選**會被丟掉；
- 動作成功時，**被篩選掉、根本沒參與這次動作的勾選**也被清掉。

三種我都實測重現了。

# Review：change tui-batch-actions 第 2 節

## 2.1 動作作用在勾選的列（`3ac96d1`）

2026-10-03，review。對照 `specs/interactive-mode/spec.md` 的「動作作用在勾選的列」，以及 `docs/review/T2.md` 的 V4、V6。只提意見，沒有改程式。

在 `git archive 3ac96d1` 取出的副本跑 `tests/unit/test_tui.py`：**36 passed**。探測測試只加在副本裡，用的是 test_tui 本身的 FakeAgent 和 FakeProc，不是真的子程序，也沒有碰 agent。沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。工作目錄裡的 `tui.py`／`test_tui.py` 有還沒 commit 的改動，不在這次的範圍。

### 逐項確認

| spec／V | 實作 | 測試 | 結果 |
|---|---|---|---|
| 只算看得到的勾選（V4） | `chosen_rows()` 只取 `self.shown()` 裡被勾選的 | `test_actions_take_the_marked_rows_that_are_on_screen`；我另外實測：勾兩列、篩選掉一列再按 `d`，送出去的只有看得到的那一個 | ✅ |
| 標題列「另有 N 個勾選被篩選掉」 | `paint_bar` 用 `hidden_marked()`（只算目前這一頁） | `test_the_header_says_how_many_marked_rows_are_hidden` | ✅ |
| merge 照畫面由上到下 | `action_merge` 改用 `chosen_rows()`（`shown()` 的順序就是表格的順序，程式裡沒有欄位排序） | `test_merge_reads_its_sources_from_top_to_bottom` | ✅（但測試偏弱，見 L1） |
| merge 至少兩個、不退回游標那一列 | 沒有勾選時，`chosen_rows()` 會退回游標那一列（一列），`< 2` 就提示 | — | ✅ |
| merge、edit、delete 只在 Agora 頁 | `check_action` 讓 merge／edit／delete 只在 Agora 頁出現 | — | ✅（所以 merge 改用 `chosen_rows()`，不會在未匯入頁拿到 agent 的列） |
| 沒勾選時用游標那一列 | `chosen_rows()` 退回 `current()` | `test_the_cursor_row_is_used_when_nothing_visible_is_marked`（**只有 Agora 頁**） | ✅（spec 的 Scenario「未匯入頁沒有勾選，按 Enter 匯入游標那一列」沒有直接的測試，L2） |
| continue、edit 只看游標那一列 | `action_primary` 的接續分支、`action_edit` 都用 `current()` | — | ✅ |
| 成功清勾選、失敗保留 | `act()` → `reload(keep_marked=code != 0)` | 1.6 的測試（只有一個指令的情況） | ⚠️ **M1** |
| V6：merge 的順序寫進 spec | spec 寫的是「畫面上由上到下」 | | ✅ |

### M1（Medium）：勾選在三種情況下沒有照「成功清、失敗留」

`act()` 結束時呼叫 `reload(keep_marked=code != 0)`。`self.marked` 是**兩頁共用**的一個集合，而 `reload` 的處理是：成功 → `self.marked.clear()`（全部清掉）；失敗 → `self.marked &= {目前這一頁的列}`（只留目前這一頁的）。實測（探測測試，FakeProc 依序回傳不同的 exit code）：

| 情況 | 之前的勾選 | 之後的勾選 | 應該是 |
|---|---|---|---|
| A. 未匯入頁勾了 opencode 和 claude 各一列，按 Enter；opencode 那一段成功，claude 那一段失敗（exit 2） | `claude:c1`、`opencode:s1` | **空的** | `claude:c1` 留著（spec：失敗 MUST 保留，方便重跑） |
| B. 未匯入頁勾了一列，Agora 頁也勾了一列，在 Agora 頁按 `d`，失敗 | `agora:…B`、`opencode:s1` | 只剩 `agora:…B` | 兩個都留著：未匯入頁那一列根本沒參與這次動作 |
| C. Agora 頁勾了兩列，篩選掉其中一列，按 `d`，成功 | 2 個（其中 1 個被篩選掉） | **0 個** | 被篩選掉的那一個留著：它沒有被送進動作（V4 的精神是「看不到的不動」） |

A 是 2.1 最直接要求的「失敗保留」沒做到，而且會讓 M2（T2-sec1）的「一個 agent 一個指令」在第二段失敗時，沒辦法按同一個鍵重跑。

**修法**：`act()` 收一個參數，代表這次動作送出去的 keys；成功時只從 `self.marked` 拿掉**這些** key，失敗時什麼都不拿掉。`reload` 只負責拿掉「已經不存在的列」的勾選，而且要對**兩頁**的列一起算：`self.marked &= {r.key for t in TABS for r in self.rows[t]}`。未匯入頁的多段匯入，每一段只清自己那一段成功的。三種情況各補一個測試（就是上面那三個探測的步驟）。

### 其他

| # | 嚴重度 | 問題 | 建議 |
|---|---|---|---|
| L1 | Low | `test_merge_reads_its_sources_from_top_to_bottom` 是**由上往下**依序勾的，所以「勾選的先後順序」和「畫面順序」剛好一樣，這個測試分不出兩者；2.1 之前的寫法（`self.rows["agora"]` 的順序）也一樣會通過 | 改成**由下往上**勾，再斷言 argv 是畫面上由上到下的順序 |
| L2 | Low | spec 的 Scenario「沒勾選時匯入游標那一列」（未匯入頁）沒有直接的測試；游標那一列的測試只在 Agora 頁 | 在未匯入頁補一個：不勾選，把游標移到第二列，按 Enter，argv 只有那一個 `--external-session-id` |
| L3 | Low | 只勾了一列，另一列被篩選掉時按 `m`，提示的是「合併要先用空白鍵勾選至少兩個」，沒有說明另外那一個被篩選掉了（雖然標題列有寫） | 有被篩選掉的勾選時，提示改成「看得到的勾選只有 1 個（另有 N 個被篩選掉）」 |
| L4 | Low | delete 的確認視窗只列出看得到的那幾個，這是對的；但如果有被篩選掉的勾選，確認視窗裡沒有再提一次 | 確認視窗的說明加一行「另有 N 個勾選被篩選掉，不會刪」，因為 delete 是最危險的那一個動作 |

## 2.2 全選切換（`ffedd3d`）

2026-10-03，review。對照 spec 的「全選切換」。在 `git archive ffedd3d` 取出的副本跑 `test_tui.py`：**39 passed**。探測測試和 mutation 都只在副本裡做。規則同上。

**沒有 High，也沒有 Medium。** 切換的邏輯是對的。

| spec | 實作 | 測試 | 結果 |
|---|---|---|---|
| `a` 切換看得到的列：沒有全部勾選 → 全部勾選；全部勾選了 → 全部取消 | `action_mark_all`：看 `shown()` 的 key，全部都勾了就 `-=`，否則就 `\|=` | `test_a_marks_every_visible_row_and_toggles_them_off_again`（就是 spec 的 Scenario「切換兩次」） | ✅ |
| 篩選掉的列，勾選狀態不變 | 只對看得到的 key 做 `-=`／`\|=` | `test_a_does_not_touch_the_rows_the_filter_hides` | ✅ 程式是對的，但測試只守了一個方向（N1） |
| 空白鍵勾選時游標不動 | `action_mark` 用 `update_cell` | `test_space_marks_without_moving_the_cursor` | ✅ |
| 標題列的「另有 N 個被篩選掉」跟著更新 | `action_mark_all` 最後呼叫 `show()` → `paint_bar()` | — | ✅ |

### N1（Low）：「全部取消時不動看不到的勾選」這個方向沒有測試

- 我把 `self.marked -= set(keys)` 改成 `self.marked.clear()`（全部取消時，連看不到的也一起清掉），**`test_tui.py` 39 個測試全部照樣通過**。
- 反方向的 mutation（全選時改成 `self.marked = set(keys)`，把看不到的勾選蓋掉）會被抓到。

也就是說，現有的測試只證明了「篩選之後按 `a`，不會勾到看不到的列」，沒有證明「篩選之後再按 `a` 取消，不會把看不到的勾選清掉」，而後者正是 commit 訊息說的「篩選來回之後，勾選不會變少」。

我在副本裡實測了程式本身：三列全勾 → 篩選到只剩「甲」→ 按 `a` → 剩下的是「乙」「丙」，所以程式是對的。**建議**把這個探測的步驟加成正式的測試。

### N2（Low）：按 `a` 之後，游標會跳回第一列

`action_mark_all` 最後呼叫的是 `self.show()`，沒有帶 `keep`，所以整個表格會重建，游標回到第 0 列。實測：游標在第 2 列，按 `a` 之後變成 0。spec 只規定空白鍵不能移動游標，但「全選之後，游標跑回最上面」一樣會讓使用者失去位置（而且右邊的預覽也會跟著換）。

**建議**：像 `action_mark` 一樣，對看得到的每一列 `update_cell(key, "mark", …)` 之後，再 `paint_bar()`；或者至少用 `self.show(keep=current.key)`。順便加一個斷言：按 `a` 前後，`cursor_row` 不變。

### N3（Low）：預覽區有焦點的時候，`a` 也有作用

`check_action` 沒有列出 `mark_all`，所以它永遠是可用的。空白鍵（`mark`）在預覽區有焦點的時候是停用的，`a` 卻會改左邊清單的勾選，兩個不一致。這和 2.3 的「按鍵列只顯示能用的」一起處理就好：把 `mark_all` 加進 `("mark", …)` 那一組。

## 修正確認（2026-10-03）：`1c390d4`（M1）、`aa81e2e`（2.2 的 N1、N2）

在 `git archive aa81e2e` 取出的副本跑 `test_tui.py`：**44 passed**（但其中一個測試不穩定，見 P1）。每一個修正各做一次 mutation，都在副本裡做。規則同上。

**M1 修對了。** `act()` 多了 `sent`，也就是這次送出去的那些 key。成功時只從勾選裡拿掉這些；失敗時什麼都不拿。`reload` 只拿掉兩頁都已經不存在的列的勾選。5 個呼叫 `act()` 的地方（import 的每一段、merge、pull、push、delete）都有傳 `sent`。

**N1、N2 也修對了。** `a` 會記住游標那一列，用 `show(keep=…)` 重建，游標不會再跳。

| 拿掉的修正 | 結果 |
|---|---|
| M1：回到「成功全清、失敗只留這一頁」 | **被抓到**（`test_a_failed_segment_keeps_its_own_marks`、`test_a_success_clears_only_the_rows_it_acted_on`） |
| M1：成功時清掉所有的勾選 | **被抓到**（同上兩個） |
| M1：`reload` 只留**這一頁**的勾選（也就是 M1 的情況 B） | **沒被抓到**，見 P2 |
| N1：全部取消時，連看不到的勾選也清掉 | **被抓到**（`test_cancelling_every_visible_row_keeps_the_hidden_marks`） |
| N2：`a` 重建時不保留游標 | **被抓到**（`test_a_leaves_the_cursor_where_it_was`） |

### P1（Low～Medium）：`test_the_escalation_stops_when_the_group_is_gone` 不穩定

在**沒有改過**的副本上連跑 3 次：失敗、通過、失敗。第一輪 mutation 裡，它在好幾個跟升級完全無關的 mutant 上也失敗了。

原因：測試把 `ESCALATE_AFTER` 設成 0.05 秒，按 Esc 之後用 `_wait(lambda: sent, pilot)` 等 SIGINT 送出去，**然後**才設 `alive[4242] = False`。可是 SIGTERM 的計時器從 `stop_group` 那一刻就開始算了，`_wait` 只要多輪詢了一次，SIGTERM 就會在「group 已經不在了」被設定之前送出去，`sent` 就變成 `[SIGINT, SIGTERM]`。

這是 T2 1.x 的測試，不是這兩個 commit 帶進來的，但它會讓任何人跑 `test_tui.py` 時隨機失敗，也會讓 mutation 的結果失準。**建議**：讓假的 `killpg` 收到 SIGINT 時就把 `alive[pgid]` 設成 False（模擬「收到 SIGINT 就結束」的程序），不要在測試裡事後去改。

### P2（Low）：「失敗時另一頁的勾選留著」，測試其實沒有測到

`test_a_failure_on_one_tab_keeps_the_other_tabs_marks` 的做法是 `app.marked = set(keys)`，其中 `keys` 只有 **Agora 頁**的列，未匯入頁根本沒有勾選。所以把 `reload` 改回「只留這一頁」之後，這個測試還是會過。程式是對的（`reload` 的確是對兩頁一起算），只是測試不符合它的名字。

**建議**：在未匯入頁也勾一列（`opencode:s1`），斷言 delete 失敗之後它還在。這就是我 2.1 的探測 B 的步驟。

### 其他

| # | 嚴重度 | 問題 | 建議 |
|---|---|---|---|
| P3 | Low | exit 3（已存進 outbox）被當成失敗，所以勾選都留著。對 merge 來說，exit 3 代表新的 Session **已經做出來了**（只是還沒上傳），這時再按一次 `m`，就會**再做一個**合併的 Session（要約會沿用快取，所以很快，但結果是兩份）。import 的 exit 3 重跑不會有問題（內容沒變，會沿用同一個 id） | `code in (0, 3)` 時清掉 `sent` 的勾選（exit 3 是「做完了，只是還沒上傳」，outbox 之後會自己送）；狀態列已經寫了「已存進 outbox」 |
| P4 | Low | 動作成功之後，`act()` 裡的 `self.show()` 和 `reload()` 裡的 `show()` 都沒有帶 `keep`，所以每一次動作做完，游標都會回到第一列（這不是新的問題） | 和 N2 一樣，記住動作之前游標那一列，`show(keep=…)` |
