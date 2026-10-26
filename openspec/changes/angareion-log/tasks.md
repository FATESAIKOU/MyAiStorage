## 0. 前提（不是 PR）

- 本 change 是設計（proposal／design／specs delta／tasks）；經非作者審查（pstack）、PMO 以介面為主向本人說明、**本人同意才實作**。
- 不動：Angareion 協定（`angareion: 1`）、TS 端 MyAiEntry、sidecar、MyPMO 的程式（design D6 的判斷：都需要不動，層級維持 c）。
- 公開 repo：內容不寫帳號、機器位址、憑證、真的 session id。
- 見證測試隔離：測試照 `tests/conftest.py` 的 `isolated_home`；不讀真 credential、不打真信。

## 1. 定案（要 PMO／本人）

- [ ] 1.1 判斷點（design「判斷點」）逐項經本人裁定：J1 輸出固定由舊到新、J2 `--limit`＝排序後取最後 N 則（「最新」＝排序鍵的全域尾端，非各 channel 各取）、預設不限（代價見 D5.1）、J3 錨點＝選取集合內找不到 exit 1、空結果 exit 0、**錨點被其他過濾條件濾掉不是錯**、J4 `--grep`＝字面／不分大小寫／只比內文、J5 aicomm log 預設 `--to me` 的注入時機與判斷寫法（`--to`／`--to=<值>`／`--mentioned` 與縮寫；注入放使用者引數前）、J6 接受快取用磁碟與 0700／0600（檔名用可逆轉義的 `channel_slug`）、J7 說明層的擬改原文（D4／D5）、J8 已知限制（`--thread` 不跨 channel、快取不解刪除）。
- [ ] 1.2 作者提議 P1～P8 在審查時逐條收斂（接受／否決記錄在審查意見與 design 對應節；審查後：P5 改 `channel_slug` 可逆轉義、P6 以「已知限制」定案寫進 spec/design、P7 的轉發機制定為頂層 `parse_known_args`＋log subparser `add_help=False`——`REMAINDER` 實測不可行；理由與定案的細節見 design D5.1，J8 由 P6 分出）。

## 2. MyAiStorage：`angareion log`（PR）

**範圍**：`src/angareion/`（cli／client／backend／github＋新的快取模組）、`skills/angareion/SKILL.md`、`docs/angareion.md`、`tests/unit/`。

- [ ] 2.1 `log` 指令骨架：旗標解析（可重複 `--channel`、`--json`）與組合規則（design D1.2）；輸入錯的 exit 1 清單（壞時間、非 ULID、`--around`＋`--thread`、孤立 `--before`／`--after`、負 `--before`／`--after`／`--limit`、`--since > --until`、未登記 channel）；`log` 不印 `--channel` 警告
- [ ] 2.2 後端的「列舉」動作：GitHub 後端照 `read` 的列表迴圈新增唯讀列舉（回傳 `(comment_id, created_at, parsed|None)`，`Link` 追頁；行為對照 github.py:382-441）；`FakeBackend` 對應實作（測試不觸網）
- [ ] 2.3 快取模組：`log-<slug>/<channel_slug>.json`（version、entity、`last_comment_id`／`last_created_at`、`etag` 記錄、messages 陣列）；目錄 0700、`mkstemp`(0600)＋原子替換；壞檔（不是 JSON／版本不認得／缺欄位／entity 失配）整檔視為空重建；寫失敗警告一次；channel 檔名用**可逆轉義**的 `channel_slug`（屬 `[A-Za-z0-9_-]` 的字元原樣、其餘 `%XX` 編 UTF-8 位元組——不依賴 `CHANNEL_NAME_RE` 在載入點成立，design D3.2）＋檔內記原始 channel 名載入時比對
- [ ] 2.4 增量同步：`since ＝ last_created_at − 60s`（空快取全量；`since` 依列舉的語意按**最後編輯時刻**過濾，見 2.2 與 spec「後端可以替換」——「同 id 內容變＝編輯覆蓋」仰賴它）、冪等 upsert（位置更新**或從未見過**都納入；同位置內容變＝編輯覆蓋；最大位置不後退；`comment_id` 一律**整數**比較）、ETag 條件式（沿用 check 的可記錄單頁規則；回應含新訊息不記錄）、**任一涉入 channel 同步失敗 → exit 2 且不出結果**
- [ ] 2.5 查詢引擎：錨點候選集（`--around` 視窗以到達位置計、`--thread` 沿 `in_reply_to` 閉包；錨點**只在選取集合內**查、同步後找不到 exit 1；錨點不符合其他過濾條件（`--from`／`--to`／`--mentioned`／時間／`--grep`）**不是錯**——照常定位、錨點本身被濾掉則不出現在輸出 exit 0，design D1.2-4 與 spec 同句）→ 過濾交集（`--from`／`--to`(含 `me`)／`--mentioned`／`--since`／`--until`／`--grep`）→ **排序**（單 channel 到達位置；跨 channel `(received_at, channel, id)`，同 client.py:158）→ `--limit` 取**排序尾端 N 則**（全域最新；非各 channel 各取；錨點沒有保留地位）→ 同序印；`me` 由身分解析
- [ ] 2.6 **不動游標的守衛**（design D2.2）：假 `CursorStore` 在 `save`／`save_many` raise 之下跑完全部旗標用例；游標檔 sha256 前後相等（空游標與有游標兩種身分）；改壞驗證（故意在 log 路徑接上游標寫入，測試轉紅）
- [ ] 2.7 測試：specs 的每個 scenario 至少一條（含：預設查全部、`--channel` 不警告、交集、時間含邊界、`--grep` 語意、`--around` 視窗與輸入錯、**錨點被過濾濾掉＝exit 0**、`--thread` 整串與錨點視為根、**根在別的 channel 視為根（已知限制）**、`--limit`、**`--limit` 跨 channel 取全域最新、`--around`＋`--limit` 截錨不報錯**、`--json`／多行完整、非訊息留言不列入、增量同步、亂序晚到、編輯同位置、304、同步失敗 exit 2、壞檔∕失配重建、權限、寫失敗警告、並行、快取與游標互不影響、**`a/b` 這類名字的快取檔不出 `log-<slug>/`、後端刪掉的留言增量下還在＋全量重建後消失**）；抽樣改壞驗證（照 #212 慣例）
- [ ] 2.8 `skills/angareion/SKILL.md` 與 `docs/angareion.md`：五個指令改六個、表格加 `log`、「什麼時候用」補「回頭找信／前後文／串用 `log`（不動游標；log 與快取不解後端的刪除，後端已刪的留言可能仍在結果裡）」、結束碼段補 `log`（0／1／2、無 3）；既有關鍵句（`test_angareion_docs.py` 所守）全數保持。**「五個」殘留逐檔點名改「六個」**（低-2）：`openspec/specs/angareion/channel-config/spec.md:29`（「見 `angareion` 的五個介面」→「六個介面」——正規 spec，改名後規格之間才對得上）、`docs/angareion.md:3`（導言）、`docs/angareion.md:11`（「## 五個指令」）、`docs/angareion.md:225`（「給 MyLinuxPool 的需求」節的交接句）、`skills/angareion/SKILL.md:8`、`skills/angareion/SKILL.md:21`、`atelier/worker-team/pm/v0.md:37`（PM 職務檔的說明；harness 檔，一併改）；README 沒有介面數字樣（不用動）。

