# 任務

## 1. 設計（本次；等 PM 核准才做第 2 步之後）

- [x] 1.1 讀 #96／#93、M0 報告、現有 `check` 的 ETag／`scan_since`、游標檔、CLI、`docs/angareion.md`、`SKILL.md` 與既有測試（github／cli／state／library／skill）
- [x] 1.2 決定：狀態檔（`wait-<agent 名>.json`、版本 1、鍵 `repo#issue|收件模式`、原子寫入、壞檔當空）、可重用條件（完整 URL＋游標＋收件模式全同）、304 安全邊界（找到即清、多頁／整頁滿不留）、壞檔與兩個行程的競態、`--channel` 警告、預設逾時維持一直等、程式接縫（`ETagRecord`／`ETagCache`／`FileETagCache`）
- [x] 1.3 PM 看過設計說可以實作 — 做完（PM 看過設計後放行實作，2026-10-18）

## 2. 測試先行（假後端／假 transport；不連網）

- [x] 2.1 新增 `tests/unit/test_angareion_etag.py` 的假 transport：每個 URL 一個回應本體＋ETag；請求帶 `If-None-Match` 且與目前本體相同 → 304（可換本體模擬「ETag 失效」），並記錄每次請求的標頭
- [x] 2.2 重啟：A 完整查一次（乾淨）→ B（新實例、同一個狀態檔）第一次就帶 `If-None-Match`（值等於 A 存的）→ 304 → `False`；模擬 10 次重啟只有第一次 200
- [x] 2.3 ETag 失效：本體變了 → 200；有寄給我的新訊息 → `True`、記錄被清；新實例不帶舊 ETag、完整查詢仍 `True`
- [x] 2.4 狀態檔不存在／壞 JSON／版本不認得／單筆欄位壞掉／游標不符 → 不帶 `If-None-Match`、完整查詢、訊息照樣找到；之後檔案被改寫成合法內容；同游標但不同查詢視窗的記錄仍重用（304 才算相同表示）
- [x] 2.5 多個 channel 各自一筆；查 A 帶 A 的、不帶 B 的；換 identity／換狀態目錄不重用
- [x] 2.6 多頁（`Link`）不留 ETag；第 1 頁剛好 `per_page` 筆、無 `Link` 也不留（99／100／101 三個邊界都測）
- [x] 2.7 `read` 之後：游標前進 → 記錄不重用 → 完整查詢；cache／`wait` 不改游標檔、`read` 不碰 `wait-…json`
- [x] 2.8 「找到但沒讀走」再 `wait`：仍叫得到（不是 304）
- [x] 2.9 狀態檔寫不進去（唯讀目錄）：`check` 照常、不掉訊息、不變 exit 2、stderr 恰好一行提示（同一行程不重複）
- [x] 2.10 狀態檔本身：roundtrip、`drop` 不建檔、寫入永遠留下合法 JSON、未知版本當空、內容不變不重寫
- [x] 2.11 CLI 警告：`read --channel`／`wait --channel` 在 stderr 有那一行、stdout 與 exit code 不變；不加 `--channel` 沒有警告；既有兩個「stderr 應該只有警告」的斷言同步收斂
- [x] 2.12 先跑一次確認新測試紅：實作前收集階段 `ImportError: cannot import name 'ETagRecord'`（紅）；實作後兩個檔 98 passed（審查修正後 104 passed）

## 3. 實作

- [x] 3.1 `src/angareion/backend.py`：`ETagRecord`、`ETagCache` Protocol、`MemoryETagCache`；`ChannelBackend.check` 簽名不動
- [x] 3.2 `src/angareion/state.py`：`wait_state_file_path`、`FileETagCache`（版本 1、原子替換、壞檔當空且下次 put／drop 一定重寫成合法內容、寫入失敗吞掉並在 stderr 提示一次、內容不變不重寫）；`CursorStore` 不動
- [x] 3.3 `src/angareion/github.py`：`GitHubIssuesBackend(..., etag_cache=…)`；`check` 以 `_etag_key(entity, recipient)` 查／存／清，重用判準是游標＋收件模式（記錄的 URL 是掃描視窗；REVIEW-96 M1 後不再要求 URL 相同，並優先查 cursor 視窗）；記錄條件（200、無下一頁、第 1 頁不滿、掃完 `found=False`、有 ETag）；找到新訊息清掉該筆；304 不碰記錄；`scan_since` 維持只在記憶體（鍵改成同一組）
- [x] 3.4 `src/angareion/cli.py`：建 GitHub 後端時傳 `FileETagCache(config.state_dir, config.identity)`；`cmd_read`／`cmd_wait` 的 `--channel` 警告常數與位置（驗證後、碰後端前）；`--channel` 的說明文字補一句
- [x] 3.5 `uv run pytest -q tests/unit` 全綠：**1405 passed in 514.52s**（審查修正後；原樣輸出在回報）

## 4. 改壞驗證（`git archive` 副本＋本次改動的檔案；假資料、不連網）

