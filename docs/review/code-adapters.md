# Code review：agents/claude.py 與 agents/opencode.py

2026-10-02，review。只提意見，沒有改程式。對照的是 design.md 第 3 版（5.2、5.4、4.4／D6）、`docs/spike/claude.md`、`docs/spike/opencode.md`、`docs/review/test-plan.md`，以及 `agents/base.py` 的介面。opencode 一節等 `agents/opencode.py` commit 之後再補。

---

## A. agents/claude.py

對象是 `9d1d549` 的 `src/agora/agents/claude.py`、`tests/unit/test_agent_claude.py`、`tests/fakes/fake_claude.py`，以及 `tests/integration/test_claude_real.py`。最後這一個看的是工作目錄裡的版本，它比 commit 多了 4 行 cleanup，還沒有 commit。

### 結論（依 PM 指定的五個重點）

| 重點 | 結論 | 說明 |
|---|---|---|
| raw 的包法與還原是否無損（含 aux、非 UTF-8） | **有條件可行** | main 存的是每一行的原字串，aux 裡 UTF-8 的檔存成文字，不是 UTF-8 的存成 `{"$base64": …}`，包起來和解開都不會失真。例外有三個：最後一行寫到一半時會刻意丟掉（S10）；main 本身如果不是 UTF-8，會直接 crash（CL8）；還原 aux 的 `.jsonl` 時會掉最後的換行，遇到寫到一半的行也會 crash（CL6） |
| sessionId／cwd 改寫是否正確 | **可行** | 每一行的 `sessionId` 都改成新的 uuid，aux 的 `.jsonl` 也一樣（spike 說 subagent 檔裡記的是父 session 的 uuid，所以這樣改是對的）。`uuid`／`parentUuid` 這條鏈沒有動，和 spike 的 V2(a) 一致。小問題在 CL7 |
| collect 找不找得到 resume 之後的那一份 | **有條件可行** | 「新 uuid 的 jsonl 放在工作目錄的編碼資料夾，然後 `--resume <新uuid>`，Claude 會接著寫同一個檔」這條路是對的，整合測試也驗證過。但**編碼規則是錯的（CL1，High，已實測）**，只要路徑裡有 `_`、空白或中文，Claude 就找不到這個 session。另外，spike 只用 `-p` 驗證過，**互動模式**的 resume 是不是也接著寫同一個檔、`/clear` 之後會怎樣，都還沒有驗證（CL5） |
| 閱讀版是否只收文字＋工具一行 | **有條件可行** | text、tool_use 的一行摘要、略過 tool_result 和 thinking 都對，測試也涵蓋了。但本機指令行、`isMeta` 行、`system` 行會混進閱讀版，也會被計成新訊息（CL2） |
| 有沒有任何路徑會讀到真的 `~/.claude` | **可行（單元測試安全）** | 所有路徑都經過 `claude_home()`，也就是先看 `AGORA_CLAUDE_HOME`，沒有才用 `Path.home()`。conftest 把兩者都指到暫存目錄。adapter 裡沒有任何寫死的 `~/.claude`。但是**沒有支援 `CLAUDE_CONFIG_DIR`**（CL4）；整合測試的 `find_jsonl` 會用 glob 掃過真的 `projects/*/` 目錄名稱（CL12） |

### 問題清單

