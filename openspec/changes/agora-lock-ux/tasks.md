# 任務

## 1. 設計

- [x] 1.1 讀 #90、`cmd_delete`／`cmd_search`／`store.continuing`／`_write_pending`、主 spec（`session-continue`／`batch-commands`／`session-sync`）與 delete／search／locked 的既有測試
- [x] 1.2 決定：跳過的結果與 exit code（跳過＝1；全部被鎖＝1；不新增碼、不用 4／5）、`--force` 的警告與確認（`--yes` 仍要、警告在刪除之前）、search 的格式與「只判斷本機」的限制
- [x] 1.3 PM 看過設計說可以實作（2026-10-18），補三點：批內被跳過的子 Session 要擋住父 Session、unknown 仍整批拒絕、Open Questions 三點都不動

## 2. 實作

- [x] 2.1 `src/agora/store.py`：`continuing_details(paths, ulid) -> dict | None`（有人在接續時回那份 pending 記錄；讀不到回 `{}`；沒人拿鎖回 `None`）；`continuing()` 的 bool 契約不變
- [x] 2.2 `src/agora/cli.py`：`_who_note`／`_short_dir`（agent／dir／started_at，家目錄縮成 `~`，缺欄位略過，其他欄位不印）；`_locked(paths, …)` 帶上它（`agora-locked <id>` 與 exit 4 不變）
- [x] 2.3 `cmd_delete`：被鎖的跳過（逐個一行含原因）、其他照刪；全部被鎖→什麼都不刪＋明說＋exit 1；`--force --yes` 連被鎖的一起刪，每個刪之前警告後果；`--force` 沒 `--yes` 什麼都不刪；被跳過的子 Session 照樣擋住父 Session 並在父的訊息寫明
- [x] 2.4 `build_parser()`：`--force` 的說明文字（要加 `--yes`、後果）；模組 docstring 的用法行
- [x] 2.5 `cmd_search`：`_updated_cell`（對話日期＋不同時補「（更新：YYYY-MM-DD）」）與 `(使用中)`（`store.continuing`）
- [x] 2.6 測試（`tests/unit/test_cli.py`／`test_cli_sync.py`／`test_cli_more.py`／`test_tui.py`）：混批次、全部被鎖（含單數訊息）、鎖放掉後重跑、`--force`（警告在刪除之前、pending 記錄還在、之後另存）、`--force` 沒 `--yes`、unknown 整批拒絕（含與被鎖同批）、被跳過的子擋住父、壞記錄但鎖有人拿照樣跳過、收不了尾的路徑縮成 `~`、search 兩個標記與更新日、continue 固定行與 exit 4 不變、說明含誰在接續、TUI 的接續拒絕用同一套說法
- [x] 2.7 改寫既有的 `test_delete_refuses_a_session_that_is_being_continued`（舊行為被取代）、`test_search_output_format`（日期欄）與 TUI 的真實鎖測試；`uv run pytest -q tests/unit` → **1375 passed**

## 3. 改壞驗證（`git archive` 副本＋本次的檔案、假資料、隔離環境；跑 `tests/unit/test_cli.py tests/unit/test_cli_more.py tests/unit/test_cli_sync.py` 三個檔，211 tests；控制組 211 passed）

