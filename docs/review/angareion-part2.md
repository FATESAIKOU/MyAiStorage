# 非作者審查報告：Angareion 後半 (tasks 2.1-2.4, 4.1)

- **審查對象**：commits `ee077d8`, `05c125a`（作者：agy1）
- **審查者**：Gemini 3.8 Flash (High)
- **審查日期**：2026-10-11
- **對照規格**：
  - `openspec/changes/angareion-a2a-v0/design.md`（D1, D2, D3, D5, D6, D7, D10）
  - `openspec/changes/angareion-a2a-v0/specs/angareion/spec.md`（Requirement: a2a 指令、ack 與 inbox 的游標、六欄位訊息與 front matter 表示法、訊息大小、身分與群組位址）
  - `openspec/changes/angareion-a2a-v0/tasks.md`（tasks 2.1-2.4, 4.1）

---

## 結論

**建議暫緩合併，待修正 1 項 High、2 項 Medium 缺陷後合入。**

- **優點與合規部分**：
  - **stdout / stderr 分離精確**：`a2a send` 成功時，標準輸出 stdout 嚴格僅輸出 `<id> <channel>\n`（供管線串接）；進度提示、速率節流等待通知、重試說明與錯誤訊息一律輸出至 stderr。
  - **Exit Code 嚴格合規**：完全遵照規範：
    - `0`：成功（包含重複 ack exit 0、無未讀訊息 exit 0）；
    - `1`：輸入或用法錯誤（位址格式無效、群組位址被拒、未帶 `--new` 且頻道不存在、ack 找不到 ID 部分失敗等）；
    - `2`：後端、網路或設定錯誤（Config 缺失、Token 錯誤、HTTP 401/403、速率超額等）。
  - **秘密防護周全**：CLI 主程式與各子命令（`cmd_send`、`cmd_inbox`、`cmd_ack`）在拋出或捕捉例外向 stderr 輸出前，全面透過 `mask_token` 過濾，token 絕不流入終端。
  - **群組與格式防禦確實**：`to` 或 `from` 輸入群組位址時被明確攔截並拒絕（exit 1）；訊息超過 GitHub 65,536 字元上限時由序列化階段阻擋；附件支援 `--attach name=path`（inline）與 `--ref name=locator`。
  - **ack 批次處理與部分失敗語意**：`a2a ack` 支援多 ID 參數，對重複 ack 輸出 notice 且 exit 0；遇到不存在的 ID 輸出錯誤並記錄 `has_error = True`，在確認其餘有效訊息後以 exit 1 結算。

- **待修正之關鍵缺陷**：
  - **H1 (本地狀態與快取脫鉤導致掉信)**：`cmd_inbox` 檢查 `cached_messages_dict` 為空時僅將 `since_query` 重設為 `None`，卻仍將 `state.json` 裡的 `etag` 傳給後端。後端回傳 304 Not Modified 後，`cmd_inbox` 直接跳過快取重建並回傳空清單（`[]`），導致未 ack 的訊息被靜默隱藏，破壞「本機快取刪掉能完整重建信箱」的核心保證。
  - **M1 (快取檔名方法誤用導致多身分覆寫)**：`cmd_inbox` 以 `hasattr(state, 'identity_slug')` 嘗試取得身分 slug，但 `identity_slug` 為 `state` 模組的頂層函式而非實例方法，導致所有身分的快取檔名皆硬編碼為 `inbox_cache.json`，多身分在本機切換時快取資料會相互踩踏。
  - **M2 (次級速率限制 403 拒絕等待大於 10 秒)**：`cmd_send` 遇到後端 403 `RateLimitError` 時，加上了 `if e.retry_after <= 10:` 判斷。GitHub 常態之次級限制通常要求等待 60 秒，被程式直接攔截並 exit 2，未能依 Spec Scenario「等待 retry-after 後重試一次」的規範重試。
  - **L1 (重複 ack 單元測試斷言缺漏)**：`test_ack_workflow` 對重複 ack 僅 assert exit code 0，未驗證後端是否真的未發出多餘留言，存在測試盲區。
  - **L2 (RateLimiter 檔案未加鎖)**：本地節流記錄 `rate_limit.json` 讀寫未設排他鎖，多進程併發時存在 lost update 競態。

---

## 詳細審查項目（依等級分列）

### High (1 項)

#### H1. 快取檔案遺失或損壞時，帶入既有 ETag 觸發 304 導致信箱被清空 (Silent Message Drop)

