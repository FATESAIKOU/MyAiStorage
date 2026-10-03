**High 一個（R1）：`stage` 新加的最後一行，會**無條件**刪掉 `outbox/.done-<ULID>`，可是背景有可能剛把「新版本」的 `outbox/<ULID>` 改名成 `.done-<ULID>`。** 我在副本裡照這個順序重播過：新版本從 outbox 不見了，背景這一輪丟出例外而中斷，Drive 上是舊版，**下一次 sync 之後，本機的鏡像也被蓋回舊版**，那次 edit 就這樣丟了。拿掉那一行，所有的測試照樣通過（那一行本來就不需要）。

另外有 3 個 Medium：
- **R2**：N5「另存成新的 Session」**沒有做**，tasks 2.3、2.6 卻標成已完成；測試斷言的反而是「留在 outbox」。
- **R3**：M4 的測試測不到它要守的東西。
- **R4**：H1 對「只改標頭的 edit」的唯一保護沒有測試。

# Review：T3 local-first-writes 第 2 節（`235e68a`，以修正後的 `ce7bfb6` 為準）

2026-10-03，review。`235e68a` 本身編譯不過（`cache.py` 第 273 行縮排錯誤），所以照 PM 的指示，以修正後的 HEAD `ce7bfb6` 為準（它只改了 `cache.py` 的 7 行，見最後）。

對照 `6046d62` 版的 spec／design，以及 `docs/review/T3-sec2.md` 的 P1～P3。在 `git archive ce7bfb6` 的副本裡：
- `compileall` 通過；
- 全部的單元測試 **480 passed**；
- 探測都是用 pytest 跑的（有 conftest 隔離）；
- mutation 的子程序另外傳了指到 `/tmp` 底下的 `AGORA_CONFIG`／`CACHE_DIR`／`STATE_DIR`／`HOME`。

沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

## T3-sec2 的 P1～P3

| | 結果 |
|---|---|
| P1：「放開鎖之後再檢查一次」的測試 | ✅ `test_it_looks_again_after_letting_the_lock_go`（我的 mutation 裡，它也被其他 mutant 帶出來失敗過） |
| P2：迴圈比對版本 | ✅ `_waiting` 改成 `(ulid, session.md 的 md5)`；mutation「改回只比 ULID」→ **被抓到**（`test_an_edit_that_lands_mid_upload_is_still_sent_by_this_process`） |
| P3：`_save` 和 `push` 拿同一把鎖 | ✅ `_save` 不再自己上傳，改成啟動背景；`push` 用阻塞的 `hold_upload_lock` 等鎖 |
| P5（sync 的說法）、P6（inline 失敗只看自己寫的那一個）、P7、P8 | ✅ 都有處理（P5 改成看鎖：有人拿著鎖 →「背景上傳中」，沒有 →「沒上傳成功」；我實測，剛啟動背景之後說的是「背景上傳中，1 筆」，是對的） |

## R1（High）：`stage` 的最後一行會刪掉背景正在比對的新版本

```python
# stage() 的最後（235e68a 新加的）
shutil.rmtree(paths.outbox / f".done-{ulid}", ignore_errors=True)
```

**重播的順序**（在副本裡用 monkeypatch 讓它照這個順序發生）：
1. 背景已經送完 v1、列完檔，Drive 上 session.md 的 md5 等於這一輪送出的 v1；
2. 這時使用者 edit X：`stage` 把舊的改名到一邊，再把 v2 搬進 `outbox/X`；
3. 背景照規則把 `outbox/X`（**現在是 v2**）改名成 `.done-X`，準備比對；
4. `stage` 跑到最後一行，`rmtree(.done-X)`，**刪掉的是 v2**；
5. 背景的 `_entry_md5s(.done-X)` 讀不到東西，丟出 `HeaderError`，`upload_batch` 沒有接住，這一輪就中斷了。

**實測結果**：「v2 還在 outbox」→ False；Drive 上是第一版；接著做一次 `store.sync`，**本機的 session.md 也變回第一版**（sync 看到索引記的 md5（v2）和 Drive 上的（v1）不同，就把 Drive 上的下載下來）。

窗口很窄（`stage` 的第 2 步到第 4 步之間只有一個 `rmtree(old)`），但結果是使用者的修改悄悄消失，這正是 H1 要防止的事。

**修法**：
- 刪掉這一行。不需要它：`_rename_back`（背景在比對失敗時）和 `_restore_done`（沒有人在跑的時候），遇到 `outbox/X` 已經存在，都已經會把 `.done-X` 丟掉。我的 mutation「拿掉這一行」→ 所有的測試都通過，也證明它沒有被依賴。
- 另外，`upload_batch` 的驗證那一段，每一筆都要接住例外（`OSError`、`HeaderError`），把那一筆算成「留下」，不要讓一筆的問題中斷整輪。