- [x] 4.1 拿掉讀狀態檔的 ETag（永遠不送 `If-None-Match`）→ **8 紅**（重啟 ×2、ETag 失效、另一視窗重用、多 channel、99 邊界、M1 漂移 ×2）
- [x] 4.2 ETag 與游標脫鉤（只按實體鍵）→ **3 紅**（壞檔參數化的游標不符、read 後游標前進、刪掉游標後）
- [x] 4.3 壞檔直接當 304（`get` 對壞檔拋錯、`check` 吞掉回 `False`）→ **4 紅**，檔名：`tests/unit/test_angareion_etag.py::test_a_bad_or_mismatched_state_file_falls_back_to_a_full_query` 的 `[not json]`、`[{"version": 2, "targets": {}}]`、`[{"version": 1, "targets": []}]`、`[壞 entry]` 四個參數
- [x] 4.4 警告不印（拿掉兩個 `if args.channel` 區塊）→ **4 紅**，檔名：`tests/unit/test_angareion_cli.py::test_wait_with_channel_prints_a_warning_and_keeps_stdout_and_exit`、`::test_read_with_channel_prints_a_warning_and_keeps_stdout_and_exit`、`::test_read_prints_messages_and_advances_the_cursor`、`::test_old_format_cursor_is_reported_and_read_from_the_start`
- [x] 4.5 找到新訊息後仍記住那次回應的 ETag → **7 紅**（沒讀走的新訊息、ETag 失效，＋壞檔 6 案的「記錄被清掉」斷言）
- [x] 4.6 第 1 頁滿 `per_page` 仍記 ETag → **1 紅**（`…[100]`）
- [x] 4.7 記錄寫進游標檔（`wait_state_file_path` 指到游標檔）→ **2 紅**（read 後互不破壞、狀態檔分離）
- [x] 4.8 控制組 104 passed；上述紅燈數與測試名已留在回報

## 5. 文件

- [x] 5.1 `docs/angareion.md`：目標那條補 `--channel` 警告；`wait` 的等法補「重新開始沿用上次 ETag、閒置重啟幾乎不吃額度、不給逾時仍一直等」；游標節把「ETag 只在記憶體」改成 `wait-<agent 名>.json`（位置、版本、可重用條件、壞檔退完整查詢、刪掉無害、不含訊息）；額度段改成「單頁且不滿頁才用 ETag；跨重啟有效」
- [x] 5.2 `skills/angareion/SKILL.md`：wait 段補「重新開始 wait 很便宜；`--timeout` 設在工具上限內」；收訊規則補「收訊不加 `--channel`（會讀到寄給別人的訊息，stderr 會警告）；只有送訊才指定 channel」
- [x] 5.3 主 spec：delta 已 validate；實際套進 `openspec/specs/angareion/spec.md` 等 PM 決定歸檔順序（PM 2026-10-18 指示）——本 change 不動 `openspec/specs` — 做完（隨本次歸檔套進 `openspec/specs/angareion/spec.md`）
- [x] 5.4 delta 自身 `openspec validate angareion-etag --strict` 通過

## 6. 驗收（PM 對真 GitHub 做；見 design §9）

- [x] 6.1 `PYTHONPATH=<experiments/m0/count>`、`M0_COUNT_LOG=<jsonl>`，連續 `wait --timeout 60` 10 次；數 `status == 200` 與 `304` — 做完（PR #99：連續 10 次 `wait --timeout 60`，94／100 個請求，2 個 200）
- [x] 6.2 預期：第一次每個 channel 1 個 200，之後重啟的第一次查詢就是 304；200 總數 ≈ N_ch — 做完（第一次每個 channel 1 個 200，之後都是 304；200 總數＝註冊 channel 數 2）
- [ ] 6.3 送一則訊息→`wait` exit 0→`read`→再 `wait`：該 channel 一次 200 後回 304 — 未在新程式上量過（只有單元測試涵蓋；M0 的舊程式觀察到同樣的行為）
- [ ] 6.4 多頁或滿 100 筆的 channel 會每次 200（設計如此）；在驗收紀錄註明 — 未量（設計如此，已寫進 docs；沒有滿 100 筆的真 channel 可量）

## 7. 收尾

- [x] 7.1 回報 PM：改了哪些檔、`pytest -q tests/unit` 原樣輸出、改壞驗證結果、沒驗到的、用的模型；commit 由 PM 做

## 8. 審查修正（REVIEW-96；高 0、中 M1、低 L1～L4＋狀態目錄 mode）

- [x] 8.1 M1：`check` 的重用判準改成游標＋收件模式（不再要求 URL 相同）；有一筆 cursor 視窗的乾淨記錄時優先查那個視窗、不採用更窄的 `scan_since`，掃描視窗漂移不再讓每次重啟多一次 full query；測試：亂序 `id=120 created=13:01/14:29`、cursor `id=150 created=13:00`、6 次重啟 200 總數 1（修前 12）
- [x] 8.2 L1：`client.py` 的 `check()` docstring 改成真話（ETag 由 CLI 的檔案 cache 跨行程保存）；design §7 改成「`client.py` 只改 docstring」
- [x] 8.3 L2：`--channel` 警告移到 `ensure_fresh`＋`entity_for`（channel 驗證）之後；channel 不存在只印錯誤、不印警告；補 CLI 測試
- [x] 8.4 L3：補「兩個 writer 共用狀態檔」與「刪掉游標檔後 wait 仍完整查詢」兩支最小測試
- [x] 8.5 L4：tasks 4.3／4.4 的紅燈註記改成實測測試檔名（見上）
- [x] 8.6 狀態目錄 `mkdir(..., mode=0o700)`；測試新建目錄是 0700
- [x] 8.7 改壞驗證補 M1：拿掉 cursor 視窗優先並要求 URL 完全相同 → **3 紅**（另一視窗重用、漂移 13:01、漂移 14:29）
- [x] 8.8 文件同步：`docs/angareion.md` 重用條件、design §2／§3／§5／§7／§8、spec delta 的重用條件＋漂移 Scenario
- [x] 8.9 跑 `uv run pytest -q tests/unit/test_angareion_etag.py tests/unit/test_angareion_cli.py`（**104 passed**）與整套 `tests/unit`（背景、單獨跑，**1405 passed in 514.52s**）、`openspec validate angareion-etag --strict`（valid）
