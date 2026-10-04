## Why

`tests/_guard.py`（T8）只在「不在 pytest 裡」時檢查隔離；在 pytest 裡就直接放行，假設 `tests/conftest.py` 已經把 HOME 與 agora 的 config／cache／state 都換成暫存目錄。

但 conftest 只管得到 `tests/` 底下的測試檔。10-04 有隊員把測試檔放在 repo 外，用 `uv run pytest <那個檔>` 跑：guard 放行、conftest 沒載入，測試 helper 因此讀寫了使用者真實的 agora 快取，兩個假 Session 進了真實的鏡像，一個真實 Session 的 id 也進了外部模型（issue #25）。口頭規則已經失敗過四次，要用程式擋。

## What Changes

- `tests/_guard.py` 在 pytest 裡也要確認隔離成立：repo 的 `tests/conftest.py` 沒有載入時，和 pytest 以外沒隔離一樣，import 當下就擋下。
- 補子程序測試：在 repo 外（暫存目錄）放一個 import 測試 helper 的測試檔，用 pytest 跑，要被擋下，而且不碰任何真實目錄。

## Capabilities

### New Capabilities

- `test-isolation`：測試 helper 只在隔離的環境執行。

### Modified Capabilities

（無）

## Impact

- 程式：`tests/_guard.py`，可能動到 `tests/conftest.py`（例如留一個「已載入」的記號）。產品程式不變。
- 測試：`tests/unit/test_guard.py`。
- 相關：issue #25；事故時在做的 #22。
