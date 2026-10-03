**沒有 High。** T2-final 的 W1、W2、W3、S3 都修對了，4.1b 和 design 5.9 也到位。

- **時間**：W1 用真正的預設值（`ESCALATE_AFTER = 5.0`）實測，送出的時間是 **SIGINT 0.0 秒、SIGTERM 5.0 秒、SIGKILL 10.0 秒**。
- **mutation**：做了 6 個，5 個被抓到。
- **唯一沒被抓到的**是 4.1b 的測試：它守不住它自己要守的那個問題（review M1，「只看 leader 還在不在」）。原因是測試裡的孫程序**繼承了互動模式的 stdout pipe**，和實際的 agent 不一樣。只要改一行就能修好（X1，Medium），建議歸檔前改。

# Review：T2（change tui-batch-actions）歸檔前的確認

2026-10-03，review。對象：`98c723e`（W1）、`68c4b4e`（W2）、`fb34c16`（W3）、`b8a8ed7`（S3，**已經在 HEAD 了**）、`169fff7`（4.1b）、`d83575e`（design 5.9）。HEAD 是 `b9269aa`，在 `b8a8ed7` 之後，`src`／`tests` 都沒有再改過。

在 `git archive` 取出的副本跑 `test_tui.py`：**66 passed**。4.1b 那個用真的子程序的測試單獨連跑 5 次，**5 次都通過**。mutation 和探測測試都只在副本裡做；做完之後，用 `ps` 確認過沒有留下任何測試用的子程序。沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。

## 修正確認

| 項目 | 改了什麼 | 驗證 | 結果 |
|---|---|---|---|
| W1：升級的時間 | 第 n 步等 `ESCALATE_AFTER * n`；刪掉重複定義的 `ESCALATION`（現在只剩一個） | 探測測試用**真正的預設值** 5.0：`SIGINT 0.0`、`SIGTERM 5.0`、`SIGKILL 10.0` ✅；mutation 改回同一個計時器 → **被抓到**（`test_each_step_of_the_escalation_gets_its_own_time` 斷言 SIGKILL 至少比 SIGTERM 晚 0.9 × `ESCALATE_AFTER`） | ✅ |
| W2：`a` 全選時不動看不到的勾選 | `test_marking_all_leaves_the_rows_the_filter_hides_alone`：先勾「乙」，篩選到只剩「甲」，按 `a`，乙還勾著；取消篩選之後也一樣 | mutation 改成 `marked = set(看得到的)` → **被抓到** | ✅ |
| W2：確認視窗直接按 Enter 是取消 | `test_enter_on_the_confirmation_window_keeps_it_cancelled`：按 `p`，斷言停在第 0 項（取消），按 Enter 之後沒有啟動子程序 | mutation 改成 Enter 一律「確定」→ **被抓到** | ✅ |
| W3：進度只讀自己那一行 | 測試的順序反過來了：先 `[agora] pull 1/2`，再一行同時含 `3/4` 和日期的失敗訊息 | mutation 把格式放寬到「行裡任何位置的 `k/N` 都算」→ **被抓到** | ✅ |
| S3：錯字 | `雲端沒有的就傳回去（等同 --not-exist-upload）`；測試改成斷言完整的字串 | `git grep 雲端沒的`：只剩 review 的歷史紀錄和 tasks 的描述 | ✅ |
| 4.1b：真的子程序 | `test_esc_stops_a_real_process_group_with_a_stubborn_grandchild`：leader 收到 SIGINT 就 exit 130；孫程序不理 SIGINT 和 SIGTERM。斷言三個訊號依序送出、孫程序死了、`killpg(pgid, 0)` 說 group 已經空了 | mutation 讓 `killpg` 只對 leader 送 → **被抓到** ✅；mutation 讓 `group_alive` 只看 leader（也就是 M1 的那個 bug）→ **沒被抓到** ❌，見 X1 | ⚠️ |
| design 5.9 | 版面圖的按鍵列改成 `空白 a enter m e d p P / ctrl+t q`，和程式的 `KEYS` 一字不差；寫明 Tab、shift+tab、↑↓ 能用但不印在按鍵列上；`d` 改成「取消／確定」的小視窗，預設停在「取消」 | 和 `tui.py` 的 `KEYS`、`action_delete` 對照過 | ✅ |

## X1（Medium，建議歸檔前修）：4.1b 的測試守不住 M1

**現象**：把 `group_alive` 改成只看 leader（`os.kill(pgid, 0)`，而不是 `os.killpg(pgid, 0)`），也就是 T2-sec1 M1 的那個 bug 之後，這個真的子程序的測試**照樣通過**。

