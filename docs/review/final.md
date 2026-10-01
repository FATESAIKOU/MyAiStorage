# PR 前最後一輪 review

2026-10-02，review。範圍是 `git log 834ca09..HEAD`（HEAD＝`639017e`），實際的新 commit 有：`3be83ee`（impl2：test_cli_more）、`cd15ad5`（PM：`-` 開頭的關鍵字、離線重建索引）、`02e289b`（PM：folder ID 依名稱分開存）、`da0fe0a`＋`639017e`（PM：acceptance.md）。PM 列出的 `153015a`、`c91f437`、`1413629`、`aaa2d4e` 都在 `834ca09` **之前**，已經在 `code-adapters.md` 的 E 節確認過，這裡不重複。

## 結論

- **還沒解決的 High：沒有。**
- **還沒解決的 Medium：3 條，都在 `docs/acceptance.md`**（F1～F3），程式本身沒有。這三條都是「照著做時，可能碰到正式資料，或者驗收結果會被誤判」，**建議在使用者早上跑驗收之前改好**。不需要擋 PR，可以放在同一個 PR 裡修。
- **PR：可以開**，前提是下面兩件事：
  1. 先修好 F1～F3（大約 10 行文件），或者在 PR 描述裡明確寫「驗收前先修 acceptance.md」；
  2. **工作目錄裡 impl1 還沒 commit 的變更**（`src/agora/agents/opencode.py` 改了 241 行、`docs/spike/opencode.md`、`test_agent_opencode.py`、`test_opencode_real.py`，以及沒有追蹤的 `opencode_noninteractive.py`、`test_e2e_opencode.py`、`test_install.py`）**這次沒有 review**。這些要嘛等它 commit、review 之後再開 PR，要嘛確定 PR 只包含 `639017e` 以前的內容。另外要注意：HEAD 的 `src` 是 **1,994 行**，離 2,000 只差 6 行。impl1 正在改的 opencode.py 應該就是在做精簡（Q1～Q9），請確認它 commit 之後還在 2,000 行以內。

## 驗證

| 指令 | 結果 |
|---|---|
| `git archive HEAD \| tar -x -C <scratchpad>/head`，再用 `PYTHONPATH=<scratchpad>/head/src` 跑 `pytest -q -rfx tests/unit`（在背景跑） | **156 passed**，0 failed（上一輪在工作目錄裡失敗的 4 個 test_cli_more，`3be83ee`／`cd15ad5` commit 之後都通過了）。這樣跑是為了只測 HEAD，不受工作目錄裡 impl1 未 commit 的變更影響；跑完之後 scratchpad 的副本已經刪掉 |
| `cat <HEAD>/src/agora/*.py <HEAD>/src/agora/agents/*.py \| wc -l` | 1,994 |
| `grep` 檢查 `tests/unit/test_cli_more.py` 有沒有用到真的 HOME、`~/.claude`、`share/opencode`，或直接叫 opencode／claude／rclone | 沒有。`.claude` 都在 `tmp_path / "chome"` 底下，agent 都是 fake，conftest 也預設把 agent 指令指向 `/nonexistent` |
| 讀 `docs/acceptance.md` 全文 | 見 F1～F3 |

沒有執行 e2e、整合測試或驗收清單，沒有碰 Drive，沒有叫真的 opencode／claude，也沒有讀任何真實的 Session。

## 逐個 commit 的 review

### `02e289b`：folder ID 依名稱分開存 — ✅ 正確

- 舊版的 `config.json` 只有一個 `folder_id`。在同一個 `AGORA_CONFIG` 底下，用 `AGORA_FOLDER_NAME=agora-test` 跑過一次之後，**下一次一般的執行就會寫進 agora-test**；反過來也會發生。改成 `folders[name]` 之後，兩邊就分開了。這個修正很重要，因為 acceptance.md 用的是**使用者真正的** `~/.config/agora/`。
- **Low（G1）**：舊格式留下來的 `folder_id`／`folder_name` 這兩個 key 不會被讀取，也不會被清掉。它們無害，但如果有人看 `config.json`，可能會被誤導。可以在 `folder_id()` 裡順手 `self.settings.pop("folder_id", None)`、`pop("folder_name", None)`。

