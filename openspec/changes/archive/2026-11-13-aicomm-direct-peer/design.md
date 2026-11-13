# #457 設計：aicomm 的跨機叫醒改直連本機的 sidecar-link 埠（pool-peer 退役 MAS-1～3）

- 單：MyAiStorage#457（c）。上游：MyLinuxPool#1085（sidecar-link；pool-peer 兩階段退役，本人 2026-11-13 同意的方向）。本 note 只寫 **aicomm 這一側**。
- 作者：設計作者（核准者模式）ollama-cloud/deepseek-v4.1-flash，2026-11-13；由專案架構師審。
- 開工依據：PMO 派工（2026-11-13T00:06Z）。介面只寫「MyLinuxPool 保證的介面」：兩個檔案的路徑與格式，以及「`127.0.0.1:P` 是某台的 sidecar」；內部設計一律引 MyLinuxPool#1085，不在本 repo 轉述。
- 本 change 只寫設計文件與 spec delta：不改 `src/`、`tests/`、`docs/aicomm.md`、`skills/aicomm/SKILL.md`、`openspec/specs/`，不改 `pool-peer`（那是 MyLinuxPool 的）。

## 1. 現況盤點（程式碼證明）

### 1.1 本機路徑（不變的部分）

`src/aicomm/cli.py`：

- 位址：`DEFAULT_SIDECAR = "http://127.0.0.1:8471"`（`:42`）、測試覆寫 `AICOMM_SIDECAR`（`:43`）、`sidecar_base()` 只收單一 loopback 位址（`:140-165`）。
- 逾時：`GET_TIMEOUT = 3.0`（`:45`）、`POKE_TIMEOUT = 5.0`（`:46`）。
- `_wake_one_inner()`（`:629-664`）：先 `GET /agents/<id>`；200 且 `state` 是 `idle`／`running` 才戳；404、連不上或 GET 逾時才走 `_wake_elsewhere()`（`:636-641`）；本機戳之後不論結果都不再查別台。
- 戳的 body 由 `_poke_body()` 組（`:411-416`）：`text`（固定句，`:65`）、`interrupt`、`via`（本機 `aicomm`、跨機 `aicomm-peer`，`:67-68`）、選用 `ref`（`:71`）。
- 戳的回應由 `_poke_answer()` 判讀（`:386-402`），409 的錯誤碼由 `_refusal_code()`／`_error_code()` 讀（`:342-374`）。

### 1.2 跨機路徑（要換掉的部分，全部在 `pool-peer`）

- 進入條件：`_wake_elsewhere()`（`:587-613`），只在 `_wake_one_inner()` 的本機 GET 回 404、連不上或逾時時被呼叫（`:636-641`）。
- 找執行檔：`_peer_executable()`（`:419-441`）：`AICOMM_POOL_PEER`（`:52`，測試與手動用）→ PATH 的 `pool-peer`（`:53`、`:429`）→ `~/.mylinuxpool/bin/pool-peer`（存在／可執行檢查 `:436-438`；`Path.home()` 的 `RuntimeError`／`OSError` 當沒有，`:432-440`）。都沒有 → `not-woken peer-unavailable`（`:597-599`）。
- 查詢：`_lookup_peers()`（`:495-515`）跑 `pool-peer find <name> --json --timeout 10`（`:497`），aicomm 對子行程多等 2 秒（`PEER_CHILD_GRACE = 2`，`:56`；`_peer_child_limit()`，`:444-446`），用 `_parse_peers()`（`:463-492`）讀 JSON 陣列，只取每一筆的 `port`（整數、不是 bool）與 `agent.state`（字串），同一個 port 只留第一筆，一筆可用的都沒有 → `peer-bad-answer`。
- 結束碼對照（`_lookup_peers()`，`:495-515`）：rc 0 → 用陣列；rc 1 → `not-woken not-found`；rc 2 → `not-woken peer-unavailable`；rc 3 → `not-woken lookup-incomplete`；`subprocess.TimeoutExpired` → `lookup-incomplete`；`OSError`（子行程沒起來）→ `peer-unavailable`；其他 rc → `not-woken peer-bad-answer`。
- 逐台戳：`_wake_elsewhere()`（`:604-613`）對每一台判斷：`state` 不是 `idle`／`running` → `not-woken state-<state>`（不是一個 token 時 `peer-bad-answer`，`:606-608`）；port 不在 `PEER_PORT_MIN..PEER_PORT_MAX`（5200–5399，`:59-60`）→ `not-woken peer-port-out-of-range`，不呼叫（`:609-610`）；其他呼叫 `_poke_peer()`（`:535-561`）：`pool-peer call <port> POST /agents/<quoted>/poke --timeout 5`（`:540`）、stdin 是同一個 poke body（`:538-542`），`call` 的 rc 2 → `not-woken peer-unavailable`、rc 3／子行程逾時 → `unknown poke-timeout`、其他 rc → `unknown peer-bad-answer`（`:543-552`），rc 0 時 `_call_answer()`（`:518-532`）把 stdout 第一行當 HTTP 狀態碼、其後當 body，再用與本機同一張表判讀（`:553-561`）。
- 合併：`_merge_peers()`（`:564-584`）：有一台 200 就是 `woken peer:<port>[,<port>…]`（成功的那幾個 port，由小到大）；否則有一台 `unknown` 就是 `unknown`；都不是就是 `not-woken`（原因碼取 port 由小到大的第一個）；`record=` 依 port 由小到大列出每一台有合格 `poke_id` 的戳。
- `wake:` 行的 `peer:<port>`：`WAKE_LOCAL`／`peer:<port>`（`:118`、`:559-560`、`:578-580`），行由 `_Wake.line` 組（`:287-297`）。
- 名字以 `-` 開頭：`_wake_elsewhere()` 一開始就拒絕（`:594-596`，`not-woken bad-name`），理由是名字會被 `pool-peer` 當成選項。
- 原因碼常數（`:76-89`）與兩組集合（`NOT_WOKEN_CODES` `:93-105`、`UNKNOWN_CODES` `:106-111`）。

### 1.3 今天的時間預算

| 階段 | 常數 | 位置 |
|---|---|---|
| 本機查詢 | 3 秒 | `:45` |
| 本機戳 | 5 秒 | `:46` |
| `find` | 10 秒，子行程再等 2 秒 | `:54`、`:56`、`:497` |
| `call` | 5 秒，子行程再等 2 秒 | `:55`、`:56`、`:540` |
| 收件人 | 一個一個處理 | `_cmd_send()` `:719-722` |

### 1.4 文件與規格提到 `pool-peer` 的地方（相關行，含所在段落）

- `docs/aicomm.md`：`:9`、`:43`、`:47`、`:52`、`:59`、`:64`、`:66`、`:68`、`:114`（整節「對 `pool-peer` 的依賴面」，`:114-127`）、`:116`、`:120`、`:121`、`:123`、`:124`、`:125`、`:126`、`:127`。
- `skills/aicomm/SKILL.md`：`:8`、`:56`、`:85`、`:86`、`:87`、`:95`、`:111`。
- `openspec/specs/comm-skill/spec.md`：Purpose `:4`；requirement 內文／scenario `:9`、`:12`、`:17`、`:48`、`:50`、`:54`、`:82`、`:86`、`:90`、`:94`、`:98`、`:101`、`:105`、`:110`、`:114`、`:118`、`:125`、`:137`、`:173`、`:177`、`:182`、`:186`、`:197`、`:201`、`:205`、`:216`、`:220`、`:253`、`:254`、`:313-323`（requirement）、`:324-346`（requirement）、`:347-353`、`:354-372`、`:373-379`、`:380-402`、`:403-404`、`:410`、`:469`、`:473`、`:475-497`、`:510`、`:524-542`、`:543-549`、`:551`、`:553`、`:569`。
- `docs/architecture/decisions.md`：D-24（`:32`）與 D-25（`:33`）的跨機路徑與查紀錄用法；本 change 不在路徑清單內動它（見 §8 U-1）。
- 其他（不是本 change 的落點）：`docs/design-notes/368-aicomm-wake.md`、`openspec/changes/archive/2026-11-04-aicomm-cross-machine-wake/`、`2026-11-07-aicomm-poke-ref/`、`2026-11-07-aicomm-wake-outcome/` 是歷史文件，歸檔時不改。

