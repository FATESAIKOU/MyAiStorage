## Why

`agora continue` 每隔 `--sync-period` 分鐘（預設 5）把 agent 目前的**整份對話**寫回：`store.stage` 把整份 raw 放進 outbox，`upload_batch` 再把它整份傳到 Drive（`docs/design.md` 130–148；`src/agora/store.py:771-975`）。raw 沒有壓縮，每一輪就是一個新檔（`src/agora/store.py:1103-1107`、`971-974`）。

長的 claude session 讓這件事變得很痛：一個 814 MB 的 raw，活躍時每 5 分鐘重傳一次，一天 288 輪約 235 GB 的上傳；其中真正新增的內容通常只有幾 MB。這不是正確性問題，是流量與時間的問題。

#105 的 B1（`Exported.raw_file`／串流）已合併：claude 匯出把 raw 寫到檔案，不再整份握在記憶體（`src/agora/agents/base.py:40-99` 的 `Exported`、`src/agora/agents/claude.py:308-335`、`src/agora/store.py:291-316`）。但**產生**那一趟仍把每一行收進 `list[str]`（`claude.py:210-234`），所以匯入一個 814 MB 的 jsonl 峰值約 1.4 GB（推論；B1b-2 對 20 MB 量到 1.78×）。而 `agora import` 是永久的功能：永遠會有很長的外部 session（本人 2026-10-22）。

本 change（第 9 版，依本人 2026-10-22 的裁定）做三件事：**每個 Session 的 raw 一律分段存**（只有 `chunks/1` 一種格式）、**`agora import` 改成串流打包**（峰值不隨工具輸出成長）、以及**一次性的搬遷**（有原始紀錄的舊 agora session 丟掉重建、從 Mac 上 code agent 的原始紀錄重新匯入；全部走「丟掉＋重匯」，不做原地轉存）。不做舊格式相容：沒有寫端預設關閉、沒有 `repack`、沒有降級政策、沒有「同時支援單檔與分段」的讀端。

## What Changes