### `cd15ad5`：`-` 開頭的關鍵字、離線重建索引 — ✅ 正確，有兩個 Low

- `parse_known_args` 之後，只有在 `search`、還沒有 keyword、多出來的參數剛好只有 1 個時，才把它當成關鍵字；其他情況照樣 `parser.error`（exit 2）。U-SRC-07 的 `-x` 也從 xfail 變成通過了。
- **Low（G2）**：`agora search session --nosync` 這種把旗標打錯、又沒給關鍵字的情況，會被靜默地當成關鍵字 `--nosync` 去搜尋，結果是 0 筆，看不出是打錯了。建議只接受「`-` 開頭、但不是 `--` 開頭」的那一個多餘參數；或者在搜尋前印一行 `[agora] 把 '--nosync' 當成關鍵字`。
- **Low（G3）**：`rebuild_from_mirror` 會把鏡像裡**所有**的 session.md 重新放進索引。但是 sync 是先把 session.md 下載到鏡像，**之後**才檢查 raw 是否完整，所以「還沒寫完」的 Session（S1）也會留在鏡像裡。刪掉 index.sqlite 之後跑 `search --no-sync`，這些 Session 就會被搜得到；不過要 continue 時，fetch_raw 的 md5 檢查會擋下來，所以不會讀到半份，只是會看到一個錯誤。建議 sync 遇到「還沒寫完」時，也把鏡像裡那一份刪掉（加一行 `local.unlink(missing_ok=True)`）。

### `3be83ee`：test_cli_more（39 個）— ✅

涵蓋了 code-pm 第 (2) 節列出的缺口（U-CON-08／10／11b／13／16、U-IMP-08b／09／10、U-MRG-02b／03、U-SHW、U-SRC-07／09／10／12／13 等）。所有 agent 都是 fake 或隔離的 `AGORA_CLAUDE_HOME`，不會碰到真實資料。

### `da0fe0a`＋`639017e`：`docs/acceptance.md`

**照著做會不會碰到正式的 `agora/`，或使用者平常的快取？** 照著**正確地**做，不會：`AGORA_FOLDER_NAME=agora-test`、`AGORA_CACHE_DIR`／`AGORA_STATE_DIR` 都指到 `/tmp/agora-acc`；`02e289b` 之後，`config.json` 也會分開記住 agora-test 的 ID；opencode 是在有 commit 的 `/tmp/agora-acc/proj` 裡執行；Claude 的檔案則落在 `projects/-private-tmp-agora-acc-proj/`。**但是有三個地方，只要手滑一次，就會碰到正式資料**：

