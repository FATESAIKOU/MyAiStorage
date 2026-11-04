---
name: pmo
description: The PMO's inbox and dispatch tool. `pmo tick` reads mail for the PMO seat in one round (the only reader), `pmo send`/`pmo open` dispatch a ticket to a team and start its PM, `pmo reply`/`pmo poke`/`pmo status`/`pmo close` talk to a team PM and follow the Close SOP, and `pmo wake set|off|show` keeps exactly one scheduled wake job for the PMO on its sidecar. Use `pmo tick` instead of a background read loop; never run two readers or two ticks at the same time.
---

# pmo

`pmo` 是 PMO 的收件與派工指令（設計：`docs/design-notes/110-pmo-skill.md`）。PMO 這個身分**只透過它讀信**：一輪 `pmo tick` 把新信讀完、比對 team 的 sidecar 狀態、判斷到期項目，並把結果印成一行一行的事件；PMO 看著事件做事。

- 指令只做機械步驟：**不建工單、不派工、不重開、不換 provider**；要不要做、做哪一個，是 PMO 的判斷。
- stdout 是事件行（第一個 token 是方括號標籤），stderr 是診斷。**錯誤字樣不含 token、金鑰或訊息內文**；事件行只有 `[msg]` 印來信全文，另外 `[decision]` 印作答與說明（對方寫的內文）、`[secretary]` 印工作標題——**貼進回報或日誌前自己先看過**。
- `pmo` 的設定與狀態都由環境變數指到本機：`PMO_SKILL_CONFIG`（teams 設定檔）、`PMO_SKILL_STATE_DIR`（狀態目錄）。**沒設就拒絕執行（exit 2），不會猜預設路徑**。**已知缺口**：`PMO_SKILL_CONFIG` 指到**不存在的檔案**目前是 Python traceback＋exit 1；遇到時照 traceback 停下回報、**不要重試**。內容不是合法 JSON 才是 `[pmo] 設定讀不起來：…`＋exit 2。

> `pmo wake set|off|show` 與 `pmo tick` 的叫醒 job 健康（`[wake-missing]`／`[wake-error]`）都在這一版；`pmo --help` 的子指令沒有 `wake` 就是版本太舊，先換成含它的版本，不要照這份文件假裝叫醒已經排好。

## 私人資訊：不要貼進訊息

訊息會離開本機。**不要在訊息、工單本文、指示信或叫醒固定句裡放私人資訊**（帳號、額度、機器位址、其他專案的內部細節）；需要傳遞時請對方去參照，不要把內容貼進來。`pmo send` 另外會擋憑證樣式的內容（正文、工單標題、工單本文），被擋是 exit 1，移除後再送。

## 指令總覽

```bash
pmo tick                                                        # 跑一輪：收信、比對 agents、到期項目、叫醒健康
pmo inbox read                                                  # 只做收信一輪（救援用；不比對 agents、不跑看門狗）
pmo inbox list [--json]                                         # 唯讀：收件項目一覽
pmo inbox ack <工作 ID> [--issue <repo#n>] [--schedule 立即開工|排入待開工佇列]
pmo inbox report <工作 ID> --status <狀態> [--issue <…>] [--pr <…>] [--body -|<文字>]
pmo inbox decision-send --body -|'<JSON>'                       # 送決定題（urgency 8）
pmo inbox rebuild [--since <後端時刻>] [--dry-run]              # 由 angareion log 重建收件帳本
pmo inbox handoff-close <id>                                    # 結案一筆交接出去的群請求
pmo check <team>                                                # 派工前預檢（逐項 OK／FAIL）
pmo send <team> <owner/repo#n> --body -|'<文字>'                # 送指示信，寫 team 狀態
pmo open <team> [--session agora:…] [--fresh] [--provider P [--model M]]
pmo reply <team> [--urgency 0-9] --body -                       # 經 aicomm：寫入＋叫醒
pmo poke <team> [--interrupt] --body -                          # sidecar 的 poke
pmo status [<team>] [--json]
pmo close <team> [--timeout <秒>]                               # 照 Close SOP 關掉 team PM
pmo wake set <every>                                            # 2m～24h，例：10m、2h
pmo wake off
pmo wake show [--json]
```

