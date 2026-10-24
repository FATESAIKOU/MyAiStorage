## Context

見 `proposal.md`「Why」。工單：MyAiStorage#212（b 級，設計先審、本人同意才實作）。

**標記**：【事實】附出處；【推論】是我（作者）從事實推的；【未知】沒查到或需要實測，列在最後一節「待驗」。

### 事實

| # | 事實 | 出處 |
|---|---|---|
| F1 | Angareion 對外五個指令；`send` 要 `--channel`，`--to`／`--mention`／`--in-reply-to`／`--urgency`／`--ref`／`--attach`／`--body` 選填或必填如其說明；成功時 stdout 印訊息 id | `skills/angareion/SKILL.md`；`src/angareion/cli.py:447-466` |
| F2 | 收訊一律不加 `--channel`；「寄給我的」＝`to` 是我、廣播、或 `mentions` 有我；`--mentioned` 不含廣播、跨所有 channel、有自己的游標；`wait` 不動游標；`wait` 結束碼 3＝逾時 | `skills/angareion/SKILL.md`；`docs/angareion.md` 位址寫法與游標 |
| F3 | 自己的 AI 名來自設定檔 `identity`，`ANGAREION_IDENTITY` 優先 | `skills/angareion/SKILL.md` |
| F4 | `--ref NAME=LOCATOR` 產生 `{"name","ref"}` 附件；一則訊息上限 65536 字元 | `src/angareion/cli.py:184-188`；`docs/angareion.md` 訊息格式 |
| F5 | channel 名不是由 AI 名推得：現有 `team-mylinuxpool` 與 `team-mylinuxpool-w1` 並存、`secretary-pmo`、`team-life-w1` | `config/angareion/channels.json` |
| F6 | sidecar 對外四個動作（open／close／status／poke），HTTP 只綁 loopback、無認證；agent id 必須是合法 AI 名 | MyLinuxPool `modules/sidecar/docs/sidecar.md` §1、§2.2 |
| F7 | `GET /agents/{id}` 回 `state`（`starting`／`running`／`idle`／`waiting-quota`／`exited`）與 `attach`（**只有 tui 有值**，headless 與 opencode 是 null）；不存在回 404 `agent_not_found` | 同上 §2.1、§2.2 |
| F8 | `POST /agents/{id}/poke` 的 `text` 必填、`interrupt` 預設 false；不打斷時「agent 正在跑就排到這一輪之後」；`exited`／`waiting-quota` 回 409 `agent_not_running`；`starting` 時排隊最多 `poke_queue_timeout`（預設 30 秒）逾時回 409 `poke_timeout`；sidecar 關閉中回 503 | 同上 §2.2、§2.4 |
| F9 | tui 的狀態來自 hooks：第一則 `UserPromptSubmit`→`running`、`Stop`→`idle`；沒有輸入的 tui 會一直停在 `starting`；對 `running` 的 agent 輸入會排隊；輸入沒被確認時 sidecar 只記 WARN、`input_unconfirmed_at` 設值，**不回錯，因為重送會變兩份** | 同上 §2.5 |
| F10 | `attach` 中的 agent 被 poke 照送並記 WARN | 同上 §2.5（Q2 裁定） |
| F11 | MyPMO `dispatch.py reply` ＝ `send_message`＋`poke_agent("channel 有新回覆，去讀。", interrupt=False)`；poke 失敗時通知本人並回 2（`--allow-poke-failure` 可吞）；`send` 是派工：驗工單號、讀工單、組樣板、送出、記 `received_at` 到 state 檔 | MyPMO `pmo/dispatch.py:2192-2205`、`:1915-1990` |
| F12 | MyPMO 對 sidecar 的 poke 逾時＝`poke_queue_timeout`＋10 秒餘裕，遇 409 `agent_not_running` 依 `state` 分類說明；不重試 | `pmo/dispatch.py:1724-1760` |
| F13 | 現有 skill 的分發：`skills/angareion/SKILL.md` 由 MyLinuxPool 建映像時用 tool-pins 裡裝 `angareion` 的**同一個 sha** 取檔，放 `~/.claude/skills/angareion/`；opencode 自動讀 `~/.claude/skills/` | `docs/angareion.md`「說明檔（skill）」 |
| F14 | harness 是 `atelier/<series>/<role>/v<N>.md`、append-only；`v>=1` 的正文只寫能力、不寫工具名；已知能力名 `github`／`angareion`／`agora`／`channel`／`team`／`monitor`，未知名只警告；`v1` 起按 agent 交付：啟動方跑 `atelier render <id> [--append <環境說明>] --out <檔>` 再交給那一個 process；環境說明（`--append`）放環境那一側、可寫工具名 | `docs/atelier/harness.md`；`docs/atelier/tool.md`「render」 |
| F15 | `pyproject.toml` 現有 console scripts：`agora`／`angareion`／`atelier` | `pyproject.toml:14-17` |
| F16 | 期 4 M2 設計已定：PMO 的實質回覆最慢等下一次叫醒（30 分鐘）；只有特定 channel 內高 urgency 的訊息 watch 才直接 poke | `plans/m2-design-165.md` §0 |

