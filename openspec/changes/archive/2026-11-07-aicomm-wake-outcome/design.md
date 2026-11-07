# #406 設計：叫醒結果分「已叫醒／對方忙（排隊）／未叫醒」並附 sidecar 紀錄 id

- 單：MyAiStorage#406（c；期 6 M1「通訊看得見」）。配對單：MyLinuxPool#786（sidecar 每次 poke 留紀錄、唯讀查詢）。
- 作者：設計作者隊員（ollama-cloud/deepseek-v4.1-flash）；介面決定來自架構師的 `ARCH-406-interface.md`。
- 狀態：aicomm 側的決定已定；跨專案介面 R-1～R-4 已和 MLP 談定（契約版本 poke-records-v1，2026-11-07 02:40Z）。本單沒有待 MLP 確認的跨專案介面項目。

## 0. 結論

1. **三個種類不變，加一個修飾字**：`wake:` 行的種類仍是 `woken`／`not-woken`／`unknown`。「對方忙（排隊）」＝`woken` 加 ` queued`（`wake: <agent> woken local queued`）。「已叫醒」＝`woken` 不帶 `queued`；「未叫醒」＝`not-woken`。
2. **`--require-wake` 把 `queued` 算成叫醒**（exit 0）；exit 4 的條件不變（有人是 `not-woken` 或 `unknown`）。沒帶旗標時 exit 一律 0（D-24 不變）。
3. **紀錄 id 寫在行尾**：` record=<位置>/<id>[,<位置>/<id>…]`，位置是 `local` 或 `peer:<port>`。**沒有 id 時整段不寫**——沒有戳、戳逾時、或 sidecar 還是舊版。所以沒拿到 id 的 `wake:` 行和現在一個字都不差。
4. **id 與 `queued` 都從 poke 回應的 body 讀**：本機讀 HTTP body，跨機讀 `pool-peer call` stdout 第二行起的 body。兩條路徑共用 `_poke_answer` 那一張判讀表，不另開請求。
5. **aicomm 不加查詢子指令**，仍然只有 `send`／`inbox`／`log` 三個指令。對照紀錄由 sidecar 的唯讀端點負責；文件寫出兩條查法：本機用 `curl`，跨機用 `pool-peer call <port> GET …`。
6. **文件**：寫明「回報一律帶 `--to`，送完看 `wake:` 行」，以及各種結果之後要怎麼做；exit 0 仍然只代表已寫進 Angareion。
7. **對 sidecar 的需求**（給 MLP#786，已談定為 **poke-records-v1**）：sidecar 自己答的每一個 poke 回應都在頂層帶 `poke_id`、`poke_outcome`，另有唯讀的 `GET /pokes/{id}`。都是選用欄位，所以兩邊不必同時上線。
8. **另加一件**：D-7 修 409 錯誤碼讀法的現行 bug（F-1，MLP 指出、架構師核對）。

## 1. 現況（只讀，引用）

- `wake:` 行只在 `_Wake.line` 組（`src/aicomm/cli.py:264-267`）：`f"wake: {self.name} {self.kind} {self.code}{suffix}"`，suffix 只有 ` interrupt`。
- poke 回應的判讀表：`_poke_answer`（`src/aicomm/cli.py:313-325`），200 → `woken local`，**不讀 200 的 body**。
- 409 的錯誤碼：`_refusal_code`（`:292-310`）只讀頂層的 `code`，或頂層是字串的 `error`。sidecar 的錯誤 body 是巢狀的 `{"error": {"code": …, "message": …}}`（MLP `modules/sidecar/docs/sidecar.md:83`），所以真的 sidecar 回 409 時一律落成 `http-409`，`agent-not-running`／`awaiting-answer` 從來沒有對到（D-7／F-1）。
- 跨機：`_poke_peer`（`:443-466`）讀 `_call_answer`（`:426-440`）的 status 和 body，再交給同一張表。合併多台：`_merge_peers`（`:469-486`）。
- exit：`_cmd_send`（`:615-620`）的 `missed = missed or not result.woken`，`EXIT_NOT_WOKEN = 4`（`:38`）。
- sidecar 現況（MyLinuxPool `modules/sidecar/docs/sidecar.md` §2.2 poke，只讀）：「`POST /agents/{id}/poke` 一律回 **200** 與一般的 agent 狀態；沒有 `202`、沒有 `deferred`」；「為假時**不**打斷，agent 正在跑就排到這一輪之後」。所以「排隊」目前回應裡看不出來，要 sidecar 在回應中說明。
- 現有契約（不得無聲破壞）：`openspec/specs/comm-skill/spec.md:412-424`（文法與 regex）、`:433-438`（原因碼清單）、`:440-467`（exit 與 `--require-wake`）、`:205-210`（「MUST NOT 要求 sidecar 新增介面」，見 §5）、`:212-217`（只有三個指令）。消費端：`src/pmo/adapters.py:196-217`（`pmo` 只讀 stdout 第一行的訊息 id，不讀 `wake:` 行，也不帶 `--require-wake`）——不受影響。MyPMO（private）裡有沒有程式解析 `wake:` 行，已由 PM 經 PMO 確認沒有整行比對（§8 R-3 已解）。

