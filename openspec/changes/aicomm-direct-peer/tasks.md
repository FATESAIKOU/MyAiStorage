## 0. 本張（設計作者；只寫 change）

範圍：`openspec/changes/aicomm-direct-peer/`。**不改 `src/`、`tests/`、`docs/aicomm.md`、`skills/aicomm/SKILL.md`、`openspec/specs/`、`docs/architecture/`**（那些是後續 PR 與 PM 的收尾）；不改 `pool-peer`；不自己 `openspec archive`。

- [x] 0.1 `proposal.md`：為什麼（MAS-1～3、`pool-peer` 退役）、做什麼（直連、查不了、多台、不變的清單、測試鉤子、delta 的 14／6／9）、Non-goals、Impact（含發版／升 pin 的依賴與已知差異、單一 PR）。
  - 完成判準：上面各項都有專段；明寫「本 change 可以先 merge，但發版／升 pin 要等 sidecar-link 上線」。
- [x] 0.2 `design.md`：現況盤點（附 `src/aicomm/cli.py` 行號）、MLP 保證的介面（只寫路徑、格式、`127.0.0.1:P`）與 aicomm 依賴的環境假設（MLP 已確認，未驗的部分寫在各列）、新行為（MAS-1～3）、模組切割與依賴（§3.8）、編號契約 C-1～C-17（§3.9）、逐條對照表（原因碼／逾時／其他規則）、測試對應（含 C 欄與兩層測試策略）、風險與取捨、被否決的選項、未決。
  - 完成判準：每個原因碼、每個逾時、每個安全規則都有一列「新做法／保留／刪除」與理由；契約每條寫成「輸入 → 輸出或錯誤」且有測試層；依賴方向寫成 `cli → sidecar_link → stdlib` 並有守衛測試；不含 MLP 的內部名詞（驗收用 `grep` 檢查；只寫介面的檔案路徑、格式與「`127.0.0.1:P` 是某台的 sidecar」）。
