anthropic/claude-opus-5-5

# #341（#121 的 121-D）設計筆記：把 continue 的子行程協議外殼從 cli.py 抽出來

- 作者：專案架構師 myaistorage-arch-m1-1（作者模式：由另一個模型的審查者照文末「審查清單」審過，才派實作）。
- 基準：main e57eaad（#339 merge 之後）。下面的行號都以它為準。
- 範圍來源：#341 工單；#121 拆單報告的 121-D。

## 1. 為什麼、做什麼

`cli.py` 有 3346 行。`agora continue` 的兩條子行程邊界（periodic round、startup load）在外殼這一側的程式，夾在 cli.py:1627-1855（約 230 行）：啟動子行程、逾時、收行程群組、錯誤對應、把 round 的 diff 套回 `_Run`。

兩個子行程模組在頂層 `from agora import cli`（round_child.py:38、start_child.py:32）。cli 則是在函式內 lazy import round_child（cli.py:1676 的 `_round_child`、:1842 的 `_apply_diff`），只為了拿 `state_of`／`decode_fingerprint` 這兩個編解碼函式。外殼、子行程、協議三者綁成一團。

**這張單只做一件事**：把協議外殼（以及它需要的、本來放在 round_child 的線上編解碼）搬到新模組 `agora/continue_shell.py`。純搬移，不改行為。

## 2. 模組切割

### 2.1 新模組 `src/agora/continue_shell.py`

**職責**：外殼這一側的子行程協議，包括啟動 `python -m agora.round_child`／`agora.start_child`、送出狀態、限時、收行程群組、把子行程的回答轉成例外或 `_Run` 的狀態。

**不做**：代理行程的監看（`_forward_sigterm`、`_GentleStop`、`_wait_*`，cli.py:1934 以後）、`_periodic_write`／`_final_write`／`_repair_pending`（輪的結果怎麼算、怎麼記），也不跑子行程本身的 round 或 load。這些都留在 cli。

**搬入的符號**（照原順序）：

| 來源 | 符號 |
|---|---|
| cli.py:66 | `CONTINUE_LOG` |
| cli.py:1627-1634 | 註解區塊、`ROUND_TIMEOUT_SECONDS`、`ROUND_TIMEOUT_MAX_SECONDS` |
| cli.py:1637 | `_round_timeout` |
| cli.py:1652 | `RoundChildDead` |
| cli.py:1658 | `_round_child` |
| cli.py:1698 | `_kill_round_group` → **改名 `_kill_child_group`**（§2.4） |
| cli.py:1733 | `_reap_group` |
| cli.py:1756 | `StartChildDead` |
| cli.py:1763 | `_start_child` |
| cli.py:1799 | `_start_payload` |
| cli.py:1809-1814 | 註解、`_CHILD_ERRORS` |
| cli.py:1816 | `_raise_child_error` |
| cli.py:1827-1832 | 註解、`ROUND_DIFF_KEYS` |
| cli.py:1834 | `_apply_diff` |
| round_child.py:41 | `state_of` |
| round_child.py:54 | `decode_fingerprint` |

用 ast 算過 cli.py:1627-1855 的自由名稱，只有：`Agent`、`AgentError`、`env_seconds`（agents/base.py）、`h`（header）、`store`、`InputError`、`CONTINUE_LOG`，以及標準庫的 `contextlib`、`json`、`math`、`os`、`signal`、`subprocess`、`sys`、`Path`；另外有字串型別註記 `"_Run"`。所以全部都能從比 cli 低的模組取得，**不需要 cli 的任何名字**。

**為什麼 `state_of`／`decode_fingerprint` 一起搬**：它們是線上格式（外殼把 `_Run` 編成 JSON、把回答的 fingerprint 解回 tuple），本身不依賴 cli。留在 round_child 的話，continue_shell 要拿它們就得 import round_child，而 round_child 在頂層 import cli，等於繞一圈又依賴回 cli。搬過來之後，協議模組就是外殼和子行程**共同依賴**的那一層：

```
cli ──► continue_shell ◄── round_child ──► cli
                       （start_child 不需要 continue_shell）
```

round_child／start_child 依賴 cli 是可以接受的：它們本來就是在子行程裡跑 cli 的 round 或 load 本體，這一張不動。

### 2.2 `InputError` 搬到 `src/agora/errors.py`

- 新增 `agora/errors.py`，只放 `class InputError(Exception)`，docstring 照抄 cli.py:70-71。
- cli 改成 `from agora.errors import InputError`。`cli.InputError` 仍然存在，而且**是同一個物件**，所以 `pytest.raises(cli.InputError)` 和 cli 內的 `except InputError` 都不受影響。
- 為什麼不放進 continue_shell：InputError 是整個 CLI 的「使用者問了做不到的事」，用在 cli 的幾十處；continue_shell 只是它的使用者之一。
- 為什麼不放 agents/base.py：它和 agent 無關。