### 1.5 測試現況

- 假 `pool-peer`：`tests/unit/_aicomm_fakes.py:74-119`（`_FAKE_POOL_PEER`、`install_fake_pool_peer()`），答案來自 `FAKE_PEER_SCENARIO`（`:74-112`），呼叫記在 `FAKE_PEER_LOG`（`:86-87`）；`pool_peer_calls()`／`assert_only_pool_peer_calls()`（`:127-151`）在 fixture 收尾時檢查 argv 只有 `find`／`call … poke`（`:461-464`）。
- 護欄：`_path_peer_is_test_only()`（`:349-389`）與 `run_aicomm()` 的 assert（`:392-410`）確保測試不會跑到真的 `pool-peer`（worker 的 PATH 上有一個）。
- 測試家族：`tests/unit/test_aicomm_send.py:914` 起是整個「#368 PR-2：pool-peer 後援」段，另有 `:1535`、`:1574`、`:1620-1800`、`:1948`、`:2061-2213` 的跨機樣本；`tests/unit/test_aicomm_docs.py` 檢查文件寫到的查紀錄用法；`tests/unit/test_aicomm_fakes_guard.py` 檢查護欄本身。
- `tests/unit/test_angareion_log_e2e.py:121` 也設了 `AICOMM_POOL_PEER`（`/nonexistent/pool-peer`），實作 PR 一起拿掉。

## 2. aicomm 依賴的介面（MyLinuxPool 保證，MyLinuxPool#1085）

只列 aicomm 會用到的部分；不轉述內部設計。

| 項目 | 保證 |
|---|---|
| 目錄 | `~/.mylinuxpool/sidecar-link/` |
| 心跳 | 檔案 `heartbeat`；內容是寫入的那個行程的 pid；mtime＝最近一輪 |
| 「新鮮」 | mtime 在 ±`LINK_FRESH_SECONDS`（90 秒，絕對值）內**而且**檔內 pid 還活著 |
| 埠清單 | 檔案 `ports`；每行一個十進位埠、遞增、不重複；只列「本機 `127.0.0.1:P` 現在連得到某台 sidecar」的埠 |
| 目標 | 對清單裡的每個 `P`，`127.0.0.1:P` 是**某台** sidecar，有 sidecar 的兩條路由 |

不保證、aicomm 不依賴的：哪一台、清單什麼時候變、有幾台、埠清單會不會含本機自己的埠、多久寫一次（只依賴「新鮮」的定義）。

### 2.1 aicomm 依賴的環境假設（M-3；MLP 已確認）

除了上面兩個檔的格式，本設計還依賴兩個**寫不進檔案格式**的環境假設。它們不成立時的症狀是「每一台都靜靜地查不了」（`peer-unavailable`），單元測試抓不到，所以列在這裡。**MLP 已確認（2026-11-13，MyLinuxPool#1085 的設計者，經 PMO 轉）**；未驗的部分寫在各列的「狀態」欄。

| # | 假設 | 不成立時的症狀 | 狀態 |
|---|---|---|---|
| A-1 | aicomm（sidecar 開的 agent 子行程）與寫 `heartbeat` 的行程在**同一個容器、同一個 pid namespace、同一個 uid**（否則 `os.kill(pid, 0)` 會是 `ProcessLookupError`，或 `PermissionError` 的判斷失去意義；心跳可能一律不新鮮） | 每一台都 `not-woken peer-unavailable` | MLP 已確認；**未驗**：從容器外以 `docker exec` 跑 aicomm |
| A-2 | aicomm 行程的 `HOME` 與寫入者的 `~` 是同一個；`127.0.0.1:P` 在**容器的 network namespace**，agent 連得到（否則讀不到目錄，或連不上／連到別的服務） | 每一台都 `not-woken peer-unavailable`／`lookup-incomplete` | MLP 已確認（2026-11-13，MyLinuxPool#1085） |

`tasks.md` 3.1 的真機驗收仍然要做：它驗的是**實際行為**（從 sidecar 開的 agent 裡，`wake:` 真的是 `woken peer:<port>`、心跳判斷真的為真），不是介面承諾。A-1 的「容器外 `docker exec`」不在 3.1 的範圍（aicomm 的正常情境是在 agent 子行程裡）。

## 3. 新行為（MAS-1～3）

### 3.1 順序（不變）

1. 先問本機 sidecar（`sidecar_base()`，預設 `http://127.0.0.1:8471`）。
2. 只有本機回 404、連不上或 GET 逾時，才查埠清單；這時還沒戳任何人。
3. 本機戳過之後（不論成敗）不再查別台。
4. 收件人一個一個處理；每位收件人剛好一行 `wake:`。

### 3.2 心跳與埠清單怎麼讀（MAS-2）

1. 目錄＝`AICOMM_SIDECAR_LINK`（只給測試與手動用，見 §4.3）或 `Path.home()/".mylinuxpool"/"sidecar-link"`。`Path.home()` 丟 `RuntimeError`／`OSError` → 當作沒有目錄（＝查不了，`peer-unavailable`）。`AICOMM_SIDECAR_LINK` 設了但是**空字串**或**不是絕對路徑** → 一樣當作沒有目錄（`peer-unavailable`）；不是用法錯誤——它在送達之後才讀，不能改 exit code，而且它是測試鉤子（契約 C-9）。
2. 心跳：「新鮮」＝`age = time.time() - st_mtime` 落在 `[-LINK_FRESH_SECONDS, LINK_FRESH_SECONDS]`（`LINK_FRESH_SECONDS = 90`）**而且**檔內 pid 還活著。四種不新鮮：(a) 讀不到（不存在、不是普通檔、沒有權限、IO 錯）；(b) mtime 超出窗（含遠在未來：時鐘跳動不該讓一個檔永遠新鮮）；(c) pid 讀不懂——去掉頭尾空白後不符合 `^[1-9][0-9]*$`，或大於 2147483647（`os.kill` 收不下的 pid 會丟 `OverflowError`，**不是** `OSError`）；(d) pid 不在（`os.kill(pid, 0)` 的 `ProcessLookupError`；`PermissionError` 算活著，其他 `OSError` 算不在）。**永遠不把 0 或負數交給 `os.kill`**（0 會送給整個 process group），所以先用上面的樣式擋。
3. `ports`：讀不到 → 查不了。解碼成 UTF-8（解不開＝清單讀不懂＝`peer-bad-answer`）。只允許最後一個換行造成的一個空字串：`ports` 去掉末尾一個換行後是空的，就算**空檔**（＝查不了＝`peer-unavailable`），不當成「一個空行」；其他空行＝格式不對。每一行必須是 `^[0-9]{1,5}$`（格式不對＝清單讀不懂），數值必須在 1–65535（是整數但超出範圍＝埠號不合法）。重複的埠只留第一次出現的那一筆（與今天「同一個 port 只戳一次」相同），處理時由小到大排序（檔案保證遞增；就算不是，結果也一樣）。
4. **清單層級的語意（定死，M-2）**：
   - **有一行不合格＝整份清單不用**：這台機器上所有要走跨機後援的收件人都拿到同一個原因碼，不會「跳過壞行、用剩下的」。這和今天的 `peer-port-out-of-range` 不同（今天是**逐台**：只有找到那台、而且那台可戳時才出現）；這是刻意的大聲失敗（§7）。
   - **優先序＝依檔案順序，第一個不合格的行決定**：同一份清單裡同時有 `0` 與 `52x1` 時，先出現的那一個決定是 `peer-port-out-of-range` 還是 `peer-bad-answer`。非 UTF-8 是整檔解不開、沒有行可言，一律 `peer-bad-answer`。
   - **扣掉本機埠之後沒有埠＝查不了＝`peer-unavailable`**（與空檔同一個結果）。這修掉「清單只有本機那個埠」時的空真（vacuous truth）：0 台時「每一台都 404／都確定沒有」也成立，但一台都沒查，說 `not-found` 就是謊稱。
