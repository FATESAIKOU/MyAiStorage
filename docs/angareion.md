# Angareion (A2A 通訊 v0)

Angareion 是 AI 之間只追加（append-only）、可審計的異步通訊通道。收件人綁定為「角色」（`repo × role`）而非特定 session，讓訊息在 session 結束、離線或環境重建後依然保留在信道上。

v0 的後端介面以 GitHub private repository 作為信道，使用 issue 代表話題（channel）、issue comments 代表訊息與確認（ack）。

---

## 1. 訊息格式與 YAML Front Matter

Angareion 的每則訊息包含六個核心欄位：
1. `from`：寄件者身分。
2. `to`：收件者身分或群組。
3. `channel`：訊息所屬的話題名稱（只使用 `channel`，不使用 `subject`）。
4. `urgency`：緊急程度，整數 0–9（越大越急，預設值為 5）。約定：
   - `8–9`：立刻處理。
   - `4–7`：儘快處理。
   - `0–3`：有空再看。
5. `attachments`：附件清單（複數形，陣列）。每項為 mapping，包含 `name`，以及 `ref` 或 `inline` 恰好其中一個。
6. `content`：訊息內文，為 YAML front matter 之後的整段 Markdown 內文（不包在 YAML 內）。

### 訊息形狀範例

在 GitHub issue 留言中，訊息以開頭的 YAML front matter（以 `---` 起訖）加上後續 Markdown 內文組成：

```yaml
---
a2a: 1
id: 01JABCDE0123456789ABCDEF00
at: 2026-10-10T09:30:00+09:00
from: {repo: owner-org/repo-name, role: PM, name: PMO}
to: {repo: owner-org/worker-team, role: team-pm, name: worker-1}
channel: phase-1-task-42
urgency: 7
attachments:
  - {name: "工單", ref: "https://github.com/owner-org/repo-name/issues/42"}
  - {name: "設定片段", inline: "key=value\nfoo=bar"}
---
這裡是內文，使用 Markdown 格式。

如果內文中有 `---` 開頭的行，解析器僅以 front matter 的第一個結束分隔線 `---` 作為邊界，確保內文完整。
```

### 欄位與結構規則
- `a2a`：格式版本號，v0 固定為 `1`。
- `id`：寄件端生成的 ULID。
- `at`：ISO 8601 時間字串（含時區）。
- `from` 與 `to`：
  - 角色位址格式為 `{repo, role, name}`。其中 `repo` 與 `role` 必填，`name` 為顯示用途之選填欄位。
  - 群組位址格式為 `{group: "<group-name>"}`。
- `attachments`：
  - `ref`：不透明 locator 字串，常見約定如 `https://...`、`repo:<owner>/<repo>@<ref>:<path>`、`agora:<id>`。
  - `inline`：字串，直接將內嵌文字放於 front matter。
- 大小限制：整則留言（front matter + `content`）不得超過後端上限（GitHub 為 65,536 字元）。超過時 `a2a send` 直接拒絕（exit 1），不截斷。

---

## 2. Channel 與 Issue 的對應

- **一個 channel 對應一個 issue**：Issue 標題即為 channel 名稱；每則訊息與 ack 都是該 issue 底下的一則 comment。
- **建立話題需使用 `--new`**：
  - 僅當使用 `a2a send --channel <name> --new ...` 時才會建立新 issue。
  - 若 channel 不存在且未帶 `--new`，`a2a send` 拒絕送出（exit 1）並列出已知 channels，避免因拼字錯誤建立多餘 issue。
- **正本與快取機制**：
  - channel 名稱為正本，本機維護 `channel -> issue number` 的映射快取。
  - 若快取丟失，可從 issue comments 列表（包含 `issue_url` 與 front matter 內的 `channel`）完整重建映射。
  - 不建議人工修改 issue 標題；若被修改，重建時仍以留言內的 `channel` 欄位為準。
  - 若因競爭條件同時 `--new` 建立了同名 issue，v0 會在 `a2a inbox` 輸出警告。

---

## 3. 身分與群組位址在 v0 的行為

- **以角色為收件人**：
  - 投遞匹配只比對 `repo` 與 `role`，`name` 僅供人類或日誌識別。
  - 同一個角色可由不同 session 執行；未 ack 的訊息由角色共享信箱，任何該角色的 session 執行 `a2a inbox` 均能讀取。
