# 非作者審查報告：agent-sidecar 第一批 (tasks 3.1, 3.2, 3.6)

- **審查對象**：commits `fcbfb10`, `18e2f2f`, `c25cf76`, `5984c89`（作者：agy4）
- **審查者**：Gemini 3.8 Flash (High)
- **審查日期**：2026-10-11
- **對照規格**：
  - `openspec/changes/agent-sidecar-design/design.md`（D1, D2, D2a, D3 包含 10-11 spike 結果, D4, D5, D8）
  - `openspec/changes/agent-sidecar-design/specs/agent-sidecar/spec.md`
  - `openspec/changes/agent-sidecar-design/tasks.md`（tasks 3.1, 3.2, 3.6）

---

## 結論

**暫緩合併（需修正 2 項 High、2 項 Medium 問題後合入）。**

- **優點與合規部分**：
  - **架構邊界清晰**：`RealHerdrClient` 嚴格遵守「只走子命令讀 JSON」、「不執行不帶子命令的 herdr」，且主動清理並防禦 `HERDR_ENV` 等巢狀變數（`check_nesting_env`、`sanitize_herdr_env`）。
  - **指令參數精準**：比對 `herdr <子命令> --help`（包括 `tab create`、`pane run`、`agent rename`、`agent prompt`、`agent read`、`pane wait-output`、`pane process-info`、`agent send-keys`、`pane close`），指令格式與參數順序皆與 herdr CLI 完全吻合。
  - **憑證邊界嚴密**：本批次未有將 A2A token 外洩至 agent 環境的漏洞；`tab_create` 僅傳遞白名單中的 `--env`；`ModelBroker` 介面隔離清晰，不持有、不記錄真實模型供應商金鑰。
  - **原子落檔機制**：`Registry` 與 `MessageQueue` 寫入流程均採「寫入同目錄 `.tmp-...` → `flush` → `os.fsync` → `os.replace`」，在 POSIX 上具備原子性。
  - **完整測試全綠**：於隔離副本執行全 repo 單元測試，886 項測試全數通過（525.77s）。
- **待修正之關鍵缺陷**：
  - **H1 (時序破壞)**：`terminate_agent` 終止等待時間被硬編碼 `min(timeout_stage, 0.05)`，導致預設 5 秒等待在生產環境被壓縮成 50 毫秒（0.05 秒），破壞優雅退出機制。
  - **H2 (路徑崩潰)**：`MessageQueue` 佇列檔名直接使用 `identity`（未跳脫斜線 `/`），遇到真實 repo 身分（如 `owner-org/worker-team`）時，因嘗試寫入不存在的子目錄而直接拋出 `FileNotFoundError` 崩潰。
  - **M1 (誤判打斷)**：`is_working` 判斷 opencode / claude 提示詞時在終端最近 15 行進行全文字串比對，歷史訊息中若提及 `esc interrupt` 會導致閒置中的 agent 被誤判為工作中而遭受額外 Esc 鍵干擾。
  - **M2 (資料靜默遺失)**：`Registry.load()` 與 `MessageQueue._load_raw()` 遇到壞檔時靜默忽略並重設為空，後續寫入會無聲無息覆寫並銷毀既有資料。

---

## 詳細審查項目（依等級分列）

### High (2 項)

#### H1. `terminate_agent` 等待時間被硬編碼 `min(timeout_stage, 0.05)`，預設 5 秒優雅退出降級為 50ms

- **問題說明**：
  - 在 `src/sidecar/host.py` 的 `terminate_agent` 中（第 356 行與第 374 行）：
    ```python
    time.sleep(min(timeout_stage, 0.05))
    ```
  - 當調用端使用預設參數 `timeout_stage=5.0` 時，`min(5.0, 0.05)` 計算結果永遠為 `0.05` 秒（50 毫秒）。
  - 這導致發送 Ctrl+C 後僅等待 0.05 秒便升級發送 SIGTERM，再過 0.05 秒便升級發送 SIGKILL，總共僅 100 毫秒！
  - Design D4 明確規定：「終止：先 `herdr agent send-keys <name> ctrl+c`（讓 agent 收尾，等同互動模式的 SIGINT）；**等 5 秒還在**，對它的 process group 送 SIGTERM、**再 5 秒 SIGKILL**」。
  - 50ms 根本不足以讓 PTY 與進程處理 Ctrl+C、結束當前回合並將對話安全寫回 agora。
