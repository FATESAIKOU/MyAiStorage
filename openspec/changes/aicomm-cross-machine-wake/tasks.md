## 0. PR-0（本張；文件與規格）

範圍：`openspec/changes/aicomm-cross-machine-wake/`、`docs/design-notes/368-aicomm-wake.md`、`docs/architecture/decisions.md`。**不改 `src/`、`tests/`、`docs/aicomm.md`、`skills/aicomm/SKILL.md`**（那是 PR-1 與 PR-2）；不改架構師的設計文字；不自己 `openspec archive`。

- [x] 0.1 `proposal.md`：為什麼（跨機叫醒、本人 11-03 方向）、範圍（aicomm 側）、不做什麼（#314 的 `aicomm wake`、`pool-peer` 本身、真機驗收）、影響。
- [x] 0.2 `design.md`：逐字用架構師定稿（`design-368-final.md`）；之後照架構師 368b 裁定，把 §3.1 尾兩句、§3.5 與 §3.7 的原因碼清單換成報告的原樣文字（D-24 不用改）。完成判準（程式可驗）：`REPORT-arch-368b.md` 第 50 行（§3.1 尾兩句）、第 56–60 行（§3.5）、第 66–68 行（§3.7 原因碼清單）三段原樣文字各在 `design.md` 出現 1 次；`diff design-368-final.md design.md` 只有這三處（3 個 hunk），§3.7 沒有殘留的舊清單。
- [x] 0.3 `specs/comm-skill/spec.md`：delta 涵蓋架構師報告 §0 的衝突條文 `:11`、`:44`、`:80`、`:95`、`:102`；MODIFIED 帶完整新條文；每個 requirement 至少一個可驗的 scenario；保留「這個檢查 MUST NOT 被宣稱為防止跨機叫醒」的不變句，並補上 `AICOMM_POOL_PEER` 測試掛鉤。
- [x] 0.4 `docs/design-notes/368-aicomm-wake.md`：縮成一段指向本 change 的短 note（保留抬頭、單號、一個指向 design 的句子），不留重複內文。完成判準：檔案裡沒有第 2 節以後的行為條文。
- [x] 0.5 `docs/architecture/decisions.md`：已決表 D-23 之後加 D-24（用架構師給的原樣列）；交接節的 #368 那行改成「介面照 #677 D5 加 #696 擴充；PMO 11-04 指示開工。PR-2 merge 前，要確認擴充已經在 MLP 定案。」
- [x] 0.6 `openspec validate aicomm-cross-machine-wake` 通過（非 strict）。
- [ ] 0.7 交付：PM 收 PR-0，`狀態:進行中` 之後開 PR-1。

完成判準（本張）：0.1～0.6 都完成；`git diff --stat` 只動上面三個路徑（加 change 目錄）；沒有 commit／push（由 PM 做）。

## 1. 契約與 spec scenario 的對照

下表讓 PR-1／PR-2 的測試名稱直接對到 delta 的條目（C-n 是架構師報告 §2 的契約清單）。

