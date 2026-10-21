## 0. 前提與定案（要本人／PMO；不是 PR）

- [ ] 0.1 定案 design L1：只先做 claude 的**增量**上傳（分段存對所有 agent 生效；opencode／agy／merge 每輪仍是整份）
- [ ] 0.2 定案 design L2：拿掉 16 MiB 門檻（所有 session 都 `chunks/1`），並**簽核**測試裡固定的「標準答案指紋」更新（`tests/unit/test_mem_golden.py:56` 的 `GOLDEN_MD5_SESSION_MD` 重算；`:293`、`:358` 兩處斷言跟著改）；兩個 raw md5（`:54-55`、`:297`、`:360`）MUST 不變。白話版在 design L2
- [ ] 0.3 定案 design L3：`AGORA_RAW_CHUNK` 預設 4 MiB
- [ ] 0.4 定案 design L4：本機仍留整份 raw（只省 Drive 流量）
- [ ] 0.5 定案 design L5：`session.md`（閱讀版）v1 不差分
- [ ] 0.6 定案 design L6：搬遷走 A（丟掉＋重匯）還是 B（原地轉存）；建議多數 A、有 tags／lineage 的少數 B；走 A 時只有 `identical` 的才進丟掉清單
- [ ] 0.7 定案 design L7：盤點後每個類別怎麼處理（G.3；含 adopt 要不要做——它決定 R4 的範圍）；**`different` 的每一筆**也要本人逐筆決定（F.1 第 4 步、G.4）
- [ ] 0.8 已知不需定案：design L8（跨機器維持後寫的贏；基準段不見 → 從整份 raw 補回缺的段）沿用既有行為，第 7 版已改列實作細節
- [ ] 0.9 定案 design L9（建議要）：K.1 的 Drive 探針（遞迴 listing 的目錄 `ID`、`mkdir`→`purge`→`mkdir` 的新 ID、`ModTime`）**是 R2 與 R3 的前置條件**；由本人或 PM 在 throwaway 資料夾跑一次；**不擋 R1**（R1 只在本地重寫匯出）
- [ ] 0.10 定案 design L10：接受搬遷窗口內舊 session「閱讀版可讀、`show --raw`／`continue` 明確錯誤」（不做舊格式相容的直接結果）
- [ ] 0.11 定案 design L11：Drive 垃圾桶保留 30 天（舊 tail／舊世代／搬遷刪掉的 session 仍占配額，內容仍在垃圾桶）
- [ ] 0.12 已知不需定案（design L8、L12–L16）：R3 驗收門檻、v1 不壓實、`adopt` 的收尾與多機一致性、匯入的記憶體驗收

## 1. R1：串流匯出（常駐能力；PR 1；d；不動 Drive、不改格式）

**範圍**：`src/agora/agents/claude.py`、`src/agora/agents/opencode.py`、`tests/unit/`、`tests/perf/`。**這是正式的實作單，不是遷移的附屬步驟**：之後任何很長的外部 session 匯入都走它，搬遷（R4）也用它。介面不變（`Agent.export` 仍回 `Exported`，`raw_file` 已經存在），改的是**產生的過程**。