## 2. 決定

### D-1 結果的種類：`woken` 加修飾字 ` queued`，不新增第四個種類

文法（取代 spec:412-424）：

```
wake: <agent> woken local[ interrupt| queued][ record=<records>]
wake: <agent> woken peer:<port>[,<port>…][ interrupt| queued][ record=<records>]
wake: <agent> not-woken <原因碼>[ record=<records>]
wake: <agent> unknown <原因碼>[ record=<records>]

<records> := <rec>[,<rec>…]
<rec>     := local/<id> | peer:<port>/<id>
<id>      := [A-Za-z0-9_-]{1,64}
```

regex（取代 spec:424 的那一條；2026-11-07 依審查 M-1 修正，用 `re.fullmatch` 比對）：

```
^wake: \S+ (woken (local|peer:\d+(,\d+)*)( interrupt| queued)?|(not-woken|unknown) [A-Za-z0-9_-]+)( record=(local|peer:\d+)/[A-Za-z0-9_-]{1,64}(,(local|peer:\d+)/[A-Za-z0-9_-]{1,64})*)?$
```

- **M-1 的修正**：
  - 舊版尾端的 `( .*)?` 接在整個 alternation 之後，`woken` 的行也吃得到。
  - 新版是封閉文法：修飾字最多一個，`record=` 只能出現一次而且格式固定，行尾之後不得再有任何東西。
  - 原因碼的字元集收成 `[A-Za-z0-9_-]+`：`state-<state>` 本來就以 `STATE_TOKEN_RE` 檢查過，`http-<碼>` 是數字。
- **取消 `<說明>` 欄位**（文法收緊，有標示；CLI 從沒印過）：
  - 欄位是自由文字，一旦允許，`record=local/a b` 就分不出是「id `a`，說明 `b`」還是壞掉的 id，C-5／C-11 也就驗不到壞 id。
  - 現行的 `src/aicomm/cli.py` 從來沒有印過 `<說明>`（`_Wake.line` 只組種類、碼與後綴），所以實際輸出不變。
  - 讀端若預期這裡可能有說明，也照樣能讀；只是以後 aicomm 不會印。文件（`docs/aicomm.md:33-36`、`skills/aicomm/SKILL.md:68-71`）與 spec 的文法要拿掉 `[ <說明>]`，另寫「每一行之後沒有其他文字」。
- **已用 Python 驗過**（架構師，2026-11-07；本設計作者以同一條 regex 重跑 C-1～C-12、C-15、C-17 與反例）：
  - 應匹配：C-1～C-12、C-15、C-17 的期望行，以及現行全部 15 個原因碼的形式（含 `woken peer:5201,5202 interrupt`）。
  - 應不匹配：`woken local interrupt queued`、`woken local queued interrupt`、`woken local bogus`、`woken local record=x/y`、`woken local record=local/a b`、65 字元的 id、`record=` 空值、`record=local/A,` 結尾逗號、`not-woken http-409 record=local/a b`、`queued local`、`woken` 單獨、`peer:5201,` 結尾逗號。
- **測試要用 `re.fullmatch`**（或 `re.match` 加上 `\Z`）：Python 的 `$` 會吃掉行尾的換行。

- 意思：
  - `woken`：sidecar 收下了，文字已送進對方的輸入。
  - `woken … queued`：對方正在跑，這則排在這一輪之後才會被處理（對方忙）。
  - `woken`（不帶 queued）而且有 `record=`：當下就送進去了。
  - `woken` 沒有 `record=`：舊版 sidecar，分不出是當下送進還是排隊。
