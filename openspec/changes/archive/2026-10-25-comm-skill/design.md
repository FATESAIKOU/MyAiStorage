## Context

見 `proposal.md`「Why」。工單：MyAiStorage#212（b 級，設計先審、本人同意才實作）。第 1～3 輪修正（回應非作者審查）與本人 2026-10-24 裁定後的定稿版本（J9 改為軟規則、拿掉 `nowake` 與設定檔、加 `--interrupt`）。

**標記**：【事實】附出處；【推論】是我（作者）從事實推的；【未知】沒查到或需要實測，列在最後一節「待驗」。

### 事實

| # | 事實 | 出處 |
|---|---|---|
| F1 | Angareion 對外五個指令；`send` 的 `--channel` 必填，`--to`／`--mention`／`--in-reply-to`／`--urgency`／`--ref`／`--attach`／`--body`；成功時 stdout 印訊息 id | `skills/angareion/SKILL.md`；`src/angareion/cli.py:447-466` |
| F2 | 收訊一律不加 `--channel`；「寄給我的」＝`to` 是我、廣播、或 `mentions` 有我；`--mentioned` 不含廣播、跨所有 channel、有自己的游標；`wait` 不動游標；`wait` 結束碼 0＝有新訊息、3＝逾時、2＝後端或設定錯 | `skills/angareion/SKILL.md`；`docs/angareion.md` 位址寫法與游標 |
| F3 | 自己的 AI 名來自設定檔 `identity`，`ANGAREION_IDENTITY` 優先 | `skills/angareion/SKILL.md` |
| F4 | `--ref NAME=LOCATOR` 產生 `{"name","ref"}` 附件；一則訊息上限 65536 字元 | `src/angareion/cli.py:184-188`；`docs/angareion.md` 訊息格式 |
| F5 | channel 名不是由 AI 名推得：現有 `team-mylinuxpool` 與 `team-mylinuxpool-w1` 並存、`secretary-pmo`、`team-life-w1` | `config/angareion/channels.json` |
| F6 | sidecar 對外四個動作（open／close／status／poke），HTTP 只綁 loopback、無認證；agent id 必須是合法 AI 名；一台機器一個 sidecar 管全部 agent | MyLinuxPool `modules/sidecar/docs/sidecar.md` §1、§2.2 |
| F7 | `GET /agents/{id}` 回 `state`（`starting`／`running`／`idle`／`waiting-quota`／`exited`）；不存在回 404 `agent_not_found` | 同上 §2.1、§2.2 |
| F8 | `POST /agents/{id}/poke` 的 `text` 必填、`interrupt` 預設 false；文件寫不打斷時「agent 正在跑就排到這一輪之後」（實測的行為見 V11：插進同一輪，在當下的 tool 呼叫之後處理）；`exited`／`waiting-quota` 回 409 `agent_not_running`；sidecar 關閉中回 503 | 同上 §2.2、§2.4 |
| F9 | tui 的狀態來自 hooks：第一則 `UserPromptSubmit`→`running`、`Stop`→`idle`；輸入沒被確認時 sidecar 只記 WARN，**不回錯，因為重送會變兩份**；sidecar 的狀態「最多晚一個 scheduler tick」 | 同上 §2.5、§2.2 status |
| F10 | `attach` 中的 agent 被 poke 照送並記 WARN | 同上 §2.5（Q2 裁定） |
| F11 | MyPMO `dispatch.py reply` ＝ `send_message`＋`poke_agent("channel 有新回覆，去讀。", interrupt=False)`；poke 失敗時通知本人並回 2（`--allow-poke-failure` 可吞）；`send` 是派工：驗工單號、讀工單、組樣板、送出、記 `received_at` 到 state 檔 | MyPMO `pmo/dispatch.py:2192-2205`、`:1915-1990` |
| F12 | MyPMO 對 sidecar 的 poke 逾時＝`poke_queue_timeout`＋10 秒餘裕，遇 409 `agent_not_running` 依 `state` 分類說明；不重試。**`poke_queue_timeout` 只用在 `starting` 的 poke 排隊**（sidecar 側：`starting` 時排隊到起來再送，逾時回 409 `poke_timeout`；`running`／`idle` 直接交給 driver），MyPMO 的餘裕是為了這一條 | `pmo/dispatch.py:1724-1760`；MyLinuxPool `modules/sidecar/docs/sidecar.md:281`、`:570`；`modules/sidecar/src/sidecar/core.py:1105-1124` |
| F13 | 現有 skill 的分發：`skills/angareion/SKILL.md` 由 MyLinuxPool 建映像時用 tool-pins 裡裝 `angareion` 的**同一個 sha** 取檔，放 `~/.claude/skills/angareion/`；opencode 自動讀 `~/.claude/skills/` | `docs/angareion.md`「說明檔（skill）」 |
| F14 | harness 是 `atelier/<series>/<role>/v<N>.md`、append-only；`v>=1` 的正文只寫能力、不寫工具名；未知能力名只警告（`KNOWN_CAPABILITIES` 在測試裡）；`v1` 起按 agent 交付：啟動方跑 `atelier render <id> [--append <環境說明>] --out <檔>`；環境說明放環境那一側、可寫工具名 | `docs/atelier/harness.md`；`docs/atelier/tool.md`「render」；`tests/unit/test_atelier_harness.py:75` |
| F15 | `pyproject.toml` 現有 console scripts：`agora`／`angareion`／`atelier` | `pyproject.toml:14-17` |
| F16 | M2 已定（D6）：PMO 的 `watch` 是腳本，模型只在被叫醒（30 分鐘）或被 poke 時才工作；**poke 與機械 ack 的對象收斂到本人**：只有 `secretary-pmo` 內、`from` 是秘書身分、`urgency ≥ 8` 的訊息 watch 才 poke PMO；理由：poke 花 Claude 額度，且 `urgency` 是寄件者自填、任何席位都能標 9 | `plans/m2-design-165.md:107`、`:112-114`（D6）、§5.3 |
| F17 | **headless 的 poke 是確定的**：opencode 的 `poke` ＝（必要時 interrupt）＋`send`，`send` 對 session 打 `POST /api/session/{id}/prompt`，並把狀態設成 running，等於開新的一輪；claude `-p` 的 `poke` 寫一則 user message 進 stdin 並把狀態設成 running | MyLinuxPool `modules/sidecar/src/sidecar/drivers/opencode.py:446-500`；`drivers/claude_code.py:585-605` |
| F18 | 工單號格式：`TICKET_RE = ^[A-Za-z0-9._-]+#[0-9]+$` | MyPMO `pmo/dispatch.py:124` |
| F19 | `dispatch.py` 找 sidecar：Mac team 用 `local_sidecar`，**worker team 用 `http://{sidecar_host}:{forward_port}`**（`sidecar_host` 預設 `127.0.0.1`，即轉發埠）；所以 `reply` 今天能戳到 worker 上的 PM | `pmo/dispatch.py:1044-1049`、`:506` |
| F20 | sidecar 的 `open` 沒有 env 欄位（有 `input` 等）；`open` 常帶第一則 `input`，starting 階段的第一則輸入可能被對話框吃掉 | MyLinuxPool `modules/sidecar/docs/sidecar.md:180`（`input`：「第一則輸入；排進佇列等 agent 起來再送」）、§2.2 open 的欄位表（沒有 env 欄位）；「starting 階段第一則輸入可能被對話框吃掉」是【推論】（來自 sidecar 文件 §2.5 的首開對話框描述） |
| F22 | **sidecar 開的子行程環境是白名單**：`PATH`、`HOME`、`LANG`、`TMPDIR`、`USER`、`LOGNAME`、`SHELL`、`TERM`、`TZ`，加前綴 `AGORA_`／`XDG_`／`LC_`／`FAKE_`，加 sidecar 維運變數與 resolve 的 overlay；sidecar 自己行程的其他環境變數**不會**進子行程 | 同上 `sidecar.md:619-626`（子行程環境的組法） |
| F21 | 隊員（Mac 與 worker）平常是 headless 的 opencode；PM 與 PMO 是 tui | 【推論】（sidecar 文件 §1 說 sidecar 控制 Claude Code 與 opencode；隊員用 headless 是環境慣例，我沒有直接出處） |

