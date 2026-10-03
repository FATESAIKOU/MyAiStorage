**沒有 High。** `23db95e` 修對了：H1、H3、T1-final 的 (2)（兩條 spec MUST 的測試）和 D2（README），4 個 mutation 都抓得到；整合測試的改寫也符合 spec。

歸檔前還剩（不含行數）：
- **(a)** 幾處文件和規格文字還沒對齊：D3、D4、D5、D6、D7，見下面的清單；
- **(b)** tasks 4.3 的 PM 驗收。

其餘都是 Low，可以在歸檔之後再處理。

# Review：T1（change command-batch-actions）歸檔前的確認

2026-10-03，review。對象是 `23db95e`；HEAD 是 `169fff7`，T1 的程式在 `23db95e` 之後就沒有再改過。

在 `git archive HEAD` 取出的副本跑**全部的**單元測試：**428 passed**。4 個 mutation 都只在副本裡做。沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。

## 修正確認

| 項目 | 改了什麼 | mutation（拿掉對應的實作） | 結果 |
|---|---|---|---|
| T1-final (2)：雲端沒有的不算成子 Session | `test_a_session_the_cloud_lost_is_not_a_child_that_blocks_its_parent`：parent 的子 Session（一個 merge）被別台刪掉了，`delete parent --yes` 照樣 exit 0；被刪的子 Session 還在本機，而且有標記 | `children()` 不看 `cloud_has` → **被抓到** | ✅ |
| T1-final (2)：正在接續的不被標記 | `test_a_session_being_continued_is_not_marked`：用真的 `flock` 拿著 pending 的鎖，sync 之後**沒有**標記；放開鎖之後再 sync，**有**標記（G1 和 Q2 兩邊都測到了） | `mark_missing` 不排除 `continuing` → **被抓到** | ✅ |
| H1：中斷的接續在**離線**時收尾 | `test_an_interrupted_continue_finished_offline_still_keeps_the_work`：留下來的記錄 → 別台刪掉 X → 完整同步，X 被標記 → `lsjson` 失敗（離線）→ `search` 觸發收尾 → 另存成 Y（parents 指向 X），X **沒有**回到 Drive | `_finish` 不看標記 → **被抓到**（mutant 在收尾時 copyto 成功，X 回到了 Drive） | ✅ |
| H3：edit 開著編輯器的時候被刪掉 | 刪除的動作搬進了假的 `editor()` 裡；開始的時候先斷言 Drive 上還有 X | 拿掉存檔前的那一次 `_refuse_if_gone` → **被抓到**（mutant 會走到 `fetch_raw`，exit 2，不是 1） | ✅ |
| H2：F6 第二個測試的 docstring | 改成照實說明：那個 Session 在標記之前就被移到 `.bad` 了，這個測試不是 mutation 的守門員 | — | ✅ |
| D2：README | 在 pull／push 那一段補上：雲端沒有的 Session、`--filter cloud=no\|yes`、兩個 flag、outbox 和接續中的不算、continue／edit 會拒絕、delete 只刪本機、agent id 的意思、沒有 `sessions/` 時拒絕 | — | ✅ 和 spec、程式都一致 |
| D8：tasks 4.2b 的敘述 | 改成「`mirror_one` 目前只有 pull 用，sync 還是自己的一份」 | — | ✅ |

## 整合測試的改寫 ✅

`tests/integration/test_store_drive.py`：`test_deleted_on_drive_disappears_from_other_mirror` 改成 `test_deleted_on_drive_stays_here_marked_not_gone`。

- **做法**：機器 a 上傳 → 機器 b 同步 → 用 rclone 直接在 Drive 上 purge → 機器 b 再做一次完整同步。
- **斷言**：b 的索引裡還有標頭；`b.mirror/<ulid>/session.md` 還在；`missing_in_cloud() == [ulid]`；搜尋得到。
- **和 spec「雲端沒有的 Session 保留在本機並標記」逐條對照**：「列檔完整成功時」標記 ✅（purge 之後的同步是完整的列檔）、「鏡像與索引 MUST 保留」✅（兩個都斷言了）、Scenario「別台機器刪掉了 → X 還在這台的索引與鏡像裡，標成雲端沒有」✅。
- **另外**：新增了 `created.append(ulid)`，這樣就算測試在 purge 之前就失敗，teardown 也會清掉；teardown 的 `purge` 沒有加 `check=True`，所以 X 已經被 purge 過的時候，也不會讓 teardown 失敗 ✅。
- 「只有 `pull --not-exist-delete` 才會刪掉」這一半，docstring 說是在 `test_pull_push.py` 裡測的（4.2 的紀錄也是這樣寫）；這次我沒有跑整合測試，所以沒有確認。

