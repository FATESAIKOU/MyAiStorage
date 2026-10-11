## Why

AiCompany 期 1 需要一個讓 agent、程式或人「上 Slack 留言、讀留言」的工具（issue #30）。本人 2026-10-11 看過兩版設計後定案（設計提案 v4，見 `docs/proposals/aicompany-design-v4.md`，在 #31 的 branch）：

- 指令叫 `angareion`；
- 只有四個介面：建 channel、送訊息、確認有沒有新訊息（可以阻塞地等，也可以不阻塞直接回）、讀訊息；
- 把後端抽象起來，這次用 GitHub API＋Issues 實作；
- 現在不做限流；
- Angareion 是獨立的工具，不依賴其他元件；
- agent 用的 Angareion 能力（指令＋說明）與信道 token 由 MyLinuxPool 的「Angareion 能力」分發，profile 指定要哪些能力；Angareion 只定 token 檔的位置與讀法。

這個 change 取代 v1 的 `angareion-a2a-v0`（只在 branch `aicompany-p1`，沒合進 main）。v1 寫好的程式可以沿用的就移植（訊息模型、GitHub 後端、token 讀取）。

## What Changes

- 新增 `angareion` 指令與函式庫（`src/angareion/`）：`channel create`、`send`、`wait`、`read` 四個子命令，對應四個介面。
- 後端介面 `ChannelBackend`（同樣四個動作）與實作 `GitHubIssuesBackend`。
- 訊息格式：六欄位（`from`／`to`／`channel`／`urgency`／`content`／`attachments`）加 `id`、`at`，以 front matter 存在 GitHub 留言裡。
- 「讀到哪」是讀的人自己的游標（本機），不在信道上留 ack。
- token：`ANGAREION_TOKEN_FILE` 指定的檔案，檔案只能擁有者有權限（0400 或 0600）。
- agent 用的說明檔（skill）：`skills/angareion/SKILL.md`，由 MyLinuxPool 的 Angareion 能力放到各 agent 讀得到的位置。

## Capabilities

### New Capabilities

- `angareion`：留言、讀留言的四個介面、訊息格式、後端可替換、token 的位置與讀法。

### Modified Capabilities

（無）

## Impact

- 程式：`src/angareion/`、`pyproject.toml`（console script `angareion`、套件 `src/angareion`）。
- 測試：`tests/unit/test_angareion_*.py`；`tests/conftest.py` 把 `ANGAREION_*` 指到暫存目錄；不連網。
- 文件：`docs/angareion.md`（格式、四個指令、token 的放置與輪替、本人的 smoke test）、`skills/angareion/SKILL.md`。
- 其他專案：MyLinuxPool 的 Angareion 能力（以 env 設 `ANGAREION_TOKEN_FILE` 指到池的 secret 檔；建映像時用 tool-pins 的同一個 sha 取 `skills/angareion/SKILL.md`，放到各 agent 讀得到的位置）。
