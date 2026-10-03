**沒有 High。** 兩個 commit 都修對了，mutation 9 個裡抓到 8 個。

但 T2 歸檔前還有一件要處理：`5ae265b` 的 Q3（進度條算做完的）和 Q4（結果視窗之後清掉狀態列），改變了 `specs/interactive-mode/spec.md` 裡**兩句寫明的話**，而 spec 和 design 5.9 都沒有跟著改（E1，Medium，只要改文字）。另外，「正常結束時進度條會填滿」實際上**看不到**，也沒有測試（E2）。T1 沒有擋歸檔的事，剩下的都要等使用者決定。

# 最後的確認：`5ae265b`（T2 4.2f）與 `01588da`（T1 4.2e）

2026-10-03，review。HEAD 是 `fa52378`。在 `git archive HEAD` 取出的副本裡做了 9 個 mutation，以及一個記錄進度條每一個值的探測測試。沒有改 repo 裡的程式，沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。

## (1) `5ae265b`：PM 試用的 Q1～Q4

| 項目 | 改了什麼 | mutation（拿掉修正） | 結果 |
|---|---|---|---|
| Q1：勾選框不能只靠顏色 | 標籤加上 `[ ]`／`[x]`；下面那一行說明會跟著狀態換（pull：「⚠ 勾了：雲端沒有的，本機這份會被刪掉」；push：「⚠ 勾了：別台機器刪掉的 Session 會被傳回 Drive」） | 勾了之後標籤不變 → **被抓到**；說明不跟著換 → **被抓到** | ✅ |
| Q2：「Enter 選擇」 | `Choose`、`AskText`、`Confirm` 的提示都改成「Enter 選擇」 | 把 delete 的視窗改回「Enter 確定」→ **被抓到** | ✅（見 E3） |
| Q3：進度條算做完的 | 收到 `k/N` 時顯示 `k-1`；正常結束（exit 0）時才是 `N` | 改回顯示「開始的」→ **被抓到**（5 個測試）；拿掉「正常結束時填滿」→ **沒被抓到**（E2） | ⚠️ |
| Q4：結果視窗之後清掉狀態列 | `act()` 結束時是 `self.say("")`；說明只留在結果視窗裡 | 狀態列繼續留著說明 → **被抓到** | ✅（見 E1） |

### E1（Medium，擋 T2 歸檔，只要改文字）：spec 和 design 寫明的兩句話，現在不成立了

1. **spec 的 Scenario「看得到進度」**（`specs/interactive-mode/spec.md` 第 34 行）寫的是：匯入五個 → 「**進度條從 1/5 走到 5/5**」。
   - 現在收到 `1/5` 時，顯示的是 0/5；收到 `5/5` 時，顯示的是 4/5。對應的測試 `test_the_progress_bar_walks_from_one_to_five` 也改成了斷言 `(4, 5)`，所以測試和 spec 現在說的是兩件不同的事。
   - 而 5/5 實際上看不到（E2）。
2. **spec 的「進度與中斷」**（第 30 行那一段）寫的是：「中斷後畫面 MUST **回到清單並說明**『重跑同一個動作會接著做』」；design 5.9 第 367 行也是一樣的說法。
   - 現在，回到清單之後狀態列是空的，那句說明只出現在**結果視窗**裡（測試 `test_interrupting_a_merge_…` 也改成在結果視窗的內容裡找那句話）。

spec 會跟著這個 change 一起歸檔，所以要在歸檔前把文字改成實際的行為。

**建議的寫法**：
- 第 34 行：「進度條顯示**已經做完**的個數：從 0/5 起，收到第 5 個時是 4/5；指令正常結束時，結果視窗說『完成』」。
- 第 30 行：「中斷後，結果視窗 MUST 說明『重跑同一個動作會接著做』，關掉之後回到清單」。
- design 5.9 的第 365 行（「依指令印的 `k/N` 更新」）和第 367 行，也改成同樣的說法。

### E2（Low～Medium）：「正常結束時進度條會填滿」實際上看不到，也沒有測試

`Run.read` 在子程序結束之後，先設定 `self.done = True`，接著就**馬上**呼叫 `dismiss`。而進度條是 `tick` 每 0.1 秒才重畫一次，所以通常在畫出 N/N 之前，視窗就已經關掉了。

