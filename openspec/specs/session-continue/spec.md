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
一個 Session 正在接續中（這台機器上有另一個 `agora continue` 正在寫回它）時，再對它 `agora continue`（不含 `--fork`）MUST 被拒絕，exit 4，並在中文說明之前先印一行固定格式 `agora-locked <agora id>`（呼叫方不必解析中文；#54）。被鎖的訊號 MUST 和一般輸入錯誤的 exit 1 分開。對它 `--fork` 不受影響。

拒絕 MUST 涵蓋兩條路徑：開頭的提早檢查（`store.continuing`），以及真的拿 `pending/<ULID>.json` 的鎖時。兩個行程幾乎同時接續時，開頭的檢查看不到後到的鎖；輸的那個 MUST NOT 覆蓋贏的那個的 pending 記錄、MUST NOT 啟動 agent，而且 MUST 給同一個訊號。

中文說明 MUST 寫出是誰在接續：該 Session 的 pending 記錄（`pending/<ULID>.json`）裡的 `agent`、`dir`、`started_at`，形如 `正在接續中（agent claude、目錄 ~/proj、從 2026-10-18T05:01:00Z 開始）`；拿不到的欄位 MUST 略過，MUST NOT 印這些以外的私人資訊（例如 `agent_session_id`）。固定行 `agora-locked <agora id>`、exit 4、以及「等它結束再接續（要分岔請用 --fork）」的說明 MUST NOT 變。`delete` 的跳過訊息與 `--force` 的警告用同一套說法（`batch-commands`）。

#### Scenario: 同一台機器再接續一次
- **WHEN** A 正在接續中，在另一個終端機 `agora continue session A --agent claude`
- **THEN** 被拒絕：exit 4，`agora-locked agora:A` 這一行在說明之前，說明 A 正在接續中、並說出是哪個 agent、哪個目錄、從什麼時候開始；第一個接續不受影響

#### Scenario: 說明寫得出是誰在接續
- **WHEN** A 的 pending 記錄有 `agent: claude`、`dir: /home/me/proj`、`started_at: 2026-10-18T05:01:00Z`，再對它 `continue`
- **THEN** 固定行之後的那一行含 agent 名稱、目錄（家目錄縮成 `~`）與開始時間；沒有這些欄位的舊記錄只說「正在接續中」，不編造

#### Scenario: 幾乎同時接續同一個 Session
- **WHEN** 兩個 `agora continue` 幾乎同時接續 A，其中一個先拿到 `pending/<ULID>.json` 的鎖
- **THEN** 另一個在拿鎖時被拒絕：exit 4，同樣先印 `agora-locked agora:A`；沒有啟動 agent，先到的那個 pending 記錄沒有被覆蓋

#### Scenario: 不是被鎖的輸入錯誤
- **WHEN** `agora continue session agora:…`（不存在的 id）`--agent claude`
- **THEN** 照舊 exit 1，不是 4；被鎖的訊號只有同機接續中會給

#### Scenario: 被鎖時 fork 照常
- **WHEN** A 正在接續中，`agora continue session A --agent claude --fork`
- **THEN** 照常 fork 並接續新的 Session，exit 0

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

### Requirement: opencode 的匯出格式（v1／v2）
adapter MUST 自己認出 opencode 的匯出格式：v2 是扁平（`messages[]` 的每一列有 `type`），v1 是巢狀（`messages[]` 的每一列有 `info`）；使用者 MUST NOT 需要指定版本，也 MUST NOT 用 `opencode --version` 猜格式（存下來的 raw 可能是別版產生的）。看不懂的形狀 MUST 是明確的錯誤（exit 非 0），MUST NOT 變成空的閱讀版。

- 閱讀版（`turns()`）對兩版都 MUST 成立：user 的文字、assistant 的內容（文字與工具呼叫各照閱讀版的規則）；`idle` 這種不是對話的列 MUST NOT 產生段落；工具呼叫 MUST 是一行 `[tool] …`。
- v2 的必填欄位與 id／參照的結構位置 MUST 以真實 v2 session 的匯出為準（不是用猜的）。

#### Scenario: v2 的 Session 匯入後閱讀版完整
- **WHEN** 用 `agora import session --external-session-id <id> --agent opencode` 匯入一個 opencode 2.x 的多輪 Session（含工具呼叫）
- **THEN** Session 的 body 有對應的 user／assistant 段落，工具呼叫有一行 `[tool] …`：不是空的