| # | 嚴重度 | 位置 | 問題 | 建議 |
|---|---|---|---|---|
| CL1 | **High** | `encode_project_dir`；`tests/fakes/fake_claude.py` 的 `encode` | 目前只把 `/`、`.` 換成 `-`。**實測 Claude Code 2.1.286 會把所有非英數字元都換成 `-`**：在我建的 `/private/tmp/agora-review-claude/a_b 專案.v2` 跑 `claude -p --session-id <uuid>`，檔案出現在 `…-agora-review-claude-a-b----v2/<uuid>.jsonl`（`_`、空白、每一個中文字、`.` 各變成一個 `-`）；adapter 的規則算出來的是 `…-a_b 專案-v2/`，那裡沒有這個檔。影響：`start_native` 把 jsonl 寫進錯的資料夾，於是 `claude --resume <新uuid>` 找不到這個 session。接著 collect 會用 glob 找到 agora 自己寫的那份，發現訊息數沒變，就回傳 None，只印一句「這次沒有新內容」。**原生接續失敗了，而且使用者看不出原因**。fake_claude 用的是同一套錯的規則，所以單元測試抓不到 | `re.sub(r"[^A-Za-z0-9]", "-", abs_path)`，fake_claude 也要改。單元測試加上 `/tmp/a_b 專案.v2` → `-tmp-a-b----v2`。BMP 以外的字元（例如 emoji）可能會變成兩個 `-`，超長的路徑可能會被截斷並加上雜湊，這兩點都要在 V5 的 spike 補驗證。整合測試的專案目錄名稱要含有 `_` 和中文（CL13） |
| CL2 | Medium | `_count_messages`、`_reading_turns`、`_SILENT_TYPES` | (a) 每一行 `type == "user"` 都算成一則訊息，包括 Claude 記錄本機指令的行（`/exit`、`/compact`、`/model`……，內容是 `<command-name>`、`<local-command-stdout>`）和 `isMeta: true` 的「Caveat」行。結果是使用者打開之後什麼都沒說，只用 `/exit` 離開，也會被存成一個幾乎是空的 continue Session（違反 S9 的「內容變多才存」）。(b) 頂層的 `system` 行（`compact_boundary`、`local_command`、`api_error` 等 subtype）不在 `_SILENT_TYPES` 裡，所以會輸出成 `## user` 底下的 `[skip system]`，而且角色標錯了。(c) 這些雜訊會進入閱讀版與搜尋 | 計數和閱讀版都略過這些行：`isMeta` 為真的行、內容開頭是 `<command-name>`／`<local-command-stdout>`／`<local-command-caveat>` 的 user 行。把 `system` 加進 `_SILENT_TYPES`（`compact_boundary` 可以考慮輸出成一行 `[compact]`）。`isCompactSummary` 的行要保留，它是壓縮之後的上下文。fixture 要加上這幾種行 |
| CL3 | Medium | `tests/unit/test_agent_claude.py`（`claude_env`）、`fake_claude.py` 的 shebang | **在這台機器上，有 2 個測試失敗**（`test_export_basic`、`test_collect_after_fake_resume`）。原因是 fake 的 shebang 寫的是 `#!/usr/bin/env python3`，在這台機器上會找到 asdf 的 shim，而 conftest 把 `HOME` 換成暫存目錄之後，shim 就壞了（「unknown command: python3」）。所以 `--version` 和 `--resume` 都叫不起來 | 照 `test_store.py` 處理 fake_rclone 的方式：在 tmp 底下產生一個 wrapper `#!/bin/sh\nexec {sys.executable} fake_claude.py "$@"`，再讓 `AGORA_CLAUDE_CMD` 指向它。這樣就和使用者的 shell 設定無關 |
| CL4 | Medium | `claude_home`／`projects_dir` | Claude Code 支援用 `CLAUDE_CONFIG_DIR` 搬移整個 `~/.claude`。如果使用者設了它，adapter 會到錯的地方找 session、把 continue 的檔寫到錯的地方。這台機器目前**沒有**設定這個變數，所以只是潛在的問題 | 優先順序：`AGORA_CLAUDE_HOME/.claude` > `CLAUDE_CONFIG_DIR` > `~/.claude`。conftest 另外 `monkeypatch.delenv("CLAUDE_CONFIG_DIR")`，確保測試不會繞過隔離 |
| CL5 | Medium（待驗證） | `start_native`／`collect`；spike V2 | spike 只驗證過 `claude --resume <id> -p`。互動模式的 resume 會不會接著寫同一個檔，還沒有驗證。而且使用者在 session 裡執行 `/clear`（或在 picker 裡換了 session）之後，Claude 會改寫到**另一個 uuid 的檔**，collect 只看 `launch.agent_session_id`，就會漏掉 `/clear` 之後的內容 | test-plan 的 M-03 人工確認要加一步：互動模式下說一句話，執行 `/clear`，再說一句話，然後離開。如果 `/clear` 會產生新檔，就在 design 5.4 寫明「`/clear` 之後的內容不會存回」，或者讓 collect 另外收集同一個專案資料夾裡、在啟動之後新建、而且 `parentUuid` 鏈接得上的檔（成本比較高，不建議現在做） |
| CL6 | Low | `start_native` 裡處理 aux `.jsonl` 的地方 | 先 `content.split("\n")`，經過 `_rewrite_lines`，再 `"\n".join`。這樣會掉最後的換行和空行；`_rewrite_lines` 對每一行都直接 `json.loads`，subagent 的檔如果最後一行只寫了一半（import 的時候 subagent 還在跑），就會丟出沒有被接住的 JSONDecodeError | 共用 `_read_lines` 的規則（最後一行不完整就丟掉），並且保留原本最後的換行；解析失敗就丟 AgentError |
| CL7 | Low | `_rewrite_lines` | docstring 說「cwd when present」，實際上是每一行都無條件設定 `cwd`，連原本沒有 `cwd` 的 `summary`、`queue-operation` 也加上了。`json.dumps` 沒有加 `ensure_ascii=False`，所以中文會被轉成 `\u` 跳脫字元（內容相同，檔案變大）。design 第 3 版寫「只改頂層 `sessionId`，其他欄位不動」，和程式不一致 | 只改原本就有 `cwd` 的行（spike V5：fork 也只是把既有的 cwd 改成新的目錄），加上 `ensure_ascii=False`。design 5.4 改成「`sessionId` 全部改寫；`cwd` 有的話改成工作目錄」 |
| CL8 | Low | `_read_lines` | 用 `read_text(encoding="utf-8")` 讀檔，非 UTF-8 的內容會丟出 UnicodeDecodeError，最後由 main 的 catch-all 接住，訊息看不出是哪個檔。中間如果有空行，也會被當成「解析失敗」 | 包成 AgentError，並且寫出檔名；中間的空行直接略過 |
| CL9 | Low | `start_injected`（`before_count=0`） | 注入的 prompt 加上 Claude 的自動回覆，計數一定會超過 0。所以使用者開了之後直接離開，也會被存成一個新的 Session | `before_count=2`，或者在 collect 時只計算第一則 assistant 之後的行 |
| CL10 | Low | `_assistant_lines` | `server_tool_use`、`mcp_tool_use` 這些也是工具呼叫，但目前會輸出成 `[skip …]`；`web_search_tool_result` 之類的結果區塊也會輸出 `[skip …]`。另外，Claude 的 assistant 是一個 content block 一行，所以同一輪對話會被切成好幾個連續的 `## assistant` 段落 | `*_tool_use` 都輸出成 `tool_line`，`*_tool_result` 都靜默。`format_reading`（base.py）把連續同一個角色的段落合併，opencode 也會受惠 |
| CL11 | Low | `_exported` | `created_at` 取的是 `objs[0]` 的 timestamp，但第一行可能是沒有 timestamp 的 `summary`／`queue-operation`。`agent_version` 每次都去跑 `claude --version`，記的是**現在**的 CLI 版本 | `created_at` 改用第一個有 timestamp 的行；`agent_version` 改用 jsonl 最後一行的 `version` 欄位，找不到時才去叫 CLI（這樣也省掉一次 subprocess，CL3 的問題也比較不會出現） |
| CL12 | Low | `tests/integration/test_claude_real.py` | `export` 和 `collect` 的退路都會呼叫 `find_jsonl`，它會用 glob 掃真的 `~/.claude/projects/*/` 底下的**所有目錄名稱**。雖然只會開啟名字是自己 uuid 的檔，但這已經是在列出真實的資料夾，和「不准列出」的規則有衝突的空間 | `find_jsonl` 先試 `projects/<encode(cwd)>/<id>.jsonl`，找不到才用 glob。export 可以多一個可選的 `hint_dir` 參數，整合測試就傳 proj 進去。這樣整合測試永遠不會掃描真實的資料夾 |
| CL13 | Low | 整合測試的專案目錄 | `/tmp/agora-it-claude/proj` 只含英數字和 `-`，所以抓不到 CL1 | 改成 `/tmp/agora-it-claude/p_專案.v2` 這種名字（記得要是自己建的） |
| CL14 | Low | `_unpack_aux` | aux 的 key 直接接在 `sidecar / rel` 後面。key 如果是 `../x` 或絕對路徑，就會寫到 sidecar 外面。raw 來自自己的 Drive（D2），但多加一道檢查的成本很低 | `rel` 含有 `..`，或者是絕對路徑，就丟出 AgentError |