| # | 嚴重度 | 位置 | 問題 | 建議 |
|---|---|---|---|---|
| F1 | **Medium** | 清理一節的 `rm`／`rm -rf`／`rclone purge` | 所有指令都是「變數加上手動代換的佔位字」。`ENC` 如果是空的（例如 python3 叫不起來，這台機器的 asdf shim 曾經發生過），或者 `<uuid>` 被留空，`rm -rf ~/.claude/projects/$ENC/<uuid>` 就可能變成 `rm -rf ~/.claude/projects/`，**刪掉使用者所有的 Claude session**。同樣地，`U` 如果是空的，`purge "gdrive:sessions/$U"` 就會變成 purge 整個 `agora-test/sessions/`（連其他人的測試資料也刪掉） | 一律用 `${VAR:?}`：`rm -- "$HOME/.claude/projects/${ENC:?}/${U:?}.jsonl"`、`rm -rf -- "$HOME/.claude/projects/${ENC:?}/${U:?}"`、`purge "gdrive:sessions/${U:?}"`、`--drive-root-folder-id "${FID:?}"`。uuid 和 ULID 也改用變數迴圈（`for U in …; do …; done`），不要讓使用者手動代換進路徑。另外，ENC 改成直接寫死 `-private-tmp-agora-acc-proj`，不必依賴 python3 |
| F2 | **Medium** | 開頭的 `export` | 安全完全依賴同一個終端機裡的三個 `export`。只要換了終端機、開了新的分頁，或者某一步之前忘了設定，`agora import`／`continue-session` 就會寫進**正式的 `agora/`**，以及**平常的 `~/.cache/agora`、`~/.local/state/agora`**。而清理一節只會去 agora-test 裡刪，所以寫錯的那幾筆會留在正式資料裡 | 把三個 `export` 寫成一個檔（例如 `/tmp/agora-acc/env.sh`），每一節都從 `source /tmp/agora-acc/env.sh` 開始；或者在每一節前面加一行檢查 `[ "$AGORA_FOLDER_NAME" = agora-test ] \|\| { echo '先 source env.sh'; false; }`。另外建議（程式端，小改）：agora 每次寫入 Drive 時，都在 stderr 印一行 `[agora] Drive 資料夾：<名稱>`，讓使用者一眼就能看出寫到哪裡 |
| F3 | **Medium** | 第 4 節「按一次 Ctrl-C」 | 預期結果寫的是「agent 停下來，agora 沒死，仍然印出新的 id（D）」。但是在 Claude 裡按一次 Ctrl-C，只會中斷**這一輪的回覆**，Claude 本身不會結束，所以使用者還得自己離開，文件卻沒有寫。而且，如果中斷發生在第一則 assistant 行寫出來之前，jsonl 裡只會多一行 `[Request interrupted by user]`（user 行），計數會等於 `before_count=2`，collect 就會回傳 None，印出「這次沒有新內容」，**D 根本不會出現**。這樣驗收會被誤判成失敗（`[Request interrupted…]` 這一行是依 Claude 的 jsonl 慣例推論的，還沒有實測） | 步驟改成：「等它開始回覆後按一次 Ctrl-C → 確認回覆停了、agora 還在 → **再說一句自編的話** → `/exit`」。預期結果：印出 D，`agora show <D>` 看得到 Ctrl-C 之後說的那句話。另外補一小節 opencode TUI 的 Ctrl-C（M-03 原本就有，這份清單漏掉了），以及「連按兩次 Ctrl-C 離開 claude」這個情況，預期是 agora 照樣存檔 |
| F4 | Low | 第 7 節 | 「包含上面記下的 id」：E 是 `/clear` 之後另外 import 的 session，它的 title 不一定含有「表格」，可能搜不到 | 改成「包含 A、B、C、D」 |
| F5 | Low | 清理一節 | 如果第 4 節真的沒有存出 D（F3），它的 Claude uuid 就不會出現在任何 `agora show` 裡，那個 jsonl 會留在真實的 `~/.claude/projects/-private-tmp-agora-acc-proj/` | 清理一節加一句：「如果有一步印出『這次沒有新內容』，那個 uuid 要到 `ls ~/.claude/projects/-private-tmp-agora-acc-proj/` 裡找（這個資料夾只有驗收建的檔，可以列出）」 |

## 先前留下、到現在仍然開著的 Low（不擋 PR）

- `code-adapters.md` E 節的 E-1（連續 `/clear`）、E-2（整合測試沒有驗證 agent 讀到了注入的內容）、E-3（part 序號的寬度）。
- `simplify.md` 與 `code-adapters.md` 的 Q1～Q9（精簡）：impl1 目前在工作目錄裡處理，還沒 commit。
- test-plan 的 M-03（opencode TUI、Ctrl-C）仍然是人工確認，也就是 acceptance.md 第 1、2、4 節要做的事；F3 修好之後才完整。

---

## 最後一輪補充：`9f88769`（impl1：opencode e2e＋Q1～Q9）與 `e1439ab`（PM：F1～F5、G1～G3）

對象：`9f88769` 的 `src/agora/agents/opencode.py`、`tests/integration/test_e2e_opencode.py`、`tests/fakes/opencode_noninteractive.py`，以及 `e1439ab` 的 `docs/acceptance.md`、`cli.py`、`store.py`。工作目錄裡 impl1 還有**沒 commit 的變更**（`opencode_noninteractive.py`、`test_e2e_opencode.py`、`test_opencode_real.py`），這次只看已經 commit 的版本。