| C | 行為 | delta 條目（`specs/comm-skill/spec.md`） |
|---|---|---|
| C-1 | 本機 200 idle → 只戳本機、`wake: <agent> woken local`、沒呼叫 `pool-peer`、exit 0 | 叫醒的對象與條件／「閒置的同台 agent」 |
| C-2 | 本機 200 running ＋ `--interrupt` → `woken local interrupt`，poke 的 `interrupt` 是 true | 叫醒的對象與條件／「明確中斷」「忙碌中也戳（不打斷）」 |
| C-3 | 本機 200 exited → `not-woken state-exited`，沒呼叫 `pool-peer` | 叫醒的對象與條件／「本機回 200 的其他 state 不查別台」「starting 不戳」 |
| C-4 | 本機 404、沒有 `pool-peer` → `not-woken peer-unavailable`、exit 0 | 找 `pool-peer` 執行檔／「沒有 `pool-peer`」；`send` 的結束碼與 `--require-wake`／「不加旗標時結束碼不變」 |
| C-5 | 本機 404、`find` rc 1 → `not-found` | 讀 `pool-peer find` 的結束碼／「`find` 回 1」 |
| C-6 | 本機 404、`find` rc 3 → `lookup-incomplete`，沒呼叫 `call` | 讀 `pool-peer find` 的結束碼／「`find` 回 3」 |
| C-7 | 本機 404、`find` rc 2 → `peer-unavailable` | 讀 `pool-peer find` 的結束碼／「`find` 回 2」 |
| C-8 | `find` 回一台 idle（5201）、`call` 200 → `woken peer:5201`；`call` 的 stdin 是固定句＋`interrupt: false` | 跨機叫醒只經 `pool-peer` 的兩個用法／「找到一台並戳成功」；戳的內容固定且不含訊息／「跨機的戳與本機相同」 |
| C-9 | `find` 回兩台 idle → 兩台都 call；一台 200 就是 `woken`，列出成功的 port | `find` 找到的每一台怎麼戳／「多台都可戳」；同一個收件人戳了好幾台時取一個結果／「多台取一個結果」 |
| C-10 | port 5100 或 5400 → 不呼叫 `call`、`peer-port-out-of-range` | `find` 找到的每一台怎麼戳／「埠超出範圍」 |
| C-11 | `find` rc 0 但 stdout 不是陣列／沒有合格項目 → `peer-bad-answer`，不呼叫 `call` | 解析 `pool-peer find` 的陣列／「`find` 的回答看不懂」 |
| C-12 | `call` rc 3 → `unknown poke-timeout`，不重送 | 讀 `pool-peer call` 的結束碼／「`call` 逾時」；戳的內容固定且不含訊息／「戳逾時」 |
| C-13 | `call` 409＋`agent_awaiting_answer`／`agent_not_running`、或 500 → `awaiting-answer`／`agent-not-running`／`http-500` | 戳的回應怎麼判讀（本機與跨機共用）／「`call` 回 409 與其他碼」 |
| C-14 | `call` rc 0 但第一行看不懂 → `unknown peer-bad-answer` | 讀 `pool-peer call` 的結束碼／「`call` 的第一行看不懂」 |
| C-15 | 本機 sidecar 連不上 → 改走 `find`（照 C-5～C-9） | 叫醒的對象與條件／「本機連不上時改查別台」 |
| C-16 | 本機 poke 逾時 → `unknown poke-timeout`，**不**改走 `find` | 叫醒的對象與條件／「本機戳逾時不改走 find」；叫醒是盡力而為，可關閉 |
| C-17 | `find`／`call` 卡住超過 `--timeout` 加 2 秒 → `lookup-incomplete`／`poke-timeout`；`send` 仍 exit 0 | 跨機叫醒的時間預算／「子行程卡住」 |
| C-18 | 每個收件人 stderr 剛好一行、格式符合 `wake:` 文法 | `wake:` 行的文法／「每個收件人剛好一行」 |
| C-19 | `--require-wake`：全 woken → 0；有 not-woken／unknown → 4；廣播或寄給自己 → 0；與 `--no-wake` 同給 → exit 1 且沒呼叫 angareion send | `send` 的結束碼與 `--require-wake`／「`--require-wake` 全部叫醒」「`--require-wake` 有人沒叫醒」「`--require-wake` 沒有人要叫醒」「`--require-wake` 與 `--no-wake` 互斥」「不加旗標時結束碼不變」 |
| C-20 | angareion send 失敗 → 結束碼照原樣，不碰 sidecar 與 `pool-peer` | send 先寫進 Angareion，成功才算送達／「寫入失敗」 |
| C-21 | 名字以 `-` 開頭、本機 404 → `not-woken bad-name`，不呼叫 `pool-peer` | 叫醒的對象與條件／「名字以 - 開頭」 |
| C-22 | `--no-wake` 或廣播 → 不呼叫 `pool-peer` | 叫醒是盡力而為，可關閉／「關閉叫醒」；叫醒的對象與條件／「廣播」 |
| C-23 | 文件寫明「exit 0 只代表已送達、看 `wake:` 行、exit 4 不要重送」，原因碼與 cli 常數一致 | `send` 的結束碼與 `--require-wake`／「文件與常數一致」 |
| C-24 | 本機 GET 200 但看不懂（不是 JSON 物件、`state` 不是字串、HTTP 層讀不懂）→ `not-woken bad-answer`，不戳、不呼叫 `pool-peer` | 叫醒的對象與條件（MODIFIED 本文）；新增 scenario「本機回應看不懂」 |
| C-25 | 本機戳回 409（`code` 或 `error` 是 `agent_awaiting_answer`／`agent_not_running`）或其他碼 → `awaiting-answer`／`agent-not-running`／`http-<碼>`；只戳一次 | 戳的回應怎麼判讀（本機與跨機共用）／「本機戳回 409 與其他碼」 |
| C-26 | 本機戳的連線被重置（非逾時的 `OSError`）→ `unknown poke-error`，不重送、不呼叫 `pool-peer` | 本機戳的連線結果／「本機戳的連線被重置」 |
| C-27 | 多台、`--interrupt`：每台各自判斷 `interrupt`；有一台成功的戳是 true 才加 ` interrupt` | `find` 找到的每一台怎麼戳／兩個 interrupt scenario |
| C-28 | `call` rc 不是 0／2／3 → `unknown peer-bad-answer`，不重送 | 讀 `pool-peer call` 的結束碼／「`call` 回了約定外的結束碼」 |
| C-29 | SKILL.md 代價段寫「pool 內任何 agent（含別台）」、硬控制寫 #346＋MyPMO#297，沒有「同台」 | 沒有硬控制，叫醒的節制由 SKILL.md 軟規則承擔／「不依角色分支」「SKILL.md 含三條軟規則」「SKILL.md 的代價說的是整個 pool」 |
| C-30 | cli 的原因碼常數恰為清單 | 叫醒的原因碼清單／「原因碼與常數一致」 |
| C-31 | 本機 GET 丟出 `http.client.HTTPException` → `not-woken bad-answer`，不戳、不呼叫 `pool-peer` | 叫醒的對象與條件（MODIFIED 本文）／「本機查詢的 HTTP 層讀不懂」 |
| C-32 | 409 的 body 看不懂或沒有錯誤碼（`code`／`error` 都不是字串）→ `http-409` | 戳的回應怎麼判讀（本機與跨機共用）／「409 的 body 看不懂或沒有錯誤碼」 |
| C-33 | 200 以外的 2xx（例如 201）→ `http-<碼>` | 戳的回應怎麼判讀（本機與跨機共用）／「200 以外的 2xx」 |
| C-34 | 執行 `find` 丟出 `OSError`（子行程沒起來）→ `not-woken peer-unavailable` | 讀 `pool-peer find` 的結束碼／「執行 `find` 失敗」 |
| C-35 | 執行 `call` 丟出 `OSError`（子行程沒起來）→ `not-woken peer-unavailable`，不重送 | 讀 `pool-peer call` 的結束碼／「執行 `call` 失敗」 |
| C-36 | `call` rc 2（連不到）→ `not-woken peer-unavailable`，不重送 | 讀 `pool-peer call` 的結束碼／「`call` 回 2」 |
| C-37 | 本機戳連線被拒（`ConnectionRefusedError`）→ `unknown poke-error`，不重送、不呼叫 `pool-peer` | 本機戳的連線結果／「本機戳的連線被拒」 |

