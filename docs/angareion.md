# Angareion

Angareion 是讓 agent、程式或人「留言、讀留言」的獨立工具。對外只有四個介面：建 channel、送訊息、確認有沒有新訊息（`wait`）、讀訊息。現在的後端實作是 GitHub API＋Issues（channel＝issue、訊息＝留言）；換後端時指令與訊息格式不變。

指令叫 `angareion`；沒有 ack、已讀回條，也沒有內建限流。

## 四個指令

| 指令 | 做什麼 |
|---|---|
| `angareion channel create <名稱> [--member <位址>]…` | 建 channel（已存在就沿用）；stdout 印 `<名稱> #<編號>` |
| `angareion send --channel <ch> --to <位址> [--urgency N] [--attach NAME=PATH]… [--ref NAME=LOCATOR]… (--body <文字> \| --body -)` | 送一則訊息；stdout 只印訊息 id |
| `angareion wait [--channel <ch>] [--timeout <秒>]` | 確認有沒有新訊息；有新訊息 exit 0，逾時沒有 exit 3；stdout 不印東西 |
| `angareion read [--channel <ch>] [--since <時刻>] [--json]` | 讀新訊息（由舊到新），並把本機游標往前推 |

- **目標**：給 `--channel <ch>` 就是那個 channel 的全部訊息；不給就是「寄給我的」（所有 channel 裡 `to` 是自己身分的訊息）。
- **`send` 的內文**：一定要給 `--body <文字>`；`--body -` 讀 stdin（多行內文用這個）。沒給 `--body` 就 exit 1——不會自動讀管線，免得卡在沒資料的 stdin 上。
- **`wait` 的等法**：`--timeout 0` 查一次就回（不阻塞）；`--timeout N` 最多等 N 秒，每 15 秒查一次；不給就一直等。等待期間不動游標——`wait` exit 0 之後，下一次 `read` 還是讀得到那則訊息。
- **`read --since`**：照給的時刻讀，不動本機游標（給程式自己管）；不給 `--since` 時從本機游標之後讀起，讀到的最後一則成為新游標。游標檔刪掉就從頭讀起。
- **`read` 的輸出**：預設是給人看的格式（`[urgency] id at <寄件者> channel: 名稱` 加縮排的內文與附件清單）；`--json` 印一個 JSON 陣列，欄位就是下面的訊息格式。
- **位址寫法**：`repo=owner/team,role=pm,name=小明`、`owner/team×pm`、`owner/team:pm`，或 JSON `{"repo": "owner/team", "role": "pm"}`。`name` 只是顯示用，不比對。
- **群組位址**（`group=…`）在 v0 會被拒絕（exit 1），身分也不可以是群組。

結束碼：

| 碼 | 意思 |
|---|---|
| 0 | 成功（`wait` 是「有新訊息」） |
| 1 | 用法或輸入錯誤（參數錯、位址錯、urgency 超出 0～9、channel 不存在） |
| 2 | 後端、網路或設定錯誤（設定缺、token 有問題、GitHub 回錯誤；不自動重試） |
| 3 | `wait` 逾時沒有新訊息 |

stdout 只放結果（`channel create` 的名稱與編號、`send` 的訊息 id、`read` 的訊息）；說明與錯誤一律放 stderr。

## 訊息格式

一則訊息是六個欄位加 `id`、`at`，以 YAML front matter 存在留言裡；front matter 之後的本文就是 `content`：

```markdown
---
angareion: 1
id: 01JX2M3N4P5Q6R7S8T9V0W1XYZ
at: '2026-10-12T03:00:00.123456+00:00'
from:
  repo: owner/channel-repo
  role: PM
  name: PMO        # 選填，顯示用
to:
  repo: owner/worker
  role: worker
channel: general
urgency: 5
attachments: []
---
這裡是內文，可以是多行 Markdown。
```

- `angareion: 1` 是格式版本，解析器不認得的版本會忽略（當成不是訊息）。
- `id` 是寄件者產生的 ULID；`at` 是含時區的 ISO 8601。
- `urgency` 是 0～9 的整數，預設 5。
- `attachments` 每項是 `{"name": …, "ref": …}`（連結）或 `{"name": …, "inline": …}`（小檔的內容），兩者恰有一個。指令上的 `--ref NAME=LOCATOR` 產生 `ref`、`--attach NAME=PATH` 把檔案內容讀進來產生 `inline`。
- 一則訊息（含 front matter）上限 65536 字元。

