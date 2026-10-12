## Why

Mac 上的 opencode 已經升到 2.x（2.0.26）：`opencode import／export` 變成 `opencode session import／export`（要 server 上下文），匯出 JSON 從 v1 巢狀改成 v2 扁平；互動模式讀的資料表也換了（`session` → `session_v2`、`message`／`part` → `session_message`）。agora 對 opencode 的匯入、接續、閱讀版、merge 後接續與 TUI 的列表／預覽／搜尋因此失效——閱讀版還是**靜默變空**，不是報錯（issue #51；MyLinuxPool spike 1.2 報告 §F' 的 12 項）。

## What Changes

- **格式嗅探**：adapter 自己認匯出格式——`messages[]` 的列有 `type` 是 v2、有 `info` 是 v1；看不懂的形狀明確報錯，不再是空的閱讀版。v1 的閱讀與跨 agent 轉換保留。
- **指令與旗標**：`_export_bytes` → `session export <id> --standalone`；`_import` → `session import <path> --standalone --directory <workdir>`；`session delete` 與 summarize 的 `run` 也補 server 上下文（不叫到使用者的 managed 背景服務）。
- **v2 的寫入**：`native()`／`append_native()` 產出 v2 扁平 payload（`info.location` 等必填欄位）；欄位全集以真實 v2 session 的 `session export` 當模板取得，逐欄餵 `session import` 補齊。
- **閱讀版**：`turns()`／`_lines_of()` 改讀 v2——user 的 `text`、assistant 的 `content`、工具呼叫一行、`idle` 列略過。
- **`reidentify`**：認 v2 的訊息 id 與參照位置（只改真 fixture 上實際存在的結構位置，其餘 byte-for-byte 保留）。
- **互動模式 DB**：改讀 `session_v2`／`session_message`（v2 優先、v1 後備）；修好 `_session_directory` 在資料表不存在時的未賦值錯誤（`UnboundLocalError`）。
- **舊 raw（v1）的同 agent 接續**：明確拒絕並說明重新匯入的方法（MUST NOT 把 v1 payload 送給 v2 import 留下半個 Session）；跨 agent 接續照常。
- **測試與文件**：新增去識別化的 v2 fixture、假 opencode 的 v2 語意（拒收 v1 巢狀）、互動模式 v2 DB fixture；`docs/spike/opencode.md` 補 v2 一節（陷阱與 fixture 產生方法）。驗收：隔離 XDG ＋ 假 Drive＋自編多輪含工具呼叫的 v2 session，跑 import → headless continue（三行＋SIGTERM）→ show → merge → 接續 merged。
- **資料目錄（XDG）**：agora 不指定、只透傳；要隔離由呼叫方（sidecar／supervisor）在啟動 agora 前設（design §8 列了選項與建議）。

## Capabilities

### New Capabilities

（無；行為落在既有的接續與互動模式上，不新設能力。）

### Modified Capabilities

- `session-continue`：新增「opencode 的 v1／v2 匯出格式」與「opencode v2 的接續與 fork」——匯出格式嗅探、閱讀版不靜默丟內容、v2 的接續／fork／merge 後接續、舊 raw 的明確拒絕、server 上下文。
- `interactive-mode`：新增「未匯入頁讀本機 opencode 的儲存」——v2 資料表（`session_v2`／`session_message`）與 v1 的都要能讀、以 v2 為準；預覽與搜尋的既有規則不變；認不得的結構只警告一次；讀單一 Session 的專案目錄失敗時回 None。

## Impact

- 程式：`src/agora/agents/opencode.py`（主體；嗅探、指令、reidentify、native／append_native、turns、互動模式 DB、summarize／delete）。`cli.py`／`tui.py`／`cache.py` 的介面不變（agent 的 protocol 不動）；`claude.py` 不受影響。
- 測試：`tests/fakes/fake_opencode.py`（v2 語意；新舊語法都收）、`tests/fakes/opencode_noninteractive.py`（v2 指令透傳）、新增 `tests/fixtures/opencode/` 的 v2 session 與 DB fixture、`tests/unit/test_agent_opencode.py`、`tests/integration/test_opencode_real.py` 等真 opencode 的測試。
- 文件：`docs/spike/opencode.md`（v2 一節：指令、格式、DB、fixture 產生方法、陷阱）。
- 部署：sidecar（MyLinuxPool#114）走 headless，依賴本單；worker 上的 XDG 由 spawner 設。無資料庫 schema 變更、無資料遷移。
- 後續（不在本單）：v1→v2 轉換器（離線接續舊 Session）列在 design 的 Open Questions；merge 之後的 sidecar HTTP 鏈屬 MyLinuxPool#114。