### 推論（不是事實）

- **I1**：tui／headless agent 在 `angareion wait` 阻塞時，從 sidecar 看是 `running`（它的 tool call 還沒結束）；這種 agent 不需要被戳，`wait` 自己每 15 秒會看到新訊息（F2）。反過來，**`idle` 的 agent 本輪已結束，不可能同時阻塞在 `inbox --wait`**，要開下一輪只有靠 poke（F17）。依據：F9、F17。**實測（V1，2026-10-24）：tui 卡在長時間前景 tool 呼叫時 `state` 是 `running`；以 `angareion wait` 本身阻塞的情形與 headless 未分別測，仍屬推論。**
- **I2（沒有去重；X 定案後改寫）**：`running` 也戳，所以**不做去重**：連送兩則會送出兩句固定短句。實測（V11，tui）：兩句是各自獨立的輸入、不合併，而且是**插進同一輪**，在當下的 tool 呼叫結束後一起處理，不是各開一輪；收件人在同一輪內多處理兩則輸入。不會壞資料、不會漏信。這是接受的已知代價，不另做鎖或合併；要少排就少送（一則訊息講完），或用 `--no-wake`。
- **I3**：MyPMO 的 Mac team 設定每 team 一個 `local_sidecar`（F19），埠不一定是預設值；但本人裁定（2026-10-24）sidecar 位址固定用 sidecar 文件的預設位址，**不做設定**。若某台機器的 sidecar 不在預設位址，aicomm 查不到＝叫醒失敗（D2.4，訊息照送達）；這是接受的限制。

## Goals / Non-Goals

**Goals**

- 回答 #212 列的四件事：介面（D1）、叫醒的判斷（D2）、Atelier 分發與版本（D3）、與 `dispatch.py` 的關係（D4）。
- 讓 PMO／PM／隊員「送一次、收一次」，不用記兩邊。
- 一份 SKILL.md、**所有角色同一份**，不依角色差異化（本人原則）；與 M2 D6 的 watch 規則並存（D2.9）。

**Non-Goals**

- 不改 Angareion（協定、指令、程式）；不改 sidecar（介面、程式）。
- 不做跨機叫醒（D2.6）；戳不帶訊息內容。
- 不取代 MyPMO 的派工流程與 worker team 的叫醒路徑（D4）。
- 不做身分認證、不做權限（Angareion 本來就「不做嚴格的身分認證」，`docs/angareion.md`）；D2.9 的軟規則是**省額度的約定**，不是安全邊界，也沒有硬控制。
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

依賴只往下：skill 依賴兩者**已公開**的介面；兩者各自的 spec 不因本 change 變動（見 proposal 的出處說明：MyLinuxPool#346 與 MyAiStorage#212）。

## D1 skill 的介面

### D1.1 指令集

