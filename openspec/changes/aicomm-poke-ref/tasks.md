## 0. 本張（設計作者；只寫 change）

範圍：`openspec/changes/aicomm-poke-ref/` 與 `docs/architecture/decisions.md` 的 D-26 列。**不改 `src/`、`tests/`、`docs/aicomm.md`、`skills/aicomm/SKILL.md`、其他 `docs/` 與既有主 spec**（那是實作 PR）；不改架構師的介面決定；不自己 `openspec archive`。

- [ ] 0.1 `proposal.md`：為什麼（巡檢要用訊息 id 查叫醒紀錄，撐 M1）、做什麼、影響（含「戳的文字固定；body 可多帶 `via`／`ref`」的改寫說明）、與 MLP#786（PR #796）的關係、風險。完成判準：上面各項都有專段；寫明 Q-1、Q-2 為 PM 決定。
- [ ] 0.2 `design.md`：D-1～D-6（含理由與被否決選項）、契約 C-1～C-14 原樣編號、影響清單、模組切割、跨專案介面、風險 R-1～R-6。完成判準：C-1～C-14 各出現一次；被否決選項沒有被刪；不含新介面。
- [ ] 0.3 `specs/comm-skill/spec.md`：MODIFIED 四條（戳的內容固定且不含訊息、不改 Angareion 與 sidecar、跨機叫醒只經 `pool-peer` 的兩個用法、`find` 找到的每一台怎麼戳）；標頭與主 spec 逐字一致；保留主 spec 既有的每一個 scenario 標題，新增 scenario 涵蓋 C-1～C-14。完成判準：用程式比對標頭逐字一致；`openspec validate aicomm-poke-ref` 綠。
- [ ] 0.4 `tasks.md`：本節與下面的實作切分。完成判準：每一條有可驗的完成判準，實作段標「blocked by：設計核准」。
- [ ] 0.5 `docs/architecture/decisions.md`：新增 D-26，D-25 註明「由 D-26 擴充」（與設計文件同一個 PR）。完成判準：`git diff` 只有 D-25 出處欄加字與新增 D-26 一列。
- [ ] 0.6 設計核准：架構師核准本 change 後，PM 才把實作段交給實作隊員。

## 1. 實作 PR（一張）

範圍：`src/aicomm/cli.py`、`tests/unit/_aicomm_fakes.py`、`tests/unit/test_aicomm_send.py`、`docs/aicomm.md`。（`skills/aicomm/SKILL.md` 不改；若 `tests/unit/test_aicomm_docs.py` 需要跟著加文件斷言則一併。）

**blocked by：設計核准**（0.6）。

一張 PR 的理由：`_poke_body`、`_poke_ref`、假 sidecar／假 `pool-peer`、17 處逐字比對 body 的現有測試與文件是同一個行為的兩面；拆開的話第一張會讓現有測試紅。MLP 端已上線，不依賴任何外部條件。

