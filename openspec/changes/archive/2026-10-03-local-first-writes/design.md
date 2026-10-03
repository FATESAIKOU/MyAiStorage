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

（2026-10-03 依 review `docs/review/T3.md` 補上 H1、M1～M7 與 L1～L7；再依 `T3-sec1.md` 補上 N1～N10，N5 由使用者決定。）

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
- 測試開關：`AGORA_UPLOAD=inline` 時，不啟動背景程序，在前景同步跑同一個函式（上傳與刪除佇列都處理）。既有的單元測試照常假設「指令結束時 Drive 上已經有了」，用這個開關，改動最少。
- 啟動 helper 回傳三種結果（N1）：已啟動背景／已在前景傳完（inline）／失敗。inline 時上傳失敗、或背景啟動失敗，都回 exit 3，訊息照舊（既有測試 `test_upload_failure_exits_3_and_stays_searchable` 等不變）。

### 一把鎖，前景背景共用（M1）
- `<state>/upload.lock` 的 flock。所有會上傳 outbox 的地方都拿它：背景、sync 裡的 `push_outbox`、`push session`。
- 背景：非阻塞，拿不到就結束。
- 指令開頭的 sync（N3）：**不在前景上傳**。outbox 或刪除佇列不是空的，就透過 helper 啟動背景；拿不到鎖代表已經有一個在跑，什麼都不做。提醒說「背景上傳中，N 筆」（L5）。
- `push session`（N2、N8）：等的是「它給的那幾個 id 離開 outbox、Drive 上 md5 對了」，不是背景整個結束。拿得到鎖就自己用批次上傳送；拿不到就每 1 秒看一次，每 10 秒在 stderr 說「背景上傳中，還在等…」。不設逾時，Ctrl-C 可以中斷。
- **晚到的**：背景放開鎖之後，再看一次 outbox 與刪除佇列，不是空的就再拿一次鎖、再跑一輪。因為 stage 一定先寫 outbox、再啟動背景，所以「正在跑的那一個在放開鎖後看到」與「新啟動的那一個拿得到鎖」至少有一個成立，不會留到下一個指令。

### 只刪自己驗過的版本（H1）
- 每一輪開始時，記下每筆 outbox 項目的 `session.md` md5（這一輪要傳的版本）。
- 驗完 Drive 的 md5 之後，先把 `outbox/<ULID>` 改名成 `outbox/.done-<ULID>`，再比對：
  - 裡面的 `session.md` md5 等於這一輪的、也等於 Drive 上的，才刪掉；
  - 不相符（上傳期間被新版本取代）就改名回去，下一輪再傳。
- `stage` 遇到 `.done-<ULID>` 時，把它當成已經不在：照常建新的 `outbox/<ULID>`。背景改名回去之前，發現 `<ULID>` 已經存在，就直接刪掉 `.done-`，因為新的版本比較新。
- `outbox_ulids` 的清理（N6）：先非阻塞地試拿 `upload.lock`，拿得到（沒有人在跑）才把留下來的 `.done-<ULID>` 改名回去（再傳一次是安全的）。
- 交錯（N7）：
  - 改名 `outbox/X` → `.done-X` 時 X 剛好不在（`stage` 正在換），會丟 `FileNotFoundError`，當成「被取代了」，跳過。
  - 改名回去時 X 已經是新的（macOS 會 ENOTEMPTY），刪掉 `.done-X`。
  - 第 5 步清舊 `raw-*` 只對這一輪離開 outbox 的那幾筆做。

### 更新既有 id 前確認還在（L7、N4、N5）
- 「是不是更新既有 id」在**前景寫入的那一刻**決定（N4）：`_save` 寫回一個原本就存在的 id 時，在 `outbox/<ULID>/.update` 放一個空檔。這些情況是：continue 寫回、edit、import 的原地更新、`recover_pending` 寫回。新建的 id 不放，包括新的 import、merge、另存的 Y。`stage` 換掉資料夾時保留這個記號。背景只看這個記號，不看索引；索引裡本來就有所有剛寫的 Session。
- 這一輪列檔的結果就是判斷依據。有 `.update` 的項目時，第 1 步先列檔；沒有就跳過。
- 列檔失敗時（V6，PM 決定）：有 `.update` 的項目**這一輪不傳**，留在 outbox 等下一次列檔成功——「看不到」不等於「還在」，照傳可能把別台剛刪掉的傳回去。新建的 id 照傳。Drive 上根本沒有 `sessions/`（列檔成功、只是空的）不算失敗，照 N12 不當成被刪。
- 有 `.update`、但 Drive 上沒有的（N5，使用者決定）：不傳回去，改成**另存成新的 Session**。做法沿用 continue 的 F4（`_finish` 另存成 Y）：
  - 新的 ULID、parents 指向 X、relation 照原本的；
  - 存進 outbox（沒有 `.update`）並 `remember`；
  - 丟掉 X 的那一筆 outbox；
  - 寫一行記錄，下一個指令開頭提醒「X 已被別台刪除，這次的修改存成了 Y」。
  X 留在本機，照 sync 的規則標成雲端沒有。

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
  - 判斷都用同一個 `store.queued_for_trash(paths)`（N10）。

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
