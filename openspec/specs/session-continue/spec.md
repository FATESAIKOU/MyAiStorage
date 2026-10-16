# session-continue Specification

## Purpose
接續一個 Session：`--fork` 先複製成新的 Session 再接續、不動原本那個；agent 還在跑時每隔 `--sync-period` 分鐘把對話安靜地寫回，途中被別台刪掉就另存成一個新的，之後都寫到它。
## Requirements
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

### Requirement: 接續期間定期寫回
`agora continue`（含 `--fork`）啟動 agent 之後，agent 執行期間 MUST 每隔 `--sync-period` 分鐘把 agent 目前的整份對話寫回接續的那個 Session，並和其他寫入一樣先存在本機、再由背景上傳到 Google Drive。

- `--sync-period` 是一個不小於 0 的數字，單位是分鐘（可以是小數）；沒給時 MUST 是 5；是 0 時 MUST NOT 自動同步，只在 agent 結束時寫回。負數或不是數字時 MUST 在開 agent 之前就拒絕（exit 1）。

- 和上一次寫回（或啟動時載入的內容）相比沒有新內容時，MUST NOT 寫。
- agent 結束時 MUST 照原本的規則做最後一次寫回。和最後一次定期寫回相同時不必再寫，但 MUST 照樣確認那個 Session 還在；被刪了就照接續的規則另存。
- 定期寫回期間 agora MUST NOT 在終端機輸出任何東西，也 MUST NOT 讀終端機的輸入；訊息記到 log 檔。最後一次寫回之後，有定期寫回失敗過時 MUST 說明失敗次數與 log 的位置。
- 定期寫回失敗（讀不到、上傳失敗等）MUST NOT 中斷 agent，下一輪再試。
- 讀到 agent 寫到一半的內容時，MUST NOT 把 Session 寫壞。
- 接續途中 Session 被刪掉時（不論是這次接續發現、還是背景上傳發現），同一段工作 MUST 只另存成一個新的 Session；之後的寫回（含最後一次與補存）MUST 寫到這個新的 Session。

#### Scenario: 執行中看得到進度
- **WHEN** 用 `agora continue`（沒給 `--sync-period`）接續 Session A，在 agent 裡持續對話超過 5 分鐘，經過一次定期寫回並上傳完成
- **THEN** 不等 agent 結束，A 在本機與 Drive 上就已經是包含新對話的版本

#### Scenario: 沒有新內容不寫
- **WHEN** agent 開著，但一次定期寫回的間隔內沒有新的對話
- **THEN** A 的 session.md 沒有被重寫，Drive 沒有新的上傳

#### Scenario: 不弄亂畫面
- **WHEN** 定期寫回發生時（包括讀到寫到一半的最後一行、上傳失敗）
- **THEN** agent 開始到結束之間，終端機上沒有任何 agora 的輸出；log 檔有記錄；結束時說明失敗過幾次

#### Scenario: 途中被刪
- **WHEN** 接續 A 的途中，A 被別台機器刪掉，之後又經過兩次定期寫回、agent 才結束
- **THEN** 只多出一個另存的新 Session B（`parents` 含 A），之後的寫回都寫到 B；結束時印出另存的說明

#### Scenario: 離線期間被刪
- **WHEN** 接續 A 時 Drive 連不上，期間 A 被別台刪掉；恢復連線後，背景上傳把這次的修改另存成 Y；agent 之後才結束
- **THEN** 只有 Y 一個另存的 Session，之後的寫回與最後一次都寫到 Y

#### Scenario: 最後一次沒有新內容但已被刪
- **WHEN** 最後一次定期寫回把 A 傳上 Drive 之後，A 被別台刪掉，之後 agent 結束時沒有新的對話
- **THEN** 這段對話另存成新的 Session，不會只留在被刪的 A 裡

#### Scenario: agora 被強制結束
- **WHEN** 定期寫回並上傳完成之後，agora 與 agent 都被強制結束
- **THEN** Drive 上已經有那一次定期寫回的內容；下一個指令的補存寫到同一個 Session（途中另存過的話寫到另存的那個）

#### Scenario: 指定間隔
- **WHEN** `agora continue session A --agent claude --sync-period 1`
- **THEN** 每 1 分鐘定期寫回一次

#### Scenario: 不自動同步
- **WHEN** `--sync-period 0`
- **THEN** agent 執行期間不做任何定期寫回，結束時照原本的規則寫回

#### Scenario: 不合法的間隔
- **WHEN** `--sync-period -1` 或 `--sync-period abc`
- **THEN** 不開 agent，exit 1，說明要給不小於 0 的分鐘數

### Requirement: 同一台機器上不能同時接續同一個 Session
一個 Session 正在接續中（這台機器上有另一個 `agora continue` 正在寫回它）時，再對它 `agora continue`（不含 `--fork`）MUST 被拒絕（exit 1），說明它正在接續中。對它 `--fork` 不受影響。

#### Scenario: 同一台機器再接續一次
- **WHEN** A 正在接續中，在另一個終端機 `agora continue session A --agent claude`
- **THEN** 被拒絕（exit 1），說明 A 正在接續中；第一個接續不受影響

### Requirement: agent 死掉要傳出來
agent 子行程以 `returncode != 0` 結束（排除 130／143、-2／-15，以及轉送過 SIGTERM 之後的結束（不論 agent 回什麼碼）；-9／-11 都算失敗）時，`agora continue` MUST 照做寫回（死前的內容 MUST NOT 丟），stderr MUST 印固定一行 `agora-agent-failed rc=<原碼>`（#54 的風格，負值照印如 `rc=-9`）；寫回成功時 MUST 回固定的 5（`EXIT_AGENT_FAILED`），寫回失敗時 MUST 維持寫回自己的失敗碼（3，同樣印那一行）。舊行為是只看寫回結果，agent 死掉又沒有新內容時回 0；第一版回 agent 原碼，但原碼會與 agora 自己的 1／2／3／4 撞碼，故改固定 5。新行為下正常結束（0）、使用者離開、headless 被 SIGTERM 溫和停下（exit 0）MUST 不變。

#### Scenario: agent 死掉且沒有新內容
- **WHEN** agent 以 exit 3 結束，結束時沒有新對話
- **THEN** 寫回照做（沒有新內容就不存），stderr 有 `agora-agent-failed rc=3`，exit 5

#### Scenario: agent 死掉但有新內容
- **WHEN** agent 生前說了一句話，之後以 exit 3 結束
- **THEN** 那句話寫回了 Session，stderr 有 `agora-agent-failed rc=3`，exit 5

#### Scenario: 正常離開
- **WHEN** agent 以 exit 0 結束
- **THEN** exit 0，stderr 沒有 `agora-agent-failed`，行為與舊版相同

#### Scenario: Ctrl-C 慣例碼與 SIGINT／SIGTERM 死不算失敗
- **WHEN** agent 以 exit 130 或 143 結束，或被 SIGINT（-2）／SIGTERM（-15）殺掉
- **THEN** exit 照寫回結果給（成功 0／失敗 3），stderr 沒有 `agora-agent-failed`

#### Scenario: 被其他訊號殺掉算失敗
- **WHEN** agent 被 SIGKILL（-9）或 SIGSEGV（-11）殺掉
- **THEN** 寫回照做，stderr 有 `agora-agent-failed rc=-9`（或 `rc=-11`），exit 5

#### Scenario: 寫回也失敗時維持寫回的碼
- **WHEN** agent 以 exit 7 結束，且寫回上傳失敗
- **THEN** exit 3，stderr 同時有上傳失敗的說明與 `agora-agent-failed rc=7`

