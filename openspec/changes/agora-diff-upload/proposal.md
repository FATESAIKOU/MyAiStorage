## Why

`agora continue` 每隔 `--sync-period` 分鐘（預設 5）把 agent 目前的**整份對話**寫回：`store.stage` 把整份 raw 放進 outbox，`upload_batch` 再把它整份傳到 Drive（`docs/design.md` 130–148；`src/agora/store.py:771-975`）。raw 沒有壓縮，檔名帶 md5，每一輪就是一個新檔，上傳成功後才刪掉被取代的舊檔（`src/agora/store.py:1103-1107`、`971-974`）。

長的 claude session 讓這件事變得很痛：一個 814 MB 的 raw，活躍時每 5 分鐘重傳一次，一天 288 輪約 235 GB 的上傳；其中真正新增的內容通常只有幾 MB。這不是正確性問題，是流量與時間的問題。

#105 的 B1（`Exported.raw_file`／串流）已合併：claude 匯出把 raw 寫到檔案，不再整份握在記憶體（`src/agora/agents/base.py:40-99` 的 `Exported`、`src/agora/agents/claude.py:308-335`、`src/agora/store.py:291-316`）。B2（`Cursor`／`Segment`／`is_append_only`／`collect_increment`）只設計過、沒實作。本 change 把 B2 的介面接上「Drive 上分段存」，讓每一輪只送新增的段；讀取端（sync、pull、`show --raw`、continue 的下載、鏡像）跟著改，並保留舊 session 可讀、舊版 agora 不會讀出壞資料。

## What Changes

- **Drive 佈局**：分段的 raw 用扁平的分段檔 `seg-<gen>-<seq>-<md5[:12]>.part`，放在原本的 `sessions/<ULID>/` 裡；`session.md` 仍是最終的完成標記，標頭的 `agora.raw` 多一個 `layout: chunks/1` 與 `segments` 清單。沒有另一個 manifest 檔。
- **寫入端**：claude 的匯出多帶一個「這一版從哪個 byte 開始是新的」的計畫（`Increment`）。`stage` 只把新增的範圍按 C（預設 4 MiB）切成段；`upload_batch` 只送 Drive 還沒有的段（缺的基準段由本機整份 raw 按清單補回），送完 `session.md` 後用 listing 逐一核對所有段的 md5，才清舊段。補不回缺的段時，這一筆不上傳、留在 outbox 並警告，不冒險發布。清理只刪「不在目前或前一版清單、且過寬限期（1 小時）」的段，不再刪所有不在新清單的段——那會刪到別台剛傳的段；前一版清單是上次確認成功時留下的記錄，不是 `stage` 當下看到的（離線時那是還沒上傳的本機版本）。單檔版本的確認（`repack`、`legacy` 寫回）也跑同一條，否則舊 `seg-*` 永遠沒人清。
- **「被別台刪掉」怎麼判斷**：送出前記下 `sessions/<ULID>` 的資料夾 ID，確認時再比。Drive 的資料夾 ID 在 purge 後重建**預期**不同（**假設**，未對真 Drive 驗；K17 的探針驗，L14 **擋在 R1b 前**，也就擋住 R2b），換 tail／`gen+1` 不會變——所以別台正常清理不會被誤判成刪除（現行的名字差異判斷會），而複製期間真的被刪掉（我們自己的 `copy` 把資料夾建回來）也抓得到，不會靜靜復活。第二條證據不依賴這個假設：**資料夾在 listing 裡但沒有 `session.md`（只剩孤兒段）也當成不在了**。這個改動是 **R1b**（獨立小 PR，與 R1 同一個 release；寫端預設還是 `legacy`），先在單檔路徑累積使用（長 session 的單檔更新仍是數分鐘的複製窗口）；R2b 只把它套到分段項目。**R1b 會拿掉單檔路徑今天唯一的名字差異偵測（`store.py:925`），所以探針沒過就不發 R1b**。段檔送出前把 mtime 設成現在，寬限期看的才是上傳時間。
- **退回整份**：來源被截斷／改寫、前綴 md5 不同、上一版是舊格式、cursor 不見、agent session 換了——任一成立就換新世代、整份**按 C 切成多段**，結果與整份路徑逐位元組相同。
- **讀取端**：sync 只看 listing 就能判斷分段 session 完不完整；`fetch_raw`／`show --raw`／`continue`／`pull` 下載缺少的段、逐段驗 md5、接回整份、驗整份 md5；段下載到一半被別台清掉就重抓一次 `session.md` 再試，**同一次操作內已驗證的段不重抓**。
- **相容（實話）**：舊的單檔 `raw-<md5>.json` 照常可讀；新 reader 兩種都吃。舊版 agora 看到分段 session 時，`raw.file` 不在 Drive，**sync 會把它從索引與鏡像拿掉**——使用者看到的是「這些 session 不見了」，不是「還沒寫完」，而且舊版沒有任何指令能修（outbox 在穩定狀態下是空的）。因此寫端預設**關閉**（`AGORA_RAW_LAYOUT=legacy`），R1／R1b 先單獨發同一版（R1 是讀端；R1b 是刪除偵測的改動，先對單檔生效，**依 L14 探針**）；本 change 也提供 `agora repack session <id>… [--all]`，**降級前**把長 session 轉回單檔。格式版本號是 `raw.layout`。
- **不做**：`session.md`（閱讀版）的差分；opencode／agy／merge 的差分；v1 的段壓實；把本機整份 raw 拿掉；B2 的「只解析新增行」記憶體優化。理由在各節。
- **預設關閉**：R2b 發佈時 `AGORA_RAW_LAYOUT` 預設 `legacy`（寫單檔），要用的機器明確設 `chunks`；等 R1／R1b 裝到每一台之後，才在 R4 翻預設（design F.4、J）。

