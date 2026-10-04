## Context

`tests/_guard.py` 的 `_check()` 看到 `PYTEST_VERSION` 就 return，理由是「conftest 已經隔離了」。這只對 `tests/` 底下的測試檔成立：pytest 只載入測試檔所在目錄（往上到 rootdir）的 conftest。

## Goals / Non-Goals

**Goals：** repo 外的測試檔用 pytest 跑時也會被擋；擋的時機在 import 當下，在任何 helper 碰到目錄之前。

**Non-Goals：** 改產品程式（`agora.store.Paths` 等）；擋住「直接用 agora 指令碰真實資料」（那是團隊規則與 #22 之後的事）。

## Decisions

- 在 pytest 裡，guard 確認 repo 的 `tests/conftest.py` 已經在 `sys.modules` 裡（比對模組的 `__file__` 與 `Path(__file__).with_name("conftest.py")`）。沒有就和 pytest 以外沒隔離一樣，SystemExit。
  - 也可以讓 conftest 在 import 時留一個記號（模組層的常數或環境變數），guard 檢查記號；實作的人選一種，理由寫在註解。
- conftest 的 `isolated_home` 是每個測試的 fixture，在 import 時還沒生效，所以 pytest 裡不能拿環境變數判斷，只能判斷 conftest 有沒有載入。
- 子程序測試：用 `tmp_path` 當 HOME 等目錄（子程序的環境從零組起，照 `test_ime_kitty.py` 的做法），在 repo 外寫一個 `import _guard` 的測試檔，`uv run pytest` 跑它；斷言被擋、退出碼非 0、訊息不含路徑。另外故意讓子程序的 HOME 指到一個「假裝是真實」的非暫存目錄（例如 repo 裡的暫時資料夾），斷言那個目錄沒有被寫入。

## Risks / Trade-offs

- [以後有人把測試搬到 `tests/` 以外的合法位置（例如 `tests/integration/` 以外的新資料夾）] → 只要在 `tests/` 底下，conftest 都會載入；真的要搬到外面時，guard 會擋，逼人想清楚隔離。
