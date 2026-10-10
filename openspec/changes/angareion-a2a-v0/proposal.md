## Why

AI 之間（PMO、各 repo 的 PM、worker 上的 AI team）要有一條可審計的通訊管道。現在靠跨 session 訊息或 herdr 送字，有四個缺口：收件人綁 session 而不是角色；session 結束信就沒了；沒有佇列；收件的 AI 停著時沒人推它。期 1 的流程（PMO → worker team → PR → 回報）需要它當底層（MyAiStorage#30，本人 2026-10-04 提出、2026-10-10 改成期 1 的 A2A 通訊並命名 Angareion）。

## What Changes

- 新增 **Angareion v0**：一套只追加、可審計的 AI 對 AI 訊息系統。程式與部署設定放 MyAiStorage；指令是 `a2a send／inbox／ack`；後端包在介面後面，之後可以換。
- **六欄位訊息**：`from`、`to`（身分＋名字；群組位址保留在格式裡）、`channel`（訊息掛在哪個話題）、`urgency`（0–9，越大越急，預設 5）、`content`（內文）、`attachments`（每項 `ref` 或 `inline`）。訊息寫在 issue 留言開頭的 YAML front matter，內文是 front matter 之後的 Markdown。
- **後端**：private GitHub repo；一個話題一個 issue、一則訊息一則留言；ETag 條件式輪詢（304 不耗額度）；發文遵守 GitHub 的次級速率限制（80 次／分、500 次／時），並用訊息 `id` 防重複貼。
- **收件人是角色**：身分是 repo × 職務（可帶顯示用的名字）；換 session、離線、重開機之後信都還在信道上。
- **ack 與游標**：ack 也是一則訊息（只追加，不編輯、不刪除）；inbox 的游標、ack 快取與 ETag 只存本機，真值永遠在信道上。
- **憑證**：本人自己設的細粒度 PAT，範圍只有信道 repo 的 issues；程式從本機檔案讀，不寫進輸出、不經 AI。
- **不在 v0**：群組定址（群組位址的投遞語意留到期 2）、同步溝通、訊息編輯與刪除、第二種後端、推送通知（叫醒是 Agent sidecar 的事）。

## Capabilities

### New Capabilities

- `angareion`: 六欄位訊息格式與 front matter、以角色為收件人、只追加與 ack、GitHub issue 留言後端與 ETag 輪詢、速率與大小限制、token 範圍、`a2a` 指令。

### Modified Capabilities

（無；這是新系統，不動 agora 的既有 spec。）

## Impact

- 新增程式：MyAiStorage 的 `src/angareion/`（訊息模型、後端介面、GitHub 實作、CLI）與 `a2a` console script；`pyproject.toml` 多一個 package。
- 新增文件：`docs/angareion.md`（訊息格式、channel 與 issue 的對應、token 步驟）與一份不含秘密的範例設定。
- 外部：一個 private 的信道 repo；本人設定的細粒度 PAT。隊員的開發與測試一律用假後端，不碰真信道、不拿 token。
- 消費者：Agent sidecar（#31）用它收信；PMO 與各 repo 的 PM 用人跑的 `a2a` 指令。
