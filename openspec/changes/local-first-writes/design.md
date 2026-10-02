## Context

寫入現在是這樣做的（`store.py`、`cli.py`）：

- **import 一個 Session**：`stage` 寫進 outbox，`remember` 只把 `session.md` 複製進鏡像，`push_one` 依序跑「copyto 原始檔 → copyto `session.md` → `list_one` 驗 md5」，然後刪掉 outbox。import 開頭還有一次不節流的 `sync`（`push_outbox` 加上 `lsjson -R`）。
- **delete**：在前景對每個 Session 跑一次 `purge`，失敗時再列檔確認（S1-4b）。

每次 rclone 沒被擋時 1～2 秒，被 rclone 內建 client 的共用配額擋下時約 50 秒（issue #11）。

## Goals / Non-Goals

**Goals：**
- 使用者執行 import、continue、merge、delete 時，不必等 Drive。
- 一批只連固定幾次 Drive。
- 在這台寫的 Session，本機有完整的一份，所以救得回來。

**Non-Goals：**
- 換回自己的 OAuth client（issue #11）。
- 常駐的 daemon；也不做「上傳完成」的系統通知。
- 改變「雲端沒有」的判斷規則。

## Decisions

### 本機完整的一份
- `remember` 除了 `session.md`，也把標頭指到的原始檔複製進鏡像，並刪掉同一個 ULID 底下其他的 `raw-*`。
- 替代方案是「上傳後從 outbox 搬過去」，但那樣上傳失敗時鏡像裡就沒有，所以不採用。

### 背景程序
- 用 `[sys.executable, "-m", "agora.cli", "_upload"]` 啟動，參數 `start_new_session=True`、`stdin=DEVNULL`，stdout 與 stderr 寫到 `<state>/upload.log`（附加寫入）。
- **一定不能繼承呼叫端的 stdout**，否則互動模式的讀取端讀不到 EOF，等待視窗會一直等到背景結束（review T2-archive X1 的同一個道理）。
- `_upload` 是內部入口：不出現在 `--help`；測試與 `push` 也可以在前景直接呼叫同一個函式。

### 鎖與「晚到的」
- `_upload` 拿 `<state>/upload.lock` 的 flock（非阻塞）。拿不到就代表已經有一個在跑，直接結束。
- 拿到鎖之後用迴圈處理：做完一輪，再看一次 outbox 與刪除佇列，空了才放開鎖結束。
- 晚到的 Session 有一個空檔：它在 `_upload` 最後一次檢查之後、放開鎖之前才進 outbox，而那時新啟動的 `_upload` 拿不到鎖。這種情況交給下一個連 Drive 的指令補傳，和背景失敗的處理相同。

### 一批的上傳
- 從 outbox 根目錄執行 `rclone copy <outbox> gdrive:sessions --files-from <清單> --no-traverse`，清單裡是 `<ULID>/raw-….json`。
- 再執行一次同樣的指令，清單裡是 `<ULID>/session.md`。
- 接著做一次 `list_sessions`（`lsjson -R --fast-list --hash`），依每個 ULID 的 md5 決定哪些可以離開 outbox。
- 被取代的舊 `raw-*`，用一次 `rclone delete --files-from` 清掉。
- `--no-traverse` 避免列出整個目標資料夾。
- 替代方案「`rclone copy --order-by name` 一次傳完」沒辦法保證 `raw-*` 一定在 `session.md` 之前，所以不採用。

### 背景刪除
- `delete` 在前景做三件事：既有的檢查、`forget_local`（從鏡像與索引移除）、寫墓碑（現在的 `<state>/deleted`）。另外把 ULID 加進 `<state>/trash-queue/<ULID>`（空檔即可）。
- `_upload` 在上傳之後處理佇列：對每個 ULID 跑 `purge`，沿用 `delete_session` 的 S1-4／S1-4b 判斷；成功的就從佇列移除。
- 背景刪除仍然是每個 Session 一次 purge。因為使用者不用等，就不為了少幾次呼叫，改成逐檔刪除加 rmdirs：那樣 Drive 垃圾桶裡看到的是零散的檔案，就失去「整個資料夾還原」的好處。
- `sync`：在 `trash-queue` 裡的 ULID，列檔看到時不放進索引，也不算進 `mark_missing`。

### import 開頭的同步
- 改成 `store.sync(paths, throttle=True)`。
- 重新匯入的判斷（`by_source`）看的是本機索引，所以節流期間別台剛匯入同一個來源的話，會多出一個 Session。這個情況很少，使用者接受「先觀察」。

### 互動模式
- 子程序（指令模式）一結束，等待視窗就結束。背景上傳是另一個 process group，所以 Esc 的升級停不到它。
- 結果視窗的說明照 spec 的寫法。

### 測試
- 假 rclone 補上「本機 → Drive 的 `copy --files-from`」與 `delete --files-from`。
- 單元測試：直接呼叫上傳函式（不經過背景程序），斷言 rclone 的呼叫次數與順序；另外有一個測試真的啟動 `_upload`，確認它沒有接到呼叫端的 stdout、而且會在結束後放開鎖。
- 整合測試：import 之後在前景呼叫一次 `_upload`（或等它結束），才檢查 Drive。

## Risks / Trade-offs

- [背景上傳完成之前，別台還看不到] → 要立刻確定的時候用 `push session <id>`；結果視窗與雲端欄都有說明。
- [背景程序失敗沒有人看到] → 下一個連 Drive 的指令會提醒還有幾個沒上傳；細節在 `upload.log`。
- [鏡像多佔一份原始檔的磁碟空間] → 大小和 agent 那邊的 session 差不多；被取代的舊檔會清掉。
- [import 的節流同步可能讓「重新匯入」多出一個] → 很少發生，使用者接受。