### 游標（讀到哪）

「讀到哪」是讀的人自己的本機狀態，不在信道上留任何記號：

- 檔案：`~/.local/state/angareion/cursor-<身分>.json`（`ANGAREION_STATE_DIR` 可覆寫）。`<身分>` 是 `repo` 與 `role` 把 `/`、`:` 換成 `_` 再接起來。
- 內容是每個目標一筆，值是「最後讀到的那一則」的 `at` 與 `id`：

  ```json
  {
    "channel:general": {"at": "2026-10-12T03:00:00.123456+00:00", "id": "01JX2M3N…"},
    "to:owner_worker__worker": {"at": "2026-10-12T03:05:00+00:00", "id": "01JX2M3N…"}
  }
  ```

- 新訊息的判斷是嚴格晚於 `(at, id)`：同一秒的兩則靠 ULID 分先後，兩則都讀得到；已讀過的留言就算在 GitHub 上被編輯（`updated_at` 變新）也不會被當成新訊息再回傳。
- GitHub 的 ETag 只存在單次 `wait` 行程的記憶體裡（用條件式請求問「有沒有變」），**不寫進游標檔**：`wait` 不會動到游標，之後的 `read` 也不會被 304 跳過。
- 刪掉游標檔就等於從頭讀；沒有其他本機狀態。

## 設定與 token

設定檔：`~/.config/angareion/config.json`（`ANGAREION_CONFIG` 可覆寫）：

```json
{
  "channel_repo": "owner/channel-repo",
  "identity": {"repo": "owner/worker", "role": "worker", "name": "worker-1"},
  "token_file": "/path/to/token"
}
```

- `channel_repo`：信道 repo（`owner/repo`），必須存在且是 private。
- `identity`：自己的位址；`send` 的 `from` 與不給 `--channel` 時的「寄給我的」都用它。
- `token_file`：選填；不寫就用預設路徑。

token 檔的位置依序是：`ANGAREION_TOKEN_FILE` → 設定的 `token_file` → `~/.config/angareion/token`。檔案權限**必須只有擁有者**（`0600` 或 `0400`——worker 上的唯讀掛載是 0400），否則拒絕執行（exit 2）。每次執行都重新讀檔、不快取；換 token 只要換檔案內容（或換 `ANGAREION_TOKEN_FILE` 指到的檔案），不用重啟什麼。

token 不會出現在任何輸出、log 或錯誤訊息裡（有出現會被遮成 `***`）。它的範圍只有信道 repo 的 Issues；讀得到它的 agent 也只拿得到這點權限。

## 本人的 smoke test

1. 開一個 private repo 當信道（例如 `fatesaikou/angareion-channel`），確認 Issues 功能開著。
2. 在 GitHub 建一個 fine-grained PAT：只給這個 repo、權限選 **Issues: Read and write**。存到 `~/.config/angareion/token`，`chmod 600 ~/.config/angareion/token`。
3. 寫 `~/.config/angareion/config.json`：`channel_repo` 指到那個 repo，`identity` 是自己的位址。
4. 裝指令：在 repo 檢出裡 `uv tool install --editable .`（或 `uv run angareion …`）。
5. `angareion channel create general --member 'repo=owner/channel-repo,role=PM'` → 應該印 `general #1`。
6. 用另一個身分（另一台機器或另一個設定目錄）`angareion send --channel general --to 'repo=owner/worker,role=worker' --body 'hello'` → 印訊息 id。
7. `angareion wait --channel general --timeout 0` → 有新訊息 exit 0；`angareion read` → 看到那則訊息；再 `angareion read` 一次 → 什麼都沒有。游標檔刪掉再 `read` → 從頭看到所有訊息。

`wait` 是每 15 秒用條件式請求問一次；GitHub 沒有推播，304 不耗額度。被 GitHub 次級限制打到時就照 HTTP 回應失敗（exit 2），不自動重試。

## TODO（給 PM）

- opencode 讀說明檔的位置還沒確認（Claude Code 是 `~/.claude/skills/angareion/SKILL.md`）。確認後補進下面「給 MyLinuxPool 的需求」那一節。

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
