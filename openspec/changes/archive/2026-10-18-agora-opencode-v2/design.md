## Context

opencode 2.0.26 的實測（MyLinuxPool spike 1.2 報告 §A～§H；issue #51 有摘要）與 agora 661eb7d 的現況：

- **指令**：`opencode export／import` 在 v2 不存在；搬到 `opencode session export／import`，兩者都是 server 操作（`--standalone`／`--server`），import 多了 `--directory`。`session delete <id>` 名字不變，但要 server 上下文。`opencode run --format json` 的事件形狀照舊。`serve --hostname --port` 不用改。
- **格式**：v2 匯出是扁平的——`info` 只有 `cost, id, location, projectID, outcome, time, title, tokens`（`directory` → `location.directory`，沒有 `version`／`model`／`slug`），`messages[]` 的每一列是 `{…, type: "user"|"assistant"|"idle"}`，assistant 的內容在 `content`。v2 **拒收** v1 巢狀 payload（`SchemaError: Missing key at ["info"]["location"]`）。
- **互動模式**：v2 換表——`session` → `session_v2`（含 `id, directory, title, time_updated, time_idle, idle_outcome, …`）、`message`／`part` → `session_message`（`id, session_id, type, seq, time_created, time_updated, data`（JSON））。
- **坑（資料與埠是兩件事）**：v2 的 CLI 沒帶 `--standalone`／`--server` 時會去找／試著起 managed 背景服務；預設埠 **49374 是全機共用的**，隔離四個 XDG 不會隔離埠。要換埠得在隔離的 XDG_CONFIG 下設定（`opencode service set port <埠>` 或等價設定；報告的錯誤訊息說的就是這個）。
- **未測**（報告 §G）：工具呼叫的 parts、跨訊息 reference、import 必填欄位全集、互動模式 SQL、live serve 期間的 export、非 headless 的 `opencode --session <id>`；spike 的模型是自建 fake provider，而且那份 fake upstream 不在這個 repo。

約束：不碰使用者的真 opencode 資料與憑證；repo 是 public（fixture 只能放自編、去識別化的內容）；測試要在沒有真 opencode 的機器上也能跑（fake 為主）；改動集中在 `src/agora/agents/opencode.py`（報告 §F'／§H）。審查（`docs/review/agora-opencode-v2-design-review.md`）指出的兩件事是這份設計的硬條件：**第 1 步合併後不能把大聲的失敗換成靜默的空閱讀版**（H1）、**隔離要連埠一起隔**（H2）。

## Goals / Non-Goals

**Goals：**

- 在 opencode 2.x 上，匯入、接續（含 `--headless`）、`--fork`、merge 後接續與 TUI 的列表／預覽／搜尋照常；閱讀版 MUST NOT 再靜默變空。
- 改動集中在 `agents/opencode.py`（＋fixture／fake／conftest／文件）；每一歩一個 PR、可以單獨合併、單獨測。
- **第 1 步單獨合併的結果必須是「比現在好」**：v2 的匯入成功時閱讀版就是完整的（v2 的 `turns()` 跟著第 1 步一起進來，H1）。
- 之後 opencode 再改版時，失敗模式是「明確的錯誤＋fixture 要跟著更新」，不是靜默丟內容。

**Non-Goals：**

- 舊 raw 的 v1→v2 轉換（本 change 先明確拒絕，見 Decisions 8；轉換器列在 Open Questions）。
- `start_native` 的 `serve`（實測零改動）、sidecar 的 HTTP 面向（MyLinuxPool#114）、claude 與其他 agent。
- 新的使用者介面：沒有新旗標、新環境變數（XDG 的透傳也不加，見 Decisions 9）。
- **第 1 步之後支援 opencode 1.x 的 CLI**：指令改用 `session export／import`（1.x 沒有），嗅探只保住 payload 的讀取，沒有指令的 fallback（見「本人未指示、作者提議」）。
- 未來版本（v3）或未知欄位的相容——遇到就明確報錯。

## Decisions

### 1. 格式嗅探與適配層的位置