- **群組位址在 v0 的行為**：
  - 格式層保留 `{group: <name>}` 支援，解析器可正常讀取，不拋出格式錯誤。
  - 但 v0 不支援群組定址與成員展開語意：
    - `a2a send` 若指定群組位址為 `to`，會拒絕送出（exit 1），並說明群組定址不在 v0。
    - `a2a inbox` 讀到群組訊息時，不會將其排入任何角色的收件匣，亦不造成錯誤。

---

## 4. 狀態檔與快取

- **設定檔位置**：
  - 預設路徑：`~/.config/angareion/config.json`。
  - 可透過環境變數 `A2A_CONFIG` 覆寫。
- **狀態檔目錄**：
  - 預設路徑：`~/.local/state/angareion/<identity>.json`。
  - 可透過環境變數 `A2A_STATE_DIR` 覆寫。
- **狀態內容與純快取原則**：
  - 狀態檔儲存 `since`（最後同步時間）、`etags`、`acked`（加速查詢之已確認訊息 ID）與 `channels` 映射。
  - **狀態檔純屬本機快取**：真值永遠存在信道 repo 上。刪除本機狀態檔後，下次執行指令只會退回完整掃描重建，絕不丟失任何訊息或 ack 記錄。

---

## 5. Token 建立、安全邊界與輪替步驟

### 安全邊界與權限
- Angareion 是 public repo，**程式碼、測試與日誌 MUST NOT 出現真實信道 repo 名稱、token 或個人隱私資訊**。
- 開發與單元測試一律使用 mock / fake transport，不對外連線，隊員不接觸真實 token。
- 憑證使用 GitHub 細粒度 Personal Access Token（Fine-grained PAT）：
  - **Repository access**：Only select repositories → 指定 private 信道 repo。
  - **Permissions**：
    - `Repository permissions` -> `Issues`：勾選 **Read and write**（GitHub 會自動附加必備的 `Metadata: Read`）。
    - 嚴禁勾選 Contents、Actions、Pull requests、Administration 或其他權限。

### Token 本機存放
- 預設存放於 `~/.config/angareion/token`，檔案權限必須為 `0600`。
- 可透過環境變數 `A2A_TOKEN_FILE` 指定其他路徑。
- 程式讀取時若檔案不存在或權限錯誤，回報清楚錯誤並 exit 2；輸出與例外中絕不印出 token 內容。

### Token 輪替步驟
1. 登入 GitHub，至 **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens**。
2. 點選 **Generate new token**，設定到期日，權限同樣僅選信道 repo 的 `Issues: Read and write`。
3. 產生後，將新 token 寫入本機 token 檔：
   ```bash
   echo -n "github_pat_NEW_TOKEN_VALUE" > ~/.config/angareion/token
   chmod 0600 ~/.config/angareion/token
   ```
4. 執行 `a2a inbox` 確認新 token 驗證通過。
5. 回到 GitHub 頁面將舊的 Fine-grained PAT 撤銷（Revoke）。

---

## 6. 速率限制與輪詢

- **發文速率節流**：
  - 遵守 GitHub 內容產生類次級速率限制（Secondary Rate Limits）：**80 次／分鐘、500 次／小時**。
  - 本機客戶端維護滑動發文時間窗。接近門檻時自動等待（等待時間 ≤ 10 秒）；若需等待過久則拒絕送出（exit 2），保護帳號不被 GitHub 封鎖。
- **403 Retry-After 處理**：
  - 後端回傳 HTTP 403 且帶有 `Retry-After` 標頭時，等待指定秒數後重試一次；重試仍失敗則 exit 2。
- **條件式輪詢（ETag）**：
  - 讀取時使用 issue comments 端點（`GET /repos/{owner}/{repo}/issues/comments?since=...&per_page=100`），並帶入 `If-None-Match`。
  - 當收到 HTTP 304 Not Modified 時，表示無新訊息，不耗損 API 額度且立即返回 exit 0。
- **重試防重複貼機制**：
  - 送出失敗或逾時重試時，呼叫端可沿用相同 `--id <ULID>`。客戶端會先檢查 channel 最新留言是否已存在該 `id`，若已存在則直接視為成功，避免重複留言。

---

## 7. CLI 指令用法

所有子指令皆支援全域身分設定或由 `--identity` / `--from` 覆寫。

### `a2a send`
送出一則訊息至指定 channel。

```bash
a2a send \
  --to "repo=owner-org/worker-team,role=team-pm" \
  --channel phase-1-task-42 \
  --urgency 7 \
  --body "請開始執行任務 42"
```

- 建立新 channel（話題）需附加 `--new`：
  ```bash
  a2a send --to "repo=owner-org/worker-team,role=team-pm" --channel new-topic --new --body "開啟新話題"
  ```
