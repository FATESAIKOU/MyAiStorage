## Context

agy（Antigravity CLI）把每個對話放在一個 SQLite 檔：`$AGORA_AGY_HOME/conversations/<conversation-uuid>.db`（WAL 模式），清單、標題與 preview 在旁邊的 `conversation_summaries.db`。`AGORA_AGY_HOME` 預設 `~/.gemini/antigravity-cli`，所有碰到 agy 檔案的地方都經過它，測試一律指到暫存目錄。

轉接器從 #26 的 PR1（import／讀取）與 PR2b（同 agent 互動接續）就存在，實作在 `src/agora/agents/agy.py`、`agy_store.py`、`agy_summaries.py`、`agy_restore.py`、`agy_wire.py`；命令邊界的拒絕在 `src/agora/cli.py`。這份 change 不改程式，只把**最終行為**寫進 spec（#141）。原本的接續預設閘門在 2026-10-21 真機驗收通過後移除，因此 spec、README 與文件都寫無閘門的現況。

## Goals / Non-Goals

**Goals**

- 一份可對照的 `agy-agent` spec：匯入（唯讀、經快照）、列表／搜尋範圍、raw 信封、同 agent 接續、邊界拒絕、憑證檔不讀。
- 每個 Requirement 都有可觀察的 Scenario，對得到現有的單元測試。

**Non-Goals**

- 改程式或改測試（本 change 只記錄已實作的行為）。
- fork／merge／headless／跨 agent 接續的實作（#26 的後續）。
- TUI 的畫面細節、Drive 的同步規則（`interactive-mode`／`session-sync` 各自涵蓋）。
- agy 版本差異的相容政策（目前只對 1.4.4 驗過）。

## Decisions

| # | 決策 | 理由 |
|---|---|---|
| D1 | spec 描述**現有行為**，不先寫未來功能 | 沒有 spec 的現況讓改動無從對照；寫未實作的東西會變成假的規格 |
| D2 | 匯入一律經快照，MUST NOT 直接開原檔 | 原檔可能在 agy 寫入中；`immutable=1` 只在沒有 `-wal` 時安全，有 `-wal` 要複製 db＋wal 重放（不複製 `-shm`，SQLite 會重建），再用 `VACUUM INTO` 收成單檔 |
| D3 | 快照要重試（`(size, mtime_ns)` 前後比對，最多 3 次） | checkpoint 可能把檔案在複製途中換掉；重試比鎖 agy 的檔案簡單且跨平台 |
| D4 | raw 是 `agy-conversation/1` 信封＋整包 db 的 base64 | 讓 `store.copy_atomic(utf8=True)` 能吃；欄位順序固定，同一份未變的快照兩次匯出 md5 相同 |
| D5 | 閱讀版與 `scan_text` 分開 | 閱讀版不含工具結果（design 4.4）；`scan_text` 是給憑證掃描的明文面，工具輸出在裡面是完整的 |
| D6 | 接續的內容裁決用 `(count(*), max(idx))` 與 summaries 的時刻，**不用 mtime** | mtime 會被複製／同步改掉；步數與 summaries 是內容本身。活體較新或相同時直接開活體，不覆蓋 |
| D7 | 覆蓋活體前先備份到 agora 自己的 state；補列失敗時收回剛還原的檔 | agy 的目錄只放 agy 會讀的東西；「有對話檔、沒有 summaries 列」比不接續更糟（#136） |
| D8 | `continue` 只支援同 agent，其餘在命令邊界就拒絕 | 跨 agent 的內容轉換還沒做；在同步／讀取之前拒絕，才不會被誤認為模型回得不好而重試 |
| D9 | 2026-10-21 驗過載入、sidecar 與列的改寫，所以沒有預設閘門 | 真 agy 載入還原的對話、正常退出後只剩 `<id>.db`、agy 接受 agora 補的列並改寫成自己的格式（`docs/agy-storage.md` §12） |

## Risks / Trade-offs

- **快照與 agy 寫入競態**：複製前後比對只能縮小窗口；檔案在複製途中消失時重試，三次都失敗就報錯，不吐半份快照。
- **`-wal`／`-shm` 的存在無法區分「活的」與「閒置留下的」**：接續寧可拒絕；訊息依 `-wal` 大小說可不可以手動刪。驗收顯示正常退出後 agy 不留 sidecar，實務上不常撞到。
- **summaries 列的形狀是抄來的**：agy 接受後會自己改寫（status、raw_summary、時間欄）；空表的退路仍是猜的（ISO `Z`、裸 `file://`），列在 open questions。
- **TOCTOU 未解**：檢查 sidecar 與 `os.replace` 之間 agy 若剛好啟動，仍可能撞上；`docs/agy-storage.md` 明說只縮小、未消除。

## Migration Plan

無。這份 change 只補文件與 spec；#141 移除的接續開關沒有相容別名（環境變數整個拿掉）。

## Open Questions

- agy 接續後會不會改名或換對話 id（review #26 finding 7）：`collect` 遇到 db 不見會明說「可能換了 id」，不猜。
- `jetbox_summaries.pb`／`agyhub_summaries.pb` 的內容；2026-10-21 的驗收沒有檢查它們有沒有該對話的條目。
- 全新的 summaries 表（沒有任何列可以抄）時，agy 怎麼解析 datetime 與 `workspace_uris` 的退路。
