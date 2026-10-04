## 1. 實作（負責：impl4）

- [x] 1.1 `tests/_guard.py`：在 pytest 裡確認 repo 的 `tests/conftest.py` 已載入，沒有就 SystemExit（訊息只說原因，不印路徑）；更新模組的 docstring，說明 10-04 的情況（不寫任何真實 id）（design「Decisions」）
  - 選了 design 說的第二種以外的查法：比對 `sys.modules` 裡模組的 `__file__`（理由寫在 `_repo_conftest_loaded()` 的 docstring）。另外 conftest import 時留一個 token（`AGORA_TESTS_ISOLATED`）給子行程繼承：pytest 把 `PYTEST_VERSION` 傳給每個子行程，所以「測試自己開的子行程」和「repo 外單獨跑的 pytest」在環境變數上看不出來分，而前者確實有隔離（繼承 conftest 設的暫存 HOME）。少了這一段 `test_background.py` 的子行程會被誤擋。
- [x] 1.2 子程序測試（`tests/unit/test_guard.py`）：repo 外的測試檔用 pytest 跑會被擋、不碰「假裝是真實」的目錄；`tests/` 底下照常；pytest 以外的兩種情況照舊。在副本裡故意改壞（例如拿掉新檢查）確認測試會紅
  - 依 PM 補充：不做「假裝是真實」的非暫存目錄，改用 tmp_path 裡的記號檔（import 通過才會寫）。子程序環境從零組起（`_pytest_env`），`uv run pytest` 跑 tmp_path 裡的測試檔。
  - 副本（`git archive HEAD` + 本次三個檔）驗過：拿掉新檢查 → 新測試紅（退出碼 0、記號檔被寫）；拿掉 token 那段 → `test_background.py` 紅。副本裡 HEAD 的 src 跑完整 unit 594 綠（worktree 裡那 3 個 test_tui 紅的是 impl2 未 commit 的 tui.py）

## 2. 收尾

- [ ] 2.1 review 審
- [ ] 2.2 和 `preview-search-keys`、`ime-kitty-keyboard` 一起開 PR 合進 main（使用者合）；issue #25 照規則關閉
