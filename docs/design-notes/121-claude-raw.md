anthropic/claude-opus-5-5

# #342（#121 的 121-E）設計筆記：把 claude 的 raw 格式抽成 `agents/claude_raw.py`

- 作者：專案架構師 myaistorage-arch-m1-1。作者模式：由另一個模型的審查者照文末「審查清單」審過，才派實作。
- 基準：main 9584a9d（#341 merge 之後）。下面的行號都以它的 `src/agora/agents/claude.py`（1385 行）為準。
- 範圍來源：#342 工單；#121 拆單報告的 121-E；#338（121-A）的互比安全網已經在 main。

## 1. 為什麼、做什麼

claude.py 把三件事混在一起：Claude Code 的檔案位置和 session 解析（turns、標題、cwd）、raw 封包格式（`{"format", "main", "aux"}` 的寫法和讀法）、還原。

這張單把 **raw 封包格式**（常數、bytes writer、reader、aux 的串流編碼、`PackedRaw` 紀錄）搬到 `agents/claude_raw.py`。純搬移，不改格式，不合併 writer。

## 2. 模組切割

### 2.1 邊界的關鍵：`_stream_raw` 和 `_Facts`／`_accumulate` 留在 claude.py

用 ast 對 claude.py 頂層符號算了依賴：

- `_stream_raw`（:583）→ `PackedRaw`、`_AUX_SEP`、`_RAW_HEAD`、`_iter_aux`、`_iter_lines`、`_Facts`、`_MISSING`、`_accumulate`
- `_accumulate`（:560）→ `_Facts`、`_take_turn`、`_title_of`、`_is_noise`、`TITLE_MAX`
- `_take_turn`（:544）→ `_user_lines`、`_assistant_lines`、`_is_noise`、`_SILENT_TYPES`

`_stream_raw` 是「讀檔、驗證、折疊成 `_Facts`、寫 raw」**一趟做完**（design B.2，為了記憶體上限），解析和格式在同一個迴圈裡交錯。

- 把 `_stream_raw` 連同 `_Facts`／`_accumulate` 一起搬，就得把整套閱讀版解析（`_take_turn` → `_user_lines`／`_assistant_lines`／`_block_lines`…）也拖過去，claude_raw 就不再是「格式」模組。
- 只搬 `_stream_raw`、把折疊改成注入，則要改簽名和本體，不是純搬移，還會動到 golden 鎖住的錯誤先後順序。

**決定**：`_stream_raw`、`_exported`、`_Facts`、`_take_turn`、`_accumulate`、`_MISSING` 都**留在 claude.py**，作為「解析加格式」的組合者。claude_raw 只提供它組合用的格式零件（`_RAW_HEAD`、`_AUX_SEP`、`_iter_aux`、`PackedRaw`）。`_iter_lines`（讀 session jsonl 的輸入端）也留在 claude.py。

### 2.2 新模組 `src/agora/agents/claude_raw.py`

**職責**：claude raw 封包的格式本身，包括格式名、整份 bytes 的 writer（`_pack_raw`）、reader（`_unpack_raw`）、串流 writer 用的固定位元組（`_RAW_HEAD`／`_AUX_SEP`）、aux 的串流編碼（`_iter_aux` 和它的讀檔 helper），以及 aux 還原時的解碼（`_aux_files`）、串流寫完的紀錄（`PackedRaw`）。

**不做**：解析 session 行的語意（turns、標題、cwd、noise）、找檔案、寫回 Claude Code 的目錄（`_unpack_aux`、`_write_new` 留在 claude.py）、cursor（`_cursor_ok` 等）。

**搬入的符號**（照原順序，四段）：

| 段 | claude.py 行（9584a9d） | 符號 |
|---|---|---|
| 1 | :38 | `FORMAT` |
| 2 | :267-354 | `_AUX_CHUNK`、`_read_chunks`、`_is_utf8`、`_iter_aux` |
| 3 | :428-441 | `_aux_files` |
| 4 | :459-514 | `_pack_raw`、`#:` 註解、`_RAW_HEAD`、`_AUX_SEP`、`PackedRaw`、`_unpack_raw` |

用 ast 算過這四段的自由名稱，只有 `AgentError`（agents/base.py），以及標準庫的 `Iterator`、`Path`、`PurePosixPath`、`array`、`base64`、`codecs`、`dataclass`、`field`、`io`、`json`。**不需要 claude.py 的任何名字**。

### 2.3 claude.py 重新匯出與呼叫方式

claude.py 加一段：

```python
from agora.agents.claude_raw import (FORMAT, PackedRaw, _AUX_SEP, _RAW_HEAD, _aux_files, _iter_aux,
                                     _pack_raw, _unpack_raw)
```

