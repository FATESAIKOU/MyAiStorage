## Context

opencode 2.0.26 的實測（MyLinuxPool spike 1.2 報告 §A～§H；issue #51 有摘要）與 agora 661eb7d 的現況：

- **指令**：`opencode export／import` 在 v2 不存在；搬到 `opencode session export／import`，兩者都是 server 操作（`--standalone`／`--server`），import 多了 `--directory`。`session delete <id>` 名字不變，但要 server 上下文。`opencode run --format json` 的事件形狀照舊。`serve --hostname --port` 不用改。
- **格式**：v2 匯出是扁平的——`info` 只有 `cost, id, location, projectID, outcome, time, title, tokens`（`directory` → `location.directory`，沒有 `version`／`model`／`slug`），`messages[]` 的每一列是 `{…, type: "user"|"assistant"|"idle"}`，assistant 的內容在 `content`。v2 **拒收** v1 巢狀 payload（`SchemaError: Missing key at ["info"]["location"]`）。
- **互動模式**：v2 換表——`session` → `session_v2`（含 `id, directory, title, time_updated, time_idle, idle_outcome, …`）、`message`／`part` → `session_message`（`id, session_id, type, seq, time_created, time_updated, data`（JSON））。
- **坑**：v2 的 CLI 沒帶 `--standalone`／`--server` 時會去找／試著起 managed 背景服務（隔離環境曾撞上共用服務的埠 49374）。
- **未測**（報告 §G）：工具呼叫的 parts、跨訊息 reference、import 必填欄位全集、互動模式 SQL、live serve 期間的 export；模型用的是自建 fake provider。

約束：不碰使用者的真 opencode 資料與憑證；repo 是 public（fixture 只能放自編、去識別化的內容）；測試要在沒有真 opencode 的機器上也能跑（fake 為主）；改動集中在 `src/agora/agents/opencode.py`（報告 §F'／§H）。

## Goals / Non-Goals

**Goals：**

- 在 opencode 2.x 上，匯入、接續（含 `--headless`）、`--fork`、merge 後接續與 TUI 的列表／預覽／搜尋照常；閱讀版 MUST NOT 再靜默變空。
- 改動集中在 `agents/opencode.py`（＋fixture／fake／文件），每一步可以單獨合併、單獨測（tasks 的五步）。
- 之後 opencode 再改版時，失敗模式是「明確的錯誤＋fixture 要跟著更新」，不是靜默丟內容。

**Non-Goals：**

- 舊 raw 的 v1→v2 轉換（本 change 先明確拒絕，見 Decisions 7；轉換器列在 Open Questions）。
- `start_native` 的 serve（實測零改動）、sidecar 的 HTTP 面向（MyLinuxPool#114）、claude 與其他 agent。
- 新的使用者介面：沒有新旗標、新環境變數（XDG 的透傳也不加，見 Decisions 8）。
- 未來版本（v3）或未知欄位的相容——遇到就明確報錯。

## Decisions

### 1. 格式嗅探與適配層的位置

- 嗅探一律走一個函式（名字實作時定，下稱 `_payload_format`）：`messages[0]` 有 `type` → v2；有 `info` → v1。`messages` 是空或不存在時退一步看 `info`：有 `location` → v2，有 `directory`／`slug` → v1；兩者都不是 → `AgentError`（明確訊息）。
- **讀寫分開**：讀（`export` 的欄位、`turns`、`reidentify`、互動模式）接受 v1 與 v2；寫（`native()`、`append_native()` 接出來的新 payload、`_import` 送出去的）只有 v2（本機安裝的 opencode 已是 2.x；v2 拒收 v1）。
- **位置：全留在 `src/agora/agents/opencode.py`**，以對稱的小函式分組（v2 專屬的 `_v2_*`、既有的 v1 路徑不動），不新增 module、不新增類別、不動 `Agent` protocol。理由：這個檔案本來就是唯一知道 opencode 的地方（design.md 5.2），跨檔散開會讓「誰知道哪個格式」變模糊；報告也說範圍集中。替代方案是 `opencode_format.py`（內部邊界較清楚、但要跨檔追、測試要多一層 import）——不採用。
- **為何不用 `opencode --version` 猜格式**：存下來的 raw 可能是舊版產生的（store 裡就有 v1 的），事實是 payload 的形狀，不是 CLI 的版本字串。

### 2. 指令與旗標

