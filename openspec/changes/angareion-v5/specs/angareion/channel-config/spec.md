## Purpose

channel 名與 backend 實體的對應、以及 backend 本身的設定，放在 MyAiStorage repo 的兩個設定檔（`config/angareion/channels.json`、`config/angareion/backends.json`）；channel 的對應由 `register-channel`／`unregister-channel` workflow 直接更新 main（本人裁定：channel 自由建立、不走 PR），backend 設定走 PR；angareion 只讀本機複本（lazy pull，失敗沿用）。

## ADDED Requirements

### Requirement: channel 設定檔
channel 名到 backend 實體的對應 MUST 寫在 MyAiStorage repo 的 `config/angareion/channels.json`（路徑已定案）：JSON、版本欄（`version`）、`channels` 對應表；每筆是 `{"backend": <backend 名>, "issue": <編號>}`（repo 不重寫，在 backend 設定裡）。`send`／`read`／`wait` 與 `channel create`／`channel delete` MUST 經這個檔解析；名字不在檔裡 MUST 視為 channel 不存在（exit 1）。angareion MUST NOT 直接寫這個檔（只有 workflow 寫，見下面的 requirement）。

#### Scenario: 設定檔指到 backend 實體
- **WHEN** 設定檔把 `general` 指到 backend `github-main` 的某個實體，執行 `angareion send --channel general --body hi`
- **THEN** 訊息進那個實體的留言；stdout 印訊息 id

#### Scenario: 名字不在設定檔
- **WHEN** `angareion send --channel nobody-knows --body hi`（或對它 `channel delete`）
- **THEN** exit 1，stderr 說明 channel 不存在；什麼都沒送、什麼都沒改

### Requirement: backend 設定檔
backend 本身的設定 MUST 寫在 MyAiStorage repo 的 `config/angareion/backends.json`（路徑已定案）：JSON、版本欄、`backends` 對應表；每筆 MUST 有 `type`（目前只有 `github`）與該 type 的設定（GitHub backend：用的 `repo`）。angareion MUST 只從這個檔知道 backend 的設定，不在個人設定檔裡放 backend 的位置；不認得的 `type` MUST exit 2（設定錯誤）。

#### Scenario: backend 的 repo 來自設定檔
- **WHEN** backend 設定檔把 `github-main` 指到某個 repo，channel 設定檔把 `general` 指到 `github-main` 的某個實體
- **THEN** `send --channel general` 的訊息進那個 repo 那個實體的留言

#### Scenario: 不認得的 backend type
- **WHEN** backend 條目的 `type` 不是 `github`
- **THEN** exit 2，stderr 說明不認得的 type；什麼都沒送

### Requirement: channel 對應的更新（register／unregister workflow）
`channels.json` 的更新 MUST 由 MyAiStorage repo 裡的 `register-channel`（新增對應）與 `unregister-channel`（移除對應）workflow 直接寫 main；angareion 送出註冊／註銷請求後 MUST NOT 等它完成。請求 MUST 帶 `action`（`register`／`unregister`）、channel 名、backend 名、實體編號；workflow MUST 驗證 channel 名（`^[A-Za-z0-9_-]+$`）、backend 名存在（在 `backends.json` 裡）。寫入 MUST 以 main 的最新版本為底（讀到該版本的 sha、只改自己那一筆、寫回帶 sha），被拒 MUST 重讀重試（次數有上限）；兩個請求幾乎同時到達時 MUST NOT 遺失任何一筆。同名已有對應而且指到不同實體時 MUST NOT 覆蓋，MUST 回報「名字已被使用」；指到同一實體 MUST 視為成功（冪等）。`unregister` MUST 只移除指向同一個實體的對應；已經沒有那一筆 MUST 視為成功；指到別的實體 MUST NOT 動檔，MUST 回報。成功與失敗 MUST 留在請求可以看見的地方（例：註冊 issue 的留言；請求的傳遞方式見 design §10）。

#### Scenario: 註冊新 channel
- **WHEN** workflow 收到 `{action: register, channel: X, backend: B, issue: N}`（B 在 backends.json、X 不在 channels.json）
- **THEN** main 的 `channels.json` 出現 `{X: {backend: B, issue: N}}`；請求上留下成功的紀錄

#### Scenario: 同名不覆蓋
- **WHEN** 收到 `X` 的註冊，但 `channels.json` 已經有 `X` 指到別的實體
- **THEN** `channels.json` 不變；請求上回報名字已被使用（不是成功）

#### Scenario: 兩個註冊同時到
- **WHEN** 兩個不同名字的註冊幾乎同時到達
- **THEN** 兩筆對應都在 `channels.json`（沒有因為併發遺失或互相覆蓋）

#### Scenario: 註銷
- **WHEN** workflow 收到 `{action: unregister, channel: X, backend: B, issue: N}`，而 `channels.json` 的 `X` 指到 B 的 N
- **THEN** `X` 從 `channels.json` 移除；請求上留下成功的紀錄

#### Scenario: 註銷已經沒有或指到別人
- **WHEN** 收到 `X` 的註銷，但 `channels.json` 已經沒有 `X`，或 `X` 指到別的實體
- **THEN** 前者視為成功；後者不動檔並在請求上回報

### Requirement: 讀取與 pull
angareion MUST 只讀本機複本（MyAiStorage 的 clone；路徑由個人設定 `config_dir` 給）。每個要用設定檔的指令一開始，若上次 fetch 超過 `config_pull_interval`（預設 900 秒）MUST 試一次 `git pull --ff-only`：同機並行 MUST 以鎖檔避免、請求 MUST 有逾時；`wait` 的輪詢中 MUST NOT pull。pull 失敗時 MUST 沿用目前的複本（stderr 警告；exit code 不因此改變）；複本缺失或不是合法 JSON MUST exit 2 並說明缺什麼。

#### Scenario: workflow 更新後新 channel 可用
- **WHEN** `channels.json` 在 main 更新（workflow 或 PR），讀取端下一次操作觸發 pull
- **THEN** 對新 channel `send` 成功（stdout 印訊息 id）

#### Scenario: pull 失敗
- **WHEN** 離線或 `git pull` 失敗，複本還在
- **THEN** 指令照常用目前的複本（stderr 有警告）；exit code 不變

#### Scenario: 本地複本缺失或格式錯
- **WHEN** 本地沒有 `channels.json`，或它是壞掉的 JSON，執行 `angareion send --channel general --body hi`
- **THEN** exit 2，stderr 說明缺什麼或格式錯；什麼都沒送