`‹team›` 一律是設定裡的 key（`<專案>-<team 代號>`）；`‹body›` 寫 `-` 代表從 stdin 讀（多行內文）。所有會**寄信**的指令（`inbox ack`／`report`／`decision-send`、`send`、`reply`）都先檢查目前身分等於設定裡的 `pmo_identity`，不符就**拒送**（exit 2，不會先送再擋）；讀信的 `tick`／`inbox read` 也在最前面擋同一個身分。

## 開機步驟（每次被開、重開、sidecar 重啟後接回）

1. **先跑一次 `pmo tick`**：把停機期間的信讀掉，順便看 team 狀態與到期項目。
2. **再跑 `pmo wake show`**：看 `tick.json` 記的想要頻率、sidecar 上自己的叫醒 job 在不在、最早的到期時刻。
3. `tick.json` 記著要叫醒（`every` 不是 `null`），但 `wake show`／tick 說 job 不在（`[wake-missing]`）：**由 PMO 決定**要不要 `pmo wake set <every>`。sidecar 的 job 只在記憶體，sidecar 重啟就會不見，不是錯誤。
4. 沒排叫醒：有信時寄件人仍會立刻叫醒；到期項目要等下一封信才會被看到。

## 一個讀者：讀信只走 `pmo tick`

PMO 身分讀信**只有一個讀者**：`pmo tick`。`angareion read`／`aicomm inbox`（含 `--wait`）都會推進同一個游標，先讀的拿走信，tick 就看不到。所以：

- **不要**另外跑 `angareion read`、`aicomm inbox`、`aicomm inbox --wait`、背景的 `wait → read` 迴圈。要回話用 `pmo reply`（經 aicomm，只寫入＋叫醒，不讀信）；要看前後文用唯讀的 `angareion log`／`aicomm log`（不動游標）。
- `pmo inbox read` 也是讀者（只是不比對 agents、不跑看門狗）。只拿來救援（例如 tick 壞掉、要單獨看一輪），**不要和 tick 同時跑**。
- **不要同時跑兩個 `pmo tick`**：游標與看門狗的「先佔再 poke」讓重複跑通常無害，但 `tick.json` 與 team 狀態的鎖會互相排隊，等不到就 exit 2，兩邊的狀態也可能互相蓋掉。一次只跑一個；上一輪還沒結束就不要開下一輪。
- **`pmo inbox rebuild` 時不要跑 tick**：rebuild 會重寫帳本，去重記號與帳本會被另一邊蓋掉；等不到鎖會 exit 2。挑沒有 tick 在跑的時候做，做完再 `pmo inbox list` 對一次。

## `pmo tick`

跑一輪就結束，**不是常駐迴圈**。每一次叫醒都跑一輪；每輪只處理「已經到期」的項目，處理完把下一次到期時刻往後推，不補跑錯過的次數。

- 全部沒事時 stdout 只有一行 `[idle]`（有未到期項目時是 `[idle] [next-due=‹時刻›]`）。**看到 `[idle]` 就結束這一輪，不要再做其他工具呼叫**；這一輪的成本就是一次模型推理，不要讓它膨脹。沒有 `[idle]` 就代表 stdout 有事件（見事件表），要看過再結束。
- 結束碼：**0** 這一輪跑完（含部分失敗，失敗已印 `[warn]`）；**1** 用法錯；**2** 身分不符、設定讀不起來（stderr `[pmo] …`），或狀態檔壞（`SchemaError`：`pmo tick` 與 `pmo inbox handoff-close` 在 stdout 印 `[要本人處理]`，其他指令在 stderr 印 `[pmo] …`，只有 `pmo wake` 也在 stderr 但印 `[要本人處理] …`）——都是這一輪什麼都沒做、游標沒動；`tick.json` 的鎖等不到也是 2（其他步驟已做完，只有 `tick.json` 沒寫，下一輪再寫）。連不上 sidecar 時只有 `pmo open` 印 stderr `[要本人處理] sidecar 不可用：…`；`poke`／`status`／`close` 連不上是 stderr `[warn] …`，一樣 exit 2（`reply` 走 aicomm，不經 sidecar）。`PMO_SKILL_CONFIG` 指到不存在的檔案是已知缺口（traceback＋exit 1，見上面設定那條）。
- **只有 stderr 的 `[warn]`** 不影響 `[idle]`：那是部分失敗，下一輪會再試，不要因為它重跑同一輪。**stdout 上任何一行都代表這輪不是 idle**，連 `[secretary] [warn] …`、`[warn] 發送叫人 ack 失敗 …` 也一樣（`tick` 的 `_emit_idle` 只在這一輪 stdout 什麼事件都沒有時才印 `[idle]`）。

