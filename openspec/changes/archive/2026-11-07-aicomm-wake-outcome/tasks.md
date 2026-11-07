## 0. 本張（設計作者；只寫 change）

範圍：`openspec/changes/aicomm-wake-outcome/` 與 `docs/architecture/decisions.md` 的 D-25 列。**不改 `src/`、`tests/`、`docs/aicomm.md`、`skills/aicomm/SKILL.md`、其他 `docs/` 與既有主 spec**（那是實作 PR）；不改架構師的介面決定；不自己 `openspec archive`。

- [x] 0.1 `proposal.md`：為什麼（期 5 四次無聲停擺）、做什麼、影響（含撤回主 spec `:205-210`「不要求 sidecar 新增介面」的說明、與 MyLinuxPool#786 的非同步上線、Q-1、D-7／F-1）。完成判準：上面各項都有專段；明寫「PM 已接受 Q-1、PMO 無異議」與次選 exit 5。
- [x] 0.2 `design.md`：D-1～D-5、D-7（含理由與被否決選項）、§4 模組切割、§5 影響清單表、§6 跨專案介面（poke-records-v1，已談定）、§7 測試策略、§8 風險與未決。契約 C-1～C-17 原樣、編號不變。完成判準：C-1～C-17 各出現一次；被否決選項沒有被刪；§6 不再有「暫定」；本單範圍外的那一條只在 §8 未決節留一句。
- [x] 0.3 `specs/comm-skill/spec.md`：MODIFIED 五條（不改 Angareion 與 sidecar、戳的回應判讀、`wake:` 行的文法、多台取一個結果、`send` 的結束碼與 `--require-wake`）；標頭與主 spec 逐字一致；保留主 spec 既有的每一個 scenario 標題，新增 scenario 涵蓋 C-1～C-15、C-17。完成判準：`openspec validate aicomm-wake-outcome` 綠（非 strict 與 strict）。
- [x] 0.4 `tasks.md`：本節與下面的實作切分。完成判準：每一條有可驗的完成判準，實作段標「blocked by：設計核准」。
- [x] 0.5 `openspec validate aicomm-wake-outcome` 通過（非 strict 與 strict 都綠）。
- [x] 0.6 設計核准：架構師核准本 change 後，PM 才把實作段交給實作隊員。
  - 證據：設計 PR #409 已 merge（`design.md`／`tasks.md` 定稿，decision D-25）；實作 PR #411 已 merge，代表核准後才交辦。

## 1. 實作 PR（建議一張）

範圍：`src/aicomm/cli.py`、`tests/unit/_aicomm_fakes.py`、`tests/unit/test_aicomm_send.py`、`tests/unit/test_aicomm_docs.py`、`docs/aicomm.md`、`skills/aicomm/SKILL.md`。

**blocked by：設計核准**（0.6）。

建議一張 PR 的理由：`_Wake` 的欄位、`_poke_answer` 的判讀、`_merge_peers` 的合併、假 sidecar 的回應、測試與文件是同一個行為的兩面；拆成兩張會讓第一張的測試寫不出 C-1～C-11（假 sidecar 還沒有欄位），第二張又要改第一張的斷言。D-7／F-1 改的是同一個錯誤 body 的判讀，併在同一張才不會互相衝突（Q-2）。這張 PR 不依賴 MLP 上線（欄位是選用的），可以獨立 merge。文件一起進（decisions 已在設計 PR）。

- [x] 1.1 `src/aicomm/cli.py`：`_Wake` 加 `queued: bool = False` 與 `records: tuple[str, ...] = ()`；`line` 仍是唯一組字串的地方，照 D-1 的文法與順序（` interrupt`／` queued`／` record=`）。新常數 `RECORD_ID_RE`（fullmatch）、`POKE_OUTCOME_QUEUED`、`WAKE_QUEUED`。
  - 證據：PR #411（commit 7ff8e53）；`src/aicomm/cli.py:113-119`（`WAKE_QUEUED`／`RECORD_ID_RE`／`POKE_OUTCOME_QUEUED`）、`:256-289`（`_Wake` 與 `line`）、`:327`（`RECORD_ID_RE.fullmatch`）；測試 `tests/unit/test_aicomm_send.py` 的 `test_406_c1`、`c2`、`c3`、`c4`、`c9_*`、`c10_*`、`c11_*`。
  - 完成判準：C-1、C-2、C-3、C-4、C-9、C-10、C-11 的單元測試綠；`test_aicomm_send.py` 內 `wake:` 出現 81 次（含 `wake:` 的斷言行 66 行）不改也綠（假 sidecar 預設不回 id）。