5. 本機埠的算法（契約）：`urllib.parse.urlsplit(sidecar_base(...)).port`；沒有埠（例如 `http://127.0.0.1`）用 **80**。只比**埠號**、不比主機：目標一律 `127.0.0.1`，而 `sidecar_base()` 允許 `localhost`／`127.x`／`::1`，比主機沒有意義。
6. 「查不了」一律是 **`not-woken peer-unavailable`**（MAS-2；與今天 `pool-peer` rc 2 同一個語意），**不改走別的路徑**：不退回 `pool-peer`、不讀別的檔、不試非 loopback 位址、不加快取。
7. 每次後援都重新讀這兩個檔（不跨收件人快取；檔在兩次之間變了就以新的為準）。

### 3.3 每個埠怎麼查（MAS-1）

對清單裡的每個 `P`（跳過與本機 sidecar 同一個埠，見 §4.3）：`GET http://127.0.0.1:P/agents/<url-quoted-id>`，逾時見 §3.6，結果分四類：

| 結果 | 條件 | 意思 |
|---|---|---|
| 不在這台 | 404 | 這台確定沒有 |
| 有回答 | 200 且 body 是 JSON 物件、`state` 是字串 | 這台有這個 id，`state` 是它報的狀態 |
| 讀不懂 | 200 但 body／`state` 讀不懂 | 這台有回答，但我們讀不出狀態 |
| 查不到 | 連不上、逾時、HTTP 層讀不懂、或 200／404 以外的狀態碼、或查詢預算用完 | 這台沒給出可用的回答 |

### 3.4 每一個「有回答」的埠怎麼戳

與本機同一套規則：

- `state` 是 `idle`／`running` → 戳：`POST http://127.0.0.1:P/agents/<url-quoted-id>/poke`，body 與本機**同一個** `_poke_body()`，只差 `via`（`aicomm-peer`）。
- `state` 是其他值 → 不戳，`not-woken state-<state>`；`state` 不是一個 token（`[A-Za-z0-9_-]+`）→ 不戳，`not-woken peer-bad-answer`。
- 「讀不懂」→ 不戳，`not-woken peer-bad-answer`（與今天「find 的陣列裡那一筆讀不懂」同一個碼）。
- 能戳的每一台都戳（MAS-3：同一個 id 在兩台就兩台都戳）；每一台的 `interrupt` 各自判斷（只有那一台是 `running` 且呼叫端帶 `--interrupt` 才是 true）。
- 戳的傳輸結果（見 §4.1）：逾時 → `unknown poke-timeout`；連線被拒 → `not-woken peer-unavailable`；其他連線錯誤（被重置等）→ `unknown poke-error`；HTTP 層讀不懂 → `unknown peer-bad-answer`。
- 戳的 HTTP 回應（含 200／409／其他碼、`poke_id`、`poke_outcome`）用**同一個** `_poke_answer()`／`_refusal_code()` 判讀，`record=` 的位置是 `peer:<port>`。**不重送**。

### 3.5 合併成一行（MAS-3）

「有回答」與「讀不懂」的埠構成答案清單（404 與「查不到」的埠不列入，與今天 find 只列找到的機器相同）：

- 扣掉本機埠之後一個埠都不剩：**查不了**，在 §3.2 就回 `not-woken peer-unavailable`，不會走到合併（空集合不能說成「每一台都確定沒有」）。
- 至少查過一個埠，而「答案清單」是空的（查過的埠全是 404 或「查不到」）：**所有**查過的埠都是「不在這台」→ `not-woken not-found`；否則（至少一個「查不到」）→ `not-woken lookup-incomplete`。這保住今天的兩個語意：`not-found` 只在「每一台都確定沒有」時說；有查不到的台就說不知道。
- 「答案清單」不是空的（至少一台有回答或讀不懂）→ 照今天的 `_merge_peers()` 規則：有一台 200 就是 `woken peer:<port>[,<port>…]`（由小到大，至少一台成功的戳帶 interrupt 才加 ` interrupt`），否則有一台 `unknown` 就是 `unknown`，都不是就是 `not-woken`（原因碼取 port 由小到大的第一個）；`record=` 依 port 由小到大。

### 3.6 逾時與預算

| 階段 | 新做法 | 理由 |
|---|---|---|
| 本機查詢 | 3 秒（`GET_TIMEOUT = 3.0`，不變） | 不動本機 |
| 本機戳 | 5 秒（`POKE_TIMEOUT = 5.0`，不變） | 不動本機 |
| 查詢清單 | **總預算 `PEER_LOOKUP_BUDGET = 10.0` 秒**；每個埠的請求逾時＝`min(PEER_GET_TIMEOUT = 3.0, 剩下的預算)`；預算用完還沒查的埠算「查不到」；預算與已用時間用 `time.monotonic()` 計（不受牆上時鐘跳動影響） | 今天的 `find` 是整個 pool 10 秒；清單變長時總時間不跟著長。健康的 loopback 查詢是毫秒級，10 秒夠幾十台；用完就說 `lookup-incomplete`，不會謊稱 `not-found` |
| 戳 | 每一台 `PEER_POKE_TIMEOUT = 5.0` 秒，沒有另外的總預算 | 與今天每台 `call` 5 秒同量級（今天另有 2 秒子行程寬限，換成直連後不需要） |
| 心跳的 age | `time.time() - st_mtime`（mtime 是牆上時鐘）與 `LINK_FRESH_SECONDS = 90` 比 | age 不能用 monotonic：mtime 不是 monotonic 的時鐘 |

常數都要有名字（`LINK_FRESH_SECONDS`、`PEER_LOOKUP_BUDGET`、`PEER_GET_TIMEOUT`、`PEER_POKE_TIMEOUT`），文件與測試引用常數，不寫裸數字。

依埠號由小到大**依序**查（不合併、不平行）：前面的埠卡住（接受連線但不回應）時，每一台吃滿 3 秒，四台就用完 10 秒預算，排在後面的埠一律算「查不到」——結果是 `lookup-incomplete`，是誠實的（沒有謊稱 `not-found`）。平行查詢留到量過再說，本 change 不做。

最壞情況：本機 3＋5 秒，跨機 10 秒查詢＋「有回答且能戳的台數 × 5 秒」。今天最壞是 10＋2 秒查詢＋台數 ×(5＋2) 秒，新做法不會更久。收件人仍然一個一個處理，總時間隨收件人數線性增加（與今天相同）。

### 3.7 只打兩條路由、輸出與安全