### 2.3 cli 重新匯出與呼叫方式

cli 加一段：

```python
from agora.continue_shell import (CONTINUE_LOG, ROUND_TIMEOUT_SECONDS, ROUND_TIMEOUT_MAX_SECONDS,
                                  RoundChildDead, StartChildDead, _apply_diff, _reap_group,
                                  _round_child, _round_timeout, _start_child, _start_payload)
```

這份清單是兩個集合的聯集（main e57eaad 實際 grep 過）：
- cli 自己在區段外用到的：`_round_child`（:1897）、`_apply_diff`（:1900）、`_start_child`（:2504）、`CONTINUE_LOG`。
- 測試或其他 src 透過 `cli.` 讀取的：`_start_child`（12 處）、`_round_child`（9 處）、`CONTINUE_LOG`（9 處）、`_apply_diff`（4 處）、`StartChildDead`（3 處）、`_reap_group`（2 處）、`RoundChildDead`（2 處）、`_round_timeout`（1 處）、`_start_payload`（1 處，tests/unit/_round_seam.py:30）。

**cli 內部一律用自己命名空間裡的裸名呼叫**（`_round_child(...)`、`_start_child(...)`、`_apply_diff(...)`），不要寫成 `continue_shell._round_child(...)`。這樣 `monkeypatch.setattr(cli, "_round_child", ...)`（4 處）和 `setattr(cli, "_start_child", ...)`（10 處）替換的就是 cli 實際呼叫的那個名字，不用改。

**不重新匯出**的名字：`_kill_child_group`、`_CHILD_ERRORS`、`_raise_child_error`、`ROUND_DIFF_KEYS`，以及舊名 `_kill_round_group`。cli 在區段外沒有用到它們（grep 確認過）。

**已知會失效的接縫**（實作時要改，見 §5 的 PM 決定）：
- `monkeypatch.setattr(cli, "_kill_round_group", ...)` 共 3 處：tests/unit/test_continue_round_child.py:888、:918，test_continue_start_child.py:176。它們要讓 `_reap_group`／`_start_child` 看到替身；搬家後，這兩個函式是在 continue_shell 的全域名稱裡找 `_kill_child_group`，替換 cli 的名字就沒有作用了。
- 不管改不改名，這 3 處的替換對象都必須改成 continue_shell。若在 cli 留一個 alias，替換它會**靜悄悄沒有效果**（測試照樣通過，卻沒有驗到東西），所以**不留 alias**。
- `cli.subprocess.Popen`、`cli.os.killpg` 這類替換（test_continue_round_child.py:454、:482、:838-839 等）不受影響：它們改的是共用的標準庫模組物件，continue_shell 用的是同一個 `subprocess`／`os`。

### 2.4 `_kill_round_group` 改名為 `_kill_child_group`

- 它從 #339 起也用來收 start child，名字裡的 round 已經不準了（#339 架構審的低項）。
- 反正 3 處替換的對象一定要改，順便改名不會多出接縫的成本，只多 1 處直接呼叫（test_continue_round_child.py:297 `cli._kill_round_group(proc)`）。
- 本體逐字不變，只改 `def` 那一行，以及 `_reap_group` 裡的一次呼叫和它 docstring 裡的一次提及。round_child.py:103 docstring 裡的名字跟著改。測試的函式名稱（例如 `test_kill_round_group_takes_...`）不用改。

### 2.5 round_child／start_child 改了什麼

- **round_child.py**：刪掉 `state_of`、`decode_fingerprint` 的定義（:41-60），改成 `from agora.continue_shell import decode_fingerprint, state_of`，保留 `round_child.state_of` 這個名字（tests 3 處、`_round_seam` 用到）。`decode_run` 照舊呼叫裸名 `decode_fingerprint`。:103 的 docstring 改名字。其他不動，**頂層的 `from agora import cli` 也不動**。
- **start_child.py**：不改。它的錯誤回答形狀 `{"err": {"kind", "text"}}` 由 continue_shell 的 `_raise_child_error` 讀；寫的那一邊（start_child.py:44）這張單不動。

### 2.6 continue_shell 本身的寫法

