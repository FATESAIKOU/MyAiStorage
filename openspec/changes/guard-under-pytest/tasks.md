## 1. 實作（負責：impl4）

- [ ] 1.1 `tests/_guard.py`：在 pytest 裡確認 repo 的 `tests/conftest.py` 已載入，沒有就 SystemExit（訊息只說原因，不印路徑）；更新模組的 docstring，說明 10-04 的情況（不寫任何真實 id）（design「Decisions」）
- [ ] 1.2 子程序測試（`tests/unit/test_guard.py`）：repo 外的測試檔用 pytest 跑會被擋、不碰「假裝是真實」的目錄；`tests/` 底下照常；pytest 以外的兩種情況照舊。在副本裡故意改壞（例如拿掉新檢查）確認測試會紅

## 2. 收尾

- [ ] 2.1 review 審
- [ ] 2.2 和 `preview-search-keys`、`ime-kitty-keyboard` 一起開 PR 合進 main（使用者合）；issue #25 照規則關閉