| 選項 | 內容 | 代價 |
|---|---|---|
| **A（建議）** | 只有 `send` 與 `inbox` | 其他事（開 channel、開關 agent）繼續用各自的工具；一個 AI 要記的只有兩個動詞 |
| B | 再加 `ack`／`status`／`poke`／`channels` | 每加一個都是要被記住的介面；Angareion 明說「沒有 ack、沒有已讀回條」，加 `ack` 等於在 skill 層補它刻意不做的東西 |
| C | 只有 `send`（收信仍用 `angareion read`） | 收信的「不加 `--channel`」「要先 `wait`」仍要靠 AI 記得；`--channel` 誤用已經是 M0 的教訓（F2） |

**建議 A。** 理由：#212 要的是「省精力」，指令少且各只做一件事。skill 不提供「單獨的 poke」與「自訂文字」——叫醒是 `send` 的副作用、文字固定；要打斷，只能附在一則訊息上用 `send --interrupt`；「暫停」（打斷後送『停下來等指示』這類自訂文字）仍是 PMO 的 `dispatch.py poke` 的事（D4）。

**`send` 的旗標**（建議）：

```
aicomm send --channel <ch> [--to <AI 名>] [--mention <AI 名>]… [--in-reply-to <ULID>]
            [--urgency 0-9] [--ticket <repo>#<n>] [--no-wake | --interrupt] --body <文字|->
```

- `--channel` **必填**（D1.4）；`--to`／`--mention`／`--in-reply-to`／`--urgency`／`--body` 原樣轉給 Angareion，驗證留給 Angareion（送嚴的規則在那裡，F1）。
- `--attach`／`--ref` **不提供**（需要時用 `angareion send`）。理由：目標是一般溝通；`--ticket` 已用掉 `--ref` 的一個用途。作者提議 P1。
- `--body` 一定要給（與 Angareion 一致）；`-` 讀 stdin。
- `--interrupt`（預設不中斷）：戳時帶 `interrupt: true`（F8）。`--no-wake` 與 `--interrupt` 互斥（同時給 exit 1）。對方在跑時的行為見 D2.2。

**`inbox` 的旗標**（建議）：

```
aicomm inbox [--wait <秒>] [--mentioned] [--json]
```

- 不提供 `--channel`、`--since`：F2 的紀律直接由 skill 擋掉（作者提議 P4）。
- `--wait <秒>`＝先 `angareion wait --timeout <秒>`：exit 0 才 `read`；exit 3（逾時）以 3 結束且不 `read`；**exit 2（後端或網路錯）原樣以 2 結束、不 `read`**（不能當成「沒有新訊息」，`skills/angareion/SKILL.md`）。`--wait 0` ＝查一次。不給 `--wait`＝直接 `read`。
- `--mentioned`：**同時**轉給 `wait` 與 `read`（否則 `wait` 被廣播叫醒、`read` 卻讀不到，兩者游標與範圍不同，F2）。給「只想看被點名、不想被廣播吵」的席位（#209）；不給＝預設的「寄給我的」。

### D1.2 要不要帶工單號

| 選項 | 內容 | 代價 |
|---|---|---|
| **A（建議）** | 選填 `--ticket <repo>#<n>` → `--ref ticket=<repo>#<n>` | 工單號是結構化附件、內文不被改；`read --json` 看得到。不帶就是一般訊息 |
| B | 不提供，由 AI 自己寫在內文 | 沒有結構 |
| C | 必填（像 `dispatch.py send`） | 一般訊息（ack、問題）被迫編號；違反 #212「不帶也能送」 |

**建議 A。** 格式驗證用 `dispatch.py` 同一條 `TICKET_RE = ^[A-Za-z0-9._-]+#[0-9]+$`（F18）；skill **只驗格式、不讀工單**（讀工單需要 GitHub 權限，且是派工的事，D4）。

### D1.3 與 #209 的 `mentions`／`in_reply_to`／`--mentioned` 怎麼配合

| 項目 | 建議 |
|---|---|
| `--mention` | 轉給 Angareion；被 mention 的對象**與 `--to` 同等**走 D2.2 的叫醒判斷 |
| `--in-reply-to` | 原樣轉給 Angareion；**不影響叫醒**。skill 不從收到的信自動帶 |
| `inbox --mentioned` | 見 D1.1 |
| 廣播（無 `--to`） | **不叫醒任何人**（J5）。廣播會叫醒整台機器所有閒置 agent，花每個人的額度；要叫人就用 `--to` 或 `--mention` |
| 順序風險 | SKILL.md 沿用 Angareion 的「讀者先升級、再送 `--mention`」警告；首版不另做版本偵測（P7） |

### D1.4 channel 怎麼決定

`send` 要有 channel（F1），但 AI 名推不出 channel（F5）。

| 選項 | 內容 | 代價 |
|---|---|---|
| **A（建議）** | `--channel` 必填 | 最單純；派工信與 BRIEF 本來就寫了 channel 名，AI 不必另外去記；不需要任何環境注入 |
| B | 環境變數當預設 channel | sidecar 的 `open` 沒有 env 欄位（F20），子行程環境又是白名單（F22），要每個 agent 有不同的值只能改 sidecar 或它的範本（整台機器一份）；而且「某 agent 的 team channel 是哪個」是 #346 說的「團隊的定義」，不該進服務層 |
| C | 從 AI 名推 `team-<專案>-<team>` | F5 證明推不出 |
| D | 讀註冊簿挑收件人最近發過信的 channel | skill 自己做路由，膨脹 |

**建議 A**。不另設 channel 的環境變數（F22：sidecar 開的 agent 拿不到自訂的環境變數）。

## D2 叫醒的判斷

### D2.1 流程