### 推論（不是事實）

- **I1**：tui agent 在 `angareion wait` 阻塞時，從 sidecar 看是 `running`（它的 tool call 還沒結束，沒有 `Stop`）；這種 agent 不需要被戳，`wait` 自己每 15 秒會看到新訊息（F2）。依據：F9 的狀態來源是 hooks，F2 的 `wait` 是長時間的 tool call。**要實測。**
- **I2**：同一個人連送多則，第一次戳讓 `idle`→`running`（`UserPromptSubmit`），後面的 `send` 看到 `running` 就不再戳，等於天然去重。依賴 I1 與 F9。
- **I3**：Mac 上 sidecar 的埠不一定是預設值（MyPMO 的 team 設定用每 team 一個 `local_sidecar` 位址，F11 附近 `dispatch.py` 檔頭說明）；所以位址要可設定。

## Goals / Non-Goals

**Goals**

- 回答 #212 列的四件事：介面（D1）、叫醒的判斷（D2）、Atelier 分發與版本（D3）、與 `dispatch.py send` 的關係（D4）。
- 讓 PMO／PM／隊員「送一次、收一次」，不用記兩邊。

**Non-Goals**

- 不改 Angareion（協定、指令、程式）；不改 sidecar（介面、程式）。
- 不做跨機叫醒（見 D2.6）；不做訊息內容的傳遞經由 sidecar（戳不帶內容）。
- 不取代 MyPMO 的派工流程（見 D4）。
- 不做身分認證、不做權限（Angareion 本來就「不做嚴格的身分認證」，`docs/angareion.md`）。
- 實作（只列 tasks）。

## 分層與依賴方向

```
 agent（PMO／PM／隊員）
        │ 呼叫
        ▼
 ┌──────────────────────┐
 │  aicomm（本 skill）    │  叫醒的判斷只在這一層
 └──────┬────────┬───────┘
  公開 CLI│        │公開 HTTP（查 agent、戳）
        ▼        ▼
  angareion     sidecar        ← 彼此沒有任何依賴、也不認識 aicomm
```

依賴只往下：skill 依賴兩者**已公開**的介面；兩者各自的 spec 不因本 change 變動（MyLinuxPool#346 的原則）。

## D1 skill 的介面

### D1.1 指令集

| 選項 | 內容 | 代價 |
|---|---|---|
| **A（建議）** | 只有 `send` 與 `inbox` | 其他事（開 channel、開關 agent）繼續用各自的工具；一個 AI 要記的只有兩個動詞 |
| B | 再加 `ack`／`status`／`poke`／`channels` | 每加一個都是要被記住的介面；Angareion 明說「沒有 ack、沒有已讀回條」（`skills/angareion/SKILL.md`），加 `ack` 等於在 skill 層補 Angareion 刻意不做的東西 |
| C | 只有 `send`（收信仍用 `angareion read`） | 收信的「不加 `--channel`」「要先 `wait`」仍要靠 AI 記得；而 `--channel` 誤用已經是 M0 的教訓（F2） |

**建議 A。** 理由：#212 要的是「省精力」，指令少且各只做一件事。`poke` 不單獨暴露——叫醒是 `send` 的副作用，不是 AI 要主動操作的事（要打斷、要自訂文字的戳，是 PMO 的 `dispatch.py poke` 的事，D4）。

**`send` 的旗標**（建議）：

```
aicomm send [--channel <ch>] [--to <AI 名>] [--mention <AI 名>]… [--in-reply-to <ULID>]
            [--urgency 0-9] [--ticket <repo>#<n>] [--no-wake] --body <文字|->
```

