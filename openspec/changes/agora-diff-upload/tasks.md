## 0. 前提與定案（要本人／PMO；不是 PR）

- [ ] 0.1 定案 design L1：只先做 claude 的差分（opencode／agy／merge 維持整份）
- [ ] 0.2 定案 design L2：接受「舊版讀不到新的分段 session」的**真實代價**——舊版 sync 會把它從索引與鏡像拿掉（使用者看到消失），且沒有舊版指令能修
- [ ] 0.3 定案 design L3：`AGORA_RAW_CHUNK` 預設 4 MiB；每輪重傳上限 C＋新增量＋aux
- [ ] 0.4 定案 design L4：本機仍留整份 raw（只省 Drive 流量），v1 不改鏡像佈局
- [ ] 0.5 定案 design L5：`session.md`（閱讀版）v1 不差分
- [ ] 0.6 定案 design L6：跨機器維持後寫的贏；基準段不見 → 從整份 raw 補回缺的段（不換版，補不回才拒收）；別台正常清理不算刪除（比資料夾 ID，不比名字；複製期間被 purge 仍會 rescue）
- [ ] 0.7 定案 design L7：升級順序與預設開關——R1／R1b 先單獨發同一版（**R1b 依 L14 探針**）、R2b 寫端預設**關閉**（`AGORA_RAW_LAYOUT=legacy`）、每台都有 R1／R1b 之後才在 R4 翻 `chunks`
- [ ] 0.8 定案 design L8：降級政策——提供 `agora repack session <id>… [--all]`，且**降級前必須先跑**（`--all` exit 0 才算）
- [ ] 0.9 定案 design L9：Drive 垃圾桶保留 30 天（舊 tail／舊世代／repack 掉的段仍占配額，內容仍在垃圾桶）
- [ ] 0.10 定案 design L10：第一次整份重傳的時機與頻寬（升級、`gen+1`、新 `continue`、repack 各一次 814 MB，不可延後）
- [ ] 0.11 已知不需定案（design L11–L13）：16 MiB 以下單檔、v1 不壓實、R2b 驗收門檻
- [ ] 0.12 定案 design L14（建議方案 A）：K.1 的 Drive 探針（遞迴 listing 的目錄 `ID`、`mkdir`→`purge`→`mkdir` 的新 ID、`ModTime`）**是 R1b 與 R2b 的前置條件**；由本人或 PM 在 throwaway 資料夾跑一次；**不擋 R1／R2a**。R1b 會把單檔路徑今天仍有效的名字差異偵測換成 ID 比較，所以探針必須先過；方案 B（R1b 先發、保留名字差異備援）的代價見 design L14

## 1. R1：讀取端先懂 `chunks/1`＋`repack`（PR 1；d；可先單獨發一個 release）

**範圍**：`src/agora/store.py`（讀端、`repack`）、`src/agora/cli.py`（`show --raw`、`repack`）、`tests/unit/`。**不改寫入格式，也不改 `upload_batch` 的上傳／刪除判斷**（那些在 R1b）。已知且接受的行為：R1 的寫端遇到別台寫的分段 session 時會寫回單檔（等於自動 repack，但不清舊段；舊段變成孤兒，由之後的清理處理）。

