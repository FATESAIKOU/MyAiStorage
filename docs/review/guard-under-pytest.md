# Review：guard-under-pytest（issue #25），impl4 的 8709271

審的是 8709271 改到的 `tests/_guard.py`、`tests/conftest.py`、`tests/unit/test_guard.py`，對照的是 `openspec/changes/guard-under-pytest` 的 proposal、design、spec、tasks。

驗證方式：
- 都在 `git archive` 的副本裡做；
- repo 外的探針都放在 scratchpad，用 `env -i` 從零組環境，所有目錄都在暫存目錄底下；
- 沒有碰工作區，也沒有碰任何真實目錄。

## 結論

**10-04 那種情況（repo 外的測試檔、HOME 是真的）已經會被擋下，測試也守著。** 但 PM 的疑慮成立：**token 單獨就能放行**。

PM 傾向的改法（有 token 時也要求四個變數都在暫存目錄）：
- 可行；
- 不會誤擋現有的子程序測試；
- 建議採用，並補一個測試。

另外有一個 Medium：測試裡的 `uv run` 會改動 repo 的 `.venv`，而且需要網路。

## G1（High）：token 單獨就能放行

- **現在的規則：** 在 pytest 裡，`_repo_conftest_loaded() or os.environ.get("AGORA_TESTS_ISOLATED")` 其中一個成立就直接 return，**完全不看**那四個目錄變數。
- **實測：** repo 外放一個 `import _guard` 之後寫記號檔的測試檔，用 `env -i` 跑 `python -m pytest`：

  | 環境 | 8709271 | PM 的改法 |
  |---|---|---|
  | 只有 token，HOME 和三個 `AGORA_*` 都沒設 | **放行，記號檔被寫入** | 擋下，訊息列出四個變數名 |
  | token 加上四個暫存目錄（等同「測試自己開的子程序」） | 放行 | 放行 |
  | 沒有 token，四個暫存目錄 | 擋下 | 擋下 |

- 第一列就是 PM 說的繞過：
  - 有人在 shell 裡 `export AGORA_TESTS_ISOLATED=1`；
  - 或者某個子程序複製了父程序的環境（例如整合測試的 `_real_env()` 是 `{**os.environ, "HOME": 真實的 home}`），token 跟著傳下去，HOME 卻是真的。
  - token 由 conftest 直接寫進 `os.environ`（不是 monkeypatch），所以 pytest 這個程序之後開的**每一個**子程序都會帶著它。
- **現有測試抓不到：** `test_guard.py` 在 8709271 和在 PM 的改法上都是全綠，沒有任何測試能區分兩者。

## PM 的改法：可行，不會誤擋

改法（我在副本裡就是這樣改的）：
- 在 pytest 裡：
  - 這個程序自己載入了 repo 的 conftest → 放行；
  - 有 token → **不 return**，往下走和 pytest 以外一樣的四個變數檢查；
  - 都沒有 → 照現在的訊息擋下。
- 為什麼載入了 conftest 的程序不能也檢查變數：
  - conftest 載入的時候（收集階段），`isolated_home` fixture 還沒生效，HOME 還是真的；
  - 所以這一支只能看「conftest 有沒有載入」，design 原本就是這麼說的。
  - 只有 token 那一支（也就是子程序）能、也應該檢查變數：子程序是在測試執行中開的，`isolated_home` 已經把四個變數設到 `tmp_path`，子程序會繼承。

**驗證：**
- 改過的副本跑完整 unit：**594 passed**，和沒改的一樣。
  - `test_background.py` 的子程序測試（`test_a_reader_of_our_stdout_gets_eof...`）用的是繼承來的環境，四個變數都在 `tmp_path`，照樣放行；
  - `test_ime_kitty.py`、`test_guard.py` 的子程序是從零組環境、沒有 `PYTEST_VERSION`，走的是 pytest 以外的分支，不受影響。
- 整合測試（沒跑，用讀程式確認）：
  - `tests/integration/*` 只在 pytest 的主程序裡 `import _guard`，那時 conftest 已經載入，所以不受影響；
  - 它們開的子程序是 `tests/fakes` 底下的假 rclone、假 agent，用 `git grep` 確認過，`tests/fakes`、`tests/fixtures`、`src` 都沒有 import `_guard`。

**要一起改的：**
- **spec「在 pytest 裡」那一條：** 改成「repo 的 conftest MUST 已經載入；或者這個程序有 conftest 留下的 token，**而且**四個變數都指到暫存目錄」。
- **design：** 補一句「token 只說明『我是隔離的測試開的』，不證明隔離，所以仍然要檢查四個變數」。
- **`_guard.py` 的 docstring：** 現在寫的是「subprocess … inherits the conftest's token together with the temporary HOME it set」，改完之後這句變成「要求」，而不是「假設」。
- **補測試：** repo 外的測試檔，從零組的環境裡**只有 token**（沒有 HOME 和 `AGORA_*`，或者有 token 但目錄不在暫存目錄），要被擋下、記號檔不能被寫入、訊息只列變數名。
  - 測試不需要真的非暫存目錄：讓變數**不設**就足以讓 `_in_temp` 判定為否；
  - 這樣做也符合 PM「不要建假裝是真實的目錄」的補充。