- **實測方法與結果**：
  - 模擬呼叫 `host.terminate_agent("victim", timeout_stage=5.0)`（進程在階段 1 與 2 保持存活）。
  - **實測結果**：全程僅耗時 **0.105 秒** 即執行至 SIGKILL，證實生產預設等待時間完全失效。
- **修復建議**：
  - 移除 `min(..., 0.05)`，直接使用 `time.sleep(timeout_stage)`。測試若需加速，應在測試案例中顯式傳入 `timeout_stage=0.01`。

#### H2. `MessageQueue` 檔名未跳脫 `identity`，帶斜線之身分直接觸發 `FileNotFoundError` 崩潰

- **問題說明**：
  - 在 `src/sidecar/queue.py` 中：
    ```python
    def _queue_file(self, identity: str) -> Path:
        return self.queue_dir / f"{identity}.json"

    def _save_raw(self, identity: str, messages: list[dict[str, Any]]) -> None:
        ...
        tmp_file = self.queue_dir / f".tmp-{identity}.json"
        ...
    ```
  - 在本專案與 Angareion 規範中，身份必定為 `repo × role`（例如 `owner-org/worker-team` 或 `owner-org/repo:role`），內含斜線 `/`。
  - 當 `identity="owner-org/worker-team"` 時，`tmp_file` 變成 `queue/.tmp-owner-org/worker-team.json`，而 `.tmp-owner-org` 目錄不存在，直接引發 `FileNotFoundError`。
- **實測方法與結果**：
  - 執行 `q.enqueue('owner-org/worker-team', {'id': '1', 'urgency': 5})`。
  - **實測結果**：
    `FileNotFoundError: [Errno 2] No such file or directory: '.../queue/.tmp-owner-org/worker-team.json'`。
- **修復建議**：
  - 將 `identity` 轉換為安全檔名（例如使用 `urllib.parse.quote_plus(identity)` 或將 `/`、`:` 取代為 `%2F` / `__` 等安全字元），使佇列檔案能在 `queue/` 平坦儲存。

---

### Medium (2 項)

#### M1. `is_working` 比對最近 15 行全文，歷史對話提及提示詞時會造成 False Positive

- **問題說明**：
  - 在 `src/sidecar/host.py` 的 `is_working` 與 `get_agent_status` 中：
    ```python
    screen = self.herdr.agent_read(agent_name, source="recent-unwrapped", lines=15)
    if indicator in screen:
        return True
    ```
  - 其中 `indicator` 為 `"esc interrupt"`（opencode）或 `"esc to interrupt"`（claude）。
  - 若歷史輸出或先前對話內容（最近 15 行內）出現過這組字串，即使 agent 當前已處於 idle 狀態（正在等待使用者輸入），`is_working` 仍會判定為 `True`。
  - 當以 `interrupt_if_working=True` 送信時，會對閒置中的 agent 發送 1~2 次 Esc，可能清除輸入暫存區或觸發 TUI 異常退出。
  - Design D2a 特別強調：「判斷 opencode 還在跑要看**畫面最下面一行**有沒有『esc interrupt』」。
- **實測方法與結果**：
  - 終端設定為閒置狀態，但前文包含對話：`"User: explain esc interrupt\nAssistant: ok\n> "`。
  - 呼叫 `host.is_working("my-agent")`。
  - **實測結果**：回傳 `True`（誤判）。
- **修復建議**：
  - 依 Design D2a 的指示，將 `screen` 按行分割並去除空白，僅檢查**最後 1~2 行有效行**：
    ```python
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    if lines and indicator in lines[-1]:
        return True
    ```

#### M2. `Registry.load()` 與 `MessageQueue._load_raw()` 壞檔時吞掉錯誤並重設，後續寫入會覆寫並永久銷毀資料

- **問題說明**：
  - `Registry.load()` 與 `MessageQueue._load_raw()` 在捕捉到 `Exception` 時，直接將記憶體資料重設為空集合 `{}` / `[]`，完全無日誌記錄、無警告、無備份。
  - 若檔案因手動編輯有誤或磁碟暫時異常導致 `json.load()` 解析失敗，下一次執行 `register()`、`update_status()` 或 `enqueue()` 時，`save()` 會以全新內容原子覆寫該檔案，導致原先所有資料無可挽回地遺失。
- **實測方法與結果**：
  - 故意將 `registry.json` 寫入損壞 JSON（如 `{invalid`），呼叫 `reg.load()` 得到 `{}`；隨後註冊新 agent，原損壞檔案被直接覆寫，舊記錄全部蒸發。