- [ ] 1.1 `claude.py`：把 `_read_lines`（`:210-234`）＋`_scan`（`:412-454`）＋`_pack_raw_file`（`:308-335`）合成**單趟串流**：讀一行→驗一行（規則不變：空白行跳過；最後一個非空白行不合法就丟並警告；其他位置的語法錯誤是 `AgentError`；非 dict 行的例外優先序照舊）→ 把 `json.dumps(line, ensure_ascii=False)` 直接寫進 raw 檔；**只緩衝一行**以判斷「最後一行」
- [ ] 1.2 `claude.py`：同一趟記 `stable_at(n)`（寫完第 n 個 main 元素後的 offset）、`main_end`（寫 `], "aux": ` 之前）、rolling md5 的 `copy()`（`sealed_md5`／`main_md5`）；這些留在 adapter 裡，用來算 R3 的 `append_from`／`seal_upto` 與 cursor 的三個封存值（**不塞進 `Exported`**）
- [ ] 1.3 `claude.py`：`_pack_aux`（`:258-271`）不變；確認 aux 序列化順序穩定（`sorted(sidecar.rglob("*"))`，`:263`）並加測試
- [ ] 1.4 `opencode.py`：`_export_bytes`（`:887-913`）不再 `target.read_bytes()`；`export`（`:1002-1032`）回 `raw_file`（把暫存檔複製／移到 `Exported` 的 tmpdir），事實摘要用 `json.load(open(path))`（`_read` 的等價物，`:991-1000`）——**不再保留一份與 raw 同大小的 `Exported.raw`**；**同時把 `turns` 放進 `Exported`**，否則 `reading_of`（`base.py:260-272`）會走 `agent.turns(exported.raw)` 把整份再讀一次
- [ ] 1.5 `agy.py`：確認 `pack_raw_file`（`agy_store.py:304-335`）已經是串流；把「`scan_text` 清單與 `json.dumps(scan_text)` 仍是 O(文字)」寫進 docstring；`export`（`agy.py:163-186`）不改行為
- [ ] 1.6 測試（雙跑）：同一份語料（CJK、大 `tool_result`、sidecar、壞行、半行、非 dict、空白行、`/clear` 換檔），舊三段式與新單趟串流比 **raw bytes、md5、`main_end`、`stable_at(n)`、例外類別與訊息**——全部相同（design I.2）
- [ ] 1.7 測試（記憶體）：`tests/perf/measure_import_mem.py`；假 claude session 100／400／800 MB（形狀：一小段對話＋大量 `tool_result`，**閱讀版大小固定**），`AGORA_UPLOAD=inline`＋假 rclone、假 HOME；量 `ru_maxrss`／牆上時間／磁碟峰值。斷言 `peak(800) ≤ 1.2 × peak(100)` 且 ≤ 256 MiB；**三份的秒數都要記下來**（不設門檻，但 R1 驗收要有數字；design I.8、K.2）
- [ ] 1.8 測試：另外兩份「行很長」（單行 1 MB／10 MB）量峰值（K14）；把結果寫進 `docs/perf.md` 的草稿（正式文件在 R4）
- [ ] 1.9 測試：opencode 的 `export` 不再有整份 `Exported.raw`（`raw` 屬性第一次被讀時才從檔案讀）；`raw_file` 的 md5 與舊路徑相同；agy 的 raw md5 不變
- [ ] 1.10 改壞驗證：把最後一行半寫也寫進 raw（要紅）；讓語意錯誤蓋掉後面的語法錯誤（要紅）；`import` 把整份讀進記憶體（800 MB 的峰值超過 1.2×100 MB，要紅）——各要紅
- [ ] 1.11 跑 `uv run pytest -q tests/unit/test_mem_golden.py tests/unit/test_agent_claude.py tests/unit/test_agent_opencode.py`（檔名以實際為準）＋`openspec validate agora-diff-upload --strict`

## 2. R2：刪除偵測加固（PR 2；d；**依 K.1 探針（L9）；探針沒過不發**；與 R3 同一個 release、先於 R3）

**範圍**：`src/agora/store.py` 的 `upload_batch`／listing 變體、`tests/fakes/fake_rclone.py`、`tests/unit/`、`docs/design.md` 與 release note。**不改寫入格式**；只改「被別台刪掉」的判斷。這是**行為變更**（單檔更新也受影響），要單獨 review、單獨在 release note 說明。