- `--to`／`--mention`／`--in-reply-to`／`--urgency`／`--body`：原樣轉給 Angareion，驗證也留給 Angareion（送嚴的規則在那裡，F1），skill 不重寫文法。
- `--attach`／`--ref`：**不提供**（需要時用 `angareion send`）。理由：兩個指令的目標是一般溝通；附件是少數情形，且 `--ticket` 已經用掉 `--ref` 的一個用途。這是作者提議（見提議清單）。
- `--body` 一定要給（與 Angareion 一致，F1）；`-` 讀 stdin。

**`inbox` 的旗標**（建議）：

```
aicomm inbox [--wait <秒>] [--mentioned] [--json]
```

- 不提供 `--channel`、`--since`：F2 的紀律由 skill 直接擋掉。
- `--wait <秒>`＝先 `angareion wait --timeout <秒>`，exit 0 才 `read`；逾時 exit 3。`--wait 0` ＝查一次。不給 `--wait`＝直接 `read`（可能沒有新訊息、輸出為空、exit 0）。
- `--mentioned`：給「只想看被點名、不想被廣播吵」的席位（F2、#209）；不給＝預設的「寄給我的」。

### D1.2 要不要帶工單號

| 選項 | 內容 | 代價 |
|---|---|---|
| **A（建議）** | 選填 `--ticket <repo>#<n>` → `--ref ticket=<repo>#<n>` | 工單號是結構化的附件、內文不被改；讀的人從 `read --json` 看得到。不帶就是一般訊息 |
| B | 不提供，由 AI 自己寫在內文 | 沒有結構；之後想依工單過濾要 grep 內文 |
| C | 必填（像 `dispatch.py send`） | 一般訊息（問候、ack、問題）被迫編號；違反 #212「不帶也能送」 |

**建議 A。** `dispatch.py send` 的 `<repo>#<n>` 寫法（F11）沿用，skill **只驗格式、不讀工單**（讀工單需要 GitHub 權限，且是派工的事，D4）。格式驗證的正規式放實作時照 `dispatch.py` 的 `TICKET_RE` 抄（我沒讀到該常數的定義，【未知】；實作時再對）。

### D1.3 與 #209 的 `mentions`／`in_reply_to`／`--mentioned` 怎麼配合

| 項目 | 建議 |
|---|---|
| `--mention` | 轉給 Angareion；**被 mention 的同台 tui 閒置 agent 也叫醒**（D2.2）。理由：mention 的語意就是「這件事你要知道」，與 `to` 同等 |
| `--in-reply-to` | 原樣轉給 Angareion；**不影響叫醒**。skill 不從收到的信自動帶（避免 AI 沒意識到自己在回覆誰） |
| `inbox --mentioned` | 見 D1.1。預設不開 |
| 廣播（無 `--to`） | **不叫醒任何人**。理由：廣播會叫醒整台機器上所有閒置 tui，花每個人的模型額度；要叫人就用 `--to` 或 `--mention`。 |
| 順序風險 | skill 的 SKILL.md 沿用 Angareion 的「讀者先升級、再送 `--mention`」警告（`skills/angareion/SKILL.md`）。**不另做版本偵測**（見提議清單） |

### D1.4 channel 怎麼決定

`send` 要有 channel（F1），但 AI 名推不出 channel（F5）。

| 選項 | 內容 | 代價 |
|---|---|---|
| A | 每次都要 `--channel` | 最單純；但這正是「要記的東西」，而且錯 channel 的代價是對方讀不到 |
| **B（建議）** | `--channel` > 環境變數 `AICOMM_CHANNEL`（啟動方給每個 agent 它的 team channel）> exit 1 並提示 | 一個 team 內的 PM／隊員／PMO↔PM 都在同一個 team channel，省去每次寫；沒設又沒給就明確失敗，不猜 |
| C | 從 AI 名推 `team-<專案>-<team>` | F5 證明推不出（`team-mylinuxpool` 沒有 team 段；PMO 的 channel 也不在清單） |
| D | 讀 Angareion 的 channel 註冊簿，挑「收件人最近發過信的 channel」 | 要讀註冊簿與歷史；等於 skill 自己做路由，膨脹 |

**建議 B。** 【未知】啟動方（sidecar／開 pane 的指令）能不能替每個 agent 設環境變數（見待驗 V4）。若不能，退到 A，skill 本身不變。

## D2 叫醒的判斷

