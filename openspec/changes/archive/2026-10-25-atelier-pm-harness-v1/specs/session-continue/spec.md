## ADDED Requirements

### Requirement: continue --harness-file
`agora continue … --harness-file <檔>` MUST 讓啟動方在啟動**那一個** agent 時交付一份 harness（agent 的指示檔）；只支援 `claude` 與 `opencode`，其他 agent MUST 拒絕。檔案 MUST 存在、不是目錄、可讀——否則 MUST 在啟動 agent 之前失敗，且不寫任何東西。交付 MUST 只對這一個 agent 的 process 生效：MUST NOT 修改 `agora` 自己的 `os.environ`，也 MUST NOT 寫使用者層級的指示檔；同一個 CLI 之後不帶 `--harness-file` 啟動的 session MUST NOT 拿到。不帶 `--harness-file` 時 MUST NOT 注入任何東西。

- `claude`：啟動參數 MUST 加 `--append-system-prompt-file <絕對路徑>` 與 `--system-prompt-snapshot off`。
- `opencode`：MUST 透過 `Launch.env` 交付，也就是只給這個 process 的 `OPENCODE_CONFIG_CONTENT`；內容 MUST 只含 `instructions`，值是 harness 檔的**絕對路徑**。MUST NOT 把整份設定重貼進環境（避免把憑證帶進環境）。呼叫方環境（`os.environ`）裡原有的 `OPENCODE_CONFIG_CONTENT`，MUST 會被只含 `instructions` 的值整份取代（現在的行為）。這一點由 #242 處理；#242 改行為時，用它自己的 delta MODIFIED 這一則。

#### Scenario: claude 加兩個旗標
- **WHEN** `agora continue session <id> --agent claude --harness-file <檔>`
- **THEN** claude 的啟動參數有 `--append-system-prompt-file <檔的絕對路徑>` 與 `--system-prompt-snapshot off`

#### Scenario: opencode 只帶 instructions
- **WHEN** `agora continue session <id> --agent opencode --harness-file <檔>`
- **THEN** 這個 process 的環境有 `OPENCODE_CONFIG_CONTENT`，內容只含 `instructions`，值是檔的絕對路徑；`agora` 自己的 `os.environ` 沒變

#### Scenario: 不帶就不注入
- **WHEN** `agora continue session <id> --agent claude|opencode` 沒有 `--harness-file`
- **THEN** 啟動參數沒有 harness 的旗標、`Launch.env` 沒有 `OPENCODE_CONFIG_CONTENT`（呼叫方環境原有的值原樣傳下去）；同一個 CLI 之前帶過 `--harness-file` 的 session 也不影響這一個

#### Scenario: 檔案不存在或是目錄
- **WHEN** `--harness-file` 指向不存在的路徑或一個目錄
- **THEN** 在啟動 agent 之前失敗（exit 非 0），說明原因；不啟動 agent
