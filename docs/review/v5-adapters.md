# Review：v5 的兩個轉接器、X1～X4 的修法、還能刪掉的程式

2026-10-02，review。對象：`2827a5c`（opencode）、`34e7e5d`（claude）、`8a1206f`（PM：X1～X4），對照 `docs/review/v5.md` 第 5 節列的事項。依照指示，沒有跑整合測試、沒有碰 Drive，也沒有讀任何真實的 Session。單元測試（HEAD，工作目錄是乾淨的）：**200 passed**。

## 結論

- **(1) 轉接器**：兩邊都照 v5.md 第 5 節做了。**只剩一件事需要用真的 Claude 確認**：在互動模式下用 `--resume` 打開 native 組出來的 session、什麼都不說就離開，這時會不會多出一行被算成新內容的 user（見 1.2）。單元測試和我手邊的工具都驗證不了這件事，要放進 PM 的整合測試或 M-03 人工確認。
- **(2) X1～X4**：四個都修對了。
- **(3) 行數**：1,532 → 1,591（多了 59 行程式碼行）。增加的部分主要是**新的功能**（把 raw 轉換成目標格式、遞迴取各段、`_sync_for`、X1～X4 的檢查），而注入時代留下來的程式在 `src` 裡**已經刪乾淨了**。要讓總數降到 1,532 以下，還需要另外刪掉大約 60 行：下面列了 **15 處，合計大約省 52 行**，做完大約是 1,539 行，和 v4 只差幾行。

## (1) 兩個轉接器

### 1.1 opencode（`2827a5c`）

| v5.md 第 5 節的事項 | 實作 | 結果 |
|---|---|---|
| 時間戳要遞增 | `native_of`：每一則訊息的 `time.created = created + len(messages)`（以毫秒為單位，每則加 1），assistant 的 `time.completed` 也是同一個值 | ✅ |
| part 數要對得上 | 一行文字對應一個 text part；`_import_verified` 會比對訊息數**和** part 數，數量不對就刪掉剛匯入的 session 並且報錯 | ✅ |
| assistant 要有 `parentID` | 指向前一則訊息（實測：沒有的話 import 會拒絕整份 payload）；其他必填的欄位也補齊了 | ✅ |
| 單行的 turn（`CONVERTED_NOTE`／`NO_REPLY`） | 就是一則只有一個 part 的訊息 | ✅ |
| 第一則必須是 user、要交替出現 | 由 cli 的 `_converted_turns` 保證；`native_of` 另外會略過空的或角色不認得的 turn（防禦） | ✅ |
| `turns()` 不能收工具結果與 thinking | `_lines_of`：只收 text 與工具的一行摘要；`synthetic` 和靜默型態的 part 都略過 | ✅ |
| 注入時代留下的程式 | `INJECT*`／`start_injected`／`_injected_payload` 都刪掉了；`_session_info` 的 `directory` 改成佔位字串（import 會用工作目錄蓋掉） | ✅ |
| `before_count` | `start_native` 回傳的是回讀到的訊息數，collect 也用同一套計數 | ✅ |

一個多餘的步驟（不是 bug）：`native_of` 最後會呼叫 `reidentify(payload, session_id)`，接著 `start_native` 又用一個**新的** session id 再做一次 `reidentify`，所以第一次是白做的（見 (3) 的 Q2）。

### 1.2 claude（`34e7e5d`）

| v5.md 第 5 節的事項 | 實作 | 結果 |
|---|---|---|
| `parentUuid` 串成一條鏈 | `_native_lines`：每一個 turn 一行，每一行有新的 `uuid`，`parentUuid` 指向前一行，第一行是 `None` | ✅ |
| 時間戳遞增 | 從現在開始，每行加 1 秒 | ✅ |
| sessionId／cwd | 先寫佔位字串，由 `start_native` 的 `_rewrite_lines` 改成新的 uuid 和工作目錄（因為佔位的行都有 `cwd`，所以一定會被改到） | ✅ |
| 會被計數 | 每一行都是普通的 user／assistant text，沒有 `isMeta`，也沒有本機指令的格式，所以 `_count_messages` 會算到它；單元測試確認了 `before_count` | ✅ |
| 單行的 turn | 就是一行，content 只有一個 text block | ✅ |
| `start_injected` | 刪掉了 | ✅ |
| **resume 時多出來的那一行** | spike/claude.md 第 127 行記錄的「resume 疑似多記了一行 user」**還是沒有被確認**。如果互動模式的 `claude --resume` 在使用者還沒輸入時，就寫入一行**不是雜訊**的 user，那麼打開之後直接 `/exit`，collect 會把它當成有新內容，存出一個空的 continue Session | ⚠️ **待確認**：請在 PM 的整合測試或 M-03 人工確認裡加一步：「continue 打開之後什麼都不說就 `/exit` → 應該印出『這次沒有新內容』」。如果確實多了一行，看它是哪一種行（例如 `isMeta`、caveat、hook 的輸出），再把它加進 `_is_noise` |
| assistant 行的欄位 | native 的 assistant 行只有 `role` 和 `content`，沒有真正的 Claude 紀錄裡會有的 `id`、`model`、`stop_reason`、`usage` | ⚠️ 待確認（Low）：`--resume` 能不能接受這種最精簡的 assistant 行，只有真的 Claude 能回答。PM 的整合測試裡，跨 agent 的那一步（opencode → claude）會碰到這個情況；如果可以接受，就在 spike/claude.md 記一句 |

