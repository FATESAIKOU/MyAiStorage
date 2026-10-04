## Context

`tests/_guard.py` 的 `_check()` 看到 `PYTEST_VERSION` 就 return，理由是「conftest 已經隔離了」。這只對 `tests/` 底下的測試檔成立：pytest 只載入測試檔所在目錄（往上到 rootdir）的 conftest。

## Goals / Non-Goals

**Goals：** repo 外的測試檔用 pytest 跑時也會被擋；擋的時機在 import 當下，在任何 helper 碰到目錄之前。

**Non-Goals：** 改產品程式（`agora.store.Paths` 等）；擋住「直接用 agora 指令碰真實資料」（那是團隊規則與 #22 之後的事）。

## Decisions

- 在 pytest 裡，guard 確認 repo 的 `tests/conftest.py` 已經在 `sys.modules` 裡（比對模組的 `__file__` 與 `Path(__file__).with_name("conftest.py")`）。沒有就和 pytest 以外沒隔離一樣，SystemExit。
  - 也可以讓 conftest 在 import 時留一個記號（模組層的常數或環境變數），guard 檢查記號；實作的人選一種，理由寫在註解。
  - 最後兩種都留了，各答一個問題：`sys.modules` 裡的 __file__ 答「這個程序有沒有載入隔離它的 conftest」（conftest 自己的 `import _guard` 也在那時就成立），token 答「這個程序是不是隔離的測試開的子程序」。理由寫在 `_repo_conftest_loaded()` 的 docstring 與 `_check()` 的註解。
- conftest 的 `isolated_home` 是每個測試的 fixture，在 import 時還沒生效，所以 pytest 裡不能拿環境變數判斷，只能判斷 conftest 有沒有載入。
- token 只說明「我是隔離的測試開的」，不證明隔離：有人可以在 shell 裡自己 export，token 也會跟著一份複製過的環境（例如整合測試的 `{**os.environ, "HOME": 真實的 home}`）傳下去，而 conftest 是在 pytest 程序裡直接寫 `os.environ`、不拿掉的。所以只有 token 而沒有載入 conftest 時，不 return，繼續走 pytest 以外那一套四個變數的檢查。
- 子程序測試：用 `tmp_path` 當 HOME 等目錄（子程序的環境從零組起，照 `test_ime_kitty.py` 的做法），在 repo 外寫一個 `import _guard` 的測試檔，用 pytest 跑它；斷言被擋、退出碼非 0、訊息不含路徑。另外故意讓子程序的 HOME 指到一個「假裝是真實」的非暫存目錄（例如 repo 裡的暫時資料夾），斷言那個目錄沒有被寫入。
  - 實作調整兩個（PM 的補充與 review）：不建「假裝是真實」的非暫存目錄，改用 tmp_path 裡的記號檔（import 通過才會寫），斷言它不存在；子程序用 `sys.executable -m pytest -p no:cacheprovider` 跑，不用 `uv run`（`uv run` 會同步 repo 的 `.venv`、需要網路），重點「pytest 沒有載入 repo 的 conftest」不變。
  - 另外補一個「只有 token、四個變數都沒設」的子程序測試，確保 token 不會單獨放行。

## Risks / Trade-offs

- [以後有人把測試搬到 `tests/` 以外的合法位置（例如 `tests/integration/` 以外的新資料夾）] → 只要在 `tests/` 底下，conftest 都會載入；真的要搬到外面時，guard 會擋，逼人想清楚隔離。
- [Low：有人手動 `import conftest`（repo 外的檔自己把 `tests/` 塞進 `sys.path` 再 import），`_repo_conftest_loaded()` 就成立，但 fixture 並沒有被 pytest 註冊，其實沒有隔離（review G3）] → 要刻意才會發生，10-04 那種「複製一個 helper 出去」不會；先記著。真的要擋，可以在 `isolated_home` 裡斷言自己確實執行過，或用 pytest 的 plugin manager 確認 conftest 已經註冊。
- [Low：token 由 conftest 用 `os.environ[...] = "1"` 設，pytest 這個程序結束前都不會拿掉，整合測試開的 agent 子程序也帶著它（review G4）] → 照上面的決定，token 單獨不再能放行（四個變數照樣要查），所以無害；conftest 的註解也說了「token 只是來源標記，不是通行證」。