- [x] 1.2 `src/aicomm/cli.py`：`_poke_answer(status, body)` 同時回種類、原因碼、合格的 `poke_id` 與 `queued`；本機與跨機共用，各自補 `local`／`peer:<port>` 前綴。**任何 HTTP 碼**都讀 `poke_id`，200 另外讀 `poke_outcome`。不合格的值當沒有，不改種類與原因碼。`_refusal_code`（D-7／F-1）依序找巢狀 `error.code`、頂層 `code`、頂層字串 `error`，第一個是字串的就定案；定案的值不認得、或三處都不是字串時是 `http-409`，不再往下找。
  - 證據：PR #411；`src/aicomm/cli.py:352-374`（`_refusal_code`）、`:376-397`（`_poke_answer`）；測試 `test_406_c5_*`、`test_406_c6_*`、`test_406_c15_*`、`test_406_c17_*`。
  - 完成判準：C-3、C-5、C-6、C-15、C-17 綠；`poke_id` 不合格時不印 `record=`、`poke_outcome` 非 `"queued"` 時不印 ` queued`，每位收件人仍恰好一行；巢狀與平面的 409 body 都對到相同的原因碼；C-15 的優先序三個樣本（巢狀 weird＋頂層 `agent_not_running` → `http-409`；巢狀碼數字 `5` → `agent-not-running`；只有 `message` → `http-409`）都對。
- [x] 1.3 `src/aicomm/cli.py`：`_merge_peers` 依 D-3 合併 `records`（含失敗的那幾台、依 port 由小到大）與 `queued`（只認每一次成功的戳都排隊）。
  - 證據：PR #411；`src/aicomm/cli.py:539-561`（`_merge_peers`）；測試 `test_406_c8_one_sent_and_one_queued_is_not_queued`、`test_406_c8_every_woken_peer_queued_is_queued`、`test_406_c8_a_failed_peer_still_lists_its_record`。
  - 完成判準：C-8 的三個樣本綠（一台 sent 一台 queued 不印 ` queued`；兩台 queued 印；一台 409 一台 200 queued 是 `woken peer:5202 queued` 且 `record=` 含兩台）。
- [x] 1.4 `tests/unit/_aicomm_fakes.py`：假 sidecar 可設 poke 的 200／400／404／409／413／500／503 body（帶或不帶 `poke_id`／`poke_outcome`，含巢狀 `error.code`），預設不帶；假 `pool-peer` 的 `call` 可回帶欄位的 body 與 rc。
  - 證據：PR #411；`tests/unit/_aicomm_fakes.py:156-166`（`poke_status`／`poke_body` 預設 `{}`）、`:210`（`poke_bodies[name]` 逐一覆寫）；測試 `test_406_c7_*`（跨機 200 帶欄位）、`test_406_c15_*` 的 `transport="call"`（巢狀錯誤 body）。
  - 完成判準：既有測試不需要改假 sidecar 的呼叫方式；新測試能分別設定每個收件人的回應。