### D2.1 流程

```
send:
 1. 驗參數（channel 解得出來、--ticket 格式、--no-wake…）           失敗 → exit 1，不送
 2. angareion send …                                              失敗 → 原結束碼，不叫醒
 3. 印訊息 id（stdout 第一行）
 4. 若 --no-wake 或 沒有 --to／--mention → 結束（exit 0）
 5. 對 {to} ∪ {mentions} − {自己}，各做：
      GET /agents/{id}（逾時短）
        404 / 連不上 / 回應看不懂   → 記一行原因，不戳
        200：
          attach == null（非 tui）   → 記「非 tui，略過」
          state ∈ {idle, starting}   → POST /agents/{id}/poke {text: 固定短句, interrupt: false}
          其他 state                  → 記「<state>，略過」
 6. exit 0
```

### D2.2 怎麼知道「在同一台 sidecar、是 tui、閒置」

| 條件 | 判斷方式 | 出處 |
|---|---|---|
| 同一台 sidecar | 對本機 sidecar 問 `GET /agents/{id}`：200＝在；404＝不在這台 | F6、F7；agent id 就是 AI 名（F6、F11：dispatch 用同一個字串當 `to` 和 `/agents/{id}`） |
| tui | 回應的 `attach` 非 null | F7 |
| 閒置 | `state == idle`；另含 `starting`（tui 還沒收過第一則輸入，戳就是它的第一則輸入，F9） | F7、F9 |

**不戳 `running` 的理由**：F8 說 `running` 時戳會排到這一輪之後；而 I1 說阻塞在 `wait` 的 agent 也是 `running`——它不需要戳；真正在忙的 agent 戳了只是排隊，其結果是它結束這一輪後多一則「去讀信」的輸入，I2 說這會造成重複戳。取捨：忙碌中的 agent 要等它自己下一次 `inbox`／`wait` 才讀到。這是**有意的代價**（見判斷點 J2）。

### D2.3 其他選項：誰來做判斷

| 選項 | 內容 | 代價 |
|---|---|---|
| **A（建議）** | skill 查 sidecar（本設計） | 符合 #346：兩邊不互相依賴；代價是 skill 要知道 sidecar 的**公開** HTTP 形狀（F7、F8），sidecar 改這兩個形狀時 skill 要跟著改（見 D2.7） |
| B | Angareion 的 `send` 加 `--wake` 去戳 | Angareion 依賴 sidecar，違反 #346 |
| C | sidecar 監看 Angareion 並自己叫醒 | sidecar 依賴 Angareion，違反 #346；而且 sidecar 要自己持有 token |
| D | 另一個常駐程式（watcher）輪詢 Angareion 再戳 | 多一個要維運的行程；MyPMO 的 `watch` 已經是這個形狀（F16），再做一個重複 |

### D2.4 叫醒失敗、sidecar 不在

**建議：訊息寫進 Angareion＝送達；叫醒是盡力而為。**

| 情形 | 行為 |
|---|---|
| Angareion 寫入失敗 | `send` 失敗（原結束碼）；不叫醒 |
| sidecar 連不上／sidecar 位址不是 loopback | 寫入成功則 exit 0；stderr 一行「已送達，未叫醒：<原因>」；位址不是 loopback 時 exit 1 且不送（在步驟 1 就擋） |
| 404（不在本機） | 正常；stderr 一行「不在本機」 |
| poke 409 `agent_not_running` | 不重試；stderr 記 state（F8、F12） |
| poke 409 `poke_timeout`／client 逾時 | **不重送**；stderr 一行「叫醒結果未知」。理由：F9 明說 hook 不通與 Enter 被吃分不出來，重送會變兩份 |
| poke 503 `shutting_down` | 不重試（F8 + `dispatch.py` 對 poke 同此，F12 之前的註記） |
| 其他非預期（HTTP 狀態、JSON 壞） | 視為叫醒失敗、不重試、不影響 exit 0 |

**對比 `dispatch.py reply`（F11）**：它 poke 失敗時 exit 2＋通知本人。skill **不**這樣做：skill 沒有「通知本人」的通道，且叫醒失敗不該讓 AI 以為訊息沒送出而重送（重送＝對方看到兩則）。取而代之的是 stderr 一行，AI 看得到。若呼叫端（例如 `dispatch.py reply`）想保留 exit 2 的語意，要自己讀 stderr 或加 `--json`（見提議 P3）。