#### Scenario: 看不懂的匯出格式
- **WHEN** 匯出檔的訊息既沒有 `type` 也沒有 `info`
- **THEN** 匯入失敗並說明格式認不出來；沒有存出任何 Session

### Requirement: opencode v2 的接續與 fork
本機的 opencode 是 2.x 時，`agora continue session <id> --agent opencode`（含 `--headless`）、`--fork`、以及 merge 出來的 Session 的接續 MUST 照常運作：接續時載入的是 agent 自己的格式（工具呼叫等不經轉換）、載入後 MUST 回讀驗證（訊息數，以及該格式可數的內容項目數——v1 的 part 數、v2 的 `content` 項目數；該格式沒有這一層時只比訊息數）、原本的 agent session MUST NOT 被改動。

- 同 agent 的 fork MUST 是原 Session 的 raw 加上新訊息（原樣載入）。
- 送給 opencode 的每一個自動化呼叫（匯出、匯入、刪除、寫要約）MUST 帶明確的 server 上下文（`--standalone` 或 `--server`），MUST NOT 讓 opencode 去找或啟動使用者的 managed 背景服務。
- `idle` 這種不是對話的列 MUST NOT 算成新內容：只開了 agent、沒有說話就離開時，MUST NOT 寫回。

#### Scenario: 接續 v2 的 Session
- **WHEN** 對一個 opencode 2.x 匯入的 Session 執行 `agora continue session <id> --agent opencode`，在 agent 裡說了一句話後離開
- **THEN** agent 看得到完整前文（含工具呼叫）；結束後寫回同一個 Session；來源的 agent session 不變；閱讀版完整

#### Scenario: 開了 agent 沒有說話
- **WHEN** 接續 v2 的 Session 後只開了 agent、沒有說話就離開
- **THEN** 沒有新的內容被寫回（`idle` 這種不是對話的列不算新內容）

#### Scenario: fork v2 的 Session
- **WHEN** `agora continue session <id> --agent opencode --fork`
- **THEN** 新 Session 有原樣的前文加上 fork 的兩則；原 Session 不變

#### Scenario: 接續 merge 出來的 Session
- **WHEN** `agora merge session <A> <B>` 之後，接續 merge 印出的那個 Session
- **THEN** 用 v2 的 payload 載入（不是被 v2 拒收的 v1 巢狀）；agent 看得到要約與來源清單；寫回的是那個 Session

#### Scenario: 不叫 managed 服務
- **WHEN** 匯入、接續、寫要約或刪除時 agora 呼叫 opencode
- **THEN** 呼叫帶明確的 server 上下文（`--standalone` 或 `--server`）：不會轉去尋找或啟動 managed 背景服務（例如預設埠 49374 的共用服務）

### Requirement: 舊版 opencode raw 的同 agent 接續
Store 裡在 opencode 2.x 之前匯入的 Session，raw 是 v1 巢狀格式。用同一個 agent 接續或 fork 這種 Session 時，agora MUST 在開 agent 之前以明確的訊息拒絕（exit 非 0），訊息 MUST 誠實說明：重新匯入的指令（`agora import session --external-session-id <agent session id> --agent opencode`）、重新匯入不一定原地更新（被 merge／continue 分支過的 Session 會另存）、以及來源不在本機時只能改用跨 agent 接續。MUST NOT 把 v1 的 payload 送給 v2 的 `session import`（那會留下半個 Session）。

跨 agent 接續舊 raw MUST 照常（閱讀版轉換後重建，本來就重寫）；被拒絕時原本的 Session 與它的 raw MUST NOT 被改動。

#### Scenario: 舊格式的 raw 用同一個 agent 接續
- **WHEN** 對一個 opencode 1.x 時代匯入的 Session（store 裡的 raw 是 v1 巢狀）執行 `agora continue session <id> --agent opencode`
- **THEN** 不開 agent，exit 非 0；訊息說明重新匯入的指令、它不一定原地更新、以及來源不在本機時只能跨 agent 接續；原 Session 與 raw 都不變

#### Scenario: 舊格式的 raw 跨 agent 接續
- **WHEN** 同上，但 `--agent claude`
- **THEN** 用閱讀版轉換照常接續；不因來源是 v1 格式而拒絕

### Requirement: 接續的定期寫回只上傳新增的 raw 段

