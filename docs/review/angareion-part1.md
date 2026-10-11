# 非作者審查報告：Angareion 前半 (tasks 1.3, 1.4, 1.6)

- **審查對象**：commits `95aa673`, `3d935dd`, `8167e5f`（作者：agy1）
- **審查者**：Gemini 3.8 Flash (High)
- **審查日期**：2026-10-11
- **對照規格**：
  - `openspec/changes/angareion-a2a-v0/design.md`（D4, D5, D6, D8）
  - `openspec/changes/angareion-a2a-v0/specs/angareion/spec.md`（GitHub 後端與輪詢、發文前的不重複檢查、token 的範圍與保管）
  - `openspec/changes/angareion-a2a-v0/tasks.md`（tasks 1.3, 1.4, 1.6）

---

## 結論

**建議暫緩合併，待修正 1 項 High、2 項 Medium 缺陷後合入。**

- **優點與合規部分**：
  - **Protocol 與 Mock 隔離完善**：`GitHubBackend` 透過 `HttpTransport` 協議抽象化 stdlib `urllib`，所有單元測試皆使用 `FakeTransport` 進行無聯網注入，徹底阻絕實體網路連線與外洩風險。
  - **Token 嚴格保護與防洩**：`load_token` 在 POSIX 上強制檢查檔案權限為 `0600`，拒絕過寬（0644/0666/0777）與過窄（0400）權限；例外訊息中路徑與權限清楚、絕不輸出 token 內容；`mask_token` 在 HTTP 異常捕捉中自動將金鑰遮蔽為 `***`。
  - **條件式輪詢 (304) 實作正確**：`list_messages` 正確帶入 `If-None-Match: <etag>` 標頭，遇到 HTTP 304 Not Modified 立即回傳 `MessageListResult([], etag, not_modified=True)`，不重複解析且不打擾 downstream。
  - **次級速率限制與認證錯誤分流**：HTTP 401 拋出 `AuthenticationError`；HTTP 403 正確解析 `Retry-After` 標頭並裝載入 `RateLimitError(retry_after=...)`。
  - **語意一致性高**：`FakeBackend` 與 `GitHubBackend` 在 `create_channel`、`find_channel`、`post_message`、`list_messages` 的回傳型別（包含 `MessageListResult` tuple unpacking 與屬性訪問）及主要異常型別高度吻合。

- **待修正之關鍵缺陷**：
  - **H1 (重複發送防護穿透)**：`post_message` 防重複檢查使用 `issues/{issue_number}/comments?per_page=30`，因 GitHub issues comments 端點預設為依留言時間**由舊到新 (Ascending)** 排序，當話題留言超過 30 則時，API 只會取得最早的 30 則留言，近期發送逾時重試的訊息無法被比對，導致逾時重試時重複張貼留言。
  - **M1 (內文關鍵字誤判阻擋發文)**：`post_message` 防重複檢查採用原始字串比對 `f"id: {msg_obj.id}" in body`，若歷史留言的 Markdown 內文中提及該訊息 ID（例如討論或引述其他工單 ID），會觸發 False Positive 誤判已送達，導致真正的新訊息被靜默吞掉不發送。
  - **M2 (分頁錯誤靜默中斷導致掉信)**：`list_messages` 在多頁抓取時，若第 2 頁及後續頁面遇到 HTTP 4xx/5xx 錯誤，以 `break` 靜默終止迴圈並回傳第 1 頁拿到的新 ETag，造成呼叫端更新本機快取游標，後續頁面的訊息永久遺失。
  - **M3 (頻道列表錯誤靜默忽略)**：`list_channels` 遇到 HTTP 4xx/5xx 錯誤時直接 `break`，未向上拋出 `BackendError`，使上層無法得知查詢失敗。

---

## 詳細審查項目（依等級分列）

### High (1 項)

#### H1. `post_message` 防重複檢查僅抓取最早 30 則留言，留言數超過 30 則時防重複機制失效

- **問題說明**：
  - 在 `src/angareion/github.py` 第 258 行：
    ```python
    recent_url = f"repos/{self.owner}/{self.repo}/issues/{issue_number}/comments?per_page=30"
    resp_recent = self._request("GET", recent_url, headers=self._headers())
    ```
  - GitHub REST API 之 `GET /repos/{owner}/{repo}/issues/{issue_number}/comments` 端點的預設排序為**建立時間由舊到新 (Ascending order)**，且該端點不支援反向排序參數。
  - 當話題累積超過 30 則留言時，`per_page=30` 永遠回傳**第 1 到 30 則歷史留言**（最早的留言），而不是最新的 30 則留言。
  - 當寄件者或 sidecar 因網路連線逾時而重試送出第 31+ 則訊息時，該端點回傳的清單中絕不會出現剛才送出的留言，導致防重複檢查失效，必定重複發布 comment。
  - 這違反 Spec Requirement「發文前的不重複檢查：a2a send 在網路失敗後重試時，MUST 先用訊息的 id 查同一個 channel 最近的留言；id 已經在信道上時 MUST 當成成功，不重複貼」。
