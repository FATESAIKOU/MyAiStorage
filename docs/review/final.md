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
