## MODIFIED Requirements

### Requirement: continue --harness-file
`agora continue … --harness-file <檔>` MUST 讓啟動方在啟動**那一個** agent 時交付一份 harness（agent 的指示檔）；只支援 `claude` 與 `opencode`，其他 agent MUST 拒絕。檔案 MUST 存在、不是目錄、可讀——否則 MUST 在啟動 agent 之前失敗，且不寫任何東西。交付 MUST 只對這一個 agent 的 process 生效：MUST NOT 修改 `agora` 自己的 `os.environ`，也 MUST NOT 寫使用者層級的指示檔；同一個 CLI 之後不帶 `--harness-file` 啟動的 session MUST NOT 拿到。不帶 `--harness-file` 時 MUST NOT 注入任何東西。

- `claude`：啟動參數 MUST 加 `--append-system-prompt-file <絕對路徑>` 與 `--system-prompt-snapshot off`。
- `opencode`：MUST 透過只給這個 process 的 `OPENCODE_CONFIG_CONTENT` 交付；harness 的部分 MUST 只含 `instructions`，值是 harness 檔的**絕對路徑**。MUST NOT 把整份設定重貼進環境（避免把憑證帶進環境）。呼叫方環境（`os.environ`）裡原有的 `OPENCODE_CONFIG_CONTENT`，若是 JSON 物件，MUST 與 harness 的部分合併：呼叫方原有的鍵 MUST 保留，兩邊都有的鍵（例如 `instructions`）MUST 以 harness 的值為準（取代，不串接）。原值不是合法 JSON、或不是物件時，MUST 視為沒有、只給 harness 的部分，且 MUST NOT 印出原值。合併 MUST 在 `Launch` 之外做（`Launch.env` 只含 harness 的部分），interactive 與 `--headless` MUST 一致。

#### Scenario: claude 加兩個旗標
- **WHEN** `agora continue session <id> --agent claude --harness-file <檔>`
- **THEN** claude 的啟動參數有 `--append-system-prompt-file <檔的絕對路徑>` 與 `--system-prompt-snapshot off`

#### Scenario: opencode 只帶 instructions
- **WHEN** `agora continue session <id> --agent opencode --harness-file <檔>`，且呼叫方環境沒有 `OPENCODE_CONFIG_CONTENT`
- **THEN** 這個 process 的環境有 `OPENCODE_CONFIG_CONTENT`，內容只含 `instructions`，值是檔的絕對路徑；`agora` 自己的 `os.environ` 沒變

#### Scenario: 呼叫方原有的設定被合併
- **WHEN** 呼叫方環境的 `OPENCODE_CONFIG_CONTENT` 是含 `provider`、`permission` 的 JSON 物件，且帶 `--harness-file`
- **THEN** 這個 process 收到的值同時有原有的 `provider`、`permission` 與 harness 的 `instructions`；`agora` 自己的 `os.environ` 沒變

#### Scenario: 鍵衝突時 harness 優先
- **WHEN** 呼叫方的值已有 `instructions`，且帶 `--harness-file`
- **THEN** 這個 process 收到的 `instructions` 是 harness 檔的絕對路徑，原有的 `instructions` 不保留；其餘的鍵照留

#### Scenario: 原值不是 JSON 物件
- **WHEN** 呼叫方的值不是合法 JSON，或是陣列、字串、數字，且帶 `--harness-file`
- **THEN** 這個 process 收到的值只含 `instructions`；stderr 印一行固定訊息「`[agora] 呼叫方的 OPENCODE_CONFIG_CONTENT 不是 JSON 物件，已忽略，只交付 harness`」，訊息與日誌不含原值；值是空字串時不印這一行

#### Scenario: 不帶就不注入
- **WHEN** `agora continue session <id> --agent claude|opencode` 沒有 `--harness-file`
- **THEN** 啟動參數沒有 harness 的旗標、`Launch.env` 沒有 `OPENCODE_CONFIG_CONTENT`（呼叫方環境原有的值原樣傳下去）；同一個 CLI 之前帶過 `--harness-file` 的 session 也不影響這一個

#### Scenario: 檔案不存在或是目錄
- **WHEN** `--harness-file` 指向不存在的路徑或一個目錄
- **THEN** 在啟動 agent 之前失敗（exit 非 0），說明原因；不啟動 agent
