**沒有 High。K1～K4 都修好了，7 個 mutation 全部抓得到。T4 可以歸檔。**

和 T4 之前（`46b04a5^`）比，還剩三個差別：一個是改善（K5：搜得到 assistant 說的話），兩個是 Low，要不要接受由 PM 決定（Z1：opencode 的逾時也接受小數；Z2：export 的標題遇到多段文字時用換行接起來）。如果要照 proposal 嚴格地「行為不變」，這兩項就要在 proposal 裡列成例外。

# Review：T4 的 K1～K4 修正（`af1d869`）

2026-10-03，review。在 `git archive` 取出的三份副本（`46b04a5^`、`46b04a5`、`af1d869`）裡，跑同一支探測程式。這支程式在 pytest **外面**跑，所以照 10-03 的規則，先把 `AGORA_CONFIG`、`AGORA_CACHE_DIR`、`AGORA_STATE_DIR` 指到 `/tmp` 底下，`AGORA_RCLONE` 指到假的 rclone，`AGORA_FOLDER_NAME=agora-test`，而且程式一開始就檢查這些有沒有設；Claude 的資料也放在 `/tmp` 底下自己編的 session 裡。mutation 也在副本裡做，並且傳同一組隔離變數。沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

單元測試（`af1d869`）：**463 passed**。commit 訊息寫的是 480，可能是因為工作目錄裡有還沒 commit 的 T3 測試；我量的是 `git archive af1d869`。

> 附註：我第一次跑的時候，把探測用的 `AGORA_FOLDER_NAME=agora-test` 和 `AGORA_RCLONE` 也一起傳進了 pytest，結果 52 個失敗（假 Drive 的路徑變成了 `agora-test/`）。拿掉之後就是全部通過。這兩個變數只給 pytest 以外的探測用；pytest 自己有 conftest 做隔離。

## 三個版本，同一支探測程式

| 情況 | T4 之前 | `46b04a5` | `af1d869` | |
|---|---|---|---|---|
| A：第一行是 `/model` → export 的標題 | `把 CSV 轉成表格` | `<command-name>/model</command-name>` | `把 CSV 轉成表格` | ✅ K1 |
| A：清單的標題 | `把 CSV 轉成表格` | `把 CSV 轉成表格` | `把 CSV 轉成表格` | ✅ |
| A：`last_message`（assistant 那一行裡，巢狀的 type 排在前面） | `('assistant', '好的，三個步驟')` | `('user', '把 CSV 轉成表格')` | `('assistant', '好的，三個步驟')` | ✅ K2 |
| A：`search_text('步驟')` | `[]` | `[]` | `[A]` | 改善（K5） |
| D：user 先說話，後面有兩個 summary → export | `摘要一` | `摘要一` | `摘要一` | ✅ |
| D：清單 | `先說的話` | `摘要二` | `先說的話` | ✅ K4（照舊的規則） |
| B：user 的一則訊息有兩段文字 → export | `第一段第二段` | `第一段\n第二段` | `第一段\n第二段` | ⚠️ Z2 |
| B：清單 | `第一段\n第二段` | `第一段\n第二段` | `第一段\n第二段` | ✅ |
| claude 的 summarize：`AGORA_SUMMARIZE_TIMEOUT=1.5` | 1.5 | 600 | 1.5 | ✅ K3 |
| claude 的 summarize：`=abc` | **丟出 ValueError** | 600 | 600 | ✅ B1（proposal 允許的改變） |
| opencode 的 summarize：`=1.5` | 600 | 600 | **1.5** | ⚠️ Z1 |
| opencode 的 CLI：`AGORA_OPENCODE_TIMEOUT=1.5` | 60 | 60 | **1.5** | ⚠️ Z1 |

### Z1（Low）：opencode 的逾時現在也接受小數

T4 之前，opencode 的 `_seconds` 用的是 `int()`，所以 `"1.5"` 會退回預設值（CLI 60 秒、summarize 600 秒）。現在共用的 `env_seconds` 是 `float()`，`"1.5"` 就是 1.5 秒。K3 修的是 claude 那一邊（本來就接受小數）；opencode 這一邊是**新的**行為。

結果比較合理（兩邊一致了），訊息也用 `%g` 印，所以整數還是印成「60 秒」。但嚴格來說，這不在 B1、B3 的範圍裡。**建議**：在 proposal 寫一句「兩邊的逾時都接受小數秒（B1 的一部分）」就好，不用改程式。

### Z2（Low）：export 的標題遇到多段文字時用換行接起來

舊的 export 是用 `_user_text`（用 `""` 接起來，再 strip）；K4 說「回到原本的兩套規則」，可是 user 那一部分現在走的是 `_title_of` → `_line_text`（用 `"\n"` 接起來）。所以，一則由好幾段 text block 組成的 user 訊息，匯入之後的標題中間會多一個換行（`第一段\n第二段`，以前是 `第一段第二段`）。清單那一邊本來就是用換行接的，所以現在兩邊反而一致了。

這只影響「第一則 user 訊息由好幾個 text block 組成」的 Session，而且標題還是會截到 60 字。**建議**：接受（在 proposal 寫一句），或者 `_session_title` 在 user 那一部分改回用 `""` 接。

## Mutation（7 個，全部被抓到）

| 拿掉的修正 | 抓到它的測試 |
|---|---|
| K1：`_title_of` 不看 noise | `test_a_local_command_is_not_the_sessions_title` |
| K2：種類改回「regex 找到的第一個 `type`」 | `test_a_nested_type_before_its_own_still_counts_as_that_kind` |
| K3：`env_seconds` 只收整數 | `test_summarize_timeout_still_reads_a_fraction` |
| K4：export 拿掉「summary 優先」 | `test_export_basic` |
| K4：清單讓 summary 蓋過先出現的 user（C2 的規則） | `test_list_sessions_reads_dir_title_and_stamp` |
| B3：keyword 那一邊不做 NFKC | `test_search_ignores_case_and_full_width`（現在也測了全形的 keyword） |
| `warn` 少了 `[agora] ` | `test_warn_is_marked_the_way_every_agora_line_is` |

T4.md 裡沒被抓到的那兩個（B3 的 keyword、`warn` 的前綴），現在都有測試守著了。

## 行數（算法同 T1-size）

| | T4 之前 | `46b04a5` | `af1d869` |
|---|---:|---:|---:|
| opencode | 538 | 511 | 512 |
| claude | 442 | 417 | 418 |
| base | 63 | 77 | 77 |
| **adapters** | **1,043** | **1,005** | **1,007**（−36） |

## 結論

K1～K4 都修好了，mutation 也都抓得到；K5（搜不到 assistant 說的話）也一起修好了。T4 可以歸檔。歸檔之前，建議在 proposal 的「例外」裡補上 Z1、Z2（或者決定 Z2 要不要改回去）。
