# 精簡建議（design 第 4 版之後）：把 src 拉回 2,000 行以內

2026-10-02，review。只給建議，沒有改程式。對象是 HEAD（`0fddb0b`）的 `src/agora`，目前是 **2,197 行**：cli 490、store 534、header 271、claude 390、opencode 414、base 96，再加上兩個 `__init__.py`（各 1 行）。

## 先看行數是怎麼組成的

| 檔案 | 總行數 | 空行 | 註解 | docstring | 程式碼（大約） |
|---|---|---|---|---|---|
| cli.py | 490 | 70 | 14 | 26 | 380 |
| store.py | 534 | 87 | 15 | 48 | 384 |
| header.py | 271 | 53 | 9 | 35 | 174 |
| agents/claude.py | 390 | 71 | 4 | 45 | 270 |
| agents/opencode.py | 415 | 67 | 20 | 76 | 252 |
| agents/base.py | 96 | 26 | 2 | 21 | 47 |
| **合計** | 2,197 | 374 | 64 | 251 | **約 1,507** |

- 空行幾乎都是 PEP 8 規定的函式之間的空兩行，函式**內部**的空行全部加起來只有 20 行，所以**不建議刪空行**。
- 我用 AST 檢查過，`src` 裡**沒有**只被定義、卻沒有被用到的函式或常數，所以也沒有死碼可以刪。
- 因此，這次的刪減來源是：**docstring 和註解的冗長部分（大約 160 行）**，加上**少量的重複程式（大約 60 行）**。

**合計大約省 225 行，2,197 → 大約 1,972 行**，留下約 30 行的餘裕。

## 不碰的東西（安全機制）

下面這些全部保留，連它們旁邊說明「為什麼」的那一行註解也保留（可以縮成一行，但不要刪掉）：

S1／S2 的 stage 與 push 順序（`.tmp`／`.old` 換入、md5 驗證）；R1 的 `OSError → StoreError`；C2 的 quarantine；N2／C1／C3／R5 的 pending flock、`pass_fds`、`.json.tmp` 再 rename；N1 的 SIGINT 還原；PWD；OC1～OC3 的 `reidentify`／`_import_verified`；OC7 的 timeout；CL1 的編碼；CL14 的 aux 路徑檢查；V1 的 `upgrade`；V3 的 `_plain`；D-1 的 e2e 清理（測試，不算在 src 裡）。

## A. 註解與 docstring（風險：無，行為不變）

原則：每個模組的 docstring 只留 **3 行**（做什麼，加上「詳見 design.md §x／spike」）；函式的 docstring 只留 **1 行**（「為什麼」寫成一句話，後面加上 review 編號，例如 `(OC1)`）；多行的註解也縮成一行。詳細的推理已經寫在 design.md、`docs/spike/*.md`、`docs/review/*.md` 裡，也都在 commit 訊息裡，所以不需要在程式裡重複一次。

| # | 位置 | 現在 → 建議 | 約省 |
|---|---|---|---|
| A1 | 各模組的 docstring：cli（14 行，有一份重複 design §5 的指令清單）、store（12）、header（7）、claude（14）、opencode（22，重複 spike 的五條規則）、base（6） | 每個都只留 3 行 | **57** |
| A2 | 5 行以上的函式 docstring：store 的 `sync`／`folder_id`／`search`；header 的 `upgrade`／`validate`／`parse_header_args`；cli 的 `recover_pending`；claude 的 `encode_project_dir`／`_warn_cleared`；opencode 的 `_id`／`_remap`／`reidentify`／`_last_model`／`_injected_payload`／`_import_verified`／`collect`；base 的 `format_reading` | 留 1～2 行（安全機制的那幾個留 2 行，例如 `recover_pending`） | **67** |
| A3 | 2～3 行的函式 docstring：claude 的 `find_jsonl`／`_split_lines`／`_is_noise`／`_rewrite_lines`／`_block_lines`；opencode 的 `_salt_for`／`_run` | 1 行 | **8** |
| A4 | 分段的尺規註解 `# ----…`（cli、store、header 各 2 處，每處 3 行） | 只留標題那一行 | **12** |
| A5 | 2 行以上的註解塊：cli 349（Ctrl-C，4 行）、405；store 156（copyto）、199（換入）、527（N8）；header 171；claude 38；opencode 37、44、59、145、390、405；base 85 | 每段 1 行 | **19** |
| | **小計** | | **163** |

## B. 重複與小重構（風險：低，行為不變，有幾條要改測試）

