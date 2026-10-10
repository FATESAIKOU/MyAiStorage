## ADDED Requirements

### Requirement: continue --fork
`agora continue session <id> --agent opencode|claude --fork` MUST 先建立一個新 Session，再用它啟動 code agent；原本那個 Session MUST NOT 被改動（內容、header、`updated_at` 都不變）。原本那個在雲端沒有時 MUST 拒絕。

新 Session：
- 使用者 header MUST 照抄原 Session（含 `verified`；merge 自動填的 `status` 除外），`title` 後面 MUST 加上 `-fork`；`--header title=…` 有給時以它為準。`id` 與 `agora` 區塊 MUST 照新 Session 產生，`agora.relation` 是 `fork`，`agora.parents` 指向原 Session；`sources` 含這次的 agent session 與原 Session。
- 對話的最後 MUST 有一則 user 發言「從 <原 id>（<原本的 header>）fork」，以及一則固定的 assistant 回覆；原 Session 以沒有回覆的 user 結尾時，這兩則之前 MUST 先補一則 assistant「（這一段在這裡結束，當時沒有回覆）」。
- MUST 在 agent 啟動前就存檔；agent 結束後有新內容時，MUST 寫回這個新 Session。
- 打開 agent 就離開（沒有新內容）時，新 Session MUST 留著，stdout MUST 印它的 id。

同 agent 時，原 Session 的對話 MUST 原樣載入（工具呼叫等不經轉換）。跨 agent 接續、merge 出來的 Session、fork 出來的 Session 都 MUST 能 fork；fork 的 fork 的 title 是 `…-fork-fork`。原 Session 有 fork 出來的子 Session 時，再匯入原 Session 的來源 MUST 原地更新原 Session。

#### Scenario: fork 一個 claude Session（同 agent）
- **WHEN** A 是 claude 匯入的，`agora continue session agora:A --agent claude --fork`，在 agent 裡說了一句話後離開
- **THEN** 多了一個新 Session B：title 是 A 的 title 加 `-fork`，case、description、tags 和 A 一樣，`agora.relation` 是 `fork`、`parents` 指向 A；B 的對話是 A 的原樣內容，加上「從 agora:A（…）fork」、固定回覆與剛才那句話；A 完全沒變

#### Scenario: 打開就離開
- **WHEN** `--fork` 啟動 agent 後什麼都沒說就離開
- **THEN** stdout 印出 B 的 id，exit 0；B 存在，對話是 A 的內容加上 fork 說明；A 沒變；B 的 agent session 之後在未匯入頁照常比對

#### Scenario: 跨 agent fork
- **WHEN** A 是 opencode 匯入的，`agora continue session agora:A --agent claude --fork`
- **THEN** 用 claude 啟動，看得到 A 的前文與 fork 說明；寫回到新 Session

#### Scenario: fork 一個 merge 出來的 Session
- **WHEN** A 是 merge 出來的（`status: draft`），`--fork`
- **THEN** agent 讀到的是 merge 的要約與來源清單加上 fork 說明；B 沒有 `status: draft`

#### Scenario: fork 的 fork
- **WHEN** 對 fork 出來的 B（title `X-fork`）再 `--fork`
- **THEN** 新 Session 的 title 是 `X-fork-fork`，`parents` 指向 B

#### Scenario: 指定 title
- **WHEN** `--fork --header title=實驗`
- **THEN** B 的 title 是「實驗」（不加 `-fork`），寫回之後也不變

#### Scenario: 離線 fork
- **WHEN** 離線時 fork、在 agent 裡說了話、離開，之後恢復連線
- **THEN** Drive 上出現的是 B 本身（含剛才的對話），沒有另存，也沒有「已被別台刪除」的提醒

#### Scenario: fork 途中新 Session 被刪掉
- **WHEN** fork 出來的 B 在 agent 執行期間被別台機器刪掉，agent 結束時有新內容
- **THEN** 照接續的規則另存成另一個新 Session（`relation: continue`，`parents` 含 B），工作不會消失；A 沒變

#### Scenario: fork 之後再匯入原來源
- **WHEN** A 已經有 fork 出來的 B，原本的 agent session 又多了對話，再 `agora import` 它
- **THEN** A 原地更新，沒有另建新的 Session