- 新增一個地方組 server 上下文（下稱 `_server_args()`）：本 change 一律 `--standalone`。
  - `_export_bytes`：`opencode session export <id> --standalone`（stdout 進檔案、stderr 分開照舊）。
  - `_import`：`opencode session import <path> --standalone --directory <workdir>`，cwd 與 `PWD` 都給 workdir（`PWD` 陷阱見 docs/spike/opencode.md）。
  - `_delete`：`opencode session delete <id> --standalone`（`_discard`、`_drop_summary_session`、pending 的補刪都走它）。
  - summarize：`opencode run --standalone <訊息> -f <材料> --format json`（`--standalone` 插在 `run` 後；「訊息在 `-f` 前」的順序照舊）。
  - `start_native` 的 `serve --hostname 127.0.0.1 --port <p>` 不變。
- **為什麼是 `--standalone` 而不是 `--server`**：agora 呼叫的時機（匯入前、agent 結束後、寫要約）多半沒有現成的 server，`--standalone` 不需要先知道位址；`--server` 的語法與行為沒實測過（報告只說有這個旗標），先不用。長命 serve 的場合（headless 的定期寫回）留給 Open Questions。
- 錯誤訊息照舊（例如 `Session not found` → 「找不到 Session」）；v2 的錯誤文字若不同，以 fake 與整合測試鎖住。

### 3. v2 的必填欄位與 payload 模板

- **方法（照報告 §F'-4）**：拿真 session 的 `session export` 當模板，再逐欄餵 `session import`，看 `SchemaError: Missing key at [...]` 補齊；欄位全集（`info` 與每種 message 列）記進 `docs/spike/opencode.md`，fake 依同一份驗證（缺欄位就拒絕）。
- **怎麼在隔離環境產生那顆真 session**（產 fixture 的步驟，也寫進 docs）：
  1. 四個 XDG（`XDG_DATA_HOME`／`XDG_CONFIG_HOME`／`XDG_STATE_HOME`／`XDG_CACHE_HOME`）指到暫存目錄，`HOME` 也指到暫存；用 `opencode debug paths` 確認位置。**不讀憑證、不碰真 store**。
  2. 工作目錄用暫存目錄（不是 repo、不是使用者的專案）。
  3. 產生自編的多輪對話＋工具呼叫：`opencode run --standalone`（自建 fake provider，或免費模型）；第二輪用 `-s <id>`，讓 payload 出現跨訊息參照；工具呼叫用「讀取暫存目錄裡自己寫的檔案」這種無害動作。
  4. `opencode session export <id> --standalone > fixture.json`；另外把 v2 DB 的 schema 與需要的列匯出成文字（互動模式 fixture 用）。
  5. 只留自編內容；把絕對路徑一般化後放進 `tests/fixtures/opencode/`（public repo，不放私人資訊）。
- `native()` 的 v2 形狀：`info` 照模板的必填欄位（含 `location`、`cost`、`tokens`、`time`、`outcome`……以實測為準）；訊息列照 v2 的 user／assistant 列（`id`、`time`、`type`；assistant 的 `content` 一則一行一個 text）；**不寫 synthetic／attachment**（設計 4.4：打開就看得見）。placeholder id 沿用 `ses`／`msg`／`prt` 前綴（v2 的前綴要求以 fixture 複驗）。
- `append_native()`：複製 v2 raw（`info.revert` 還在就照舊套用；v2 樣本沒看到這個欄位，保留防禦性讀取），接上新訊息列；原 raw 其餘 byte-for-byte 保留（OC2）。
- **不確定就少改**：`reidentify` 只動 fixture 上實際存在的 id／參照欄位；沒有證據的欄位不碰。

### 4. 閱讀版（`turns()`／`_lines_of()`）

- v2：user 列的 `text` → 一行；assistant 列的 `content` 內每個 text → 一行、每個 tool → 一行 `[tool] <name> <input>`；`idle` 列略過；`files`（附件）不算對話。工具 content 的實際形狀以第 1 步的真 fixture 複驗（報告 §G-1）——複驗前先以 fixture 的形狀為準，fake 也用它。
- 既有規則不變：不輸出 reasoning／step／tool 結果；不認得的型別 `[skip <type>]`；`native(turns(raw))` 的 round trip 要成立。
- 這一步修掉「import 成功但 body 空」的靜默失敗（spike §A 階段 2）。

### 5. 互動模式的 DB

- **選表**：先看 `session_v2` 與需要的欄位；沒有才退回 v1 的 `session`；都沒有 → `_warn_schema()` 一次＋空清單（照舊）。兩者都在時以 `session_v2` 為準——v2 是現在的 store，舊表若還在，內容是過去式（或已被遷移），合併只會列出重複的舊資料。
- 清單 SQL 照實測欄位：`select id, directory, title, time_updated from session_v2 order by time_updated desc, id desc`。
- 預覽與搜尋改讀 `session_message`（`data` 是 JSON）：v1 的規則不變（最後 20 則、只算 user／assistant、text 且非 synthetic、截 2000 字、NFKC＋casefold、LIKE 先掃再全掃、程式確認命中），只是表與欄位換掉；實際欄位（`type`、`data` 的形狀、排序用 `time_created` 或 `seq`）以第 3 步的真 DB fixture 定案。
- `_session_directory`：用選中的表；`row` 先設 None，表不存在或查不到就回 None——修掉 spike 量到的 `cannot access local variable 'row'`（`_db()` 吞掉 sqlite3 的錯誤之後，變數從沒被賦值）。