- **實測方法與結果**：
  - 模擬 issue #1 已有 35 則留言，第 35 則留言 ID 為 `01MSG_NUM_35`。
  - 當再次呼叫 `backend.post_message("my-chan", msg_35)` 時：
  - **實測結果**：`post_message` 未能偵測到已存在的 comment 35，發送了額外的 `POST` 請求（POST count = 1）。
- **修復建議**：
  - 若 issue comments 總數可能超過 30 則，可利用帶 `since` 時間參數（例如由當前時間往前算 10 分鐘 `since=...`）查詢近期的 comments，或先讀取 issue 的 comments 總數計算最後一頁分頁，確保取得的是最新的留言。

---

### Medium (3 項)

#### M1. `post_message` 防重複檢查比對整個 comment body，內文引述 ID 會觸發 False Positive 吞信

- **問題說明**：
  - 在 `src/angareion/github.py` 第 262-263 行：
    ```python
    for c in resp_recent.json():
        body = c.get("body", "")
        if f"id: {msg_obj.id}" in body or f'id: "{msg_obj.id}"' in body:
            comment_url = c.get("html_url", "")
            return comment_url
    ```
  - 此處是直接在整個留言字串 `body` 做子字串搜尋，而不是解析 YAML front matter。
  - 若其他 agent 或使用者在先前的留言 Markdown 內文中討論、引用了該 ID（例如：`Hey, please look at id: 01TARGETID`），此判斷式即判定該 ID 已存在。
  - 後續發送 ID 為 `01TARGETID` 的訊息時，會誤以為「此訊息已經貼出」，直接回傳引述留言的 URL 並終止發送，導致真訊息被靜默吞掉。
- **實測方法與結果**：
  - 建立一則既有留言，其 front matter ID 為 `01OTHERID`，但內文含有 `Hey, look at message id: 01TARGETID`。
  - 呼叫 `backend.post_message("my-chan", target_msg)`（ID 為 `01TARGETID`）。
  - **實測結果**：POST requests count = 0，回傳了既有引述留言的 URL，新訊息完全未貼出。
- **修復建議**：
  - 比對時應呼叫 `parse_message(body)`，僅比對解析後的 `parsed.id == msg_obj.id`；或以正則式限制只比對 front matter 內 `^id:\s*...$` 行，避免受內文干擾。

#### M2. `list_messages` 多頁抓取遇 4xx/5xx 錯誤以 `break` 靜默忽略，導致 ETag 推進且訊息永久遺失

- **問題說明**：
  - 在 `src/angareion/github.py` 第 308-312 行：
    ```python
    while url:
        if url != first_url:
            current_resp = self._request("GET", url, headers=self._headers())
            if current_resp.status >= 400:
                break
    ```
  - 當抓取第 1 頁成功後，GitHub 回傳了 `new_etag` 與第 2 頁之 `Link: <...>; rel="next"`。
  - 若在抓取第 2 頁時遇到短暫網路故障或 GitHub 500/502 錯誤，程式執行 `break`，結束迴圈。
  - 隨後在第 339 行將第 1 頁搜集到的訊息與第 1 頁的 `new_etag` 當作成功結果回傳。
  - 接收端（`a2a inbox`）會將此 `new_etag` 寫入本機狀態。下一次輪詢時帶著此 ETag 請求，GitHub 便會回傳 304 Not Modified。第 2 頁之後未抓取的訊息將永遠不會被收件人讀取。
- **實測方法與結果**：
  - 模擬第 1 頁回傳 1 則訊息及 next link，第 2 頁回傳 HTTP 500。
  - **實測結果**：未拋出任何例外，回傳 `len(messages) == 1` 及第 1 頁的 `etag`。
- **修復建議**：
  - 若後續分頁請求失敗（`status >= 400`），應拋出 `BackendError(f"Failed to fetch comments page: HTTP {current_resp.status}")`，通知上層失敗重試，不可前進游標。

#### M3. `list_channels` 遇 HTTP 4xx/5xx 錯誤靜默終止，未向外報錯

- **問題說明**：
  - 在 `src/angareion/github.py` 第 219-220 行：
    ```python
    resp = self._request("GET", url, headers=self._headers())
    if resp.status >= 400:
        break
    ```
  - 對照 `find_channel` 遇到 `status >= 400` 會拋出 `BackendError`，`list_channels` 卻靜默 `break` 並回傳不完整的快取清單。
- **修復建議**：
  - 統一錯誤處理，遇 `status >= 400` 拋出 `BackendError`。

---

### Low (2 項)

#### L1. `Backend` Protocol 未包含 `list_channels` 定義