- 嗅探一律走一個函式（名字實作時定，下稱 `_payload_format`）：**每一列都要同質**——`messages[]` 的每一列有 `type` → v2；每一列有 `info` → v1；混合（例如 `append_native` 在 v2 raw 上接了 v1 形狀的列）→ `AgentError`。只看第一列不夠（L1）。
- `messages` 是空或不存在時退一步看 `info`：有 `location` → v2，有 `directory`／`slug` → v1；兩者都不是 → `AgentError`（明確訊息）。
- **讀寫分開**：讀（`export` 的欄位、`turns`、`reidentify`、互動模式）接受 v1 與 v2；寫（`native()`、`append_native()` 接出來的新 payload、`_import` 送出去的）只有 v2（本機安裝的 opencode 已是 2.x；v2 拒收 v1）。
- **位置：全留在 `src/agora/agents/opencode.py`**，以對稱的小函式分組（v2 專屬的 `_v2_*`、既有的 v1 路徑不動），不新增 module、不新增類別、不動 `Agent` protocol。理由：這個檔案本來就是唯一知道 opencode 的地方（design.md 5.2），跨檔散開會讓「誰知道哪個格式」變模糊；報告也說範圍集中。替代方案是 `opencode_format.py`（內部邊界較清楚、但要跨檔追、測試要多一層 import）——不採用。
- **為何不用 `opencode --version` 猜格式**：存下來的 raw 可能是舊版產生的（store 裡就有 v1 的），事實是 payload 的形狀，不是 CLI 的版本字串。

### 2. 指令、旗標與 server 上下文（全部在第 1 步）

- 新增一個地方組 server 上下文（下稱 `_server_args()`）：本 change 一律 `--standalone`。**匯出、匯入、刪除、寫要約在同一步進來**（審查 M7）——PR 1 自己的失敗清理就走 `_discard`→`_delete`，切在後面的步驟會讓同一條流程一半帶旗標、一半不帶，spec 的 MUST 也要等到後面才成立。
  - `_export_bytes`：`opencode session export <id> --standalone`（stdout 進檔案、stderr 分開照舊）。
  - `_import`：`opencode session import <path> --standalone --directory <workdir>`，cwd 與 `PWD` 都給 workdir（`PWD` 陷阱見 docs/spike/opencode.md）。
  - `_delete`：`opencode session delete <id> --standalone`（`_discard`、`_drop_summary_session`、pending 的補刪都走它）。
  - summarize：`opencode run --standalone <訊息> -f <材料> --format json`（`--standalone` 插在 `run` 後；「訊息在 `-f` 前」的順序照舊）。
  - `tests/fakes/opencode_noninteractive.py` 的透傳（`export`／`import`／`session delete`）跟著更新，不能等到第 5 步（M7）。
  - `start_native` 的 `serve --hostname 127.0.0.1 --port <p>` 不變。
- **為什麼是 `--standalone` 而不是 `--server`**：agora 呼叫的時機（匯入前、agent 結束後、寫要約）多半沒有現成的 server，`--standalone` 不需要先知道位址。`--server` 的語法與行為沒實測過——**第 1 步就要在隔離環境量一次**（對一個運行中的私有 serve 跑 `session export／import --server …`，L4），結果記進 `docs/spike/opencode.md`；若量到 `--standalone` 與 live serve 相衝，`_server_args()` 的形狀再改（那會是第 1 步內的事，不留到驗收）。
- 錯誤訊息照舊（例如 `Session not found` → 「找不到 Session」）；v2 的錯誤文字若不同，以 fake 與整合測試鎖住。

### 3. v2 的 payload：native()／append_native()（第 2 步）