- **修復建議**：
  - 在遇到解析錯誤時（排除 FileNotFoundError）：
    1. 記錄警告或錯誤日誌；
    2. 自動保留備份（例如 rename 為 `registry.json.corrupt.<timestamp>`）；
    3. 或向外拋出明確例外（如 `CorruptStateError`），避免在未確認狀態下直接覆寫。

---

### Low (2 項)

#### L1. `interrupt()` 等待迴圈中 `except Exception: return True` 吞掉讀取錯誤

- **問題說明**：
  - 在 `src/sidecar/host.py` 的 `interrupt` 迴圈中，若 `agent_read` 拋出 `HerdrError` 或連線中斷，程式執行 `except Exception: return True`，將讀取異常視同「打斷成功」。
  - 讀取失敗可能是 pane 崩潰或通訊問題，盲目回傳 `True` 可能導致後續 `send_prompt` 向異常的 pane 寫入。
- **修復建議**：
  - 讀取異常時應記 log 並於超時前重試，若持續失敗應回傳 `False` 或向上拋出錯誤。

#### L2. `recover_agents` 僅比對 agent 名稱，未確認進程與 pane 存活

- **問題說明**：
  - 在 `recover_agents` 中，僅以 `rec.agent_name in live_names` 判定 agent 存活。
  - 在 herdr 中，若 agent 已執行完畢（done）但名稱尚未手動 clear，名稱可能仍會出現在列表上。
  - Design D2a / D5 提及應配合 `pane process-info` 或 `pane get` 對照前景進程。
- **修復建議**：
  - 建議恢復時進一步以 `pane_process_info` 確認仍有前景行程在運行。

---

## 突變測試 (Mutation Testing) 記錄

於獨立副本（`git archive HEAD | tar -x -C /tmp/sidecar_review_replica`）中執行之驗證結果：

| 突變測試項目 | 修改內容 | 預期反應 | 實測結果 | 評價 |
|---|---|---|---|---|
| **1. 佇列優先級排序** | `queue.py` 將 `(-urgency, at)` 改為 `(urgency, at)` | 優先級高的訊息被排至後方，測試應失敗 | **FAIL (紅燈)**：`AssertionError: at index 0: 'msg-1' != 'msg-2'` | 測試防護有效 |
| **2. opencode 打斷按鍵次數** | `host.py` 中將 opencode 的 `esc` 發送由 2 次改為 1 次 | 未發滿 2 次 Esc，測試應失敗 | **FAIL (紅燈)**：`AssertionError: assert 1 == 2` | 測試防護有效 |
| **3. 巢狀環境偵測** | `host.py` `__init__` 中將 `check_nesting_env` 拋錯邏輯 pass 掉 | 巢狀變數存在時未阻擋，測試應失敗 | **FAIL (紅燈)**：`Failed: DID NOT RAISE RuntimeError` | 測試防護有效 |
| **4. 終止超時降級** | 測量預設 `timeout_stage=5.0` 之真實等待時間 | 應符合 D4 之 5 秒等待規範 | **FAIL (實測僅耗時 0.105 秒)** | 抓出 H1 缺陷 |
| **5. 帶斜線之 identity 佇列寫入** | 傳入真實身分 `owner-org/worker-team` | 應能正常入列 | **FAIL (`FileNotFoundError`)** | 抓出 H2 缺陷 |
| **6. is_working 歷史文字干擾** | 模擬終端末尾 15 行出現過 `esc interrupt` 但當前 idle | 應回傳 `False` | **FAIL (誤判為 `True`)** | 抓出 M1 缺陷 |

---

## 給 PM 的清單

1. **請 PM 轉交 agy4 修正以下項目**：
   - **H1**：修正 `src/sidecar/host.py` 中的 `terminate_agent`，移除 `min(timeout_stage, 0.05)`，改為真實 `timeout_stage` 等待。
   - **H2**：修正 `src/sidecar/queue.py` 中的 `_queue_file` 與 `_save_raw`，對包含 `/` 與 `:` 的 `identity` 做檔名跳脫/編碼，並補上相應單元測試。
   - **M1**：修正 `src/sidecar/host.py` 中的 `is_working` 與 `get_agent_status`，改為僅比對終端末行，避免歷史對話干擾。
   - **M2**：修正 `registry.py` 與 `queue.py` 中的壞檔處理，遇損壞 JSON 時備份檔案並報警，避免靜默覆寫。
   - **L1 / L2**：視時程安排一併收斂。
2. **後續審查與合併步驟**：
   - 待 agy4 完成上述修正並補充測試後，再進行第二輪快速審查即可合併入 main/aicompany 分支。