```
send:
 1. 驗參數（--ticket 格式、--no-wake 與 --interrupt 互斥…）          失敗 → exit 1，不送
 2. angareion send …                                                  失敗 → 原結束碼，不叫醒
 3. 印訊息 id（stdout 第一行）
 4. 若 --no-wake 或 沒有 --to／--mention → 結束（exit 0）
 5. 對 ({to} ∪ {mentions}) − {自己}，各做（位址固定為 sidecar 文件的預設 loopback 位址）：
      GET /agents/{id}（逾時 3 秒）
        404 / 連不上 / 回應看不懂   → 記一行原因，不戳
        200，state == idle          → POST /agents/{id}/poke {text: 固定短句, interrupt: false}
        200，state == running       → POST /agents/{id}/poke {text: 固定短句, interrupt: false}（插進同一輪，在當下的 tool 呼叫之後處理，V11；tui 實測；headless、opencode 在 running 時未測）；--interrupt 時 interrupt: true（中斷正在跑的 tool 呼叫，V10；tui 實測，headless、opencode 未測）
        200，其他 state              → 記「<state>，略過」
 6. exit 0
```

### D2.2 怎麼知道「在同一台 sidecar、可戳」

| 條件 | 判斷方式 | 出處 |
|---|---|---|
| 同一台 sidecar | 對本機 sidecar 問 `GET /agents/{id}`：200＝在；404＝不在這台 | F6、F7；agent id 就是 AI 名（dispatch 用同一個字串當 `to` 與 `/agents/{id}`，F11） |
| 可戳的狀態 | `state` 是 `idle` 或 `running`；其餘（`starting`／`waiting-quota`／`exited`）不戳 | F7、F8、F9、F17 |

**不再區分 tui／headless**（J10 建議放寬）：F17 證明兩者的 poke 都會開新一輪。**因此也不依賴 `attach` 欄位**（`sidecar.md` 明說 attach「不是狀態」、值不得解析）。`starting`／`waiting-quota`／`exited` **不戳**；`starting` 尤其不戳：`open` 通常帶第一則 `input`，starting 階段再戳會變兩則、或被對話框吃掉，skill 分辨不出（F20）；起來之後的 agent 自己會照 input 去讀信。這同時拿掉了 `poke_queue_timeout` 的長等待（它只用在 `starting` 的 poke 排隊，`sidecar.md:281`、`:570`；`core.py:1105-1124` 只有 `starting` 分支會等）：戳只在 `idle` 與 `running` 兩種狀態送，兩者都立刻返回（tui：貼上後等 0.3 秒按 Enter 就返回，`running` 的輸入由 claude 輸入框排隊；opencode：`POST /api/session/{id}/prompt` 立即返回，`drivers/opencode.py:13-14`；以上是讀文件與程式，未實測）。skill 的 client 逾時照 D2.4：逾時＝「叫醒結果未知」、不重送。

**對方在 `running` 時怎麼辦（J2）**：**(a)，`running` 也送不打斷的 poke**。出處：PMO 2026-10-24 依本人字面判定（本人說「可以中斷也可以不中斷等對方讀」）；不是本人另外選的。

- 預設（不中斷）＝sidecar 的不中斷 poke（F8）：`idle` 馬上收到，開新的一輪（V9）；`running` 時**插進同一輪**，在當下的 tool 呼叫結束後處理（V11；tui 實測；headless、opencode 在 running 時未測）；`--interrupt` 才帶 `interrupt: true`，會中斷正在跑的 tool 呼叫，戳的文字成為新的輸入（V10；tui 實測，headless、opencode 未測）。
- 代價（照實寫，依實測修正；tui 實測；headless、opencode 在 running 時未測）：`running` 的收件人不會多開一輪，而是**這一輪多處理一則輸入**（阻塞在 `inbox --wait` 的 agent 本來自己就會醒，I1，多處理的那一則通常只是叫它去讀本來就要讀的信）；連送兩則是兩則獨立的輸入（I2，無去重、不合併）。**不會漏信。**
- 因此不再需要「每輪結束前跑一次 `aicomm inbox`」的軟規則（原退路 (b) 已拿掉）；軟規則只剩 D2.9 的三條。
- `starting`／`waiting-quota`／`exited` 仍不戳。

### D2.3 誰來做判斷

| 選項 | 內容 | 代價 |
|---|---|---|
| **A（建議）** | skill 查 sidecar（本設計） | 兩邊不互相依賴；代價是 skill 要知道 sidecar 的**公開** HTTP 形狀（F7、F8），sidecar 改它們時 skill 要跟著改（D2.7） |
| B | Angareion 的 `send` 加 `--wake` | Angareion 依賴 sidecar |
| C | sidecar 監看 Angareion 並自己叫醒 | sidecar 依賴 Angareion，且要持有 token |
| D | 另一個常駐 watcher | 多一個行程；MyPMO 的 `watch` 已是這個形狀（F16） |

### D2.4 叫醒失敗、sidecar 不在

**建議：訊息寫進 Angareion＝送達；叫醒是盡力而為。**

| 情形 | 行為 |
|---|---|
| Angareion 寫入失敗 | `send` 失敗（原結束碼）；不叫醒 |
| sidecar 連不上 | 寫入成功則 exit 0；stderr 一行「已送達，未叫醒：<原因>」 |
| 404（不在本機） | 正常；stderr 一行「不在本機」 |
| poke 409 `agent_not_running` | 不重試；stderr 記 state（F8、F12） |
| poke client 逾時 | **不重送**；stderr 一行「叫醒結果未知」。理由：F9 明說重送會變兩份 |
| poke 503 `shutting_down` | 不重試 |
| 其他非預期（HTTP 狀態、JSON 壞） | 視為叫醒失敗、不重試、不影響 exit 0 |