**原因**：測試裡的 `LEADER` 是用 `subprocess.Popen([...])` 啟動孫程序的，沒有指定 stdout，所以孫程序**繼承了 leader 的 stdout**，也就是互動模式在讀的那個 pipe。結果是：
1. leader 收到 SIGINT、exit 130 之後，pipe 的寫入端還在孫程序手上，所以等待視窗的讀取執行緒讀不到 EOF，也就**一直不會**呼叫 `proc.wait()`；
2. leader 就一直是個 zombie，而 zombie 還算是存在的程序，所以 `os.kill(pgid, 0)` 會成功；
3. 「只看 leader」的 mutant 因此以為 group 還在，照樣升級，孫程序也就被殺掉了，測試通過。

實際的情況不一樣：寫要約的 opencode，stdout 是接到 **agora 自己開的 pipe**（`_summarizing`），不是互動模式的那一個。所以 agora 一結束，互動模式就讀到 EOF、把它收掉；這時「只看 leader」就會以為 group 已經沒了、停止升級，agent 就這樣留了下來。這正是 M1。

**驗證**（在副本裡）：把孫程序改成 `stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL` 之後：
- 原本的程式：測試**通過**（1.8 秒）；
- M1 的 mutant：測試**失敗**，`assert [2] == [2, 15, 9]`，也就是只送出了 SIGINT。

**修法**：`LEADER` 裡孫程序的 `Popen` 加上 `stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL`，這樣更接近 agora 實際的樣子。另外，建議在測試的最後加一個 `finally: os.killpg(pgid, SIGKILL)`（接住 ProcessLookupError）。這樣就算測試在中途失敗，也不會留下一個 300 秒的孫程序。

## 歸檔前還剩什麼（不含行數與 PM 試用）

### 建議歸檔前做

| # | 項目 |
|---|---|
| X1 | 4.1b 的孫程序不要繼承 pipe，加上 finally 清理（上面）。一兩行的改動，但它是「整個 group 都會停下來」**唯一**的真實保證 |
| X2 | tasks.md 有**兩個** `4.2b`：一個是 W1（已完成），一個是 PM 試用的 Q1～Q4（還沒做）。歸檔前請重新編號，不然看不出到底剩下什麼 |
| — | tasks 第二個 4.2b（PM 試用的 Q1～Q4：勾選框不能只靠顏色、delete 視窗的提示文字、進度條算「做完的」、結果視窗之後清掉狀態列）是 PM 那邊的項目，我沒有 review 過，要不要擋歸檔由 PM 決定 |

### 可以歸檔之後再處理（Low，記下來）

| 來源 | 項目 |
|---|---|
| T2-sec2 | L1（merge 順序的測試，分不出勾選的順序和畫面的順序）、L3（只勾一個時，`m` 的提示沒有提到被篩選掉的那些）、L4（delete 確認視窗沒有提到被篩選掉的勾選）、P3（merge 的 exit 3 會留著勾選，重跑會多做一份）、P4（動作完成之後游標回到第一列）、Q2（按鍵列是寫死的，不會跟著焦點變：預覽區有焦點時，還是會印出不能用的鍵。嚴格來說，這不符合 spec 的「只顯示**目前**能用的鍵」） |
| T2-sec3 | R1（`agora_rows` 的 `paths` 應該改成必填）、R2（✗ 的那個 Scenario 沒有走過「刪掉 → 同步 → 重讀」的完整一條路）、S2（未匯入頁的 pull 也出現「雲端沒有的就刪掉本機的」，對 agent 的 session 來說說明不對）、S4（傳回去之後，✗ 要等到下一次完整同步才會變回 ✓）、S6（測試名稱說 pull，按的是 push）、U2（其他視窗開著的時候，Tab 還是會切換底下的頁面）、U3（沒有 extra 的 `Confirm`，按 Tab 或空白會丟出 NoMatches） |
| T2-final | 結果視窗依照 exit code 的說明，只有「已中斷」有測試；0、2、3 的說法沒有測試。「看得到進度」只斷言了最後是 5/5 |

## 這次讀過、跑過的東西

5 個 commit 的 diff，以及 `b8a8ed7` 的錯字修正；HEAD 的 `tui.py` 裡的 `killpg`／`group_alive`／`stop_group`／`KEYS`；tasks.md。單獨跑 4.1b 的測試 5 次，全部通過；`test_tui.py` 66 passed。用預設的 5 秒實測升級的時間。6 個 mutation 抓到 5 個；沒抓到的那一個，原因已經在副本裡實測確認（孫程序改成 DEVNULL 之後就抓得到了）。做完之後，用 `ps` 確認沒有任何殘留的測試子程序。