**結束碼**（建議）：0＝訊息已寫進 Angareion（不論叫醒結果）；1＝用法錯（含 channel 解不出、非 loopback、格式錯）；2＝Angareion 後端或設定錯；3 僅 `inbox --wait` 逾時。叫醒結果另放 stderr，`send --json` 則在 stdout 印 `{"id":…,"wake":[{"to":…,"result":…}]}`（【選項】，列在提議 P3）。

### D2.5 戳的內容

固定短句，繁體中文，例如「有新訊息：請執行 aicomm inbox。」。**不含寄件者、不含內容、不含工單**。理由：(1) 訊息可能含工單細節而 sidecar 的 log 記 poke 嗎【未知】；(2) 訊息內容是不可信輸入，經 Angareion→`inbox` 才進模型，戳是 skill 自己寫的字，兩條路徑分開；(3) 收件人讀了信才有完整上下文。

### D2.6 不能叫醒的 agent 怎麼收信

| 收件人 | 怎麼收 | 備註 |
|---|---|---|
| headless（`attach` 為 null；隊員常見） | 閒著時 `aicomm inbox --wait <秒>` 阻塞等，逾時再下一次（Angareion 的既有做法，`skills/angareion/SKILL.md`「閒著的時候」） | skill **不戳** headless：sidecar 文件沒有說明 headless 的 poke 在 agent 閒置時的語意（§2.2 只說 `exited`／`waiting-quota` 會 409），【未知】V2 |
| 別台機器的 agent | 同上；或由控制端（例如 MyPMO 的 watch，F16）定期戳 | 本機的 sidecar 只綁 loopback、無認證（F6），skill **不做**跨機轉發與 ssh（brief 與本人原則：各守各的介面） |
| tui 但 `running` | 它下次 `inbox`／`wait` 時讀到（D2.2） | |
| sidecar 沒在跑 | 同 headless | |
| 所有收件人的保底 | 訊息已在 Angareion，`wait` 與 `read` 一定讀得到 | 這就是「送達」的定義 |

### D2.7 對 sidecar 的依賴面與版本

skill 依賴 sidecar 的兩個公開端點與三個欄位（`state`、`attach` 的有無、404 語意）。**回應不符預期一律當叫醒失敗、不戳**（寧可不戳也不亂戳）。sidecar 若改這些形狀，應在 sidecar 的版本說明標示；skill 的 `--version` 印出它認得的 sidecar 欄位版本以便對照（【建議，實作時決定】）。這是 #346 原則下**無法避免**的耦合：兩者之間的約定就是 sidecar 公開的 HTTP 形狀。

### D2.8 位址與安全

- sidecar 位址：環境變數 `AICOMM_SIDECAR`（可給多個，逗號分隔，依序問，第一個 200 的為準；I3），預設為 sidecar 文件的預設 loopback 位址。
- **只接受 loopback 主機**（`localhost`、`127.0.0.0/8`、`::1`）；其他一律 exit 1。理由：sidecar 無認證（F6），skill 不該被設定成去戳遠端；跨機要走的轉發由別人做。
- 查詢與戳的逾時：查 3 秒；戳＝sidecar `poke_queue_timeout` 預設 30 秒＋10 秒餘裕（照 F12）。因 `idle` 的 tui 戳約 0.3 秒返回（F9），只有 `starting` 才會久；`send` 因此最壞多等約 40 秒。【選項】可改成 `starting` 時背景戳不等結果；列在判斷點 J3。
- 自己不戳自己：`--to` 或 `--mention` 等於自己的 AI 名（F3）時略過。

## D3 經 Atelier 分發：位置、交付、版本

「經 Atelier 分發」有兩層意思，我分開講：(甲) **執行檔與說明檔**怎麼到各 agent 手上；(乙) **告訴 agent「你該用它」** 的職務規矩（harness）怎麼寫。

### D3.1 檔放哪、怎麼交