## (2) X1～X4 的修法（`8a1206f`）— ✅ 都正確

| # | 修法 | 確認的內容 |
|---|---|---|
| X1 | 某一段在濾掉 `[skip]`、做完 `merge_turns` 之後是空的，就直接 `continue`，連標示也不加 | 不會再出現兩個來源標示疊在一起 |
| X2 | 同上；而且如果**所有**段落都是空的（`turns` 只剩總說明），就丟出 InputError「這些來源裡沒有可以接續的對話內容」 | 最後一段如果是空的，就不會加標示，所以整串的結尾一定是 assistant（真正的回覆，或者 `NO_REPLY`） |
| X3 | 分成 `path`（目前這條遞迴路徑）和 `done`（已經取過的 id）。**先**檢查 `path`（循環 → InputError，並且把整條路徑印出來），**再**檢查 `done`（菱形 → 略過） | 順序是對的：A→M→A 的情況下，A 已經在 `path` 裡，所以會報錯，不會被當成「已經取過」而略過 |
| X4 | `cmd_merge` 遇到重複的 ULID（不論有沒有加 `agora:` 前綴）就丟出 InputError | 有新的測試 |

## (3) 行數：為什麼沒有變少，還能刪哪裡

各檔的程式碼行（不含空行、註解、docstring），用和 design 第 8 節相同的算法：

| 檔案 | v4（`5b748be`） | 現在 | 差 |
|---|---|---|---|
| cli.py | 382 | 418 | **+36** |
| claude.py | 275 | 289 | +14 |
| base.py | 48 | 53 | +5 |
| opencode.py | 257 | 261 | +4 |
| store.py | 390 | 390 | 0 |
| header.py | 180 | 180 | 0 |
| **合計** | **1,532** | **1,591** | **+59** |

**為什麼沒有變少**：注入時代的程式本來就不多（claude 的 `start_injected` 大約 6 行、opencode 的注入 payload 大約 20 行、cli 的 `reading/` 暫存檔大約 6 行），這些都已經刪掉了，`src` 裡也找不到任何 `inject`／`reading/` 的殘留。但 v5 同時加進了**真正的新功能**：兩個轉接器的 `native()`（opencode 大約 30 行，因為 import 的 schema 要求很多必填欄位；claude 大約 15 行）、cli 的 `_raw_segments` 加上 `_converted_turns`（大約 35 行，包含 W1～W6、X1～X4 的邊界處理），以及 `_sync_for`（`0ac0a86`，大約 6 行）。所以 v5 帶來的是**行為一致**（不論哪一種 continue，打開時都看得到前文），而不是更短的程式。這點要先跟使用者說清楚。

### 還能刪掉或合併的地方

**v5 相關（cli 與兩個轉接器）**