**對比 `dispatch.py reply`（F11）**：它 poke 失敗時 exit 2＋通知本人。skill **不**這樣做：skill 沒有「通知本人」的通道，叫醒失敗也不該讓 AI 以為訊息沒送出而重送。要保留 exit 2 語意的呼叫端（`reply`）自己讀 `--json` 或 stderr（P3；是 MyPMO 的決定，D4 步驟 4）。

**結束碼**：0＝訊息已寫進 Angareion（不論叫醒結果）；1＝用法錯；2＝Angareion 後端或設定錯；3 僅 `inbox --wait` 逾時。

### D2.5 戳的內容

固定短句，**不含寄件者、不含內容、不含工單**，例如「有新訊息：請執行 aicomm inbox。」（P8）。理由：(1) 戳的文字不進 sidecar 的 log（實測 V3），固定短句更不會洩漏內容；(2) 訊息內容是不可信輸入，經 Angareion→`inbox` 才進模型，戳是 skill 自己寫的字，兩條路徑分開；(3) 收件人讀了信才有完整上下文。

### D2.6 不能叫醒的 agent 怎麼收信

| 收件人 | 怎麼收 |
|---|---|
| 同台 sidecar、`idle`（tui 或 headless） | 被戳（馬上收到）；跑 `aicomm inbox` |
| 同台、`running`（含阻塞在 `inbox --wait` 的） | 也被戳（不打斷；tui 實測會插進同一輪、在當下的 tool 呼叫之後處理，headless、opencode 在 running 時未測，D2.2）；阻塞在 wait 的 agent 本來就會自己醒（I1），這一戳讓它這一輪多處理一則輸入 |
| 同台、`starting`／`waiting-quota`／`exited` | 不戳；`starting` 的起來後照 `input` 讀信；其他等被重開 |
| 別台機器的 agent | skill 不做跨機（Non-Goal）。收件人那台上的 agent 靠 `inbox --wait`；**worker team 的 PM 由 PMO 端的 `dispatch.py` 經轉發埠叫醒（F19，D4）**；對 PMO 則靠 M2 D6 的 watch（F16） |
| sidecar 沒在跑 | 同上 |
| 所有收件人的保底 | 訊息已在 Angareion，`wait` 與 `read` 一定讀得到——這就是「送達」的定義 |

**事實更正（相對初稿）**：初稿說 headless「閒著時 `inbox --wait` 阻塞等」。這是錯的：`idle` 代表本輪已結束，不在任何 tool 裡（I1）；閒置 headless 只有 poke 才會開下一輪，而 poke 在 headless 是確定的（F17）。所以 headless 納入叫醒（J10）。

### D2.7 對 sidecar 的依賴面與版本

skill 依賴 sidecar 的兩個公開端點與兩個欄位（`state` 的值 `idle`／`running`、404 語意）。**回應不符預期一律當叫醒失敗、不戳**。sidecar 若改這些形狀，應在 sidecar 的版本說明標示；skill 的 `--version` 印安裝的 sha，對照表放 `docs/aicomm.md`（P10）。這是無法避免的耦合：兩者之間的約定就是 sidecar 公開的 HTTP 形狀。

### D2.8 位址與安全

- **沒有設定檔、沒有使用者可設定的位址**（本人 2026-10-24 裁定）：sidecar 位址固定用 sidecar 文件的預設位址（loopback），不可設定。F22 仍成立：sidecar 開的 agent 的子行程環境是白名單，自訂的 `AICOMM_*` 本來也進不去。
- **測試用覆寫（作者提議 P5，不是本人要選的事）**：`AICOMM_SIDECAR`（單一 loopback 位址），**只給測試與本機手動用**，文件寫明 sidecar 開的 agent 拿不到它（F22），正式部署一律是預設位址。理由：測試只能對自己起的假 sidecar 說話，位址寫死的話測試無法接管；提議不增加使用者可見的設定面。可拒絕——拒絕的話，測試改在 `aicomm` 內部以函式參數注入位址，不經環境變數。
- 覆寫值的驗證：只收單一位址、只擋非 loopback 主機（`localhost`、`127.0.0.0/8`、`::1`）；需要叫醒但覆寫值非 loopback → exit 1，**在寫進 Angareion 之前**擋。理由：sidecar 只綁 loopback、無認證（F6）。**這不能防跨機**（worker 的轉發埠本身就是 loopback，F19），也不試圖分辨（P2）。沒有覆寫時預設位址一定通過。
- **需要叫醒的定義**：沒有 `--no-wake`，且（`--to` ∪ `--mention`）排除自己後**不是空的**。不需要叫醒（`--no-wake`、廣播、候選只剩自己）時不碰 sidecar。
- 逾時：查 3 秒、戳 5 秒（`idle` 的戳約 0.3 秒返回，F9）；不重試。
- 自己不戳自己（`--to` 或 `--mention` 等於自己的 AI 名，F3）。

### D2.9 誰可以叫醒誰（本人裁定 2026-10-24；J9）

**本人的原則**：這是「A2A 通訊」skill，用 angareion／sidecar 跟其他 AI 通訊，**所有角色用同一份、不依角色差異化**。sidecar 不認識職位，現在也不需要細粒度控制。

**定案：沒有硬控制，只有 SKILL.md 的軟規則。** 不做 `nowake` 名單、不做 aicomm 設定檔。軟規則三條（角色名稱只是文字，程式與 spec 不分角色）：

1. **預設不中斷**：`send` 預設不中斷對方；要中斷才明確加 `--interrupt`（D1.1；對方在跑時照樣戳，不打斷；tui 實測會插進同一輪，headless、opencode 在 running 時未測，D2.2）。
2. **隊員基本上不要中斷上位職（PM、PMO）**：不要對他們用 `--interrupt`。
3. **「收到」類回覆不叫醒對方**：沒有要對方動作的回覆（收到、確認）一律加 `--no-wake`。這同時是防迴圈：兩個 agent 互相回「收到」不會互相叫醒。不採「有 `--in-reply-to` 就預設不叫醒」：回報完成也是回覆，卻需要叫醒 PM，語意會錯。