### 6. summarize 與 delete 的 server 上下文

- 同 §2。`_last_reply` 的事件解析不用改（v2 `run --format json` 形狀照舊，實測）。`_model_of` 走 export；v2 的模型讀 assistant 列的 `model.id`（v1 是 `modelID`），形狀以 fixture 複驗。
- 影響的路徑：`summarize`（run 的旗標、`_model_of`、`_drop_summary_session`）、`_sweep_pending`（補刪）、`_import_verified` 的失敗清理（`_discard`）與 merge 的刪除警告。

### 7. 舊 raw（v1）的接續：明確拒絕

- 情況：opencode 2.x 之前匯入的 Session，raw 是 v1；opencode 2.x 的 `session import` 拒收（實測）。
- **決定**：同 agent 接續／fork 在開 agent 前拒絕，訊息指出重新匯入的指令（`agora import session --external-session-id <agent session id> --agent opencode`；來源還在 v2 的 store 裡時會原地更新 raw）。跨 agent 接續不受影響（閱讀→重建，本來就重寫）。
- **不採「自動重新 export 本機那份」**：本機 DB 的那份不一定和 store 裡的 raw 是同一份（跨機器 import 的 raw 在本機可能沒有、或比較舊），靜默換掉內容比拒絕更糟。
- **不採 v1→v2 轉換**（本 change）：工具呼叫在 v2 `content` 的形狀還沒複驗（§G-1），現在寫轉換器等於用猜的。留下來當後續單（Open Questions）。
- 代價：舊 Session 在重新匯入前不能同 agent 接續；這是明確、可回復的限制，不是資料遺失。

### 8. 資料目錄（XDG）：agora 不指定

- 選項：
  1. **現狀**：不指定、環境完全繼承（pass-through）。
  2. **透傳**：新增環境變數讓呼叫方經 agora 注入（例如 `AGORA_OPENCODE_XDG_*`）。
  3. **agora 自己指到預設的隔離目錄**。
  4. **呼叫方負責**：sidecar／supervisor／排程器在啟動 agora 前設好四個 XDG。
- 分析：opencode 只認環境變數（`opencode debug paths` 實測）。agora 的主要用途是讀「使用者的」Session（匯入、TUI），(3) 會直接把它藏起來，等於壞掉。(2) 與 (4) 的責任重疊（worker 上的 sidecar 本來就是 spawner），還多一層介面。(4) 的責任最清楚——測試（conftest）與驗收腳本已經在做這件事。
- **建議：(1)＋(4)**：agora 不指定、env 照傳、不新增變數；「要隔離就在啟動 agora 前設四個 XDG」寫進 `docs/spike/opencode.md` 與驗收步驟；sidecar（MyLinuxPool#114）在自己的 spawner 統一設。附帶：所有自動化呼叫都給明確 server 上下文（§2），這是另一件事（避免 managed 服務的副作用），兩者都要。

### 9. 測試與驗收策略

- 單元測試以 fake 為主（v2 語意＋fixture）：嗅探、指令／旗標、reidentify、native／append、閱讀版、DB、summarize／delete。
- 真 opencode 的整合測試（`tests/integration/test_opencode_real.py`、`test_e2e_opencode.py`）跟著改成 v2 語法與格式；隔離 HOME／XDG；免費模型不可用時 skip（照現有的模型鏈）。這些測試在沒有 opencode 的機器上本來就 skip。
- 驗收（issue #51）：隔離 XDG ＋ 假 Drive（`tests/fakes/fake_rclone.py`）＋自編多輪含工具呼叫的 v2 session：`agora import` → `continue --headless`（三行 stderr、SIGTERM 後寫回）→ `agora show`（閱讀版完整）→ `merge` 兩顆 → 接續 merged Session。

## 報告 §G「未測」項目的補驗落點