### 測試的缺口

- encode 的參數化測試只有 `/` 和 `.`（CL1）。
- fixture 裡沒有本機指令行、`isMeta` 行、`system` 行、`server_tool_use`（CL2、CL10）。
- 沒有「aux 裡有非 UTF-8 的二進位檔，包起來再解開，位元組完全相同」的測試（目前的 aux 都是文字）。
- 沒有「aux 的 `.jsonl` 最後一行寫到一半」的測試（CL6）。
- 沒有「設了 `CLAUDE_CONFIG_DIR` 也不影響測試隔離」的測試（CL4）。
- CL3 修好之前，這台機器上會有 2 個測試是紅的。

### 對 design.md 的修改建議

1. 5.4 的 claude 原生那一列：把「工作目錄編碼」的規則明確寫成「所有非英數字元都換成 `-`」（CL1）；並把改寫的範圍寫成「`sessionId` 全部改寫，`cwd` 有的話改成工作目錄」（CL7）。
2. 4.4 閱讀版：Claude 的本機指令、`isMeta`、`system` 行都不收，也不計入「內容變多」（CL2）。
3. 5.4 加一句：`/clear` 之後的內容會不會存回，以 M-03 的結果為準（CL5）。
4. 第 4 節的 Mac 路徑：Claude 的資料位置，依序是 `AGORA_CLAUDE_HOME` > `CLAUDE_CONFIG_DIR` > `~/.claude`（CL4）。

