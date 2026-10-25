# aicomm

AI 之間的溝通指令（工單 MyAiStorage#212；設計在 `openspec/changes/comm-skill/`）。使用說明給 agent 看的是 `skills/aicomm/SKILL.md`，這份記設計上的對照。

## 它做什麼

- `aicomm send`：用 `angareion send` 寫入訊息（這就是「送達」），成功後對 `--to` 與 `--mention` 的 AI 各判斷一次要不要在本機 sidecar 上叫醒。
- `aicomm inbox`：用 `angareion read`（要等時先 `angareion wait`）讀寄給自己的信；固定不帶 `--channel`。
- 只用標準函式庫；對 Angareion 只呼叫它的指令列（subprocess），不 import `angareion` 套件；沒有設定檔。

## 同一個身分只留一個讀者（#219）

`angareion read` 讀走信就推進游標，所以同一個身分有兩個讀者時，先讀的拿走信，另一個看到空的（#212 驗收前的調查：背景的 `wait → read` 迴圈搶先讀走，被 poke 叫醒的前景 `inbox` 是空的）。`aicomm inbox --wait` 在 wait 之後會接著 read，所以放到背景一樣會讀走信，不算「只等不讀」。

- 軟規則：被 sidecar 管的 agent（會被 poke 叫醒）不跑背景讀信迴圈，被叫醒就在前景 `aicomm inbox`；沒有人會 poke 的，才用 `aicomm inbox --wait` 當唯一的讀者。
- 補救：被叫醒但 inbox 是空的 → 先看自己有沒有背景讀信任務，它的輸出裡可能已經有那一則；要補讀用 `angareion read --channel <ch> --since <時刻>`（不移動游標）。
- 提示：`aicomm inbox` 的 read 成功（exit 0）但 stdout 是空的（`--json` 時是 `[]`），就在 stderr 加一行同樣的補救；exit 0 與 stdout 不變。`--wait` 逾時（exit 3）與後端錯（exit 2）不出提示。

## 對 sidecar 的依賴面

aicomm 只對 sidecar 發兩種請求，位址固定為 sidecar 文件的預設 loopback 位址 `http://127.0.0.1:8471`：

| 請求 | 用途 | 讀什麼 |
|---|---|---|
| `GET /agents/{id}`（逾時 3 秒） | 這個 AI 在不在這台、狀態是什麼 | 200 的 `state`；404 ＝不在這台 |
| `POST /agents/{id}/poke`（逾時 5 秒） | 叫醒 | body `{"text": <固定短句>, "interrupt": <bool>}` |

- `state` 是 `idle` 或 `running` 才戳；`starting`、`waiting-quota`、`exited` 不戳。不看 tui 或 headless，不讀 `attach`。
- `interrupt` 預設 false；給了 `--interrupt` 且對方 `running` 才是 true（對 `idle` 的戳本來就不打斷）。
- 回應不符預期（非 JSON、沒有字串的 `state`、不認得的 HTTP 狀態）一律當叫醒失敗、不戳，訊息已送達的結果不變。
- 戳逾時不重送（重送可能變兩份），stderr 印「叫醒結果未知」。
- sidecar 若改了這些形狀，aicomm 要跟著改；對照以這張表為準。

## 位址覆寫（只給測試與手動）

環境變數 `AICOMM_SIDECAR` 可以把位址改成**另一個單一 loopback 位址**（`localhost`、`127.0.0.0/8`、`::1`），測試靠它指到自己起的假 sidecar。sidecar 開的 agent 的子行程環境是白名單，拿不到這個變數，所以正式部署一律是預設位址。

- 只有「需要叫醒」時才檢查：沒有 `--no-wake`，且 `--to` 加 `--mention` 排除自己後還有人。覆寫值不合格就 exit 1，而且是在寫進 Angareion 之前。
- 這個檢查**不能防止跨機叫醒**（轉發埠本身也是 loopback），也不宣稱能。

## 自己是誰

「自己不戳自己」用環境變數 `ANGAREION_IDENTITY` 判斷；aicomm 不讀 Angareion 的設定檔。沒有設這個變數時不會排除任何人。

## 結束碼

見 `skills/aicomm/SKILL.md`。`send` 的 exit 0 只代表訊息已寫進 Angareion。

## 版本

`aicomm --version` 印套件版本。指令與說明檔跟 `angareion` 在同一個 distribution，用同一個 git sha 釘。