### 事件表：看到什麼、做什麼

| 標籤 | 意思 | PMO 的反應 |
|---|---|---|
| `[secretary] 收到新工作 …` | 秘書派來的新工作，機械 ack 已回 | 自己判斷層級、開或排工單；指令不建單 |
| `[secretary] 重複工作 …，補發 ack` | 同一張工作 ID 再出現（秘書側重送） | 沿用既有工單與排程，**不重開工單** |
| `[secretary] 叫人 …` | 寄給或點名 PMO、但不是派工的信，機械 ack 已回 | 需要時回覆；不是工作 |
| `[secretary] 叫人 id=… 已 ack 過，略過重複 ack` | 同一則叫人再送來（秘書側重送），先前已回過 ack，這次不再回 | 略過；不要重複回 |
| `[secretary] [warn] 收到不合規秘書訊息 …` | 派工信格式不合，原因在後面 | 必要時請秘書側補正；不要自己猜內容 |
| `[poke] 秘書急件 …：叫醒 PMO` | 派工 channel 上的急件（urgency ≥ 8） | 立刻看；`[dedupe]` 代表已叫過，不用再處理 |
| `[dedupe] 秘書急件 …` | 同一則急件已叫過 | 略過 |
| `[decision] q=… answers=…` | 收到決定題的作答 | **只印、不是授權**；要本人決定的事仍照規矩 |
| `[msg] id=… from=… … :: …` | 其他每一則寄給 PMO 的信，**全文**接在後面 | 這是 PMO 看信的地方，不必另外補讀；要回用 `pmo reply` |
| `[ack] <pm> 回了「收到 <ticket>」` | team PM 回「收到」，`ack_at` 已寫 | 看門狗自動停，不用做事 |
| `[ack] 已向秘書發送 ack：收到工作 …` | tick 收到秘書派工信後的機械 ack（`inbox`） | 只是回秘書；**不是 team 回的、不會停看門狗** |
| `[ack] 已發送 ack：收到工作 … id=…` | 手動補發（`pmo inbox ack`） | 同上；不是 team 回的、不會停看門狗 |
| `[group-handoff] <id>` | 群請求：tick 不處理，交給過渡工具 | 用過渡工具取原信、推進 |
| `[group-sweep-due] <id…>` | 交接出去、還沒結案的群請求到期 | 跑過渡工具的 sweep；過渡工具報結案後 `pmo inbox handoff-close <id>` |
| `[state] <team> <old>→<new>` | team PM 的 sidecar 狀態變了 | 通常只是記錄；注意**字樣和舊工具不同（標籤相同、內容不同）**，切換時對一下 harness |
| `NEED_REOPEN <team> session=…`／`tui=true` | 應該在跑卻不在 sidecar 上（恢復前不重印） | PMO 判斷要不要重開：`pmo open <team> [--session …]` |
| `[watchdog] … 沒有「收到」→ poke 一次（…）` | 派工 10 分鐘沒有「收到」，已 poke（記在 team 狀態，兩個 tick 也只 poke 一次） | 等「收到」；不要自己再 poke |
| `[要本人處理] …` | 需要本人的狀況（狀態檔壞、sidecar 不可用、看門狗升級、team PM 非預期結束、全部冷卻…） | **停下、回報**；不要猜、不要自己重試或繞過。tick 印在 stdout；`pmo open` 連不上 sidecar 時是 stderr `[要本人處理] sidecar 不可用：…`（`poke`／`status`／`close` 連不上是 stderr `[warn] …`），都是 exit 2 |
| `[no-candidate] <team> <provider>` | team PM 因 no candidate 結束 | 照模型順序換下一個 provider，用 `pmo open <team> --provider … --model …` 重開（實作與審查要用不同的 `(provider, model)`） |
| `[waiting-quota] <team>` | 在等額度（只印一次） | **不用處理**，sidecar 會自動重開；同一輪若另有 `reopen_at=null` 的 `[要本人處理]` 才要本人 |
| `[worker-action] <team> watchdog-poke`／`poll` | worker team 需要過渡工具做的動作（tick 不碰 worker 的 sidecar） | 用過渡工具做 |
| `[wake-missing] every=…` | `tick.json` 記著要叫醒，sidecar 上卻沒有自己的 job（sidecar 重啟過）；同一次消失只印一次 | 決定要不要 `pmo wake set <every>` |
| `[wake-error] <job> <碼>` | 自己的叫醒 job 上次執行失敗（例：`agent_not_running`、`agent_awaiting_answer`）；碼變了才印 | 只是告知：job 照排程繼續，不自動重試；PMO 回來後下一次成功就消失 |
| `[idle] [next-due=…]` | 這一輪什麼都沒有（附最早的到期時刻） | **直接結束這一輪**；可拿 `next-due` 決定叫醒間隔 |
| `[warn] …`（stderr） | 部分失敗 | 看原因；下一輪會再試，不要重跑同一輪（只有 stderr 的 `[warn]` 不影響 `[idle]`，見上面「`pmo tick`」） |