- 第一行用 `from __future__ import annotations`；`_Run` 只出現在字串型別註記裡，**不為了型別去 import cli**（連 `TYPE_CHECKING` 都不用；repo 沒有型別檢查器）。
- import 只有：標準庫、`agora.header as h`、`agora.store`、`agora.errors.InputError`、`agora.agents.base` 的 `Agent, AgentError, env_seconds`。
- 搬過來的函式本體逐字不變，只有 §4 列出的替換。模組 docstring 寫明職責和「不 import cli、round_child、start_child」這條規則。

## 3. 依賴方向（加進 docs/architecture/overview.md）

- `continue_shell` → {`header`, `store`, `errors`, `agents.base`}；**不 import** `agora.cli`、`agora.round_child`、`agora.start_child`，也不 lazy import。
- `cli` → `continue_shell`、`errors`（頂層）。
- `round_child` → `continue_shell`、`cli`（頂層）；`start_child` → `cli`（頂層）。
- `errors` 不 import 任何 agora 模組。

## 4. 給實作的介面契約

- **C-1 新模組與搬移清單**：`src/agora/continue_shell.py` 含 §2.1 表中所有符號，cli.py／round_child.py 裡不再有它們的定義（重新匯出的 import 不算定義）。
- **C-2 逐字搬移**：每個搬移符號在新檔裡的原始碼（含 docstring 和緊鄰的 `#:` 註解），和 main e57eaad 的舊原始碼，只允許以下差異：
  - (a) `_kill_round_group` → `_kill_child_group`（def 行、`_reap_group` 的一處呼叫，以及它 docstring 裡的一處提及，cli.py:1698、:1736、:1745）；
  - (b) 刪除 `_round_child`、`_apply_diff` 內的 `from agora import round_child` 各一行；
  - (c) `round_child.state_of(` → `state_of(`、`round_child.decode_fingerprint(` → `decode_fingerprint(`。

  證明方式：實作者用 ast 取出新舊兩邊每個符號的原始碼片段（`ast.get_source_segment`，加上前置的 `#:` 註解），套用上面三種替換之後逐字比對，**把比對結果（每個符號 same／diff）貼進 PR 描述**。比對腳本不進 repo。
- **C-3 依賴方向（靜態）**：新增測試 `tests/unit/test_continue_shell_deps.py`（第一行 `import _guard`），用 ast 掃 `src/agora/continue_shell.py` 的所有 `Import`／`ImportFrom`（包含函式內的），斷言沒有 `agora.cli`、`agora.round_child`、`agora.start_child`，`from agora import` 後面也不能出現 `cli`／`round_child`／`start_child`。errors.py 不能 import 任何 `agora` 模組，也一起斷言。
- **C-4 依賴方向（執行期）**：同一個測試檔，用 `subprocess.run([sys.executable, "-c", "import sys, agora.continue_shell; print(sorted(m for m in sys.modules if m in ('agora.cli','agora.round_child','agora.start_child')))"])`，斷言輸出是 `[]`。
- **C-5 cli 重新匯出**：§2.3 清單裡的 11 個名字，`getattr(cli, n) is getattr(continue_shell, n)`；並且 `cli.InputError is errors.InputError`。放在同一個測試檔。
- **C-6 接縫仍有效**：既有的 `monkeypatch.setattr(cli, "_round_child"|"_start_child", ...)` 不改，所在的測試全綠。證明了 cli 內部是用裸名呼叫的。
- **C-7 round_child 的名字保留**：`round_child.state_of is continue_shell.state_of`、`round_child.decode_fingerprint is continue_shell.decode_fingerprint`（同一個測試檔）。
- **C-8 改名**：`continue_shell._kill_child_group` 存在；`cli` 和 `continue_shell` 都**沒有** `_kill_round_group` 這個屬性（同一個測試檔斷言 `not hasattr`）。
- **C-9 行為不變**：全 unit 綠。測試的改動只限 §5 決定 1 列出的那幾行，加上新增的 test_continue_shell_deps.py；沒有任何斷言或期望值改變。
- **C-10 文件**：本 note、`docs/architecture/overview.md`、`docs/architecture/decisions.md` 和程式在同一個 PR（overview／decisions 由架構師先 commit 在這個 branch 上）。

**改壞要紅**：
- 在 continue_shell 的任一函式內加 `from agora import cli` → C-3 紅（C-4 抓不到 lazy 的情況，所以兩支都要有）。
- 在 cli 改成 `continue_shell._round_child(...)` 呼叫 → 既有的 `setattr(cli, "_round_child")` 測試會跑真的子行程（或紅）→ C-6 紅。
- 在 cli 留 `_kill_round_group = _kill_child_group` → C-8 紅。
- 漏了重新匯出 `_start_payload` → `_round_seam` 紅。

## 5. 要 PM 決定的事