### 這次跑過的指令（claude 部分）

| 指令 | 結果（只記形狀） |
|---|---|
| `.venv/bin/python -m pytest -q tests/unit/test_agent_claude.py` | 15 passed、2 failed（`agent_version` 是 None；fake 的 `--resume` rc=1） |
| 在 `HOME=<暫存目錄>` 的環境下直接執行 `tests/fakes/fake_claude.py --version` | 輸出「unknown command: python3. Perhaps you have to reshim?」（CL3 的原因）；用正常的 HOME 執行則會印出版本 |
| `echo ${CLAUDE_CONFIG_DIR:+set}` | 沒有設定 |
| 在自己建的 `/private/tmp/agora-review-claude/a_b 專案.v2` 執行一次 `claude -p --model haiku --session-id <uuid> '只回覆兩個字：好的'` | rc=0。之後**只對兩個候選路徑做 `test -f`**，沒有列出 `~/.claude/projects`：檔案在「非英數字元全部換成 `-`」那個路徑底下（CL1）。驗證完之後，按這個 uuid 刪掉了那一個 jsonl 和那個資料夾，也刪掉了 `/private/tmp/agora-review-claude` |

沒有讀任何真實的 Session 或 MyBrain，也沒有碰 Drive 或 rclone.conf。

---

## B. agents/opencode.py

（等 `agents/opencode.py` commit 之後補上。）