| §G 項目 | 落在哪一步 |
|---|---|
| 工具呼叫的 parts（v2 `content` 的形狀；閱讀版的一行） | 第 1 步產生含工具呼叫的真 fixture；第 2 步的閱讀版與 native／append 用它複驗；第 5 步整條流程再驗一次 |
| 跨訊息 reference（reidentify 要改哪些結構位置） | 第 1 步的 fixture 要有多輪＋工具的參照；reidentify 只改實測存在的欄位；第 5 步用 import→export round trip 確認 |
| import 必填欄位全集 | 第 2 步：模板＋逐欄 `SchemaError` 補齊；結果記進 `docs/spike/opencode.md`；fake 依同一份拒絕缺欄位的 payload |
| 互動模式 SQL 未實作、未實測（§G-3） | 第 3 步：真 DB fixture＋列表／預覽／搜尋的測試 |
| merge 之後的 sidecar HTTP 鏈（§G-4） | 不在本單（MyLinuxPool#114）；本單只驗「merge 建立＋接續 merged」 |
| managed 服務的生產行為（§G-6） | Decisions 8 的 server 上下文＋第 5 步驗收（確認沒有服務被叫到／共用服務不受影響） |
| 模型是 fake、真實對話格式細節 | 第 1 步的真 fixture（自建 fake provider 或免費模型）＋第 5 步的整條流程 |

## 本人未指示、作者提議

以下不是本人的指示，是設計時作者提的（要讓本人知道）：

- 舊 raw 用同一個 agent 接續時**明確拒絕**（Decisions 7），而不是自動重新 export 或 v1→v2 轉換。
- server 上下文一律 `--standalone`，`--server` 先不用（Decisions 2；語法未實測）。
- v1 的**讀取**支援保留（嗅探），寫入只有 v2（Decisions 1）。
- XDG 不新增介面、agora 不指定；由呼叫方負責（Decisions 8）。
- 互動模式 DB 以 v2 為準、v1 後備（Decisions 5）。
- fixture 進 repo（去識別化）＋DB 用文字 dump 當 fixture（第 1、3 步）。
- fake 保持新舊語法都收，讓 v1 的既有測試留著（第 1 步）。
- 第 1 步先做 fixture 產生，因為後面每一步都靠它。
- `_delete` 的 server 上下文放第 4 步（照 issue 的建議切法）；第 1 步的測試用 fake，不依賴真實 v2 的刪除。

## Risks / Trade-offs

- [v2 的欄位全集／工具形狀未知] → fixture 先行＋逐欄探測；fake 以 fixture 驗證；不確定就少改；失敗是明確的 `SchemaError` 而不是靜默。
- [舊 Session 在重新匯入前不能同 agent 接續] → 明確訊息＋原地更新路徑（重新匯入）；轉換器列後續單。
- [managed 服務被叫到] → 所有呼叫經 `_server_args()`；單元測試斷言 argv；驗收確認隔離外沒被碰。
- [headless 的定期寫回期間 serve 還活著（未實測）] → 第 5 步驗收用 `--sync-period` 真的做一次寫回；若 `--standalone` 與 live serve 相衝，再評估 `--server`（Open Questions）。
- [v2 還會改版] → 嗅探＋fixture 釘住；認不得就明確報錯，不猜。
- [DB schema 認不得] → 警告一次＋空清單（照舊），不讓 TUI 出錯。
- [import 非交易式] → 回讀驗證（訊息數＋part 數）照舊；失敗的殘骸用 `session delete` 清。
- [fixture 帶到私人資訊] → 只放自編內容、一般化路徑；產生步驟在隔離 XDG、不讀憑證（Decisions 3）。

## Migration Plan

- **沒有 schema 變更、沒有資料遷移。** 部署就是換程式。
- 使用者路徑：下次 `agora import` 同一個來源時，舊 Session 的 raw 會原地更新成 v2；要接續舊 Session 就先重新匯入（Decisions 7 的訊息會說）。
- 回退：純程式碼變更，回退 commit 即回到 v1 寫入路徑（但對 v2 安裝就是現在的壞法）。無狀態要清理。
- 開工順序：tasks 1→5，每步一個 PR、可單獨合併、可單獨測。

## Open Questions

1. **`--server <url>` 的語法與必要性**：長命 serve（headless 的定期寫回）要不要改走 `--server`？本 change 不用；第 5 步驗收若發現 `--standalone` 與 live serve 相衝再處理。
2. **v1→v2 轉換器**：要讓舊 Session 在**離線**（本機沒有那個 agent session）也能接續就得做；前提是 §G-1 的工具形狀已複驗。做不做、什麼時候做，本人決定。
3. **v2 升級有沒有把舊 session 遷進 `session_v2`**：影響「重新匯入」對舊 Session 是否總是可行。開工時在隔離環境量一次即可，不影響設計。
4. **舊表（`session` 等）在 v2 是否還留著**：選表規則（v2 優先、v1 後備）兩種情況都涵蓋，不影響設計。