`agora continue` 的定期寫回與最後一次寫回，在目標是 claude Session、而且上一個版本是分段儲存（`agora.raw.layout: chunks/1`）時，MUST 只上傳這一輪新增的 raw 位元組（新的封存段與 tail，按 `AGORA_RAW_CHUNK` 切成多段），MUST NOT 重傳已封存的段、MUST NOT 上傳整份 raw。使用者看到的結果不變：每一輪寫回之後，`session.md` 仍是包含目前整份對話的版本，讀者接回來仍得到整份 raw。

- 這條只改**上傳的位元組**，不改「什麼時候寫回」「寫回哪一個 Session」「讀者看到什麼」的規則（本 spec 的其他 Requirement 不變）。
- 封存前綴被改寫、來源短到封存點之前、沒有 cursor 時，那一輪 MUST 退回整份（新的世代、整份按 C 切成多段、`session.md` 最後上傳）；前一版在新版確認前 MUST 保持完整。只砍掉封存點之後的內容不算改寫（新的 tail 較短，仍可 append）。基準段不在 Drive 上不是退回整份的理由：上傳的一方 MUST 從本機的整份 raw 補回缺的段；補不回才不上傳。
- **每次新開一個 `continue` 的第一次寫回是整份**：`start_native` 會把每一行換成新的 agent session id（所以封存前綴必然不同），來源 jsonl 也是新檔案；那一輪 MUST 走整份，之後同一個 continue 的定期寫回才差分。這是固定成本，不是失敗。
- 目標是**不是 `chunks/1`** 的 Session（舊格式、未知格式）時，MUST 在啟動 agent 之前就拒絕，訊息說明沒有原地轉存、要重匯請用 `agora import … --as-new`；MUST NOT 自動把它升級、MUST NOT 開 agent 之後才失敗。
- 差分上傳失敗（離線、段沒傳完、Drive md5 不符）MUST NOT 中斷 agent；MUST 照「定期寫回失敗」的既有規則下一輪再試，entry 留在 outbox。
- 閱讀版 `session.md` 仍整份上傳（這個 change 不做它的差分）；`--sync-period 0`、沒有新內容不寫、最後一次寫回、被刪另存等行為都不變。
- 換機器或清掉本機快取之後的第一次寫回 MAY 是整份（沒有 cursor）；之後才差分。

#### Scenario: 長 claude Session 的下一輪
- **WHEN** **在同一個 `continue` 內**接續一個 814 MB 的 claude Session，5 分鐘後多了 1 MB 的對話
- **THEN** 這一輪只上傳新的段與 tail（≤ 4 MiB＋1 MB）與整份 `session.md`；Drive 上的版本仍包含整份對話；agent 畫面沒有任何 agora 的輸出

#### Scenario: 新開 continue 的第一次寫回
- **WHEN** 使用者結束 agent 後再對同一個 814 MB 的 Session 執行 `agora continue`（新的 agent session）
- **THEN** 第一次寫回是整份（約 195 個段、814 MB）；之後才只送新增的段；這一輪沒有壞版本，失敗也照「定期寫回失敗」處理

#### Scenario: 來源被改寫
- **WHEN** 接續期間 agent 的 jsonl 被壓縮或改寫，前綴與上一版不同
- **THEN** 這一輪走整份（新的世代、整份按 C 切成多段、`session.md` 最後）；前一版在新版確認前仍完整；agent 不受影響

#### Scenario: 差分上傳失敗
- **WHEN** 某一輪的段上傳失敗（離線或 rclone 錯誤）
- **THEN** entry 留在 outbox；agent 繼續跑；下一輪再試；結束時照既有規則說明失敗次數與 log 位置

#### Scenario: 換機器後第一次寫回
- **WHEN** 另一台機器接續同一個分段 Session，本機沒有 cursor
- **THEN** 第一次寫回是整份（一次）；之後的輪次只上傳新增的段；讀者看到的版本仍完整

#### Scenario: 舊格式的 Session 不能接續
- **WHEN** 對一個 header 沒有 `layout` 的 Session 執行 `agora continue`
- **THEN** 在開 agent 之前拒絕（exit 非 0），訊息說明沒有原地轉存、要重匯請用 `agora import … --as-new`；不寫任何東西

#### Scenario: 沒有新內容
- **WHEN** 一個定期寫回的間隔內沒有新的對話
- **THEN** 不寫、不上傳（與現行相同）

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