| # | 位置 | 建議 | 約省 | 風險 |
|---|---|---|---|---|
| Q1 | cli.py `cmd_continue` 的 `workdir`／`fallback` | `src.get("dir") and Path(src["dir"]).is_dir()` 被算了兩次。先算一次 `src_dir = …`，兩個地方共用 | 2 | 無 |
| Q2 | opencode.py `native_of` 最後的 `reidentify(...)`、`session_id` 參數，以及 `native()` 裡的 `_session_id()` | `start_native` 本來就會用新的 id 再做一次 `reidentify`，所以 `native_of` 直接回傳用佔位 id 的 payload 就好（`info.id` 也用佔位字串） | 3 | 低：`tests/unit/test_agent_opencode.py` 裡直接呼叫 `turns_of`／`native_of` 的測試，要改成透過 `ADAPTER.native()` 再 `start_native` 來驗證 id 的形狀 |
| Q3 | opencode.py 模組層的 `turns_of`／`native_of`，加上類別裡只是轉呼叫的 `turns()`／`native()` | 把邏輯直接寫進方法裡，刪掉兩個轉呼叫用的外殼 | 4 | 低：測試裡有直接用到 `turns_of`，要改成 `ADAPTER.turns(...)` |
| Q4 | opencode.py `_session_info(session_id, title, created)` | `title` 永遠是 `NATIVE_TITLE`，這個參數可以拿掉 | 1 | 無 |
| Q5 | opencode.py `native_of` 開頭的防禦檢查（3 行：濾掉空行、略過不認得的角色） | cli 的 `_converted_turns` 已經保證「第一則是 user、交替出現、沒有空的 turn、沒有 `[skip]` 行」。不過留著防禦也合理；要刪的話，cli 那邊要有測試把這個契約鎖住（目前已經有 `test_converted_continue_marks_sources_and_keeps_boundaries`） | 3 | 低 |
| Q6 | base.py 的 `reading(agent, raw)` | 只是 `format_reading(agent.turns(raw))` 的一行包裝，cli 裡用了 2 次；可以直接寫成 `format_reading(agent.turns(raw))` | 2 | 低：測試裡有 `reading(` 的地方（test_agent_*、test_opencode_real）要跟著改 |
| Q7 | cli.py 的 pending record 裡有 `"title"`，`"parent_headers"[0]` 裡也有 `"title"` | `_finish` 改成只從 `parent_headers[0]["title"]` 取，刪掉 `"title"` 這個 key（舊的 pending 也要能讀：`record.get("title") or …` 保留一行就好） | 1 | 低 |
| | **小計** | | **16** | |

**v4 的精簡清單裡還沒做、現在也算在程式碼行裡的項目**（`docs/review/simplify-v4.md` 的 B 節。import 和 decorator 都算程式碼行，所以這些仍然有效）

| # | 位置 | 建議 | 約省 | 風險 |
|---|---|---|---|---|
| B4 | claude.py 的 `from agora.agents.base import (…)`，8 行 | 寫成一行，和 opencode.py 一樣 | **7** | 無 |
| B5 | claude.py 的 `_session_dir`（5 行）、`_last_model`（8 行） | 改成和 `_exported` 裡 `created`／`version` 一樣的 `next(…)` 運算式，放進 `_exported` | **9** | 低（測試是透過 `export()` 的欄位驗證的，沒有直接呼叫這兩個函式） |
| B1 | header.py 的 `@dataclass class Ref` 加上 4 個欄位（6 行），以及 `unquote` 的 import | `src` 裡只用 `parse_ref` 來**驗證**，從來沒有用到它的回傳值。改成 `check_ref(text) -> None` | **7** | 低：`tests/unit/test_header.py` 的 `Ref(...)` 斷言要改成只驗證「會不會丟出 HeaderError」 |
| B6 | store.py `Paths` 的三個 `@property`（9 行）；`list_sessions`／`list_one` 各自取 md5；`rebuild_from_mirror` 和 `remember` 做的事重複 | property 改成一行的寫法（3 行）；取 md5 抽成 `_md5(entry)`；兩者共用 `_put_file(index, md)` | **10** | 無 |
| B3 | claude.py 的 `claude_home()`，只有 `config_dir()` 在用 | 把它併進 `config_dir()`（`config_dir` 這個名字要保留，因為測試有用到） | **3** | 無 |
| | **小計** | | **36** | |

**合計：大約省 52 行 → 1,591 → 大約 1,539 行**，比 v4 的 1,532 多大約 7 行，而這 7 行就是 v5 新增的功能淨增加的部分。

**不建議刪的**：`_converted_turns` 裡的 `NO_REPLY`、段落標示和 `CONVERTED_NOTE`（W1／W2）；`_raw_segments` 的 `path`／`done`（X3）；opencode `native_of` 裡那些 import schema 規定必填的欄位（每少一個，import 就會拒絕整份 payload，這是實測過的）；`_import_verified`；以及 v4 精簡清單裡「不碰的東西」那一節列的所有安全機制。

## 這次跑過的指令

| 指令 | 結果（只記形狀） |
|---|---|
| `git show 2827a5c 34e7e5d 8a1206f`，以及讀 `opencode.py`／`claude.py`／`cli.py` 的相關段落 | 見 (1)(2) |
| `grep` `src` 裡的 `inject`／`INJECT`／`start_injected`／`reading` | 只剩 `base.reading` 和閱讀版本身，沒有注入時代的殘留 |
| 用 `ast` 計算 HEAD 和 `5b748be` 各檔的程式碼行 | 見 (3) 的表 |
| `grep -rlF` 檢查 `turns_of`、`native_of`、`reading(`、`Ref(`、`claude_home`、`_session_dir`、`_last_model` 在測試裡有沒有被用到 | 決定各條的風險 |
| `nohup .venv/bin/python -m pytest -q tests/unit` | 200 passed |

