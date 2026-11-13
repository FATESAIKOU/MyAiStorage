## 0. 本張（設計作者；只寫 change）

範圍：`openspec/changes/aicomm-direct-peer/`。**不改 `src/`、`tests/`、`docs/aicomm.md`、`skills/aicomm/SKILL.md`、`openspec/specs/`、`docs/architecture/`**（那些是後續 PR 與 PM 的收尾）；不改 `pool-peer`；不自己 `openspec archive`。

- [x] 0.1 `proposal.md`：為什麼（MAS-1～3、`pool-peer` 退役）、做什麼（直連、查不了、多台、不變的清單、測試鉤子、delta 的 14／6／7）、Non-goals、Impact（含發版／升 pin 的依賴與已知差異）。
  - 完成判準：上面各項都有專段；明寫「本 change 可以先 merge，但發版／升 pin 要等 sidecar-link 上線」。
- [x] 0.2 `design.md`：現況盤點（附 `src/aicomm/cli.py` 行號）、MLP 保證的介面（只寫路徑、格式、`127.0.0.1:P`）、新行為（MAS-1～3）、逐條對照表（原因碼／逾時／其他規則）、測試對應、風險與取捨、被否決的選項、未決。
  - 完成判準：每個原因碼、每個逾時、每個安全規則都有一列「新做法／保留／刪除」與理由；不含 MLP 的內部名詞（驗收用 `grep` 檢查；只寫介面的檔案路徑、格式與「`127.0.0.1:P` 是某台的 sidecar」）。