`[wake-hint]` **不是 tick 印的**：那是 `pmo send` 在「叫醒間隔比看門狗（10 分鐘）長、或沒開」時多印的一行提示（見下面的派工流程），指令不會因此改任何東西。

## 派工流程

一次派工是「`check` → `send` → `open` → 看 tick 的「收到」→ 做完之後 `close`」：

1. **`pmo check <team>`**：逐項 `OK`／`FAIL`（team 存在且未退役、PM 名合法且角色是 pm、team 狀態與 `tick.json` 讀得起來、channel 讀得到、本機 sidecar 有回應）。worker team 多一行 `--  worker team：…`（資訊列，不算 FAIL）。有 FAIL 就是 **exit 1**，先修再派。
2. **`pmo send <team> <owner/repo#n> --body -`**：讀工單（唯讀 `gh issue view`）、送指示信、寫 team 狀態（工單、訊息 id、後端時刻、`--since`）。成功印 `[send] 指示信已送出 id=… 後端時刻=…`。叫醒間隔比看門狗（10 分鐘）長、或是沒開時，多印一行 `[wake-hint]`；它只是提示，指令不改任何東西——由 PMO 決定要不要 `pmo wake set 10m`。
3. **`pmo open <team>`**：在該 team 的 sidecar 上開 team PM，第一則輸入帶派工的 `--since`。`--session` 接回既有 session、`--fresh` 只配互動版（tui）、`--provider`／`--model` 優先於設定。成功印 `[open] team=… id=… state=… attach=…`；互動版把 `attach.local` 原樣貼上執行。
4. **等 `[ack]`**：team PM 回「收到 ‹工單›」時，tick 寫 `ack_at` 並印 `[ack]`。沒回時看門狗會在 10 分鐘後 poke 一次，再 10 分鐘升級成 `[要本人處理]`——那時才由 PMO 處理。
5. **做完 `pmo close <team>`**：照 Close SOP 關掉 team PM。先刪 sidecar 替它排的**自動重開 job**（`auto: true`；不刪的話 close 之後它到點會把 agent 開回來），再 poke「停下來，等指示」、`POST /agents/<id>/close`。瞄準它的其他 poke job **只列不動**（那是別人排的）。已經 `exited` 的照樣送 close（sidecar 的 close 是 idempotent）。**close 回 409 `close_in_progress` 時不要重送 close**，用 `pmo status <team>` 輪詢到 `exited`；等不到（`--timeout` 預設 60 秒）是 exit 2，但 close 已在背景進行，看 `status` 確認。