沒有跑整合測試，沒有碰 Drive，沒有叫真的 agent，也沒有讀任何真實的 Session。

---

## 精簡之後的行為確認（`9ba343b`、`d0346e3`）

依照指示，沒有跑整合測試，也沒有碰 Drive。**結論：行為沒有變。** 單元測試 **201 passed**；程式碼行 **1,591 → 1,553**（cli 418、store 387、claude 270、opencode 256、header 169、base 53）。

我把精簡**之前**（`6224628`）的 `opencode.py`／`claude.py` 存到 scratchpad，和現在的版本一起載入同一個 Python，用相同的輸入比對輸出（比完之後 scratchpad 的檔案已經刪掉）：

| 項目 | 結果 |
|---|---|
| opencode `turns()`（`oc-basic.json`） | 新舊**完全相同** |
| opencode 的 `native()` 結果經過 `reidentify` 之後（用同一個 session id、固定 `time.time()`，輸入是一組 4 個 turn，包含總說明、來源標示、`[tool]` 行和 `NO_REPLY`） | 每一則訊息的角色、id、`parentID`，以及每一個 part 的 id 和文字，新舊**完全相同**；`info` 除了 `id` 以外也一樣（title 都是 `NATIVE_TITLE`） |
| opencode `native()` 的**佔位 id** | `ses_agora_pending0`、`msg_agora<n>`、`prt_agora<n>-<k>`，都**保留了** `ses`／`msg`／`prt` 的前綴（不然 import 會直接拒絕，這是 trap 3），而且在同一份 payload 裡不會重複。`start_native` 會用 `reidentify` 全部換成正式的 id：`parentID` 透過 `message_map` 對應，part 透過 `part_map` 對應，`sessionID` 統一改成新的 session id。`cli` 裡只有 `start_native` 會用到 `native()` 的輸出 |
| claude `_exported`（`cl-basic.jsonl`） | `dir`／`model`／`created_at`／`agent_version`／`message_count`／`title`，新舊**完全相同**。`next(…)` 的寫法和原本的 `_session_dir`（第一個字串型態的 `cwd`）、`_last_model`（從後面往前找，第一個帶有非空 `model` 的 assistant）語意一樣 |
| claude `config_dir()` | 優先順序不變（`AGORA_CLAUDE_HOME/.claude` > `CLAUDE_CONFIG_DIR` > `~/.claude`） |
| header `check_ref` | 切割的規則和錯誤的情況都和原本的 `parse_ref` 相同，只是不再回傳 `Ref`；`src` 和測試裡已經沒有任何地方用到 `parse_ref` |
| store `Paths` 的 property 改成 lambda | 寫成 `mirror = property(lambda s: …)`，沒有型別註記，所以不會變成 dataclass 的欄位，`Paths(config=…, cache=…, state=…)` 照樣能用 |
| store `_md5`、`_put_file` | 純粹是抽出來共用，`remember`／`list_sessions`／`list_one` 的行為不變 |

**`rebuild_from_mirror` 現在連 `put` 也包進了 `try`**：`except` 只接 `(h.HeaderError, OSError, UnicodeDecodeError)`，所以實際上新包進去的，只有 `md5_file(md)` 和 `put`。

- `md5_file` 丟出的 `OSError`（例如檔案在讀取途中被刪掉）：以前會讓整個 `search --no-sync` 失敗，現在會跳過那一份。這是**改善**。
- `put` 寫索引時丟出的 `sqlite3.Error`：它**不是** `OSError` 的子類別，所以**不會被吞掉**，照樣會傳出去，和以前一樣。也就是說，索引寫入失敗不會被悄悄略過，不會出現「重建了，但少了幾筆」卻沒有任何訊息的情況。

剩下的 Q5～Q7（約 6 行）沒有做，都不影響正確性。

跑過的指令：`git show 9ba343b d0346e3 -- src`；把 `6224628` 的兩個轉接器存到 scratchpad，用 importlib 和現在的版本一起載入並比對（之後已經刪掉）；`git grep parse_ref|turns_of|native_of`；`.venv/bin/python -m pytest -q tests/unit`（201 passed）；用 `ast` 計算程式碼行（1,553）。