- `interrupt` 與 `queued` 互斥：帶 interrupt 的戳不會排隊。sidecar 若同時回了 interrupt 和 `poke_outcome: "queued"`，以 ` interrupt` 為準，不印 ` queued`。
- 「對方忙」的另一種：對方在提問框裡（409 `agent_awaiting_answer`）。sidecar 對這種**不送、不排隊**，所以維持 `not-woken awaiting-answer`，不算 queued。文件要寫清楚兩者的差別。
- **理由**：(a) 三個種類和 `--require-wake`、D-24、原因碼清單都綁在一起，不改種類就不必動 exit 規則和清單測試（`tests/unit/test_aicomm_docs.py:262-276`）。(b) 排隊的時候文字其實已經交出去了，「該做的已做完、不要重送」這一點和 `woken` 相同。(c) 解析 `^wake: \S+ woken` 的舊程式照樣把它算成叫醒，不會誤判成失敗。
- **被否決**：
  - 新增第四個種類 `queued`（`wake: a queued local`）：每一個用 `woken` 判斷成功的消費端都會把排隊誤判成「沒叫醒」，`--require-wake` 也得另立規則。
  - 把 `queued` 塞進原因碼：原因碼只用在 `not-woken`／`unknown`（spec:433），語意不合。
  - 沿用 sidecar 已移除的 `deferred` 這個詞：sidecar 文件明說已拔除，再用會混淆。

### D-2 `--require-wake` 與 exit code

| 結果 | 沒帶旗標 | `--require-wake` |
|---|---|---|
| 全部 `woken`（含 `queued`） | 0 | 0 |
| 有一位 `not-woken` 或 `unknown` | 0 | 4 |
| 沒有人要叫醒（廣播、只寄給自己） | 0 | 0 |

- **理由**：exit code 回答的是「呼叫端還要不要做什麼」。排隊不需要再做什麼（重送會變兩份；要對方立刻看，應該是下一則用 `--interrupt`，這已經是 SKILL 的規矩）。「已叫醒」和「排隊」的差別只寫在 `wake:` 行。
- **被否決**：
  - (b) 排隊在 `--require-wake` 下回 4：每次寄給正在跑的人都會變成 4，會誘導重送。
  - (c) 新增 exit 5 表示「有人排隊、沒人失敗」：要再加一個碼和一套多收件人的優先序，換來的資訊 `wake:` 行已經有。單的原文寫的是「輸出與 exit code 區分」，這一條是否接受列在 §8 Q-1；PM 已接受本設計（見 proposal 的 Q-1 段）。
  - 沒帶旗標時就讓叫醒失敗變成非 0：會讓 `pmo` 的 `Aicomm.send`（`src/pmo/adapters.py:213`，非 0 就丟例外）把已經送達的信當成失敗，違反 D-24。

### D-3 紀錄 id 的來源與寫法

- **本機**：`POST /agents/<id>/poke` 的回應。**不論 HTTP 碼**，都讀頂層的 `poke_id`；200 另外讀 `poke_outcome`（§6 R-1 已談定）。錯誤 body 原本就會讀（`_refusal_code`，`:292-310`）。
- **跨機**：`pool-peer call` rc 0 時，stdout 第二行起的 body 和本機一樣讀，位置寫 `peer:<port>`。
- **檢查**：`poke_id` 要是字串而且完全符合 `[A-Za-z0-9_-]{1,64}` 才採用。不合格就當沒有（不印 `record=`），**不改變種類與原因碼**。這樣 sidecar 回應的內容就不可能讓一行變兩行，也不可能偽造欄位。`poke_outcome` 只認 `"queued"`；其他值（含錯誤回應的 `refused`／`failed`）或沒有這個欄位都不印 ` queued`。
- **多台**：一位收件人戳了好幾台時，`record=` 依 port 由小到大，列出**每一台有拿到合格 id 的戳**，失敗的那幾台也列（sidecar 那邊也記了，對照時用得上）。` queued` 只在「每一台成功的戳都回 `queued`」時才加。` interrupt` 照現行規則（spec:426-431）。
- **沒有 id 時不寫**：沒有戳（`state-*`、`not-found`、`bad-name`、`lookup-incomplete`、`peer-port-out-of-range`、本機 GET 的 `http-*`）、戳逾時或連線錯（`unknown poke-timeout|poke-error`，回應根本沒到）、舊版 sidecar。
- **理由**：`key=value` 放在行尾，舊的 parser 讀到的前段不變。只在有 id 時才寫，所以 `tests/unit/test_aicomm_send.py` 內 `wake:` 出現 81 次（含 `wake:` 的斷言行 66 行）裡，沒有 id 的情況不必改。
- **被否決**：
  - 一律寫 `record=-`：所有現有的 `wake:` 行都會變，舊 parser 若精確比對 `woken local` 整行就會壞。「沒有」的資訊從種類與原因碼本來就推得出來。
  - 把 id 印在 stdout：stdout 第一行是訊息 id 的契約（spec:8-9），而 `pmo` 只讀第一行。多加幾行雖然不會破壞 `pmo`，但 stdout 是給訊息 id 的，叫醒的結果一直都在 stderr。
  - aicomm 自己產生 id 再傳給 sidecar：違反「戳的 body 只有 `text` 與 `interrupt`」（spec:124-126）。sidecar 也記不住別人給的 id 是否唯一。