- 附件用法：
  - `--attach <name>=<path>`：讀取本機文字檔案並展開為 `inline` 附件。
  - `--ref <name>=<locator>`：指定外部引用識別碼（如 URL、repo 位置等）。
- 重試時指定既有 ID：
  - `--id <ULID>`：保留原訊息 ID 進行重試，防止重複建立。
- 輸出規範：
  - 成功時 stdout **只輸出一行**：`<id> <channel>`。
  - 進度與日誌輸出至 stderr。
  - Exit code：`0` 成功；`1` 參數或驗證錯誤（如 channel 不存在且未帶 `--new`、訊息超長）；`2` 網路或後端錯誤。

### `a2a inbox`
列出發給自己的未 ack 訊息。

```bash
a2a inbox
```

- 參數選項：
  - `--json`：以 JSON 陣列格式輸出至 stdout，每項包含 `{id, at, from, to, channel, urgency, content, attachments, acked, comment_url}`。
  - `--all`：列出所有發給自己的訊息（含已 ack 者，供審計使用）。
  - `--identity <repo×role>`：覆寫預設接收者身分。
- 行為：
  - 依訊息時間 `at`（同時間則以 `id`）由舊到新排序。
  - 若無未處理訊息，stdout 為空並回傳 exit 0。

### `a2a ack`
確認已接收並處理訊息。

```bash
a2a ack 01JABCDE0123456789ABCDEF00 [--note "已完成處理"]
```

- 支援一次傳入多個 ID：
  ```bash
  a2a ack 01JABCDE... 01JFGHIJ...
  ```
- 行為：
  - 在對應的 channel 發送一則包含 `ack: <id>` front matter 的新留言。
  - 若該訊息已 ack 過，不會重複發送 ack 留言，回傳 exit 0。
  - 若找不到指定 ID 的訊息，輸出錯誤並 exit 1（其餘合法 ID 仍會完成 ack）。

---

## 8. 本人專用 Smoke Test 清單

> [!CAUTION]
> **以下測試涉及真實 GitHub repo 與真實 Token，僅供本人（專案擁有者）親自驗收執行。隊員（AI）嚴禁執行、嚴禁接觸真實憑證。**

### 準備工作
1. **建立 Private 信道 Repo**：在 GitHub 上建立一個新的 private repository（例如 `owner-org/a2a-channel-smoke`）。
2. **生成 Fine-grained PAT**：
   - 僅授權該 private repo。
   - 僅開啟 `Issues: Read and write` 權限。
3. **準備兩台測試環境（或兩個隔離工作目錄 A 與 B）**：
   - 環境 A 設定：
     - 身分：`{repo: "owner-org/team-a", role: "worker", name: "node-a"}`
     - 建立 `~/.config/angareion/config.json` 與 `token`。
   - 環境 B 設定：
     - 身分：`{repo: "owner-org/team-b", role: "worker", name: "node-b"}`
     - 建立對應之 `config.json` 與 `token`。

### 驗收步驟
1. **[環境 A] 建立新話題並發信**：
   ```bash
   a2a send \
     --to "repo=owner-org/team-b,role=worker" \
     --channel smoke-test-channel \
     --new \
     --body "Smoke test ping from node A"
   ```
   - 驗證：stdout 印出 `<id> smoke-test-channel`，Exit code 0；GitHub 信道 repo 出現新 Issue「smoke-test-channel」與一則含 front matter 的留言。

2. **[環境 B] 讀取信箱**：
   ```bash
   a2a inbox
   ```
   - 驗證：成功列出環境 A 發送的訊息，顯示對應的 `id`、`from`、內文。

3. **[環境 B] 確認回執 (Ack)**：
   ```bash
   a2a ack <剛才收到的 id> --note "Node B received successfully"
   ```
   - 驗證：Exit code 0；GitHub 上該 Issue 出現一則含 `ack: <id>` 的確認留言。

4. **[環境 B] 驗證收件匣已清空**：
   ```bash
   a2a inbox
   ```
   - 驗證：無新訊息，Exit code 0。

5. **[環境 B] 反向發送訊息至環境 A**：
   ```bash
   a2a send \
     --to "repo=owner-org/team-a,role=worker" \
     --channel smoke-test-channel \
     --body "Smoke test reply from node B"
   ```
   - 驗證：Exit code 0；留言追加在同一個 Issue 底下。

6. **[環境 A] 讀取並 Ack 反向訊息**：
   ```bash
   a2a inbox
   a2a ack <環境 B 的訊息 id>
   ```
   - 驗證：環境 A 讀取成功並完成 ack，雙向通訊驗證完畢。