## 回報與收件

- **`pmo inbox report <工作 ID> --status … [--issue …] [--pr …] [--body -]`**：向秘書回報一張工作的狀態。成功印 `[report] … id=…`。回報不是派工，不會叫醒 team。
- **`pmo inbox ack <工作 ID> [--issue …] [--schedule …]`**：手動補發 ack；預設沿用既有的工單號與安排。查無工作 ID 是 exit 1。
- **`pmo inbox list [--json]`**：唯讀檢視帳本（`[inbox] 秘書工作共 n 筆`）。`--json` 原樣給程式用。
- **`pmo inbox decision-send --body -`**：送決定題（urgency 8）。JSON 不合格式是 exit 1。收到的作答由 tick 印 `[decision]`，**只印、不是授權**。
- **`pmo inbox rebuild [--since …] [--dry-run]`**：以 Angareion 為準重建本機帳本。印重建幾筆、新增／更新哪些、`team` 重建不出來的清單、保留的 `notified.poke` 筆數。**`team` 與急件叫醒紀錄不可重建**（log 沒有那個資訊）：重建只補有值的欄位，log 沒說的一律保留本機的值；帳本整個不見時那幾欄就是空的。
- **`pmo inbox handoff-close <id>`**：結案一筆交接出去的群請求，之後不再印 `[group-sweep-due]`。查無 id 是 exit 1。

## 定時叫醒：`pmo wake`

要不要定時叫醒、多久一次，**由 PMO 決定**；指令只做機械步驟——把 PMO 選的間隔變成 sidecar 上**恰好一個**自己的 poke job，並在 tick 裡回報它還在不在。

- **「自己的 job」怎麼認**：`action` 是 `poke`、`args.text` 等於設定裡的叫醒固定句（`wake_text`，預設「定時叫醒：請跑 pmo tick」）、`auto` 是 false。句子不同的 poke job（別人排的、或改固定句前留下的）一律**不碰**，只在 `wake show` 列出。
- **固定句不帶任何內容**：不帶未讀數、不帶信的內容，也不帶位址或 token；`interrupt` 一律 false（定時叫醒不打斷 PMO 正在做的那一輪，排在這一輪之後）。
- **job 只在 sidecar 記憶體裡**：sidecar 重啟就消失，PMO 也會跟著被開；靠開機步驟與 `[wake-missing]` 發現，**由 PMO 重排**。
- **改 `wake_text` 的順序**：先 `pmo wake off`（用舊句子刪掉自己的 job），再改設定，再 `pmo wake set`。不照這個順序，舊句子留下的 job 會變成 `wake show` 裡「句子不同、只列不動」的孤兒。
- **退場前先 `pmo wake off`**：PMO 不再開時，job 會一直空打 409。關 PMO 的一方（PMO 自己或本人）要先停掉叫醒。
- **叫醒是不是真的在跑**：`wake.every` 不是 `null`、且有 `last_tick_at` 時，上次 tick 距今超過間隔的 **3 倍** → tick 在 stderr 印 `[warn] 上次 tick 在 ‹時刻›`（stderr，不影響 stdout 的 `[idle]`）。這是**叫醒壞掉的訊號**：去查 `pmo wake show`，不要以為下一輪會再試。
- **tick 每輪看一次 job 的健康**（設定有 `pmo_sidecar` 才看，`wake.every` 是 `null` 也看）：
  - `wake.every` 不是 `null`：自己的 job 不在 → `[wake-missing] every=…`（同一次消失只印一次，job 回來才重設）；job 有 `last_error` 且碼和上次不同 → `[wake-error] ‹job id› ‹碼›`（例 `agent_not_running`、`agent_awaiting_answer`）；自己的 job 超過一個 → stderr `[warn] 自己的叫醒 job 有 ‹n› 個`。
  - `wake.every` 是 `null` 但 sidecar 上有自己的 job（刪不掉、或有人用同一個固定句排了一個）→ stderr `[warn] tick.json 沒記叫醒頻率，sidecar 上有自己的 job ‹id› every=…`，只印一次。
  - 這一步的 `GET /jobs` 失敗只印 stderr `[warn] GET /jobs：‹碼或原因›`，其他步驟照跑。

