## Context

見 `proposal.md`「Why」。工單：MyAiStorage#212（b 級，設計先審、本人同意才實作）。第 1 輪修正（回應非作者審查）與第 2 輪修正後的版本。

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
| F8 | `POST /agents/{id}/poke` 的 `text` 必填、`interrupt` 預設 false；不打斷時「agent 正在跑就排到這一輪之後」；`exited`／`waiting-quota` 回 409 `agent_not_running`；sidecar 關閉中回 503 | 同上 §2.2、§2.4 |
| F9 | tui 的狀態來自 hooks：第一則 `UserPromptSubmit`→`running`、`Stop`→`idle`；輸入沒被確認時 sidecar 只記 WARN，**不回錯，因為重送會變兩份**；sidecar 的狀態「最多晚一個 scheduler tick」 | 同上 §2.5、§2.2 status |
| F10 | `attach` 中的 agent 被 poke 照送並記 WARN | 同上 §2.5（Q2 裁定） |
| F11 | MyPMO `dispatch.py reply` ＝ `send_message`＋`poke_agent("channel 有新回覆，去讀。", interrupt=False)`；poke 失敗時通知本人並回 2（`--allow-poke-failure` 可吞）；`send` 是派工：驗工單號、讀工單、組樣板、送出、記 `received_at` 到 state 檔 | MyPMO `pmo/dispatch.py:2192-2205`、`:1915-1990` |
| F12 | MyPMO 對 sidecar 的 poke 逾時＝`poke_queue_timeout`＋10 秒餘裕，遇 409 `agent_not_running` 依 `state` 分類說明；不重試 | `pmo/dispatch.py:1724-1760` |
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

- **I1**：tui／headless agent 在 `angareion wait` 阻塞時，從 sidecar 看是 `running`（它的 tool call 還沒結束）；這種 agent 不需要被戳，`wait` 自己每 15 秒會看到新訊息（F2）。反過來，**`idle` 的 agent 本輪已結束，不可能同時阻塞在 `inbox --wait`**，要開下一輪只有靠 poke（F17）。依據：F9、F17。**要實測。**
- **I2（盡力而為的去重）**：同一個寄件人連送，第一次戳讓 `idle`→`running`，後面的 `send` 看到 `running` 就不再戳。**前提**：sidecar 的狀態已更新（F9：最多晚一個 tick）。**代價**：兩個寄件人同時送、或同一個人連送落在那個空檔裡，各自都看到 `idle` 就各戳一次，收件人多一輪去讀（可能是空的）收件匣；不會壞資料。這是接受的已知代價，不另做鎖。
- **I3**：Mac 上 sidecar 的埠不一定是預設值（MyPMO 的 team 設定每 team 一個 `local_sidecar`，F19），所以位址要可設定。

## Goals / Non-Goals

**Goals**

- 回答 #212 列的四件事：介面（D1）、叫醒的判斷（D2）、Atelier 分發與版本（D3）、與 `dispatch.py` 的關係（D4）。
- 讓 PMO／PM／隊員「送一次、收一次」，不用記兩邊。
- 與 M2 已定案的「誰可以叫醒 PMO」一致（D2.9）。

**Non-Goals**

- 不改 Angareion（協定、指令、程式）；不改 sidecar（介面、程式）。
- 不做跨機叫醒（D2.6）；戳不帶訊息內容。
- 不取代 MyPMO 的派工流程與 worker team 的叫醒路徑（D4）。
- 不做身分認證、不做權限（Angareion 本來就「不做嚴格的身分認證」，`docs/angareion.md`）；D2.9 的限制是**省額度的政策**，不是安全邊界。
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

**建議 A。** 理由：#212 要的是「省精力」，指令少且各只做一件事。`poke` 不單獨暴露——叫醒是 `send` 的副作用；要打斷、要自訂文字的戳是 PMO 的 `dispatch.py poke` 的事（D4）。

**`send` 的旗標**（建議）：

```
aicomm send --channel <ch> [--to <AI 名>] [--mention <AI 名>]… [--in-reply-to <ULID>]
            [--urgency 0-9] [--ticket <repo>#<n>] [--no-wake] --body <文字|->
```

