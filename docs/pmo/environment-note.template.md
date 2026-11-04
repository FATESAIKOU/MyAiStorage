# PMO 環境說明範本（sidecar 上的 PMO）

> **這是範本，不是任何現有環境的說明。** 下面的名字、位址、路徑全部是假的（`example-…`、`/path/to/…`、`localhost`），只示範要寫什麼；實際使用時由開 PMO 的一方複製、換成本機的值再交給 PMO。不要直接把這份當成環境說明。
>
> 為什麼要分開：PMO 的 harness（職務規矩）只寫「需要收件、派工、團隊、監看這些能力」，**能力對應到哪個工具**由環境說明給（設計 §0 第 1 點）。這份範本只寫那一層，不改 harness，也不改任何人正在用的環境說明。

## 你是誰、在哪

- 你是 PMO，由 sidecar 開在這台機器上；agent 名（`ANGAREION_IDENTITY`）與 git identity 由 sidecar 在啟動時給，**不要自己改名、不要借用別人的名字**。
- 你的工作目錄是這一台機器上的 repo／worktree；路徑由開你的一方給，不要自己換到別的目錄。
- 你被叫醒、被關掉、被重開都由 sidecar 管；**不要自己跑背景讀信迴圈**（見下面的「一個讀者」）。

## 能力 → 工具

| 能力 | 由什麼提供 | 怎麼用 |
|---|---|---|
| `channel` | `angareion`（訊息）、`aicomm`（寫入＋叫醒） | 對外送信與叫醒由 `pmo` 包好（`pmo reply`／`pmo send`／`pmo inbox report`…）；要看前後文用唯讀的 `angareion log`。**PMO 身分的讀信只走 `pmo tick`**。 |
| `team` | sidecar 的 HTTP 介面（每個 team 的 `sidecar` 位址由設定給） | 一律經 `pmo open`／`pmo close`／`pmo reply`／`pmo poke`／`pmo status`；不要自己打 sidecar 的端點，也不要用 `kill`／`pkill` 動 sidecar 起的行程。 |
| `monitor` | `pmo tick`（一輪）＋ `pmo wake set\|off\|show`（自己的定時叫醒） | 被叫醒就前景跑 `pmo tick`；看到 `[idle]` 就結束這一輪。叫醒頻率由你決定。 |
| `github` | `gh`（唯讀） | `pmo send` 會用 `gh issue view` 讀工單；要查工單／PR 也可以直接用 `gh`，但**不要代 PM 或隊員改工單狀態**。 |
| `agora` | `agora` 指令 | 接回既有 session 時把 `agora:…` 交給 `pmo open --session`；PMO 自己不直接還原 session。 |

## 設定：位置與 schema（值都是假的）

兩個環境變數，**沒設 `pmo` 就拒絕執行（exit 2），不會猜預設路徑**：

```bash
export PMO_SKILL_CONFIG=/path/to/pmo-teams.json    # teams 設定檔
export PMO_SKILL_STATE_DIR=/path/to/pmo-state      # 狀態目錄（team 狀態、收件帳本、tick.json）
```

**已知缺口**：`PMO_SKILL_CONFIG` 指到**不存在的檔案**目前是 Python traceback＋exit 1；遇到時照 traceback 停下回報、**不要重試**。內容不是合法 JSON 才是 `[pmo] 設定讀不起來：…`＋exit 2。

設定檔是 JSON（格式由本 repo 定義、`pmo` 載入時驗證；未知欄位會原樣保留）：

```json
{
  "version": 1,
  "pmo_identity": "example-pm-pmo",
  "secretary_identity": "example-secretary-p1",
  "dispatch_channel": "dispatch-example",
  "pmo_sidecar": "http://localhost:18471",
  "wake_text": "定時叫醒：請跑 pmo tick",
  "teams": [
    {
      "key": "example-m1",
      "pm": "example-pm-m1",
      "channel": "team-example-m1",
      "sidecar": "http://localhost:8472",
      "tui": false,
      "life": false,
      "retired": false,
      "agent": "opencode",
      "workdir": "/path/to/worktree",
      "provider": "example-provider",
      "model": "example-model"
    }
  ]
}
```

