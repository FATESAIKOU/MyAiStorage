## Why

AiCompany 期 1 需要在每台 worker（以及 Mac）上控制 AI agent 的程式（issue #31）。本人 2026-10-11 看過三版設計後定案 **v4**（`docs/proposals/aicompany-design-v4.md`；v2、v3 與本人的回饋也在同一個目錄）：

- sidecar 只做 agent 的生命週期控管，給它的指示只有四件事：開啟、關閉、搜集狀態（只限它開的）、戳（可選中斷；「暫停」就是戳＋中斷）。
- 外部用 HTTP 控制，三種形式：立即、定時、輪詢；HTTP 只綁本機，不做認證。
- 一個 worker 一個 daemon，Linux 跑得起來就行，Mac 也能用；不基於 herdr。
- Claude Code、opencode 是介面的實作；用它們的程式化模式開（先做 spike）。
- 開啟時用 agora 指定繼承的 Session（new／continue／fork／merge），並在 Session 打上 tag `sidecar:<id>`；id 由呼叫方給。
- 相依只有 sidecar → agora、sidecar → LLMGateway；不依賴任何信道工具。
- 開 agent 用 LLMGateway 的共通 JSON（呼叫方只給 subscription 的 id，sidecar 自己 `get`）；額度用完時登錄冷卻、重新挑一把（不挑回剛用完的）、用 continue 重開並送一則 `resume_prompt`；全部冷卻時自動掛一個到最早解除時刻的重開 job；LLMGateway 無法確認時 open 回 503、換把時以固定間隔再試；挑哪一把是可替換的小介面（預設照 priority）。
- 狀態全部揮發（只在記憶體），不處理重啟；收到 SIGTERM 先進入關閉中（新請求回 503、到期的 job 不再執行）、停掉 scheduler，再逐一 close 全部 agent 才結束。
- 狀態轉移只在 scheduler 執行緒做、每個 agent 同時只有一個；`GET /agents` 讀最後一次的狀態、不觸發任何轉移；換把中途失敗走 waiting-quota 與重試 job。
- LLMGateway 的連線設定放在獨立的檔案，每次操作重新讀，改它不必重啟 sidecar。

本人 2026-10-12 中午看完介面圖後，把設計改成 **v5**（唯一依據 `design-v5-changes.md`；以 v4 為底，只列改的地方）：
- `open` 的 `session` 參數＝agora session id（選填，接續用）；agent id 用共通命名（`<repo 名>-<pm|arch|impl|test>[-<id>]`）。
- `AgentDriver` 拿掉 `env_for`（憑證與環境變數直接當 `launch` 的參數）；要 agent 立刻讀＝先打斷（如果在跑）再送；`state` 拿不到恢復時間就報「現在＋1 小時」；`stop` 溫和＝閒了才退、強制＝立刻，退出＝結束整個 agent 程序。
- `cooldown` 只送 `until`（拿掉 `window`）；不用 `lgw connect`，`SubscriptionSource`＝curl 本機 LLMGateway port 或本機檔案（拿掉 gateway extra 的設計）。

本人 2026-10-12 對 v5 疑點的裁定（`v5-decisions.md`）再把三件事定案：
- `open` 只收 agora id（接續）或不給（新開）：fork／merge 的入口拿掉——由 PMO／PM 先在 agora 做好 Session 再以 id 交進來；`SessionStore` 的 fork／merge 能力保留。
- `/jobs` 保留現狀（at／every／until＋`GET`／`POST`／`DELETE`），越簡單越好、不加功能。
- opencode driver 改寫成 opencode v2：先做 v2 server API 的 spike（Mac 上已是 2.0.26，用 `--standalone` 的私有 server，不干擾共用的背景服務），再改 driver；worker 映像的 opencode 一併升 v2（MyLinuxPool 開單）。

這個 change 取代 v1 的 `agent-sidecar-design`（只在 branch `aicompany-p1`，沒合進 main）。v1 的程式大多不適用（herdr、收信迴圈、中繼都拿掉了），可以沿用的部分在 tasks 裡列出。

## What Changes

- 新增 daemon `sidecar`（`src/sidecar/`）：HTTP 控制面（`/agents`、`/jobs`）、四個動作、job 排程（at／every）、自動換把與重開。
- 內部介面：`AgentDriver`（實作：Claude Code、opencode）、`SessionStore`（實作：agora）、`SubscriptionSource`（實作：LLMGateway；本機開發用讀 JSON 檔的實作）、`Selector`（預設照 priority）。
- agora 增加 sidecar 需要的能力（見 `session-continue` 的 delta）：`continue --headless`（程式化模式、照常定期寫回、stderr 印新 Session 的 id）、`--headless-port`、附加 tag 的 `--add-tag`。
- v5 差異（見 design）：`session` 參數改為選填的 agora session id（fork／merge 的入口拿掉、store 的能力保留）；agent id 用共通命名；`AgentDriver` 拿掉 `env_for`；`cooldown` 只送 `until`；`SubscriptionSource` 改為 curl 本機 port 或本機檔案；`/jobs` 定案保留（不加功能）；opencode driver 改寫成 v2（spike 後；worker 映像的 opencode 一併升 v2）。

## Capabilities

### New Capabilities

- `sidecar`：四個動作、HTTP 的三種形式、agent 的開法、Session 的繼承與 tag、subscription 與換把、揮發的狀態。

### Modified Capabilities

- `session-continue`：增加程式化開法（`--headless`）與附加 tag（`--add-tag`）。

## Impact

- 程式：`src/sidecar/`（新）、`src/agora/`（小改）、`pyproject.toml`（console script `sidecar`）。
- 測試：`tests/unit/test_sidecar_*.py`（假 driver、假 agora、假 subscription 來源；不連網、不開真的 agent）。
- 文件：`docs/sidecar.md`（HTTP 介面、設定、怎麼在 worker 與 Mac 上跑）。
- 其他專案：LLMGateway（`list`／`get`／`cooldown`，sidecar 直連本機 port）；MyLinuxPool（worker 上 supervisor 拉起 `sidecar run`；worker 映像的 opencode 升 v2）。