| 選項 | 內容 | 代價 |
|---|---|---|
| **A（建議）** | 執行檔作為本 repo 同一個 distribution 的第四個 console script `aicomm`（F15），說明檔放 `skills/aicomm/SKILL.md`，**走 angareion skill 現有的路**（F13）：建映像／Mac 安裝時用 tool-pins 裡同一個 sha 裝指令、取說明檔、放 `~/.claude/skills/aicomm/`；opencode 自動讀同一處 | 零新機制；版本與 `angareion` 同 sha 一起釘。代價：說明檔是「每個使用者層級」都有，不是按 agent 交付——**但這個 skill 對所有角色都適用**（PMO、PM、隊員都要溝通），不像 PM harness 要擋隊員，所以沒有 v1 harness 那個「按 agent」的需求 |
| B | Atelier 增加一種 artifact：`atelier/skills/<name>/v<N>/…`（append-only、`atelier show/render` 一路到底），啟動方把它 render 進該 agent 的 skills 目錄 | 角色可拿不同版本、版本與 Atelier 的紀律一致；但要改 `src/atelier/`、格式檢查、spec，且 per-agent skills 目錄的交付方式【未知】V5（現有 `render` 只產一個指示檔，`docs/atelier/tool.md`） |
| C | 把用法直接寫進 harness 本文／環境說明（`--append`） | harness 正文不能寫工具名（F14），只能放環境說明；環境說明每環境一份、散；而且 skill 的完整說明（旗標、結束碼）放進每個 agent 的系統指示會佔上下文 |
| D | 只靠各 repo 的 `AGENTS.md` | 每個 repo 各抄一份、一定漂移 |

**建議 A。** 理由：這個 skill 的實質內容是**一個指令加一份說明**，跟 `angareion` 完全同型，沿用既有交付路徑最小；B 的好處（角色別版本）目前沒有需求。**判斷點 J4**請本人確認「經 Atelier 分發」要不要字面上走 B。

### D3.2 harness 怎麼提到它（乙層）

- harness 的 `capabilities` 加一個中性能力名 `comm`（F14：未知名只警告，所以不需改格式檢查即可先用；但 `KNOWN_CAPABILITIES` 該補，列 tasks）。**正文只寫能力**（「與其他 AI 互相留言並在對方閒置時叫醒」），工具名 `aicomm` 與用法寫在**環境說明**（F14），例如 Mac 與 worker 各一份。
- harness 是 append-only（F14）：要加 `comm` 就得出新版（pm `v3`、impl／rev `v2`）。**建議不為了這件事單獨出新版**：第一輪不改 harness，靠 skill 的 `description`（讓 agent 在需要「留言／等信／叫某個 agent」時載入）加環境說明；下一次 harness 本來就要改版時順便加 `comm`。理由：harness 改版要經審查＋本人同意（F14「改規矩的唯一路徑」），不值得為一行能力名單獨走。代價：第一輪靠 skill 的 description 被載入，【未知】agent 是否可靠地選到它（V6）；若實測不可靠，就單獨出版本。
- PMO 的職務規矩不在本 repo（PMO 是 MyPMO 的事）；PMO 要怎麼被告知【未知】，列為跨 repo 的 tasks。

### D3.3 版本

| 層 | 怎麼管 |
|---|---|
| 指令＋說明檔 | **同一個 git sha**（跟 `angareion` 一起被 tool-pins 釘）；說明檔 frontmatter 不另寫版本號，避免兩個版本號分歧。`aicomm --version` 印安裝的 sha（做法同 sidecar 的 `--version`，F6 附近 §7，僅類比） |
| 與 Angareion 的相容 | 同 distribution、同 sha，天然一致；`--mention` 需要讀者先升級（`skills/angareion/SKILL.md`）——因為同 sha 一起裝，**同一批 agent 不會有「指令新、Angareion 舊」** |
| 與 sidecar 的相容 | 不同 repo、不同釘法；靠 D2.7：回應不符就不戳。sidecar 的 `--version` 與 skill 認得的欄位對照放 `docs/aicomm.md` |
| harness | 見 D3.2 |

**升級順序**：先升級所有讀者（裝新 sha），再讓任何人開始用 `--mention`（沿用 #209 的規矩）；`aicomm` 首版**不帶新的訊息欄位**，所以首版本身沒有讀者先升級的問題。

## D4 與 MyPMO 的 `dispatch.py send` 的關係

`dispatch.py` 有三個相關的動作（F11）：`send`（派工）、`reply`（回覆＋戳）、`poke`（單獨戳）。它們不是同一件事：

