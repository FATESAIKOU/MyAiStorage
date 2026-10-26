## 0. 前提（不是 PR）

- 本 change 是設計（proposal／design／specs delta／tasks）；經非作者審查（pstack）、PMO 以介面為主向本人說明、**本人同意才實作**。
- 不動：Angareion 協定（`angareion: 1`）、TS 端 MyAiEntry、sidecar、MyPMO 的程式（design D6 的判斷：都需要不動，層級維持 c）。
- 公開 repo：內容不寫帳號、機器位址、憑證、真的 session id。
- 見證測試隔離：測試照 `tests/conftest.py` 的 `isolated_home`；不讀真 credential、不打真信。

## 1. 定案（要 PMO／本人）

- [ ] 1.1 判斷點（design「判斷點」）逐項經本人裁定：J1 輸出固定由舊到新、J2 `--limit`＝保留最新 N 則且預設不限、J3 錨點找不到＝exit 1、J4 `--grep`＝字面／不分大小寫／只比內文、J5 aicomm log 預設 `--to me` 的注入時機、J6 接受快取用磁碟與 0700／0600、J7 說明層的擬改原文（D4／D5）。
- [ ] 1.2 作者提議 P1～P8 在審查時逐條收斂（接受／否決記錄在審查意見與 design 對應節）。

## 2. MyAiStorage：`angareion log`（PR）

**範圍**：`src/angareion/`（cli／client／backend／github＋新的快取模組）、`skills/angareion/SKILL.md`、`docs/angareion.md`、`tests/unit/`。

- [ ] 2.1 `log` 指令骨架：旗標解析（可重複 `--channel`、`--json`）與組合規則（design D1.2）；輸入錯的 exit 1 清單（壞時間、非 ULID、`--around`＋`--thread`、孤立 `--before`／`--after`、負 `--before`／`--after`／`--limit`、`--since > --until`、未登記 channel）；`log` 不印 `--channel` 警告
- [ ] 2.2 後端的「列舉」動作：GitHub 後端照 `read` 的列表迴圈新增唯讀列舉（回傳 `(comment_id, created_at, parsed|None)`，`Link` 追頁；行為對照 github.py:382-441）；`FakeBackend` 對應實作（測試不觸網）
- [ ] 2.3 快取模組：`log-<slug>/<channel>.json`（version、entity、`last_comment_id`／`last_created_at`、`etag` 記錄、messages 陣列）；目錄 0700、`mkstemp`(0600)＋原子替換；壞檔（不是 JSON／版本不認得／缺欄位／entity 失配）整檔視為空重建；寫失敗警告一次；channel 名做檔名（`CHANNEL_NAME_RE` 限安全字元）
- [ ] 2.4 增量同步：`since ＝ last_created_at − 60s`（空快取全量）、冪等 upsert（位置更新**或從未見過**都納入；同位置內容變＝編輯覆蓋；最大位置不後退）、ETag 條件式（沿用 check 的可記錄單頁規則；回應含新訊息不記錄）、**任一涉入 channel 同步失敗 → exit 2 且不出結果**
- [ ] 2.5 查詢引擎：錨點（`--around` 視窗以到達位置計、`--thread` 沿 `in_reply_to` 閉包；錨點必須在過濾的 channel 內、找不到 exit 1）→ 過濾交集（`--from`／`--to`(含 `me`)／`--mentioned`／`--since`／`--until`／`--grep`）→ `--limit`（保留最新 N）→ 排序（跨 channel `(received_at, channel, id)`，同 client.py:158）；`me` 由身分解析
- [ ] 2.6 **不動游標的守衛**（design D2.2）：假 `CursorStore` 在 `save`／`save_many` raise 之下跑完全部旗標用例；游標檔 sha256 前後相等（空游標與有游標兩種身分）；改壞驗證（故意在 log 路徑接上游標寫入，測試轉紅）
- [ ] 2.7 測試：specs 的每個 scenario 至少一條（含：預設查全部、`--channel` 不警告、交集、時間含邊界、`--grep` 語意、`--around` 視窗與輸入錯、`--thread` 整串與錨點視為根、`--limit`、`--json`／多行完整、非訊息留言不列入、增量同步、亂序晚到、編輯同位置、304、同步失敗 exit 2、壞檔∕失配重建、權限、寫失敗警告、並行、快取與游標互不影響）；抽樣改壞驗證（照 #212 慣例）
- [ ] 2.8 `skills/angareion/SKILL.md` 與 `docs/angareion.md`：五個指令改六個、表格加 `log`、「什麼時候用」補「回頭找信／前後文／串用 `log`（不動游標）」、結束碼段補 `log`（0／1／2、無 3）；既有關鍵句（`test_angareion_docs.py` 所守）全數保持

## 3. MyAiStorage：`aicomm log` 與說明（PR，可與 2 同 PR）

**範圍**：`src/aicomm/`、`skills/aicomm/SKILL.md`、`docs/aicomm.md`、`tests/unit/test_aicomm_*.py`。

- [ ] 3.1 `aicomm log`：引數原樣轉發（subprocess `angareion log`）、`--to`／`--mentioned` 注入規則（都沒給才補 `--to me`；字面 `me` 交給 angareion 解）、**對 sidecar 零請求**、結束碼與 stdout／stderr 原樣；假 `angareion` 測試（沿用 `tests/unit/_aicomm_fakes.py` 的手法）
- [ ] 3.2 說明更新照 design D5.2 擬改原文：SKILL.md「三個指令」＋指令區塊＋「同一個身分只留一個讀者」新增 log 分工句＋結束碼一行＋導言與 frontmatter description；`docs/aicomm.md` 同步
- [ ] 3.3 `EMPTY_INBOX_HINT`（`src/aicomm/cli.py:244-247`）句尾改「要補查用 `aicomm log`（不動游標）」；同步更新 `test_aicomm_inbox.py` 的 `HINT_KEYS` 與 `test_aicomm_docs.py`（REMEDY 句 `angareion read --since <時刻>` 本體保留在補救 bullet，測試句不動也能守）
- [ ] 3.4 文件測試補守新句（SKILL／docs 含「三個指令」、log 分工句、「不動游標」），並保持 `test_no_secrets_or_private_info_patterns` 綠

## 4. 驗收

- [ ] 4.1 拋棄式環境端到端（**不打真信**；用拋棄式身分與假 sidecar／fake rclone 的同套隔離）：數則信含多行內文與 `in_reply_to` 串 → `log` 取到完整內文、`--around` 還原漏掉的中間段、`--thread` 取整串；`angareion log --to me` 與 `aicomm log` 輸出一致；全程游標檔位元組不變
- [ ] 4.2 每條測試證明「改壞會紅」（抽樣即可，照倉庫慣例）；不碰真 credential、不讀真 session
- [ ] 4.3 效能以 design 的推論（I1～I4）為據；若本人要實測（U3），另開單