## G2（Medium）：`test_a_test_file_outside_the_repo_is_refused_under_pytest` 用 `uv run`，會改動 repo 的 `.venv`，而且需要網路

- 子程序的指令是 `["uv", "run", "pytest", ...]`，`cwd=str(REPO)`。`uv run` 會先依 `uv.lock` 同步 **REPO 的 `.venv`**：
  - 在副本裡（沒有 `.venv`），這個測試**當場建了一個 `.venv`，裝了 78 個套件**；
  - 在 worktree 裡，它會把開發者的 `.venv` 同步回 lock 的內容：裝缺的、移除多的。
- uv 的快取在子程序的 `XDG_CACHE_HOME`（`tmp_path` 底下），每次都是空的，所以每跑一次就要**從網路下載**。離線時這個測試會失敗。失敗的時候 guard 沒有輸出，斷言 `said` 會先紅，所以不會被誤判為通過。
- 這不會碰到使用者的資料，但測試會順手改動 repo 的 `.venv`，又依賴網路，都不應該。
- **建議：** 改成 `[sys.executable, "-m", "pytest", "-p", "no:cacheprovider", 那個檔]`。
  - 重點是「pytest 沒有載入 repo 的 conftest」，用同一個直譯器的 pytest 一樣能重現 10-04 的情況；
  - 這樣不用網路、不動 `.venv`，`_pytest_env` 裡那段為了 uv 的 PATH 處理也可以拿掉。
  - 我的探針就是這樣跑的，結果和 `uv run` 一樣。

## 測試抓得到錯嗎（在 `git archive 8709271` 的副本裡，一次改壞一處，跑 `test_guard.py` 與 `test_background.py`）

| 改壞的地方 | 結果 |
|---|---|
| 在 pytest 裡直接 return（退回 #25 之前的樣子） | ✓ repo 外的測試紅 |
| 拿掉 token 那一支（沒載入 conftest 就擋） | ✓ `test_background.py` 的子程序測試紅 |
| conftest 不設 token | ✓ 同上 |
| `_repo_conftest_loaded()` 永遠回 True | ✓ repo 外的測試紅 |
| 訊息裡加上 cwd 與 HOME 的路徑 | ✓ repo 外的測試紅（「不印路徑」的斷言） |
| **只有 token 就放行（就是現在的程式）** | **✗ 沒有測試能區分**（G1 要補的那個測試） |

## 子程序的環境：是從零組的，不碰真實目錄 ✓

- `_run()`（pytest 以外的兩個測試）：
  - 環境只有 `PATH`、`PYTHONPATH`，加上測試給的變數；
  - 沒隔離的那個情況，HOME 是 `tmp_path/fake_home`，斷言它在被擋下之後仍然是空的。
- `_pytest_env()`：
  - 也是從零組的：`PATH`（前面加上直譯器的 bin）、`PYTHONPATH`、`AGORA_FOLDER_NAME=agora-test`、指到假 rclone 的 `AGORA_RCLONE`；
  - HOME、三個 `AGORA_*`、四個 `XDG_*` 都在 `tmp_path` 底下。
- 唯一從父程序帶來的是 `PATH`，它只用來找執行檔。
- 唯一碰到 `tmp_path` 以外的地方，就是 G2 說的 REPO `.venv`。

## Low

- **G3：** 手動 `import conftest` 會讓 `_repo_conftest_loaded()` 成立，但 fixture 並沒有被 pytest 註冊，所以其實沒有隔離。
  - 例如 repo 外的檔先 `sys.path.insert(0, tests)` 再 `import conftest`。
  - 這要刻意才會發生，10-04 那種「複製一個 helper 出去」不會。
  - 先記下來就好；真的要擋，可以在 `isolated_home` 裡斷言自己確實執行了，或用 `pytest` 的 plugin manager 確認 conftest 已經註冊。
- **G4：** conftest 用 `os.environ[...] = "1"` 設 token，pytest 這個程序結束前都不會拿掉，整合測試開的 agent 子程序也會帶著它。
  - 照 G1 改完之後，token 本身不再能放行，這就無害了，不用改。
  - 註解可以補一句「token 只是來源標記，不是通行證」。
- **G5：** tasks 1.2 寫「依 PM 補充：不做假裝是真實的非暫存目錄」。G1 要補的測試用「變數不設」就能做到，和這個補充不衝突。

## 給 PM 的清單（請轉告 impl4）

1. **G1：** `_check()` 改成「conftest 已載入 → 放行；有 token → 往下檢查四個變數；都沒有 → 擋」。同步改 spec、design、docstring，並補「只有 token」的測試。
2. **G2：** `uv run pytest` 改成 `sys.executable -m pytest -p no:cacheprovider`，不改 `.venv`、不用網路。
3. **（Low）** G3、G4 記錄在 design 的 Risks 即可。

## 測試執行

- 完整 unit 在 `git archive HEAD`（8709271）的乾淨副本裡跑：compileall 通過，**594 passed**。
- 套上 PM 改法的副本：同樣 **594 passed**。
- 沒有跑整合測試。
- worktree 裡 impl2 還沒 commit 的 `tui.py`、`test_tui.py` 沒有碰。