- [ ] 2.1 `store.Drive`：新加一個 listing 變體（`sync` 用的 `list_sessions` 不動，仍是 `--files-only`），`lsjson -R --hash` **不加 `--files-only`**，一次回 `{ULID: {name: (md5, mtime)}}` 與 `{ULID: folder_id}`（目錄項 `IsDir`、`Path` 是 ULID、`ID`）；`_listing_with_md5` 的 `upload_batch` 版本改用它；listing 沒帶目錄 ID 時，送出前與確認**各**補一次 `lsjson gdrive:sessions --dirs-only`（`folder_id()` 的形狀，`store.py:230-234`）；兩次都拿不到 → 這一筆更新不送（留在 outbox 並警告），fail-closed
- [ ] 2.2 `store.upload_batch`：把 `:925` 的 `any(n != "session.md" and n not in files for n in before)` 整個拿掉，改成比「送出前（`again`，`:837`）與確認時（`:899`）的 `sessions/<ULID>` 資料夾 ID」；不同 → `purge`＋`_rescue_deleted`（`:932`、`:935`）。檢查放在 `session.md` md5 相符（`:908`）之後、缺段檢查之前
- [ ] 2.3 `store.upload_batch` 的存在檢查（`:812` 的 `gone`、`:848` 的 `again`）：條件改成「ULID 不在 listing，**或那個 ULID 的檔案裡沒有 `session.md`**」——purge 後第一個大段 copy 中途失敗只留孤兒段時，下一輪 MUST 當成不在了、走 rescue，MUST NOT 把它補完復活
- [ ] 2.4 測試（fake）：`tests/fakes/fake_rclone.py:148` 的目錄 `ID` 改成「每個資料夾建立時配一個序號」（同一路徑 purge 後重建拿到**新** ID）、`lsjson` 不加 `--files-only` 時列出目錄項、支援讓指定的 `copy` 失敗；別台正常清理（換掉被取代的檔、`gen+1`）不 purge／不另存；複製期間被 purge（fake 在第一個 `copy` 之後刪資料夾）→ 確認時 ID 不同 → purge＋另存 Y；listing 沒有目錄 ID → 走 `--dirs-only`；兩次都沒有 → 那一筆更新留在 outbox 並警告；**只剩孤兒段的資料夾 → 下一輪 rescue，不復活**
- [ ] 2.5 改壞驗證：確認時不比資料夾 ID、存在檢查只看 ULID 不看 `session.md`、名稱差異仍被當成刪除、拿不到 ID 仍送出——各要紅
- [ ] 2.6 跑 `uv run pytest -q tests/unit/test_cli_sync.py tests/unit/test_store_raw_copy.py tests/unit/test_cli_pull_push.py`（檔名以實際為準）＋`openspec validate agora-diff-upload --strict`
- [ ] 2.7 文件與 release note（不等 R4）：`docs/design.md` 的「被別台刪掉」段落改成「資料夾 ID＋`session.md` 存在檢查」（並寫明 ID 是假設、K.1 探針是發布前置）；`docs/perf.md` 註記更新路徑的 rclone 次數（正常 ≤ 7、`--dirs-only` 退路 ≤ 9）；release note 寫明這是**行為變更**、探針結果與 fail-closed 的意思

## 3. R3：格式換代（讀端＋寫端；PR 3；c；**依 R1、R2（含 L9 探針）**；R3a 先、R3b 後，**同一個 release**）

**範圍**：`src/agora/store.py`、`src/agora/cli.py`、`src/agora/cache.py`、`src/agora/agents/base.py`、`src/agora/agents/claude.py`、`tests/fakes/fake_rclone.py`、`tests/unit/`。這是唯一會改變 Drive 佈局與上傳行為的 PR。**測試改動面**：`grep` `tests/` 有 **15 個檔案、61 行／63 處**直接引用 `raw-*` 檔名或 `["raw"]["file"]`（`test_upload_batch.py` 13、`test_cli_sync.py` 14、`test_store_raw_copy.py` 6、`test_store.py` 6、`test_cli_more.py` 6、`test_cache.py` 4、`test_store_more.py` 3、`test_cli.py` 2、`test_mem_golden.py` 2、`test_continue_start_child.py` 1、`test_agent_agy.py` 1、`tests/integration/test_store_drive.py` 1、`test_pull_push.py` 2、`test_e2e_opencode.py` 1、`tests/perf/measure_continue_mem.py` 1），要改成「Drive 上的段＋`session.md`」與「本機整份 raw 檔名由 `md5` 推得」；`test_mem_golden.py` 的兩處是 glob **鏡像**的 `raw-*.json`（`:294`、`:359`），鏡像仍留整份 raw，所以找法不用改，只有 `GOLDEN_MD5_SESSION_MD`（`:56`）要重算。