## 2. PR-1：本機路徑換格式＋`--require-wake`（c 級）

範圍：`src/aicomm/cli.py`（`_wake_one`／`_wake_one_inner`／`_cmd_send`／旗標與原因碼常數）、`tests/unit/test_aicomm_send.py`（既有的中文斷言改成新格式）、`tests/unit/test_aicomm_docs.py`、`docs/aicomm.md`、`skills/aicomm/SKILL.md`。**不碰 `pool-peer`**；本機 404 時是 `not-woken peer-unavailable`（還沒有後援可查）。

- [ ] 2.1 本機結果改成 `wake:` 格式：`woken local[ interrupt]`、`not-woken <原因碼>`、`unknown <原因碼>`；`_wake_one` 的兜底改成 `unknown internal-error`；原因碼做成常數。
  - 完成判準：C-1～C-3、C-16、C-18（只有 local）、C-20、C-22、C-24～C-26、C-31～C-33、C-37 綠；`tests/unit/test_aicomm_send.py:129、:315、:392、:402、:414、:427、:532` 的舊斷言已換。
- [ ] 2.2 `--require-wake`：有任何 `not-woken`／`unknown` 回 4；沒人要叫醒回 0；與 `--no-wake` 同給 exit 1，且在寫進 Angareion 之前拒絕。
  - 完成判準：C-19 綠；拒絕路徑沒有任何 `angareion send` 與 sidecar 請求。