1. **完成判準「既有測試不改」要放寬到下面這幾行**（只改替換對象和呼叫名稱，不改斷言）：
   - test_continue_round_child.py:297 `cli._kill_round_group(proc)` → `continue_shell._kill_child_group(proc)`；
   - :888、:918 `monkeypatch.setattr(cli, "_kill_round_group", ...)` → `monkeypatch.setattr(continue_shell, "_kill_child_group", ...)`；
   - test_continue_start_child.py:176 同上；
   - 以上兩個檔各加一行 `from agora import continue_shell`。

   不放寬的話，只能把 `_kill_round_group` 和 `_reap_group` 留在 cli，continue_shell 再反過來 import cli，就違反了這張單的依賴方向。**建議放寬**。
2. **模組名 `continue_shell`**：沿用工單和 #121 報告的用詞。若想要更直白的 `continue_child_protocol` 之類，由我改 note；形狀不變。

## 6. 被捨棄的方案

- **cli 留 alias 保住 `cli._kill_round_group` 的替換**：替換 alias 不會影響實際呼叫，測試會悄悄地沒有驗到東西。比改 4 行測試糟。
- **continue_shell 用 lazy import 拿 round_child 的編解碼**：等於繞一圈依賴回 cli，C-4 也會被 lazy 騙過。
- **用 `_CHILD_ERRORS` 由 cli 注入 InputError**：模組要靠呼叫方補齊才完整，初始化順序變成隱性的契約。
- **把代理監看（`_GentleStop` 等）一起搬**：職責不同（監看前景 agent，對照子行程協議）；混在一起，新模組又會變成第二個 cli。真的需要時另開。

## 7. 風險與未決

- **風險（中）**：import 順序。cli 頂層 import continue_shell，continue_shell 不 import cli，不會有循環。round_child 頂層先 import cli、再 import continue_shell，cli 載入時已經把 continue_shell 載好了，也沒有循環。由 C-4 加上全 unit 驗證。
- **風險（低）**：`CONTINUE_LOG` 搬家。cli、round_child（`cli.CONTINUE_LOG`）、測試（`cli.CONTINUE_LOG` 9 處）都透過重新匯出拿到同一個字串，不受影響。
- **未決（不在這一張）**：start_child 的錯誤回答形狀（寫在 start_child.py:44）和讀的那一邊（`_raise_child_error`）分在兩個模組，沒有共同常數。等 continue_shell 穩定後，可以把它搬成協議常數（記在 decisions 的待決）。

## 8. PR 切分

一個 PR（d）。架構師的三份文件先 commit 在 branch `m1/341-continue-shell`；實作者在同一個 branch 上加：`src/agora/errors.py`、`src/agora/continue_shell.py`、cli.py／round_child.py 的改動、test_continue_shell_deps.py，以及 §5 決定 1 列的測試行。

## 9. 審查清單（給非作者審查者；作者模式）

請逐條回「符合／不符合（附理由）」：

1. **模組切割**：§2.1 的職責和「不做」清楚嗎？搬入的符號是否都屬於「外殼這一側的子行程協議」？有沒有該搬沒搬、或不該搬卻搬了的（對照 cli.py:1627-1855、round_child.py:41-60）？
2. **介面**：§2.3 的重新匯出清單是否等於「cli 區段外的實際使用」加上「測試和 src 透過 `cli.` 的實際使用」？請自己 grep 驗證；不允許多出來或少掉的名字。
3. **依賴方向**：§3 有沒有形成循環？`state_of`／`decode_fingerprint` 搬到 continue_shell 的理由是否成立？
4. **SOLID 與這個層級的檢查**：
   - 單一職責：continue_shell 只做協議外殼，沒有混進監看或輪的結果處理。
   - 依賴反轉：低層模組不知道 cli。
   - 開放封閉：本張不適用（純搬移），請確認確實沒有新的抽象被偷渡進來。
   - 介面隔離：重新匯出沒有超出需要。
   - InputError 的位置（§2.2）是否合理？
5. **被捨棄的方案**（§6）：理由是否站得住？特別是「不留 alias」。
6. **契約**：C-1～C-10 是否都能用測試或 PR 描述裡可檢查的證據驗證？「改壞要紅」的四條是否真的會紅？
7. **測試策略**：C-3（靜態）加 C-4（執行期）兩支並存的理由是否成立？C-2 的逐字比對方法能不能抓到搬移時順手改字？
8. **風險與未決**：有沒有漏掉的 import 時機、或接縫失效的情況？請 grep `setattr(cli,`、`cli\.` 對照搬移清單。
9. **PM 決定**（§5）：放寬的測試行是否最小？還有沒有別的路，可以既不違反依賴方向、又完全不改測試？