- **必填欄位的方法（照報告 §F'-4）**：拿真 session 的 `session export` 當模板，再逐欄餵 `session import`，看 `SchemaError: Missing key at [...]` 補齊；欄位全集（`info` 與每種 message 列）記進 `docs/spike/opencode.md`，fake 依同一份驗證（缺欄位就拒絕）。
- `native()` 的 v2 形狀：`info` 照模板的必填欄位（含 `location`、`cost`、`tokens`、`time`、`outcome`……以實測為準，`_session_info` 的欄位集跟著換）；訊息列照 v2 的 user／assistant 列（`id`、`time`、`type`；assistant 的 `content` 一則一行一個 text）；**不寫 synthetic／attachment**（設計 4.4：打開就看得見）。placeholder id 沿用 `ses`／`msg`／`prt` 前綴（v2 的前綴要求以 fixture 複驗）。
- `append_native()`（**審查 M1**）：複製 v2 raw、接上新訊息列；原 raw 內容相同（語意相等；byte-for-byte 只對 store 裡的原 raw 成立）（OC2）。**底下只懂 v1 的 helper 要逐個寫 v2 版**——v2 扁平列沒有 `info`，它們現在會回空／None，輕則被 `SchemaError` 拒絕、重則匯入鏈不對的 Session（靜默）：
  - `_apply_revert`（讀 `info.revert`／`messages[].parts`）、
  - `_role_fields`（讀 `info.role`／`providerID`／`modelID`）、
  - `_unused_stem`（讀 `info`／`parts` 的 id）、
  - append_native 內文的 `said`／`last_question`／`_created_ms(m.get("info"))`、
  - `_last_model`（讀 `info.modelID`）。
- **不確定就少改**：`reidentify` 只動 fixture 上實際存在的 id／參照欄位；沒有證據的欄位不碰。

### 4. 隔離環境、fixture 與 fake upstream

- **能發 tool call 的 fake upstream 是 1.1 的前置交付物**（審查 M6）：放 `tests/fakes/`（本機、不連外、不用憑證），能回一句話與一次工具呼叫，讓 fixture 與驗收都能重跑。做不到時的出口：先收純文字 fixture，工具形狀記成 open，閱讀版對工具先 `[skip tool]`，並在 PR 說明標明——但要當成「還沒做完」，不是預設路線。
- **產生真 session 的隔離步驟**（fixture 與驗收共用）：
  1. 四個 XDG（`XDG_DATA_HOME`／`XDG_CONFIG_HOME`／`XDG_STATE_HOME`／`XDG_CACHE_HOME`）指到暫存目錄，`HOME` 也指到暫存；用 `opencode debug paths` 確認位置。**不讀憑證、不碰真 store**。
  2. **在隔離的 XDG_CONFIG 下把 managed 服務的埠改成一個空閒埠**（`opencode service set port <埠>` 或等價設定）：預設 49374 是全機共用的，只隔資料目錄不夠（H2）。
  3. 清掉／控制繼承來的 `OPENCODE_*` 環境變數；pytest 外的步驟（fixture 產生、驗收）另外自己設 `AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR`／`AGORA_RCLONE`／`RCLONE_CONFIG`（`tests/conftest.py` 的隔離只在 pytest 內，L3）。
  4. 工作目錄用暫存目錄（不是 repo、不是使用者的專案）。
  5. 產生自編的多輪對話＋工具呼叫：`opencode run --standalone`（用 1.1 的 fake upstream 或免費模型）；第二輪用 `-s <id>`，讓 payload 出現跨訊息參照；工具呼叫用「讀取暫存目錄裡自己寫的檔案」這種無害動作。
  6. `opencode session export <id> --standalone > fixture.json`；另外把 v2 DB 的 schema 與需要的列匯出成文字（互動模式 fixture 用）。
  7. **去識別化清單**（L2）：`providerState`、`projectID`、HOME 路徑、主機名、email、時間戳與 id 逐一檢查；用 fake upstream 產生，確保沒有真模型的輸出。只放自編內容，一般化絕對路徑後放 `tests/fixtures/opencode/`（public repo）。
  8. **fixture 至少要有一個跨訊息參照**（例如 assistant 列指到它回答的那一則），否則 reidentify 的改壞驗證會是空的（L6）；沒有就補一輪或改用有工具的回合。
- **開跑前後確認預設埠的服務沒被動**（H2）：1.2／5.1 在跑真 opencode 的前後各看一次 49374 有沒有在聽（PID／etime 或等在聽的狀態），**只看服務在不在，不讀任何資料**。被動到就是失敗。

### 5. 閱讀版與計數（第 1 步）