### D-4 查詢：aicomm 不加子指令

- 對照紀錄時，用 `record=` 裡的位置和 id 直接查 sidecar：
  - `local/<id>`：`curl -s 127.0.0.1:8471/pokes/<id>`
  - `peer:<port>/<id>`：`pool-peer call <port> GET /pokes/<id>`
- **理由**：(a) aicomm 是寄信的工具，查叫醒紀錄是 sidecar 的資料，巡檢（PMO 側）會直接讀 sidecar（MLP#786「巡檢依紀錄偵測」），aicomm 多包一層只是轉手（SRP）。(b) 「只有三個指令」是 spec:212-217 的契約，`aicomm status` 還被當成不認得的例子；改它要另外說理由，這張單沒有。(c) aicomm 對 sidecar 的請求維持兩種（spec:205-210 的 scenario 不變）。
- **被否決**：`aicomm wake-log <record>`。它會把 `pool-peer` 的 GET 也拉進 aicomm 的依賴面，所以等真的有 AI 經常要查時再開單（列為 §8 P-2）。

### D-5 文件措辭（`docs/aicomm.md`、`skills/aicomm/SKILL.md`）

必寫：

1. **回報一律帶 `--to`，送完看 stderr 的 `wake:` 行**：廣播不叫醒任何人。要對方動作的回報（完成、提問）必須帶 `--to`。
2. **exit 0 只代表已寫進 Angareion**（不變）。
3. **看到什麼怎麼做**：
   - `woken`：不用再做。
   - `woken … queued`：對方忙，這一輪結束就會看到。急的話下一則用 `--interrupt`，但仍然照軟規則，隊員不中斷 PM／PMO。
   - `not-woken`：信在，對方之後 inbox 讀得到。若需要對方動作，另外告知 PM／PMO（例如對方沒開席位）。
   - `unknown`：不要重送。
4. `record=` 的意思和兩條查法（D-4）。
5. 原因碼清單不變；`queued` 不是原因碼。

### D-7 修 409 的錯誤碼讀法（MLP 指出的現行 bug，F-1）

- sidecar 的錯誤 body 一向是巢狀的 `{"error": {"code": …, "message": …}}`（MLP `modules/sidecar/docs/sidecar.md:83`）。
- 但 `_refusal_code`（`src/aicomm/cli.py:292-310`）只讀頂層的 `code`，或頂層是字串的 `error`。所以真的 sidecar 回 409 時，一律落成 `http-409`，`agent-not-running` 和 `awaiting-answer` 從來沒有對到。
- 現有的測試與 spec（spec:362-366 的 scenario）用的都是平面形狀，所以沒抓到。
- 改法：先讀 `error.code`（`error` 是物件、`code` 是字串時），再照舊讀平面的形狀（相容）。
- **優先序的規則（審查 L-4 定案）**：
  - 依序找 `error.code`（巢狀）、頂層的 `code`、頂層字串的 `error`，**第一個是字串的就定案**。
  - 定案的值不認得時，結果是 `http-409`，**不再往下找**。
  - 三處都不是字串時，也是 `http-409`。
  - 理由：一個錯誤 body 只代表一個錯誤，讀到第一個字串就停，不會把兩種形狀的碼混在一起（和現行 `_refusal_code` 的 `break` 同一個精神）。sidecar 只會回一種形狀，混合的形狀只出現在壞掉或偽造的回應裡，這時保守地回 `http-409`。