這 8 個名字是兩個集合的聯集（9584a9d 上 grep 過）：
- **claude.py 留下的程式在用的**：`_stream_raw` 用 `PackedRaw`、`_AUX_SEP`、`_RAW_HEAD`、`_iter_aux`；`_exported` 用 `PackedRaw`；`ClaudeAgent` 用 `_pack_raw`（:1081、:1100）、`_unpack_raw`（:1075、:1096、:1162）、`_aux_files`（:1132）。
- **測試或其他 src 透過 claude 模組讀的**：`C._pack_raw`（13 處）、`C.FORMAT`（6 處）、`C._unpack_raw`（2 處），以及 `claude._iter_aux`（8 處）、`claude._unpack_raw`（1 處）。src 其他模組只透過 claude 用 `_exported`、`find_jsonl`、`projects_dir`，這三個不搬。

**claude.py 內部一律用自己命名空間的裸名呼叫**（現況就是），所以 tests/unit/test_mem_golden.py:441 的 `monkeypatch.setattr(claude, "_unpack_raw", count_unpack)` 照樣有效。

**claude.py 的 import 清理**：搬完後 claude.py 不再用到 `base64`、`codecs`、`io`（把四段拿掉後，對剩下的程式用 ast 算過），這三行 import 要刪。`Iterable` 留著，`_stream_raw` 的字串型別註記 `"Iterable[int] | bool"` 還在用它。

**只為測試存在的重新匯出**：`FORMAT` 搬完後 claude.py 自己不再用，重新匯出只是為了 6 處測試讀 `C.FORMAT`。這是相容層，記為 decisions 的待決 P-10。

**不重新匯出**：`_AUX_CHUNK`、`_read_chunks`、`_is_utf8`。claude.py 留下的程式不用它們。依 D-12，不留「替換了也沒效果」的名字。

**已知會失效的接縫**（見 §5 的 PM 決定）：tests/unit/test_store_raw_copy.py 有 5 處 `monkeypatch.setattr(claude, "_is_utf8", ...)`（:450、:471、:492、:509、:543）和 4 處 `real = claude._is_utf8`（:442、:464、:485、:536）。它們要讓 `_iter_aux` 看到替身；搬家後，`_iter_aux` 是在 claude_raw 的全域名稱裡找 `_is_utf8`，替換對象必須改成 `claude_raw`。

### 2.4 claude_raw 本身的寫法

- 版面：模組 docstring → `from __future__ import annotations` → import → 一行標記 `# --- moved verbatim (#342) ---` → 搬移區塊（同 #341 C-2）。
- import 只有：上一節列的標準庫，以及 `from agora.agents.base import AgentError`。
- docstring 寫明職責、格式（可以照抄 claude.py 開頭 :5-8 的格式說明），以及「不 import claude、store」。
- claude.py 的模組 docstring 不改。

## 3. 依賴方向（更新 docs/architecture/overview.md）

- `agents/claude_raw` → `agents/base`（只拿 `AgentError`），以及標準庫。**不 import** `agora.agents.claude`、`agora.store`，也不用相對匯入或動態匯入。
- `agents/claude` → `agents/claude_raw`（新增），其餘不變（`agents.base`、`private_tmp`、`store`）。
- claude_raw 的傳遞依賴：`agents.base` → `private_tmp`。

## 4. 給實作的介面契約

- **C-1 搬移與殘留**（含 import 清理）：claude_raw.py 有 §2.2 表中的 11 個符號（`FORMAT`、`_AUX_CHUNK`、`_read_chunks`、`_is_utf8`、`_iter_aux`、`_aux_files`、`_pack_raw`、`_RAW_HEAD`、`_AUX_SEP`、`PackedRaw`、`_unpack_raw`）。新測試 `tests/unit/test_claude_raw_deps.py`（第一行 `import _guard`）用 ast 斷言 claude.py 頂層沒有這 11 個名字的 def／class／Assign／AnnAssign，也沒有 `import base64`／`import codecs`／`import io`。
- **C-2 逐字搬移（按行範圍，審查者可重跑；不需要任何替換）**：
  ```sh
  git show 9584a9d:src/agora/agents/claude.py | sed -n '38p;267,354p;428,441p;459,514p' > /tmp/342-old.txt
  sed -n '/^# --- moved verbatim (#342) ---$/,$p' src/agora/agents/claude_raw.py | sed 1d > /tmp/342-new.txt
  diff -B /tmp/342-old.txt /tmp/342-new.txt && echo VERBATIM-OK
  ```
  實作者把輸出貼進 PR，審查者和 PM 在 PR 上自己重跑。`diff -B` 只容忍空行差異。
