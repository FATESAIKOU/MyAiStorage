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
- [x] 2.6 測試（`tests/unit/test_cli.py`／`test_cli_sync.py`／`test_cli_more.py`）：混批次、全部被鎖、鎖放掉後重跑、`--force`（警告在刪除之前、pending 記錄還在、之後另存）、`--force` 沒 `--yes`、unknown 整批拒絕（含與被鎖同批）、被跳過的子擋住父、search 兩個標記與更新日、continue 固定行與 exit 4 不變、說明含誰在接續
- [x] 2.7 改寫既有的 `test_delete_refuses_a_session_that_is_being_continued`（舊行為被取代）與 `test_search_output_format`（日期欄）；`uv run pytest -q tests/unit` → **1373 passed**

## 3. 改壞驗證（`git archive` 副本＋本次未提交的檔案、假資料、隔離環境）

- [x] 3.1 拿掉跳過邏輯（回到第一個被鎖就整批拒絕）→ 紅 4 個：`test_delete_skips_a_session_that_is_being_continued_and_deletes_the_rest`、`test_delete_when_every_session_is_being_continued_deletes_nothing`、`test_delete_of_a_session_being_continued_goes_through_after_the_lock_is_released`、`test_delete_refuses_a_parent_whose_locked_child_in_the_batch_was_skipped`
- [x] 3.2 拿掉 `--force` 的警告（直接刪）→ 紅 1 個：`test_delete_force_deletes_a_session_being_continued_after_saying_the_consequence`
- [x] 3.3 把「使用中」判斷弄反（`store.continuing` 有人拿鎖回 False、留下來的記錄回 True）→ 紅 8 個（test_cli.py：search 標記、混批次、全部被鎖、鎖放掉後重跑、`--force` ×2、被跳過的子擋父、G1 收不了尾的記錄照刪）＋ test_cli_sync.py 3 個（`continue` 被鎖的拒絕、誰在接續、競合窗口）

## 4. 文件

- [x] 4.1 README：delete 段（跳過、誰在接續、`--force`、exit）、search 段（更新日、`(使用中)`、只判斷本機）、exit code 表的 1 那一列
- [x] 4.2 `docs/design.md` 5.1（search 行）、5.6（delete：跳過與 `--force`）、5.4（被鎖的說明含誰在接續）、exit code 清單、用法行
- [x] 4.3 主 spec 同步：delta 手動套進 `openspec/specs/session-continue`／`batch-commands`／`session-sync`（`openspec validate --specs --strict` 8 個全過；change 本身的 delta 也仍 valid）。**歸檔（`openspec archive agora-lock-ux`）留給 PM 指定的時機**——套用後才歸檔會撞重複的 Requirement

## 5. 收尾

- [x] 5.1 commit（只加自己改的路徑，訊息結尾 `Refs #90`），回報 PM：改了哪些檔、`pytest -q tests/unit` 的原樣輸出、改壞驗證結果、沒驗到的、用的模型