- 對**每一台** sidecar 只發兩種請求：`GET /agents/<id>`、`POST /agents/<id>/poke`。不發 `GET /pokes/…`、不發 `/agents`（清單）、不發任何其他路由。
- 目標一律是 `127.0.0.1:P`；不連非 loopback 位址；`_OPENER` 已關掉 proxy（`:245`），照用。
- poke 的 body 只有 `text`（固定句）、`interrupt`、`via`、選用 `ref`；不含寄件人、內文、工單號。
- 輸出只放 `wake:` 行：種類、`peer:<port>`、驗證過的原因碼／`state` token、` interrupt`／` queued`、驗證過的 `record=peer:<port>/<id>`。**不轉印任何回應 body**，也不把清單內容、機器位址、埠以外的東西寫進 stderr／stdout。
- 清單是本機檔案，aicomm 只把它當「一串埠」：就算檔案被亂寫，最多是對某個 loopback 服務送出上面那兩個固定請求；回應裡除了驗證過的 token 不會有任何東西被採信或印出。

### 3.8 模組切割與依賴（M-1）

新程式切成兩個模組，邊界就是「讀檔與判讀」：

| 模組 | 做什麼 | 依賴 | 不做什麼 |
|---|---|---|---|
| 新增 `src/aicomm/sidecar_link.py`（約 90 行） | 「目錄→可用埠」：解析目錄（env 或 home）、判斷心跳新鮮、解析 `ports`（UTF-8、空行、樣式、範圍、去重、排序）、扣掉呼叫端傳入的本機埠 `local_port` | 只依賴標準函式庫（`os`、`re`、`time`、`pathlib`）；介面 `read(*, local_port: int, env=os.environ, home=Path.home, now=time.time, pid_alive=<預設 os.kill 版>)`：`local_port` 必填，時鐘（`now`）、pid 檢查（`pid_alive`）、home 以參數注入，預設用真的 | MUST NOT `import aicomm.cli`；MUST NOT 發 HTTP；MUST NOT 有原因碼字串；MUST NOT 自己算本機埠（只用傳進來的 `local_port`） |
| `src/aicomm/cli.py` | 算本機埠（`urllib.parse.urlsplit(sidecar_base(...)).port`；沒有埠用 80；只比埠號）、逐埠 `GET`、逐埠 `POST`、`_merge_peers()`、原因碼與 `wake:` 行 | `sidecar_link`、`urllib`、既有判讀表 | MUST NOT 自己解析 `heartbeat`／`ports` 的格式 |

- 回傳值是型別化的結果：`Ports(tuple[int, ...])` 或 `Unusable(kind)`，`kind ∈ {unavailable, bad-format, out-of-range}`。**原因碼字串不在這個模組**：cli 把 `unavailable → peer-unavailable`、`bad-format → peer-bad-answer`、`out-of-range → peer-port-out-of-range` 對應過去。原因碼仍只在 cli 的常數一處定義，「原因碼與常數一致」的測試不必動。
- 為什麼逐埠查／戳與合併留在 cli：它們和本機路徑共用 `_request`、`_poke_answer`、`_poke_body`、`_merge_peers`、`_Wake`；搬出去會讓新模組反過來 import cli（環），或得把一半的 cli 一起搬走（超出本單）。`_wake_elsewhere()`（`:587-613`）保留名字與位置，內容改成「`sidecar_link.read(...)` → 逐埠 GET → 逐埠 POST → `_merge_peers()`」。
- 依賴方向：`cli → sidecar_link → stdlib`；`sidecar_link` MUST NOT 依賴 `cli`。**守衛測試**（照 D-22 的做法）在子行程 `import aicomm.sidecar_link`，assert `aicomm.cli` 不在 `sys.modules`；另 assert 這個模組的原始碼不含 `urllib`／`http.client`／`aicomm.cli`（契約 C-17）。
- SOLID：SRP＝「讀檔與判讀」和「HTTP 與結果判讀」分開，兩個模組各只有一個改變的理由（MLP 改檔案格式 vs aicomm 改傳輸）；OCP＝MLP 改 `ports` 的格式時只改 `sidecar_link`，cli 的原因碼對應不變；DIP＝時鐘與 pid 檢查以參數注入，90 秒邊界與「pid 0／超大 pid」不必改 mtime、不必起行程就能測。
- 檔案大小：刪掉 `:52-60`、`:419-561`（約 150 行），cli 加回大約 60 行；`sidecar_link.py` 約 90 行。淨變小，所以這張單不再切 cli；切其他部分超出範圍。
- 測試策略分兩層：
  - `sidecar_link`：純單元測試，注入 `now` 與 `pid_alive`，不起 HTTP、不起子行程；覆蓋心跳四種不新鮮、`ports` 三類錯誤與優先序、扣掉本機埠後為空、env 規則、守衛。
  - cli 的跨機路徑：`FakeSidecar` ＋ 假的 link 目錄（`AICOMM_SIDECAR_LINK`）的整合測試，是現有的 `run_aicomm` 家族；覆蓋四類查詢結果、戳的傳輸結果、合併、預算、不快取、兩條路由。
  - 驗收門檻由 PM 定（本 change 只寫設計）。

### 3.9 契約 C-1～C-17（spec delta 的編號索引）

spec delta 是規範本體；下表是它的收斂索引，每條寫成「輸入 → 輸出或錯誤」。實作報告與 `tasks.md` 逐條對到測試名稱（`§5` 的表有 C 欄）。

| C | 輸入 | 輸出或錯誤 | spec 對應 | 測試層 |
|---|---|---|---|---|
| C-1 | `heartbeat` 讀不到（不存在／不是普通檔／無權限／IO 錯） | `Unusable(unavailable)` → `not-woken peer-unavailable` | 讀 sidecar-link 的心跳 | 純單元 |
| C-2 | `heartbeat` 的 `age = time.time() - st_mtime`；`\|age\| ≤ LINK_FRESH_SECONDS`（90）且 pid 活 | 新鮮；否則 `unavailable` → `peer-unavailable` | 同上 | 純單元（注入 `now`） |
| C-3 | `heartbeat` 內容去頭尾空白後符合 `^[1-9][0-9]*$` 且 ≤ 2147483647 | 合格；否則 `unavailable`（`os.kill` 不收 0／負數／超大 pid） | 心跳的 pid 驗證 | 純單元 |
| C-4 | `pid_alive(pid)`：`ProcessLookupError`／其他 `OSError` vs `PermissionError` | 前者不在 → `unavailable`；`PermissionError` 算活著 | 心跳的 pid 驗證 | 純單元（注入 `pid_alive`） |
| C-5 | `ports` 讀不到 | `Unusable(unavailable)` → `peer-unavailable` | 讀 sidecar-link 的埠清單 | 純單元 |
| C-6 | `ports` 不是 UTF-8／有空行（只允許末尾換行造成的一個）／某行不符合 `^[0-9]{1,5}$` | `Unusable(bad-format)` → `peer-bad-answer`；**整份清單不用**；優先序＝檔案順序第一個不合格的行；非 UTF-8 一律 `bad-format` | 讀 sidecar-link 的埠清單／清單的錯誤是整份的 | 純單元 |
| C-7 | `ports` 某行是整數但不在 1–65535 | `Unusable(out-of-range)` → `peer-port-out-of-range`；整份清單不用 | 讀 sidecar-link 的埠清單／清單的錯誤是整份的 | 純單元 |
| C-8 | 合格且非空的 `ports`＋呼叫端傳入的本機埠 `local_port`（P₀） | `Ports`：去重（留第一次）、升冪、扣掉 P₀；扣完為空 → `unavailable` → `peer-unavailable` | 讀 sidecar-link 的埠清單／清單的錯誤是整份的 | 純單元 |
| C-9 | `AICOMM_SIDECAR_LINK`：未設 → 用 `home/.mylinuxpool/sidecar-link`；空字串／非絕對路徑／`Path.home()` 失敗 → 沒有目錄 | 沒有目錄 → `peer-unavailable`（不改 exit code） | sidecar 位址固定為預設 loopback 位址 | 純單元＋cli 整合 |
| C-10 | 每個埠 `GET http://127.0.0.1:P/agents/<quoted>` | 404→不在這台；200＋JSON 物件＋`state` 字串→有回答；200 但讀不懂→`peer-bad-answer`；連不上／逾時／HTTP 層讀不懂／非 200 非 404／預算用完→查不到 | 每一個埠怎麼查 | cli 整合 |
| C-11 | 每一台戳的傳輸結果 | 逾時→`unknown poke-timeout`；連線被拒（含包在 `URLError`）→`not-woken peer-unavailable`；其他連線錯誤→`unknown poke-error`；`HTTPException`→`unknown peer-bad-answer` | 跨機查詢與戳的連線結果 | cli 整合 |
| C-12 | 戳的 HTTP 回應（200／409／其他） | 與本機共用 `_poke_answer()`／`_refusal_code()`；每一台的 `interrupt` 各自判斷；`record=` 位置是 `peer:<port>` | 戳的回應怎麼判讀 | cli 整合 |
| C-13 | 一個收件人在好幾台的結果 | 任一 200→`woken peer:<ports 升冪>`；否則任一 `unknown`→`unknown`；否則 `not-woken`（原因碼取最小 port）；` queued` 只在每一台 200 都排隊時 | 同一個收件人戳了好幾台時取一個結果 | cli 整合 |
| C-14 | 查詢預算 `PEER_LOOKUP_BUDGET=10.0`、每埠 `min(PEER_GET_TIMEOUT=3.0, 剩餘)`、`time.monotonic()`、依埠號升冪 | 用完還沒查的埠＝查不到；結果不會是 `not-found` | 跨機叫醒的時間預算 | cli 整合 |
| C-15 | 一次 `send` 的多位收件人 | 每個收件人都重新讀 `heartbeat`／`ports`（不快取） | 「查不了」的結果 | cli 整合 |
| C-16 | 後援期間的所有請求 | 只 `GET /agents/<id>`、`POST /agents/<id>/poke` 到 `127.0.0.1:P`；不呼叫 `pool-peer`／任何外部指令；不連非 loopback；不轉印 body | 跨機叫醒直連本機的 sidecar-link 埠／不改 Angareion 與 sidecar | cli 整合＋守衛 |
| C-17 | 子行程 `import aicomm.sidecar_link` | `aicomm.cli` 不在 `sys.modules`；模組原始碼不含 `urllib`／`http.client`／`aicomm.cli` | §3.8（依賴方向） | 守衛 |