- [ ] 1.1 `store.py`：加 `raw_layout(raw)`／`raw_segments(raw)`／`raw_complete(raw, files)`；`layout == "chunks/1"` 時檢查清單非空、`sum(size) == raw.size`、每個 `file` 是安全的 basename（不含 `/`、`\`、`..`、不以 `.` 開頭）、`md5` 是 32 位 hex；**同時有 `raw.file` 且它在 listing 的 md5 等於 `raw.md5` 時也算完整（「兩種都在」的退路，E.1）**；未知 `layout` **不猜段格式**：整份可用（`raw.file` 在 listing 且 md5 相符）時算完整、讀整份，整份不可用才回「不認得」
- [ ] 1.2 `store.mirror_one`（`store.py:616-634`）：改用 `raw_complete`；分段不完整 → 拿掉鏡像 `session.md`、`index.drop`、回報未完成（行為與現在 G3 相同）
- [ ] 1.3 `store.sync`（`store.py:1431-1543`）：完整性判斷改用 `raw_complete`；`unfinished.json` 升到 `version: 2`，記 `raw_fingerprint`（清單的 `(file, md5)`＋listing 實際 md5 的 canonical JSON 的 md5）；v1 檔忽略、下次多抓一次
- [ ] 1.4 `store.ensure_raw_file(paths, drive, ulid, header) -> Path`：鏡像已有整份且 md5 對 → 直接回；否則一次 `copy --files-from` 下載缺少的段到 `<cache>/segments/<ULID>/`，逐段驗 md5，依序串接、驗整份 md5／size，原子寫入 `mirror/<ULID>/raw-<md5>.json`，刪暫存段與其他舊 `raw-*.json`；**段失敗時若 header 有 `raw.file` 且 listing 的 md5 相符，改用一次 `download` 取整份（「兩種都在」的退路），整份也失敗才 `StoreError`**
- [ ] 1.5 `store.ensure_raw_file` 的重試：段 404 → 重抓一次 `session.md`；**保留仍在新清單（同名同 md5）的暫存段**，只補缺的；仍失敗 → `StoreError`、刪暫存、不留半份
- [ ] 1.6 `store.fetch_raw`（`store.py:1600-1619`）改用 `ensure_raw_file` 再讀 bytes
- [ ] 1.7 `cli.cmd_show --raw`（`cli.py:2584-2586`）：整份存在時用 `shutil.copyfileobj` 串流到 `sys.stdout.buffer`；不再把整份放進記憶體
- [ ] 1.8 `cache._pull_agora`（`cache.py:249-257`）：分段版本也用 `ensure_raw_file`；確認 pull 完鏡像有整份、索引完整
- [ ] 1.9 測試：手工造一個分段 fixture（3 段＋tail、`session.md` 列清單）→ sync 建索引、`show --raw` 位元組等於整份、pull 接回、缺一段時跳過、舊格式照舊、**未知 layout 且整份可用 → 當完整並讀整份；未知 layout 且整份不可用 → 被拒絕**；**同時有段與整份**（段完整但整份缺／段缺但整份完整）各一個；**`segments[].file` 含 `/`、`\`、`..` 或以 `.` 開頭時被拒**
- [ ] 1.10 測試：`ensure_raw_file` 的失敗路徑（段 404 → 重抓 header 且已驗段不重抓；md5 不符 → 不留檔；size 不符 → 不留檔；網路錯 → `StoreError`）
- [ ] 1.11 改壞驗證：`raw_complete` 漏比一個段、`sum(size)` 不比、`ensure_raw_file` 不驗整份 md5、`show --raw` 改回整份讀進記憶體、**未知 layout 且整份可用時仍拒絕（要紅）**、**未知 layout 且整份不可用時仍當成舊格式回完整（要紅）**、重試時整批重抓——各要紅
- [ ] 1.12 `store._write_published(paths, ulid, session_md5, segments)`：把「這一台在這個 ULID 最後一次確認成功的版本」寫成 `state/published/<ULID>.json`（本機檔、不上傳、不進 outbox）；`stage` 的簽名**不變**（上一版清單不再由 `stage` 猜，理由見 design B.4）。`delete`／`forget_local` 一起刪掉它。
- [ ] 1.13 `store.repack_one(paths, drive, ulid)`：取整份 raw（outbox → 鏡像 → `ensure_raw_file`）；**明確 plan=None**（不受 `AGORA_RAW_LAYOUT=chunks` 影響）、`update=True`（既有 id；若這筆之後由背景接手，存在檢查照常生效），用 `stage` 寫成單檔（`stage` 會把 `agora.raw` 整個換成 `{file, md5, size}`，`layout`／`segments` 自然消失）→ `remember` → 啟動上傳（**不要用 `_save`**，它會在上傳器看到之前就 kick，順序會亂）；上傳整份 raw → `session.md`、用 `_upload_checked` 核對兩個 md5；取上傳鎖（與 `push` 同一把），避免和背景上傳器同時寫。舊清單留在記憶體（這次取代的那一份），不要靠 `stage` 事後再讀。
- [ ] 1.14 `store.repack_one` 的收尾：確認成功後用**同一條清理規則**（design B.5.5；目前清單＝空、prev＝這次取代的舊清單）一次批次刪掉 `seg-*`，並掃掉同一個資料夾裡「不被任何清單指到、且過寬限期」的孤兒段（R1 機器寫回時留下的），失敗只警告，**MUST 在確認之後**；寫 `state/published` 記錄。`--all` = 先完整 sync → 逐一 repack 索引裡 `chunks/1` 的 session **＋ `unfinished.json` v2 裡 `layout: chunks/1` 的（那些不在索引裡）** → 再一次完整 sync 重新確認；還有 `chunks/1`（含轉不了的）就列出並回非 0（exit 0 才代表可以降級）；收尾那次 sync 的 listing 順便掃「單檔 header 但資料夾還有 `seg-*`」的孤兒。**不要**為了判斷把 Drive 上每個 `session.md` 都下載一次。
- [ ] 1.15 `cli.py`：新 action `repack`（`agora repack session <id>… [--all]`）；不給 id 又沒有 `--all` → exit 1；單一 id 失敗不擋其他 id；輸出「轉了幾個／剩幾個 chunks/1」
- [ ] 1.16 測試：分段 fixture → repack 後 Drive 上沒有 `layout`／`segments`、`raw.file` 在 listing 且 md5 相符（舊版兩個 predicate 通過）；本機沒有整份 raw 且離線 → 這一個失敗、Drive 上的分段版本原樣不動；`--all` 有殘留 → exit 非 0；**`--all` 把 `unfinished.json` 裡 `chunks/1` 的也算進去（不在索引裡也一樣）**；**單檔確認後孤兒 `seg-*` 超過寬限期才被刪**；確認前不刪任何段（故障注入 `before-repack-cleanup`）
- [ ] 1.17 跑 `uv run pytest -q tests/unit/test_cli_sync.py tests/unit/test_store_raw_copy.py tests/unit/test_cli_pull_push.py`（檔名以實際為準）＋`openspec validate agora-diff-upload --strict`

## 1b. R1b：刪除偵測加固（PR 1b；d；**依 K.1 探針（L14）；探針沒過不發**；與 R1 同一個 release；可與 R1 平行；先於 R2b）

**範圍**：`src/agora/store.py` 的 `upload_batch`／listing 變體、`tests/fakes/fake_rclone.py`、`tests/unit/`、`docs/design.md` 與 release note。**不改寫入格式**；只改「被別台刪掉」的判斷。這是**行為變更**（單檔更新也受影響），要單獨 review、單獨在 release note 說明。

- [ ] 1b.1 `store.Drive`：新加一個 listing 變體（`sync` 用的 `list_sessions` 不動，仍是 `--files-only`），`lsjson -R --hash` **不加 `--files-only`**，一次回 `{ULID: {name: (md5, mtime)}}` 與 `{ULID: folder_id}`（目錄項 `IsDir`、`Path` 是 ULID、`ID`）；`_listing_with_md5` 的 `upload_batch` 版本改用它；listing 沒帶目錄 ID 時，送出前與確認**各**補一次 `lsjson gdrive:sessions --dirs-only`（`folder_id()` 的形狀，`store.py:230-234`）；兩次都拿不到 → 這一筆更新不送（留在 outbox 並警告），fail-closed。**不改 `copy` 的內容與 `stage` 的輸出**
- [ ] 1b.2 `store.upload_batch`：把 `:925` 的 `any(n != "session.md" and n not in files for n in before)` 整個拿掉（單檔項目也一樣），改成比「送出前（`again`，`:837`）與確認時（`:899`）的 `sessions/<ULID>` 資料夾 ID」；不同 → `purge`＋`_rescue_deleted`（`:932`、`:935`）。檢查放在 `session.md` md5 相符（`:908`）之後、缺段檢查之前
- [ ] 1b.3 `store.upload_batch` 的存在檢查（`:812` 的 `gone`、`:848` 的 `again`）：條件改成「ULID 不在 listing，**或那個 ULID 的檔案裡沒有 `session.md`**」——purge 後第一個大段 copy 中途失敗只留孤兒段時，下一輪 MUST 當成不在了、走 rescue，MUST NOT 把它補完復活
- [ ] 1b.4 測試（fake）：`tests/fakes/fake_rclone.py` 的目錄 `ID` 改成「每個資料夾建立時配一個序號」（同一路徑 purge 後重建拿到**新** ID，現在 `fake_rclone.py:148` 是資料夾名）、`lsjson` 不加 `--files-only` 時列出目錄項、支援讓指定的 `copy` 失敗；別台正常清理（換掉被取代的 `raw-*`、`gen+1`）不 purge／不另存；複製期間被 purge（fake 在第一個 `copy` 之後刪資料夾）→ 確認時 ID 不同 → purge＋另存 Y、Drive 上沒有 X；listing 沒有目錄 ID → 走 `--dirs-only`；兩次都沒有 → 那一筆更新留在 outbox 並警告；**只剩孤兒段的資料夾 → 下一輪 rescue，不復活**
- [ ] 1b.5 改壞驗證：確認時不比資料夾 ID（複製期間被 purge：X 被復活）、存在檢查只看 ULID 不看 `session.md`（孤兒段-only：X 被復活）、名稱差異仍被當成刪除（正常清理就誤 purge）、拿不到 ID 仍送出——各要紅
- [ ] 1b.6 跑 `uv run pytest -q tests/unit/test_cli_sync.py tests/unit/test_store_raw_copy.py tests/unit/test_cli_pull_push.py`（檔名以實際為準）＋`openspec validate agora-diff-upload --strict`
- [ ] 1b.7 文件與 release note（不等 R3）：`docs/design.md` 的「被別台刪掉」段落改成「資料夾 ID＋`session.md` 存在檢查」（並寫明 ID 是假設、K17，且 **K.1 探針是 R1b 的發布前置**）；`docs/perf.md` 註記更新路徑的 rclone 次數（正常 ≤ 7、`--dirs-only` 退路 ≤ 9）；release note 寫明這是**行為變更**（單檔更新也受影響）、探針結果（重建的資料夾是否拿到新 ID）與 fail-closed 的意思（拿不到 ID 的更新留在 outbox，不會無保護地送）

## 2. R2a：claude 的 cursor 與 append 判定（PR 2a；d；不動 Drive）

**範圍**：`src/agora/agents/base.py`、`src/agora/agents/claude.py`、`src/agora/cli.py`、`src/agora/round_child.py`（可能不用改）、`tests/unit/`；不改 `store.stage`／`upload_batch` 的輸出格式。

- [ ] 2.1 `base.py`：`Increment{exported, append_from, seal_upto, cursor}`；`Agent.collect_increment(launch, cursor)` 的預設實作＝`collect()` 包成 `Increment(exported, None, None, None)`
- [ ] 2.2 `claude.py`：`_pack_raw_file` 回傳 `(md5, size, stable_offsets)` 或等價的 callback；在寫完第 `n` 個 main 元素時記 `stable_at(n)` 與 rolling md5 的 `copy()`；記 `main_end`（`]` 之前）與 `main_md5`（最後一個元素之後）
- [ ] 2.3 `claude.py`：`collect_increment` 讀 cursor；`stable_at(cursor.sealed_line_count) == cursor.raw_stable` 且該位置的前綴 md5 == `cursor.sealed_md5` → `append_from = cursor.raw_stable`；否則 `append_from = None`；`seal_upto = main_end`。**`sealed_line_count` 以解析後（`_scan` 已 `main.pop()` 掉半行）的 main 元素數為準**，cursor 與下一輪用同一個定義
- [ ] 2.4 `claude.py`：cursor `{version: 1, agent_session_id, path, source_size, sealed_line_count, sealed_md5, raw_stable, base_md5, main_line_count, main_md5}`；`agent_session_id` 與這次的 `exported.session_id` 不同 → full；store 在封存時把 `sealed_*` 換成 `main_*` 與 `seal_upto`，沒封存就不動；`AGORA_RAW_SEGMENT_MIN` 以下的 raw 回 `seal_upto=None`（不啟用分段）
- [ ] 2.5 `cli.py`：`_collect_new` 讀 `<state>/cursors/<ULID>.json`；`Increment` 放到 `run` 上；`_save`／`_save_continue` 把 `Increment` 的 `append_from`／`seal_upto`／cursor 傳給 `stage`；cursor 由 `stage` 在 swap 成功後寫（`_write_one` 不寫）
- [ ] 2.6 `cli.py`：`delete`／`store.forget_local` 清 cursor；新 import／fork 沒有 cursor
- [ ] 2.7 測試：金標準 `append(prefix) == full(file)`（逐位元組）——隨機切點、CJK、大 tool_result、sidecar 變動、**半行→完成**（半行時 `main.pop()`、cursor 的元素數不含它；補完後下一輪 append 且封存點前移一個元素）、壞行、`/clear` 新檔
- [ ] 2.8 測試：非追加（短到封存點之前、同大小改寫、封存前綴改寫）都回 `append_from=None`；aux 變動**不**回 full（只讓 tail 變）；只砍掉封存點之後是 append（tail 較短）；`agent_session_id` 不同 → full
- [ ] 2.9 測試：cursor 在 `stage` 失敗後不前進；重開行程／刪 cursor → 第一次 full、第二次 append；`test_mem_golden` 的三個 md5 不變（小 session 不走分段）
- [ ] 2.10 測試：**aux 序列化順序穩定**——同一個 sidecar 連續 export 兩次，aux 片段逐位元組相同；加一個檔案後既有項目的相對順序不變（`_pack_aux` 的 `sorted`，`claude.py:263`）
- [ ] 2.11 改壞驗證：`stable_at` 差一個 byte、前綴 md5 比錯位置、cursor 在失敗後仍寫、aux 變動誤判 full、短到封存點之前仍 append、半行時把 `main.pop()` 前後的元素數混用——各要紅
- [ ] 2.12 跑相關 unit＋`openspec validate agora-diff-upload --strict`

## 3. R2b：Drive 分段寫入與上傳（PR 2b；c；最痛的那一片；依 R1＋R1b（含 L14 探針）＋R2a；**預設關閉**）

**範圍**：`src/agora/store.py`、`src/agora/cli.py`、`src/agora/cache.py`、`tests/fakes/fake_rclone.py`、`tests/unit/`。這是唯一會改變 Drive 佈局與上傳行為的 PR。

- [ ] 3.1 `store.stage` 接受 `plan`（`append_from`／`seal_upto`）：讀上一版 header（outbox → 鏡像），驗 `prev.raw.md5 == cursor.base_md5` 與 `sum(prev.segments[:-1].size) == append_from`；不符 → full
- [ ] 3.2 `store.stage`：**段一律按 `AGORA_RAW_CHUNK` 切成多段**（A.1／B.2.5）——append 時 `[append_from, main_end)` 切 `ceil(範圍/C)` 段、tail 是 `[main_end, size)`；範圍不到 C 就不封存；**full 時 `[0, main_end)` 切 `ceil(main_end/C)` 段**（不是一段）＋tail。段檔寫進 `tmp`、邊寫邊算 md5／size、fsync；header 寫 `raw.layout`／`raw.segments`；整份 `raw.file` 照舊放進 outbox；把上一個 entry 的 `seg-*` 一起帶進 `tmp`；**swap 成功後**寫 cursor（封存點有變才更新 `sealed_line_count`／`sealed_md5`／`raw_stable`）。**不寫** `.prev-segments.json`（design B.4）
- [ ] 3.3 `store.stage`：整份 md5 沒變（edit）→ 沿用上一版清單、不產生段；小於 `AGORA_RAW_SEGMENT_MIN` → 單檔、不寫 `layout`；`AGORA_RAW_LAYOUT=legacy` → 單檔、不寫 `layout`（等於自動 repack）
- [ ] 3.3a `cli._import_one`：claude 改用 `agent.collect_increment(launch, cursor=None)`（不是 `export`），讓第一次匯入的大 session 就切成段（continue 的第一次寫回仍是整份：新的 agent session）；`fork`（`cli.py:2049`）／`merge`（`cli.py:751`）維持 bytes／單檔
- [ ] 3.4 `store.upload_batch`：分段 entry 的 `raws` 只列這一輪要送的段（**不列整份 `raw.file`**）；缺的基準段由整份 raw 按清單 `size` 切出、驗 md5；`sessions` 的判斷改成「清單裡的段都在 Drive 或這一輪送出」，不用 `sent_raws` 代理；整批沒有 `.update` 但有基準段時，先列一次（失敗就這一輪不送）
- [ ] 3.5 `store.upload_batch`：確認階段加驗每一個段（`files.get(s.file) == s.md5`）；缺段／不符 → 留在 outbox，下一輪補回；`.done` 的第二次比對對分段版本比清單的每一個段（不比 `raw.file`）
- [ ] 3.6 `store.upload_batch` 清理（**新規則**）：對**每一個**這一輪確認成功、離開 outbox 的項目（分段與單檔都算）跑同一條：保留「目前清單」＋「`state/published` 記錄的前一版清單（只有 md5 等於送出前 Drive 的 `session.md` 時才算）」＋「`ModTime` 在 `AGORA_RAW_ORPHAN_GRACE` 內的所有 `seg-*`」，其餘一次 `delete --files-from`；確認成功後寫新的 `state/published` 記錄（寫失敗只警告）；`raw-*` 照舊（不是 `raw.file` 的就刪）；失敗只警告、下次再試。**MUST NOT** 刪「listing 有、但不在目前清單」的所有段
- [ ] 3.6a **刪除偵測已在 R1b 做完**（tasks 1b.1–1b.3：listing 帶目錄 `ID`、`--dirs-only` 退路、ID 比較、存在檢查要求 `session.md`）；R2b **不重做、不改規則**，只確認分段路徑沿用同一個 listing 變體（含 `ModTime`；`lsjson --hash` 本來就有）與同一個 fail-closed。分段版的確認 listing 仍然要帶目錄 ID 與 `ModTime`
- [ ] 3.7 `store.upload_batch`：batch dir 放這一輪要送的所有東西——legacy raw 用 `os.link`（失敗才 copy）、新段用 `os.link`、缺的基準段從整份 raw 切、`session.md` 快照；**送出前對 batch dir 裡每個檔案 `os.utime(f, None)`**（hard link 共用 inode，outbox 那份的 mtime 也會變；已查沒有任何決策讀它）；一次 `copy --files-from` 送 raws／segments、再一次送 `session.md`；失敗清掉 batch dir；分段版本不要把整份 `raw.file` 放進去
- [ ] 3.8 `store.push_mirror`／`cache._push_mirrored`：分段版從鏡像的整份 raw 按段切回缺少的段、上傳、驗證；`--not-exist-upload` 也走這條
- [ ] 3.9 `tests/fakes/fake_rclone.py`：記錄每次 `copy --files-from` 的來源檔名與大小；支援「只讓某個段失敗」「上傳後立刻 listing 看不到某個段」「`ModTime` 可控」的注入；**`copy` 要保留來源 mtime（`copy2` 或 `os.utime`，現在是 `shutil.copyfile`，`fake_rclone.py:163`、`:173`）並在 `lsjson` 回 `ModTime`**，否則 `os.utime` 與寬限期都測不出來（資料夾 `ID` 的 fake 在 R1b 的 1b.4）
- [ ] 3.10 測試：814 MB（或小 `CHUNK` 的等效）多輪——第 2 輪起只送新段＋tail；`calls.log` 的 bytes 與次數符合 `C+I+S+aux`；Drive 上的段接起來等於整份；第一次切段是約 195 個檔、不是一個巨段；**一路 append 的段數符合 `ceil(size/C)`～`2·ceil(size/C)`（每輪新增 1 MB、4 MiB 一段時約 326＋1）**
- [ ] 3.11 測試：崩潰點（`after-segment-generation`／`after-segment-upload`／`after-session-upload`／`after-confirm`／`before-cleanup`／`after-cleanup`）——每個點之後舊版仍完整、下一輪收斂、I1–I5 成立
- [ ] 3.12 測試：跨機器——A 寫、B sync＋append、A 再寫並清掉 B 的基準 → B 下一輪從整份 raw 補回缺的段；**別台每輪換 tail／`gen+1` 不得觸發 purge 或 `_rescue_deleted`（資料夾 ID 不變）**；**複製期間被 purge（`FAKE_RCLONE_PURGE_AFTER` 在第一個 `copy` 之後刪資料夾）→ 確認時 ID 不同 → purge 剛建回來的資料夾＋另存 Y，Drive 上沒有 X**；**purge 後第一個大段 copy 失敗只剩孤兒段 → 下一輪 rescue、不復活**；真的 purge 時仍要 rescue。另加：同時 stage＋upload 的競態；`edit` 只換 header 不重傳段；`push --not-exist-upload` 重建段（單檔版的 ID／孤兒段測試在 R1b 的 1b.4）
- [ ] 3.13 測試：舊版 predicate 模擬（sync 之後從索引與鏡像消失、`fetch_raw` 明確錯誤）；舊版寫回整份後新 reader 的兩條路（先段、後整份）
- [ ] 3.14 測試：清理新規則——別台剛傳的段（寬限期內）不被刪；**前一版清單是 `state/published` 的記錄、md5 對得上**才保護（離線連 stage 三代：Drive 公開的第一代 tail 被保住，不是第三代的本機清單）；記錄對不上時只剩寬限期；孤兒段超過寬限期才被刪；清理在確認前不跑；**單檔版本確認後也清（`repack`／`legacy` 寫回留下的孤兒）**
- [ ] 3.15 改壞驗證：先傳 `session.md`、確認只比 `session.md`、清理在確認前、`append_from` 差一 byte、補回的段 md5 不符仍上傳、段名對調、**送出前不 `os.utime`（段上傳後 ModTime 還是舊的，被別台清理刪掉）**——各要紅（ID 比較與 `session.md` 存在檢查的改壞驗證在 R1b 的 1b.5）
- [ ] 3.16 實作四個環境變數：`AGORA_RAW_LAYOUT`（`legacy`＝**預設**／`chunks`；未知值明確報錯）、`AGORA_RAW_CHUNK`（bytes，預設 4194304；測試 4096）、`AGORA_RAW_SEGMENT_MIN`（bytes，預設 16777216）、`AGORA_RAW_ORPHAN_GRACE`（秒，預設 3600）；解析失敗 → 明確錯誤，不靜默回預設
- [ ] 3.17 `_rescue_deleted` 對分段 entry 的測試：entry 有整份 raw＋`chunks/1` header → 另存的 Y 是單檔、內容等於整份、`parents` 指向 X；沒有可補的段（所有段都是新的）；**另存時把 raw 以檔案路徑交給 `stage`（`store.py:325` 接受 `Path`），不是 `read_bytes()` 把 814 MB 讀進上傳器的記憶體（`store.py:997` 現在是 bytes）**
- [ ] 3.18 量測：`tests/perf/measure_continue_mem.py` 或新的小腳本，記上傳 bytes／rclone 次數／輪耗時／子行程峰值；和 `docs/perf.md` 的現值對照；另記 listing 的總量（N 個長 session）
- [ ] 3.19 跑整套 `uv run pytest -q tests/unit`＋`openspec validate agora-diff-upload --strict`

## 4. R3：文件、驗收、遷移與回退（PR 3；d；依 R2b）

- [ ] 4.1 `docs/design.md`：4.1 的 Drive 佈局與上傳順序、4.3 的 sync、5.10 的 pull／push，加上 `seg-*`、`layout`、`segments`、只送新段、補回缺段、核對全部（含資料夾 ID）、清舊段（目前＋`state/published` 記錄的前一版＋寬限期；單檔確認也清）、送出前 `os.utime`（刪除偵測的段落已在 R1b 的 1b.7 更新，這裡只補分段相關）
- [ ] 4.2 `README.md` 的 Drive 目錄表與 `docs/acceptance.md` 相關列：分段 session 的檔案形狀、`show --raw` 的接回、舊 session 照舊
- [ ] 4.3 `docs/perf.md`：新增一段量測（814 MB 或等效小 `CHUNK`）：每輪 bytes、rclone 次數、輪耗時、listing 總量；寫明前提（`C+I+S+aux`）與未知
- [ ] 4.4 遷移與回退步驟（照 design F.3／F.4 寫，不要留舊說法）：R1／R1b 先發並裝到每一台→R2b 預設 `legacy`、要用才設 `chunks`→R4 翻預設；**降級前先 `agora repack session --all`，exit 0 才降級**；`AGORA_RAW_LAYOUT=legacy` 只關行為、不能代替 repack；舊版會讓分段 session 從索引與鏡像消失
- [ ] 4.5 `experiments/agora-chunks/probe_drive.sh`（或 `.py`）的**規格**與一支不連真 Drive 的乾跑（`--dry-run` 只印要跑的指令）；探針要含 `mkdir sessions/probe` → 記 `ID` → `purge` → `mkdir` → 再記 `ID` 的比較（K17）與 `ModTime` 檢查（K16）；真正的探針由本人或 PM 跑（design K.1），**必須在 R1b 之前跑完（L14）**
- [ ] 4.6 隱私：文件寫明「掃描整份 raw，不掃單一段檔」；範例不含真 token、真路徑、真 session；垃圾桶 30 天的隱私面寫進文件（L9）
- [ ] 4.7 驗收：照 4.2 的步驟在隔離環境（fake rclone、假 HOME）跑一次長 session 的多輪寫回；`openspec validate agora-diff-upload --strict`
- [ ] 4.8 歸檔：本 change 的實作 PR 合併後 `openspec archive agora-diff-upload`（把 delta 併進主 spec）

## 5. R4：翻預設（PR 4；d；依 R1／R1b、R2b 已發佈且每台都裝了 R1／R1b）

- [ ] 5.1 前置檢查：逐台確認 `agora --version` 有 R1／R1b（沒有握手訊號，這一步只能人工做，design K15／L7）；把清單寫進 release note
- [ ] 5.2 `AGORA_RAW_LAYOUT` 預設從 `legacy` 改成 `chunks`；未知值仍明確報錯；`legacy` 仍可用（kill switch）
- [ ] 5.3 release note：升級後第一個寫回是整份、新 `continue` 的第一次也是整份、降級前要 repack；附上「怎麼確認每台有 R1／R1b」
- [ ] 5.4 驗收：在全部機器 ≥ R1／R1b 的環境下，升級後第一個寫回切成段；舊機器仍讀得到（因為都有 R1／R1b）

## 6. 後續（不在本 change；列出來避免忘記）

- [ ] 6.1 段壓實（design D.2）：段數 > 512 時，由 uploader 從鏡像整份 raw 合併相鄰封存段；一次一個 run
- [ ] 6.2 aux 很大時拆成獨立段或 range reference（design K5）；先量測真實 sidecar 大小
- [ ] 6.3 opencode 的穩定前綴探針（export 的 `info`／最後一則是否會變）；若穩定，接同一個 `collect_increment`
- [ ] 6.4 `session.md` 的 body 差分（小 header＋body 段；設計 H）
- [ ] 6.5 B2 的「只解析新增行」：讓 claude 不再整份 `_read_lines`／打包；降低本機 CPU 與 1.78× 峰值