| `dispatch.py` | 做什麼 | 對 skill |
|---|---|---|
| `send <team> <repo#n>` | 驗工單號、讀工單、組派工樣板、送出、**把 `received_at` 寫進 state 檔供 `open --since` 與 watch 用**、trial 守門；此時 PM **可能還沒開起來** | 不取代：它的價值在樣板與 state，不在「送＋戳」；而且發生在 PM 開起來之前，沒有東西可戳 |
| `reply <team>` | `send_message`＋固定文字 poke | **這就是 skill 的 `send`，只是綁死在一個 team 的 PM 上**。由 skill 取代 |
| `poke <team> [--interrupt]` | 自訂文字、可打斷 | 並存：PMO 的控制手段（打斷、暫停語意，sidecar §1 的「暫停＝poke＋interrupt」），skill 刻意不暴露 |

| 選項 | 內容 | 代價 |
|---|---|---|
| A | 全面取代（`send`／`reply`／`poke` 都走 skill） | `send` 的 state、樣板、trial 守門要搬進 skill 或丟掉——skill 變成 MyPMO 專用，違反「通用」；`poke --interrupt` 無法表達 |
| B | 包一層：`dispatch.py send` 最後一步呼叫 `aicomm send` | 派工當下 PM 常常還沒開，叫醒無意義；而且 `dispatch.py` 要在送出後取 `received_at`（F11），多一層沒有好處 |
| **C（建議）** | **並存，按用途分**：派工＝`dispatch.py send`（不變）；日常溝通（ack、回報、提問、PMO↔PM、PM↔隊員）＝`aicomm`；`dispatch.py reply` 改成**包一層** `aicomm send`（或直接由 PMO 改用 aicomm、`reply` 標 deprecated）；`dispatch.py poke` 不變 | 兩套工具並存一段時間；界線清楚（派工 vs. 溝通） |

**遷移步驟**（建議，跨 repo 的由 PMO 排）：

1. 本 change 實作完成、`aicomm` 隨 tool-pins 的下一個 sha 進映像與 Mac（MyAiStorage＋MyLinuxPool）。
2. 待驗 V1～V4 在拋棄式環境實測通過（brief：本設計階段不碰 sidecar）。
3. PMO 與 PM 的環境說明加一行「溝通用 `aicomm`」（MyPMO／各 repo）；先**只用在新的對話**，派工流程不動。
4. MyPMO 的 `dispatch.py reply` 改為呼叫 `aicomm send`（一個 MyPMO 的小 PR）；測試覆蓋「叫醒失敗仍視為送達」的差異（F11 的 exit 2 語意會變，要 PMO 決定要不要保留）。
5. 觀察一段時間（建議一個 PM 週期）後，`reply` 標 deprecated。`send`、`poke`、`watch`、`open` 保留。
6. 保底不拆：MyPMO 的 watch 仍是定期戳（F16），即使 `aicomm` 叫醒失敗，最慢也會被下一次定期叫醒補上。

## 本人的判斷點

| # | 要本人選的事 | 選項 | 作者建議 |
|---|---|---|---|
| J1 | 介面的範圍 | A 只有 `send`／`inbox`；B 再加 ack 等；C 只有 `send` | **A**（D1.1） |
| J2 | `running` 的 tui 要不要戳 | A 不戳（略過，等它自己讀）；B 戳（不打斷，排到這一輪之後） | **A**。代價：長時間忙碌中的 agent 會晚收到；補救是保底的定期戳（D4 步驟 6）與 `urgency` 高時人工用 `dispatch.py poke`。B 會有重複戳 |
| J3 | `starting` 的 tui 戳要不要等結果（最壞多等約 40 秒） | A 等；B 背景戳不等（結果未知） | **A**（單純、有結果可報）；等實測看 `starting` 常不常見 |
| J4 | 「經 Atelier 分發」的意思 | A 沿用 angareion skill 的分發路徑（同 sha）；B Atelier 新增 skill artifact；C 寫進環境說明 | **A**（D3.1）。B 是之後角色別版本有需求時再做 |
| J5 | 廣播要不要叫醒 | A 不叫醒；B 叫醒所有同台閒置 tui | **A**（D1.3）。要叫醒全部時用 `--mention` 列名 |
| J6 | 要不要第一輪就改 harness（`comm` 能力名） | A 不改，等下次改版；B 單獨出新版 | **A**（D3.2），除非實測 skill 的 description 載入不可靠 |
| J7 | 與 `dispatch.py` 的關係 | A 全取代；B 派工包一層；C 並存、`reply` 被取代 | **C**（D4） |
| J8 | 名字 | `aicomm`（暫名）；備選 `ai-talk`、`mail` | `aicomm`。**不用 `comm`**（與 POSIX 的 `comm` 指令撞名）|