**之後真的要硬控制**：走 sidecar `open` 時帶白名單（MyLinuxPool#346 的方向）；不在 aicomm 做。

**代價（本人 2026-10-24 接受）**：沒有硬控制，所以**任何同台 agent 都能叫醒 PMO**（PMO 在 Mac sidecar 是 `idle` 時，`aicomm send --to <PMO>` 會戳醒它，花一輪額度）；軟規則失守的後果是多一輪額度，不是壞資料。這與 M2 D6 的 watch 規則**並存、不衝突**：watch 管的是秘書派工的急件（只有 `secretary-pmo`＋秘書身分＋`urgency ≥ 8` 才 poke PMO、其餘等 30 分鐘，F16）；aicomm 管的是 AI 之間的一般溝通。兩者各管各的，M2 的規則不改。

## D3 經 Atelier 分發：位置、交付、版本

「經 Atelier 分發」有兩層意思：(甲) **執行檔與說明檔**怎麼到各 agent 手上；(乙) 告訴 agent「你該用它」的職務規矩（harness）怎麼寫。

### D3.1 檔放哪、怎麼交（J4）

| 選項 | 內容 | 代價 |
|---|---|---|
| **A（建議）** | 執行檔作為本 repo 同一個 distribution 的第四個 console script `aicomm`（F15），說明檔放 `skills/aicomm/SKILL.md`，**走 angareion skill 現有的路**（F13）：用 tool-pins 同一個 sha 裝指令、取說明檔、放 `~/.claude/skills/aicomm/`。**這條路本身不經過 Atelier**（Atelier 只管 harness 與 `render`；skill 檔的分發是 MyLinuxPool／Mac 安裝步驟的事） | 零新機制、版本與 `angareion` 同 sha。代價：和本人「透過 Atelier 分發」的字面意思不同；說明檔是使用者層級（所有角色都有），但這個 skill 對所有角色都適用，沒有「按 agent」需求 |
| B | Atelier 增加 artifact 種類：`atelier/skills/<name>/v<N>/…`（append-only、`show`／`render`），啟動方把它交進該 agent 的 skills 目錄 | 角色可拿不同版本、與 Atelier 紀律一致；但要改 `src/atelier/`、格式檢查、spec，且 per-agent skills 目錄的交付方式【未知】V5 |
| C | 把用法寫進 harness 本文／環境說明（`--append`） | 正文不能寫工具名（F14）；環境說明每環境一份、散；完整說明佔系統指示上下文 |
| D | 只靠各 repo 的 `AGENTS.md` | 一定漂移 |

**建議 A**；**本人裁定 A（J4）**；B 等角色別版本有需求時再做。

### D3.2 harness 怎麼提到它（乙層）

- harness 的 `capabilities` 加中性能力名 `comm`（F14：未知名只警告；`KNOWN_CAPABILITIES` 該補，列 tasks）。**正文只寫能力**，工具名 `aicomm` 與用法寫在**環境說明**（F14）。
- harness 是 append-only：要加 `comm` 就得出新版。**建議不為了這件事單獨出新版**（J6）：第一輪靠 skill 的 `description` 加環境說明；下一次 harness 本來要改版時順便加。若實測 skill 的 description 載入不可靠（V6），再單獨出版本。
- PMO 的職務規矩不在本 repo；PMO 怎麼被告知【未知】，列為跨 repo 的 tasks。

### D3.3 版本

| 層 | 怎麼管 |
|---|---|
| 指令＋說明檔 | **同一個 git sha**（與 `angareion` 一起被 tool-pins 釘）；說明檔不另寫版本號。`aicomm --version` 印安裝的 sha |
| 與 Angareion 的相容 | 同 distribution、同 sha，天然一致 |
| 與 sidecar 的相容 | 不同 repo、不同釘法；靠 D2.7：回應不符就不戳 |
| harness | 見 D3.2 |

**升級順序**：先升級所有讀者（裝新 sha），再讓任何人開始用 `--mention`（沿用 #209 的規矩）；`aicomm` 首版不帶新的訊息欄位。

## D4 與 MyPMO 的 `dispatch.py` 的關係（J7）

`dispatch.py` 有三個相關動作（F11）：

| `dispatch.py` | 做什麼 | 對 skill |
|---|---|---|
| `send <team> <repo#n>` | 驗工單號、讀工單、組派工樣板、送出、**把 `received_at` 寫進 state 供 `open --since` 與 watch 用**、trial 守門；此時 PM 可能還沒開起來 | 不取代 |
| `reply <team>` | `send_message`＋固定文字 poke，**經 `sidecar_base` 找 sidecar：Mac team 用 `local_sidecar`，worker team 用轉發埠（F19）** | **只對 Mac team 換成 `aicomm send`；worker team 保留現行**（`aicomm` 只問本機 sidecar，換過去 worker team 的 PM 就叫不醒，是退步） |
| `poke <team> [--interrupt]` | 自訂文字、可打斷 | 並存（sidecar 的「暫停＝poke＋interrupt」＝自訂文字的打斷，仍歸 `dispatch.py poke`；skill 只有附在訊息上的 `send --interrupt`，不提供單獨的 poke 與自訂文字） |

