# Angareion

> 這個檔目前只有「給 MyLinuxPool 的需求」一節；格式、四個指令、身分、token 的放置與輪替、本人的 smoke test 由實作者依 `openspec/changes/angareion-v0/tasks.md` 3.1 補上。

## 給 MyLinuxPool 的需求

這一節是交接資料，不是 Angareion 的設計決定：Angareion 對外只有四個介面，token 與說明檔怎麼放到 worker 上是 MyLinuxPool 的「Angareion 能力」的事。

### token

- Angareion 只讀 `ANGAREION_TOKEN_FILE` 指到的檔案（沒設時才找設定的 `token_file` 與 `~/.config/angareion/token`）。
- worker 上：由 Angareion 能力以 env 設 `ANGAREION_TOKEN_FILE`，指到池掛進來的 secret 檔（`/run/mlp-secrets/<GitHub secret 名>`；檔名照池的規則，由本人命名）。
- 檔案 MUST 只有擁有者有權限（0400 或 0600），否則 Angareion 拒絕（exit 2）。0400 的唯讀掛載可以。
- token 的範圍只有信道 repo 的 Issues；agent 讀得到它（本人已接受，Q6）。
- 輪替：換 secret 後重新分發；Angareion 每次執行都重新讀檔，不快取。

### 說明檔（skill）

- 來源：本 repo 的 `skills/angareion/SKILL.md`。
- 建映像時用 tool-pins 裡裝 `angareion` 的同一個 sha 取這個檔（版本和指令一致），放到各 agent 讀得到的位置：
  - Claude Code：`~/.claude/skills/angareion/SKILL.md`；
  - opencode：實作時確認後在這裡補上。
- profile 指定要哪些能力；沒有選 Angareion 能力的 profile 不會有 token 與說明檔。
