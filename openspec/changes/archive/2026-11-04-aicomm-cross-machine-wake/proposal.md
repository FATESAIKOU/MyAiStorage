## Why

`aicomm send` 現在只會叫醒**本機** sidecar 上的收件人：`GET /agents/<id>` 回 404 就印「不在本機，不戳」，跨機的收件人永遠叫不醒，送信的一方也看不出「叫不醒」與「叫醒了」的差別（stderr 是自由格式的中文句子，沒有程式解析它）。多台 worker 與 Mac 之後會是常態，PMO／PM／隊員常常不是同一台；訊息本身會到（Angareion），但要對方**動起來**還是得有人戳。

本人 2026-11-03 的方向（MyPMO#299）：**不做新服務**（先前「每台一個 watcher」的方案作廢），各機 sidecar 全互通（由 MyLinuxPool `pool-peer` 提供），安全議題記在 MyPMO#297、期 6 再議。#368（c；上游 M5 必做）做 aicomm 這一側：本機優先，找不到再經 `pool-peer` 找別台並戳；叫醒結果改成固定格式的一行，叫不醒要看得到、但**不能**讓呼叫端重送。

設計定稿見 `design.md`（架構師 myaistorage-arch-w1-1，2026-11-04；介面是 MyLinuxPool PR #677 design §D5 加 PR #696 的 D5 擴充）。架構師報告列的衝突條文（主 spec `comm-skill` 的 `:11`、`:44`、`:80`、`:95`、`:102`）由本 change 的 spec delta 一併改掉——光改文件不夠，主 spec 會和新行為矛盾。

## What Changes

- **本機路徑先換格式**（PR-1，不依賴 `pool-peer`）：叫醒結果的 stderr 改成固定格式、本機與跨機共用——

  ```
  wake: <agent> woken local[ interrupt]
  wake: <agent> woken peer:<port>[,<port>…][ interrupt]
  wake: <agent> not-woken <原因碼>[ <說明>]
  wake: <agent> unknown <原因碼>[ <說明>]      # 可能已經戳到：不要重送
  ```

  每個收件人剛好一行；`send` 的 exit 0 仍然只代表**已送達**。新增選用旗標 `--require-wake`：有任何一位 `not-woken` 或 `unknown` 就回 **4**（訊息已送達，不要重送）；與 `--no-wake` 同時給是 exit 1，而且在寫進 Angareion 之前就拒絕。`_wake_one` 的兜底改成 `unknown internal-error`。
- **跨機後援**（PR-2）：本機回 404／連不上／GET 逾時（還沒戳，不會重複）才 `pool-peer find <to> --json --timeout 10`；`find` 的陣列只讀每一筆的 `port` 與 `agent.state`，port 不在 5200–5399 就在 aicomm 先擋（`peer-port-out-of-range`，不呼叫 `call`）；可以戳的每一台都 `pool-peer call <port> POST /agents/<id>/poke --timeout 5`，body 與本機相同（固定句＋`interrupt`）。執行檔由 `AICOMM_POOL_PEER` 指定（測試與手動用），沒設就從 PATH 找 `pool-peer`；找不到是 `not-woken peer-unavailable`，不算錯。`find`／`call` 的結束碼與逾時語意照 design §3；aicomm 只打兩個路由（`GET /agents/<id>`、`POST /agents/<id>/poke`）。
- **spec delta**（`specs/comm-skill/spec.md`）：MODIFIED 六條與新行為衝突的 requirement：「send 先寫進 Angareion，成功才算送達」、「叫醒的對象與條件」、「戳的內容固定且不含訊息」、「sidecar 位址固定為預設 loopback 位址」、「沒有硬控制，叫醒的節制由 SKILL.md 軟規則承擔」、「叫醒是盡力而為，可關閉」。ADDED 十三條：「跨機叫醒只經 `pool-peer` 的兩個用法」、「找 `pool-peer` 執行檔」、「跨機叫醒的時間預算」、「讀 `pool-peer find` 的結束碼」、「解析 `pool-peer find` 的陣列」、「`find` 找到的每一台怎麼戳」、「戳的回應怎麼判讀（本機與跨機共用）」、「本機戳的連線結果」、「讀 `pool-peer call` 的結束碼」、「`wake:` 行的文法」、「同一個收件人戳了好幾台時取一個結果」、「叫醒的原因碼清單」、「`send` 的結束碼與 `--require-wake`」。
- **文件**（PR-1、PR-2 各自改）：`docs/aicomm.md` 的「送達與叫醒」「結束碼」「依賴」三段、`skills/aicomm/SKILL.md` 的「送達與叫醒」「結束碼」兩段，寫明「exit 0 只代表已送達；看 stderr 的 `wake:` 行；exit 4 不要重送」，原因碼清單與 cli 的常數一致。
- **決策清單**：`docs/architecture/decisions.md` 加 D-24（叫醒結果的格式與 `--require-wake`；跨機只經 `pool-peer`），並把交接節 #368 那行改成「PMO 11-04 指示開工、PR-2 merge 前確認 D5 擴充已定案」。

## Non-goals

- **#314 的 `aicomm wake`**（只戳不送，b，未 merge）不在本單；#314 定案後若要跨機，重用本單的 `_wake_one`（同一套規則與 `wake:` 格式），另開單。
- **`pool-peer` 本身**：CLI、埠段、Gateway 綁定、PATH 上有沒有它，都歸 MyLinuxPool（#677／#696）。
- **真機驗收**（worker 戳 Mac 或反過來）不是 PR，是 PR-2 之後另一步；要 MLP 部署 `pool-peer` 加上本人的單次授權。
- 不改 Angareion、不改 sidecar 的介面；不改 `aicomm inbox`／`aicomm log`／送信本身；不改 `--no-wake`、`--interrupt` 的語意與固定句；不改 `AICOMM_SIDECAR` 的 loopback 檢查。
- 不在本 change 動 `src/`、`tests/`：那是 PR-1 與 PR-2。

## Impact

- 本 change 的產出：`openspec/changes/aicomm-cross-machine-wake/`（`proposal.md`、`design.md`、`tasks.md`、`specs/comm-skill/spec.md`、`.openspec.yaml`），以及同一張 PR-0 的 `docs/design-notes/368-aicomm-wake.md`（縮成指向本 change 的短 note）與 `docs/architecture/decisions.md`（D-24 與交接節）。
- 後續 PR 的落點（不在本 change，見 `tasks.md`）：`src/aicomm/cli.py`、`tests/unit/_aicomm_fakes.py`、`tests/unit/test_aicomm_send.py`、`tests/unit/test_aicomm_docs.py`、`docs/aicomm.md`、`skills/aicomm/SKILL.md`。
- 主 spec `openspec/specs/comm-skill/spec.md`：由本 change 的 delta 在歸檔時同步（歸檔與同步主 spec 是 PM 的收尾）。
- 跨 repo（只列）：MyLinuxPool 的 `pool-peer`（#677＋#696）與部署；MyPMO 的環境說明措辭不在本單。
- 公開 repo：不含帳號、機器位址、憑證、真的 session id、私人資訊。