## R2（Medium）：N5「另存成新的 Session」沒有做

- spec（「只有自己驗過的版本離開 outbox」）：「不在 Drive 上的話……MUST NOT 把它傳回去，而是**另存成一個新的 Session**」；Scenario「別台在上傳前刪掉了」的 THEN 也寫了「這次的修改另存成新的 Session Y（parents 指向 X）」；design 第 68 行也一樣。
- 程式（`upload_batch`）：只印一行「雲端沒有，這筆不傳回去；見 push --not-exist-upload／pull --not-exist-delete」，然後把那一筆**留在 outbox**。
- 測試 `test_an_update_someone_else_deleted_is_not_sent_back` 斷言的就是「留在 outbox」（註解寫著 "it stays, for the user to decide"），和 spec 相反。
- tasks 2.3 寫「有記號但 Drive 上沒有的，另存成新的 Session（L7、N5）」，2.6 寫「別台在上傳前刪掉的另存成新的 Session」，兩個都打了勾。

這就是 T3-sec1 N5 說的那種**永遠卡住**的情況：提醒裡給的兩個選擇都走不通（`pull --not-exist-delete` 會拒絕 outbox 裡的；`push --not-exist-upload` 又會被同一條規則擋下），它也不會被標成雲端沒有。

**要做的**：照 spec，把那一筆的內容另存成一個新的 Session（新的 ULID，parents 指向 X，relation 照原本的寫入），移出原本的 outbox 項目；測試改成斷言 Y 出現在 Drive 上、X 沒有被傳回去。tasks 2.3、2.6 的勾在做完之前先拿掉。

## R3（Medium）：M4 的測試測不到它要守的東西

把「原始檔那一次失敗就整輪結束」的 `return` 拿掉（失敗之後照樣傳 session.md），`test_a_failed_raw_upload_sends_no_session_md_at_all` **照樣通過**。原因是它用的是 `FAKE_RCLONE_FAIL=copyto`，這會讓**兩次** copy 都失敗：session.md 那一次本來就傳不上去，所以有沒有那個 `return`，結果都一樣。（tasks 2.8 說這個 mutant 是紅的，但用這個測試的話，它是綠的。）

**修法**：讓假 rclone 只在**原始檔那一批**失敗（例如 `FAKE_RCLONE_FAIL` 比對 `--files-from` 清單的內容裡有沒有 `raw-`，或者 monkeypatch `_copy_batch`，在第一次呼叫時丟出 StoreError），再斷言 Drive 上沒有 session.md。

## R4（Medium）：H1 對「只改標頭的 edit」唯一的保護，沒有測試

驗證那一段有兩個比對：session.md 的 md5（`now_md5 != sent_md5`），和原始檔的 md5。現有的測試（`test_a_version_staged_mid_upload_is_reported_as_still_waiting`），第二版也換了**原始檔**，所以就算拿掉 session.md 那一項，原始檔那一項也會擋下來（我的 mutation：拿掉 session.md 的比對 → **照樣通過**，tasks 2.8 也記成「等價的 mutant」）。

但 spec 的 Scenario「上傳中又改了同一個」是 `edit --header title=新的`，**原始檔不會變**。這時唯一的保護就是 session.md 那一項。

**實測**（只改標頭、原始檔不變的 edit，發生在上傳中途）：
- 原本的程式：新的標題留在 outbox ✅；
- 拿掉 session.md 的比對：`left == []`，新的標題**不見了** ❌。

所以這不是等價的 mutant，而是**少了一個測試**。**修法**：把這個探測（第二版只改標頭、原始檔的 bytes 一樣）加成正式的測試。

## 其他（Low～Low/Medium）