- [x] 0.3 `specs/comm-skill/spec.md`：MODIFIED 14 條、REMOVED 6 條（各附 Reason／Migration）、ADDED 9 條（心跳與清單的正文分成「讀心跳」「心跳的 pid 驗證」「讀埠清單」「清單的錯誤是整份的」四條，各自一個行為）；MODIFIED 的標題與主 spec 逐字一致，原有 scenario 標題全部保留（`本機戳逾時不改走 find`、`跨機執行檔的測試掛鉤`、`call` 回 409 與其他碼`、`子行程卡住` 等照留，內容改成新行為）；ADDED／MODIFIED 正文引用編號契約 C-1～C-17（`design.md` §3.9）。
  - 完成判準：`openspec validate aicomm-direct-peer --strict` 綠（非 strict 也綠）。
- [x] 0.4 `openspec validate aicomm-direct-peer --strict` 通過。
- [ ] 0.5 設計核准：專案架構師核准本 change 後，PM 才把 §1 的實作（單一 PR）交給實作隊員；非作者審查。
  - 完成判準：`design.md`／`tasks.md` 依審查意見定稿並 merge；未決 U-1（decisions.md 的 D-24／D-25）有結論。

## 1. 程式＋測試＋文件（同一個 PR；原 PR-1／PR-2 合成，L-1）

範圍：`src/aicomm/sidecar_link.py`（新）、`src/aicomm/cli.py`、`tests/unit/_aicomm_fakes.py`、`tests/unit/test_aicomm_send.py`、`tests/unit/test_aicomm_fakes_guard.py`、`tests/unit/test_angareion_log_e2e.py` 的 `AICOMM_POOL_PEER` 那一行（`tests/unit/test_angareion_log_e2e.py:121`）、`docs/aicomm.md`、`skills/aicomm/SKILL.md`、`tests/unit/test_aicomm_docs.py`。

**為什麼是一個 PR（L-1）**：程式不再呼叫 `pool-peer`、文件卻還寫 `pool-peer` 的中間態會讓 main 比合併前更糟（文件與程式不一致），文件測試也會被迫放水。文件量不大（`docs/aicomm.md` 一節、`SKILL.md` 七處），所以程式、測試、`docs/aicomm.md`、`skills/aicomm/SKILL.md`、`test_aicomm_docs.py` 一起 merge；**§2 的 PR-2 已取消**，它的工作在 1.7～1.8（原 §3、§4 的編號保留，免得舊引用失效）。

**只合這一步，main 會不會更糟？** 主線本身不會更糟：本機路徑不動、既有測試改成新 fake 後全綠、沒有新增請求種類、文件與程式一致。但這是**行為切換點**：在沒有 `~/.mylinuxpool/sidecar-link/{heartbeat,ports}` 的機器上，跨機叫醒會從「有 `pool-peer` 就能用」變成 `not-woken peer-unavailable`。所以：merge 可以（單允許先 merge），但**不要發版、不要讓 MyLinuxPool 升 pin**，直到各台開始寫那兩個檔（見 §3）；merge 前另要照 3.2 確認有沒有機器直接從 main 安裝 aicomm（L-2）。

- [ ] 1.1 新增 `src/aicomm/sidecar_link.py`（M-1）：只做「目錄→可用埠」——解析目錄（`AICOMM_SIDECAR_LINK` 或 `Path.home()/".mylinuxpool"/"sidecar-link"`；`Path.home()` 失敗、env 空字串或非絕對路徑＝沒有目錄）、心跳的「新鮮」判斷（`age = time.time() - st_mtime` 的絕對值 ≤ `LINK_FRESH_SECONDS = 90`、pid 樣式 `^[1-9][0-9]*$` 且 ≤ 2147483647、`os.kill(pid, 0)`；不得傳 0／負數／超大 pid）、`ports` 的解析（UTF-8、空行、`^[0-9]{1,5}$`、1–65535、依檔案順序第一個壞行決定、去重留第一次、升冪）、扣掉本機埠（`urllib.parse.urlsplit(sidecar_base(...)).port`，沒有埠用 80，只比埠號）。回傳 `Ports(tuple[int, ...])` 或 `Unusable(kind)`，`kind ∈ {unavailable, bad-format, out-of-range}`；**原因碼字串不放在這個模組**。`now`／`pid_alive`／home 以參數注入（預設用真的）；只 import 標準函式庫，MUST NOT import `aicomm.cli`，MUST NOT 發 HTTP。
  - 完成判準：心跳四種不新鮮、`ports` 三類錯誤與優先序、扣掉本機埠後為空、env 規則都用注入的 `now`／`pid_alive` 測到（不起行程、不改 mtime）；模組裡沒有原因碼字串、沒有 `urllib`／`http.client`。
- [ ] 1.2 `src/aicomm/cli.py`（查與戳）：`_wake_elsewhere()` 保留名字與位置，內容改成「`sidecar_link.read(...)` → `Unusable(kind)` 對應到 `peer-unavailable`／`peer-bad-answer`／`peer-port-out-of-range` → 逐埠 `GET`（四類結果）→ 逐埠 `POST`（同一張 `_poke_answer` 表）→ `_merge_peers()`」；查詢總預算 `PEER_LOOKUP_BUDGET = 10.0`（`time.monotonic()` 計）、每埠 `PEER_GET_TIMEOUT = 3.0`、每一台戳 `PEER_POKE_TIMEOUT = 5.0`；跨機的傳輸結果照 `design.md` §4.1（連線被拒 → `peer-unavailable`、逾時 → `poke-timeout`、其他連線錯誤 → `poke-error`、HTTP 層讀不懂 → `peer-bad-answer`）；常數不寫裸數字；依埠號升冪**依序**查（不平行）。
  - 完成判準：`wake:` 行的種類／原因碼／` peer:<port>`／` record=peer:<port>/<id>` 與舊版在相同情境下一致；不重送；本機路徑一行未動；`design.md` §3.9 的 C-1～C-16 每一條都對得到測試。
- [ ] 1.3 `src/aicomm/cli.py`（刪除）：`POOL_PEER_ENV`／`POOL_PEER_COMMAND`／`PEER_FIND_TIMEOUT`／`PEER_CALL_TIMEOUT`／`PEER_CHILD_GRACE`／`PEER_PORT_MIN`／`PEER_PORT_MAX`、`_peer_executable()`、`_peer_child_limit()`、`_run_pool_peer()`、`_parse_peers()`、`_lookup_peers()`、`_call_answer()`、`_poke_peer()`，以及不再用到的 import（`subprocess`／`shutil` 等）。原因碼常數與 `NOT_WOKEN_CODES`／`UNKNOWN_CODES` 不動。`AICOMM_SIDECAR_LINK` 的設定移到 `sidecar_link.py`。
  - 完成判準：`grep -n "pool-peer\|POOL_PEER\|PEER_PORT\|peer_call\|peer_find" src/aicomm/` 只剩說明新機制的註解（或全無）；「原因碼與常數一致」的測試不改也綠。
- [ ] 1.4 `tests/unit/_aicomm_fakes.py`：移除假 `pool-peer` 與相關 helper／guard；加「假 sidecar-link 目錄」（`heartbeat`／`ports`，可寫不新鮮、死 pid、pid 0／2147483648、空清單、壞格式、超範圍、重複、含本機埠、env 空字串／相對路徑）、`world.add_peer(...)`（起一個 `FakeSidecar` 並把埠寫進 `ports`）、以及 `run_aicomm()` 的新護欄（`AICOMM_SIDECAR` 必設；`AICOMM_SIDECAR_LINK` 必設，或預設目錄不存在——測試永遠讀不到真的 `~/.mylinuxpool/sidecar-link`）。
  - 完成判準：任何測試都不可能連到真的 sidecar-link 或真的 `pool-peer`；既有測試的呼叫方式不需要大改。
- [ ] 1.5 `tests/unit/test_aicomm_send.py`：照 `design.md` §5 的表逐家族改寫（保留／改寫／刪除，逐列對 C 編號），新增 `sidecar_link` 的純單元測試與 cli 的整合測試：心跳與清單的邊界（含 89／90／91 秒、未來 mtime、pid 已不在、pid 0／2147483648、`PermissionError`、`Path.home()` 失敗、env 空字串／相對路徑）、清單的一行壞＝整份不用與優先序、扣掉本機埠後為空、本機埠跳過、不快取、查詢預算用完與前埠卡住、peer 戳被重置（`unknown poke-error`）、peer 戳回應 HTTP 層讀不懂（`unknown peer-bad-answer`）、peer 的 200 body 太深（`not-woken peer-bad-answer`）、每一台 peer 只有兩條路由。
  - 完成判準：改寫後的家族全綠；刪掉的測試在 PR 說明逐一列出並說明由哪一支接手；沒有測試依賴 `pool-peer` 的 argv 或 rc；`design.md` §3.9 的 C-1～C-17 各有至少一支測試（清單列在 PR 說明）。
- [ ] 1.6 `tests/unit/test_aicomm_fakes_guard.py`：護欄改成新的斷言（沒有真的 sidecar-link／真的 `pool-peer`；對每一台 peer 只有 `GET /agents/<id>` 與 `POST /agents/<id>/poke`），並加 **C-17 的依賴守衛**：在子行程 `import aicomm.sidecar_link`，assert `aicomm.cli` 不在 `sys.modules`。
  - 完成判準：故意把 hook 拿掉、指到真的目錄、或在 `sidecar_link.py` 加一行 `import aicomm.cli` 時，護欄轉紅。
- [ ] 1.7 文件（同一個 PR，L-1）：`docs/aicomm.md` 改掉所有 `pool-peer` 的句子（`:9`、`:43`、`:47`、`:52`、`:59`、`:61-68`、`:114-127` 的「對 `pool-peer` 的依賴面」整節改成「對 sidecar-link 的依賴面：心跳、埠清單、直連的兩條路由、本機埠跳過、查不了的意思、測試鉤子 `AICOMM_SIDECAR_LINK`」）；`skills/aicomm/SKILL.md`（`:8`、`:56`、`:85`、`:86`、`:87`、`:95`、`:111`）同上、保留三條軟規則、代價段、排隊與 `record=` 的說明（不把 `woken` 寫成「已讀」）；`tests/unit/test_aicomm_docs.py` 的查紀錄與原因碼檢查改成新的跨機查紀錄寫法（`curl -s 127.0.0.1:<port>/pokes/<id>`）。
  - 完成判準：`uv run pytest tests/unit/test_aicomm_docs.py` 綠；`grep -n "pool-peer" docs/aicomm.md skills/aicomm/SKILL.md` 無輸出；原因碼清單與 `src/aicomm/cli.py` 的常數一致；`docs/` 沒有 MLP 的內部名詞。**不再有「暫時接受舊句子」的放水**。
- [ ] 1.8 回歸：只跑這張單相關的 `tests/unit/test_aicomm_send.py`、`tests/unit/test_aicomm_fakes_guard.py`、`tests/unit/test_aicomm_docs.py`（`test_angareion_log_e2e.py` 那一行改動由 PM 的整組測試驗）；與 HEAD 的基線比較沒有新紅。
  - 完成判準：三檔全綠；`git status` 只有這一步該有的路徑。

## 3. 驗收（不是 PR；原 §2 的 PR-2 已併入 §1，編號 3／4 保留讓舊引用不失效）

- [ ] 3.1 真機：**從 sidecar 開的 agent 裡**（不是人的 shell）戳另一台（或反過來），`wake:` 行是 `woken peer:<port>`，用 `curl -s 127.0.0.1:<port>/pokes/<id>` 查得到同一筆；`ports` 裡的本機埠沒有被戳第二次；同一個 agent 內心跳新鮮的判斷為真（`wake:` 不是 `peer-unavailable`，且 `ports` 上的埠真的被查過）。後兩點是 `design.md` §2.1 的 A-1／A-2 假設的唯一驗法：人的 shell 驗不到 agent 的 `HOME`／pid namespace／network namespace。
  - blocked by：1.x 已 merge 且已發版；MyLinuxPool 的 sidecar-link 已在各台寫 `~/.mylinuxpool/sidecar-link/{heartbeat,ports}`；本人的單次授權（經 PMO）。
- [ ] 3.2 發版／升 pin：MyLinuxPool 等的就是 MAS 發版；在 3.1 通過前**不要**發版、不要升 pin。**merge 前 PM 仍要確認沒有機器直接從 main 安裝 aicomm**（L-2）。MLP 已回答這一問（2026-11-13，經 PMO 轉）：**沒有 worker 映像從 main 安裝 aicomm（映像釘 sha 安裝）**；**未驗：Mac／PMO 機器上 repo 外手動安裝的 aicomm**。所以 merge 前 PM 還要**問 PMO 確認 Mac／PMO 機器**有沒有那種手動安裝：都沒有就直接 merge；有任何一台有，merge 改等 sidecar-link 上線後，否則那些機器會在 merge 當下失去跨機叫醒。沒有 sidecar-link 的機器會失去跨機叫醒這個差異，由 PMO 判斷（`design.md` §6 第 1 點）。

## 4. 歸檔前檢查（PM 收尾，archive／sync 主 spec 之前）

- [ ] 4.1 主 spec `openspec/specs/comm-skill/spec.md`：用本 change 的 delta 同步（14 MODIFIED／6 REMOVED／9 ADDED）；Purpose 段（`:4`）的「查不到時經 `pool-peer` 找別台」改成直連 sidecar-link 的埠的寫法；其餘 Purpose 不動。
- [ ] 4.2 對主 spec 跑標題逐字比對：14 條 MODIFIED 的標題都在、沒有重複；6 條 REMOVED 真的移除；9 條 ADDED 都在；MODIFIED 底下的 scenario 除了刻意改寫的（「本機連不上時改查別台」、「本機戳逾時不改走 find」、「跨機執行檔的測試掛鉤」、「`call` 回 409 與其他碼」、「子行程卡住」、「沒有戳就沒有 record」、「戳的傳輸失敗沒有 record」等）之外都在原位。
  - **歷史包袱（validate 逼的，低-2）**：`本機戳逾時不改走 find`、`跨機執行檔的測試掛鉤`、`` `call` 回 409 與其他碼 ``、`子行程卡住` 這四個 scenario 名字描述的是**已經不存在的機制**（`find`、執行檔、`call`、子行程）。改名會被 validate 擋下（MODIFIED 不能悄悄改 scenario 名），所以照留；`跨機執行檔的測試掛鉤` 的內容已換成 `AICOMM_SIDECAR_LINK`、`子行程卡住` 的內容已寫「沒有子行程可收」。
- [ ] 4.3 歸檔後另開一張小單（d）：把 4.2 那四個舊名 scenario 改名，或在主 spec 那四條加一句「名稱沿用舊版、內容已換」，免得下一個讀主 spec 的人以為跨機還在用 `find`／`call`／子行程。**接受不改也可以，但要有人明確說接受**（否則歸檔清單缺一項）。
- [ ] 4.4 `docs/architecture/decisions.md`：U-1 的結論落地（由架構師在本次修正輪補 D-28，並在 D-24／D-25 註明跨機路徑與查紀錄的措辭被取代）；若決定不補，記錄理由。
- [ ] 4.5 `openspec validate aicomm-direct-peer`（非 strict 與 strict）在歸檔前綠；`openspec validate --archived` 不因本 change 新紅。

## 5. 不在本 change

- **MyLinuxPool#1085 的 sidecar-link 本體與 `pool-peer` 第二階段退役**：心跳與埠清單怎麼寫、`127.0.0.1:P` 怎麼維護、何時刪 `pool-peer`，都歸 MLP。
- **`MLP_SIDECAR_PORT`**：aicomm 今天不讀它（`design.md` §8 U-5）；要支援另開單。
- **沒有 sidecar-link 的機器**：差異由 PMO 判斷，不在本 change。
- **真機驗收**（3.1）：不是 PR，由 PM／PMO 排。
- 不改 Angareion、`inbox`／`log`、送信本身、`--no-wake`／`--interrupt` 的語意、固定句、`AICOMM_SIDECAR` 的 loopback 檢查、poke 的 body 與原因碼集合。
