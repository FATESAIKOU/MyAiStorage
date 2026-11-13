## Why

aicomm 今天的跨機叫醒（#368）完全靠 MyLinuxPool 的 `pool-peer`：本機 sidecar 查不到人時，`pool-peer find <id> --json --timeout 10` 找出哪幾台有這個 id，再 `pool-peer call <port> POST /agents/<id>/poke` 逐台戳（`src/aicomm/cli.py:587-613`、`:495-561`）。MyLinuxPool#1085 要把跨機的 sidecar 發現改成每台自己登錄、本機直連，`pool-peer` 兩階段退役；aicomm 的要求是 MAS-1～3（出處：MyLinuxPool#1085 的「對 MAS 的要求」，本人 2026-11-13 同意的方向）。aicomm 不換掉 `pool-peer`，它就不能刪。

另外，`pool-peer` 是一支要在 PATH（或 `~/.mylinuxpool/bin`）上找到的執行檔（`:419-441`），它的 stdout 也要 aicomm 自己解析（`:463-532`）。改成本機直連之後，跨機叫醒只依賴兩個檔案與 loopback 連線，路徑更短、也少一層子行程與逾時寬限。

## What Changes

- **MAS-1：跨機叫醒不再呼叫 `pool-peer`。** 本機 404／連不上／逾時之後，讀 `~/.mylinuxpool/sidecar-link/` 的心跳與埠清單，對清單裡每個埠 P 直接 `GET http://127.0.0.1:P/agents/<id>`；找到且狀態可戳就 `POST http://127.0.0.1:P/agents/<id>/poke`。本機 sidecar 的查詢與戳照舊（`127.0.0.1:8471`）。poke 的 body 不變（`text`／`interrupt`／`via=aicomm-peer`／`ref`）。
- **MAS-2：查不了就說查不了。** 心跳不新鮮、檔不在、沒有可用的埠 → `not-woken peer-unavailable`（與今天 `pool-peer` 結束碼 2 同一個語意），不改走別的路徑（不退回 `pool-peer`、不讀別的檔、不試非 loopback 位址）。
- **MAS-3：同一個 id 在兩台就兩台都戳**（與今天 `find` 全列的語意相同），結果合併成一行。
- **不變的**：`wake:` 行的文法與原因碼集合、本機優先、`--no-wake`／`--require-wake`／`--interrupt` 的語意、名字以 `-` 開頭的拒絕、poke 的 body、`AICOMM_SIDECAR` 的 loopback 檢查。原因碼逐條對照（含 `not-found`、`lookup-incomplete`、`peer-port-out-of-range`、`peer-bad-answer` 的新來源）見 `design.md` §4。
- **測試鉤子換掉**：`AICOMM_POOL_PEER`、PATH 與 `~/.mylinuxpool/bin/pool-peer` 的搜尋全部移除；改用 `AICOMM_SIDECAR_LINK` 指定清單目錄（只給測試與手動用）。
- **spec delta**（`specs/comm-skill/spec.md`）：MODIFIED 十四條（`send` 的送達、叫醒的對象與條件、戳的內容、sidecar 位址、沒有硬控制、叫醒是盡力而為、不改 Angareion 與 sidecar、戳的回應判讀、本機戳的連線結果、跨機時間預算、`wake:` 行的文法、多台取一個結果、原因碼清單、`send` 的結束碼）、REMOVED 六條（`pool-peer` 的兩個用法、找執行檔、讀 `find` 結束碼、解析 `find` 陣列、讀 `call` 結束碼、`find` 找到的每一台怎麼戳）、ADDED 九條（直連 sidecar-link 的埠、讀心跳、心跳的 pid 驗證、讀埠清單、清單的錯誤是整份的、「查不了」的結果、每一個埠怎麼查、每一個埠怎麼戳、跨機查詢與戳的連線結果）。每一條 REMOVED 都寫了 Reason 與 Migration；ADDED／MODIFIED 的正文引用編號契約 C-1～C-17（`design.md` §3.9）。
- **文件與主 spec**：`docs/aicomm.md` 與 `skills/aicomm/SKILL.md` 的 `pool-peer` 段改成直連的寫法（後續 PR）；主 spec 由 delta 在歸檔時同步（PM 收尾）。

