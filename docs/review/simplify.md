# 精簡建議：src/agora（已 commit 的部分）

2026-10-02，review。只給建議，沒有改程式。對象是 `78ea7a0` 時已經 commit 的 `header.py`、`store.py`、`cli.py`、`agents/base.py`，以及 `agents/claude.py`（以 `9d1d549` 為準；工作目錄裡 impl2 正在改它，所以 claude 這幾條請 impl2 自己判斷要不要併進去）。

## 現在的行數

| 檔案 | 已 commit | 工作目錄（含還沒 commit 的） |
|---|---|---|
| header.py | 181 | 181 |
| store.py | 474 | 474 |
| cli.py | 390 | 390 |
| agents/base.py | 89 | 89 |
| agents/claude.py | 334 | 390（impl2 修改中） |
| agents/opencode.py | — | 361（impl1，還沒 commit） |
| **合計** | 1,468 | **約 1,887** |

**結論：照現在的寫法，2,000 行做得到，但只剩約 110 行的餘裕**，而 CL1～CL14 和 opencode 的修正還會再加一些。下面的建議合計大約省 **75～85 行**，而且多數會讓程式更好讀。風險：「無」表示行為完全不變；「低」表示行為不變，但要改測試或呼叫端；「中」表示會碰到資料格式或介面。

## 建議清單（依「省得多、風險低」排序）

| # | 位置 | 建議 | 約省 | 風險 |
|---|---|---|---|---|
| P1 | claude.py 的 `_user_lines`／`_assistant_lines` | 兩個函式的 block 迴圈幾乎一模一樣（text、silent、skip），差別只在 assistant 多了 `tool_use`。合併成 `_block_lines(content, *, tools: bool)`，再加上 `message` 是 str 的特例 | 12 | 低（閱讀版的測試都在） |
| P2 | claude.py 的 `_user_text`（給 title 用） | 它重做了一次 `_user_lines` 的工作（只取 text）。改成 `title = next(…_user_lines(o)…)` 的第一行，過濾掉 `[skip …]` | 10 | 低 |
| P3 | claude.py 的 `_agent_version` | 刪掉 subprocess 加 regex，改成取 jsonl 最後一行的 `version` 欄位（CL11）。這樣也少一次叫 CLI，CL3／E1 的 shebang 問題對 export 也就不會發生 | 8 | 低（`test_export_broken_cli_gives_no_version` 要改寫） |
| P4 | header.py 的 `Ref.__str__` 與 `quote` 的 import | `src` 裡沒有任何地方用到，只有一個 round-trip 測試在用 | 9 | 無（連同那個測試一起刪） |
| P5 | header.py 的 `RESERVED` | 只有定義，`src` 裡沒有任何地方讀它。留著當文件也可以，但 design 3.1 已經有這張表了 | 4 | 無 |
| P6 | store.py 的 `_index_outbox` 與 `remember` | 兩個函式做的事一樣（複製到鏡像，再 `put`）。`_index_outbox` 改成對每一筆呼叫 `remember(paths, folder, md5, index=index)`；`remember` 加一個可選的 `index` 參數，避免每次都新開一個 Index | 6 | 低 |
| P7 | cli.py 的 `_save`，以及 `"outbox"` 這個特殊的 md5 | 現在是 push 之前 `remember(…, "outbox")`，成功之後再 `put` 一次真正的 md5。「未上傳」已經改成用 `outbox_ulids` 判斷（C11），所以 `"outbox"` 這個值沒有人在讀。改成 push 之前用真正的 md5 `remember` 一次就好；`_index_outbox` 也用真正的 md5 | 4 | 低（sync 的 `known.get(ulid) == md5` 會因此直接跳過重新下載，這反而比較好） |
| P8 | store.py 的 `push_outbox` 與 `push_one` | 兩邊都讀並解析同一份 session.md，也都檢查 raw。抽出 `_read_entry(folder) -> hdr`（失敗就丟出 `HeaderError`），push_outbox 用它來決定要不要 quarantine，push_one 直接收 hdr | 4 | 低 |
| P9 | store.py 的 `outbox_ulids`／`_outbox_ulids` | 公開的版本只是轉呼叫私有的版本。合併成一個公開的 `outbox_ulids` | 3 | 無 |
| P10 | cli.py 的 `cmd_import` | 三個分支都各自設定 `hdr["source"]` 和 `title`。改成每個分支只決定 `hdr`，最後共用三行：source、title、`_emit(_save(…))` | 4 | 低 |
| P11 | store.py 的 `fetch_raw` | 抓不到 raw 時重抓 session.md 的那段，又自己寫了一次下載與 md5 檢查。改成「重抓 session.md 之後，以 `retry=False` 遞迴呼叫自己一次」 | 3 | 低 |
| P12 | claude.py 的 `start_native` 裡處理 aux 的地方 | 用 `restored` 字典重新組一次 aux，再丟給 `_unpack_aux`。改成 `_unpack_aux(aux, sidecar, rewrite=lambda text: …)`，在裡面處理 `.jsonl` | 3 | 低（順便修 CL6） |
| P13 | claude.py 的 `_reading_turns` | user 和 assistant 兩個分支的寫法一樣。改用 `{"user": _user_lines, "assistant": _assistant_lines}.get(otype)` 分派 | 4 | 無 |
| P14 | cli.py 的 `cmd_merge` | 產生 title 時又用 `index.header(…)` 把每個 parent 重查了一次。改成在迴圈裡就把 parent 的 header 留下來 | 2 | 無 |
| P15 | base.py 的 `Launch.env`，以及 cli.py 的 `env={**os.environ, **launch.env}` | 兩個 adapter（包括 impl1 還沒 commit 的 opencode.py）都沒有設定 `env`。刪掉這個欄位，`subprocess.run` 就不必傳 env | 2 | 低（如果 impl1 之後需要它，再加回來） |
| P16 | build_parser | `--header` 的 `add_argument` 重複寫了 4 次。用一個小迴圈，或者一個 `_with_header(parser)` 輔助函式 | 3 | 無 |
| P17 | store.py `Drive.folder_id` 的 `settings.get("folder_name")` | `folder_name` 和 `folder_id` 總是一起寫入，所以「有 name 沒有 id」的情況不會發生，可以直接用 `AGORA_FOLDER_NAME` 或預設的 `agora` | 1 | 無 |
| P18 | claude.py 的 `encode_project_dir` | `str(workdir) if os.path.isabs(…) else os.path.abspath(…)` 可以直接寫成 `os.path.abspath(workdir)`（對絕對路徑來說，結果一樣，只是多做一次正規化）；修 CL1 時一起改成一行 regex | 1 | 無（CL1 本身另外處理） |
| **合計** | | | **約 83** | |

