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

（2026-10-03 依 review `docs/review/T3.md` 補上 H1、M1～M7 與 L1～L7。）

### 本機完整的一份
- `remember` 除了 `session.md`，也把標頭指到的原始檔放進鏡像，並刪掉同一個 ULID 底下其他的 `raw-*`。
- 同名同大小就不複製；要複製時用原子寫入。因為 sync 每次都會對 outbox 裡的每一筆呼叫 `remember`。
- 替代方案「上傳後從 outbox 搬過去」：上傳失敗時鏡像裡就沒有，所以不採用。
- edit 與 `recover_pending` 的收尾也走 `_save`，所以一樣適用（L2）。

### 背景程序：獨立的入口（M3）
- 用 `[sys.executable, "-m", "agora.background"]` 啟動，不經過 `cli.main()`，所以：
  - 不跑 `recover_pending`；
  - 不印提醒；
  - 不會出現在 `--help`；
  - 也不會在自己裡面再 spawn 自己。
- 啟動參數：
  - `start_new_session=True`、`stdin=DEVNULL`、`close_fds=True`（預設值，不要改掉）。不關的話，continue 收尾時拿著的 pending 鎖會被繼承，`continuing()` 就會一直以為「正在接續」。
  - `env` 原封不動傳下去，包含 `AGORA_FOLDER_NAME`、`AGORA_CACHE_DIR` 等等。
  - stdout 與 stderr 寫到 `<state>/upload.log`。啟動時檔案超過 1 MB，就只留最後 256 KB。
- **一定不能接呼叫端的 stdout**，否則互動模式的讀取端讀不到 EOF（review T2-archive X1 的同一個道理）。
- 測試開關：`AGORA_UPLOAD=inline` 時，不啟動背景程序，在前景同步跑同一個函式。既有的單元測試照常假設「指令結束時 Drive 上已經有了」，用這個開關，改動最少。

### 一把鎖，前景背景共用（M1）
- `<state>/upload.lock` 的 flock。所有會上傳 outbox 的地方都拿它：背景、sync 裡的 `push_outbox`、`push session`。
- 背景與 sync：非阻塞。拿不到就跳過 outbox；sync 的提醒說「背景上傳中，N 筆」（L5）。
- `push session`：阻塞等鎖，60 秒逾時；等的時候在 stderr 說「背景上傳中，等它結束…」。
- **晚到的**：背景放開鎖之後，再看一次 outbox 與刪除佇列，不是空的就再拿一次鎖、再跑一輪。因為 stage 一定先寫 outbox、再啟動背景，所以「正在跑的那一個在放開鎖後看到」與「新啟動的那一個拿得到鎖」至少有一個成立，不會留到下一個指令。

### 只刪自己驗過的版本（H1）
- 每一輪開始時，記下每筆 outbox 項目的 `session.md` md5（這一輪要傳的版本）。
- 驗完 Drive 的 md5 之後，先把 `outbox/<ULID>` 改名成 `outbox/.done-<ULID>`，再比對：
  - 裡面的 `session.md` md5 等於這一輪的、也等於 Drive 上的，才刪掉；
  - 不相符（上傳期間被新版本取代）就改名回去，下一輪再傳。
- `stage` 遇到 `.done-<ULID>` 時，把它當成已經不在：照常建新的 `outbox/<ULID>`。背景改名回去之前，發現 `<ULID>` 已經存在，就直接刪掉 `.done-`，因為新的版本比較新。
- `outbox_ulids` 的啟動清理：沒有人在跑時，留下來的 `.done-<ULID>` 改名回去（再傳一次是安全的）。

### 更新既有 id 前確認還在（L7）
- 這一輪列檔的結果，就是判斷依據；第 3 步的列檔挪到第 1 步之前做，同一次列檔兩邊共用，所以總次數不變。
- 標頭的 `agora.previous_sources` 有值，或索引裡本來就有這個 ULID，代表這是**更新既有 id**。這時如果列檔裡沒有它，就不傳，留在 outbox，提醒時用 `cloud_lost` 的說法。