- **Drive 佈局（只有一種）**：分段的 raw 用扁平的分段檔 `seg-<gen>-<seq>-<md5[:12]>.part`，放在原本的 `sessions/<ULID>/` 裡；`session.md` 仍是最終的完成標記，標頭的 `agora.raw` 是 `{md5, size, layout: chunks/1, segments}`——**不再有 `raw.file`**（新寫入的 Session 在 Drive 上沒有整份 raw 這個檔；保留的舊 Session 除外），本機鏡像的檔名由 `raw.md5` 推得。沒有另一個 manifest 檔。**16 MiB 門檻拿掉**：小 session 就是一個 tail，檔案數與今天相同；留著它等於留第二種格式（design A.5）。
- **寫入端**：claude 的匯出是**單趟串流**（邊讀邊寫 raw、邊記封存點與 md5，不再留整份行）；`stage` 在打包那一趟就算好 manifest，outbox 只放 `session.md` 與整份 raw（**不預切段**）；`upload_batch` 比對 manifest 與 listing，只把缺的段從整份 raw 切進 batch dir、一次送出，送完 `session.md` 後用 listing 逐一核對所有段的 md5，才清舊段。清理只刪「不在目前或前一版 manifest、且過寬限期（1 小時）」的段；前一版清單是上次確認成功時留下的記錄（`state/published/<ULID>.json`），不是 `stage` 當下看到的。
- **匯入（常駐能力）**：`agora import` 的記憶體峰值 MUST NOT 隨**不進閱讀版的部分**（例如工具輸出）成長（claude 單趟串流；opencode 不再多一份整份 bytes；agy 的 raw 本來就串流）；**第一次上傳就是分段**；中斷後只補 listing 缺的段，不必重讀原檔。正式 Requirement＋Scenario 在 `session-sync`，實作單是 R1。
- **讀取端**：sync 只看 listing 就能判斷分段 session 完不完整；`fetch_raw`／`show --raw`／`continue`／`pull` 下載缺少的段、逐段驗 md5、接回整份、驗整份 md5；段下載到一半被別台清掉就重抓一次 `session.md` 再試，**同一次操作內已驗證的段不重抓**。**不是 `chunks/1` 的 Session**（舊格式、未知格式）：建索引、閱讀版可讀、raw 明確不可用，**不猜、不半讀、不從索引或鏡像刪掉**（搬遷安全，不是相容）。
- **「被別台刪掉」怎麼判斷**：送出前記下 `sessions/<ULID>` 的資料夾 ID，確認時再比。Drive 的資料夾 ID 在 purge 後重建**預期**不同（**假設**，未對真 Drive 驗；探針驗，是這個 release 的前置），換 tail／`gen+1` 不會變——所以別台正常清理不會被誤判成刪除，而複製期間真的被刪掉也抓得到。第二條證據不依賴這個假設：**資料夾在 listing 裡但沒有 `session.md`（只剩孤兒段）也當成不在了**。段檔送出前把 mtime 設成現在，寬限期看的才是上傳時間。這是 R2（獨立小 PR，和 R3 同一個 release）。
- **退回整份**：來源被截斷／改寫、前綴 md5 不同、沒有 cursor、agent session 換了——任一成立就換新世代、整份**按 C 切成多段**，結果與整份路徑逐位元組相同。不是 `chunks/1` 的 Session 不自動升級：`edit`／`continue`／`push` 拒絕，一般 `import`（沒有 `--as-new`）也拒絕，交給 `--as-new`（另存重匯；沒有原地轉存，design F.3）。
- **搬遷（一次性）**：順序是「**先讓 outbox 排空** → 升級 → 盤點 → 重新匯入（`agora import … --as-new`，因為舊 session 還在本機索引裡）→ **只丟掉「兩邊都是 Drive 上那一份、而且內容與舊的相同（`raw.md5` 相同）」的舊 session** → 驗收」，其他機器各跑一次。**本人指定的必保清單**（id 在 repo 外的檔案，`--must-keep <file>`，`0600`、用完本人刪）：清單內的 session 與整條 lineage 要逐一核對 `identical` 才可丟；任何一筆不是就整條先留，正在接續的先不動、列出來（design F.1.1）。**清單檔讀不到、解析出 0 個 id、或有一行不認得 → fail-closed**（明確錯誤、不給任何候選；不認得的行只印行號）。`inventory` 每筆輸出 `droppable`／`not_droppable_reason`，頂層是 `drop_list`（只有 id）；**丟掉時只把 `drop_list` 交給 `delete`**，不是人看 JSON 手算。重匯後內容**不同**的每一筆（原檔變短、被改寫，或只是長大）要本人逐筆決定，不在 `drop_list`；**沒有原始紀錄的類別一律不丟**（design G.3）。丟掉用既有的 `agora delete … --yes`（Drive 垃圾桶 30 天、佇列可續跑）；本機鏡像會立刻消失，所以回退路是垃圾桶＋Mac 上的原始紀錄。重匯會失去使用者 metadata（tags／refs／case／description／status）與 lineage（relation／parents）；**沒有原地轉存**（`agora migrate adopt` 不做，design F.3），要保留就整條不丟（舊格式、閱讀版可讀）或自己抄回新 Session。design F、L6、L7。
- **盤點（給非技術讀者）**：哪些 agora session 在 Mac 上可能沒有原始紀錄（別台機器匯入的、agy 的、merge 的、fork 的、原檔被刪的、header 缺 `source` 的……），用 `agora migrate inventory` 只讀 metadata（索引的 `agora` 區塊用 `json_extract` 只取需要的部分＋Drive listing＋本機 agent store 的存在性＋本機 outbox 的 entry（只讀 header 的 `layout`，判 `outbox_legacy`））分類，輸出只有數量、類別、id、「替代版本在不在、內容一不一樣」、「是否在必保清單／是否正在接續／有沒有卡住的舊格式 outbox entry」與「這一筆可不可以丟（`droppable`）＋原因」，**不讀對話內容、不觸發 sync、不下載任何 `session.md`**；每一類的處理選項與建議在 design G。
- **拿掉的東西**（各一句理由）：`AGORA_RAW_LAYOUT`（沒有第二種格式可切）、`AGORA_RAW_SEGMENT_MIN`（留著就是第二種格式）、寫端預設關閉與 R4 翻預設（沒有相容期要等）、`repack` 與降級政策（沒有單檔要回去）、「舊版讀不到／消失」的處理（舊版不再是支援目標；搬遷窗口的代價寫在 F）、「同時支援單檔與分段」的讀端與「兩種都在」的整份退路（新寫入的 Session 在 Drive 上沒有整份 raw）、`raw.file`（指向不存在的檔，正是舊版誤判的原因）、`_replaced_raws`（Drive 上不再有新的 `raw-*`；舊 raw 隨整個資料夾的 purge 進垃圾桶）、`agora migrate adopt`（本人裁定全部 A；它是唯一讀舊 raw／逐檔刪舊 raw 的相容例外，F.3）。
- **不做**：`session.md`（閱讀版）的差分；opencode／agy／merge 的**增量**上傳（它們每輪仍是整份，只是切成段）；v1 的段壓實；把本機整份 raw 拿掉；continue 的「只解析新增行」（仍整份重打包）。

## Capabilities

### New Capabilities

（無。）

### Modified Capabilities

