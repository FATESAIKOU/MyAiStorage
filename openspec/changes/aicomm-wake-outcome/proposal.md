## Why

`aicomm send` 現在只知道「訊息已寫進 Angareion」，不知道收件方有沒有被叫醒。期 5 發生四次「回報者以為已回、實際沒有送出／對方沒有被叫醒」的無聲停擺；送信端分不出「已叫醒」、「對方忙（排隊）」與「未叫醒」，失敗後也沒有可對照的 sidecar 紀錄 id，查不到那一次到底發生了什麼。

期 6 的 M1（通訊看得見）要補這件事：`aicomm` 回報叫醒結果（已叫醒／對方忙／未叫醒），並附上 sidecar 那邊的紀錄 id 以便對照（MyAiStorage#406）。

## What Changes

- **`wake:` 行加一個修飾字，不新增第四個種類**：種類仍是 `woken`／`not-woken`／`unknown`。「對方忙（排隊）」是 `woken` 加 ` queued`（`wake: <agent> woken local queued`）。「已叫醒」是 `woken` 不帶 `queued`；「未叫醒」是 `not-woken`。
- **行尾加紀錄 id**：` record=<位置>/<id>[,<位置>/<id>…]`，位置是 `local` 或 `peer:<port>`。沒有 id 時整段不寫，所以沒有拿到 id 的 `wake:` 行與現在一字不差。
- **id 與 `queued` 都從 poke 回應的 body 讀**：本機讀 HTTP body，跨機讀 `pool-peer call` stdout 第二行起的 body；兩條路徑共用同一張判讀表（「戳的回應怎麼判讀（本機與跨機共用）」），不另開請求。
- **`--require-wake` 把排隊算成叫醒**（exit 0）；exit 4 的條件不變（有人是 `not-woken` 或 `unknown`）。沒帶旗標時 exit 一律 0（D-24 不變）。
- **文件**：`docs/aicomm.md` 與 `skills/aicomm/SKILL.md` 寫明「回報一律帶 `--to`，送完看 `wake:` 行」、各種結果之後要怎麼做、`record=` 的兩條查法；exit 0 仍然只代表已寫進 Angareion。
- **修 409 錯誤碼讀法的現行 bug（D-7／F-1）**：sidecar 的錯誤 body 是巢狀的 `{"error": {"code": …}}`，但 `_refusal_code` 只讀頂層的 `code` 或頂層字串的 `error`，所以真的 sidecar 回 409 時一律落成 `http-409`，`agent-not-running` 與 `awaiting-answer` 從來沒有對到。改法是依序找巢狀 `error.code`、頂層 `code`、頂層字串 `error`，第一個是字串的就定案；定案的值不認得時是 `http-409`，不再往下找（與現行 `break` 同一個精神）。這是 bug 修正，不是介面變更，併在本單一起做（Q-2）。
- **對 sidecar 的需求（MyLinuxPool#786，已談定為 poke-records-v1）**：sidecar 自己答的每一個 poke 回應（200、400、404、409、413、503、500）都在頂層帶 `poke_id` 與 `poke_outcome`（200 是 `sent`／`queued`，錯誤是 `refused`／`failed`）；另有唯讀的 `GET /pokes/{id}`。兩邊都是選用欄位，所以可以非同步上線。
- **spec delta**（`specs/comm-skill/spec.md`）：MODIFIED 五條與新行為相關的 requirement——「不改 Angareion 與 sidecar，不攜帶私人資訊」、「戳的回應怎麼判讀（本機與跨機共用）」、「`wake:` 行的文法」、「同一個收件人戳了好幾台時取一個結果」、「`send` 的結束碼與 `--require-wake`」。另外主 spec 的 Purpose 段要加一句「`wake:` 行附 sidecar 紀錄 id」（歸檔時同步，見 `tasks.md`）。
- **決策清單**：`docs/architecture/decisions.md` 新增 D-25（本設計 D-1～D-4、D-7 的摘要），並在 D-24 註明「由 D-25 擴充」；**本設計 PR 已含**（AGENTS.md：總圖與決策清單的改動，和引起它的設計文件放在同一個 PR）。

### 撤回既有原則（主 spec `:205-210`）