- 這是 bug，不是介面變更。Q-2 由 PM 決定**併在 #406 一起改**：它改的是同一個 `_refusal_code`，而且 D-3 也要讀同一個錯誤 body，分開改反而會互相衝突。

## 3. 給實作的可驗收介面契約

每條都可以用 `tests/unit/_aicomm_fakes.py` 的假 sidecar 與假 `pool-peer` 驗證。下面的 `<ID>` 是合格的 id，例如 `01JX0000000000000000000000`。

- **C-1**：本機 GET 200 `idle`；poke 200，body 含 `"poke_id": "<ID>", "poke_outcome": "sent"` → stderr 恰為 `wake: <agent> woken local record=local/<ID>`，exit 0。
- **C-2**：本機 GET 200 `running`，沒帶 `--interrupt`；poke 200，`poke_outcome: "queued"` → `wake: <agent> woken local queued record=local/<ID>`。沒帶旗標 exit 0，帶 `--require-wake` 也是 exit 0。
- **C-3**：poke 200，body 沒有 `poke_id`／`poke_outcome`（舊 sidecar）→ `wake: <agent> woken local`（和現在一字不差）。
- **C-4**：GET 200 `running`，帶 `--interrupt`；poke 200，`poke_outcome` 是 `"sent"` 或 `"queued"` → `wake: <agent> woken local interrupt record=local/<ID>`（不印 ` queued`）。
- **C-5**：`poke_id` 不合格的情況：`"a b"`、`"x\nwake: y woken local"`、65 個字元、數字 `123`、`null` → 不印 `record=`，種類不變，每位收件人仍恰好一行。`poke_outcome` 是 `"later"`、`1` 或 `null` → 不印 ` queued`。
- **C-6**：poke 409，body `{"code": "agent_awaiting_answer", "poke_id": "<ID>"}` → `wake: <agent> not-woken awaiting-answer record=local/<ID>`。409 `agent_not_running`、503 `unavailable` 同理（後者是 `not-woken http-503 record=local/<ID>`）。
- **C-7**：跨機：`find` 回 5201 `running`；`call` rc 0，stdout `200\n{"poke_id":"<ID>","poke_outcome":"queued"}` → `wake: <agent> woken peer:5201 queued record=peer:5201/<ID>`。
- **C-8**：多台合併：5201 回 200 `sent` 加 `<A>`，5202 回 200 `queued` 加 `<B>` → `wake: <agent> woken peer:5201,5202 record=peer:5201/<A>,peer:5202/<B>`（不印 ` queued`）。兩台都回 `queued` → 印 ` queued`。5201 回 409 加 `<A>`、5202 回 200 `queued` 加 `<B>` → `woken peer:5202 queued record=peer:5201/<A>,peer:5202/<B>`。
- **C-9**：沒有戳就沒有 `record=`：`state-exited`、`not-found`、`bad-name`、`peer-unavailable`、`lookup-incomplete`、`peer-port-out-of-range` 這幾種，行和現在一字不差。
- **C-10**：戳逾時、連線錯、`call` rc 3／rc 2／約定外的 rc → 沒有 `record=`（`unknown poke-timeout` 等，和現在一字不差）。
- **C-11**：文法：多位收件人、結果各不相同時，stderr 每位恰好一行，每一行都以 `re.fullmatch` 符合 D-1 的 regex（M-1 修正版）。D-1 列出的反例都要寫成測試，確認**不匹配**：`interrupt` 與 `queued` 並列（`woken local interrupt queued`、`woken local queued interrupt record=local/x`）、`woken local bogus`、`record=x/y`、含空白的 id（`record=local/a b`）、65 字元的 id。
- **C-12**：`--require-wake`：全部是 `woken`（含 `queued`）→ 0；有一位 `not-woken`／`unknown` → 4。和 `--no-wake` 互斥 → 1，而且在寫入之前就拒絕（不變）。
- **C-13**：aicomm 對 sidecar 只發 `GET /agents/<id>` 與 `POST /agents/<id>/poke`（不發 `GET /pokes/…`）；對 `pool-peer` 只用 `find`、`call … POST …/poke`；poke 的 body 仍只有 `text` 與 `interrupt`；子指令仍只有三個。
- **C-14**：文件測試：`docs/aicomm.md` 與 `SKILL.md` 都含 `woken local queued`、`record=`、「回報一律帶 `--to`」、`GET /pokes/`（兩條查法），以及「`queued` 不要重送」。原因碼清單仍與常數相等（現有測試不變）。
- **C-15**（D-7）：本機或 `call` 的 poke 回 409 時，三種 body 的結果分別是：
  - `{"error": {"code": "agent_awaiting_answer", "message": "…"}}` → `not-woken awaiting-answer`
  - `{"error": {"code": "agent_not_running"}}` → `not-woken agent-not-running`
  - `{"error": {"code": "poke_timeout"}}` → `not-woken http-409`

  另外，平面形狀（`{"code": …}`、`{"error": "<字串>"}`）的結果和現在相同。

  優先序（L-4），以下都是 409：
  - `{"error": {"code": "weird"}, "code": "agent_not_running"}` → `not-woken http-409`：巢狀的碼是字串就定案，不認得也不往下找。
  - `{"error": {"code": 5}, "code": "agent_not_running"}` → `not-woken agent-not-running`：巢狀的碼不是字串，才往下找頂層的 `code`。
  - `{"error": {"message": "x"}}` → `not-woken http-409`。
