**沒有 High。** T1、T2、T4 都修好了，6 個 mutation 全部被抓到。一般會遇到的各種讀不了的 client 檔，都不會把內容帶到畫面、stdout／stderr 或例外訊息：
- 不是 UTF-8（UTF-16 有 BOM、UTF-16 沒有 BOM、二進位檔）；
- JSON 壞掉、形狀不對；
- 沒有讀取權限、是目錄、symlink 迴圈。

**但「任何」還差兩個（U1，Low～Medium）**：
- **極深的巢狀 JSON** 會丟 `RecursionError`，沒被接住。首次設定當掉時，Textual 會印出區域變數，裡面有**完整的假 secret**；
- **路徑寫成 `~不存在的使用者/…`** 會丟 `RuntimeError`，首次設定也會當掉（這個不會洩漏內容）。

兩個都是人為造出來的情況，一行就能修好。修好之後，T5 就可以收尾。

# Review：`3d2fb86`（T5 review 的 T1、T2、T4）

2026-10-03，review。
- 在 `git archive 3d2fb86` 的副本裡跑：`compileall` 通過，單元測試 **508 passed**。commit 訊息寫的是 510，可能量的是含有別人改動的工作目錄。
- 探測和 mutation 都用 pytest 跑（有 conftest 的隔離），子程序另外傳了 `/tmp` 底下的 `AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR`／`HOME`。
- 所有的 client 檔都是自編的，假 secret 是 `GOCSPX-FAKE-REVIEW-ONLY`。
- 沒有讀使用者真的 client 檔和 `rclone.conf`，沒有碰 Drive，也沒有讀任何真實的 Session。

判斷有沒有洩漏，我查四種形式：完整的 secret、字母之間夾真的 NUL 字元、字母之間夾 `\x00` 這四個字（repr 印出來的樣子），以及 secret 的前 12 個字。

## `read_client` 直接讀

| 檔案 | 結果 | 有沒有印出任何東西 |
|---|---|---|
| Google 的 Desktop JSON（`installed`） | ✅ 讀到 | 沒有 |
| 兩行 `Client-ID = …`／`SECRET = …` | ✅ 讀到 | 沒有 |
| UTF-8 加 BOM | ✅ 讀到（T4） | 沒有 |
| `web` 類型 | None（T4：不收） | 沒有 |
| UTF-16（有 BOM） | None | 沒有 |
| UTF-16LE 沒有 BOM（JSON 和兩行格式各一個；這種 bytes 剛好是合法的 UTF-8，靠解析失敗擋下來） | None | 沒有 |
| 二進位檔（PNG 開頭，後面接 secret） | None | 沒有 |
| JSON 壞掉（secret 在裡面） | None | 沒有 |
| `{"installed": [secret]}`、`{"installed": "x", "web": {…}}` | None | 沒有 |
| 權限 000 | None | 沒有 |
| 目錄、symlink 迴圈 | None | 沒有 |
| **巢狀 20 萬層的 JSON**（secret 放在前面） | ❌ **丟出 `RecursionError`** | 例外訊息本身沒有帶內容 |
| **`~nosuchuser_review/x.json`** | ❌ **丟出 `RuntimeError`**（`Could not determine home directory.`） | 沒有 |

## 在首次設定的畫面裡（`_first_run`，選第二項，打路徑，然後選「用內建的 client」）