- [ ] 2.3 `docs/aicomm.md`「送達與叫醒」「結束碼」與 `skills/aicomm/SKILL.md` 同兩段改寫：exit 0 只代表已送達、看 stderr 的 `wake:` 行、exit 4 不要重送、原因碼清單。
  - 完成判準：C-23 綠（文件與 cli 常數一致）。
- [ ] 2.4 全 unit 與基線一致；fixture 的護欄不變（`foreign_requests() == []`）。
- blocked by：PR-0（spec delta 要先進；PM 若決定讓 delta 跟這張一起進，則不 blocked）。

## 3. PR-2：跨機後援（c 級）

範圍：`src/aicomm/cli.py`（`AICOMM_POOL_PEER`、`find`／`call` 的 argv 與解析、埠段 5200–5399、子行程逾時）、`tests/unit/_aicomm_fakes.py`（假 `pool-peer`）、`tests/unit/test_aicomm_send.py`、`docs/aicomm.md`（依賴表加 `pool-peer` 的兩個用法與 5200–5399）。

- [ ] 3.1 本機 404／連不上／GET 逾時才 `find`；本機 200（含其他 state）與其他 HTTP 碼不查別台；本機 poke 逾時不查別台。
  - 完成判準：C-15、C-16 綠；本機成功時 fixture 收尾 assert 沒有 `pool-peer` 呼叫。
- [ ] 3.2 `find`：argv `find <id> --json --timeout 10`；rc 0／1／2／3／其他與陣列解析照 spec；丟格式不對的項目、同 port 只留一筆（第一筆）；一筆合格的都沒有 → `peer-bad-answer`。
  - 完成判準：C-4～C-7、C-11、C-34 綠。
- [ ] 3.3 每一台：`state` 非 `idle`／`running` → `state-<state>`；port 不在 5200–5399 → `peer-port-out-of-range` 且不 `call`；可戳的每一台都戳。
  - 完成判準：C-9、C-10、C-27 綠。
- [ ] 3.4 `call`：argv `call <port> POST /agents/<quoted-id>/poke --timeout 5`、stdin 是固定句＋`interrupt`；200／409（`code` 或 `error`，兩個都認）／其他碼／第一行看不懂／rc 2／rc 3 照 spec；不重送。
  - 完成判準：C-8、C-12～C-14、C-28、C-35、C-36 綠。
- [ ] 3.5 子行程上限：`--timeout` 加 2 秒，自己收掉子行程；`find` → `lookup-incomplete`、`call` → `poke-timeout`。
  - 完成判準：C-17 綠。
- [ ] 3.6 假 `pool-peer` 與護欄：`AICOMM_POOL_PEER` 預設指到 `/nonexistent/pool-peer`；fixture 收尾 assert 只收到 `find <id> --json --timeout 10` 與 `call <port> POST /agents/<id>/poke --timeout 5`。
  - 完成判準：C-18（含 peer）綠；`--no-wake`／廣播／名字以 `-` 開頭時沒有 `pool-peer` 呼叫（C-21、C-22）。
- [ ] 3.7 `skills/aicomm/SKILL.md` 的代價段改成「pool 內任何 agent（含別台機器上的，經 `pool-peer`）都能叫醒 PMO」，硬控制改寫成「本機走 MyLinuxPool#346；跨機併在 MyPMO#297」。
  - 完成判準：C-29 綠；`grep -n 同台 skills/aicomm/SKILL.md docs/aicomm.md` 沒有講叫醒範圍的殘句。