- [x] 1.5 `tests/unit/test_aicomm_send.py`：新增 C-1～C-13、C-15、C-17。**本機項**（C-1～C-6、C-9／C-10／C-12 中的本機項）用本機假 sidecar；**跨機項**（C-7／C-8、C-9／C-10／C-15 中的跨機項：`peer-unavailable`、`lookup-incomplete`、`peer-port-out-of-range`、`call` rc 3／rc 2／約定外）用假 `pool-peer`。C-11 的每一行用 `re.fullmatch` 比對 D-1 的 regex，且 D-1 的反例（interrupt 與 queued 並列、`woken local bogus`、`record=x/y`、含空白的 id、65 字元 id）必須不匹配。C-13 沿用 fixture 的 `foreign_requests() == []` 護欄，並 assert 沒有 `GET /pokes/…`、`pool-peer` 只有 `find`／`call … poke`、poke body 只有 `text`／`interrupt`。
  - 證據：PR #411；`tests/unit/test_aicomm_send.py:1591-2030` 的 `test_406_c1`～`test_406_c17`（含 `WAKE_406_RE` 與反例 `test_406_c11_the_grammar_rejects_broken_lines`、`test_406_c13_the_new_fields_add_no_request_and_no_subcommand`）。
  - 完成判準：C-1～C-13、C-15、C-17 全綠；`git diff` 沒有動到原因碼常數的清單。
- [x] 1.6 `tests/unit/test_aicomm_docs.py`：`test_c18` 加 `queued`／`record=` 的形式；新增 C-14：兩份文件都含 `woken local queued`、`record=`、「回報一律帶 `--to`」、`GET /pokes/`（本機與跨機兩條查法）、「`queued` 不要重送」。
  - 證據：PR #411；`tests/unit/test_aicomm_docs.py:279-288`（`test_c18_the_wake_line_grammar_is_written_down` 加 `woken local queued record=local/<id>` 與 `record=<records>`）、`:337-350`（`test_406_c14_queued_and_the_record_id_are_written_down`）。
  - 完成判準：C-14 綠；「原因碼與常數一致」的現有測試不變也綠；文件沒有把 `woken` 寫成「已讀」。
- [x] 1.7 `docs/aicomm.md`、`skills/aicomm/SKILL.md`：照 D-5 改「送達與叫醒」「結束碼」「依賴」三段；寫明排隊的處理、`not-woken` 之後的處理、`unknown` 不要重送、`record=` 的兩條查法與「回報一律帶 `--to`」。文法的 `[ <說明>]` 要拿掉（`docs/aicomm.md:33-36`、`skills/aicomm/SKILL.md:68-71`），另寫「每一行之後沒有其他文字」。
  - 另須寫明（MLP#786 r3、架構師核准時補充）：沒查到 agent 的 poke（404／400／413）的紀錄只放在 1000 筆的小緩衝，`GET /pokes/<id>` 可能很快回 404 `poke_not_found`；文件要寫「查回 404 poke_not_found＝紀錄已過期，不代表沒叫醒」。
  - 證據：PR #411；`docs/aicomm.md:33-36`（文法，`[ <說明>]` 已拿掉）、`:44`（`queued` 不要重送／不是原因碼）、`:54`（回報一律帶 `--to`）、`:59`（兩條查法與 404 `poke_not_found` 不代表沒叫醒）；`skills/aicomm/SKILL.md:68-71`、`:81`、`:85`、`:87`、`:99`（同五項）；`grep -n "<說明>"` 無輸出（見報告）。
  - 完成判準：C-14 綠；`grep -n "queued" docs/aicomm.md skills/aicomm/SKILL.md` 看得到「不要重送」與「不是原因碼」的說明；`grep -n "<說明>" docs/aicomm.md skills/aicomm/SKILL.md` 沒有輸出；兩份文件含「404 poke_not_found」的說明。
- [x] 1.8 回歸：只跑這張單相關的 `tests/unit/test_aicomm_send.py`、`tests/unit/test_aicomm_docs.py`；與 HEAD 的基線比較沒有新紅。
  - 證據：PR #411（commit 7ff8e53 只動六個路徑：`docs/aicomm.md`、`skills/aicomm/SKILL.md`、`src/aicomm/cli.py`、`tests/unit/_aicomm_fakes.py`、`tests/unit/test_aicomm_docs.py`、`tests/unit/test_aicomm_send.py`）；本次歸檔重跑這兩檔全綠（原樣輸出在報告）。
  - 完成判準：兩檔全綠；`git status` 只有上面六個路徑。

## 2. 驗收（不是 PR）