- **問題說明**：
  - 在 `src/angareion/cli.py` 第 321-340 行：
    ```python
    cache_file = ...
    cached_messages_dict: dict[str, dict[str, Any]] = {}
    if cache_file.is_file():
        try:
            cached_messages_dict = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cached_messages_dict = {}

    etag = state.etags.get("messages")
    # If we have no cached messages, query since=None to ensure complete rebuild
    since_query = state.since if cached_messages_dict else None

    try:
        res = backend.list_messages(since=since_query, etag=etag)
    ...
    if not res.not_modified:
        # 重建快取與狀態
        ...
    ```
  - 當快取檔案（如 `inbox_cache.json`）被使用者手動刪除、清理或損壞時，`cached_messages_dict` 為 `{}`。
  - 程式正確將 `since_query` 設為 `None`，**但第 329 行仍然從 `state` 讀出舊的 `etag` 並傳給 `backend.list_messages`**！
  - 後端比對 ETag 相符，回傳 304 Not Modified（`res.not_modified = True`）。
  - `cmd_inbox` 遇到 304 跳過解析，`cached_messages_dict` 依舊為空字典，隨後在第 374 行過濾訊息時回傳 `[]`！
  - 只要信道上沒有新訊息產生（ETag 未改變），該收件人執行 `a2a inbox` 永遠只會看到空信箱，即使信道上有未 ack 的訊息！
  - 這嚴重違反 Spec Requirement：
    「inbox 的游標、ack 快取與 ETag MUST 只存在本機狀態目錄，而且是快取：刪掉之後重跑 MUST 仍然列出所有還沒 ack 的訊息（不重不漏）」
- **實測方法與結果**：
  - 寄件者發送訊息，收件人首次執行 `a2a inbox` 成功讀出。
  - 刪除本機 `inbox_cache.json`，再次執行 `a2a inbox`。
  - **實測結果**：輸出 `[]`（信箱全空，原本未 ack 之訊息直接蒸發）。
- **修復建議**：
  - 當 `cached_messages_dict` 為空（需要完整重建快取）時，`etag` 也必須同時重設為 `None`，強制向後端進行完整 200 讀取：
    ```python
    etag = state.etags.get("messages") if cached_messages_dict else None
    ```

---

### Medium (2 項)

#### M1. 快取檔名判斷誤用 `hasattr(state, 'identity_slug')`，所有身分共用單一 `inbox_cache.json`

- **問題說明**：
  - 在 `src/angareion/cli.py` 第 321 行：
    ```python
    cache_file = config.state_dir / f"{state.identity_slug(identity) if hasattr(state, 'identity_slug') else 'inbox'}_cache.json"
    ```
  - 檢查 `src/angareion/state.py`，`identity_slug` 是頂層獨立函式（`def identity_slug(identity: dict[str, Any]) -> str:`），並不是 `LocalState` 的成員方法。
  - 因此 `hasattr(state, 'identity_slug')` 恆為 `False`，條件式永遠落入 fallback 值 `'inbox'`。
  - 產生的快取檔名永遠是 `inbox_cache.json`。
  - 若一台機器有多個職務或透過 `--identity` 切換身分收信，所有身分的快取訊息全混入同一份 `inbox_cache.json`，相互污染與覆寫。
- **修復建議**：
  - 在 `cli.py` 頂端匯入 `from angareion.state import LocalState, identity_slug`。
  - 將檔名計算改為：
    ```python
    cache_file = config.state_dir / f"{identity_slug(identity)}_cache.json"
    ```

#### M2. `cmd_send` 遇到 GitHub 次級限制 403 且 retry-after > 10s 時拒絕等待，與 Spec Scenario 不一致

- **問題說明**：
  - 在 `src/angareion/cli.py` 第 282-293 行：
    ```python
    except RateLimitError as e:
        if e.retry_after <= 10:
            sys.stderr.write(f"Rate limit hit, retrying in {e.retry_after}s...\n")
            time.sleep(e.retry_after)
            try:
                backend.post_message(args.channel, msg)
            except BackendError as e_inner:
                sys.stderr.write(f"Error: {mask_token(str(e_inner), token)}\n")
                return 2
        else:
            sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
            return 2
    ```
  - Spec Requirement 與 Scenario：
    「Scenario: 遇到次級速率限制
    - **WHEN** 發文時後端回次級速率限制（403、附 retry-after）
    - **THEN** 等待 retry-after 後重試一次；仍失敗就 exit 2，訊息沒有貼出去，可以重跑」
  - GitHub 回傳的 Secondary Rate Limit 標頭通常為 `Retry-After: 60`（60 秒）。
  - `cli.py` 寫入硬限制 `if e.retry_after <= 10:`，導致所有典型的 GitHub 403 次級限制直接被擋下並 exit 2，完全不進行等待重試。
- **修復建議**：
  - 依 Spec Scenario，403 的等待重試不受本地 10 秒發文節流限制，應印出提示後等待 `retry_after` 秒並重試一次；重試再失敗才以 exit 2 退出。