- [x] 0.3 `specs/comm-skill/spec.md`：MODIFIED 14 條、REMOVED 6 條（各附 Reason／Migration）、ADDED 7 條；MODIFIED 的標題與主 spec 逐字一致，原有 scenario 標題全部保留（`本機戳逾時不改走 find`、`跨機執行檔的測試掛鉤`、`call` 回 409 與其他碼`、`子行程卡住` 等照留，內容改成新行為）。
  - 完成判準：`openspec validate aicomm-direct-peer --strict` 綠（非 strict 也綠）。
- [x] 0.4 `openspec validate aicomm-direct-peer --strict` 通過。
- [ ] 0.5 設計核准：專案架構師核准本 change 後，PM 才把 PR-1 交給實作隊員；非作者審查。
  - 完成判準：`design.md`／`tasks.md` 依審查意見定稿並 merge；未決 U-1（decisions.md 的 D-24／D-25）有結論。

## 1. PR-1 程式＋測試（blocked by：設計核准 0.5）

範圍：`src/aicomm/cli.py`、`tests/unit/_aicomm_fakes.py`、`tests/unit/test_aicomm_send.py`、`tests/unit/test_aicomm_fakes_guard.py`、`tests/unit/test_angareion_log_e2e.py` 的 `AICOMM_POOL_PEER` 那一行（檔名以實作為準：`tests/unit/test_angareion_log_e2e.py:121`）。

**只合這一步，main 會不會更糟？** 主線本身不會更糟：本機路徑不動、既有測試改成新 fake 後全綠、沒有新增請求種類。但這是**行為切換點**：在沒有 `~/.mylinuxpool/sidecar-link/{heartbeat,ports}` 的機器上，跨機叫醒會從「有 `pool-peer` 就能用」變成 `not-woken peer-unavailable`。所以：merge 可以（單允許先 merge），但**不要發版、不要讓 MyLinuxPool 升 pin**，直到各台開始寫那兩個檔（見 §3）。若要完全避開中間態，就等 sidecar-link 上線後再 merge；兩者都合規，由 PM 決定。

- [ ] 1.1 `src/aicomm/cli.py`（讀與驗證）：新增 sidecar-link 目錄解析（`AICOMM_SIDECAR_LINK` 或 `Path.home()/".mylinuxpool"/"sidecar-link"`；`Path.home()` 失敗＝沒有目錄）、心跳的「新鮮」判斷（mtime 絕對值 90 秒、pid 樣式、`os.kill(pid, 0)`；不得傳 0／負數）、`ports` 的解析（UTF-8、空行、`^[0-9]{1,5}$`、1–65535、去重、排序）、本機埠的計算與跳過、以及「查不了／格式不對／埠超範圍」對應到 `peer-unavailable`／`peer-bad-answer`／`peer-port-out-of-range`。每次後援重新讀（不快取）。
  - 完成判準：清單與心跳的每一個拒絕樣本都有單元測試；`not-woken peer-unavailable` 不會改走別的路徑（沒有 `pool-peer` 可執行、也不執行它）。
- [ ] 1.2 `src/aicomm/cli.py`（查與戳）：`_wake_elsewhere()` 改成「讀清單 → 逐埠 `GET`（四類結果）→ 逐埠 `POST`（同一張 `_poke_answer` 表）→ `_merge_peers()`」；10 秒查詢總預算與 3 秒單次上限、每一台戳 5 秒；跨機的傳輸結果照 `design.md` §4.1（連線被拒 → `peer-unavailable`、逾時 → `poke-timeout`、其他連線錯誤 → `poke-error`、HTTP 層讀不懂 → `peer-bad-answer`）。
  - 完成判準：`wake:` 行的種類／原因碼／` peer:<port>`／` record=peer:<port>/<id>` 與舊版在相同情境下一致；不重送；本機路徑一行未動。
- [ ] 1.3 `src/aicomm/cli.py`（刪除）：`POOL_PEER_ENV`／`POOL_PEER_COMMAND`／`PEER_FIND_TIMEOUT`／`PEER_CALL_TIMEOUT`／`PEER_CHILD_GRACE`／`PEER_PORT_MIN`／`PEER_PORT_MAX`、`_peer_executable()`、`_peer_child_limit()`、`_run_pool_peer()`、`_parse_peers()`、`_lookup_peers()`、`_call_answer()`、`_poke_peer()`，以及不再用到的 import（`subprocess`／`shutil` 等）。原因碼常數與 `NOT_WOKEN_CODES`／`UNKNOWN_CODES` 不動。
  - 完成判準：`grep -n "pool-peer\|POOL_PEER\|PEER_PORT\|peer_call\|peer_find" src/aicomm/cli.py` 只剩說明新機制的註解（或全無）；「原因碼與常數一致」的測試不改也綠。
- [ ] 1.4 `tests/unit/_aicomm_fakes.py`：移除假 `pool-peer` 與相關 helper／guard；加「假 sidecar-link 目錄」（`heartbeat`／`ports`，可寫不新鮮、死 pid、空清單、壞格式、超範圍、重複、含本機埠）、`world.add_peer(...)`（起一個 `FakeSidecar` 並把埠寫進 `ports`）、以及 `run_aicomm()` 的新護欄（`AICOMM_SIDECAR` 必設；`AICOMM_SIDECAR_LINK` 必設，或預設目錄不存在——測試永遠讀不到真的 `~/.mylinuxpool/sidecar-link`）。
  - 完成判準：任何測試都不可能連到真的 sidecar-link 或真的 `pool-peer`；既有測試的呼叫方式不需要大改。
- [ ] 1.5 `tests/unit/test_aicomm_send.py`：照 `design.md` §5 的表逐家族改寫（保留／改寫／刪除），新增心跳與清單的邊界、本機埠跳過、不快取、查詢預算用完、每一台 peer 只有兩條路由的測試。
  - 完成判準：改寫後的家族全綠；刪掉的測試在 PR 說明逐一列出並說明由哪一支接手；沒有測試依賴 `pool-peer` 的 argv 或 rc。
- [ ] 1.6 `tests/unit/test_aicomm_fakes_guard.py`：護欄改成新的斷言（沒有真的 sidecar-link／真的 `pool-peer`；對每一台 peer 只有 `GET /agents/<id>` 與 `POST /agents/<id>/poke`）。
  - 完成判準：故意把 hook 拿掉或指到真的目錄時，護欄轉紅。
- [ ] 1.7 `tests/unit/test_aicomm_docs.py`：文件檢查改成新的跨機查紀錄寫法（`curl -s 127.0.0.1:<port>/pokes/<id>`）與原因碼清單；`docs/aicomm.md`／`skills/aicomm/SKILL.md` 的改寫在 PR-2，若 PR-1 先 merge 而文件還沒改，這兩條測試要改成暫時接受舊句子（或 PR-2 與 PR-1 同一個 PR，PM 決定）。
  - 完成判準：`uv run pytest tests/unit/test_aicomm_docs.py` 在 PR 合併時是綠的。
- [ ] 1.8 回歸：只跑這張單相關的 `tests/unit/test_aicomm_send.py`、`tests/unit/test_aicomm_fakes_guard.py`、`tests/unit/test_aicomm_docs.py`；與 HEAD 的基線比較沒有新紅。
  - 完成判準：三檔全綠；`git status` 只有這一步該有的路徑。

## 2. PR-2 文件（blocked by：PR-1 merge）

範圍：`docs/aicomm.md`（`:9`、`:43`、`:47`、`:52`、`:59`、`:61-68`、`:114-127` 的「對 `pool-peer` 的依賴面」整節改成「對 sidecar-link 的依賴面：心跳、埠清單、直連的兩條路由、本機埠跳過、查不了的意思、測試鉤子 `AICOMM_SIDECAR_LINK`」）、`skills/aicomm/SKILL.md`（`:8`、`:56`、`:85`、`:86`、`:87`、`:95`、`:111`）、`tests/unit/test_aicomm_docs.py` 的對應檢查。

**只合這一步，main 會不會更糟？** 不會：只改文件與文件測試，程式行為不變；但**文件必須與已 merge 的程式一致**，所以不能先於 PR-1。

- [ ] 2.1 `docs/aicomm.md`：改掉所有 `pool-peer` 的句子（`grep -n "pool-peer" docs/aicomm.md` 無輸出）；寫明心跳與埠清單的兩個檔、新鮮的定義、本機埠跳過、查不了的三種情況、跨機查紀錄的 `curl -s 127.0.0.1:<port>/pokes/<id>`、`AICOMM_SIDECAR_LINK` 只給測試與手動。
  - 完成判準：沒有 `pool-peer` 的殘留；原因碼清單與 `src/aicomm/cli.py` 的常數一致；`docs/` 沒有 MLP 的內部名詞。
- [ ] 2.2 `skills/aicomm/SKILL.md`：同上（`grep -n "pool-peer" skills/aicomm/SKILL.md` 無輸出）；保留三條軟規則、代價段、排隊與 `record=` 的說明。
  - 完成判準：`uv run pytest tests/unit/test_aicomm_docs.py` 綠；文件沒有把 `woken` 寫成「已讀」。
- [ ] 2.3 回歸：只跑 `tests/unit/test_aicomm_docs.py`。
  - 完成判準：綠；`git status` 只有這一步該有的路徑。

## 3. 驗收（不是 PR）

- [ ] 3.1 真機：從 worker 戳另一台（或反過來），`wake:` 行是 `woken peer:<port>`，用 `curl -s 127.0.0.1:<port>/pokes/<id>` 查得到同一筆；`ports` 裡的本機埠沒有被戳第二次。
  - blocked by：PR-1 已 merge 且已發版；MyLinuxPool 的 sidecar-link 已在各台寫 `~/.mylinuxpool/sidecar-link/{heartbeat,ports}`；本人的單次授權（經 PMO）。
- [ ] 3.2 發版／升 pin：MyLinuxPool 等的就是 MAS 發版；在 3.1 通過前**不要**發版、不要升 pin。沒有 sidecar-link 的機器會失去跨機叫醒，這個差異由 PMO 判斷（`design.md` §6 第 1 點）。

## 4. 歸檔前檢查（PM 收尾，archive／sync 主 spec 之前）

- [ ] 4.1 主 spec `openspec/specs/comm-skill/spec.md`：用本 change 的 delta 同步（14 MODIFIED／6 REMOVED／7 ADDED）；Purpose 段（`:4`）的「查不到時經 `pool-peer` 找別台」改成直連 sidecar-link 的埠的寫法；其餘 Purpose 不動。
- [ ] 4.2 對主 spec 跑標題逐字比對：14 條 MODIFIED 的標題都在、沒有重複；6 條 REMOVED 真的移除；7 條 ADDED 都在；MODIFIED 底下的 scenario 除了刻意改寫的（「本機連不上時改查別台」、「本機戳逾時不改走 find」、「跨機執行檔的測試掛鉤」、「`call` 回 409 與其他碼」、「子行程卡住」、「沒有戳就沒有 record」、「戳的傳輸失敗沒有 record」等）之外都在原位。
- [ ] 4.3 `docs/architecture/decisions.md`：U-1 的結論落地（由架構師補一列並在 D-24／D-25 註明跨機路徑與查紀錄的措辭被取代）；若決定不補，記錄理由。
- [ ] 4.4 `openspec validate aicomm-direct-peer`（非 strict 與 strict）在歸檔前綠；`openspec validate --archived` 不因本 change 新紅。

## 5. 不在本 change

- **MyLinuxPool#1085 的 sidecar-link 本體與 `pool-peer` 第二階段退役**：心跳與埠清單怎麼寫、`127.0.0.1:P` 怎麼維護、何時刪 `pool-peer`，都歸 MLP。
- **`MLP_SIDECAR_PORT`**：aicomm 今天不讀它（`design.md` §8 U-5）；要支援另開單。
- **沒有 sidecar-link 的機器**：差異由 PMO 判斷，不在本 change。
- **真機驗收**（3.1）：不是 PR，由 PM／PMO 排。
- 不改 Angareion、`inbox`／`log`、送信本身、`--no-wake`／`--interrupt` 的語意、固定句、`AICOMM_SIDECAR` 的 loopback 檢查、poke 的 body 與原因碼集合。