- [ ] 3.8 N3 的護欄（對應「歸檔前檢查」的 A.2～A.4）：測試 assert 缺 `=` 被拒時沒有 `pool-peer` 呼叫、`aicomm log` 沒有 `pool-peer` 呼叫；`docs/aicomm.md` 寫明「`peer:<port>` 是 pool 內 sidecar 的埠號，不是機器位址」，且 `wake:` 行的 `<說明>` 不轉印 `pool-peer` 的 stderr。
  - 完成判準：這三項在 PR-2 的測試與文件裡找得到（A.2～A.4 的「確認 PR-2 有…」）。
- blocked by：PR-1。D5 擴充：PMO 2026-11-04 回覆 #696 已 merge、架構師留言即定稿（來源是 PMO 轉述）；PR-2 merge 前仍看一眼 #677 design 是否已寫入。有出入就以 MLP 的文字為準，回來改 `design.md` 第 2、3 節。

## 歸檔前檢查（PM 收尾時，archive／sync 主 spec 之前）

- [ ] A.1 主 spec 的 Purpose 段改成：「AI 之間溝通的指令 `aicomm`：`send` 寫進 Angareion，收件人若是 pool 裡（本機 sidecar，查不到時經 `pool-peer` 找別台）閒置或執行中的 agent 就順便戳一下，結果寫成 stderr 的 `wake:` 行；`inbox` 讀寄給自己的信。叫醒的判斷只放在這一層，Angareion、sidecar 與 `pool-peer` 各自只守自己的介面（設計見 `openspec/changes/archive/2026-10-25-comm-skill/` 與 `openspec/changes/archive/<日期>-aicomm-cross-machine-wake/`，MyAiStorage#212、#368）。」
- [ ] A.2 「其他參照 `--ref`」：條文不改（`--require-wake` 不影響叫誰）；確認 PR-2 有測試 assert 缺 `=` 被拒時沒有 `pool-peer` 呼叫。
- [ ] A.3 「log 薄包 angareion log…」：條文不改；確認 PR-2 有測試 assert `aicomm log` 沒有 `pool-peer` 呼叫。
- [ ] A.4 「不改 Angareion 與 sidecar，不攜帶私人資訊」：條文不改（`peer:<port>` 不是機器位址）；確認 `docs/aicomm.md` 有一句「`peer:<port>` 是 pool 內 sidecar 的埠號，不是機器位址」，且 `wake:` 行的 `<說明>` 不轉印 `pool-peer` 的 stderr。
- [ ] A.5 歸檔後對主 spec 跑標題逐字比對：MODIFIED 的標題都在、ADDED 的標題都不重複；`grep -n 同台 openspec/specs/comm-skill/spec.md` 沒有講叫醒範圍的殘句。
- [ ] A.6 scenario 標題「閒置的同台 agent」仍是「同台」：這一條不能在 delta 改名——`openspec validate`（1.14.3）會報 `MODIFIED "叫醒的對象與條件" omits scenario(s) the current spec still has: "閒置的同台 agent"`，`archive` 也會拒絕（MODIFIED 取代整個區塊，改標題等於刪掉既有 scenario）。要改就由 PM 在歸檔後直接改主 spec 的這一條標題（本 change 的 delta 不動），或等 openspec 支援 scenario 改名再另開單；`grep -n 同台` 在主 spec 會命中這一個標題。

## 4. 驗收（不是 PR）

- [ ] 4.1 真機上從 worker 戳 Mac（或反過來），對方收到 poke、stderr 是 `wake: <agent> woken peer:<port>`。
- blocked by：PR-2 已 merge；MLP 已部署 `pool-peer`；本人的單次授權（經 PMO）。

## 5. 不在本 change

- #314（`angareion status`／`aicomm wake`）的 `aicomm wake`：等它定案，若要跨機就重用 `_wake_one`（同一套規則與 `wake:` 格式），另開單。
- `pool-peer` 本身、埠段、Gateway 的綁定：歸 MyLinuxPool（#677／#696）。
- 環境說明範本（MyPMO 那邊）的措辭。
- `find` rc 0 陣列裡「查不到的台」的項目形狀，與 `find` 會不會列出本機自己：design §6 的待決，等 MLP 定案再決定要不要多報。