- [ ] 3.1 `store.py`：`raw_layout(raw)`／`raw_segments(raw)`／`raw_complete(raw, files)`；`layout == "chunks/1"` 時檢查 manifest 非空、`sum(size) == raw.size`、每個 `file` 是安全的 basename（不含 `/`、`\`、`..`、不以 `.` 開頭）、`md5` 是 32 位 hex；**沒有 `layout` 或不是 `chunks/1` 時回 `None`（舊格式／未知），不是 `False`**
- [ ] 3.2 `store.raw_name(raw)`：本機整份 raw 的檔名由 `raw.md5` 推得（`raw-<md5[:12]>.json`），取代 `raw["file"]` 的讀取點（`grep` 到 16 行：`cache.py:253`、`:342-343`；`store.py:518`、`:571`、`:594-597`、`:603`、`:613`、`:629`、`:996`、`:1117`、`:1519-1524`、`:1605-1608`）
- [ ] 3.3 `store.stage`（`:324-399`）：`_stage_raw`（`:291-316`）在同一趟算 manifest（每 C bytes 一個 md5／size）；header 寫 `agora.raw = {md5, size, layout: chunks/1, segments}`；**outbox 只放 `session.md` 與整份 raw，不切段**；上一版 entry 的 raw 照舊帶過來（`:353-365`）；`Path` 來源先試 `os.link`、失敗才複製；整份 md5 沒變（edit）沿用上一版 manifest；`update`／`kind` 不變
- [ ] 3.4 `store.stage`：接受 `plan`（`append_from`／`seal_upto`）——讀上一版 header（outbox → 鏡像），驗 `prev.raw.md5 == cursor.base_md5` 與 `sum(prev.segments[:-1].size) == append_from`；不符 → full；**swap 成功後**寫 cursor（封存點有變才更新 `sealed_line_count`／`sealed_md5`／`raw_stable`）；**不寫** `.prev-*`
- [ ] 3.5 `agents/base.py`：`Increment{exported, append_from, seal_upto, cursor}`；`Agent.collect_increment(launch, cursor)`；預設＝`collect()` 包成 `Increment(exported, None, None, None)`
- [ ] 3.6 `agents/claude.py`：`collect_increment` 讀 cursor；`stable_at(cursor.sealed_line_count) == cursor.raw_stable` 且該位置的前綴 md5 == `cursor.sealed_md5` → `append_from = cursor.raw_stable`；否則 `append_from = None`；`seal_upto = main_end`。cursor `{version: 1, agent_session_id, path, source_size, sealed_line_count, sealed_md5, raw_stable, base_md5, main_line_count, main_md5}`
- [ ] 3.7 `cli.py`：`_collect_new`（`:1419`）讀 `<state>/cursors/<ULID>.json`；`Increment` 放到 `run` 上；`_save`／`_save_continue` 把 `append_from`／`seal_upto`／cursor 傳給 `stage`；`delete`／`store.forget_local` 清 cursor 與 `state/published`
- [ ] 3.8 `store.upload_batch`：分段 entry 的 `raws` **不列整份 raw**；比對 manifest 與 listing，把缺的段從 outbox 的整份 raw 按 `size` 切進 batch dir（切出來 md5 不符 → 這一筆不送）；`sessions` 的判斷改成「manifest 的段都在 Drive 或這一輪送出」；整批沒有 `.update` 但有基準段時，先列一次（失敗就這一輪不送）
- [ ] 3.9 `store.upload_batch`：確認階段加驗每一個段（`files.get(s.file) == s.md5`）；缺段／不符 → 留在 outbox，下一輪補回；`.done` 的第二次比對沿用它（整份 raw 就在 entry 裡，manifest 是它的純函數）
- [ ] 3.10 `store.upload_batch` 清理（**新規則**）：對每一個這一輪確認成功、離開 outbox 的項目：保留「目前 manifest」＋「`state/published` 記錄的前一版 manifest（只有 md5 等於送出前 Drive 的 `session.md` 時才算）」＋「`ModTime` 在 `AGORA_RAW_ORPHAN_GRACE` 內的所有 `seg-*`」，其餘一次 `delete --files-from`；確認成功後寫新的 `state/published` 記錄（寫失敗只警告）；**拿掉 `_replaced_raws`／`:971-974` 的 `raw-*` 清理**（Drive 上不再有）；失敗只警告、下次再試
- [ ] 3.11 `store.upload_batch`：batch dir 放這一輪要送的段與 `session.md` 快照；**送出前對 batch dir 裡每個檔案 `os.utime(f, None)`**；一次 `copy --files-from` 送段、再一次送 `session.md`；失敗清掉 batch dir
- [ ] 3.12 `store.mirror_one`（`:616-634`）／`store.sync`（`:1431-1543`）：完整性改用 `raw_complete`；`None`（舊／未知）→ **建索引、標成 raw 不可用、不刪鏡像、不刪索引**，`unfinished.json` 記 `legacy: true`；`False` → 照現行「還沒寫完」跳過；`unfinished.json` 升到 `version: 2`，記 `{session_md5, raw_fingerprint}`（manifest 的 `(file, md5)`＋listing 實際 md5 的 canonical JSON 的 md5），v1 檔忽略
- [ ] 3.13 `store.ensure_raw_file(paths, drive, ulid, header) -> Path`：鏡像已有整份且 md5 對 → 直接回；`layout != "chunks/1"` → `StoreError`（舊／未知，raw 不可用）；否則一次 `copy --files-from` 下載缺少的段到 `<cache>/segments/<ULID>/`，逐段驗 md5，依序串接、驗整份 md5／size，原子寫入鏡像、刪暫存段與其他舊 `raw-<md5>`；**段 404 → 重抓一次 `session.md`，保留仍在新 manifest 的暫存段，只補缺的**；`store.fetch_raw`（`:1600-1619`）改用它再讀 bytes
- [ ] 3.14 `cli.cmd_show --raw`（`:2584-2586`）：整份存在時用 `shutil.copyfileobj` 串流到 `sys.stdout.buffer`
- [ ] 3.15 `cli.cmd_edit`（`:2555`）／`cmd_continue`：`layout != "chunks/1"` 的 Session 在抓 raw 之前就給明確錯誤（「raw 是舊格式或未知格式，請先遷移（agora migrate adopt）」），不要走 `fetch_raw` 的半套
- [ ] 3.16 `cli._import_one`（`:514-570`）：claude 用 `collect_increment(launch=None, cursor=None)`（或等價的串流 `export`＋manifest），讓第一次匯入就是分段；`raw_src` 用 `raw_file`（`:529` 已經是）；fork（`cli.py:2049`）／merge（`cli.py:751`）維持 bytes／整份
- [ ] 3.17 `store.push_mirror`／`cache._push_mirrored`（`cache.py:328-344`）：分段版從鏡像的整份 raw 按 manifest 切出缺少的段、**用 batch dir＋一次 `copy --files-from`** 上傳、驗證（`session.md`＋每一段）；`--not-exist-upload` 也走這條（不然 revive 一個 814 MB 的 session 是 195 次 `copyto`）
- [ ] 3.18 `store._rescue_deleted`（`:978-1044`）：另存的 Y 也走分段（同一個 `stage`）；raw 以**檔案路徑**交給 `stage`（`:325` 接受 `Path`），不要 `read_bytes()`（`:997`）
- [ ] 3.19 `_upload_checked`（`:584-605`）／`push_outbox`：分段版送完段與 `session.md` 後驗 `session.md` ＋每一段的 md5
- [ ] 3.20 `tests/fakes/fake_rclone.py`：記錄每次 `copy --files-from` 的來源檔名與大小；支援「只讓某個段失敗」「上傳後立刻 listing 看不到某個段」「`ModTime` 可控」；**`copy` 要保留來源 mtime（`shutil.copy2` 或 `os.utime`；現在是 `shutil.copyfile`，`:163`、`:173`）並在 `lsjson` 回 `ModTime`**（資料夾 `ID` 的 fake 在 R2 的 2.4）
- [ ] 3.21 測試：814 MB（或小 `CHUNK` 的等效）多輪——第 2 輪起只送新段＋tail；`calls.log` 的 bytes 與次數符合 `C+I+S`；Drive 上的段接起來等於整份；第一次切段是約 195 個檔、不是一個巨段；一路 append 的段數符合 `ceil(size/C)`～`2·ceil(size/C)`；**小 session 是一個段**
- [ ] 3.22 測試：崩潰點（`after-manifest`／`after-segment-cut`／`after-segment-upload`／`after-session-upload`／`after-confirm`／`before-cleanup`／`after-cleanup`）——每個點之後舊版仍完整、下一輪收斂、I1–I5 成立
- [ ] 3.23 測試：跨機器——A 寫、B sync＋append、A 再寫並清掉 B 的基準 → B 下一輪補回缺的段；**別台換 tail／`gen+1` 不得觸發 purge 或 `_rescue_deleted`**；**複製期間被 purge → 確認時 ID 不同 → purge＋另存 Y，Drive 上沒有 X**；**只剩孤兒段 → 下一輪 rescue、不復活**；`edit` 只換 header 不重傳段；`push --not-exist-upload` 重建段
- [ ] 3.24 測試：舊格式——手工造沒有 `layout` 的 `session.md`＋`raw-*.json` → sync 建索引、`show` 可讀、`show --raw` 明確錯誤、鏡像與索引都還在、`unfinished.json` 記 `legacy`、下一次 sync 不再下載；未知 `layout`（`chunks/9`）同一個行為
- [ ] 3.25 測試：清理新規則——別台剛傳的段（寬限期內）不被刪；**前一版 manifest 是 `state/published` 的記錄、md5 對得上**才保護（離線連 stage 三代）；記錄對不上時只剩寬限期；孤兒段超過寬限期才被刪；清理在確認前不跑
- [ ] 3.26 測試：`ensure_raw_file` 的失敗路徑（段 404 → 重抓 header 且已驗段不重抓；md5 不符 → 不留檔；size 不符 → 不留檔；網路錯 → `StoreError`）
- [ ] 3.27 改壞驗證：設計 I.2 的 1–19（第 20 條屬 R1；21–24 屬 R4）——各要紅
- [ ] 3.28 實作環境變數：`AGORA_RAW_CHUNK`（bytes，預設 4194304；測試 4096）、`AGORA_RAW_ORPHAN_GRACE`（秒，預設 3600）；解析失敗 → 明確錯誤，不靜默回預設；**沒有** `AGORA_RAW_LAYOUT`／`AGORA_RAW_SEGMENT_MIN`
- [ ] 3.29 量測：`tests/perf/measure_continue_mem.py` 或新的小腳本，記上傳 bytes／rclone 次數／輪耗時／子行程峰值；和 `docs/perf.md` 的現值對照；另記 listing 的總量（N 個長 session）
- [ ] 3.30 跑整套 `uv run pytest -q tests/unit`＋`openspec validate agora-diff-upload --strict`；`test_mem_golden` 的 `GOLDEN_MD5_SESSION_MD` 已依 0.2 簽核重算

## 4. R4：搬遷、盤點、文件、量測（PR 4；c；依 R1、R3；**K.1 已過**；`adopt` 依 0.7）

- [ ] 4.1 `cli.py`：新 action `migrate`＋子命令 `inventory`（唯讀）：讀本機索引的 `sessions` 表（`ulid`、`agent`、`source_id`、`md5`、`json_extract` 只取需要的欄位（`$.agora.source`／`.relation`／`.raw.md5`／`.size`／`.layout`／`.file`／`.segments`（只留檔名／md5／size），lineage 另用 `$.agora.parents`）；**不 SELECT `body`**、**不把整個 `header` 載進來**）＋Drive listing（只有檔名／大小／md5）＋本機 agent store 的存在性（`claude.find_jsonl`、`opencode.list_sessions` 的 id 集合、`agy_store.db_path().is_file()`）；分類 `local-present`／`local-missing`／`other-machine`／`host-unknown`／`no-source`／`agent-unavailable`／`drive-only`／`new-format`；`old_children` **不用 `Index.children`**（它會解析每個 header，`store.py:1263-1274`），用只讀 id 的 `json_each(header,'$.agora.parents')` 查詢＋排除 `cloud_missing`；`--json` 只輸出 host、counts、每筆的 `ulid`／`agent`／`external_id`／`host`／`relation`／`layout`／`raw_size`／`class`／`replacement`（物件：`ulid`／`relation_to_old`／`raw_size`／`reason`）／`old_children`（**沒有 title／description／tags／body**）；**不下載任何 `session.md`**；**不呼叫 `store.sync`／`kick_uploader`**（只列 Drive、不下載、不更新鏡像、不上傳）。`replacement` 的 `identical` ＝ 兩邊整份 `raw.md5` 相同**且兩邊（舊的與替代版本）都是 Drive 上那一份**（索引的 `session.md` md5 等於 listing 的）**且替代版本列的每一個段都在 listing**（重用 E.1 的 `raw_complete`）；其餘是 `different`（附 `reason`：`md5-differs`／`drive-version-elsewhere`／`segments-missing`／`old-drive-version-elsewhere`／`old-not-on-drive`）；無法比較是 `null`。**索引版本檢查**：開 `Index` 前先看 `PRAGMA user_version`；不符就明確報錯、要求先跑一次正常指令（重建會讀鏡像的 `session.md`，不能讓 `inventory` 觸發）
- [ ] 4.2 測試：`inventory` 對手工造的假索引（每個類別各一筆）輸出正確分類、`replacement` 與 `old_children`；輸出不含 body／title（斷言 JSON 的 key 集合）；`drive-only` 只出現 ULID；`new-format` 不被列入丟掉候選；**原檔變短後重匯 → `different` 且不在丟掉候選**；**替代版本還在 outbox（Drive 沒有它的 `session.md`）→ `different`／`drive-version-elsewhere`**；**替代版本的 `session.md` 在、但 listing 少一個段 → `different`／`segments-missing`、不在丟掉候選**；**索引 `user_version` 不符時 `inventory` 報錯、不重建（不讀鏡像的 `session.md`）**；**`calls.log` 沒有任何 `copy`／`copyto`（只有 listing）**
- [ ] 4.3 `cli._import_one`（`:514-570`）：新開關 `--as-new`（遷移用）——**忽略同一個 `agent`＋`session_id` 的舊格式 Session**；若已經有同來源的 `chunks/1` Session 就回它（重跑 no-op；`inventory` 用同一個查詢，但另外要求那一版在 Drive 上、段都在）；否則建一個新的 Session（`relation: import`、**不設 parents**，否則舊 Session 會因為「有子 Session」而刪不掉）；**不要**走 `cli.py:544-552` 的「內容不能變少」檢查（那會把舊 raw 讀進記憶體）；舊 Session 一個位元組都不動。**同時**：沒有 `--as-new` 時，`existing[0]` 不是 `chunks/1` → **拒絕**（exit 非 0，訊息指向 `--as-new`／`adopt`）；不走 `cli.py:547-552` 的 fail-open、也不走 `cli.py:566-573` 的分支路徑（會建出 `parents` 指到舊 Session 的新 Session）。測試：舊 session 還在索引裡時 `import --as-new` 產生新 id、舊的還在；重跑回同一個新 id；同來源已有 `chunks/1` 時 no-op；**一般 import 對舊格式被拒絕（Drive 與本機不動、沒有新子 Session）**
- [ ] 4.4 `cli.py`：`migrate adopt <id>…`（**條件式，依 0.7**）：只處理 `layout != "chunks/1"` 的；**五條守則**（design F.3）——取上傳鎖；`store.continuing_details` 非 None → 跳過並列出；`outbox/<ULID>`（含 `.done-` 等 set-aside）存在 → 拒絕；**先用 listing 拿 Drive 上 `session.md` 的 md5 當基準**（本機那份不同就先下載 Drive 的、用它的 header），**發布前再列一次比對，不同 → 中止、不動 Drive**；收尾只刪記下來的那個 `raw.file` 檔名（其他 `raw-*` 不掃）。整份 raw 由鏡像（`raw-<md5>`）或下載舊 `raw.file`（驗 md5）取得；同一個 header、**同一份閱讀版 body（用 Drive 上那一份，不重算）**、同一個 id、`update=True` 走 `stage`；在持鎖下走**與批次上傳同一條分段上傳路徑**（段 → `session.md` → 核對每一段；不是 `_upload_checked` 的單檔 `copyto`）；確認成功後用同一條清理規則刪舊段、寫 `state/published/<ULID>.json`，並**只刪開始時記下的那個 `raw.file` 檔名**（一次 `delete --files-from`，進垃圾桶；其他 `raw-*` **不掃**）；MUST 在確認之後（L14）
- [ ] 4.5 測試：`adopt` 後同一個 id、`chunks/1`、manifest 完整、舊 `raw-*` 不在（垃圾桶）、header 的其他欄位（tags／refs／case／relation／parents）一個都沒變、閱讀版 body 與 Drive 上那份相同（含手改過的情境）；舊 raw md5 與 header 不符 → 拒絕、Drive 原樣不動；**別台在 adopt 期間改過 `session.md` → 發布前中止、Drive 原樣不動**；**本機 header 比 Drive 舊 → 用 Drive 那份、不要蓋回去**；**正在 continue 的 → 跳過並列出**；**outbox 還有舊版本 → 拒絕**；**只刪記下來的那一個 `raw-*` 檔名（別台同時寫回的新 raw 還在）**
- [ ] 4.6 `docs/design.md`：4.1 的 Drive 佈局與上傳順序、4.3 的 sync、5.10 的 pull／push、刪除偵測（R2 已寫的部分只補分段相關）、`raw.file` 不再存在、`unfinished.json` v2
- [ ] 4.7 `README.md` 的 Drive 目錄表與 `docs/acceptance.md` 相關列：分段 session 的檔案形狀、`show --raw` 的接回、舊格式的「可讀但 raw 不可用、不要刪」
- [ ] 4.8 `docs/perf.md`：新增兩段量測——（a）814 MB 的第 2 輪起每輪 bytes／rclone 次數／輪耗時／listing 總量；（b）匯入 100／400／800 MB 的峰值／時間／磁碟（R1 的結果）；寫明前提（`C+I+S`）與未知（K4–K6）
- [ ] 4.9 搬遷文件（照 design F.1，不留舊說法）：升級→**先跑一次正常指令讓 `Index` 的版本升級重建完成**→盤點→重匯（**一定要 `--as-new`**，並說明為什麼；一般 import 對舊格式已經會拒絕）→**丟之前緊接著再跑一次 `inventory`，以那一次的輸出為準**（`delete` 的預覽看不到「相同」）→**丟掉只丟 `identical` 的**（兩邊都是 Drive 上那一份、替代版本的每一個段都在 listing、整份 `raw.md5` 相同；`different` 的逐筆決定，附兩邊大小；一條 lineage 有一個 `different` 就整條先留）→丟掉以 lineage 為單位（父有批外子 Session 會被拒絕；要走 adopt 的子孫，祖先也只能 adopt）→驗收→其他機器；`agora delete … --yes` 的預覽與垃圾桶 30 天；本機鏡像立刻消失、不可逆的部分（tags／lineage）；A／B 兩條路徑與建議；**確認點 ② 要等上傳完成**（`identical` 才代表替代版本真的在 Drive 上、段都在）
- [ ] 4.10 `experiments/agora-chunks/probe_drive.sh`（或 `.py`）的**規格**與一支不連真 Drive 的乾跑（`--dry-run` 只印要跑的指令）；含 `mkdir sessions/probe` → 記 `ID` → `purge` → `mkdir` → 再記 `ID`（K17）與 `ModTime`（K13）；真正的探針由本人或 PM 跑（design K.1）
- [ ] 4.11 `tests/perf/measure_import_mem.py` 的正式版本（R1 的草稿轉正）＋一條 CI 之外的說明；真的量測由任何人跑（不碰真資料）
- [ ] 4.12 隱私：文件寫明「掃描整份 raw，不掃單一段檔」；`inventory` 的輸出形狀寫進文件（不含 title／body）；範例不含真 token、真路徑、真 session；垃圾桶 30 天的隱私面寫進文件（L11）
- [ ] 4.13 驗收：照 4.9 的步驟在隔離環境（fake rclone、假 HOME、假索引）走完 F.1 的六步與三個確認點（含「原檔變短 → `different` → 不在預設清單」，以及「替代版本還沒上傳完 → 不是 `identical`」，以及「第 4 步前再跑一次 `inventory` 才是給 `delete` 的清單」）；`openspec validate agora-diff-upload --strict`
- [ ] 4.14 歸檔：本 change 的實作 PR 合併後 `openspec archive agora-diff-upload`（把 delta 併進主 spec）
- [ ] 4.15 改壞驗證（I.2 的 21–26）：`inventory` 觸發 sync／下載（要紅）；把 `different` 當成可以丟（要紅）；替代版本少一段仍判 `identical`（要紅）；`inventory` 自己觸發索引重建、讀鏡像的 `session.md`（要紅）；`adopt` 發布前不比對 `session.md`（要紅）；一般 `import` 遇到舊格式仍更新／分支（要紅）

## 5. R5：翻預設（取消；不是任務）

這一節保留只為對照上一版：沒有 `AGORA_RAW_LAYOUT`，沒有「預設關閉→翻預設」這種順序；只有一種格式（design A.5、J.1）。**沒有勾選項**——它不該出現在任何人的待辦裡。

## 6. 後續（不在本 change；列出來避免忘記）

- [ ] 6.1 段壓實（design D.2）：段數 > 512 時，由 uploader 從鏡像整份 raw 合併相鄰封存段
- [ ] 6.2 aux 很大時拆成獨立的段（design K5）；先量測真實 sidecar 大小
- [ ] 6.3 opencode 的串流解析（`json.load` 的 O(匯出) 界限，design B.2、K15）
- [ ] 6.4 agy 的 `scan_text` 串流（design B.2、K5）
- [ ] 6.5 `session.md` 的 body 差分（小 header＋body 段；design H）
- [ ] 6.6 continue 的「只解析新增行」：不重讀整份 jsonl 前綴（design B.2、附錄）
- [ ] 6.7 重匯既有長 session 的「內容不能變少」檢查改成串流（design B.8 的已知限制）
- [ ] 6.8 非 claude 的 full 改用內容定址的段名（拿掉 `gen`，只換有變的段）：省下「每輪 12 份整份」的線上重複與上傳（design A.1；v1 只寫明，不做）