## 4. 逐條對照表（今天的每個原因碼／逾時／規則 → 新做法）

### 4.1 原因碼

| 原因碼 | 今天怎麼來 | 新做法 | 決定 |
|---|---|---|---|
| `not-found` | `find` rc 1 | 清單裡**每個**埠都 404 | 保留，語意不變（每一台都確定沒有） |
| `peer-unavailable` | `find` rc 2、`call` rc 2、子行程起不來 | (1) 心跳或清單不可用（檔不在、讀不到、mtime 超過 90 秒、pid 不在、pid 讀不懂、pid 太大、清單空檔、**扣掉本機埠之後沒有埠**）＝ MAS-2 的「查不了」；(2) 某一台的戳連線被拒 | 保留；`pool-peer` 換成兩個檔，`call` rc 2 換成連線被拒；「扣掉本機埠後沒有埠」沿用空檔的結果（不再可能是空真的 `not-found`） |
| `lookup-incomplete` | `find` rc 3、aicomm 的子行程逾時 | 沒有任何埠給出可讀的回答，而且至少一個埠「查不到」（連不上、逾時、HTTP 層讀不懂、非 200 也非 404、查詢預算用完） | 保留 |
| `peer-port-out-of-range` | `find` 回報的 port 不在 5200–5399（**逐台**：只有找到那台、而且那台可戳時才出現） | 清單某一行是十進位整數但不在 1–65535；**整份清單不用**（這台機器上所有跨機收件人都拿到這個碼）；同一份清單有多個壞行時，依檔案順序第一個不合格的行決定 | 保留，範圍窄化成「合法的埠號」，**語意從逐台變成整份清單**：有一行壞就整份不用；pool 的 5200–5399 段（`:59-60`）刪除——埠由清單決定，不再由 aicomm 猜 |
| `peer-bad-answer` | `find` 的 stdout 不是陣列／沒有合格項目、`find` 的 rc 不在約定內（`:515`）；`call` 的 stdout 第一行不是狀態碼或 rc 不在約定內 | (1) 清單格式不對（非 UTF-8、空行、不是十進位）；(2) 某一台 200 但 body／`state` 讀不懂或不是 token；(3) 某一台戳的回應 HTTP 層讀不懂 | 保留；來源從子行程的 stdout 換成清單與 sidecar 的回應；清單的優先序見上一列（非 UTF-8 一律這個碼）；「rc 不在約定內」的來源隨子行程消失，沒有對應 |
| `bad-answer` | 本機查詢讀不懂 | 不變 | 保留 |
| `bad-name` | 名字以 `-` 開頭（怕被 `pool-peer` 當選項） | 行為不變（見 §4.3） | 保留 |
| `state-<state>` | 有回答但不是 idle／running | 不變（本機與每一台都一樣） | 保留 |
| `agent-not-running`／`awaiting-answer` | 409 的錯誤碼（本機；跨機經 `call` 的 body） | 不變；跨機現在直接讀 HTTP 409 的 body | 保留 |
| `http-<碼>` | 戳的其他狀態碼 | 不變（兩邊都是 HTTP） | 保留 |
| `poke-timeout` | 本機戳逾時；`call` rc 3／子行程逾時 | 本機不變；某一台的戳逾時 | 保留 |
| `poke-error` | 本機戳的其他連線錯誤（含被拒、被重置） | 本機不變；某一台的戳被重置等（連線被拒除外，那是 `peer-unavailable`） | 保留 |
| `internal-error` | `_wake_one()` 的兜底 | 不變 | 保留 |

原因碼的**集合不變**（`NOT_WOKEN_CODES`／`UNKNOWN_CODES` 不動），所以「原因碼與常數一致」的測試與文件清單都不用改內容。

一個刻意的差異：本機戳的連線被拒今天是 `unknown poke-error`（`:656-658`），跨機的連線被拒新做法是 `not-woken peer-unavailable`（今天 `call` rc 2 的語意）。理由：本機的行為本單不動（單的「保持不變」）；跨機沿用今天「那一台戳不到」的碼，而且連線被拒是確定沒送出去，說 `unknown` 會讓人不敢重試。

### 4.2 逾時

| 今天 | 新做法 | 決定 |
|---|---|---|
| 本機 GET 3 秒（`:45`） | 不變 | 保留 |
| 本機戳 5 秒（`:46`） | 不變 | 保留 |
| `find` 10 秒＋子行程 2 秒（`:54`、`:56`） | 查詢總預算 `PEER_LOOKUP_BUDGET`（10.0 秒），每個請求最多 `PEER_GET_TIMEOUT`（3.0 秒）；沒有子行程，2 秒寬限刪除 | 換掉 |
| `call` 5 秒＋子行程 2 秒（`:55`、`:56`） | 每一台戳 `PEER_POKE_TIMEOUT`（5.0 秒）；沒有子行程，寬限刪除 | 換掉 |
| 收件人一個一個處理 | 不變 | 保留 |

### 4.3 其他規則