- **C-16**（條件成立才做）：**不在本單範圍**；本 change 不把它寫進 spec delta 或 tasks。
- **C-17**：poke 回 404／500 時，body 帶合格的 `poke_id` → 原本的那一行（`not-woken http-404` 等）尾端加上 ` record=local/<ID>`。

## 4. 模組切割與依賴方向

- 只改 `src/aicomm/cli.py`，仍然只用標準函式庫，依賴方向不變：aicomm → angareion（指令）、sidecar（HTTP）、pool-peer（指令）。
- `_Wake` 增加兩個欄位，`queued: bool = False` 與 `records: tuple[str, ...] = ()`（每一項已經是 `local/<id>` 或 `peer:<port>/<id>`）。`line` 仍是唯一組字串的地方。
- `_poke_answer(status, body)` 改成同時回 id 和 outcome（例如回傳 `(kind, code, poke_id | None, queued: bool)`，或一個小的 NamedTuple）。本機和跨機共用，各自補上位置前綴。**不要**各寫一份 body 解析。
- `_merge_peers` 依 D-3 合併 `records` 和 `queued`。
- 新常數：`RECORD_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")`（用 fullmatch）、`POKE_OUTCOME_QUEUED = "queued"`、`WAKE_QUEUED = "queued"`。
- SOLID：
  - S：判讀 body 只在 `_poke_answer`，組行只在 `_Wake.line`。符合。
  - O：新欄位都是選用，舊回應照舊。符合。
  - L：不適用（沒有繼承）。
  - I：aicomm 只讀它要的兩個欄位，不讀 agent 狀態的其他欄位。符合。
  - D：依賴的是 sidecar 的 HTTP 文件形狀，不 import MLP 的程式。符合。

## 5. 對現有契約的影響清單

| 位置 | 改什麼 | 相容性 |
|---|---|---|
| `openspec/specs/comm-skill/spec.md:412-424` | 文法與 regex 換成 D-1 | 舊行仍符合新 regex；新行（`queued`／`record=`）不符合舊 regex。**這是有標示的擴充** |
| `spec.md:359-376`（戳的回應判讀） | 加讀任何 HTTP 碼的 `poke_id`，200 的 `poke_outcome`；修 409 巢狀 `error.code` 的讀法（D-7） | 舊回應結果不變；平面形狀照舊可讀 |
| `spec.md:426-431`（多台合併） | 加 records 與 queued 的合併規則 | `woken`／`not-woken`／`unknown` 的取法不變 |
| `spec.md:440-467`（exit） | 加「`queued` 算 `woken`」一句與 scenario | 不變 |
| `spec.md:205-210`（「MUST NOT 要求 sidecar 新增介面」） | 要改成：「aicomm 只**使用**查 agent 與戳兩種請求；**MAY 讀**戳回應中的選用欄位 `poke_id`／`poke_outcome`（MyLinuxPool#786）；欄位缺少時行為與舊版相同」 | **要求文字改了**，等於撤回一條既有原則，要在 proposal 寫明 |
| `spec.md:4`（總述） | 加一句「`wake:` 行附 sidecar 紀錄 id」 | — |
| `src/aicomm/cli.py:247-270, 292-310, 313-325, 443-486, 551-563` | 見 §4；`_refusal_code` 先讀 `error.code`（D-7） | — |
| `docs/aicomm.md:27-46, 83-96, 98-111` | 文法、依賴面的表（加 body 欄位）、跨機、D-5 的措辭 | — |
| `skills/aicomm/SKILL.md:62-90` | 同上；另加「回報一律帶 `--to`」 | — |
| `tests/unit/test_aicomm_docs.py:278-285` | `test_c18` 加 `queued`／`record=` 的形式 | 不變的斷言不動 |
| `tests/unit/test_aicomm_send.py` | 新增 C-1～C-13、C-15、C-17 | `test_aicomm_send.py` 內 `wake:` 出現 81 次（含 `wake:` 的斷言行 66 行）不必改（假 sidecar 預設不回 id） |
| `docs/architecture/decisions.md` | 新增 D-25（本檔 D-1～D-4、D-7 的摘要），D-24 註明「由 D-25 擴充」 | 設計 PR 一起交 |
| `docs/architecture/overview.md:5` | 不在這張單補 aicomm 的模組地圖（範圍外，維持「之後再補」） | — |