| 指令 | 做什麼 | 輸出 | 結束碼 |
|---|---|---|---|
| `pmo wake set <every>` | 間隔寫成 `‹整數›m`／`‹整數›h`，範圍 **2m～24h**。同間隔、同 id 的一個 job 就不動 sidecar；否則**先建後刪**（中間不會沒人叫醒，代價是短暫兩個 job、最多多跑一輪） | `[wake] every=… job=… next=…`；刪不掉舊 job 是 stderr `[warn] 刪不掉自己的叫醒 job ‹id›：‹碼›`（exit 仍是 0） | 0；1 間隔格式錯；2 PMO 不在 sidecar、sidecar 拒絕或連不上、`tick.json` 壞（stderr `[要本人處理] …`）、等不到鎖、`POST /jobs` 的 2xx 讀不懂又找不到剛建的 job |
| `pmo wake off` | 刪掉所有自己的 job，`wake.every` 設成 `null`；之後 tick 不再印 `[wake-missing]` | `[wake] off`，每個刪掉的 job 一行 `[wake] 刪掉自己的叫醒 job：‹id›`；沒有自己的 job 時印 `[wake] 沒有自己的叫醒 job` | 0；2 同 `set`；刪不掉自己的 job 是 stderr `[warn]` 且 exit 2（`wake.every` 仍設成 `null`） |
| `pmo wake show [--json]` | 唯讀：自己的 job（id、every、next、last_run、last_error）、想要的頻率、`last_tick_at`、最早的到期時刻（含看門狗，和 tick 的 `[idle] next-due` 同一個算法） | 第一行 `[wake] every=… set_at=… last_tick_at=… next-due=…`（`wake.every` 是 `null` 時印 `every=off`，沒有的值印 `-`），自己的 job 每個一行 `[wake] job ‹id› every=… next=… last_run=… last_error=…`；沒有自己的 job 時印 `[wake] sidecar 上沒有自己的叫醒 job`；有「瞄準 PMO、句子不同」的 job 時先印 `[wake] 瞄準 ‹identity› 的其他 poke job（句子不同，只列不動）：` 再逐行列出（只列不動） | 0；2 PMO 不在 sidecar、sidecar 拒絕或連不上、`tick.json` 或 team 狀態檔壞（stderr `[要本人處理] …`） |

- `POST /jobs` 回 2xx 但回應讀不懂時，指令會用一次 `GET /jobs` 找回剛建的那一筆（POST 之前沒有、自己的、`every` 相同）；找回就照正常流程繼續。找不到就只留 stderr `[warn] POST /jobs 回 ‹碼›，但回應讀不懂，找不到剛建的 job；請跑 pmo wake show 確認` 並 exit 2——**不刪 job、不寫 `wake.every`**，先用 `wake show` 看實際有什麼再決定。
- 等不到鎖時 stderr 是 `[warn] pmo wake 等不到鎖（wake.lock 最多 30 秒、tick.json.lock 最多 10 秒），沒有寫 tick.json`，exit 2；其他 sidecar 失敗是 `[warn] pmo wake 失敗：…`。`set`／`off` 全程拿著 `wake.lock`，兩份會排隊，不會留下兩個 job。
- **`set` 的鎖逾時不是「什麼都沒變」**：`set` 需要換 job 時先建新 job、刪舊 job，最後才拿 `tick.json.lock` 寫 `wake.every`；逾時 exit 2 時 sidecar 上的 job 已經換了、`wake.every` 卻沒寫（`off` 同理：job 可能已刪、`wake.every` 沒寫）。**exit 2 之後先跑 `pmo wake show` 看實際狀態**再決定，不要直接重下同一個指令。

**建議頻率**（建議，不是規定；PMO 依情況調）：