- v2 的 `turns()`／`_lines_of()` **跟第 1 步一起進來**（H1）：user 列的 `text` → 一行；assistant 列的 `content` 內每個 text → 一行、每個 tool → 一行 `[tool] <name> <input>`；`idle` 列略過；`files`（附件）不算對話。工具 content 的實際形狀以第 1 步的真 fixture 複驗（報告 §G-1）。
- 第 1 步的驗收測試 MUST 包含「匯入 v2 fixture 後，body 有 user／assistant 段落」；否則第 1 步就是把大聲的失敗換成靜默的空閱讀版（H1）。
- 既有規則不變：不輸出 reasoning／step／tool 結果；不認得的型別 `[skip <type>]`；`native(turns(raw))` 的 round trip 要成立（native 在第 2 步，round trip 也在第 2 步）。
- **計數定義**（審查 M8／N4）：`message_count` 是「user／assistant 列數」，與 `turns()` 一致；`idle` 不是對話、不算新內容——這半（一行 `len`）跟 `export()` 一起在**第 1 步**落地，第 1 步的 `_import_verified` 先只比訊息數（與下面「沒有這一層時只比訊息數」一致）；單元與真 opencode 的案例在第 2 步補。`collect()` 的「沒有新內容」比較、`native()` → import → export 的列數比對都用這個定義。
- **v2 的「part」定義**（審查 M2；第 2 步落地）：回讀驗證第 2 層改成「該格式可數的內容項目數」——v1 是 `parts` 數、v2 是 assistant 列 `content` 的項目數；該格式沒有這一層時只比訊息數（第 1 步就是這個情形）。spec 的句子照這個說法改（見 delta spec），不再寫死「part 數」。

### 6. 互動模式的 DB

- **選表**：先看 `session_v2` 與需要的欄位；沒有就讀 v1 的 `session`。**兩者都在時以 v2 為準，但只出現在 v1 表的 Session MUST 仍被列出**（以 id 聯集、v2 的資料優先）——升級若沒把舊 Session 遷進 `session_v2`，清單不會把它們藏掉（審查 M4）。都沒有 → `_warn_schema()` 一次＋空清單（照舊）。
- **3.1 是這一步的第一步**：在隔離環境量「v2 升級有沒有遷移舊 session」「舊表還在不在」「同一顆 Session 在兩表的 id 是否相同」，記進 `docs/spike/opencode.md`；量到沒遷移時，id 聯集的 fixture 要涵蓋「只在舊表的 Session」。（Open Questions 原第 3、4 項不再是問句。）
- 清單 SQL 照實測欄位：`select id, directory, title, time_updated from session_v2 order by time_updated desc, id desc`；舊表的列以同一組欄位讀，聯集時同 id 用 v2 的列。
- 預覽與搜尋改讀 `session_message`（`data` 是 JSON）：v1 的規則不變（最後 20 則、只算 user／assistant、text 且非 synthetic、截 2000 字、NFKC＋casefold、LIKE 先掃再全掃、程式確認命中），只是表與欄位換掉；實際欄位（`type`、`data` 的形狀、排序用 `time_created` 或 `seq`）以 3.1 量到的真 DB fixture 定案。
- `_session_directory`：用選中的表；`row` 先設 None，表不存在或查不到就回 None——修掉 spike 量到的 `cannot access local variable 'row'`（`_db()` 吞掉 sqlite3 的錯誤之後，變數從沒被賦值）。

### 7. summarize 與 delete

- server 上下文在第 1 步（Decisions 2，審查 M7）。`_last_reply` 的事件解析不用改（v2 `run --format json` 形狀照舊，實測）。
- 第 4 步剩的是 v2 的行為驗證：`_model_of` 讀 assistant 列的 `model.id`（v1 是 `modelID`，形狀以 fixture 複驗）；summarize 的成功／空回覆／失敗／逾時；pending 補刪；`_drop_summary_session` 失敗時保留記錄；merge 的刪除警告。**第 3、4 步在第 1 步之後可以並行**（M7）。

### 8. 舊 raw（v1）的拒絕與重新匯入