---

### Low (2 項)

#### L1. 重複 ack 測試斷言缺漏，無法防禦重複發送 ack

- **問題說明**：
  - 在 `tests/unit/test_angareion_cli.py` 的 `test_ack_workflow` 中：
    ```python
    # Repeating ack is notice, exit 0, no duplicate
    ret_repeat = main(
        ["ack", msg_id],
        backend=backend,
    )
    assert ret_repeat == 0
    ```
  - 測試僅檢查 `ret_repeat == 0`，未檢查後端是否多發出了一則留言，亦未檢查 stderr。
  - 當我們在突變測試中故意刪除 `if target_id in state.acked: continue` 時，後端確實重複張貼了一則 ack 留言，但測試依然綠燈通過。
- **修復建議**：
  - 在測試中追加斷言：`assert len(backend.list_messages().messages) == 2`，並檢查 capsys stderr 含有 `"already acknowledged"`。

#### L2. `RateLimiter` 檔案無鎖定，併發可能發生 Race Condition

- **問題說明**：
  - `RateLimiter` 讀寫 `rate_limit.json` 採無鎖機制。若有多個 agent 同時送出訊息，可能因併發讀寫造成歷史記錄遺失。
- **建議**：
  - 考量 v0 為低頻操作，可於期 2 或有併發需求時以 `fcntl.flock` 補充排他鎖。

---

## 突變測試 (Mutation Testing) 記錄

於獨立副本（`git archive HEAD | tar -x -C /tmp/angareion_review_replica`）中執行之驗證結果：

| 突變測試項目 | 修改內容 | 預期反應 | 實測結果 | 評價 |
|---|---|---|---|---|
| **1. stdout / stderr 分離** | `cmd_send` 在 stdout 多輸出除 `<id> <channel>` 以外之文字 | stdout 格式不符，測試應失敗 | **FAIL (紅燈)**：`AssertionError: assert 3 == 2` | 測試防護有效 |
| **2. 頻道不存在退出碼** | `cmd_send` 缺少 `--new` 時將 exit code 1 改為 2 | 錯誤類別不符，測試應失敗 | **FAIL (紅燈)**：`AssertionError: assert 2 == 1` | 測試防護有效 |
| **3. 錯誤訊息金鑰遮蔽** | `main` 頂層捕捉異常時移除 `mask_token` | stderr 露出原始金鑰，測試應失敗 | **FAIL (紅燈)**：`AssertionError: assert 'ghp_...' not in ...` | 測試防護有效 |
| **4. 重複 ack 幂等性斷言** | `cmd_ack` 移除 `if target_id in state.acked: continue` | 重複 ack 張貼多餘留言，測試應失敗 | **PASS (綠燈，漏抓！)** | 抓出 L1 測試盲區 |
| **5. 快取遺失 ETag 假 304** | 刪除 `inbox_cache.json`，保留 `state.json` 的 `etag` 執行 inbox | 應重新抓取未讀訊息 | **FAIL (信箱清空，回傳 `[]`)** | 抓出 H1 關鍵缺陷 |
| **6. 身分 slug 檔名回退** | 印出 `cache_file` 計算結果 | 應使用 `identity_slug` | **FAIL (回退為 `inbox_cache.json`)** | 抓出 M1 缺陷 |
| **7. 403 Retry-After > 10s 重試** | 後端回傳 403 `retry_after=60` | 應等待並重試一次 | **FAIL (實測未等待重試，直接 exit 2)** | 抓出 M2 缺陷 |

---

## 給 PM 的清單

1. **請 PM 轉交 agy1 修正以下項目**：
   - **H1**：修正 `src/angareion/cli.py` 之 `cmd_inbox`，當 `cached_messages_dict` 為空時，強制將 `etag` 設為 `None`，確保信箱快取能向後端完整重建，避免掉信。
   - **M1**：修正 `src/angareion/cli.py` 之 `cache_file` 檔名計算，改用頂層 `identity_slug(identity)` 函式，防止多身分共用單一快取檔。
   - **M2**：修正 `cmd_send` 對 403 次級速率限制之處理，符合 Spec Scenario，在後端回傳 403 時等待指定秒數重試一次。
   - **L1**：補充 `tests/unit/test_angareion_cli.py` 中 `test_ack_workflow` 的斷言，確認重複 ack 時後端留言數未增加且 stderr 有 notice 提示。
2. **Task 4.1 驗證結論**：
   - `uv run python -m compileall -q src` 通過無報錯。
   - 單元測試套件執行完整通過。
3. **後續審查與合入**：
   - 本後半審查報告已歸檔至 `docs/review/angareion-part2.md`，待 agy1 完成 H1、M1、M2 修正後即可順利合入。