## 歸檔前還剩什麼（不含行數）

### 要做（文件和規格的文字，都是小改動）

| # | 位置 | 現在寫的 | 應該是 |
|---|---|---|---|
| D3 | change 的 `design.md` Decisions「寫回既有 id 前檢查」 | 「continue、edit、import 的更新路徑在寫之前**看標記**」 | 看標記**加上**當下的 Drive（continue／edit 開始前、continue 結束時、edit 存檔前）；import 不會更新它，改建一個新的；中途被刪就另存成 Y（3.7～3.10 的決定）。這份 design 會跟著一起歸檔 |
| D3 | 同一份的 Risks | 「判斷雲端沒有**只用這次的列檔結果，不額外呼叫**」 | continue／edit 會再完整列檔一次（M1，使用者接受的代價） |
| D4 | `docs/design.md` 5.6（delete） | 沒有提到 | 正在接續（有人拿著鎖）的拒絕；留下來的記錄照刪，並提示；Drive 上已經沒有的當成刪掉，但列檔也失敗時不算（S1-4／S1-4b） |
| D4 | `docs/design.md` 5.10 | 「接續中（pending）的也不算」 | 「正在接續（有人拿著 pending 的鎖）的不算；留下來的記錄不算」；另外補上 G3（沒有 `sessions/` 時，`--not-exist-delete` 拒絕）和 F5（agent 的清單讀不到時不刪快取）。README 已經寫了，design 反而沒有 |
| D5 | `specs/session-sync/spec.md`「push 只寫回該寫的檔案」 | 「同名覆蓋，Drive 上多的不刪」 | 從 outbox 送出時（`push_one`）會刪掉被取代的舊 `raw-*`（N8），所以要寫成「同名覆蓋；送 outbox 時會清掉被取代的舊原始檔，其他多的不刪」 |
| D6 | 同一份的 Scenario「沒給 id」 | 「執行 `agora pull`」 | 「執行 `agora pull session`」（只打 `agora pull` 得到的是「型態要寫 session」） |
| D7 | `specs/batch-commands/spec.md`「進度」的例子 | `[agora] 來源 1/3：…` | `[agora] 來源 1/3  agora:…`（實際的格式；T2 的進度解析也是照這個格式寫的） |
| — | `specs/session-sync/spec.md` 第 34 行「接續中（pending）」 | | 建議改成「正在接續（pending 的鎖有人拿著）」，和 G1 一致（選擇性） |

### 要做（流程）

- tasks **4.3**：「review 審程式；PM 驗收」（行數由使用者決定）。PM 驗收可以用 `docs/review/acceptance-draft.md` 的第一段。

### 可以歸檔之後再處理（Low，記下來）

| 來源 | 項目 |
|---|---|
| T1-final | merge 的要約快取，鍵含 **agent** 和**提示詞版本**的那兩項，還是沒有測試（mutation 拿掉之後照樣通過） |
| T1-sec3 H4 | `continuing()`：在 `exists()` 和 `open()` 之間檔案剛好被刪掉時，`FileNotFoundError` 會被當成「有人拿著鎖」，那一次會誤判成「正在接續」 |
| T1-sec3 H5 | 刪掉有留下記錄的 Session 之後，那筆記錄還是每個指令都會重試；如果有一天收尾成功了，會冒出一個新的 Y |
| T1-sec3 H6／G2 | 一批 delete 裡，只要有一個正在接續，整批都不刪（有子 Session 的那種只是略過那一個） |
| T1-sec3 F10、F11 | 另存 Y 的時候當掉，會多出一份；結束的時候剛好離線，之後推 outbox 時沒有再檢查一次 |
| T1-sec3 L3 | `--filter cloud=` 的值沒有驗證（`cloud=maybe` 會被當成 yes） |
| T1-final D8 | sync 還是保留自己那一份 G3 的邏輯（tasks 已經照實寫了） |
| — | 「雲端沒有的不算子 Session」的另一半，也就是「不觸發 import 的分岔」，沒有單獨的測試（新測試只測了「不擋刪除」）。兩邊用的都是同一個 `children()`，所以 mutation 一樣會被抓到，風險低 |

## 這次讀過、跑過的東西

`23db95e` 的全部 diff（README、tasks、整合測試、`test_cli.py`、`test_store_more.py`）；`039c4cc` 的 spec 修改；HEAD 的 change design.md、兩份 spec、`docs/design.md` 5.6／5.10 裡相關的句子（用 grep 查）；整合測試的 `two_machines` fixture（讀過，**沒有跑**）。4 個 mutation 都被抓到。單元測試 428 passed。沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。