### 結論

- **還沒解決的 High：1 條（P1）**。`test_e2e_opencode.py` 的 teardown 又用了 `store.Index(paths).known()`，也就是 D-1 的錯誤**又出現了**：它會 purge 共用的 `agora-test/sessions/` 裡**所有**的 Session。工作目錄裡還沒 commit 的版本也一樣。
- **還沒解決的 Medium：2 條（P2、P3）**，都在測試裡，`src` 沒有。
- **PR：修好 P1 之後就可以開**（只要改 1 行）。P2、P3 可以放進同一個 PR，也可以當成後續工作，但在下一次跑 e2e 之前要改好。`src` 本身：**沒有還沒解決的 High 或 Medium**。

### opencode.py 精簡之後，行為有沒有變 — ✅ 沒有變

| 項目 | 確認的方法 | 結果 |
|---|---|---|
| 三種 id 重編 | 把舊版（`d3ae9cb`）和新版（HEAD）的 `reidentify` 載入同一個 Python，用 `oc-basic.json` 加上一個人工的 `info.revert`，各跑一次 | **輸出完全相同**（`a == b`）。`_id(prefix, *positions, …)` 產生的 msg／prt 格式和寬度都沒有變；`revert` 改成用合併後的對應表 `{**message_map, **part_map}`，因為前綴不同（`msg_`／`prt_`），不會撞到 |
| 注入 | 同樣的做法，固定 `time.time()` 之後，比對 `_injected_payload` | **輸出完全相同**。改成「先用暫時的 id，再交給 `reidentify`」之後，id 的規則就只剩一個地方 |
| 回讀驗證 | 讀程式 | `_import_verified` 沒有變，訊息數和 part 數都比對；`_delete` 和 `_discard` 合併了，rc 也照樣會檢查 |
| timeout | 讀程式 | `_run` 多了 `stdout=` 參數，export 也改走 `_run`，所以同樣有 timeout；而且仍然是 stdout 寫檔、stderr 用 PIPE，沒有 `2>&1` |
| 其他 | | `_cli_version` 刪掉了，沒有 `info.version` 時，`agent_version` 就是 None（cli 允許這種情況）。`_MESSAGE_REFERENCES`／`_PART_REFERENCES` 現在有被用到了 |
| 單元測試 | `git archive HEAD` 取出到 scratchpad，再跑 `pytest -q tests/unit` | **156 passed** |
| 行數 | HEAD 的 `src` | **1,925 行**（原本是 1,994 行） |

只有一個 Low：`_id` 裡的 `assert` 在 `python -O` 下不會執行。實際上不會溢位，所以可以接受。

### test_e2e_opencode.py 的清理是否只動自己建的東西