- 情況：opencode 2.x 之前匯入的 Session，raw 是 v1；opencode 2.x 的 `session import` 拒收（實測）。
- **決定**：同 agent 接續／fork 在開 agent 前拒絕（發生在 `start_native`／`append_native`，那時 pending 還沒寫，所以原 Session 與 raw 不動）。
- **訊息要誠實**（審查 M5）：重新匯入**不一定**原地更新——被 merge／continue 分支過的 Session 會走「保留舊版、另開分支」；來源不在本機時連重新匯入都做不到，只能跨 agent 接續。訊息 MUST 這樣說，不誤導使用者。
- **原地更新加「有內容的 turn 不能變少」的檢查**（M5／N1；第 2 步，動 `cli._import_one`）：重新匯入一個既有 Session 之前，比對新匯出與 store 裡那份的 user／assistant turn 數，**只算至少有一行內容的 turn**（legit 的 undo／revert／rewind 讓空 turn 消失，不算變少）；新的比較少（遷移丟東西等）就不覆蓋——拒絕並**寫明下一步**（改用跨 agent 接續，或先讓這個 Session 分支再重新匯入、依分支規則另存），不能只說少了多少。「不變」與「變多」照舊更新；分支過的情形照現行規則另存。spec 的 delta 在 `batch-commands`。
- **不採「自動重新 export 本機那份」**：本機 DB 的那份不一定和 store 裡的 raw 是同一份（跨機器 import 的 raw 在本機可能沒有、或比較舊），靜默換掉內容比拒絕更糟。
- **不採 v1→v2 轉換**（本 change）：工具呼叫在 v2 `content` 的形狀還沒複驗（§G-1），現在寫轉換器等於用猜的。留下來當後續單（Open Questions）。
- 代價：舊 Session 在重新匯入前不能同 agent 接續；這是明確、可回復的限制，重新匯入會用 v2 的匯出覆蓋 store 裡的 v1 raw（列在「本人未指示、作者提議」）。

### 9. 資料目錄（XDG）：agora 不指定；埠是另一件事

- 選項：
  1. **現狀**：不指定、環境完全繼承（pass-through）。
  2. **透傳**：新增環境變數讓呼叫方經 agora 注入（例如 `AGORA_OPENCODE_XDG_*`）。
  3. **agora 自己指到預設的隔離目錄**。
  4. **呼叫方負責**：sidecar／supervisor／排程器在啟動 agora 前設好四個 XDG。
- 分析：opencode 只認環境變數（`opencode debug paths` 實測）。agora 的主要用途是讀「使用者的」Session（匯入、TUI），(3) 會直接把它藏起來，等於壞掉。(2) 與 (4) 的責任重疊（worker 上的 sidecar 本來就是 spawner），還多一層介面。(4) 的責任最清楚。
- **建議：(1)＋(4)**：agora 不指定、env 照傳、不新增變數；「要隔離就在啟動 agora 前設四個 XDG」寫進 `docs/spike/opencode.md` 與驗收步驟；sidecar（MyLinuxPool#114）在自己的 spawner 統一設。
- **埠不在 XDG 裡**（H2）：測試／驗收要隔離 managed 服務時，另外在隔離的 XDG_CONFIG 下設一個空閒埠（Decisions 4）；所有自動化呼叫給明確 server 上下文（Decisions 2）是另一層，兩者都要。

### 10. 測試、改壞驗證與驗收