| 今天的規則 | 新做法 | 決定 |
|---|---|---|
| 只打兩個路由 | 對每一台都只有 `GET /agents/<id>` 與 `POST /agents/<id>/poke` | 保留 |
| 只連 loopback、不用 proxy | 目標固定 `127.0.0.1:P`；proxy 照舊關掉 | 保留（今天跨機的目標由 `pool-peer` 決定，現在由 aicomm 自己固定） |
| 不轉印 `pool-peer` 的 stdout／stderr | 沒有子行程；改成不轉印任何 sidecar 的回應 body | 換掉 |
| 埠段 5200–5399（`:59-60`） | 刪除；改驗證「每行是十進位、1–65535」 | 刪除（範圍由清單決定） |
| 同一個埠出現兩次只戳一次 | 清單去重（留第一次），由小到大處理 | 保留 |
| 本機的 sidecar 不在清單的語意裡 | 新增：跳過與本機 sidecar 同一個埠（`urllib.parse.urlsplit(sidecar_base(...)).port`；沒有埠用 80；只比埠號、不比主機；預設 8471） | 新增，防同一個 sidecar 被戳兩次（spec 的「同一個對象的同一台 sidecar 不戳兩次」）。只比埠號擋不住「某個 P 的隧道繞回本機 sidecar」，但那種情況本機已經回 404，再查一次也還是 404，無害 |
| 名字以 `-` 開頭不交給 `pool-peer` | 仍然拒絕（`not-woken bad-name`） | 保留。這條原本只是為了 argv；新傳輸把名字放在 URL path（已 quote），技術上不再需要，但單明列「保持不變」，改它會動 `wake:` 行的行為，要另開單 |
| `AICOMM_POOL_PEER`（測試與手動）、PATH 找 `pool-peer`、`~/.mylinuxpool/bin/pool-peer` | 刪除。測試鉤子換成 **`AICOMM_SIDECAR_LINK`**：指定清單目錄（預設 `~/.mylinuxpool/sidecar-link`），只給測試與手動用；安全界線是 loopback＋兩條路由，不依賴誰拿得到這個變數。設了但是空字串或不是絕對路徑 → 當成沒有目錄（`peer-unavailable`，契約 C-9） | 換掉。它只決定**讀哪個目錄**，不是 sidecar 位址，也不是使用者可設定的叫醒位址；能打哪些埠由清單決定 |
| `AICOMM_SIDECAR`（本機位址覆寫、只收 loopback） | 不變；另外用它算出要跳過的本機埠 | 保留 |
| `find` 的 JSON 陣列、rc 對照、`call` 的 stdout 第一行（`:463-532`） | 全刪；換成清單的格式驗證與 HTTP 狀態碼 | 刪除 |
| 每一台的 `interrupt` 各自判斷（`payload = _poke_body(bool(interrupt and state == "running"), …)`，`:538`） | 不變，只是每一台用自己的 `state`（§3.4） | 保留 |
| `record=` 依 port 由小到大（`ordered = sorted(…)` `:572`、`records = tuple(…)` `:575`） | 不變（清單先升冪排序） | 保留 |
| rc 0 時其他台可能還沒查到，MUST NOT 另外標示，只回報找到的那幾台（主 spec `:374`） | 不變：合併只寫成功的 port，不標示「沒查」或「查不到」 | 保留 |

### 4.4 明確刪除的程式與路徑

`POOL_PEER_ENV`／`POOL_PEER_COMMAND`／`PEER_FIND_TIMEOUT`／`PEER_CALL_TIMEOUT`／`PEER_CHILD_GRACE`／`PEER_PORT_MIN`／`PEER_PORT_MAX`（`:52-60`）、`_peer_executable()`（`:419-441`）、`_peer_child_limit()`（`:444-446`）、`_run_pool_peer()`（`:449-460`）、`_parse_peers()`（`:463-492`）、`_lookup_peers()`（`:495-515`）、`_call_answer()`（`:518-532`）、`_poke_peer()`（`:535-561`），以及 `subprocess`／`shutil` 若不再用到就一起移除。`_wake_elsewhere()`（`:587-613`）保留名字與位置，內容換成讀清單、逐埠查、逐埠戳、合併。

## 5. 既有測試怎麼對應

假 `pool-peer` 整個移除（`_FAKE_POOL_PEER`、`install_fake_pool_peer()`、`pool_peer_calls()`、`assert_only_pool_peer_calls()`、`_path_peer_is_test_only()` 與 `run_aicomm()` 裡對應的 assert，`tests/unit/_aicomm_fakes.py:74-152`、`:349-410`）。做法：

- 兩層（§3.8）：`sidecar_link` 走純單元測試（注入 `now`／`pid_alive`）；cli 的跨機路徑走 `FakeSidecar` ＋ 假 link 目錄的整合測試（就是 `run_aicomm` 家族）。
- `FakeSidecar` 照用（它已經是 loopback HTTP、記下每個請求、可逐個別設定 `state`／poke 回應）。
- fixture 多一個「假 sidecar-link 目錄」：`tmp_path/link/{heartbeat,ports}`，`AICOMM_SIDECAR_LINK` 指到它；`world.add_peer(state=…)` 起一個 `FakeSidecar` 並把它的埠寫進 `ports`；`world.link(...)` 能寫「不新鮮的心跳」（舊 mtime／死 pid）、「沒有心跳」、「沒有 ports」、「格式不對的 ports」、空字串／相對路徑的 `AICOMM_SIDECAR_LINK`。
- 護欄：`run_aicomm()` 除了 `AICOMM_SIDECAR`，要求 `AICOMM_SIDECAR_LINK` 有設、或預設目錄（HOME，`isolated_home` 指到暫存）不存在；測試永遠讀不到真的 `~/.mylinuxpool/sidecar-link`。
- `world.sidecar.foreign_requests()` 照用；對每一台 peer 的 `foreign_requests()` 也照樣 assert 空（只有兩條路由）。
- 其餘原本用 `use_peer()`／`world.peer_calls()` 的斷言（`test_406_c9_bad_name_means_no_record`、`test_413_c3_a_bad_message_id_drops_the_ref_key_on_a_peer_too`、`test_413_c9_no_wake_no_recipient_and_only_self_send_no_request`、`test_413_c10_a_failed_send_pokes_nobody`、`test_406_c11_every_line_matches_the_closed_grammar`）一律改成「各台 peer sidecar 沒有請求」。