## Capabilities

### New Capabilities

（無。）

### Modified Capabilities

- `session-sync`：raw 的分段儲存（`chunks/1`）、完整性判斷、接回整份、未知格式不猜段（只有整份可用才讀，整份不可用才拒絕）；只上傳新增段與非追加時退回整份；清舊段的規則（目前＋前一版＋寬限期）；由段轉回單檔（`repack`）；pull／push 對分段 session 的處理。
- `local-first-writes`：一批只連固定幾次 Drive 的步驟 1／3／4 改成「只送這一輪需要的段」、「核對每一個段與資料夾 ID」與「單檔確認也清舊段」；離開 outbox 的條件加上「列的每個段都在 Drive 上、md5 相符」；「被別台刪掉」改用資料夾 ID 判斷（段名變動不算，複製期間被 purge 仍會 rescue；改動在 R1b，先在單檔生效；另外「資料夾裡沒有 `session.md`」也算不在了）。
- `session-continue`：接續期間定期寫回的 raw 只上傳新增的段；改寫／截斷時退回整份；失敗不影響前一版。

## Impact

- 程式（實作不在本 change）：
  - `src/agora/agents/base.py`：`Increment` 介面、`collect_increment` 預設實作。
  - `src/agora/agents/claude.py`：cursor、前綴 md5、封存點；`Increment` 的 append／full 判定。
  - `src/agora/store.py`：分段計畫、`stage` 按 C 切段、`upload_batch` 只送缺的段／從整份 raw 補回／核對全部（含資料夾 ID）／清理規則（目前＋`state/published` 的前一版＋寬限期；單檔也清）／送出前 `os.utime`、**刪除偵測（資料夾 ID＋`session.md` 存在檢查；R1b 就做，單檔也適用）**、`ensure_raw_file` 接回、`mirror_one`／`sync` 的完整性、`unfinished.json` v2、`push_mirror` 的分段路徑、`repack` 的重寫與上傳。
  - `src/agora/cli.py`：`_collect_new` 帶 cursor、`_write_one`／`_save_continue` 傳計畫、`show --raw` 串流、`repack` 子命令、cursor 的保存與清除。
  - `src/agora/cache.py`：pull／push 對分段 session 的整份接回與重建。
  - 環境變數：`AGORA_RAW_LAYOUT`（`legacy`＝預設／`chunks`）、`AGORA_RAW_CHUNK`（bytes，預設 4194304）、`AGORA_RAW_SEGMENT_MIN`（bytes，預設 16777216）、`AGORA_RAW_ORPHAN_GRACE`（秒，預設 3600）。
- 文件：`docs/design.md`（4.1、4.3、5.10 的儲存與 push 說明）、`README.md` 的 Drive 目錄表、`docs/acceptance.md` 相關列、`docs/perf.md` 的新量測。
- 測試：`tests/unit/test_store_*`、`test_cli_sync`、`test_continue_*`、`test_mem_golden`、`tests/fakes/fake_rclone.py`（段檔與失敗注入）；新增分段的重組金標準與隨機切點屬性測試。
- 不在本 change：實作、真正的 Drive 探針（只寫規格，見 design K）、`session.md` 差分、opencode／agy 的差分、段壓實。

## Non-goals

- 不改 raw 的內容格式（claude 的 `{"format": "claude-jsonl/1", ...}` 信封不動）；分段只是把同一份位元組切開儲存。
- 不引入新的衝突解決策略：跨機器仍是後寫的贏；差異只在「基準段不見時下一輪補回缺的段」，以及「正常清理不再被誤判成刪除」。
- 不要求使用者或隊員讀憑證、真 session 或連真 Drive 才能驗證；也不預設所有機器同時升級（F.4 的順序就是為此）。