| # | 問題 | 建議 |
|---|---|---|
| R5 | `upload_batch` 讀 `_entry_md5s` 時，只要是 `OSError` 就把那一筆**隔離**到 `.bad`。可是 `OSError` 可能只是剛好碰上 stage 正在換資料夾（`outbox/X` 有一瞬間不在），這時被隔離的，會是剛換進來的那個**健康的**新版本（不會丟，但不會上傳，使用者看到的是「有 1 筆壞檔」） | 只有 `HeaderError`（內容壞了）才隔離；`OSError` 就跳過，算成留下 |
| R6 | `uploader_is_running()` 是靠「真的去拿一下鎖」來判斷的，而互動模式每次重讀、sync、`outbox_ulids` 都會呼叫它。如果剛啟動的背景，剛好在那一瞬間 `_take`，就會以為已經有人在跑，直接結束（「已經有一個上傳在跑」），可是那個短暫拿鎖的人並不會上傳，所以那幾筆要等到下一個指令 | 背景的 `_take` 拿不到時，等一下再試幾次（例如 3 次、每次 100 ms），再判斷是不是真的有人在跑；或者判斷「有沒有人在跑」改看一個 pid 檔，不要去拿鎖 |
| R7 | `push` 是用阻塞的 `flock` 等鎖，所以那段「每 10 秒說一次『背景上傳中，還在等…』」的迴圈，實際上永遠不會執行到（flock 會一直卡到拿到為止）。而且它等的是**整個**背景（包含刪除佇列）結束，design（N2、N8）寫的是「等它給的那幾個 id 離開 outbox」、「每 1 秒看一次」 | 改成非阻塞地試，每 1 秒試一次，每 10 秒說一次；或者照 design，直接等自己那幾個 id 離開 outbox |
| R8 | `remember` 複製原始檔用的是 `wanted.read_text(encoding="utf-8")`，再 `write_atomic`（寫文字）。原始檔應該**逐位元組**複製：文字模式的換行轉換，或者不是 UTF-8 的位元組，都會讓本機那一份的 md5 和標頭不符（不是 UTF-8 的話，`remember` 會直接丟出 `UnicodeDecodeError`，而這時 outbox 已經寫好了） | 用 `shutil.copyfile` 複製到暫存檔，再 `os.replace` |
| R9 | `.update` 記號是在 `stage` 和 `remember` **之後**才寫的。背景如果剛好在這中間讀到這一筆，就會把它當成新的，跳過 L7 的檢查 | 讓 `stage` 收一個 `update` 參數，在把 tmp 換進去**之前**就把記號寫進 tmp（design 也說「`stage` 換掉資料夾時保留這個記號」） |
| R10 | `pull` 遇到刪除佇列裡的，印「正在刪除，不能 pull」之後直接 `return`，所以會被算進「拉下 N 個」，exit 0；`push` 是丟出錯誤（算失敗）。spec 說兩個都 MUST 拒絕 | `pull` 也丟出 StoreError，算成失敗 |
| R11 | 2.4 的「sync 跳過刪除佇列」：把這一項拿掉（列檔看到就照樣拉回來），測試照樣通過。`test_sync_leaves_a_queued_for_deletion_session_alone` 裡排隊的那一個，大概本來就不在 Drive 上，所以 sync 根本不會去拉它 | 測試要讓排隊的那一個**還在 Drive 上**（purge 之前），再斷言 sync 之後索引裡沒有它 |
| R12 | 批次上傳的兩次 copy 都加了 `--ignore-times`，所以只改標頭的 edit，也會把整個原始檔（可能好幾 MB）重新傳一次 | 原始檔那一次改用 `--checksum`（Drive 有 md5，一樣就跳過）；session.md 那一次維持 `--ignore-times` |

## rclone 呼叫次數 ✅

3 個新的 Session，一次 `upload_batch` 的 rclone 呼叫依序是：`mkdir`、`lsjson`（設定檔是新的，第一次查 folder id，每台機器只有這一次）、`copy`（原始檔）、`copy`（session.md）、`lsjson`（驗 md5）。上傳本身是 **3 次**，符合 spec 的「不超過 4 次」。有更新既有 id 的時候會多一次 `lsjson`（spec 寫的 MAY）。

## Mutation（8 個）

| 拿掉的實作 | 結果 |
|---|---|
| M4：原始檔失敗照樣傳 session.md | **沒被抓到**（R3） |
| H1：驗完 Drive 就刪，不再比對 outbox 現在的 session.md | **沒被抓到**（R4：這不是等價的，見實測） |
| 拿掉 `stage` 最後刪 `.done-` 的那一行 | **沒被抓到**，因為那一行不需要，而且有害（R1） |
| N4：不看 `.update` | **被抓到** |
| N4：全部當成更新 | **被抓到** |
| 2.1：鏡像不放原始檔 | **被抓到** |
| P2：迴圈改回只比 ULID | **被抓到** |
| 2.4：sync 不跳過刪除佇列 | **沒被抓到**（R11） |

## `ce7bfb6`（修正語法錯誤的那一個）

把 push 的佇列拒絕移到 `if ulid in staged:` 的前面（語法錯誤就是這兩行插錯了位置），並且加上 pull 的拒絕（R10 的說法）。`compileall` 通過。

## 結論

- 先修 **R1**：刪掉 `stage` 最後那一行，並且讓驗證那一段每一筆都接住例外。
- 再做 **R2**：照 spec 另存成新的 Session，並且拿掉 tasks 2.3、2.6 的勾。
- 補 **R3、R4** 的測試；
- **R5～R12** 可以和第 3 節一起處理。