- [x] 3.1 拿掉跳過邏輯（遇到第一個被鎖的就 `raise InputError`＝#90 前的行為）→ **9 紅**（test_cli.py：混批次、全部被鎖、鎖放掉後重跑、壞記錄照樣跳過、`--force` ×2、之後另存、被跳過的子擋父、unknown 與被鎖同批；含本次新增的壞記錄測試）
- [x] 3.2 拿掉 `--force` 的警告（直接刪）→ **1 紅**：`test_delete_force_deletes_a_session_being_continued_after_saying_the_consequence`
- [x] 3.3 把「使用中」判斷弄反（`store.continuing` 有人拿鎖回 False、留下來的記錄回 True）→ **test_cli.py 11 紅**（search 標記、混批次、全部被鎖、鎖放掉後重跑、壞記錄照樣跳過、壞記錄路徑 `~`、`--force` ×2、被跳過的子擋父、G1 收不了尾的記錄照刪、中斷的接續補存）＋ **test_cli_sync.py 8 紅**（誰在接續、第二個 `continue` 被拒、另一行程持有的鎖、競合窗口、headless 被拒 ×2、pending 跟著另存改名、硬殺後補存）
- [x] 3.4 壞掉的 pending 記錄回 `None`（＝審查 M1 指的舊 design 寫法）→ **1 紅**：`test_delete_still_skips_when_the_pending_record_cannot_be_read`（沒有它時 0 red：照 design 寫會把有鎖的 Session 刪掉）

## 4. 文件

- [x] 4.1 README：delete 段（跳過、誰在接續、`--force`、exit）、search 段（更新日、`(使用中)`、只判斷本機）、exit code 表的 1 那一列
- [x] 4.2 `docs/design.md` 5.1（search 行）、5.6（delete：跳過與 `--force`）、5.4（被鎖的說明含誰在接續）、exit code 清單、用法行
- [x] 4.3 主 spec 同步：delta 手動套進 `openspec/specs/session-continue`／`batch-commands`／`session-sync`（`openspec validate --specs --strict` 8 個全過；change 本身的 delta 也仍 valid）。歸檔本身已經實測會成功——`openspec archive agora-lock-ux --yes` 說「Specs already in sync; no files changed」並把 change 移進 `archive/2026-10-18-agora-lock-ux`，不會撞重複的 Requirement（審查 L7 更正：原先寫的「會撞重複」不成立）。**時機留給 PM 指定**（`agora-opencode-v2`、`angareion-v5` 也還沒歸檔）

## 5. 收尾

- [x] 5.1 commit（只加自己改的路徑，訊息結尾 `Refs #90`），回報 PM：改了哪些檔、`pytest -q tests/unit` 的原樣輸出、改壞驗證結果、沒驗到的、用的模型

## 6. 審查修正（REVIEW-90，非作者審查）

- [x] 6.1 M1：`design.md` 的 `continuing_details` 壞檔契約改成 `{}`（與實作、2.1 一致）；補測試 `test_delete_still_skips_when_the_pending_record_cannot_be_read`（壞記錄＋鎖有人拿 → 照樣跳過、訊息只說「正在接續中」、不編欄位）
- [x] 6.2 M2：TUI 清單上 Enter 的接續拒絕改用 `cli._who_note`（同一個 helper，四處共用）；真實鎖的既有 TUI 測試改成寫一筆真記錄並釘住 agent／目錄／開始時間（不外洩 `agent_session_id`）
- [x] 6.3 L1：spec（主 spec ＋ delta）、`docs/design.md`、README 補「背景上傳器起不來時仍是 3」（`EXIT_IN_OUTBOX` 的既有優先序；不把新寫的 MUST 說成絕對的 1）
- [x] 6.4 L2：本節 3.1／3.3 的數字改成自己重跑的實測（9 紅；11＋8 紅）
- [x] 6.5 L3：全部被鎖只有一個時的訊息改成「`agora:X` 正在接續中，沒有刪除任何東西」（兩個以上仍是「N 個都在接續中」）；測試釘住，design 那句一起改
- [x] 6.6 L4：「中斷的接續沒補存成功（…）」的路徑也走 `_short_dir`（家目錄縮成 `~`）；測試把 state 放在 `$HOME` 下釘住
- [x] 6.7 L5：預覽列改成「`agora:X`（使用中，標題）」——不再出現「（使用中）（標題）」
- [x] 6.8 L6：design 寫明 id 去重是這次新加的（`dict.fromkeys`；#90 沒要求）
- [x] 6.9 L7：4.3 的理由改成事實（歸檔實測會成功、`Specs already in sync`；時機仍留給 PM）