- **C-3 依賴方向（靜態）**：同一個測試檔，用 ast 掃 claude_raw.py 的所有節點（`ast.walk`），斷言：
  - (i) 所有 `Import`／`ImportFrom` 裡，`agora` 開頭的只允許 `agora.agents.base`；
  - (ii) 沒有相對匯入（`level > 0`）；
  - (iii) 沒有呼叫 `import_module`／`__import__`。
- **C-4 依賴方向（執行期）**：在子行程裡跑 `import sys, agora.agents.claude_raw`，斷言 `agora.agents.claude`、`agora.store` 都不在 `sys.modules`。
- **C-5 重新匯出**：§2.3 的 8 個名字，`getattr(claude, n) is getattr(claude_raw, n)`；其中的函式和類別（`PackedRaw`、`_aux_files`、`_iter_aux`、`_pack_raw`、`_unpack_raw`）另外斷言 `__module__ == "agora.agents.claude_raw"`。
- **C-6 不重新匯出**：`not hasattr(claude, n)`，n 是 `_AUX_CHUNK`、`_read_chunks`、`_is_utf8`。
- **C-7 接縫仍有效（正向斷言）**：test_mem_golden.py:441 的 `setattr(claude, "_unpack_raw")` 不改，所在測試保持綠。但那支期望呼叫 0 次，接縫失效也照樣會過，所以**另外**在 test_claude_raw_deps.py 加兩支正向測試：
  - (a) `monkeypatch.setattr(claude, "_unpack_raw", spy)`（spy 計數後呼叫真的那個），再呼叫 `claude.ADAPTER.turns(claude_raw._pack_raw([], {}))`（ClaudeAgent.turns 在 :1075 用裸名 `_unpack_raw`），斷言 spy 被呼叫 1 次；
  - (b) `monkeypatch.setattr(claude, "_pack_raw", spy)`，再呼叫 `claude.ADAPTER.native([])`（:1081），斷言 spy 被呼叫 1 次。

  這兩支證明 claude.py 是透過自己命名空間的名字呼叫的。
- **C-8 行為不變**：以下都綠、不改：
  - golden `test_the_new_export_is_head_bit_for_bit`（tests/unit/test_agent_claude_stream.py）；
  - #338 的兩支互比測試（同檔）；
  - 全 unit。

  測試的改動只限 §5 決定 1 列的那幾行，加上新增的 test_claude_raw_deps.py；斷言和期望值都不改。
- **C-9 文件**：本 note、overview、decisions 和程式放在同一個 PR（架構師先 commit 在這個 branch 上）；merge 前由架構師把 overview／decisions 改成實作後的實際狀態（同 #341）。

**改壞要紅**：
- 在 claude_raw 任一函式內加 `from agora.agents import claude`（或 `from . import claude`）→ C-3 紅。
- claude.py 留著舊的 `def _pack_raw`，import 寫在它後面 → C-1 紅（C-5 的 `is` 抓不到）。
- claude.py 也重新匯出 `_is_utf8` → C-6 紅。
- claude.py 改成 `claude_raw._unpack_raw(...)` 呼叫 → C-7 (a) 紅。
- claude_raw 改了任何一個字元（例如 `_RAW_HEAD` 的組法、`_AUX_CHUNK` 的值）→ C-2 的 diff 紅；改到位元組的話，golden 和互比也紅。

## 5. 要 PM 決定的事

1. **放寬「既有測試不改」到下面這幾行**（只改替換和讀取的對象，不改斷言）：tests/unit/test_store_raw_copy.py
   - :442、:464、:485、:536：`real = claude._is_utf8` → `real = claude_raw._is_utf8`；
   - :450、:471、:492、:509、:543：`monkeypatch.setattr(claude, "_is_utf8", ...)` → `monkeypatch.setattr(claude_raw, "_is_utf8", ...)`；
   - 檔頭加一行 `from agora.agents import claude_raw`。

   **建議放寬**。不放寬的退路：把 aux 串流（`_AUX_CHUNK`、`_read_chunks`、`_is_utf8`、`_iter_aux`）和 `_aux_files` 都留在 claude.py，claude_raw 只剩封包本體（`FORMAT`、`_pack_raw`、`_unpack_raw`、`_RAW_HEAD`、`_AUX_SEP`、`PackedRaw`）。這樣測試完全不改，但 aux 的編碼（寫）和解碼（讀）會和封包分在兩個模組，格式模組不完整。

## 6. 被捨棄的方案

- **`_stream_raw` 連同 `_Facts`／`_accumulate` 一起搬**：會把整套閱讀版解析拖進格式模組（§2.1）。
- **`_stream_raw` 搬走、折疊改成注入回呼**：要改簽名和本體，不是純搬移；golden 鎖住的錯誤先後順序（例如 non-dict 行和語法錯誤誰先報）也得重新證明。真的需要時另開單。
- **claude.py 也重新匯出 `_is_utf8` 讓測試不用改**：替換它不會影響 `_iter_aux`，測試會悄悄地沒有驗到東西（D-12）。
- **順便合併兩個 writer**：#338 已經決定不合併（D-8）。