- **問題說明**：
  - 在 `src/angareion/backend.py` 的 `Backend(Protocol)` 中，僅定義了 `create_channel`、`find_channel`、`post_message`、`list_messages`。
  - `FakeBackend` 與 `GitHubBackend` 皆實作了 `list_channels`，導致呼叫端（`cli.py` 與 `state.py`）必須使用 `hasattr(backend, "list_channels")` 來判斷防禦。
- **修復建議**：
  - 將 `list_channels(self) -> list[tuple[str, int]]: ...` 加入 `Backend` Protocol 定義。

#### L2. `load_token` 嚴格要求 0600，拒絕 0400 (只讀) 權限

- **問題說明**：
  - 在 `src/angareion/config.py` 中：
    ```python
    if mode != 0o600:
        raise TokenPermissionError(...)
    ```
  - 規格書指定「檔案權限 0600」，實作精準符合規格；但實務上只讀權限 `0400` 亦具備擁有者隔離安全性。若使用者習慣設為只讀會收到 `TokenPermissionError`。
- **建議**：
  - 保持目前實作（符合 spec 要求），於錯誤提示清楚標明需為 0600。

---

## 突變測試 (Mutation Testing) 記錄

於獨立副本（`git archive HEAD | tar -x -C /tmp/angareion_review_replica`）中執行之驗證結果：

| 突變測試項目 | 修改內容 | 預期反應 | 實測結果 | 評價 |
|---|---|---|---|---|
| **1. Token 異常防洩遮蔽** | `github.py` 移除 `mask_token`，直接丟出原始例外字串 | 例外文字含 Token，測試應失敗 | **FAIL (紅燈)**：`AssertionError: assert 'ghp_VERY_SECRET...' not in ...` | 測試防護有效 |
| **2. 權限嚴格 0600 檢查** | `config.py` 放寬權限至允許 `0644` | 非 0600 檔案未被攔截，測試應失敗 | **FAIL (紅燈)**：`Failed: DID NOT RAISE TokenPermissionError` | 測試防護有效 |
| **3. 條件式輪詢 304 反應** | `github.py` 遇到 304 時將 `not_modified` 改回傳 `False` | 304 未正確標示未修改，測試應失敗 | **FAIL (紅燈)**：`AssertionError: assert False is True` | 測試防護有效 |
| **4. 403 Retry-After 標頭解析** | `github.py` 硬編碼 `retry_after = 60` 忽略標頭 | 解析出的等候秒數不符，測試應失敗 | **FAIL (紅燈)**：`AssertionError: assert 60 == 42` | 測試防護有效 |
| **5. 發文防重複 (Idempotency)** | `github.py` 移除/略過 `post_message` 重複檢查 | 重複發文發出多餘 POST，測試應失敗 | **FAIL (紅燈)**：`BackendError: Failed to post comment ... HTTP 404` | 測試防護有效 |
| **6. Link 分頁連結追蹤** | `github.py` 分頁迴圈強制中斷 `url = None` | 第二頁訊息未被讀取，測試應失敗 | **FAIL (紅燈)**：`AssertionError: assert 1 == 2` | 測試防護有效 |
| **7. 留言內文引用 ID 誤判** | 既有留言內文包含 `id: <target>` | 應正常發送真訊息 | **FAIL (實測未發送，被誤判為已存在)** | 抓出 M1 缺陷 |
| **8. 留言數 > 30 則防重複穿透** | issue 留言數為 35 則，重試第 35 則發送 | 應辨識出已存在而不重複發送 | **FAIL (實測發送了第 2 次 POST)** | 抓出 H1 缺陷 |
| **9. 分頁第 2 頁 HTTP 500 靜默吞掉** | 第 2 頁回傳 HTTP 500 內部伺服器錯誤 | 應拋出 BackendError 中斷 | **FAIL (實測未拋錯，回傳第 1 頁與 ETag)** | 抓出 M2 缺陷 |

---

## 給 PM 的清單

1. **請 PM 轉交 agy1 修正以下項目**：
   - **H1**：修正 `src/angareion/github.py` 之 `post_message` 防重複檢查，不可僅讀取前 30 則留言（因 GitHub 預設按時間正序排序）；需透過 `since` 時間過濾或定位最後一頁留言。
   - **M1**：修正 `post_message` 中比對重複 ID 之邏輯，改用 `parse_message` 解析 front matter 的 `parsed.id` 比對，避免內文提及 ID 造成假重複與吞信。
   - **M2**：修正 `list_messages` 分頁抓取邏輯，若後續分頁遇到 HTTP >= 400，必須拋出 `BackendError`，絕不可靜默 `break` 並更新 ETag。
   - **M3**：修正 `list_channels` 遇 HTTP >= 400 時的錯誤處理，統一拋出 `BackendError`。
   - **L1**：在 `src/angareion/backend.py` 的 `Backend` Protocol 中補上 `list_channels` 簽章。
2. **後續步驟**：
   - 本前半審查已歸檔，待前半缺陷收斂後進行回歸驗證。