| 選項 | 內容 | 代價 |
|---|---|---|
| A | 全面取代 | `send` 的 state、樣板、trial 守門要搬進 skill——skill 變成 MyPMO 專用；單獨的 poke 與自訂文字（「暫停」）無法表達；worker team 退步 |
| B | 包一層：`dispatch.py send` 最後一步呼叫 `aicomm send` | 派工當下 PM 常常還沒開，叫醒無意義；多一層沒有好處 |
| **C（建議）** | **並存、按用途分**：派工＝`dispatch.py send`；日常溝通＝`aicomm`；`reply`：**Mac team 換成 `aicomm send`，worker team 保留現行**；`poke` 不變 | 兩套工具並存一段時間；界線：aicomm 是「一則訊息＋固定文字的戳（可附 `--interrupt`）」，`dispatch.py poke` 是「單獨的、自訂文字的戳」 |

**誰負責**：`reply` 的改動在 MyPMO，**由 PMO 負責**（它同時決定叫醒失敗要不要保留 exit 2 的語意）；worker team 叫醒的長期歸屬（是否讓 worker 上的 aicomm 自己叫醒同台 PM，不經 PMO）見 tasks 5.3，不在本 change。同一台 worker 上的隊員→PM（都在 worker 的 sidecar）則直接可用 aicomm，因為它們在同一台。

**遷移步驟**（跨 repo 的由 PMO 排）：

1. 本 change 實作完成、`aicomm` 隨 tool-pins 的下一個 sha 進映像與 Mac。
2. 待驗在拋棄式環境實測通過（本設計階段不碰 sidecar）。
3. PMO 與 PM 的環境說明加一行「溝通用 `aicomm`」；先只用在新的對話，派工流程不動。
4. `dispatch.py reply` 對 Mac team 改為呼叫 `aicomm send`（PMO 的小 PR；是否保留 exit 2 由 PMO 決定）；worker team 不動。
5. 觀察一個 PM 週期後，Mac 路徑的舊 `reply` 程式碼標 deprecated。`send`、`poke`、`watch`、`open` 保留。
6. PMO 的 watch 與 30 分鐘叫醒不拆（F16）。**它只涵蓋 PMO**，不是 PM／隊員的保底（D2.2）。

## 本人的裁定（2026-10-24）

經 PMO 轉達。本人原則：這是「A2A 通訊」skill，所有角色用同一份、不依角色差異化。

| # | 事項 | 裁定 |
|---|---|---|
| J1 | 介面範圍（只有 `send`／`inbox`） | 本人：照建議（A，D1.1） |
| J2 | 對方在 `running` 時 | **(a)：`running` 也送不打斷的 poke**（實測語意：插進同一輪，在當下的 tool 呼叫之後處理，不是排到這一輪之後；V11；tui 實測；headless、opencode 在 running 時未測）。出處：PMO 2026-10-24 依本人字面判定（本人的原話：可以中斷也可以不中斷等對方讀），不是本人另外選的。`--interrupt` 才中斷（本人已定） |
| J4 | 分發路徑 | 本人：照建議（A，沿用 angareion skill 的路徑，這條路不經 Atelier；D3.1） |
| J5 | 廣播不叫醒 | 本人：照建議（A，D1.3） |
| J6 | harness 第一輪不改 | 本人：照建議（A，D3.2） |
| J7 | 與 `dispatch.py` 並存，`reply` 只換 Mac team | 本人：照建議（C，D4） |
| J9 | 誰可以叫醒誰 | **本人定案**：不要 `nowake` 名單和 `~/.config/aicomm` 設定；改成 SKILL.md 軟規則（預設不中斷／隊員基本上不要中斷 PM、PMO／「收到」類回覆 `--no-wake`）；要硬控制之後走 sidecar open 帶白名單（MyLinuxPool#346）。代價（任何同台 agent 都能叫醒 PMO，與 M2 D6 並存）本人接受（D2.9） |
| J10 | 閒置的 headless 也叫醒 | 本人：照建議（A，條件為 `state` 是 `idle` 或 `running`〔J2〕，不分 tui／headless） |

（J3 已刪；J8 名字降為作者提議 P11。）

## 作者提議、本人未指示

| # | 我加的東西 | 理由 | 可拒絕 |
|---|---|---|---|
| P1 | 不提供 `--attach`／`--ref` | 保持介面小 | 可改為轉傳 |
| P2 | 測試覆寫位址只擋非 loopback，**只在要叫醒時檢查**；不宣稱能防跨機 | sidecar 只綁 loopback、無認證；轉發埠也是 loopback，檢查分辨不出 | 可拿掉 |
| P3 | `send --json`（stdout 印 `{"id","wake":[…]}`）給程式讀叫醒結果 | `reply` 的 exit 2 語意需要機器可讀的結果（PMO 決定，D4） | 可不做 |
| P4 | `inbox` 不提供 `--channel`／`--since` | 擋 M0 的誤用 | 可放開 |
| P5 | 測試用的位址覆寫 `AICOMM_SIDECAR`（單一 loopback 位址；測試／手動用，正式部署用 sidecar 預設位址） | D2.8；測試要接管位址；F22 讓環境變數進不了 sidecar 開的 agent | 可拒絕，改以內部函式參數注入 |
| P6 | 自己不戳自己 | 避免備忘戳醒自己 | 可拿掉 |
| P7 | 首版不做 `--mention` 的版本偵測 | 同 sha 一起裝 | 可加 |
| P8 | 戳的文字固定、不含寄件者與內容 | D2.5 | 可改 |
| P9 | harness 之後加 `comm` 能力名 | D3.2 | 可不加 |
| P10 | 新建 `docs/aicomm.md` 記 sidecar 欄位對照 | D2.7 | 可併入 SKILL.md |
| P11 | 名字暫用 `aicomm`，不用 `comm`（與 POSIX `comm` 撞名） | 本人不必選；PM／PMO 可改 | 可改名 |
| P12 | SKILL.md 的軟規則：D2.9 的三條（預設不中斷／隊員基本上不要中斷 PM、PMO／「收到」類回覆 `--no-wake`）；J2 定為 (a)，不再有「每輪結束前 `inbox`」這條 | D2.2、D2.9；本人已定 D2.9 三條，此列只記文字落在 SKILL.md | 之後要硬控制走 sidecar open 白名單 |
| P13 | `--interrupt` 對 `running` 的對象送 `interrupt: true`（`idle` 的戳本來就不打斷）；`--no-wake` 與 `--interrupt` 互斥 | `--interrupt` 是明確表態；互斥避免矛盾 | 可改 |

