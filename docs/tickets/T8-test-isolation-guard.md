# T8：測試的小工具在沒有隔離時拒絕執行

2026-10-04，PM。狀態：進行中。

## 為什麼

隊員在 pytest 以外直接 import 測試檔、呼叫裡面的 helper（`_app`、`_index`、`_slow_search_app`…），而 conftest 的隔離只在 pytest 裡生效，所以讀寫到使用者**正式的** `~/.cache/agora`、`~/.local/state/agora`：

| 時間 | 誰 | 後果 |
|---|---|---|
| 10-03 08:38 | 不明（`docs/review/T4.md` 附近的探測） | 假 Session `01000…0001～0003` 寫進正式鏡像與墓碑 |
| 10-03 之後某次 | 不明 | `01000…0001～0003` 又出現 |
| 10-04 00:28 | impl1（`f019167` 的診斷） | 約 8 筆**真實 Session 的 ULID、標題、內文開頭印在終端機，進了外部模型（Ollama Cloud）的對話**；假 Session `01AAAA…`、`01BBBB…` 寫進正式鏡像與索引 |

PM 已經用 `agora pull session … --not-exist-delete` 清掉這五筆假 Session（正式的鏡像剩 6 筆，和 Drive 一致）。外流到外部模型的那段**收不回來**，要告訴使用者。

## 要做的

| # | 內容 |
|---|---|
| 1 | 新增 `tests/_guard.py`：不在 pytest 裡（沒有 `PYTEST_VERSION` 環境變數），而且 `AGORA_CACHE_DIR`、`AGORA_STATE_DIR`、`AGORA_CONFIG`、`HOME` 任一個沒有指到暫存目錄（`/tmp`、`/private/tmp`、`/var/folders`、`/private/var/folders` 底下）時，`raise SystemExit("…測試的 helper 只能在隔離的環境用…")`。訊息裡**不**印出任何路徑以外的內容 |
| 2 | `tests/unit/*.py`、`tests/integration/*.py`、`tests/conftest.py` 的最上面都 `import` 它（或由一個共用的 helper 模組 import），讓「直接 import 測試檔」的那一刻就擋下 |
| 3 | 測試：用子程序以 `python -c "import sys; sys.path.insert(0,'tests/unit'); import test_tui"` 在沒有隔離的 env 跑，斷言 exit 非 0、訊息正確、**沒有**碰到傳入的假 HOME 以外的地方；有隔離時正常 import |
| 4 | `docs/design.md` 測試那一節補一句：測試的 helper 只能在 pytest 或隔離的環境裡用 |