主 spec 的「不改 Angareion 與 sidecar，不攜帶私人資訊」原本寫「MUST NOT 要求 sidecar 新增介面」。本 change **有意識地撤回這條原則的一部分**：aicomm 仍然只**使用**查 agent 與戳兩種請求，不新增自己發出的路由，但 **MAY 讀**戳回應中的選用欄位 `poke_id`／`poke_outcome`（MyLinuxPool#786）；欄位缺少時行為與舊版相同。這是因為要拿到可對照的紀錄 id，只能由 sidecar 在既有的 poke 回應裡附帶，aicomm 無法自己產生（D-3）。spec delta 會把這一段要求文字改掉，避免審查時被當成違規。

### 與 MyLinuxPool#786 的依賴與非同步上線

- aicomm 讀的欄位全部是**選用**：sidecar 先上、aicomm 先上都可以。沒有欄位時，`wake:` 行和現在一字不差；aicomm 不因為缺少欄位而改變種類、原因碼或結束碼。
- 跨機的欄位能否原樣帶回來，已由 MLP 確認 `pool-peer call` 不改寫 body（R-3）。R-1～R-4 已談定（poke-records-v1），C-7、C-8 不必再標暫定。
- 真機驗證（24 小時都查得到紀錄）不在此 change 的單元測試裡，由 PM／PMO 在 M1 收束時排。

### Q-1：exit code 不區分「排隊」（PM 已接受）

單的原文寫「輸出與 exit code 區分已叫醒／對方忙（排隊）／未叫醒」；本設計**刻意不讓 exit code 分出排隊**：exit code 只回答「呼叫端還要不要再做什麼」，排隊不需要再做什麼（重送會變兩份；要對方立刻看，應該是下一則用 `--interrupt`）。三者的差別寫在 `wake:` 行。理由見 `design.md` D-2。

PM 已決定 **Q-1 接受**，PMO 無異議（2026-11-07），差異與理由如下。若本人認為這偏離單的原意，次選是在 `--require-wake` 下新增 exit 5 代表「沒有人失敗，但有人排隊」；那要另改結束碼表與文件，本 change 不實作。

## Non-goals

- **P-1 廣播提示不納入本單**：廣播時在 stderr 多印一行「廣播不叫醒任何人」是行為變更，PM 未指示，另單處理。
- **aicomm 不加查詢子指令**（D-4）：`aicomm` 仍然只有 `send`／`inbox`／`log` 三個子指令；對照紀錄直接查 sidecar（本機 `curl`、跨機 `pool-peer call`）。`aicomm wake-log` 留到以後真的有需要時再開單（P-2）。
- **MyLinuxPool#786 的 sidecar 實作**：poke 回應欄位、紀錄保存、`GET /pokes/{id}`、`pool-peer` 不改寫 body，都歸 MLP；本 change 只寫 aicomm 這一側依賴的介面。
- **真機驗收**不是 PR，是 M1 收束時由 PM／PMO 排的步驟。
- 不改 Angareion、不改 aicomm 的 `inbox`／`log`、送信本身、`--no-wake`／`--interrupt` 的語意、固定句、`AICOMM_SIDECAR` 的 loopback 檢查。
- 本 change 不動 `src/`、`tests/`、`docs/aicomm.md`、`skills/aicomm/SKILL.md`、其他 `docs/`（`docs/architecture/decisions.md` 的 D-25 除外，見 Impact）：那些是後續實作 PR。

## Impact

- 本 change 的產出：`openspec/changes/aicomm-wake-outcome/`（`proposal.md`、`design.md`、`tasks.md`、`specs/comm-skill/spec.md`、`.openspec.yaml`）與 `docs/architecture/decisions.md`（D-25）。
- 後續實作 PR 的落點（不在本 change，見 `tasks.md`）：`src/aicomm/cli.py`（含 D-7／F-1 的 409 錯誤碼修正）、`tests/unit/_aicomm_fakes.py`、`tests/unit/test_aicomm_send.py`、`tests/unit/test_aicomm_docs.py`、`docs/aicomm.md`、`skills/aicomm/SKILL.md`。
- 主 spec `openspec/specs/comm-skill/spec.md`：由本 change 的 delta 在歸檔時同步；Purpose 段加一句 `wake:` 行附 sidecar 紀錄 id（歸檔與同步主 spec 是 PM 的收尾）。
- 跨 repo（只列）：MyLinuxPool#786 的 sidecar 叫醒紀錄與 `pool-peer`；MyPMO 沒有整行比對 `wake:` 行（PM 經 PMO 確認），MyAiStorage 的 `src/pmo` 不解析 `wake:` 行（R-3，已解）。
- 公開 repo：不含帳號、機器位址、憑證、真的 session id、私人資訊。