- `--channel` **必填**（D1.4）；`--to`／`--mention`／`--in-reply-to`／`--urgency`／`--body` 原樣轉給 Angareion，驗證留給 Angareion（送嚴的規則在那裡，F1）。
- `--attach`／`--ref` **不提供**（需要時用 `angareion send`）。理由：目標是一般溝通；`--ticket` 已用掉 `--ref` 的一個用途。作者提議 P1。
- `--body` 一定要給（與 Angareion 一致）；`-` 讀 stdin。

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
| `--mention` | 轉給 Angareion；被 mention 的對象**與 `--to` 同等**走 D2.2 的叫醒判斷與 D2.9 的限制 |
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
 1. 驗參數（--ticket 格式、--no-wake…）；要叫醒時才檢查 sidecar 位址  失敗 → exit 1，不送
 2. angareion send …                                                  失敗 → 原結束碼，不叫醒
 3. 印訊息 id（stdout 第一行）
 4. 若 --no-wake 或 沒有 --to／--mention → 結束（exit 0）
 5. 對 ({to} ∪ {mentions}) − {自己} − {D2.9 的不叫醒名單}，各做：
      GET /agents/{id}（逾時 3 秒）
        404 / 連不上 / 回應看不懂   → 記一行原因，不戳
        200，state == idle          → POST /agents/{id}/poke {text: 固定短句, interrupt: false}
        200，其他 state              → 記「<state>，略過」
 6. exit 0