| 情況 | 建議 | 理由 |
|---|---|---|
| 有派工在等「收到」、有未結案的交接、或有 worker team 在跑 | **10m** | 看門狗 10 分鐘，間隔 10 分鐘時最晚 20 分鐘看到；5m 只快 5 分鐘，叫醒次數卻加倍 |
| 什麼都沒在等（`tick` 只印 `[idle]`、沒有 `next-due`） | **30m～60m**，或 `wake off` | 有信時寄件人會立刻叫醒；拉長只會延後發現本機 team PM 的狀態變化 |
| 剛派工（`send` 印了 `[wake-hint]`）、或剛看到 `[wake-missing]` | 依上面兩列重新判斷 | 別讓看門狗等超過一個間隔 |

叫醒間隔只影響「到期之後多久才被看到」，不影響「會不會重複處理」。額度用量偏高時，把間隔拉長（照環境說明的額度規則）。

## 錯誤與停損

**結束碼**（每個指令的細節見上；這裡是通則）：

- **0**：成功／這一輪跑完。包含「部分失敗、已印 `[warn]`」。
- **1**：用法錯（旗標拼錯、缺必填、格式錯）、查無 id／team、team 已退役、工單讀不到、指示信超過上限、內容含憑證樣式。`pmo check` 有 FAIL 也是 1（不是用法錯，但同樣是「還沒準備好」）。`PMO_SKILL_CONFIG` 指到**不存在的檔案**是已知缺口：traceback＋exit 1，見上面「`pmo` 的設定與狀態都由環境變數指到本機」那條。
- **2**：設定或狀態檔壞、身分不符、外部失敗（Angareion／aicomm／sidecar／GitHub 後端）、鎖等不到、`SentButNotRecorded`。**看到 2 不要反射性重送**：字樣會說明是「送不出去」還是「已送出、帳本沒更新」。

**幾個會讓人做錯的地方**：

- **`SentButNotRecorded`**（字樣：`已送出…，但帳本寫不進去…。不要重送`）：信**已經送出**，只是本機狀態沒記。不要重送；先修狀態檔，再跑 `pmo inbox list` 對一次。
- **身分不符**（`身分不符拒送：目前 identity 為…`）：換設定或環境，不要換身分硬送，也不要請別的 session 代送。
- **鎖等不到**：`tick.json.lock` 最多 10 秒、`wake.lock` 最多 30 秒、team 狀態鎖最多 10 秒。逾時是 exit 2 或 `[warn]`；**下一輪再試**，不要同時重跑兩份去搶鎖。
- **sidecar 400**：參數錯，**不重試**，照訊息修參數。**409**：拒絕，交 PMO 判斷；`close_in_progress` 不重送 close，用 `pmo status` 輪詢。
- **aicomm 逾時**（`pmo reply`）：信中可能已經寫進去了，字樣是「送出結果不明，不要直接重送」；先查再決定。
- **`database is locked`**（共用的 opencode 服務）：看到就印原文，**不重試、不重開**，經通道回報。
- **同一個失敗修兩次還修不掉、或 `[要本人處理]`**：停下、回報，不要自己繞過或改規格。

## 不要做的事

- 不要用 PMO 身分跑第二個讀者（`angareion read`、`aicomm inbox`、背景 `wait → read`）；讀信只走 `pmo tick`。
- 不要同時跑兩個 `pmo tick`；不要在 `pmo inbox rebuild` 時跑 tick。
- 不要讓指令代替判斷：不自動建工單、不自動派工、不自動重開、不自動換 provider；`[decision]` 不是授權。
- 不要重送「結果不明」或「已送出、帳本沒更新」的信；不要用 `--since` 反覆撈已處理的信。
- 不要在叫醒固定句裡放任何內容（未讀數、訊息、位址、token）；不要用 `wake set` 排比 24 小時更長的間隔，要比 24 小時久就用 `wake off`。
- 不要手改狀態檔去「修好」`[要本人處理]`；壞檔照原樣停下來處理。
- 不要把 token、金鑰、密碼寫進訊息、報告或 log；不要貼私人資訊。
- 不要在 `[idle]` 之後繼續做其他工具呼叫。