- 單元測試以 fake 為主（v2 語意＋fixture）：嗅探、指令／旗標、reidentify、閱讀版、native／append、計數、DB、summarize／delete。
- **改壞驗證的規則**（H2）：`--standalone`／`--directory` 的「拿掉旗標」只在 fake 上做（斷言 argv）；真 opencode 的改壞驗證不做這一項（避免刻意去走 managed 服務的路徑），只做「適配關掉」的其他項。真 opencode 開跑前後要確認預設埠的服務沒被動。
- **conftest 的 opencode 護欄**（M5；第 1 步）：`tests/conftest.py` 的 `_repo_session_ids`／清理改成 v2 語法（`session list --standalone`、`session delete <id> --standalone`），而且**指令非 0 時要大聲失敗**（pytest.fail／raise），不是回空集合——v2 上旗標不同時它會靜默失明。
- **整合測試的語法／版本探測**（L5；第 1 步）：現有的真 opencode 整合測試先探測語法（例如 `opencode session --help` 認不認得 `export`），在 v2 上 skip 到第 5 步更新；否則它們在 2.x 的機器上不是 skip，而是真的跑（並且會撞 49374）。
- 真 opencode 的整合測試（第 2、4、5 步）隔離 XDG／HOME、用空閒埠、用 fake upstream；免費模型不可用時照舊 skip 的機制留給「不是為了驗 v2 而跑」的測試，**驗 v2 的驗收與改壞驗證用 fake upstream，要的是紅不是 skip**（M6）。
- **驗收**（issue #51；第 5 步）：隔離 XDG＋空閒埠的 managed 服務＋fake upstream＋假 Drive（`tests/fakes/fake_rclone.py`）＋自編多輪含工具呼叫的 v2 session：`agora import` → `continue --headless`（三行 stderr；`--sync-period` > 0，真的做一次定期寫回；SIGTERM 後溫和停下、寫回）→ `agora show`（閱讀版完整）→ `merge` 兩顆 → 接續 merged Session。
- **非 headless 的路徑**（M3）：另外一條手動驗收（或測試）——隔離 XDG＋空閒埠的 managed 服務先跑著，`session import --standalone` 之後用 `opencode --session <id>`（或 attach）確認看得到；`opencode_noninteractive.py` 的替身只證明參數，不證明 TUI 路徑。

## 報告 §G「未測」項目的補驗落點

| §G 項目 | 落在哪一步 |
|---|---|
| 工具呼叫的 parts（v2 `content` 的形狀；閱讀版的一行） | 第 1 步的 fixture 與閱讀版（H1）；第 2 步的 native／append 用它複驗；第 5 步整條流程再驗一次 |
| 跨訊息 reference（reidentify 要改哪些結構位置） | 第 1 步的 fixture 要有多輪＋工具的參照（L6）；reidentify 只改實測存在的欄位；第 5 步用 import→export round trip 確認 |
| import 必填欄位全集 | 第 2 步：模板＋逐欄 `SchemaError` 補齊；結果記進 `docs/spike/opencode.md`；fake 依同一份拒絕缺欄位的 payload |
| 互動模式 SQL 未實作、未實測（§G-3） | 第 3 步：3.1 的量測＋真 DB fixture＋列表／預覽／搜尋的測試 |
| merge 之後的 sidecar HTTP 鏈（§G-4） | 不在本單（MyLinuxPool#114）；本單只驗「merge 建立＋接續 merged」 |
| managed 服務的生產行為（§G-6） | Decisions 9 的埠隔離＋server 上下文；第 1 步的前後檢查與第 5 步驗收（只看服務在不在，不讀資料） |
| 模型是 fake、真實對話格式細節 | 第 1 步的 fake upstream＋真 fixture；第 5 步的整條流程 |
| 非 headless 的 `opencode --session <id>`（審查 M3） | 第 5 步的手動驗收（隔離環境＋運行中的服務） |

## 本人未指示、作者提議

以下不是本人的指示，是設計時作者提的（要讓本人知道）：