### 一批的上傳（M4）
1. 列檔一次（`list_sessions`）。
2. 從 outbox 根目錄執行 `rclone copy <outbox> gdrive:sessions --files-from <清單> --no-traverse --ignore-times`，清單是 `<ULID>/raw-….json`。**這一次不是 exit 0，整輪結束**，不傳任何 `session.md`。
3. 同樣的指令再跑一次，清單是 `<ULID>/session.md`。
4. 再列檔一次，驗 md5，決定哪些可以離開 outbox（照 H1 的規則）。
5. 被取代的舊 `raw-*`，用一次 `rclone delete --files-from` 清掉。

- 第 1 步只有在有「更新既有 id」的項目時才需要；沒有的話跳過，就是 spec 說的「不超過 4 次」。
- 兩輪分開還有一個好處：每個新的 ULID 在第一輪只有一個檔，不會因為平行傳輸建出兩個同名的資料夾。
- 「固定幾次 rclone」不等於「固定幾次 API 請求」：每個檔案還是會在 Drive 上查一次。PM 實跑時要用 `-v --stats` 量 API 請求數，不只數 process。

### 背景刪除
- `delete` 在前景做的事：
  - 既有的檢查；
  - `forget_local`，從鏡像與索引移除；
  - 寫墓碑（`<state>/deleted`）；
  - 拿掉 `outbox/<ULID>`（L3）；
  - 加進 `<state>/trash-queue/<ULID>`（空檔即可）；
  - 啟動背景。
- 雲端沒有的 Session 只刪本機，不進佇列（L6）。
- 背景在上傳之後處理佇列：每個 ULID 跑一次 `purge`，沿用 `delete_session` 的 S1-4／S1-4b 判斷；成功的從佇列移除。
  - 仍然是每個 Session 一次 purge。因為使用者不用等，就不改成逐檔刪除加 rmdirs：那樣 Drive 垃圾桶裡看到的是零散的檔案。
- 佇列裡的 ULID：
  - `sync` 列檔看到時，不放進索引，也不算進 `mark_missing`；
  - `pull`、`push`（含 `--not-exist-upload`）拒絕，說「正在刪除」（M5）；
  - 每個指令開頭提醒「有 N 個等著移到 Drive 垃圾桶」（L4）。背景正在跑時不提醒。

### exit code（M2）
- 存進本機並成功啟動背景：exit 0。
- 背景程序啟動失敗（例如 OSError）：exit 3，outbox 照舊。
- 互動模式依同樣的規則說明；不再用 `code == 3` 判斷「已存進 outbox」。

### import 開頭的同步
- 改成 `store.sync(paths, throttle=True)`。主規格 `batch-commands` 的「import 一次多個」有 MODIFIED delta。
- 重新匯入的判斷（`by_source`）看的是本機索引。所以節流期間，如果別台剛匯入同一個來源，會多出一個 Session。這種情況很少，使用者接受「先觀察」。

### 前景仍然會連 Drive 的（L1）
- continue 開始前與結束時、edit 存檔前，寫回既有 id 前的雲端檢查照 T1 的決定，仍在前景（`session-sync`）。
- 這次不改。要改的話，要重新決定 T1-sec3 M1，那是使用者的決定。

### 互動模式
- 子程序（指令模式）一結束，等待視窗就結束。背景是另一個 process group，所以 Esc 的升級停不到它。

### 測試
- 假 rclone 補上「本機 → Drive 的 `copy --files-from`」與 `delete --files-from`。
- 單元測試：
  - 預設 `AGORA_UPLOAD=inline`；
  - 斷言 rclone 的呼叫次數與順序；
  - H1：上傳中途 stage 新版本，新版本留在 outbox，之後也會傳上去；
  - M4：原始檔那一次失敗時，不傳任何 session.md；
  - 一個測試真的啟動 `agora.background`，確認沒有接到呼叫端的 stdout、沒有繼承 pending 鎖、結束後鎖會放開。
- 整合測試：提供 `wait_uploaded()`，等到鎖沒人拿、outbox 與佇列都空了。

## Risks / Trade-offs

- [背景上傳完成之前，別台還看不到] → 要立刻確定的時候用 `push session <id>`；結果視窗與雲端欄都有說明。
- [背景程序失敗沒有人看到] → 下一個連 Drive 的指令會提醒還有幾個沒上傳；細節在 `upload.log`。
- [鏡像多佔一份原始檔的磁碟空間] → 大小和 agent 那邊的 session 差不多；被取代的舊檔會清掉。
- [import 的節流同步可能讓「重新匯入」多出一個] → 很少發生，使用者接受。