| 對象 | 結果 | 說明 |
|---|---|---|
| Drive 的 ULID | ❌ **P1（High）** | `ulids.update(store.Index(paths).known())`：`search`／`sync` 已經把 `agora-test/sessions/` 的**全部** Session 都同步進這個 cache，所以 teardown 會把其他次執行、其他整合測試、impl2 的 e2e 的 Session 全部 purge 掉。impl2 在 `aaa2d4e` 修 D-1 的方法，是「只收集 `run_main` 在 stdout 印出來的 id」，這裡應該照做：刪掉這一行，在 `run_main` 裡記下 import／continue／merge 印出的 id（可以直接沿用 `test_e2e_cli.py` 的 `created["printed"]` 寫法） |
| opencode session | ✅ | 依 id 一個一個 `session delete`，id 來自測試本身，以及 wrapper 紀錄裡的 `--session`／`-s`；`PWD` 也有設定。另外，opencode 這邊用的是 conftest 隔離的 HOME（`AGORA_REAL_HOME` 被刻意 unset），**真的 `~/.local/share/opencode` 不會被碰到**，資料庫隨著 tmp 一起消失。小提醒：wrapper 的 docstring 還寫著「把 HOME 改回 `AGORA_REAL_HOME`，因為隔離的 HOME 會讓 export 找不到」，和 fixture 現在的做法相反，要改成一致的說法（Low） |
| claude 的 jsonl | ⚠️ **P2（Medium）** | (a) uuid **只在** `header_of(id3)` 的斷言成功之後才記下來（`created["uuids"].append(uuid3)`），沒有像 `test_e2e_cli.py`（E3）那樣，也從 claude wrapper 的 `e2e-args.log` 收集 `--session-id`。只要 continue 失敗，claude 在真實 `~/.claude/projects/` 裡寫的 jsonl 就會留下來。(b) 刪除時用的是 `C.projects_dir().glob(f"*/{session_id}.jsonl")`、`glob(f"*/{session_id}")`，以及 `cfgdir.glob(f"todos/{session_id}-*.json")`，這些都會掃過真實的 `~/.claude/projects/*/` 和 `~/.claude/todos/` 的檔名（CL12、D-2 已經在 claude 那邊拿掉的寫法，這裡又出現了）。(c) 最後用 `glob(f"*{encode(proj)}*")` 找資料夾，然後 `rmtree(stale / "memory")`，這是用子字串比對**真實的**專案資料夾名稱，再整個刪掉 memory。 | (a) teardown 也讀 `fake_home / "e2e-args.log"`，收集 `--session-id`／`--resume` 後面的 uuid；(b)(c) 一律改用精確的路徑 `C.projects_dir() / C.encode_project_dir(proj) / f"{uuid}.jsonl"`（以及同名的 sidecar 資料夾）、`session-env/<uuid>`、`file-history/<uuid>`。todos 不要處理（工具已經禁止了，所以不會產生 todos）。資料夾只對 `encode_project_dir(proj)` 這一個精確的名字做 `rmdir`／刪掉 `memory`。這些都可以直接照抄 `test_e2e_cli.py`（`aaa2d4e`）的寫法 |

### 有沒有讀到真實資料的可能

| # | 嚴重度 | 位置 | 問題 | 建議 |
|---|---|---|---|---|
| P3 | **Medium** | `opencode_noninteractive.py`、`test_e2e_opencode.py` 的 `opencode_run`、`test_opencode_real.py` | opencode 的 `run` 只靠 prompt 裡的「不要呼叫任何工具」，**沒有用設定限制工具**（Claude 那邊 E6 已經用 `--disallowedTools` 擋住了）。opencode 的 read／glob／grep／bash 都可以用絕對路徑讀到 `/Users/…` 底下的任何檔案；HOME 被隔離，只會改變 `~` 指向哪裡 | 在這三個地方的 env 裡設定 `OPENCODE_PERMISSION='{"*":"deny"}'`（opencode 支援用這個環境變數設定權限；團隊平常用的是 allow-all，這裡要反過來）。用一次 `opencode run` 確認它真的會拒絕工具呼叫，再把結果記到 spike/opencode.md |
| — | ✅ | claude | e2e 和整合測試裡**所有的** claude 呼叫都有 `--disallowedTools`：`claude_noninteractive.py` 的 `GUARDS`、`test_e2e_cli.py:169`、`test_claude_real.py` 的 `run_claude` |

### `--disallowedTools` 會不會吃掉 prompt — ✅ 不會

`--disallowedTools` 是可變長度的選項，會一直吃參數，直到遇到下一個選項為止。所有地方都把它放在**最前面**，後面緊接著另一個選項，所以 prompt 一定不會被吃掉：

- `claude_noninteractive.py`：`[real, "--disallowedTools", "Bash Read … Task", "--model", "haiku", "--resume"|"--session-id", <id>, "-p", <prompt>]`：工具清單後面接的是 `--model`，所以會在這裡停下來；prompt 是最後一個位置參數。
- `test_e2e_cli.py:169`：`["claude", "--disallowedTools", DISALLOW, "--model", "haiku", …]`，同上。
- `test_claude_real.py`：`["--disallowedTools", DISALLOW, *argv]`，`argv` 都是以 `-p` 或 `--resume` 開頭，同上。