實測：一個正常結束、每 0.15 秒出一行 `k/5` 的假程序，進度條實際出現過的值是 `0/5 → 1/5 → 2/5 → 3/5`，然後視窗就關了，**從來沒有出現 5/5**（實際的指令每一項都要好幾秒，所以會看到 4/5，但一樣看不到 5/5）。

把 `self.done = code == 0` 拿掉之後，測試照樣全過；`test_the_bar_counts_what_is_finished_and_fills_only_on_a_clean_exit` 只斷言了結果視窗裡有「完成」，沒有看進度條。

**建議**：二選一。
- (a) 結束時，在 dismiss **之前**用 `call_from_thread` 先把進度條更新成 N/N，再 dismiss；再補一個斷言進度條真的到過 N/N 的測試。
- (b) 承認「填滿」只是一個概念上的狀態，把 commit 訊息和 docstring 裡「only a clean exit fills it」那一類的說法拿掉，spec 照 E1 的寫法改就好。

### 其他（Low）

| # | 問題 | 建議 |
|---|---|---|
| E3 | `AskText`（輸入工作目錄的那個視窗）的提示也被改成了「Enter 選擇」。那裡沒有東西可以選，Enter 就是送出打好的文字，所以「選擇」這兩個字反而不對 | `AskText` 改回「Enter 確定」或「Enter 送出」；Q2 說的只是**有預設選項**的那些視窗（`Choose`、`Confirm`） |
| E4 | 驗收草稿（`docs/review/acceptance-draft.md`）的 7.12、7.14 寫的是「狀態列是同一句」「狀態列可能還留著 7.12 的那一句」，Q4 之後就不對了；7.15 說的「勾選框不管勾了沒都畫成 `X`」（Q1）也一樣。這份是 review 自己的文件，這次照指示只 add 了 final-checks.md，沒有改它 | 下一次改草稿時一起改（需要的話我可以接著做） |

## (2) `01588da`：T1 4.2e，只補測試

| 測試 | mutation | 結果 |
|---|---|---|
| `test_merge_cache_notices_a_different_agent`：同樣的兩個來源，先用 opencode merge，再換 claude merge，第二次一定要再叫兩次 AI，不能沿用（模型設定固定住，所以兩次之間只有 agent 的名字不一樣） | 快取鍵拿掉 agent → **被抓到** | ✅ |
| `test_merge_cache_notices_a_different_prompt_version`：把 `SUMMARY_PROMPT_VERSION` 加 1 之後，一定要重寫 | 快取鍵拿掉提示詞版本 → **被抓到** | ✅ |
| `test_a_child_the_cloud_lost_does_not_make_import_branch`：唯一的子 Session（merge）被別台刪掉之後，再匯入同一個來源，就是**原地更新**同一個 id，不會分岔 | `children()` 不看 `cloud_has` → **被抓到**（這個測試和之前的「不擋刪除」那一個都失敗了） | ✅ |

這樣，T1-final 和 T1-archive 列出來「有實作但沒有測試」的 MUST，現在都有測試守著了。

## 歸檔前還有沒有擋的事

### T1（command-batch-actions）

**沒有 review 這一邊擋的事。** T1-archive 的 D3～D7 已經在 `2d37ba2` 改好了（我在 HEAD 上用 grep 確認過 design.md、兩份 spec、`docs/design.md`），4.2d（`bbc7a4a`，見 `T1-4.2d.md`）和 4.2e（`01588da`）也都確認過了。剩下的只有：
- tasks **4.2d** 還沒有打勾（等這份確認）；
- tasks **4.3**：**行數**（HEAD 是 **3,447**，tui 827；要使用者決定）、**P1**（`P1-options.md`，要使用者決定）。

### T2（tui-batch-actions）

- **E1**：spec 那兩句話和 design 5.9，要照 Q3／Q4 之後的行為改寫。**這個擋歸檔**，但只要改文字。
- **E2**：要嘛讓進度條在結束時真的填滿並補測試，要嘛把「填滿」的說法拿掉（和 E1 一起決定就好）。
- tasks **4.3**：行數要使用者決定。PM 試用和 review 都已經完成了。

其他的 Low（E3、E4，以及 T2-archive 列出來的那些）都不擋歸檔。
