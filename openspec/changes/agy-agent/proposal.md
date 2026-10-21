## Why

agy（Antigravity CLI）的轉接器從 #26 的 PR1／PR2b 起就在 `src/agora/agents/agy*.py` 裡，但 `openspec/specs/` 一直沒有它的 spec：`session-continue` 只寫 opencode／claude 的接續，import／列表／搜尋與 agy 的邊界拒絕沒有文字可對。行為已經實作、測試也齊了，缺的是把**現有行為**寫成一份 spec，之後改動才有東西可以對照。

## What Changes

- 新增 capability `agy-agent`，把 agy 轉接器的最終行為寫一次：
  - **匯入**：唯讀、經快照（沒有 `-wal` 用 `immutable=1`；有 `-wal` 複製 db＋wal 後重放，**不複製 `-shm`**），不叫 agy 的 CLI，不讀憑證檔。
  - **列表與搜尋**：清單來自 `conversation_summaries.db`；搜尋只看標題與 preview（不搜內文）。
  - **raw 信封**：`agy-conversation/1`，整包 db base64；同一份未變的 db 匯兩次 md5 相等。
  - **接續**：`continue --agent agy` 只支援同 agent；還原對話檔＋補 summaries 列，失敗時收回；內容裁決用步數與 summaries 的時刻，活體較新就直接開活體。
  - **邊界拒絕**：merge（以 agy 寫要約）、跨 agent 的 continue、`--fork`／`--headless` 在讀取或啟動之前拒絕並說明。
  - **憑證檔不讀**。
- **不是新行為**：程式沿用 #26 的實作。#141 只拿掉接續的預設閘門（2026-10-21 真機驗收通過後），文件與這份 spec 跟著改成現況。

## Capabilities

### New Capabilities

- `agy-agent`：agy 轉接器的匯入、列表／搜尋、raw 信封、同 agent 接續與邊界拒絕。

### Modified Capabilities

（無。）

## Impact

- 程式：無變更（本 change 記錄的是已實作的行為）；接續的預設閘門在 #141 移除。
- 文件：README、`docs/design.md`、`docs/acceptance.md` 第 18 節、`docs/agy-storage.md` §12／§13 一起改成現況。
- 測試：`tests/unit/test_agent_agy.py`、`tests/unit/test_agy_continue.py`、`tests/unit/test_agy_fixture.py` 已經是這份 spec 的實作對照。
