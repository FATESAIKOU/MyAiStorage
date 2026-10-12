## Purpose

Channel 名與 backend 實體的對應、以及 backend 本身的設定，放在 MyAiStorage repo 的兩個設定檔裡用 PR 管理；讀取端 clone 之後定期 pull，angareion 只讀本地複本。

## ADDED Requirements

### Requirement: channel 設定檔
channel 名到 backend 實體的對應 MUST 寫在 MyAiStorage repo 的 channel 設定檔（`config/angareion/channels.json`；確切位置待本人確認，見 design）。格式 MUST 是 JSON：版本欄加 `channels` 對應表，每個 channel 指到一個 backend 名與該 backend 內的實體定位（GitHub backend：issue 編號）。`send`／`read`／`wait` MUST 經這個檔解析 channel 名；名字不在檔裡 MUST 視為 channel 不存在（exit 1）。

#### Scenario: 設定檔指到 backend 實體
- **WHEN** channel 設定檔把 `general` 指到 GitHub backend 的某個 issue，執行 `angareion send --channel general --body hi`
- **THEN** 訊息進那個 issue 的留言；stdout 印訊息 id

#### Scenario: 名字不在設定檔
- **WHEN** `angareion send --channel nobody-knows --body hi`
- **THEN** exit 1，stderr 說明 channel 不存在；什麼都沒送

### Requirement: backend 設定檔
backend 本身的設定 MUST 寫在 MyAiStorage repo 的 backend 設定檔（`config/angareion/backends.json`；確切位置待本人確認，見 design）。格式 MUST 是 JSON：版本欄加 `backends` 對應表，每個 backend 有 `type`（例 `github`）與該 backend 的設定（GitHub backend：用的 repo）。angareion MUST 只從這個檔知道 backend 的設定，不在個人設定檔裡放 backend 的位置。

#### Scenario: backend 的 repo 來自設定檔
- **WHEN** backend 設定檔把 `github-main` 指到某個 repo，channel 設定檔把 `general` 指到 `github-main` 的某個 issue
- **THEN** `send --channel general` 的訊息進那個 repo 那個 issue 的留言

### Requirement: 更新與讀取流程
兩個設定檔的更新 MUST 是對 MyAiStorage 開 PR，由 AiStorage PM 審查＋merge（AiStorage 的 d 等級工單）。讀取端 MUST 先 clone（含這兩個檔的 MyAiStorage 複本），之後定期 pull；pull 的排程由部署決定（cron 或 systemd timer，建議至少每小時一次），不是 angareion 指令自己做的事。angareion MUST 只讀本地複本：複本缺失或不是合法 JSON MUST exit 2 並說明缺什麼；pull 失敗時沿用上次可用的複本（排程端處理，angareion 不管）。

#### Scenario: PR 合併後新 channel 可用
- **WHEN** channel 設定檔在 MyAiStorage 的 PR 被 merge，讀取端 pull 之後對新 channel `send`
- **THEN** 訊息送出成功（stdout 印訊息 id）

#### Scenario: 本地複本缺失或格式錯
- **WHEN** 本地沒有 channel 設定檔，或它是壞掉的 JSON，執行 `angareion send --channel general --body hi`
- **THEN** exit 2，stderr 說明缺什麼；什麼都沒送