工具清單是用空白隔開、包成一個參數的字串，Claude 接受這種格式。

### e1439ab：F1～F5、G1～G3 的確認

| # | 狀態 | 確認的內容 |
|---|---|---|
| F1 | ✅（做法和建議不同，可以接受） | 清理改成**寫死的路徑**，沒有任何會變空的變數或佔位字：Claude 用 `rm -rf "$HOME/.claude/projects/-private-tmp-agora-acc-proj"`（和 `encode_project_dir('/private/tmp/agora-acc/proj')` 的結果一致）；Drive 用 `test -n "$FID" && … purge gdrive:sessions`。`FID` 抓不到時（python3 失敗，或者找不到 agora-test），`next()` 會丟出例外，`FID` 變成空字串，就不會執行 purge。**提醒（Low）**：這會清掉**整個** `agora-test/sessions/`，包括其他測試留下的資料。對使用者自己的驗收來說可以接受，但要在文件寫一句「清理時，不要同時有整合測試或 e2e 在跑」 |
| F2 | ✅ | 改成 `env.sh`，每一節的第一行都是 `source`，並且會印出 `[acc] … ✓` 讓人確認。我建議的程式端提示（每次寫入 Drive 時印出資料夾名稱）沒有做，維持 Low |
| F3 | ✅ | 第 4 節改成：等第一段說完 → 再送一句話 → 在回覆到一半時按 Ctrl-C → `/exit`。計數會大於 `before_count=2`，所以會存出 D；文件也寫明了「在 Claude 說出任何話之前就按 Ctrl-C，會印出『這次沒有新內容』，這也是正確的」 |
| F4 | ✅ | 改成「包含 A、B、C、D」。continue 會沿用父 Session 的 title（「驗收表格」），所以這四個都搜得到 |
| F5 | ✅ | 清理改成刪掉整個驗收專案的 Claude 資料夾，所以沒有存檔的 uuid 也會一起清掉 |
| G1 | ✅ | `folder_id()` 會 `pop` 舊的 `folder_id`／`folder_name`；下一次寫入 `config.json` 時，它們就會消失 |
| G2 | ✅ | 只有 `-` 開頭、而且**不是** `--` 開頭的那一個多餘參數，才會被當成關鍵字；打錯的 `--flag` 照樣會報錯 |
| G3 | ✅ | sync 遇到「還沒寫完」時，也會把鏡像裡那份 session.md 刪掉，離線重建時就不會把它放回來 |

另外，acceptance.md 現在的第 2、3、4、5 節都**沒有帶 `--dir`**，靠的是 `source.dir`（`/private/tmp/agora-acc/proj`）的預設值；第 6 節的 merge 沒有 `source`，所以用的是 `env.sh` cd 進去的那個目錄。兩者一致，Claude 的資料夾名稱也和清理時用的路徑相同。✅

### 這次跑過的指令

| 指令 | 結果（只記形狀） |
|---|---|
| `git show d3ae9cb:…/opencode.py` 存到 scratchpad；用 importlib 和 exec 把新舊兩版載入同一個 Python，比對 `reidentify` 與 `_injected_payload` | 兩者都完全相同；之後 scratchpad 的檔案已經刪掉 |
| `git archive HEAD \| tar -x`，再用 `PYTHONPATH=<head>/src` 跑 `pytest -q tests/unit` | 156 passed；`src` 是 1,925 行；之後副本已經刪掉 |
| `grep -cF` 檢查工作目錄裡 `test_e2e_opencode.py` 的 `known()`／`todos`／`OPENCODE_PERMISSION` | 還沒 commit 的版本也一樣有 `known()` 和 todos 的 glob，也沒有 `OPENCODE_PERMISSION` |

沒有執行 e2e、整合測試或驗收清單，沒有碰 Drive，沒有叫真的 opencode 或 claude，也沒有讀任何真實的 Session。
