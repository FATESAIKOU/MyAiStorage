## Why

Mac 上的 opencode 已經升到 2.x（2.0.26）：`opencode import／export` 變成 `opencode session import／export`（要 server 上下文），匯出 JSON 從 v1 巢狀改成 v2 扁平；互動模式讀的資料表也換了（`session` → `session_v2`、`message`／`part` → `session_message`）。agora 對 opencode 的匯入、接續、閱讀版、merge 後接續與 TUI 的列表／預覽／搜尋因此失效——閱讀版還是**靜默變空**，不是報錯（issue #51；MyLinuxPool spike 1.2 報告 §F' 的 12 項）。

## What Changes

- **格式嗅探**：adapter 自己認匯出格式——`messages[]` 的每一列同質，有 `type` 是 v2、有 `info` 是 v1，混合或看不懂的形狀明確報錯；v1 的閱讀與跨 agent 轉換保留。
- **指令與旗標**：`_export_bytes` → `session export <id> --standalone`；`_import` → `session import <path> --standalone --directory <workdir>`；`session delete` 與 summarize 的 `run` 的 server 上下文**同步進來**（同一條流程不會一半帶旗標、一半不帶）；第 1 步先量一次 `--server` 的語法與行為。
- **v2 的寫入**：`native()`／`append_native()` 產出 v2 扁平 payload（`info.location` 等必填欄位）；欄位全集以真實 v2 session 的 `session export` 當模板取得，逐欄餵 `session import` 補齊；`append_native` 底下只懂 v1 的 helper（`_apply_revert`／`_role_fields`／`_unused_stem`／`_last_model` 等）逐個寫 v2 版。
- **閱讀版**：`turns()`／`_lines_of()` 改讀 v2——user 的 `text`、assistant 的 `content`、工具呼叫一行、`idle` 列略過；**跟第 1 步一起進來**，匯入 v2 後 body 就有段落（第 1 步的測試要驗這件事）。
- **計數**：`message_count` 只算 user／assistant 列（`idle` 不算新內容）；回讀驗證的第 2 層是「該格式可數的內容項目數」（v1 的 part 數／v2 的 `content` 項目數）。
- **`reidentify`**：認 v2 的訊息 id 與參照位置（只改真 fixture 上實際存在的結構位置，其餘 byte-for-byte 保留）。
- **互動模式 DB**：改讀 `session_v2`／`session_message`（v2 的資料優先）；只出現在 v1 表的舊 Session 以 id 聯集照樣列出，不會被藏掉；修好 `_session_directory` 在資料表不存在時的未賦值錯誤（`UnboundLocalError`）。
- **舊 raw（v1）的同 agent 接續**：明確拒絕，訊息誠實（重新匯入不一定原地更新、來源不在本機時只能跨 agent 接續）；MUST NOT 把 v1 payload 送給 v2 import 留下半個 Session。重新匯入既有 Session 前比對**有內容的 turn**數，**變少就不覆蓋**（拒絕訊息寫明下一步）。
- **測試與文件**：本機能發 tool call 的 fake upstream 進 `tests/fakes/`（fixture 與驗收都用它，驗 v2 不用真模型）；去識別化的 v2 fixture；假 opencode 的 v2 語意（拒收 v1 巢狀）；conftest 的 opencode 護欄改 v2 語法且失敗時大聲報錯；隔離環境連 managed 服務的埠一起隔；驗收：隔離 XDG＋空閒埠＋假 Drive＋自編多輪含工具呼叫的 v2 session，跑 import → headless continue（三行＋SIGTERM，含定期寫回）→ show → merge → 接續 merged，另驗非 headless 的 TUI 路徑。
- **資料目錄（XDG）**：agora 不指定、只透傳；要隔離由呼叫方（sidecar／supervisor）在啟動 agora 前設（design §8 列了選項與建議）；埠不在 XDG 裡，另外隔離。
- **不再支援 opencode 1.x 的 CLI**：第 1 步之後指令改用 `session export／import`（1.x 沒有）；嗅探只保住 payload 的讀取。

## Capabilities

### New Capabilities

（無；行為落在既有的接續、互動模式與 import 上，不新設能力。）

### Modified Capabilities

- `session-continue`：新增「opencode 的 v1／v2 匯出格式」與「opencode v2 的接續與 fork」——匯出格式嗅探、閱讀版不靜默丟內容、v2 的接續／fork／merge 後接續、計數與 `idle`、server 上下文；「舊版 opencode raw 的同 agent 接續」的拒絕訊息改誠實。
- `interactive-mode`：新增「未匯入頁讀本機 opencode 的儲存」——v2 資料表與 v1 的都要能讀、v2 的資料優先但舊 Session 以 id 聯集照樣列出；預覽與搜尋的既有規則不變；認不得的結構只警告一次；讀單一 Session 的專案目錄失敗時回 None。
- `batch-commands`：修改「import 一次多個」——重新匯入既有來源時，新的匯出對話數比 store 那版少 MUST NOT 原地更新（拒絕並說明）。

## Impact

- 程式：`src/agora/agents/opencode.py`（主體；嗅探、指令、reidentify、native／append_native、turns、計數、互動模式 DB、summarize／delete）；`src/agora/cli.py`（`_import_one` 的原地更新保護）。`tui.py`／`cache.py` 的介面不變（agent 的 protocol 不動）；`claude.py` 不受影響。
- 測試：`tests/fakes/` 的本機 fake upstream（新交付物）、`fake_opencode.py`（v2 語意；新舊語法都收）、`opencode_noninteractive.py`（v2 指令透傳）、`tests/conftest.py`（opencode 護欄）、`tests/fixtures/opencode/` 的 v2 session 與 DB fixture、`tests/unit/test_agent_opencode.py`、真 opencode 的整合測試。
- 文件：`docs/spike/opencode.md`（v2 一節：指令、格式、DB、埠與環境變數、`--server` 量測、fixture 產生方法、陷阱）。
- 部署：sidecar（MyLinuxPool#114）走 headless，依賴本單；worker 上的 XDG 由 spawner 設。無資料庫 schema 變更、無資料遷移。
- 後續（不在本單）：v1→v2 轉換器（離線接續舊 Session）列在 design 的 Open Questions；merge 之後的 sidecar HTTP 鏈屬 MyLinuxPool#114。