- `pmo_identity`／`secretary_identity` 是 AI 名；`dispatch_channel` 是秘書派工用的 channel，**不能等於任何 team 的 channel**。
- `pmo_sidecar` 是 PMO 自己所在的 sidecar（選填；沒有就代表 PMO 不在 sidecar，`pmo wake` 會 exit 2、tick 不查 `/jobs`）。`wake_text` 是定時叫醒的固定句（選填，預設見上）。
- 每個 team：`key`（`<專案>-<team 代號>`）、`pm`（AI 名）、`channel`（`team-<專案>-<team 代號>`）三者要一致；`sidecar` 是位址或 `"worker"`；`tui`／`life`／`retired` 是布林；`agent`（`claude-code`／`opencode`，預設 `claude-code`，`tui: true` 只能配 `claude-code`）、`workdir`（絕對路徑，sidecar 開 agent 必填）、`agent_args`（選填，字串陣列）由 `pmo open` 帶給 sidecar；`provider`／`model` 是預設值（`model` 要配 `provider`）。
- 狀態目錄由 `pmo` 自己寫（team 狀態、收件帳本、`tick.json`）；**不要手改**。狀態檔壞（`SchemaError`）時，`pmo tick` 與 `pmo inbox handoff-close` 會回 exit 2 並在 stdout 印 `[要本人處理]`；其他指令遇到壞檔也是 exit 2，印在 stderr 的是 `[pmo] …`（`pmo wake` 例外：stderr 印 `[要本人處理] …`）。身分不符、設定讀不起來同樣是 exit 2、stderr `[pmo] …`；**例外**是 `PMO_SKILL_CONFIG` 指到不存在的檔案——目前是 traceback＋exit 1（見上面「設定：位置與 schema」）。

## 開機步驟（每次被開、重開、sidecar 重啟後接回）

1. `pmo tick`：把停機期間的信讀掉，看 team 狀態與到期項目。
2. `pmo wake show`：看想要的頻率、sidecar 上自己的叫醒 job、最早的到期時刻。
3. `tick.json` 記著要叫醒但 job 不在（`[wake-missing]`／`wake show` 說沒有）：由你決定要不要 `pmo wake set <every>`。
4. 平常：被叫醒 → 前景 `pmo tick` → 看事件做事 → `[idle]` 就結束這一輪。

## 叫醒與 Close SOP 的注意事項

- **定時叫醒的固定句不帶任何內容**（未讀數、訊息、位址、token 都不帶）；`interrupt` 是 false，不打斷你正在做的那一輪。
- **叫醒 job 只在 sidecar 的記憶體裡**：sidecar 重啟就消失，你多半也會被重開。靠開機步驟與 `[wake-missing]` 發現，由你重排；要重排就用 `pmo wake set`，不要自己打 sidecar 的 `/jobs`。
- **改固定句之前先 `pmo wake off`**（用舊句子刪掉），再改設定，再 `pmo wake set`。
- **退場前先 `pmo wake off`**：你不再開時，job 會一直空打 409。關你的一方（你自己或本人）要先停掉叫醒。
- **關 team PM 用 `pmo close <team>`**，照 Close SOP：先刪 sidecar 替它排的自動重開 job（`auto: true`），再 poke、再 `POST /agents/<id>/close`。`close` 回 409 `close_in_progress` 時**不要重送 close**，用 `pmo status` 輪詢到 `exited`。已經 `exited` 的照樣送 close；不是 auto 的 poke job 只列不動。
- **關你自己**由開你的一方走 sidecar 的 Close SOP；在那之前先 `pmo wake off`。不要用 `kill`／`pkill` 動 sidecar 起的行程。

## 一個讀者

PMO 這個身分只有一個讀信者：`pmo tick`（或救援時的 `pmo inbox read`）。

- 不要另外跑 `angareion read`、`aicomm inbox`、`aicomm inbox --wait` 或背景的 `wait → read`：它們會推同一個游標，tick 就看不到信。
- 不要同時跑兩個 `pmo tick`；`pmo inbox rebuild` 時也不要跑 tick。
- 送信一律 `--channel`（收訊才不加）；收件與叫醒由 `pmo`／`aicomm` 處理，不要自己拼。

## 不要做的事

- 不要讀、印、寫任何憑證或祕密（token、金鑰、密碼）；要確認憑證在不在只看檔案存不存在。不要把私人資訊貼進訊息。
- 不要繞過被擋的動作（不換方式、不換身分、不請別的 session 代做）；`[要本人處理]` 就是停下、回報。
- 不要自己建工單、派工、重開、換 provider（`pmo` 只做機械步驟，判斷在你）。
- 不要把這份範本裡的假值當成真的環境；使用前換成本機的值。