- `session-sync`：raw 一律分段（`chunks/1`）、manifest 在 `session.md`、完整性判斷、接回整份、未知／舊格式不猜不刪（含一般 `import` 的拒絕、升級前留在 outbox 的舊格式版本不上傳）；只上傳缺的段與非追加時退回整份；清舊段的規則（目前＋前一版＋寬限期）；**匯入大 Session 的記憶體契約與第一次分段上傳**；`migrate inventory`（只讀 metadata、不觸發 sync、`replacement` 的 `identical`／`different`、必保清單的保護規則）的介面；pull／push 對分段 session 的處理。
- `local-first-writes`：一批只連固定幾次 Drive 的步驟 1／3／4 改成「只送這一輪缺的段（從整份 raw 切進 batch dir）」、「核對每一個段與資料夾 ID」；離開 outbox 的條件加上「manifest 的每個段都在 Drive 上、md5 相符」；「被別台刪掉」改用資料夾 ID 判斷（段名變動不算，複製期間被 purge 仍會 rescue；另外「資料夾裡沒有 `session.md`」也算不在了）。
- `session-continue`：接續期間定期寫回的 raw 只上傳新增的段；改寫／截斷時退回整份；每次新開的 `continue` 第一次仍是整份；失敗不影響前一版。

## Impact

- 程式（實作不在本 change）：
  - `src/agora/agents/base.py`：`Increment` 介面、`collect_increment` 預設實作。
  - `src/agora/agents/claude.py`：單趟串流打包（取代 `_read_lines`＋`_scan`＋`_pack_raw_file` 的三段式）、`stable_at`／`main_end`／rolling md5、cursor、`Increment` 的 append／full 判定。
  - `src/agora/agents/opencode.py`：`export` 回 `raw_file`（不再 `read_bytes()` 進 `Exported.raw`）。
  - `src/agora/store.py`：manifest 與切段、`stage` 只放整份 raw、`upload_batch` 只送缺的段／核對全部（含資料夾 ID）／清理規則（目前＋`state/published` 的前一版＋寬限期）／送出前 `os.utime`、**刪除偵測（資料夾 ID＋`session.md` 存在檢查）**、`ensure_raw_file` 接回、`mirror_one`／`sync` 的完整性、`unfinished.json` v2（含 `legacy`）、`push_mirror` 的分段路徑、`raw-<md5>` 的檔名推導（取代 `raw.file`）。
  - `src/agora/cli.py`：`_collect_new` 帶 cursor、`_write_one`／`_save_continue` 傳計畫、`show --raw` 串流、`_import_one` 走串流匯出、`--as-new`、`migrate inventory`（含 `--must-keep`）子命令、cursor 的保存與清除。
  - `src/agora/cache.py`：pull／push 對分段 session 的整份接回與重建。
  - 環境變數：`AGORA_RAW_CHUNK`（bytes，預設 4194304）、`AGORA_RAW_ORPHAN_GRACE`（秒，預設 3600）。**不再有** `AGORA_RAW_LAYOUT`、`AGORA_RAW_SEGMENT_MIN`。
- 文件：`docs/design.md`（4.1、4.3、5.10 的儲存與 push 說明、刪除偵測）、`README.md` 的 Drive 目錄表、`docs/acceptance.md` 相關列、`docs/perf.md` 的新量測（含匯入的記憶體）、搬遷步驟（design F）。
- 測試：`tests/unit/test_store_*`、`test_cli_sync`、`test_continue_*`、`test_mem_golden`（`session.md` 的常數要重算，raw 的兩個不變）、`tests/fakes/fake_rclone.py`（段檔、資料夾 ID、`ModTime`、失敗注入）；新增分段的重組金標準、隨機切點屬性測試、串流打包「舊路徑 vs 新路徑」雙跑、假 session 100／400／800 MB 的記憶體 ratio、`inventory` 的 `identical`／`different` 與「不觸發 sync」、必保清單的 lineage 阻擋與打錯 id。直接引用 `raw-*` 或 `raw["file"]` 的測試有 15 個檔案、61 行（R3 的範圍寫在 tasks）。
- 不在本 change：實作、真正的 Drive 探針（只寫規格，見 design K）、`session.md` 差分、opencode／agy 的增量上傳、段壓實。

## Non-goals

- 不改 raw 的內容格式（claude 的 `{"format": "claude-jsonl/1", ...}` 信封不動）；分段只是把同一份位元組切開儲存。
- 不支援舊的單檔 raw：不讀、不寫、不轉回（`repack` 與原地轉存 `agora migrate adopt` 都不做）；舊格式的 Session 只能保留原樣（閱讀版可讀、raw 不可用）或丟掉重匯。
- 不引入新的衝突解決策略：跨機器仍是後寫的贏；差異只在「基準段不見時下一輪補回缺的段」，以及「正常清理不再被誤判成刪除」。
- 不要求使用者或隊員讀憑證、真 session 或連真 Drive 才能驗證；探針與量測只用假內容、獨立資料夾。