## 風險與取捨

- **耦合 sidecar 的公開 HTTP 形狀**：不可免（D2.7）；降低影響的方式是「看不懂就不戳」。
- **sidecar 側的前提：tui 的 workdir 要先 trust 過**（實測，2026-10-24；MyLinuxPool#374）。沒 trust 過時，agent 一直卡在 `starting`；推測是 trust 對話框吃掉第一則 input（與實測一致：第 1 次畫面上沒看到對話框；第 2 次 trust 過就正常；見 MyLinuxPool#374）。這是 sidecar／環境那一側的前提（環境說明指定的專屬 worktree 要事先 trust 過），aicomm 不處理；aicomm 本來就不戳 `starting`（D2.2）。
- **提示注入**：戳的文字由 skill 固定、不轉述內容（D2.5）；`inbox` 的輸出仍進模型，與 `angareion read` 相同，沒有惡化。
- **額度**：每次成功戳＝一次模型輪次。沒有去重（I2）：連送會排多句；廣播不戳；軟規則讓「收到」類回覆不戳（D2.9）。**沒有硬控制：任何同台 agent 都能叫醒 PMO，本人已接受（D2.9）。**
- **軟規則**：D2.9 的三條規則靠 AI 遵守 SKILL.md；失守的後果是多一輪額度（D2.9），不是壞資料。
- **私人資訊**：戳與 stderr 都不含內文；說明檔沿用 Angareion 的「不要貼私人資訊」。
- **測試隔離**：測試只能對自己起的假 sidecar／假 angareion 說話，測試**一律設 `AICOMM_SIDECAR` 指到自己起的假 sidecar；沒設就讓測試失敗（guard）**，免得碰到真的 sidecar（若 P5 被拒絕，改為注入位址的函式參數，guard 同理）（`tests/_guard.py` 的精神；具體做法列 tasks）。

## 待驗與實測結果

出處：#212 的實測結果留言（2026-10-24，https://github.com/FATESAIKOU/MyAiStorage/issues/212#issuecomment-6388726800；拋棄式環境，自己起的測試 sidecar、假 agora、假 angareion，沒碰真 session）。第 1 次實測因 tui 卡在 `starting` 停損，原因見最後一列。

| # | 結果 | 實測（2026-10-24）與證據 |
|---|---|---|
| V1 | **已測（tui）** | tui 卡在長時間前景 tool 呼叫時 `state` 是 `running`（三次取樣都是）。備註：Claude Code 會擋前景的 `sleep`（agent 沒執行就回到 `idle`），所以實測改用長時間的 `ping`；不影響設計。未分別測 `angareion wait` 本身阻塞與 headless |
| V11 | **已測（tui）** | `running` 時連續兩次不中斷的戳：畫面上是兩行獨立的輸入，**不合併**；兩則**插進同一輪**，在當下的 tool 呼叫結束後一起處理，不是各開一輪。aicomm 兩次都成功返回 |
| V10 | **已測（tui）** | `interrupt: true` 對 `running` 的 tui **會中斷**正在跑的 tool 呼叫（畫面顯示被打斷），戳的文字成為新的輸入，約 3 秒後回到 `idle`；sidecar log 記 `interrupt via poke; no Stop event - setting idle`。headless 未測 |
| V3 | **已測** | 戳的文字**不進** sidecar 的 log：log 只有 `POST /agents/<id>/poke` 這一行，grep 戳的文字 0 筆 |
| V9 | **已測（headless claude-code）** | 閒著的 headless 收到不中斷的戳會**開新的一輪**：`idle`→`running`（1 秒內）→`idle`（約 4 秒）。回覆文字沒有落地可看，只有 `state` 的變化。opencode 未測 |
| V5 | 未測 | 這次實測沒有涵蓋；D3.1 採 A（不經 Atelier），選 B 才需要 |
| V6 | 未測 | 這次實測沒有涵蓋；實作後在 6.1 端到端一併看 |
| V8 | 未測 | 這次實測沒有涵蓋（沒有人 attach 打字的情境） |
| 附 | 第一則 input | 第 1 次實測 tui 卡在 `starting`：workdir 沒 trust 過；推測是 trust 對話框吃掉第一則 input，與實測一致（第 1 次畫面上沒看到對話框；第 2 次 trust 過就正常；見 MyLinuxPool#374）。見「風險與取捨」 |

（V2 已由 F17 回答；V4 已由 F20 回答；V7 隨 J3 刪除。）

## 沒把握

- PMO 的職務規矩放哪、Mac 上 PMO／PM 目前的 skill 安裝路徑（F13 只記 worker 映像的做法）。
- 我沒讀 `dispatch.py` 的 `cmd_watch` 內文；「PM／隊員沒有定期叫醒」依據是 M2 文件與本設計都沒寫（推論）。
- 實測只涵蓋 V1、V3、V9、V10、V11，且 tui 與 headless 只各測了其中幾項（見上表）；opencode 沒測；V5、V6、V8 未測。沒有碰真 session。