```

### D2.2 怎麼知道「在同一台 sidecar、閒置」

| 條件 | 判斷方式 | 出處 |
|---|---|---|
| 同一台 sidecar | 對本機 sidecar 問 `GET /agents/{id}`：200＝在；404＝不在這台 | F6、F7；agent id 就是 AI 名（dispatch 用同一個字串當 `to` 與 `/agents/{id}`，F11） |
| 閒置 | `state == idle` | F7、F9、F17 |

**不再區分 tui／headless**（J10 建議放寬）：F17 證明兩者的 poke 都會開新一輪。**因此也不依賴 `attach` 欄位**（`sidecar.md` 明說 attach「不是狀態」、值不得解析）。`starting` **不戳**：`open` 通常帶第一則 `input`，starting 階段再戳會變兩則、或被對話框吃掉，skill 分辨不出（F20）；起來之後的 agent 自己會照 input 去讀信。這同時拿掉了 `poke_queue_timeout` 的長等待：戳只剩 `idle` 這條路，client 逾時幾秒即可。

**不戳 `running` 的代價（J2，照實寫）**：`running` 的 agent 要等它自己下一次 `inbox`／`wait` 才讀到。**沒有任何保底的定期叫醒給 PM 或隊員**：M2 的 30 分鐘叫醒只給 PMO（F16），本設計與 M2 都沒有說 PM／隊員會被定期叫醒（【推論】，我沒讀 `watch` 內文）。審查列的退路有三：(a) 對 `running` 也送不打斷的 poke（排到這一輪之後）；(b) SKILL.md 規定「每一輪結束前跑一次 `aicomm inbox`，之後才閒下來」；(c) 本人接受漏收。**建議 (b)**：成本是零行程式、不多花額度，且 I1 的「`running` 的 agent 多半在忙或在 wait」讓漏收只剩「一輪做很久且完全不看信」；(a) 會讓阻塞在 wait 的 agent 多一輪、也放大重複戳；(c) 是 (b) 失效時的實情，仍須告知本人。**這是軟規則**（靠 AI 遵守 SKILL.md），不是機制。

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

固定短句，**不含寄件者、不含內容、不含工單**，例如「有新訊息：請執行 aicomm inbox。」（P8）。理由：(1) 戳的文字是否進 sidecar 的 log 【未知】V3，不含內容就不必管；(2) 訊息內容是不可信輸入，經 Angareion→`inbox` 才進模型，戳是 skill 自己寫的字，兩條路徑分開；(3) 收件人讀了信才有完整上下文。

### D2.6 不能叫醒的 agent 怎麼收信

| 收件人 | 怎麼收 |
|---|---|
| 同台 sidecar、`idle`（tui 或 headless） | 被戳；跑 `aicomm inbox` |
| 同台、`running`（含阻塞在 `inbox --wait` 的） | 阻塞在 wait 的自己會醒（I1）；在忙的，靠 SKILL.md「每輪結束前 `inbox`」(D2.2) |
| 同台、`starting`／`waiting-quota`／`exited` | 不戳；`starting` 的起來後照 `input` 讀信；其他等被重開 |
| 別台機器的 agent | skill 不做跨機（Non-Goal）。收件人那台上的 agent 靠 `inbox --wait`；**worker team 的 PM 由 PMO 端的 `dispatch.py` 經轉發埠叫醒（F19，D4）**；對 PMO 則是 D2.9 |
| sidecar 沒在跑 | 同上 |
| 所有收件人的保底 | 訊息已在 Angareion，`wait` 與 `read` 一定讀得到——這就是「送達」的定義 |

**事實更正（相對初稿）**：初稿說 headless「閒著時 `inbox --wait` 阻塞等」。這是錯的：`idle` 代表本輪已結束，不在任何 tool 裡（I1）；閒置 headless 只有 poke 才會開下一輪，而 poke 在 headless 是確定的（F17）。所以 headless 納入叫醒（J10）。

### D2.7 對 sidecar 的依賴面與版本

skill 依賴 sidecar 的兩個公開端點與兩個欄位（`state` 的值 `idle`、404 語意）。**回應不符預期一律當叫醒失敗、不戳**。sidecar 若改這些形狀，應在 sidecar 的版本說明標示；skill 的 `--version` 印安裝的 sha，對照表放 `docs/aicomm.md`（P10）。這是無法避免的耦合：兩者之間的約定就是 sidecar 公開的 HTTP 形狀。

### D2.8 位址與安全

- **設定來源只有 aicomm 自己的設定檔，不用環境變數**（F22：sidecar 開的 agent 的子行程環境是白名單，自訂的 `AICOMM_*` 進不去；而 `HOME`／`XDG_*` 在白名單內，所以設定檔找得到）。一台機器一份：`$XDG_CONFIG_HOME/aicomm/config.json`，沒設 `XDG_CONFIG_HOME` 就 `$HOME/.config/aicomm/config.json`。欄位：`sidecar`（單一 loopback 位址，可省略＝sidecar 文件的預設位址）、`nowake`（AI 名清單，可省略＝空）。
- **邊界驗證**：讀檔時就驗——JSON 壞、欄位型別錯、`nowake` 裡有不合法的 AI 名、`sidecar` 不是單一位址 → 設定壞。檔案不存在＝預設位址＋空名單（正常）。
- **設定壞的處理**：「可能要叫醒」（有 `--to` 或 `--mention`、且沒有 `--no-wake`）時 → exit 1，**在寫進 Angareion 之前**，訊息說明哪個欄位壞。`--no-wake` 或廣播不讀設定檔、不受影響。
- **路徑覆寫只留一個**：`AICOMM_CONFIG`（指到設定檔），給測試與本機手動用。**文件要寫明：sidecar 開的 agent 拿不到它**（F22），所以正式部署只能靠上面的預設路徑。
- sidecar 位址**只收一個**（一台機器一個 sidecar，F6；多位址也是跨機的入口）。
- **只擋非 loopback 主機**（`localhost`、`127.0.0.0/8`、`::1`）。理由：sidecar 只綁 loopback、無認證（F6），指向別的主機的位址一定是設錯。**這不能防跨機**：worker 的轉發埠本身就是 loopback（F19），設定檔的 `sidecar` 指到轉發埠會通過檢查——這是使用者自己的明確選擇，檢查分辨不出，也不試圖分辨（P2）。
- **有意的嚴格選擇**：位址只在「**需要叫醒**」時才驗與使用。**定義**：沒有 `--no-wake`，且（`--to` ∪ `--mention`）排除自己與 `nowake` 名單後**不是空的**。不需要叫醒（`--no-wake`、廣播、候選全被排除）時不碰 sidecar，位址設錯不影響送信；需要叫醒但位址非 loopback → exit 1，**在寫進 Angareion 之前**就擋，避免「半個動作」。
- 逾時：查 3 秒、戳 5 秒（`idle` 的戳約 0.3 秒返回，F9）；不重試。
- 自己不戳自己（`--to` 或 `--mention` 等於自己的 AI 名，F3）。

### D2.9 誰可以叫醒誰（與 M2 對齊；J9）

**衝突**：PMO 在 Mac sidecar 上是 `idle` 時，照 D2.2 任何席位 `aicomm send --to` 它都會戳醒它；而 M2 已定案「poke 的對象收斂到本人」，因為 poke 花額度、`urgency` 是自填（F16）。另外兩個 agent 互相回「收到」會互相叫醒，每次一輪模型（I2 的去重只在對方還是 `running` 時有效）。

| 選項 | 內容 | 代價 |
|---|---|---|
| **a（建議）** | **收件人那側的「不叫醒名單」**：aicomm 自己的設定檔的 `nowake`（AI 名清單，D2.8），名單上的收件人**不論誰寄、都不戳**。**名單的語意＝「收件人所在那台機器」的設定**，也就是會戳它的寄件人讀到的那份（寄件人和收件人在同一台才會戳，D2.2）；PMO 的 AI 名放在 Mac 的設定檔裡。PMO 因此只被 M2 的規則（`watch` 的 poke／30 分鐘叫醒）叫醒，與 M2 §5.3 一致 | 要有人維護名單（一行，Mac 的安裝步驟放）；名單是政策不是安全邊界（F16/Non-Goal：任何人仍可自己 `dispatch.py poke`）。名單不能走 sidecar 注入（F20、F22），所以放 aicomm 自己的設定檔 |
| b | 只有關係對的才叫醒（PMO→PM、PM→自己 team 的隊員、隊員→自己的 PM） | AI 名文法推不出「誰是 PMO」「誰的 PM」（team 段 `m1` 在不同專案重複）；要另做關係表，等於把「團隊的定義」搬進 skill |
| c | 照 D2.2（不限制） | 與 M2 D6 衝突；額度與迴圈風險 |

**建議 a**，另加**防迴圈**（選一種，寫死）：**SKILL.md 規定「收到／確認這類沒有要對方動作的回覆一律加 `--no-wake`」**（軟規則）。不採「有 `--in-reply-to` 就預設不叫醒」：回報完成也是回覆，卻需要叫醒 PM，語意會錯。迴圈的另一道天然限制是 D2.2：被叫醒的對方在讀信那一輪是 `running`，只有它回完又回到 `idle` 才可能被再戳，且 `--no-wake` 規則讓「收到」不戳。

**PMO 那邊與 M2 的 watch 規則怎麼一致**：PMO 在 Mac 設定檔的 `nowake` 裡 → 任何 `aicomm send --to <PMO>` 都只寫進 Angareion、不戳；PMO 仍會被 M2 `watch` 在 `secretary-pmo`＋秘書身分＋`urgency ≥ 8` 時 poke，其餘等 30 分鐘叫醒，與 M2 完全相同。PMO 發出去的信（PMO→PM）照 D2.2 戳 PM。**代價**：PM→PMO 的訊息最慢等 30 分鐘才被 PMO 看到（與現況一致，沒有退步）。

## D3 經 Atelier 分發：位置、交付、版本

「經 Atelier 分發」有兩層意思：(甲) **執行檔與說明檔**怎麼到各 agent 手上；(乙) 告訴 agent「你該用它」的職務規矩（harness）怎麼寫。

### D3.1 檔放哪、怎麼交（J4）

| 選項 | 內容 | 代價 |
|---|---|---|
| **A（建議）** | 執行檔作為本 repo 同一個 distribution 的第四個 console script `aicomm`（F15），說明檔放 `skills/aicomm/SKILL.md`，**走 angareion skill 現有的路**（F13）：用 tool-pins 同一個 sha 裝指令、取說明檔、放 `~/.claude/skills/aicomm/`。**這條路本身不經過 Atelier**（Atelier 只管 harness 與 `render`；skill 檔的分發是 MyLinuxPool／Mac 安裝步驟的事） | 零新機制、版本與 `angareion` 同 sha。代價：和本人「透過 Atelier 分發」的字面意思不同；說明檔是使用者層級（所有角色都有），但這個 skill 對所有角色都適用，沒有「按 agent」需求 |
| B | Atelier 增加 artifact 種類：`atelier/skills/<name>/v<N>/…`（append-only、`show`／`render`），啟動方把它交進該 agent 的 skills 目錄 | 角色可拿不同版本、與 Atelier 紀律一致；但要改 `src/atelier/`、格式檢查、spec，且 per-agent skills 目錄的交付方式【未知】V5 |
| C | 把用法寫進 harness 本文／環境說明（`--append`） | 正文不能寫工具名（F14）；環境說明每環境一份、散；完整說明佔系統指示上下文 |
| D | 只靠各 repo 的 `AGENTS.md` | 一定漂移 |

**建議 A**；**判斷點 J4 請本人確認「經 Atelier」是否要字面上走 B**。

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
| `poke <team> [--interrupt]` | 自訂文字、可打斷 | 並存（sidecar 的「暫停＝poke＋interrupt」；skill 刻意不暴露） |

| 選項 | 內容 | 代價 |
|---|---|---|
| A | 全面取代 | `send` 的 state、樣板、trial 守門要搬進 skill——skill 變成 MyPMO 專用；`poke --interrupt` 無法表達；worker team 退步 |
| B | 包一層：`dispatch.py send` 最後一步呼叫 `aicomm send` | 派工當下 PM 常常還沒開，叫醒無意義；多一層沒有好處 |
| **C（建議）** | **並存、按用途分**：派工＝`dispatch.py send`；日常溝通＝`aicomm`；`reply`：**Mac team 換成 `aicomm send`，worker team 保留現行**；`poke` 不變 | 兩套工具並存一段時間；界線清楚 |

**誰負責**：`reply` 的改動在 MyPMO，**由 PMO 負責**（它同時決定叫醒失敗要不要保留 exit 2 的語意）；worker team 叫醒的長期歸屬（是否讓 worker 上的 aicomm 自己叫醒同台 PM，不經 PMO）見 tasks 5.3，不在本 change。同一台 worker 上的隊員→PM（都在 worker 的 sidecar）則直接可用 aicomm，因為它們在同一台。

**遷移步驟**（跨 repo 的由 PMO 排）：

1. 本 change 實作完成、`aicomm` 隨 tool-pins 的下一個 sha 進映像與 Mac。
2. 待驗在拋棄式環境實測通過（本設計階段不碰 sidecar）。
3. PMO 與 PM 的環境說明加一行「溝通用 `aicomm`」；Mac 安裝步驟放 aicomm 設定檔（`nowake` 放 PMO 的 AI 名）；先只用在新的對話，派工流程不動。
4. `dispatch.py reply` 對 Mac team 改為呼叫 `aicomm send`（PMO 的小 PR；是否保留 exit 2 由 PMO 決定）；worker team 不動。
5. 觀察一個 PM 週期後，Mac 路徑的舊 `reply` 程式碼標 deprecated。`send`、`poke`、`watch`、`open` 保留。
6. PMO 的 watch 與 30 分鐘叫醒不拆（F16）。**它只涵蓋 PMO**，不是 PM／隊員的保底（D2.2）。

## 本人的判斷點

| # | 要本人選的事 | 選項 | 作者建議 |
|---|---|---|---|
| J1 | 介面的範圍 | A 只有 `send`／`inbox`；B 再加 ack 等；C 只有 `send` | **A**（D1.1） |
| J2 | `running` 的 agent 要不要戳（代價：PM／隊員沒有定期叫醒的保底） | (a) 不打斷地戳；(b) 不戳＋SKILL.md 規定「每輪結束前 `inbox`」；(c) 不戳、接受漏收 | **(b)**（D2.2） |
| J4 | 「經 Atelier 分發」的意思 | A 沿用 angareion skill 的分發路徑（**這條路不經 Atelier**）；B Atelier 新增 skill artifact；C 寫進環境說明 | **A**（D3.1）；B 等角色別版本有需求時再做 |
| J5 | 廣播要不要叫醒 | A 不叫醒；B 叫醒所有同台閒置 agent | **A**（D1.3） |
| J6 | 要不要第一輪就改 harness（`comm` 能力名） | A 不改，等下次改版；B 單獨出新版 | **A**（D3.2） |
| J7 | 與 `dispatch.py` 的關係 | A 全取代；B 派工包一層；C 並存、`reply` 只換 Mac team | **C**（D4） |
| J9 | **誰可以叫醒誰**（與 M2 D6／§5.3 對齊） | a 收件人側「不叫醒名單」（aicomm 設定檔的 `nowake`，語意＝收件人所在那台機器的設定；PMO 放進去）；b 只有上下游才叫醒；c 不限制 | **a**＋SKILL.md「收到類回覆加 `--no-wake`」（D2.9） |
| J10 | **要不要叫醒閒置的 headless**（#212 標題寫 tui；條件放寬成 `state == idle`） | A 放寬（不分 tui／headless）；B 只戳 tui | **A**。隊員平常是 headless（F21），B 的話 PM→隊員這條主路徑永遠不會叫醒，做不到「不用記兩邊」；F17 證明 headless 的 poke 語意確定 |

（J3「戳 `starting` 要不要等」已刪：只戳 `idle`。J8 名字降為作者提議 P11。）

## 作者提議、本人未指示

| # | 我加的東西 | 理由 | 可拒絕 |
|---|---|---|---|
| P1 | 不提供 `--attach`／`--ref` | 保持介面小 | 可改為轉傳 |
| P2 | 只擋非 loopback，**只在要叫醒時檢查**；不宣稱能防跨機 | sidecar 只綁 loopback、無認證；轉發埠也是 loopback，檢查分辨不出 | 可拿掉 |
| P3 | `send --json`（stdout 印 `{"id","wake":[…]}`）給程式讀叫醒結果 | `reply` 的 exit 2 語意需要機器可讀的結果（PMO 決定，D4） | 可不做 |
| P4 | `inbox` 不提供 `--channel`／`--since` | 擋 M0 的誤用 | 可放開 |
| P5 | aicomm 自己的設定檔（`config.json`：`sidecar` 單一位址、`nowake` 名單）；唯一的環境變數是路徑覆寫 `AICOMM_CONFIG`（測試／手動用） | D2.8、D2.9；F22 讓環境變數進不了 sidecar 開的 agent | 欄位與路徑可改 |
| P6 | 自己不戳自己 | 避免備忘戳醒自己 | 可拿掉 |
| P7 | 首版不做 `--mention` 的版本偵測 | 同 sha 一起裝 | 可加 |
| P8 | 戳的文字固定、不含寄件者與內容 | D2.5 | 可改 |
| P9 | harness 之後加 `comm` 能力名 | D3.2 | 可不加 |
| P10 | 新建 `docs/aicomm.md` 記 sidecar 欄位對照 | D2.7 | 可併入 SKILL.md |
| P11 | 名字暫用 `aicomm`，不用 `comm`（與 POSIX `comm` 撞名） | 本人不必選；PM／PMO 可改 | 可改名 |
| P12 | SKILL.md 的兩條軟規則：每輪結束前 `inbox`；「收到」類回覆 `--no-wake` | D2.2、D2.9 | 可改成機制 |

## 風險與取捨

- **耦合 sidecar 的公開 HTTP 形狀**：不可免（D2.7）；降低影響的方式是「看不懂就不戳」。
- **提示注入**：戳的文字由 skill 固定、不轉述內容（D2.5）；`inbox` 的輸出仍進模型，與 `angareion read` 相同，沒有惡化。
- **額度**：每次成功戳＝一次模型輪次。去重靠 I2（盡力而為，可能多戳一次）；廣播不戳；PMO 不被 aicomm 戳（D2.9）。
- **軟規則**：D2.2 與 D2.9 的兩條規則靠 AI 遵守 SKILL.md；失守的後果是漏收（前者）或多一輪（後者），不是壞資料。
- **私人資訊**：戳與 stderr 都不含內文；說明檔沿用 Angareion 的「不要貼私人資訊」。
- **測試隔離**：測試只能對自己起的假 sidecar／假 angareion 說話，預設 sidecar 位址在測試中**必須被環境變數蓋掉**（`tests/_guard.py` 的精神；具體做法列 tasks）。

## 待驗（本設計階段未實測；不呼叫 sidecar、不跑 opencode）

| # | 要驗的事 | 為什麼 |
|---|---|---|
| V1 | 阻塞在 `angareion wait` 的 agent，sidecar 看到的 `state` 是不是 `running`（I1，tui 與 headless 各一） | 決定「不戳 running」會不會漏掉真的閒置的 agent |
| V3 | sidecar 是否記錄 poke 的 `text` 到 log | 影響 D2.5 |
| V5 | claude／opencode 能否按 agent 指定 skills 目錄 | D3.1-B 是否可行 |
| V6 | 只靠 skill 的 `description` agent 是否可靠地用它 | D3.2 |
| V8 | 有人 attach 在打字時被戳的後果（F10 只說 WARN） | 是否需要避開 |
| V9 | headless（`-p`）agent 剛結束一輪時的 `state` 是否確實回到 `idle`，且此時的 poke 確實開新一輪（F17 是讀碼，未實測） | J10 |

（V2 已由 F17 回答；V4 已由 F20 回答；V7 隨 J3 刪除。）

## 沒把握

- PMO 的職務規矩放哪、Mac 上 PMO／PM 目前的 skill 安裝路徑（F13 只記 worker 映像的做法）。
- 我沒讀 `dispatch.py` 的 `cmd_watch` 內文；「PM／隊員沒有定期叫醒」依據是 M2 文件與本設計都沒寫（推論）。
- 本設計**沒有任何實測**：沒有呼叫 sidecar、沒有跑 opencode、沒有碰真 session。