## 只改善可讀性、不省行數（建議一起做）

- **R-a**：cli.py 有 14 處 `print(f"[agora] …", file=sys.stderr)`，claude.py 也有 1 處；store.py 已經有 `_warn`。把它改成公開的 `store.warn`（或放進 base.py），全部統一。行數差不多，但訊息格式只會有一個地方定義。
- **R-b**：cli.py 用的是 `store.hashlib.md5(…)`，也就是借用 store 匯入的模組。在 store 加一個 `md5_bytes(b)`，或者 cli 自己 import hashlib。
- **R-c**：cmd_continue 裡決定 `workdir` 的那一行太長了（三元運算加上 `is_dir`）。拆成兩、三行，或者抽成一個 `_workdir(args, src)`。
- **R-d**：`_finish` 只是為了呼叫 `collect`，就組了一個 `Launch(argv=[] …)`。長期來看，把 `collect` 的參數改成 `(agent_session_id, cwd, before_count)` 會比較直接。不過這會改到兩個 adapter 的介面，**等 opencode.py 進來、也穩定了之後再改**。

## 不建議刪的

- `stage` 的 `.tmp`／`.old` 換入流程，以及 `_outbox_ulids` 的補回邏輯（大約 15 行）：這是 C4／R6 的正確性修正，沒有更短又一樣安全的寫法。
- `recover_pending` 裡的鎖、`path.exists()` 檢查、quarantine 與重試的區分：每一段都對應到一個已經重現過的競態，或壞檔的情境（C1～C3、R4、R5）。
- `Index` 的 `created` 欄位：目前沒有人在讀它（search 是在 Python 裡排序的），刪掉可以省 1 行。但改 schema 時，現有的 `index.sqlite` 會在 INSERT 時失敗，得另外加 `PRAGMA user_version` 的檢查，加了反而更長。要嘛留著，要嘛乾脆讓 search 改用 SQL 的 `ORDER BY created`（這樣它就有用了）。

## 檢查方式

這次只讀了 `git show HEAD:<檔案>`，另外用 `git grep` 確認了 `RESERVED`、`Launch.env`、`"outbox"`、`folder_name`、`Ref.__str__` 在 `src` 裡被用到的地方。沒有跑程式，也沒有碰 Drive 或任何真實的 Session。