## 7. 風險與未決

- **低**：test_mem_golden.py 的 `json.loads@claude` 計數以呼叫端的 `__name__ == "agora.agents.claude"` 過濾。被計的 `json.loads` 在 `_stream_raw`（留在 claude.py），所以計數不變；搬走的 `_unpack_raw` 在那支測試裡本來就斷言為 0 次（而且是透過 claude 的接縫計數），也不受影響。推論，由 C-8 驗證。
- **低**：tests/perf/measure_continue_mem.py:315-317 的 `PROBED` 用 `("agora.agents.claude", "_pack_raw")` 包 claude 模組屬性來量測。claude.py 內部是用裸名呼叫，包住重新匯出的名字仍然量得到（同 C-7 的道理），不用改。perf 腳本不在 unit 裡，只能靠推論。
- **低**：`PackedRaw` 的 docstring 寫「they stay in the adapter」。搬到 claude_raw 仍然在 adapter 的 package 裡，說法還是對的，不改（逐字搬移）。
- **未決（記到 decisions）**：claude.py 搬完約 1230 行，仍然偏大。真正的下一刀是「Claude session 解析」（turns／標題／noise／`_Facts`）和「adapter 本體」（找檔、啟動、還原）分開，屬於另一張單。

## 8. PR 切分

一個 PR（d）。架構師的三份文件先 commit 在 branch `m1/342-claude-raw`；實作者在同一個 branch 上加 `src/agora/agents/claude_raw.py`、改 claude.py、新增 test_claude_raw_deps.py，以及 §5 決定 1 的測試行。

## 9. 審查清單（給非作者審查者；作者模式）

請逐條回「符合／不符合（附理由）」：

1. **模組切割**：§2.1 把 `_stream_raw`／`_Facts`／`_accumulate` 留在 claude.py 的理由是否成立？請自己對 claude.py 跑一次依賴分析確認。§2.2 的四段是否都屬於「格式」，有沒有漏搬或多搬（例如 `_unpack_aux`、`_iter_lines`、`EMPTY_MD5`／cursor）？
2. **介面**：§2.3 的 8 個重新匯出，是否正好等於「claude.py 留下的程式用到的」加上「測試和 src 透過 claude 讀的」？不重新匯出的 3 個，是否沒有其他讀者？請 grep `C\.`、`claude\.`、`setattr(claude`、`from agora.agents.claude import`。
3. **依賴方向**：claude_raw → base 單向、沒有循環？傳遞依賴是否如 §3 所寫？
4. **SOLID 與這個層級的檢查**：claude_raw 是否只有格式這一個職責？有沒有偷渡新的抽象？重新匯出有沒有超出需要？
5. **被捨棄的方案**（§6）：理由是否站得住？
6. **契約**：C-1～C-9 是否都能驗證？C-2 的行範圍是否正好涵蓋四段、而且沒有夾到不搬的行（請對 9584a9d 重跑 C-2 的前半段）？「改壞要紅」四條是否真的會紅？
7. **測試策略**：C-3 用白名單（只准 `agora.agents.base`）而不是黑名單，是否恰當？C-4 同時斷言 store 不被載入，是否合理？
8. **風險**：§7 對 test_mem_golden 計數的推論是否正確？還有沒有別的測試用 `__name__`、`__module__`、`sys._getframe`，或 claude 模組的屬性去辨認呼叫端？
9. **PM 決定**（§5）：放寬的行是否最小？退路的描述是否正確？

## 10. 修訂紀錄

### 第 2 輪（審查：tickets/REPORT-342-design-rev.md，Sonnet，針對 f503957：「可以」，3 條低）

| 發現 | 處理 |
|---|---|
| 低 1：C-7 效力偏弱（那支測試期望呼叫 0 次，接縫失效也照樣過） | 已補：C-7 加兩支正向測試（`turns` 走 `_unpack_raw`、`native` 走 `_pack_raw`，spy 要被呼叫 1 次）；「改壞要紅」加一條 |
| 低 2：claude.py 殘留 `base64`／`codecs`／`io` 的 import；`FORMAT` 只供測試的重新匯出 | 已補：§2.3 寫明要刪這三行 import（`Iterable` 因字串註記保留），C-1 加斷言；`FORMAT` 記成 decisions P-10 |
| 低 3：overview 的行數是約數 | 已校正成約 1230／約 180；C-9 規定 merge 前改成實際值 |
| 資訊：private_tmp 和 base 之間既有的 lazy 循環 | 不用處理：overview 已記為已知的雙向依賴，不是這張單引入的 |

