## ADDED Requirements

### Requirement: 測試 helper 只在隔離的環境執行
import 任何測試模組或測試 helper 時，如果使用者真實的目錄可能被碰到，MUST 在 import 當下就擋下（SystemExit），而且 MUST NOT 讀寫任何真實目錄。

- 不在 pytest 裡：`AGORA_CACHE_DIR`、`AGORA_STATE_DIR`、`AGORA_CONFIG`、`HOME` 都要指到暫存目錄，否則擋下（T8，照舊）。
- 在 pytest 裡：repo 的 `tests/conftest.py` MUST 已經載入（它負責每個測試的隔離），否則擋下。
- 擋下時的訊息 MUST 只有變數名稱或原因，不印出任何路徑、標題或 Session 的內容。

#### Scenario: 在 repo 的 tests 底下用 pytest
- **WHEN** 用 `uv run pytest` 跑 `tests/` 底下的測試
- **THEN** 照常執行

#### Scenario: repo 外的測試檔
- **WHEN** 在暫存目錄放一個 import 測試 helper 的測試檔，用 `uv run pytest <那個檔>` 跑，HOME 等變數沒有指到暫存目錄
- **THEN** 被擋下，訊息說明原因；真實的 HOME、agora 的 config／cache／state 都沒有被讀寫

#### Scenario: pytest 以外沒隔離
- **WHEN** 在 pytest 以外 import 測試 helper，HOME 沒指到暫存目錄
- **THEN** 被擋下（照舊）

#### Scenario: pytest 以外有隔離
- **WHEN** 在 pytest 以外 import 測試 helper，四個變數都指到 /tmp 底下
- **THEN** 照常 import