- [ ] 1.1 `src/aicomm/cli.py`：新常數 `POKE_VIA_LOCAL = "aicomm"`、`POKE_VIA_PEER = "aicomm-peer"`、`POKE_REF_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")`（與 `RECORD_ID_RE` **分開定義**）；新純函式 `_poke_ref(out: str) -> str | None`（取第一行、strip、`fullmatch`）與 `_poke_body(interrupt: bool, via: str, message_id: str | None) -> dict`（鍵序 `text, interrupt, via, ref`；`message_id` 為 `None` 時不帶 `ref` 鍵）。完成判準：C-5、C-8、C-13 的單元測試綠；`_poke_ref` 對 64 字原樣回傳、65 字與接 `\x00` 回 `None`。
- [ ] 1.2 `src/aicomm/cli.py`：`_cmd_send` 在 angareion 成功之後、叫醒迴圈之前算一次 `message_id`，依序往下傳 `_wake_one` → `_wake_one_inner` → `_wake_elsewhere` → `_poke_peer`（參數名 `message_id`，不用 `ref`）；本機（約 `:628`）與跨機（約 `:515`）都改呼叫 `_poke_body`，不再各自手寫 dict；`_wake_one` 的兜底不變；`:62` 的註解改寫（D-6）。完成判準：C-1、C-2、C-3、C-4、C-6、C-7、C-9、C-10、C-11、C-12、C-14 綠；`wake:` 行、原因碼清單、結束碼的現有斷言不改也綠；`git diff` 沒有動到原因碼常數。
- [ ] 1.3 `tests/unit/_aicomm_fakes.py`：確認假 angareion 能指定 `send` 的 stdout（現有 `FAKE_ANG_SEND_OUT` 以 `print` 輸出，會多一個 `\n`；`  <id>  \n`、`<id>\nextra\n` 整合樣本（`\r\n` 只在純函式測試）若 `print` 做不到，擴充成可原樣輸出）；假 sidecar 與假 `pool-peer` 能取回 poke body 與 `call` 的 stdin 逐字。完成判準：既有測試不需要改假 sidecar 的呼叫方式；新測試能設定 C-3、C-4 的所有樣本。
- [ ] 1.4 `tests/unit/test_aicomm_send.py`：新增 C-1～C-14 的測試。C-3 五個樣本（`"has space"`、65 個 `a`、`""`、`"abcé"`、`"a.b"`）各驗一次；C-4：`\r\n`、`  <ULID>  \n`、`<ULID>\nextra\n` 三個樣本在 `_poke_ref` 純函式測試各驗一次，整合測試只用後兩個（`\r` 不放整合測試：subprocess 的 universal newlines 會把 `\r\n` 轉成 `\n`）；C-5 為純函式測試；C-11 同時斷言 `text == WAKE_TEXT` 與鍵集合只會是兩種之一。完成判準：C-1～C-14 全綠。
- [ ] 1.5 `tests/unit/test_aicomm_send.py`：逐字比對 body 的現有測試跟著改（預期中的改動，不算回歸）。位置（以設計時的 HEAD 為準，實作時以 grep `"interrupt"` 為準）：
  - 本機 `world.sidecar.pokes() == [{"text": WAKE_TEXT, "interrupt": …}]`：`:216`、`:273`、`:280`、`:293`、`:299`、`:338`、`:610`、`:631`、`:726`、`:739`、`:752`、`:1639`、`:1961`；
  - 鍵集合 `set(poke) == {"text", "interrupt"}`：`:387`（改為 C-11）；
  - 跨機 stdin 比對：`:1228`、`:1377`、`:1378`、`:1963`。
  
  完成判準：改完後以 `grep -n '"interrupt": \(True\|False\)}' tests/unit/test_aicomm_send.py` 看不到只有兩個鍵的 body 比對（新的比對含 `via`，合格時含 `ref`）；改的只是期望值，沒有放寬成「只檢查 text」。
- [ ] 1.6 `docs/aicomm.md`：`:103`（對 sidecar 的依賴面表格，body 的現有說明）與 `:120`（`pool-peer call` 那一列，目前沒有 body 欄，要補一句 stdin 與本機同一個 body、含 `via: aicomm-peer`）的 body 改成 `{"text", "interrupt", "via", "ref"?}`，並加說明：`via` 是自己宣告的來源，只當線索、不當證據、不能拿來授權；`ref` 是這則訊息的 id，只在格式合格時才帶；這不是 `send --ref`；舊的 sidecar 會忽略這兩個鍵；查紀錄時可用紀錄裡的 `ref` 對照訊息 id。完成判準：文件含上述各點；`grep -n "aicomm-peer" docs/aicomm.md` 抓得到 `:120` 那一列；`grep -n '"interrupt"' docs/aicomm.md` 沒有只列兩個鍵的 body；`skills/aicomm/SKILL.md` 沒有改動。
- [ ] 1.7 改壞驗證（隊員在副本做，驗完刪掉）：(a) 拿掉 `fullmatch` 改成 `match`＋`$` → C-5 轉紅；(b) 不驗 `ref` 直接帶第一行 → C-3 轉紅；(c) 跨機改回手寫 dict → C-13 或 C-2 轉紅；(d) 讓 `ref` 不合格時帶 `null` → C-3 轉紅。完成判準：四處都轉紅；沒轉紅的要列出來。
- [ ] 1.8 回歸：只跑 `tests/unit/test_aicomm_send.py`、`tests/unit/test_aicomm_docs.py`（以及 C-8 若放在別的檔就那一檔）；與 HEAD 的基線比較沒有新紅；`git status` 只有上面列的路徑。

## 2. 驗收（不是 PR）

- [ ] 2.1 真機：從 worker 戳另一個 agent，sidecar 的叫醒紀錄含 `via: "aicomm"` 與 `ref` = 該則訊息 id（跨機為 `aicomm-peer`）。
- blocked by：1.x 已 merge；本人的單次授權（經 PMO）。不在本 change：由 PM／PMO 在 M1 收束時排，本次不勾。

## 3. 歸檔前檢查（PM 收尾時，archive／sync 主 spec 之前）

- [ ] A.1 對主 spec 跑標題逐字比對：四條 MODIFIED 的標題都在、沒有重複；原有 scenario 標題都還在。
- [ ] A.2 `openspec validate aicomm-poke-ref`（非 strict）綠。