## 3. MyAiStorage：`aicomm log` 與說明（PR，可與 2 同 PR）

**範圍**：`src/aicomm/`、`skills/aicomm/SKILL.md`、`docs/aicomm.md`、`tests/unit/test_aicomm_*.py`。

- [ ] 3.1 `aicomm log`：薄包 `angareion log`（subprocess）。**轉發機制（中-5）**：`main` 頂層 `parse_known_args` 判子指令；`log` 註冊為**不宣告旗標**的 subparser（`add_help=False`，`--help` 也轉發給 angareion）、extras 照出現順序原樣轉發；非 log 子指令照現行嚴格 `parse_args`；測試釘行為：`aicomm log --bogus` → stderr 是 angareion 的訊息、exit 1（不是 aicomm 的 `unrecognized arguments`）。**注入規則（中-2）**：使用者沒給 `--to`（含 `--to=<值>` 與未歧義縮寫；判準＝去 `--` 後是 `to` 或 `mentioned` 的前綴）也沒給 `--mentioned` 才注入 `--to me`、注入放使用者引數**之前**；字面 `me` 交給 angareion 解、**對 sidecar 零請求**、結束碼與 stdout／stderr 原樣；假 `angareion` 測試（沿用 `tests/unit/_aicomm_fakes.py` 的手法）
- [ ] 3.2 說明更新照 design D5.2 擬改原文：SKILL.md「三個指令」＋指令區塊＋「同一個身分只留一個讀者」新增 log 分工句＋結束碼一行＋導言與 frontmatter description；`docs/aicomm.md` 同步；**用法錯訊息跟著改字（低-1）**：`src/aicomm/cli.py:83`（「（aicomm 只有 send 與 inbox）」）與 `:284`（`UsageError("aicomm 只有 send 與 inbox 兩個指令")`）改成「只有 send、inbox 與 log」，連動 `tests/unit/test_aicomm_send.py:49`（`SUFFIX` 字面）與 `:66-70` 斷言
- [ ] 3.3 `EMPTY_INBOX_HINT`（`src/aicomm/cli.py:244-247`）照審查實測會綠的寫法：**原句一字不動、句尾追加**「；要補查用 `aicomm log`（不動游標）。」（「改為」的寫法會讓 6 個測試紅，design D5.2）；`test_aicomm_docs.py:128-130` **不動**（REMEDY 原句完好）、`test_aicomm_inbox.py:99` 的 `HINT_KEYS` **不動**（keys 都在）；測試的新增是「追加句在」的守衛（3.4）
- [ ] 3.4 文件測試補守新句（SKILL／docs 含「三個指令」、log 分工句、「不動游標」；`EMPTY_INBOX_HINT` 的「aicomm log」追加句；**注入判斷的兩個字面 `to`／`mentioned` 與 aicomm 語意的同步守衛**照 F14 手法讀 `src/aicomm/cli.py` 斷字面），並保持 `test_no_secrets_or_private_info_patterns` 綠

## 4. 驗收

- [ ] 4.1 拋棄式環境端到端（**不打真信**；用拋棄式身分與假 sidecar／fake rclone 的同套隔離）：數則信含多行內文與 `in_reply_to` 串 → `log` 取到完整內文、`--around` 還原漏掉的中間段、`--thread` 取整串；`angareion log --to me` 與 `aicomm log` 輸出一致；全程游標檔位元組不變
- [ ] 4.2 每條測試證明「改壞會紅」（抽樣即可，照倉庫慣例）；不碰真 credential、不讀真 session
- [ ] 4.3 效能以 design 的推論（I1～I4）為據；若本人要實測（U3），另開單