- [ ] 2.1 真機：從 worker 戳 PMO 側（或反過來），`wake:` 行帶 `record=peer:<port>/<id>`，用 `pool-peer call <port> GET /pokes/<id>` 查得到同一筆。
- blocked by：1.x 已 merge；MyLinuxPool#786 已上線並確認 R-1～R-4；本人的單次授權（經 PMO）。
- 不在本 change：真機驗收由 PM／PMO 在 M1 收束時排（brief #406 歸檔），本次不勾。

## 3. 歸檔前檢查（PM 收尾時，archive／sync 主 spec 之前）

- [x] A.1 主 spec 的 Purpose 段（`spec.md:4`）加一句「`wake:` 行附 sidecar 紀錄 id，便於對照」；其餘 Purpose 不動。
  - 證據：本次歸檔在 `openspec/specs/comm-skill/spec.md:4` 的「各自只守自己的介面」之後加「；`wake:` 行附 sidecar 紀錄 id，便於對照」，其餘字句未動。
- [x] A.2 對主 spec 跑標題逐字比對：五條 MODIFIED 的標題都在、沒有重複；原有 scenario 標題都還在（尤其「本機戳回 409 與其他碼」「`call` 回 409 與其他碼」「409 的 body 看不懂或沒有錯誤碼」「200 以外的 2xx」「每個收件人剛好一行」「多台取一個結果」「`--require-wake`」七條）。
  - 證據：程式比對（腳本見報告）：5 條 delta `### Requirement:` 標題在主 spec 各出現 1 次、delta 與主 spec 都無重複；5 條 requirement 底下的原有 scenario（1／4／1／1／6 個）全部在原位；上列七條都在（`--require-wake` 是 requirement 標題，其下 6 個 scenario 都在）。
- [ ] A.3 `openspec validate aicomm-wake-outcome`（非 strict）綠；`openspec validate --archived` 不因本 change 紅。
  - 結果（2026-11-07）：歸檔前 `openspec validate aicomm-wake-outcome` 綠（exit 0）；`openspec validate --archived` 歸檔前已有 8 個 archived change 紅（既有），歸檔後本 change 以 18/20 列入紅（未完成的是 2.1 真機驗收與本條），第二句未達成。要它綠只能把 2.1 勾成已完成（不實）或把它移出 tasks；照 brief（真機驗收不在本 change、不勾）保留，交 PM 決定。
- [x] A.4 本 change 的跨專案介面照 poke-records-v1 定稿；範圍外的選用請求欄位由 PM 另開單處理。
  - 證據：`design.md` §6.0「R-1～R-4 都已談定，本單沒有待 MLP 確認的跨專案介面項目」；§6 的 R-1～R-4 與 `docs/architecture/decisions.md:33`（D-25）一致；§8「未決（不在本單）」的 D-6（poke 帶 `via`／`ref`）由 PM 另開增補單。
- [x] A.5 設計 PR 已含 D-25（`docs/architecture/decisions.md`）；D-25 與 `docs/architecture/overview.md` 沒有互相矛盾；overview 這次不補 aicomm 模組地圖（範圍外）。
  - 證據：`docs/architecture/decisions.md:33` 有 D-25（核准者模式、Q-1／Q-2、2026-11-07、MLP#786 poke-records-v1）；`docs/architecture/overview.md:5` 明寫 aicomm 還沒收錄、之後再補，與 D-25 沒有矛盾；本次 `git status` 不含 overview。

## 4. 不在本 change

- **P-1**：廣播時在 stderr 多印一行提示（行為變更，PM 未指示）。
- **P-2**：`aicomm wake-log <record>` 或任何唯讀查詢子指令（D-4）。
- **MyLinuxPool#786 的 sidecar 實作**：poke 回應欄位、紀錄保存、`GET /pokes/{id}`、`pool-peer` 不改寫 body。
- **真機驗收**（2.1）：由 PM／PMO 在 M1 收束時排。
- 不改 Angareion、`inbox`／`log`、送信本身、`--no-wake`／`--interrupt` 的語意、固定句、`AICOMM_SIDECAR` 的 loopback 檢查。