## 6. 跨專案介面（給 MyLinuxPool#786）——契約版本 poke-records-v1

### 6.0 談定狀態

- 2026-11-07 02:33Z 寄 MLP 的架構師：`wake: … not-woken not-found`，pool 裡沒有這個席位。照 BRIEF 改寄 MLP 的 PM（`woken peer:5307`）。
- 02:38Z MLP#786 的架構師（claude-opus-5-5）回覆：R-1～R-4 全部接受，有三處小改。
- 02:40Z 本專案架構師回覆「R-1～R-4 照修改版談定」。MLP 會把下面的內容寫成 #786 設計的契約版本 **poke-records-v1**。
- R-1～R-4 都已談定，本單沒有待 MLP 確認的跨專案介面項目。

### R-1 poke 回應（談定）

- sidecar **自己答的每一個 poke 回應**都在頂層帶 `poke_id` 與 `poke_outcome`：200、400、404、409、413、503、500。
- `poke_outcome` 的值：
  - 200 是 `sent` 或 `queued`。
  - 錯誤是 `refused`（4xx）或 `failed`（5xx）。
- `sent` 與 `queued` 的判定：
  - `queued`：送出了，而且送之前對方是 running，而且沒帶 interrupt。
  - `sent`：送出了，而且送之前對方是 idle 或 starting（含 starting 排隊之後才送出的），或帶了 interrupt。
- 200 的 body 仍是 agent 狀態。錯誤 body 原本的 `{"error": {"code", "message"}}` 不變。
- 欄位沒有，就代表是舊版 sidecar。
- aicomm 這邊的對應（修 D-3）：
  - **任何 HTTP 碼**的回應，只要 `poke_id` 合格就印 `record=`。
  - `poke_outcome` 只拿 `queued` 來判斷。
  - 其他的鍵（包括紀錄裡細的 `result`）都忽略，aicomm 不依賴。

### R-2 id 格式（談定）

- `pk-` 加 26 字 Crockford ULID，符合 aicomm 的 `^[A-Za-z0-9_-]{1,64}$`。
- 契約只保證在一台 sidecar 內唯一；實際上隨機段有 80 bit，跨台也不會撞。
- 不含 agent 名，不含 text。

### R-3 跨機（談定，MLP 已讀 pool-peer 的 `cmd_call` 確認）

- stdout 第一行是 status，之後是原樣的 body，pool-peer 不改寫。
- path 只要求以 `/` 開頭，METHOD 照給的送。
- GET 時給空的 stdin，就不會帶 body。

### R-4 唯讀查詢（談定）

`GET /pokes/{id}` 回整筆紀錄，含 `id`、`agent`、`at`、`interrupt`、`outcome`（`sent|queued|refused|failed`）、`error`（碼或 null）、`confirmed_at`、`unconfirmed_at`。另外還有其他鍵，看不懂的就忽略。找不到回 404 `poke_not_found`。不回 text。

`confirmed_at`／`unconfirmed_at` 的限制：

- 只有 claude tui 送給 idle 或 starting 的輸入才會有值，其他一律 null。
- 要等 #786 的第 3 個 PR 上線才有值，之前恆為 null。
- 所以文件不得把 `woken` 寫成「已讀」（§8 R-2）。

保存：

- 記憶體：每台至少最新 1 萬筆，`GET /pokes/{id}` 查這裡，估計可保存數天，但不保證。
- 磁碟：最多 4 個 4 MiB 的 JSONL（約 4 萬筆）。
- sidecar 重啟會保留；容器重建會清空。