| 既有測試家族（`tests/unit/test_aicomm_send.py:914` 起） | C | 新做法 |
|---|---|---|
| 執行檔家族：`test_c4_without_a_pool_peer…`、`test_c4_without_the_hook_pool_peer_comes_from_path`、`…home_bin`、`test_when_path_and_home_bin…`、`…not_executable…`、`…path_home_fails…`、`test_the_hook_wins_over_path` | C-9 | 刪除（沒有執行檔、PATH／bindir 搜尋）；換成心跳／清單的可用性家族：檔缺、mtime 舊、pid 死、pid 不是正整數、pid 0、pid 2147483648、空 `ports`、`ports` 格式不對、`ports` 埠超範圍、`AICOMM_SIDECAR_LINK` 空字串／相對路徑 |
| `test_c5_find_rc_1_is_not_found` | C-10、C-13 | 改成「每個埠都 404 → `not-found`」 |
| `test_c6_find_rc_3_is_lookup_incomplete…` | C-10、C-13 | 改成「一個埠連不上＋其他 404 → `lookup-incomplete`」 |
| `test_c7_find_rc_2…` | C-1～C-9 | 改成「心跳／清單不可用 → `peer-unavailable`」 |
| `test_find_with_an_rc_outside_the_contract…`、`test_c34_a_find_that_cannot_be_started`、`test_c28_a_call_rc_outside…`、`test_c35_a_call_that_cannot_be_started`、`test_c14_a_call_whose_first_line…`、`…bytes that are not utf8`、`test_c18_the_pool_peers_output…`、`test_c18_a_calls_own_output…`、`test_the_child_limit…`、`test_420_a_find_answer_too_deep…`、`test_420_a_deep_find_answer…` | C-6、C-10、C-11 | 刪除（沒有子行程、rc、stdout 可解析）；改寫的替代品：`ports` 不是 UTF-8 → 格式不對 → `peer-bad-answer`；**peer 這邊仍可能拿到讀不懂的 JSON**：某一台 200 的 body 太深 → `not-woken peer-bad-answer`；某一台的戳回應 HTTP 層讀不懂 → `unknown peer-bad-answer`；某一台的戳被重置 → `unknown poke-error`；回應 body 帶一個位址字串 → assert 它不出現在 stderr |
| `test_c11_a_find_answer_that_cannot_be_read…` | C-6 | 改成「`ports` 不是十進位 → `peer-bad-answer`」 |
| `test_c8_one_peer_is_poked_with_the_local_pokes_body` | C-12 | 改成「埠上的假 sidecar 收到同一個 body，`via` 為 `aicomm-peer`」 |
| `test_each_recipient_is_looked_up_on_its_own`、`test_the_poke_path_quotes_the_name` | C-10、C-15 | 保留（各自查一次、每位收件人重新讀檔；URL quote 仍在） |
| `test_the_name_is_one_argv_token_and_never_a_shell_word` | C-16 | 刪除（沒有 argv）；改成「名字只出現在 quote 過的 URL path」 |
| `test_c15_a_sidecar_that_cannot_be_reached_still_looks_elsewhere`、`test_c15_a_local_answer_never_sends_it_to_another_machine`、`test_c16_a_local_poke_timeout_never_asks_pool_peer` | C-16 | 保留，assert 改成「沒有任何台 peer 收到請求」 |
| `test_c9_the_same_port_reported_twice_is_poked_once`、`test_c9_the_first_item_of_a_port…`、`test_c9_every_pokeable_peer…`、`test_c9_the_woken_ports_are_listed_from_the_smallest`、`test_c9_a_peer_in_another_state…`、`test_a_peer_state_that_is_not_one_token…` | C-8、C-12、C-13 | 保留（清單去重、多台都戳、排序、狀態判斷都在） |
| `test_c10_a_port_outside_the_pool_range…`、`test_c10_the_edges_of_the_pool_range…` | C-7 | 改成「0／65536／非數字」三個樣本（`peer-port-out-of-range` 與 `peer-bad-answer`），並加「一行壞＝整份不用」「依檔案順序第一個壞行決定」「只有本機埠→`peer-unavailable`」 |
| `test_c27_each_peer_decides_its_own_interrupt`、`…did_not_wake…` | C-12 | 保留 |
| `test_c12_a_call_that_times_out…`、`test_c17_a_find_that_hangs…`、`test_c17_a_call_that_hangs…` | C-11、C-14 | 改成「那一台的 GET 逾時 → `lookup-incomplete`」、「那一台的戳逾時 → `unknown poke-timeout`」；另加「查詢總預算用完 → 沒查的埠算查不到」「前面的埠卡住吃光預算 → 後面的埠算查不到」 |
| `test_c13_a_call_answer_is_read_with_the_local_pokes_table`、`test_406_c13…`、`test_406_c15…`（`transport="call"`）、`test_406_c17…`、`test_406_c7`／`c8`／`c10`（`call` 樣本）、`test_413_c2…`、`test_413_c7…`、`test_413_c13…` | C-12 | 保留，`call` 的樣本改成「peer 的假 sidecar 直接回該狀態與 body」 |
| `test_c36_a_call_that_cannot_connect_is_peer_unavailable` | C-11 | 保留（埠上沒有 listener） |
| `test_c18_a_peer_result_is_still_one_line…`、`test_an_unknown_from_one_peer_beats…`、`test_two_unknowns…`、`test_two_not_woken…`、`test_c22…`、`test_a2…`、`test_a3_log_never_asks_pool_peer`、`test_c21_a_name_starting_with_a_dash…` | C-13、C-16 | 保留，assert 改成「清單／各台 peer 沒有請求」 |
| `tests/unit/test_aicomm_docs.py` 的查紀錄與原因碼檢查 | — | 保留；跨機查紀錄的寫法從 `pool-peer call … GET /pokes/<id>` 改成 `curl -s 127.0.0.1:<port>/pokes/<id>` |
| `tests/unit/test_aicomm_fakes_guard.py` | C-16、C-17 | 保留並改：護欄改成「假的 sidecar-link 目錄」（含空字串／相對路徑）、子行程 import `sidecar_link` 時 `aicomm.cli` 不在 `sys.modules`、以及上面的新 assert |

新增的測試（設計核准後與程式同一個 PR 寫；每一條對到 C）：`sidecar_link` 純單元——心跳新鮮／不新鮮的邊界（89／90／91 秒、未來時間、pid 死、pid 不是正整數、pid 0、pid 2147483648、`PermissionError`）、`ports` 格式（空行、非 UTF-8、非十進位、0、65536、重複、優先序）、扣掉本機埠後為空、`AICOMM_SIDECAR_LINK` 空字串／相對路徑、`Path.home()` 失敗（C-1～C-9）；cli 整合——四類查詢結果（C-10）、戳的四種傳輸結果（C-11，含 peer 戳被重置與 HTTP 層讀不懂）、合併與 `record=` 排序（C-13）、查詢預算用完與前埠卡住（C-14）、不快取（C-15）、每一台 peer 只有兩條路由（C-16）、守衛（C-17）。

## 6. 風險與取捨

1. **沒有 sidecar-link 的機器會失去跨機叫醒**（已知差異，交 PMO 判斷）：今天只要機器上有 `pool-peer` 就能跨機叫醒；MAS-1 之後，沒有那兩個檔的機器一律 `not-woken peer-unavailable`。單已寫明由 PMO 判斷；PMO 要先確認那些機器上有人用 aicomm 跨機叫醒，有的話 pool-peer 的第二階段退役要等那些機器另有路徑。
2. **`127.0.0.1:P` 是某台 sidecar 的假設**：由 MyLinuxPool#1085 保證（介面見 §2）。若轉發壞了但心跳還新鮮，結果是 `lookup-incomplete`／`peer-unavailable`，不會戳到錯的機器——aicomm 只戳 `/agents/<id>/poke`，那台 sidecar 上沒有這個 id 就回 404，不會把叫醒送到別的 agent。
3. **對別人的埠送 poke 的安全面**：只送兩種固定請求，body 固定、不含訊息內容；回應只採信驗證過的 `state` token、`poke_id`、`poke_outcome`；不印 body、不印位址。清單被亂寫時，最壞是對一個 loopback 服務送出那兩個請求（見 §3.7）。
4. **清單過期與埠被別的服務接手**：心跳的 90 秒窗把「過期」限制在 90 秒內；真的接手時，aicomm 對那個服務只有那兩個固定請求，且回應只當成「一台 sidecar 的回答」來判讀（見 §3.7）。要更強（先確認那台真的是 sidecar）就得加第三種請求，單明列不新增，所以接受。
5. **清單很長**：查詢有 10 秒總預算，超過的埠算「查不到」，結果是 `lookup-incomplete`，不會謊稱 `not-found`。今天的 `find` 也是同一個 10 秒量級。依埠號由小到大**依序**查，前面的埠卡住（接受連線但不回應）會吃預算，後面的埠算查不到——同樣是 `lookup-incomplete`，誠實；平行查詢留到量過再說，本 change 不做。
6. **心跳的 pid 可能被重用**：一個過期的心跳，若 pid 剛好被別的行程重用，會被當成新鮮；mtime 的 90 秒窗是主要防線，pid 只是第二道。這是介面本身的限制，不是 aicomm 能補的（要補就得讀 `/proc`，超出介面）。
7. **少了一次跨機的「整體查詢」語意**：今天 `find` 的 rc 3 是 pool-peer 整體的判斷；新做法是逐埠判斷再合併（§3.5），結果碼相同，但「查不到」的粒度從「整個 pool」變成「某一台」。這是資料來源換了之後自然的差異，逐條對照見 §4.1。
8. **不需要執行檔**：新做法沒有子行程、沒有 PATH／bindir 搜尋、沒有 argv，也少了 `subprocess` 的逾時與收尾；行為只取決於兩個檔與 loopback 連線。這是簡化，但也是「`pool-peer` 不見了」之後唯一的路。
9. **發版／升 pin 的依賴**：本 change 可以先 merge；MyLinuxPool 會等 MAS 發版後才升 pin，所以在 sidecar-link 上線之前，**不要發版、不要升 pin**（`tasks.md` 的單一 PR 與 3.2 已標）。主線程式碼在 merge 後、發版前仍是舊版行為（pin 沒動），不會有中間態；中間態只出現在「已發版的 aicomm 跑在沒有那兩個檔的機器上」，那就是第 1 點的差異。合併前 PM 要先確認沒有機器直接從 main 安裝 aicomm（`tasks.md` 3.2）；MLP 已回「worker 映像釘 sha、沒有從 main 安裝」，未驗的是 Mac／PMO 機器上 repo 外的手動安裝，所以 merge 前還要 PMO 確認那幾台；有的話 merge 要改等 sidecar-link 上線。

