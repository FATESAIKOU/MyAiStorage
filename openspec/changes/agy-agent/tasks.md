## 1. 拿掉接續的預設閘門（#141）

- [x] 1.1 `src/agora/agents/agy.py`：刪掉 `RESTORE_OPT_IN_ENV`、`AGY_RESTORE_OFF`、`restore_enabled()` 與 `start_native` 開頭的檢查；`os` import 跟著拿掉
- [x] 1.2 `src/agora/cli.py`：刪掉這三個名字的 import 與 `cmd_continue` 的 `restore_enabled()` 檢查
- [x] 1.3 `tests/unit/test_agy_continue.py`：拿掉 fixture 裡設定的接續開關，刪掉兩個閘門測試
- [x] 1.4 `tests/unit/test_atelier_harness.py`：非憑證環境變數名的範例改用 `AGORA_AGY_HOME`（斷言語意不變）

## 2. 文件改成現況

- [x] 2.1 `README.md`：目錄說明與「agy 的範圍與限制」拿掉「接續預設拒絕／開關」，寫成同 agent 接續可用、會寫入真的 `~/.gemini/antigravity-cli`、建議先備份
- [x] 2.2 `docs/design.md`：「agy 接續的預設開關」改成 2026-10-21 驗過、無閘門的現況
- [x] 2.3 `docs/acceptance.md` 第 18 節：18.5／18.11 拿掉接續開關的環境變數；18.15 改成「不設任何開關再接一次」；18.17 與「已知問題」標明 2026-10-21 已驗
- [x] 2.4 `docs/agy-storage.md` §12：寫入驗證結果（載入：是；正常退出後沒有 `-wal`／`-shm`；agy 改寫 summaries 列的逐欄差異）
- [x] 2.5 `docs/agy-storage.md` §13：改成「曾經有閘門，2026-10-21 驗證後移除」的簡短歷史說明（保留，不刪）

## 3. spec

- [x] 3.1 `specs/agy-agent/spec.md`：匯入（唯讀、經快照：無 `-wal` 用 `immutable=1`、有 `-wal` 複製 db＋wal 後重放、不複製 `-shm`）
- [x] 3.2 同檔：清單與搜尋只到標題＋preview
- [x] 3.3 同檔：raw 信封 `agy-conversation/1`（同一假 db 匯兩次 md5 相等）
- [x] 3.4 同檔：`continue --agent agy`（還原＋補列＋失敗收回；活體較新就開活體）
- [x] 3.5 同檔：不支援的動作（merge、跨 agent continue、fork、headless）在邊界拒絕並說明
- [x] 3.6 同檔：憑證檔不讀
- [x] 3.7 `openspec validate agy-agent` 通過

## 4. 收尾

- [x] 4.1 `uv run pytest -q tests/unit/test_agy_continue.py tests/unit/test_agent_agy.py tests/unit/test_atelier_harness.py tests/unit/test_agy_fixture.py` 全綠
- [x] 4.2 回報寫進 `MyAiStorage-team/reports/impl-141.md`