| # | 位置 | 建議 | 約省 | 風險 |
|---|---|---|---|---|
| B1 | header.py 的 `Ref` dataclass、`parse_ref` 的回傳值、`unquote` 的 import | `src` 裡只用 `parse_ref` 來**驗證**，從來沒有用到它回傳的 `Ref`。改成 `check_ref(text) -> None`，dataclass 刪掉 | 9 | 低：`tests/unit/test_header.py` 的 `test_parse_ref_rules` 與 `test_ref_round_trips…` 要改成只驗證「會不會丟出 HeaderError」 |
| B2 | header.py 的 `user_updates` 與 cli.py 的 `_edit_in_editor` | 兩邊都各自寫了一次「擋系統欄位、`type` 必須是 `Session`、檢查 ref」，抽成 `h.check_user_fields(d, *, require_type: bool)` 共用 | 6 | 低（錯誤訊息要統一成同一套） |
| B3 | claude.py 的 `claude_home()`／`config_dir()`／`projects_dir()` | `claude_home` 只有 `config_dir` 在用。合併成一個 `config_dir()`（保留這個名字，因為測試有用到），`projects_dir` 也改成一行 | 6 | 無 |
| B4 | claude.py 的 `from agora.agents.base import (…)` | 8 行的 import 寫成 1 行（opencode.py 已經是這樣寫的） | 7 | 無 |
| B5 | claude.py 的 `_session_dir`、`_last_model` | 寫法改成和 `_exported` 裡的 `created`／`version` 一樣的 `next(…)` 運算式，放進 `_exported`，兩個函式都刪掉 | 10 | 低（`_last_model` 的單元測試是透過 `export().model` 驗證的，不受影響） |
| B6 | store.py 的 `Paths` 的三個 `@property`、`list_sessions`／`list_one` 各自取 md5、`rebuild_from_mirror` 和 `remember` 做的事重複 | property 改成一行的寫法（`mirror = property(lambda s: s.cache / "sessions")`）；取 md5 抽成 `_md5(entry)`；`rebuild_from_mirror` 和 `remember` 共用一個 `_put_file(index, md)` | 10 | 無。`outbox_count` **保留**（3 個測試檔都有用到） |
| B7 | opencode.py 的 `_injected_info`（17 行的 dict）、`_timeout`（6 行） | dict 改成每行放好幾個 key（大約 8 行）；`_timeout` 縮成 3 行 | 12 | 無（payload 的內容不變；`test_injected_payload_has_every_field…` 會繼續檢查） |
| B8 | base.py 的 `Launch.env`、`field` 的 import；cli.py 的 `**launch.env` | 沒有任何 adapter 設定過 `env`（我之前在 P15 也提過） | 2 | 低：之後真的需要時再加回來 |
| | **小計** | | **62** | |

## 合計

| | 約省 |
|---|---|
| A（註解與 docstring） | 163 |
| B（重複與小重構） | 62 |
| **合計** | **225 → 大約 1,972 行** |

建議的順序：先做 A（完全不會改到行為，只動 diff 很容易 review 的部分，做完就大約是 2,034 行），再做 B1～B8（每一條各自 commit，跑過單元測試再做下一條）。

## 替代方案（需要 PM／使用者決定）

design 第 8 節的「2,000 行」目前算的是**檔案的總行數**。如果改成只算**程式碼行**（例如用 `cloc` 的 code 欄位，不含空行、註解、docstring），現在的 `src` 大約是 1,507 行，離上限還有將近 500 行，說明文字也就不用為了行數去砍。這個決定會影響以後所有的精簡工作，所以建議在 design 第 8 節寫清楚用哪一種算法。即使不改算法，上面的 A 和 B 也都值得做，因為它們同時也讓程式更好讀。

## 檢查的方式

- 用 `ast` 統計每個檔案的空行、註解、docstring 行數，並且列出 2 行以上的 docstring 與註解塊（就是上面 A 的來源）。
- 用 `ast` 搭配正規表示式，在整個 `src` 裡數每個函式和頂層常數被引用的次數，結果沒有任何只被定義、沒被引用的項目。
- 用 `grep -rlF` 確認 `claude_home`、`Ref(`、`outbox_count`、`config_dir`、`_session_dir`、`_last_model`、`_injected_info` 在測試裡有沒有被用到（決定 B 各條的風險）。

沒有改程式，也沒有跑任何會碰到 Drive 或真的 agent 的東西。