## 作者提議、本人未指示

| # | 我加的東西 | 理由 | 要不要由你拒絕 |
|---|---|---|---|
| P1 | 不提供 `--attach`／`--ref` | 保持介面小；`--ticket` 已用掉 ref | 可改為轉傳 |
| P2 | 位址只接受 loopback | sidecar 無認證 | 可拿掉，但我不建議 |
| P3 | `send --json`（stdout 印 `{"id","wake":[…]}`）給程式（如 `dispatch.py reply`）讀叫醒結果 | `reply` 現在要 exit 2 的語意（F11），需要機器可讀的結果 | 可不做，改讀 stderr |
| P4 | `inbox` 不提供 `--channel`／`--since` | 擋住 M0 的誤用（F2） | 可放開 |
| P5 | `AICOMM_CHANNEL`、`AICOMM_SIDECAR` 兩個環境變數 | D1.4、D2.8 | 名字可改 |
| P6 | 自己不戳自己 | 避免送給自己的備忘戳醒自己 | 無傷，可拿掉 |
| P7 | 首版不做 `--mention` 的版本偵測 | 同 sha 一起裝，不會有新指令舊 Angareion | 可加 |
| P8 | 戳的文字固定、不含內容 | D2.5 | 可改成含寄件者 |
| P9 | harness 的 `comm` 能力名（下一次改版再加） | D3.2 | 可不加 |
| P10 | 新建 `docs/aicomm.md` 記 sidecar 欄位對照 | D2.7 | 可併入 SKILL.md |

## 風險與取捨

- **耦合 sidecar 的公開 HTTP 形狀**：不可免（D2.7）；降低影響的方式是「看不懂就不戳」。
- **提示注入**：訊息內容是不可信輸入；戳的文字由 skill 固定、不轉述內容（D2.5）；`inbox` 的輸出仍會進模型，這點與 `angareion read` 相同，沒有惡化。
- **額度**：每次成功戳＝一次模型輪次。去重靠 I2；廣播不戳（J5）。
- **私人資訊**：戳與 stderr 都不含內文；說明檔沿用 Angareion 的「不要貼私人資訊」。
- **測試隔離**：測試只能對自己起的假 sidecar／假 angareion 說話，預設 sidecar 位址在測試中**必須被環境變數蓋掉**，否則會碰到真的 sidecar（MyAiStorage 既有 `tests/_guard.py` 的精神；具體做法列 tasks）。

## 待驗（本設計階段未實測；不呼叫 sidecar、不跑 opencode）

| # | 要驗的事 | 為什麼 |
|---|---|---|
| V1 | 阻塞在 `angareion wait` 的 tui，sidecar 看到的 `state` 是不是 `running`（I1） | 決定「不戳 running」會不會漏掉真的閒置的 agent |
| V2 | headless（`-p`／opencode）的 agent 被 poke 的實際行為；以及 `attach` 在 headless 是否一定為 null | 目前「只戳 tui」靠 `attach` 判斷（F7） |
| V3 | sidecar 是否記錄 poke 的 `text` 到 log | 影響 D2.5 的「不含內容」是否必要 |
| V4 | 啟動方能否給每個 agent 設 `AICOMM_CHANNEL`／`ANGAREION_IDENTITY`／`AICOMM_SIDECAR` 環境變數 | D1.4 的選項 B 是否可行 |
| V5 | claude／opencode 能否按 agent 指定 skills 目錄 | 選項 D3.1-B 是否可行 |
| V6 | 只靠 skill 的 `description` agent 是否可靠地用它 | D3.2 |
| V7 | `starting` 的 tui 被 poke 的實測時間 | J3 |
| V8 | `attach` 中有人在打字時被戳的後果（F10 只說 WARN） | 是否需要避開 |

## 沒把握

- `dispatch.py` 的 `TICKET_RE` 與 PMO 的職務規矩放哪（我只讀了 `send`／`reply`／`poke` 相關段落，F11、F12）。
- sidecar 的 headless poke 語意（V2）、log 內容（V3）。
- Mac 上 PMO／PM 目前用的 skill 安裝路徑（F13 只記 worker 映像的做法；Mac 沒查到，【未知】）。
- 本設計**沒有任何實測**：沒有呼叫 sidecar、沒有跑 opencode、沒有碰真 session。
