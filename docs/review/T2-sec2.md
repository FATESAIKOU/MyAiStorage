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
