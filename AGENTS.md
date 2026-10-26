# AGENTS.md

## 這個 repo 的 issue 是工單

這個 repo 是大專案「MyAiStorage」。它的 issue 是一人公司工作樹上的工單，規則以 [FATESAIKOU/MyPMO 的工單規則](https://github.com/FATESAIKOU/MyPMO/blob/main/%E5%B7%A5%E5%96%AE%E8%A6%8F%E5%89%87.md) 為準（private repo；讀不到時照下面的摘要）。MyPMO 的 Actions 每天檢查一次，違規會通知本人。

### 開 issue

- 層級：**b** 功能組新增或重構／**c** 功能組擴張或改修（好幾步）／**d** 小擴張或 bug（一個 PR 做得完）。**a（大專案新設或轉向）不開在這裡，開在 MyPMO。**
- 誰能開：b 只有本人能開（PM 只能以「提案中」提出）；隊員開的 c 要這個專案的 PM 或 reviewer 核准才成立；d 隊員可以自由開，但要另一位隊員同意。
- label：恰一個 `層級:`、恰一個 `狀態:`（構想／提案中／待開工／進行中／等本人／阻塞）；`狀態:等本人` 時再加至少一個 `等本人:核准|合併|驗收|裁定|動作`。沒有 `層級:` 的 issue 會被列為「樹外」。
- 內文段落：`## 為什麼`、`## 來源`、`## 相關`；等本人時加 `## 等什麼`。
- 從屬用 sub-issue（最多一個父，父的層級要比子高）；依存用 blocked by。
- 這是 public repo：**不寫 MyBrain 的內容與私人資訊**，需要時連到 MyPMO 的工單。

### 做事

- 開工改 `狀態:進行中`；這個大專案同時進行中的 b、c 合計最多 2 張（d 不限；2026-10-11 本人的週決定 MyPMO#47 放寬）。
- 卡在本人：`狀態:等本人`＋種類。卡在別的工單或外部條件：`狀態:阻塞`＋blocked by 或寫明等什麼。
- 做完以 completed 關閉；不做以 not planned 關閉並加 `否決`（本人不同意）或 `作廢`（前提不存在）。
- 本人的決定（核准、裁定、驗收、結案別人的工單）由 MyPMO 的決定檔 PR 套用，不在這裡直接改。

## 這個 repo 的做法

### 測試隔離

- 每個測試由 `tests/conftest.py` 的 autouse fixture（`isolated_home`）把 `HOME`、`AGORA_CONFIG`、`AGORA_CACHE_DIR`、`AGORA_STATE_DIR`（注意是 `_DIR`）指到該測試的暫存目錄，Angareion 的三個變數（`ANGAREION_CONFIG`、`ANGAREION_STATE_DIR`、`ANGAREION_TOKEN_FILE`）也一樣，並清掉 `ANGAREION_IDENTITY`、`XDG_*`、`CLAUDE_CONFIG_DIR`；不要在測試裡讀寫真的家目錄與 agora 目錄。
- 沒有 pytest 保護的探針、手動腳本：自己建暫存目錄，把 `HOME`、`AGORA_CONFIG`、`AGORA_CACHE_DIR`、`AGORA_STATE_DIR`、`TMPDIR` 都指進去，並清掉 `XDG_*`、`CLAUDE_CONFIG_DIR`，再執行。
- 探針執行前，要在**同一個行程**裡 assert `Paths.from_env()` 的 config／cache／state 與 `HOME`、`TMPDIR` 都在暫存目錄底下，不符就中止（範例：`tests/perf/measure_continue_mem.py` 的 `NOT ISOLATED` 檢查）。
- 新的測試模組（或共用 helper）第一行照既有寫法 `import _guard`；測試檔放在 repo 的 `tests/` 底下，用 `uv run pytest` 跑（放在 repo 外沒有 conftest，會被擋下）。規格見 `openspec/specs/test-isolation/spec.md`。
- 不連真的 Drive：`AGORA_RCLONE` 指到 `tests/fakes/fake_rclone.py`（用包裝腳本呼叫）；`AGORA_UPLOAD=inline` 讓上傳在前景完成。測試用的假 agent 在 `tests/fakes/`；單元測試（整合測試除外）沒設假 agent 時，agent 指令是 `/nonexistent/...`，會直接失敗而不是跑真的；`AGORA_AGY_HOME` 同樣只對單元測試設到暫存目錄。

### 跑測試

- 在 #210 修好之後，不用再加 `env -u ANGAREION_IDENTITY`：外面設著的 `ANGAREION_IDENTITY` 由 `tests/conftest.py` 的 `isolated_home` 統一清掉；需要身分的測試自己用 `monkeypatch.setenv` 設。
- 只停自己開的行程（記下自己的 pid 再停）；不要用 `pkill`／`killall` 依名稱殺 pytest 或 agora 行程，會誤殺別人的。

### 測試資料

- 假的 secret 不能符合掃描器的樣式（例如 `sk-`、`ghp_` 開頭）：在執行時把前綴拆開組字串，檔案裡不留完整樣式（範例：`tests/unit/test_credscan.py` 開頭的說明與 `"sk" + "-ant-" + rnd(40)`）。