巡檢用的列表：`GET /pokes?agent=&since=&result=&limit=&before=`。aicomm 不用它。

## 7. 測試策略

- **層級**：C-1～C-13、C-15、C-17 全部是單元測試，放在 `tests/unit/test_aicomm_send.py`，用現有的假 sidecar（`tests/unit/_aicomm_fakes.py` 的 `RawSidecar`／`world`）與假 `pool-peer`（`AICOMM_POOL_PEER`）：
  - 假 sidecar 加一個可設的 poke body（帶或不帶 `poke_id`／`poke_outcome`／錯誤 body，含巢狀 `error.code`）。預設不帶，所以現有測試不動。
  - C-14 放在 `tests/unit/test_aicomm_docs.py`。
- **替身**：不連真的 sidecar、不跑真的 `pool-peer`（照 AGENTS.md 的測試隔離）。
- **跨專案對照**（MLP 上線之後）：拿 MLP 的 sidecar 測試或文件裡的真實回應樣本，做一份 fixture，餵給 `_poke_answer`，確保欄位名與巢狀錯誤碼一致。這份 fixture 在 MLP 的 sidecar 實作上線後補。
- **驗收門檻**由 PM 定。真機驗證（24 小時都查得到）是 M1 收束的事，由 PM／PMO 排，不在這張單的單元測試裡。

## 8. 風險與未決

### 需要 PM 決定

- **Q-1（單的措辭）**：單寫的是「輸出與 exit code 區分已叫醒／對方忙（排隊）／未叫醒」。D-2 的設計是：exit code 只分「要不要再做什麼」（排隊算成功），三者的差別寫在 `wake:` 行。
  - **建議接受**，理由見 D-2。
  - 若 PM 或本人堅持 exit 也要分出排隊，次選是在 `--require-wake` 下新增 exit 5，代表「沒有人失敗，但有人排隊」。這要改 spec:440 和 SKILL 的結束碼表，架構師可以另出設計。
  - 這是語意與需求的問題，以 PM 為準；若 PM 認為已偏離單的原意，交 PMO。
  - **PM 決定：Q-1 接受**；PMO 無異議（2026-11-07），差異與理由見 proposal。
- **P-1（作者提議，PM 未指示）**：廣播（沒有 `--to`／`--mention`、也沒有 `--no-wake`）時，在 stderr 印一行 `aicomm: 廣播不叫醒任何人；要對方動作請帶 --to`。這可以替 D-5 第 1 點把關。代價是正當的廣播也會多一行提示。這條是行為，PM 決定要不要做，**不納入本 change**（C-1～C-17 沒有它）。
- **P-2**：`aicomm` 的唯讀查詢子指令留到以後（D-4）。
- **Q-2（D-7／F-1 要不要併進 #406）**：架構師建議併進。**PM 決定併進本單**：它改的是同一個 `_refusal_code`，而且 D-3 也要讀同一個錯誤 body，分開改反而會互相衝突。

### 需要 MLP 回覆

- 無；R-1～R-4 已談定（poke-records-v1）。

### 風險

- **R-1**：`spec.md:205-210` 原則上「不要求 sidecar 新增介面」，本單等於有意識地撤回這條。proposal 要寫明，避免審查時被當成違規。
- **R-2**：`poke_outcome: "sent"` 只代表 sidecar 當下送出了，**不代表對方已讀**（tui 的 Enter 可能被吃掉，見 sidecar 文件的 `input_unconfirmed_at`）。「被讀」只能用 `GET /pokes/{id}` 的 `confirmed_at` 查。文件不得把 `woken` 寫成「已讀」。
- **R-3**：已解：PM 經 PMO 確認 MyPMO 沒有整行比對，MyAiStorage 的 `src/pmo` 不解析 `wake:` 行。
- **R-4**：跨機時 pool-peer 若截斷或改寫 body，id 會在 C-5 的檢查被丟掉。**R-3 已由 MLP 確認 pool-peer 不改寫 body**；剩下的風險是 sidecar 或 pool-peer 實作與契約有出入，上線後用真實樣本補 fixture 對照。

### 未決（不在本單）

- D-6（poke 帶 `via`／`ref`）：MLP 已答：舊 sidecar 忽略不認得的鍵；PM 決定不在本單，另開增補單（建議見架構師交接檔）。