- 舊 raw 用同一個 agent 接續時**明確拒絕**（Decisions 8），而不是自動重新 export 或 v1→v2 轉換。
- server 上下文一律 `--standalone`，`--server` 先不用；第 1 步量一次語法（Decisions 2；量的結果可能改形狀）。
- v1 的**讀取**支援保留（嗅探），寫入只有 v2（Decisions 1）。
- **第 1 步之後不再支援 opencode 1.x 的 CLI**：指令改用 `session export／import`，1.x 沒有這些子指令；嗅探只保住 payload 的讀取，沒有指令的 fallback；Linux worker 上的 opencode 版本未知（L7）。
- 重新匯入會**用 v2 的匯出覆蓋 store 裡的 v1 raw**（原地更新前有「有內容的 turn 不能變少」的檢查；被分支過的 Session 另存，M5／N1）。
- **「重新匯入不覆蓋更少對話」的檢查對所有 agent 生效**（寫在共用的 `cli._import_one`，claude 也走這條）：本人確認要不要限縮到 opencode（N1 (c)）。
- `message_count` 的 idle 語意：只算 user／assistant 列，`idle` 不算（M8）。
- conftest 的 opencode 護欄改成 v2 語法＋`--standalone`，失敗時大聲報錯（M5）。
- 驗收與改壞驗證用**本機 fake upstream**，不用真模型；免費模型的路徑只在「不是為了驗 v2」時用（M6）。
- XDG 不新增介面、agora 不指定；由呼叫方負責（Decisions 9）；埠另外隔離。
- 互動模式 DB 以 v2 為準、v1 的 Session 以 id 聯集補上（v2 的資料優先，M4）。
- fixture 進 repo（去識別化清單見 Decisions 4）＋DB 用文字 dump 當 fixture。
- fake 保持新舊語法都收，讓 v1 的既有測試留著（第 1 步）。
- 第 1 步先做 fixture 與 fake upstream，因為後面每一步都靠它。
- `_delete` 的 server 上下文併入第 1 步（M7）。

## Risks / Trade-offs

- [v2 的欄位全集／工具形狀未知] → fake upstream＋fixture 先行＋逐欄探測；不確定就少改；失敗是明確的 `SchemaError` 而不是靜默。
- [重新匯入的原地更新會覆蓋 v1 raw] → 「有內容的 turn 不能變少」的檢查（第 2 步；spec 在 `batch-commands`；合法的 undo 不算變少）；被分支過的 Session 照現行規則另存；訊息誠實說明。
- [managed 服務被叫到] → 全部呼叫經 `_server_args()`；測試斷言 argv；隔離環境先改埠；真 opencode 前後檢查服務沒被動。
- [headless 的定期寫回期間 serve 還活著（未實測）] → 第 1 步量 `--server`；第 5 步驗收用 `--sync-period` > 0 真的做一次寫回。
- [非 headless 的 TUI 路徑沒被測到] → 第 5 步的手動驗收（隔離環境＋運行中的服務）。
- [v2 還會改版] → 嗅探＋fixture 釘住；認不得就明確報錯，不猜。
- [DB 遷移沒發生] → id 聯集不藏舊 Session（M4）；3.1 先量。
- [conftest 護欄靜默失明] → v2 語法＋非 0 大聲失敗（M5）。
- [改壞驗證變成 skip 而不是紅] → 驗 v2 的項目用 fake upstream（M6）。
- [import 非交易式] → 回讀驗證（訊息數＋內容項目數）照舊；失敗的殘骸用 `session delete` 清。
- [fixture 帶到私人資訊] → 去識別化清單＋fake upstream 產生（Decisions 4）。

## Migration Plan

- **沒有 schema 變更、沒有資料遷移。** 部署就是換程式。
- 使用者路徑：下次 `agora import` 同一個來源時，舊 Session 的 raw 會更新成 v2（原地更新有「有內容的 turn 不能變少」的檢查；分支過的就另存）；要接續舊 Session 就重新匯入（訊息會誠實說明可能是新 Session、來源不在本機時只能跨 agent）。
- 第 1 步之後不再支援 1.x 的 CLI（指令形態變了）。
- 回退：純程式碼變更，回退 commit 即回到 v1 寫入路徑（但對 v2 安裝就是現在的壞法）。無狀態要清理。
- 開工順序：tasks 1→2→5，第 3、4 步在第 1 步之後可以並行（M7）。

## Open Questions

1. **要不要改走 `--server`**：長命 serve 的場合（headless 的定期寫回）用 `--server` 會不會比 `--standalone` 穩？第 1 步會量語法與行為，是否改形狀看量的結果；不影響現在的切法。
2. **v1→v2 轉換器**：要讓舊 Session 在**離線**（本機沒有那個 agent session）也能接續就得做；前提是 §G-1 的工具形狀已複驗。做不做、什麼時候做，本人決定。