| 檔案 | 結果 |
|---|---|
| UTF-16、權限 000、JSON 壞掉、二進位檔 | ✅ 不會當掉。視窗寫「這個檔案裡沒有 client_id／client_secret…」，給三個選項；選「用內建的」之後，`authorize` 收到 `None`。畫面和 stdout／stderr 都沒有 secret |
| 目錄 | ✅ 寫「找不到這個檔案。」（說法有點不準：它其實存在，只是一個目錄。Low，可以不改） |
| **巢狀的 JSON** | ❌ **當掉**：`WorkerFailed: … RecursionError(…)`。Textual 印的 traceback 裡有 `text = '{"installed":{"client_secret":"GOCSPX-FAKE-REVIEW-ONLY","x":[[…'`。也就是說，**完整的假 secret 出現在終端機上**（區域變數截在 80 個字，而 secret 剛好在前 80 個字裡） |
| **`~nosuchuser_review/x.json`** | ❌ **當掉**：`WorkerFailed: … RuntimeError('Could not determine home directory.')`。這一次是首次設定在 `Path(typed).expanduser().is_file()` 那一行就丟出來的，還沒讀檔，所以沒有洩漏，但畫面直接結束了 |

## U1（Low～Medium）：還有兩種例外沒被接住

1. `read_client` 的 `except (OSError, ValueError, TypeError, AttributeError)` 漏了 `RecursionError`。它屬於 `RuntimeError`，`json.loads` 遇到很深的巢狀時會丟出來。
2. 首次設定在 `read_client` **之外**，自己呼叫了 `Path(typed).expanduser()`。遇到 `~不存在的使用者` 會丟 `RuntimeError`，而 `read_client` 裡面的 `expanduser` 也一樣沒被接住。

兩種都不是真正的 client 檔會長的樣子，可是 PM 的要求是「**任何**讀不了的 client 檔」，而第 1 種會把完整的 secret 印出來。

**建議**（兩處都很小）：
- `read_client` 改成 `except Exception: return None`。這個函式的回答本來就只有「tuple 或 None」；`MemoryError`、`RecursionError` 都屬於 `Exception`；
- 首次設定判斷「找不到這個檔案」的那一行，改用 `os.path.expanduser`（遇到不認得的使用者不會丟例外，只會原樣回傳），或者包在 `try` 裡，當成找不到；
- 補一個測試：巢狀 20 萬層、secret 放在前面的 JSON，斷言回傳 `None`，而且 `capfd` 什麼都沒有。

## README（T2）

✅ 不再教人把 secret 打在命令列上。改成：
- 優先用首次設定的第二個選項；
- 手動的方式，讓 `python3 -c` 從下載的 JSON 讀進 `ID`、`SECRET` 兩個變數，歷史紀錄裡只有 `$SECRET` 這幾個字；
- 最後 `unset`，並提醒把 JSON 移走（值已經在權限 600 的 `rclone.conf` 裡）；
- 這兩行 `python3 -c` 讀檔失敗時，Python 印的是錯誤的 `str()`（例如 `'utf-8' codec can't decode byte 0xff in position 0`、`KeyError: 'installed'`），不會帶出檔案內容。

兩個小地方（Low）：
- 讀檔失敗時，`ID`／`SECRET` 會是空的，`rclone config create … client_id="" client_secret=""` 就會**悄悄用內建的 client**，使用者以為設好了自己的。建議在 `rclone` 那一行前面加 `[ -n "$ID" ] && [ -n "$SECRET" ] &&`；
- 「在首次設定選第二個項」，應該是「第二項」。

D5 和 README 的其他改動：
- 搬家的步驟補上了「換成新的 `rclone.conf`」✅；
- 「第一個選項仍是內建的；第二個是自己的」✅；
- 不再說兩行格式是 rclone 的 ✅；
- 寫明 `web` 不收 ✅。

## Mutation（6 個，全部被抓到）

只改 `tui.py`，跑 T5 的 11 個測試：

| 改了什麼 | 抓到它的測試 |
|---|---|
| 只接 `OSError` | `test_a_client_file_that_is_not_utf8_falls_back_without_saying_what_was_in_it` |
| 讀不到時把 `repr(e)` 印到 stderr | 同上（現在測試斷言的是「什麼都沒印」，不只是「沒有 secret」） |
| 又收 `web` | `test_only_a_desktop_client_is_accepted` |
| BOM 不處理（`utf-8` 而不是 `utf-8-sig`） | `test_a_utf8_bom_is_not_a_reason_to_refuse_the_client_file` |
| 讀不到就直接用內建的，不給重試 | `test_a_client_file_we_cannot_read_falls_back_to_rclones_own`、`…_not_utf8_…` |
| 「找不到這個檔案」和「內容不對」說成同一句 | `test_a_client_file_we_cannot_read_falls_back_to_rclones_own` |