## 7. 被否決的選項

| 選項 | 為什麼不 |
|---|---|
| 保留 `pool-peer`，只在有清單時改走直連 | 單要的是 pool-peer 退役；兩條路徑並存會讓原因碼與測試各有一套，退役時再改一次 |
| 讀清單但沿用 `pool-peer call` 戳（只換查詢） | 戳也要換；`call` 一樣要吃埠段與 rc，退役時兩邊都要動 |
| 每次後援只查前幾個埠（例如 5 個）就停 | MAS-3 要求同一個 id 在兩台都戳；少查就會漏戳。改用 10 秒總預算，時間有上限、埠不預先丟 |
| 查到第一台就停 | 同上（MAS-3） |
| 心跳 pid 用 `/proc/<pid>` 判斷 | 介面只保證「pid 還活著」；`os.kill(pid, 0)` 是標準做法，也不綁 Linux |
| 心跳不看 mtime，只看 pid | 一個 pid 被重用就會讓死掉的 link 看起來活著；兩個條件都看才是介面定義的「新鮮」 |
| `ports` 只取「像埠」的行，其餘跳過 | 靜默跳過會把「清單壞了」變成「查不完整」或「找不到」；壞格式要說出來（`peer-bad-answer`） |
| `ports` 不驗證範圍，直接連 | 0、65536 這種值不是合法的埠，先擋掉才不會和「連不上」混在一起（今天的 `peer-port-out-of-range` 就是這個目的） |
| 清單含本機埠時照戳 | 同一個 sidecar 會被戳兩次（spec 禁止）；而且本機已經查過／戳過 |
| 扣掉本機埠後沒有埠時回 `not-found`（空集合恆真） | 一台都沒查，說「每一台都確定沒有」是謊稱；與空檔同一個結果：`peer-unavailable`（§3.2 第 4 點） |
| 清單有壞行時跳過那一行、用剩下的 | 靜默跳過會把「清單壞了」變成「查不完整」或「找不到」；有一行壞就整份不用（§3.2 第 4 點） |
| 測試鉤子沿用 `AICOMM_POOL_PEER` 指到一個目錄 | 名字說明是執行檔；新鉤子是指目錄，另立 `AICOMM_SIDECAR_LINK` 才不會誤導 |
| 新原因碼（例如 `link-unavailable`、`port-unreadable`） | 單明列原因碼保持不變；而且今天的原因碼逐一都對得上（§4.1），不需要新增 |
| 保留埠段 5200–5399 | 埠由清單決定，那個段是 pool-peer 時代的猜測；留著只會在清單用到別段時誤判 |

## 8. 未決（要人決定）

- **U-1 `docs/architecture/decisions.md` 的 D-24／D-25**（已決）：D-24 寫「跨機只經 `pool-peer find`／`call … poke`，埠段 5200–5399 在 aicomm 先擋」，D-25 寫跨機查紀錄用 `pool-peer call …`；本 change 之後兩處都過時。架構師在本次修正輪的裁定：由架構師在本 change 的修正輪 commit **D-28**（內容為本設計 §3～§4 的摘要），並在 D-24／D-25 的末欄註記「跨機路徑由 D-28 取代」；`docs/architecture/decisions.md` 不在作者可 commit 的路徑，本 change 的檔案清單不變。
- **U-2 沒有 sidecar-link 的機器**（差異本身）：單明列「由 PMO 判斷」，不在本 change 決定。**建議**：PMO 先盤點那些機器有沒有人用 aicomm 跨機叫醒，再決定 pool-peer 第二階段退役的時機。
- **U-3 `AICOMM_SIDECAR_LINK` 要不要也接受 sidecar-link 自己寫的舊路徑**：介面只保證 `~/.mylinuxpool/sidecar-link/`；本設計只讀這一個目錄，不猜別的路徑。若 MLP 之後改路徑，要另開單。
- **U-4 90 秒窗與查詢預算的數字**：本設計照介面定義的 90 秒；查詢預算 10 秒沿用今天的 `find`。兩者都是常數，若上線後量到 pool 變大或 link 變慢，再調（不影響介面）。
- **U-5 `MLP_SIDECAR_PORT`**：單的 MAS-1 括號寫「`127.0.0.1:${MLP_SIDECAR_PORT:-8471}`」，但今天 aicomm 的位址是固定的 `127.0.0.1:8471`（只有 `AICOMM_SIDECAR` 這個測試覆寫），主 spec 也寫「位址固定、不讀設定檔」。本設計照舊（不改本機位址），**建議**若真要支援 `MLP_SIDECAR_PORT` 另開單（它會動「位址固定」那條 requirement）。

## 9. 影響清單

- 本 change：`openspec/changes/aicomm-direct-peer/`（`proposal.md`、`design.md`、`tasks.md`、`specs/comm-skill/spec.md`、`.openspec.yaml`）。
- 實作＋文件同一個 PR（L-1；理由見 `tasks.md` §1）：`src/aicomm/sidecar_link.py`（新）、`src/aicomm/cli.py`、`tests/unit/_aicomm_fakes.py`、`tests/unit/test_aicomm_send.py`、`tests/unit/test_aicomm_fakes_guard.py`、`tests/unit/test_angareion_log_e2e.py`（只改那一行 `AICOMM_POOL_PEER`）、`docs/aicomm.md`、`skills/aicomm/SKILL.md`、`tests/unit/test_aicomm_docs.py`。
- 歸檔（PM）：主 spec `openspec/specs/comm-skill/spec.md` 由本 change 的 delta 同步；Purpose 段的「查不到時經 `pool-peer` 找別台」要照 delta 改。
- 跨 repo（只列）：MyLinuxPool#1085（sidecar-link 的心跳與埠清單、pool-peer 退役）；MyLinuxPool#786（poke 的 `via`／`ref`／`poke_id`／`poke_outcome`，不變）。
- 公開 repo：不含帳號、機器位址、憑證、真的 session id、私人資訊；只寫介面的檔案路徑、格式與「`127.0.0.1:P` 是某台的 sidecar」。
