## 1. 實作

- [x] 1.1 `src/agora/cli.py`：`EXIT_LOCKED = 4`（3 已是 `EXIT_IN_OUTBOX`）與 `LockedError`／`_locked()`；`main` 的出口先印 `agora-locked <agora id>` 再印原本的中文說明，回 4
- [x] 1.2 提早檢查（`store.continuing`）改丟 `LockedError`（訊息不變）
- [x] 1.3 `_write_pending` 拿鎖時再問一次：目的地 `pending/<ULID>.json` 已鎖，或 `<ULID>.json.tmp` 已被另一個 `_write_pending` 鎖著（`O_CREAT|O_RDWR` 開啟，不 truncate），就丟 `LockedError`；輸的那個不覆蓋記錄、不啟動 agent

## 2. 測試（`tests/unit/test_cli_sync.py`）

- [x] 2.1 提早檢查（fake 鎖）：exit 4、`agora-locked <id>` 在說明之前、agent 沒開
- [x] 2.2 另一個行程真的持有 `pending/<ULID>.json` 的鎖（不 mock，照 test_tui.py 的做法）：exit 4、固定那一行
- [x] 2.3 競合窗口：開始的檢查之後才拿到鎖（掛在 agent 準備的接縫上）→ exit 4、贏的記錄沒被覆蓋、沒有留下 `.json.tmp`
- [x] 2.4 兩邊都在 `_write_pending` 裡：`*.json.tmp` 的鎖被另一個行程拿著 → exit 4，正要 rename 的記錄沒有被 truncate
- [x] 2.5 `--fork` 不受影響（既有測試）；其他輸入錯誤仍是 exit 1
- [x] 2.6 每一項在 `git archive` 的副本改壞驗證（exit code 改回 1、拿掉那一行、去掉兩個鎖檢查）會紅

## 3. 文件

- [x] 3.1 README 的 exit code 表補上 4
- [x] 3.2 `docs/design.md` 5.4 的 exit code 與「同一台機器不能同時接續」那條改寫
- [x] 3.3 delta：`session-continue` 的 Requirement 改成 exit 4 加固定一行，附四個 Scenario

## 4. 收尾

- [x] 4.1 `uv run pytest -q tests/unit` 全綠、`python -m compileall` 綠
- [x] 4.2 commit（Refs #54）並 push `agora-locked-signal`；回報 PM