## 結論

T1、T2、T4 都修好了，常見的各種讀不了的檔案都不會洩漏，mutation 也都抓得到。剩下的 U1 是 `except Exception` 加上 `os.path.expanduser` 兩個小改動，加一個測試。**修好 U1 就可以收尾 T5**，那個修正我可以快速再看一次。README 的兩個 Low 可以順便改。

還要請 PM 跟使用者確認的（T5.md 提過）：10-03 那次的同意畫面是不是正式版。

---

# 補看：`598981a`（U1）

2026-10-03，review。
- 在 `git archive 598981a` 的副本裡跑：`compileall` 通過，單元測試 **522 passed**。
- 用的是和上面同一份探測（自編的假 secret `GOCSPX-FAKE-REVIEW-ONLY`，四種洩漏形式都查）。
- 隔離方式相同；沒有碰 Drive，也沒有讀使用者真的 client 檔和 `rclone.conf`。

**U1 修好了，T5 可以收尾。**

| 情況 | 改之前（`3d2fb86`） | `598981a` |
|---|---|---|
| `read_client`：巢狀 20 萬層的 JSON | 丟出 `RecursionError` | `None`，什麼都沒印 |
| `read_client`：`~nosuchuser_review/x.json` | 丟出 `RuntimeError` | `None`，什麼都沒印 |
| 首次設定：巢狀的 JSON | **當掉，完整的假 secret 出現在 Textual 的 traceback 裡** | 不會當掉；視窗寫「這個檔案裡沒有 client_id／client_secret…」，選「用內建的」之後，`authorize` 收到 `None`。畫面、stdout／stderr **都沒有** secret（四種形式都查了） |
| 首次設定：`~nosuchuser_review/x.json` | 當掉 | 不會當掉；視窗寫「找不到這個檔案。」，退回內建的 |
| 上面那 15 種讀不了的檔案（UTF-16、權限 000、目錄、壞掉的 JSON……）重跑一次 | — | 全部一樣是 `None`，沒有輸出 |

做法：
- `read_client` 改成 `except Exception: return None`；
- 讀檔前用 `os.path.expanduser`（遇到不認得的使用者不會丟例外）；
- 首次設定判斷「找不到這個檔案」改用新的 `_is_file`（`os.path.isfile(os.path.expanduser(...))`，不會丟例外）。

Mutation（2 個，都被抓到）：

| 改回去 | 抓到它的測試 |
|---|---|
| `except` 改回原本的列舉（不含 `RecursionError`） | `test_a_client_file_nested_deep_enough_to_exhaust_the_parser` |
| `_is_file` 改回 `Path(where).expanduser().is_file()` | `test_a_path_naming_a_user_that_does_not_exist_is_not_a_crash` |

兩個小地方（不擋收尾）：
- 新的註解寫「`os.path.expanduser` raises RuntimeError for `~someone-who-does-not-exist`」，其實丟例外的是 `Path.expanduser`，`os.path.expanduser` 會原樣回傳（docstring 裡 `_is_file` 那一段寫對了）；
- 兩行格式那一行，又加回了「rclone writes `Client-ID = …`」這個註解（T4 說過 rclone 沒有這種格式）。

改一下註解就好。另外，`598981a` 也改了互動模式的結果視窗（新的測試 `test_the_result_window_does_not_promise_a_background_delete_that_is_not_queued`，看起來是 T3-sec6 的 W5），那一部分不在這次的範圍內，之後和 T3 的補看一起看。