## Capabilities

### New Capabilities

（無。）

### Modified Capabilities

- `comm-skill`：跨機叫醒的傳輸從 `pool-peer`（子行程、argv、結束碼、stdout 解析）改成直連本機的 sidecar-link 埠（兩個檔案＋loopback 的兩條路由）；原因碼集合與 `wake:` 行文法不變，但每個跨機原因碼的來源與時間預算依 `design.md` §4 對照更新。

## Non-goals

- **不改 `pool-peer`**（MyLinuxPool 的）：CLI、埠段、Gateway 綁定、何時刪，都歸 MyLinuxPool#1085 的第二階段。
- **不改 MLP 的心跳與埠清單格式**：aicomm 只讀，不寫、不修、不猜其他路徑。
- **不新增跨機請求**：對每一台 sidecar 仍只有 `GET /agents/<id>` 與 `POST /agents/<id>/poke`；不加查詢子指令、不加 `GET /pokes/…`、不加快取。
- **不改本機路徑**：位址、逾時、狀態判斷、409 的判讀都不動；`MLP_SIDECAR_PORT` 不引入（今天 aicomm 不讀它，見 `design.md` §8 U-5）。
- **不寫程式**：本 change 只有 openspec 文件；`src/`、`tests/`、`docs/aicomm.md`、`skills/aicomm/SKILL.md` 是後續 PR。
- **不動 `openspec/specs/`**（主 spec 同步是歸檔時 PM 的事）與 `docs/architecture/decisions.md`（D-24／D-25 的過時措辭由架構師在修正輪補 D-28 與註記，見 `design.md` §8 U-1；那不在作者的 commit 路徑）。
- **真機驗收**不是 PR：要等 sidecar-link 上線與本人的單次授權。

## Impact

- 本 change 的產出：`openspec/changes/aicomm-direct-peer/`（`proposal.md`、`design.md`、`tasks.md`、`specs/comm-skill/spec.md`、`.openspec.yaml`）。
- 後續**一個 PR**（程式＋測試＋文件，L-1 合成）：`src/aicomm/sidecar_link.py`（新）、`src/aicomm/cli.py`、`tests/unit/_aicomm_fakes.py`、`tests/unit/test_aicomm_send.py`、`tests/unit/test_aicomm_fakes_guard.py`、`tests/unit/test_angareion_log_e2e.py`（只改提到 `pool-peer` 的那一行）、`docs/aicomm.md`、`skills/aicomm/SKILL.md`、`tests/unit/test_aicomm_docs.py`。合成一個 PR 的理由：文件與程式若分成兩步，中間態的 main 會讓文件寫著已退役的 `pool-peer`，文件測試也得放水。
- 主 spec `openspec/specs/comm-skill/spec.md`：由本 change 的 delta 在歸檔時同步；Purpose 段的「查不到時經 `pool-peer` 找別台」也要改。
- 跨 repo（只列）：MyLinuxPool#1085（sidecar-link 的心跳與埠清單、pool-peer 退役）；MyLinuxPool#786（poke 的 `via`／`ref`／`poke_id`／`poke_outcome`，不變）。
- **發版／升 pin**：本 change 可以先 merge，但**發版與 MyLinuxPool 升 aicomm 的 pin 要等 sidecar-link 上線**（各台開始寫那兩個檔）；在那之前，已發版的 aicomm 在沒有檔案的機器上跨機叫醒會變成 `not-woken peer-unavailable`（已知差異，交 PMO 判斷，見 `design.md` §6）。
- 公開 repo：不含帳號、機器位址、憑證、真的 session id、私人資訊；只寫介面的檔案路徑、格式與「`127.0.0.1:P` 是某台的 sidecar」。
