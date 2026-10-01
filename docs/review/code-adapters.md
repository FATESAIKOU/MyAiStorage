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

對象是 `e4d0228` 的 `src/agora/agents/opencode.py`、`tests/unit/test_agent_opencode.py`、`tests/fakes/fake_opencode.py`、`tests/fixtures/opencode/`、`tests/integration/test_opencode_real.py`。對照的是 design 第 3 版 5.2／5.4、`docs/spike/opencode.md`（V1 陷阱 1–6、V3、V5），以及 base.py。

### 結論（依 PM 指定的重點）

| 重點 | 結論 | 說明 |
|---|---|---|
| id 重編是否完整（三種 id、所有參照欄位、固定寬度、依匯出順序遞增） | **有條件可行** | 三種前綴都對（陷阱 3），寬度固定是 28 個字元，message id 是「time.created 加上陣列位置」，所以和 `ORDER BY time_created, id` 的順序一致（陷阱 5）。`sessionID`／`messageID`／`parentID` 也都改寫了（N14）。但是 **part id 會撞號（OC1，High，已重現）**；參照欄位的改寫範圍又**太寬**，會改到工具的 metadata（OC2）；`partID` 沒有改到（OC6） |
| import 後是否回讀驗證訊息數，失敗時刪掉半個 session | **原生：可行；注入：不可行** | `start_native` 在 import 之後立刻 export 一次並比對訊息數，不一致就 `session delete`，然後報錯（陷阱 2、4）。但只比對**訊息數**，所以 OC1 那種「訊息都在、part 掉了」抓不到。`start_injected` **完全沒有回讀**（OC3） |
| 注入的 export 形狀 | **可行** | 一則 user 訊息，內容是說明加上閱讀版全文（N4、V3）。`info` 補齊了 opencode 匯入與匯出都需要的欄位（整合測試抓到的第 1 個問題已經修好）。`before_count=1` 是對的。另見 OC5（閱讀版會一層一層重複） |
| export 是否沒有 `2>&1` | **可行** | stdout 寫到暫存檔，stderr 用 PIPE 分開接（陷阱 1）。也有測試確認進度行不會進到 stdout |
| 測試會不會碰到真的 `~/.local/share/opencode`，或在沒有 commit 的資料夾跑 `session list` | **單元測試：可行；整合測試：有條件** | 單元測試全部透過 `sys.executable` 的 wrapper 叫 fake，不會叫到真的 opencode，HOME 也被 conftest 隔離了。整合測試的 `session list` 只在 `/tmp/agora-it-opencode/proj` 裡跑，fixture 會確保那裡是有 commit 的 git repo，刪除也是一個 id 一個 id 地刪。但是 **`real_home` 這個 fixture 實際上沒有作用（OC4，已實測）**：測試本體裡的 HOME 仍然是 pytest 的暫存目錄，teardown 時卻是真的 HOME，所以「建立」和「清理」用的是兩個不同的 opencode 資料庫 |

### 問題清單

| # | 嚴重度 | 位置 | 問題 | 建議 |
|---|---|---|---|---|
| OC1 | **High** | `reidentify`（約 174–177 行） | part id 是 `prt_` 加上 **該則訊息**的 time 加上 **訊息內的** part 序號，再加上 salt，裡面沒有訊息的位置。所以兩則訊息只要 `time.created` 相同（同一毫秒），或其中一則**沒有** `time.created`（程式會沿用前一則的 stamp），它們的第 0 個 part 就會拿到**完全相同的 id**。**已重現**：用自編的兩則訊息 payload 跑 `reidentify`，「同一毫秒」與「缺少 time」兩種情況，兩個 part 的 id 都一模一樣。opencode 的 import 是 `onConflictDoNothing`（陷阱 2），**第二個 part 會被靜默丟掉**；因為訊息數沒有變，回讀驗證也會通過。結果是接續之後的對話少了內容，而且沒有任何警告 | part id 改成包含訊息位置，例如 `prt_` + time(12) + **訊息序號(6)** + part 序號(4) + salt(6)。這樣仍然是固定寬度，訊息內的順序也不變（同一則訊息的 time 與訊息序號相同，只有 part 序號在遞增）。回讀驗證另外比對 **part 總數**。單元測試加上「兩則訊息同一毫秒」和「缺少 time.created」這兩個案例 |
| OC2 | Medium | `_rewrite_references` | 它會遞迴走過**整個** payload，所有名叫 `sessionid` 的 key（不分大小寫）都改成新的 session id。**已重現**：task 工具 part 的 `state.metadata.sessionId`（它指向的是 subagent 的**子 session**）被改成了新的父 session id，等於讓它指向自己。工具的 input、output 裡如果剛好有 `sessionId`、`messageID`、`parentID` 這種 key（例如使用者自己的 JSON），也會被改掉，這和 docstring 說的「除了 id，其他位元組都不變」不一致 | 只改結構上的位置：`info.id`；`messages[].info.{id, sessionID, parentID}`；`messages[].parts[].{id, sessionID, messageID}`；`info.revert.{messageID, partID}`（見 OC6）。`state`、`metadata`、`input`、`output` 一律不動。單元測試加上「task part 的 metadata.sessionId 保持不變」 |
| OC3 | Medium | `start_injected` | 和 start_native 不一樣，這裡沒有做陷阱 4 的回讀驗證。如果注入的 payload 被拒絕（只寫進一半，或者是空的 session），agent 照樣會被打開；使用者說了話之後，collect 會因為 `>1` 而存檔，但 agent **根本沒有看到閱讀版**，整個過程沒有任何訊息 | 把 start_native 的「export 回讀 → 數量不符就刪掉並報錯」抽成 `_import_verified(payload, workdir)`，兩邊共用（也順便省幾行） |
| OC4 | Medium | `tests/integration/test_opencode_real.py` 的 `real_home`／`trash` | `real_home` 是 module scope，直接改 `os.environ["HOME"]`。但 conftest 的 autouse `isolated_home` 是 function scope，會在**每個測試本體**裡用 monkeypatch 再把 HOME 換成暫存目錄。**已實測**：我把 conftest 複製到 scratchpad，配一個同樣寫法的 module fixture，結果測試本體裡的 HOME **不是**真的家目錄。所以：(a) 測試裡的 `opencode run`／`import`／`export` 用的是暫存目錄裡全新的資料庫，不是 docstring 說的「real store」；(b) `trash` 的 teardown 是在 monkeypatch 還原之後才跑，這時 HOME 是真的家目錄，它會到**真的**資料庫裡列出並刪掉 PROJ 底下的 session，和測試建立的 session 不是同一批；(c) `trash.append(...)` 收集的 id 完全沒有被用到 | 明確選一種做法，建議用比較安全的那種：在 function scope 用 monkeypatch 把 `HOME`（以及 `XDG_DATA_HOME`）指到**這個 module 專用的暫存目錄**，讓 opencode 的資料庫完全隔離（目前測試能跑通，表示免費模型不需要真的 auth）；teardown 只刪 `trash` 裡記下的 id。如果真的需要碰真的資料庫，就在 function scope 用 monkeypatch 設定真的 HOME，並且把 docstring 和 teardown 都改成一致的做法 |
| OC5 | Low | `start_injected`／`reading_of` | 注入的閱讀版會以一般的 user text part 存進新的 session。之後如果再從這個 session 跨 agent 接續、或者被 merge，它的閱讀版裡會**再包一次**上一層的完整閱讀版。每多接一次，內容就重複一層，搜尋結果也會重複。Claude 那邊用的是 `@路徑`，檔案的內容是放在 attachment 行裡（靜默），所以不會有這個問題 | 注入的 part 加上 `"synthetic": true`（或者 `metadata.agora = "injected"`）。`_lines_of` 遇到它時，只輸出一行 `[注入的閱讀版]`。另外，opencode 自己產生的 `synthetic: true` text part（附加檔案時，它會放進檔案內容）也一樣略過，這也比較符合 D6 的「不收工具結果」 |
| OC6 | Low | `reidentify` | session 的 `info.revert`（`{messageID, partID, …}`）裡，`partID` 不在 `_REFERENCE_KEYS` 裡，所以重編之後它指向一個不存在的 part。在被 revert 過的 session 上接續時，undo／redo 的狀態會壞掉 | 留一份 `part_map`，把 `partID` 也照著對應過去（OC2 改成只處理結構位置時，一起加進去） |
| OC7 | Low | `_run`、`_export_bytes`、`_import`、`_delete` | 所有對 opencode 的 subprocess 呼叫都沒有 timeout。cli 會在**每個指令開始時**跑 recover_pending，而它會呼叫 collect，也就是 export。opencode 只要卡住一次（例如資料庫被鎖住），之後每一個 agora 指令都會跟著卡住 | export／import／delete 都加上 `timeout=60`，逾時就丟出 AgentError（這樣會走到 R4 的「下次再試」） |
| OC8 | Low | `export` 的 `_version()` | 每次 export、collect 都會多跑一次 `opencode --version`。但匯出檔的 `info.version` 本來就有版本（fixture 裡是 `1.18.34`），而且那才是產生這個 session 的版本 | 改用 `info.version`，找不到時才叫 CLI（和 claude 的 P3 一致，也可以省幾行） |
| OC9 | Low | `_delete` | rc 沒有檢查，但錯誤訊息寫的是「已經刪掉 {id}」。如果刪除失敗，訊息就是錯的，下一次重試還會撞到同一個 id | 檢查 rc；失敗時，錯誤訊息改成「刪除也失敗了，請手動 `opencode session delete <id>`」 |
| OC10 | Low | `collect` | 沒有 `agent_session_id` 時，opencode 回傳 None（cli 會把它當成「沒有新內容」，然後刪掉 pending），但 claude 會丟出 AgentError（pending 會留下來再試）。兩個 adapter 的行為不一致 | 統一成丟出 AgentError。pending 裡一定會有這個 id（N4），如果沒有，就表示 record 壞了 |
| OC11 | Low | `tests/conftest.py` | 單元測試的隔離只靠 HOME。這台機器目前沒有設定 `XDG_DATA_HOME`，但如果有人設了，忘了用 `fake` fixture 的測試就會去叫真的 opencode，連到真的資料庫 | conftest 加上 `monkeypatch.delenv("XDG_DATA_HOME")`、`delenv("XDG_CONFIG_HOME")`，並且預設把 `AGORA_OPENCODE_CMD`／`AGORA_CLAUDE_CMD` 指向一個「一定會失敗」的 stub。需要 fake 的測試再自己覆蓋 |

### 測試的缺口

- OC1 需要的案例：同一毫秒、缺少 `time.created`、回讀時比對 part 數。
- OC2：task part 的 metadata 不變；工具 input 裡有 `sessionId` 這個 key 的時候也不變。
- OC3：注入時 import 只寫進一半，要報錯，並且刪掉那個 session。
- fixture 裡沒有 `synthetic` text part、沒有 `compaction`／`subtask` part、也沒有 `info.revert`（OC5、OC6）。
- OC4 修好之前，整合測試的「真實資料庫」語意都不成立。

### 對 design.md 的修改建議

1. 5.4 opencode 原生那一列：「依匯出順序遞增」改寫成「message id＝time＋訊息序號；part id＝time＋**訊息序號**＋part 序號」，並且說明回讀時同時比對訊息數與 part 數（OC1）。
2. 同一列：寫明「只改結構位置上的 id 與參照，`state`／`metadata` 不動」（OC2）。
3. 5.4 注入那一列：寫明「注入同樣要回讀驗證」，並且說明注入的 part 有一個標記，閱讀版只會輸出一行（OC3、OC5）。

### 這次跑過的指令（opencode 部分）

| 指令 | 結果（只記形狀） |
|---|---|
| `.venv/bin/python - <<…`：用自編的兩則訊息 payload 呼叫 `opencode.reidentify` | 「同一毫秒」：part id 相同；「缺少 time」：part id 相同（OC1）。task part 的 `state.metadata.sessionId` 變成了新的 session id（OC2） |
| 把 `tests/conftest.py` 複製到 scratchpad，配一個和 `real_home` 同樣寫法的 module fixture，執行 pytest | 測試本體裡的 HOME 不是真的家目錄（OC4）。scratchpad 已經刪掉 |
| `echo ${XDG_DATA_HOME:+set}` 等 | `XDG_DATA_HOME`、`XDG_CONFIG_HOME`、`OPENCODE_DATA_DIR` 都沒有設定 |
| `.venv/bin/python -m pytest -q tests/unit` | 98 passed |

沒有執行任何真的 opencode 指令，也沒有讀 `~/.local/share/opencode`、Drive 或 rclone.conf。

---

## C. e2e 與文件

對象：`3bd4823` 的 `tests/integration/test_e2e_cli.py`、`tests/fakes/claude_noninteractive.py`；以及 `README.md`、`docs/design.md` 第 3 版，對照目前的 `src/agora/cli.py`（`2471a66`）。這次**沒有執行** e2e，因為它會用到真的 Drive 和 3 次 `claude -p`，只做了靜態檢查，再加上一個不花費 token 的 shebang 檢查。

### C-1. e2e：是否真的走過每個指令

**結論：有條件可行。** import、search、continue（原生與注入各一次）、merge 都是透過 `agora.cli.main` 執行的，而且斷言了 relation、parents、`source.session_id`，以及 wrapper 收到的是 `--resume` 還是 `--session-id`。這條主鏈驗證得很完整。但是：

| # | 嚴重度 | 問題 | 建議 |
|---|---|---|---|
| E1 | **High**（在這台機器上） | `claude_noninteractive.py` 的 shebang 是 `#!/usr/bin/env python3`，和 CL3 是同一個問題：cli 用 `os.environ` 啟動 wrapper，而 conftest 已經把 HOME 換成 pytest 的暫存目錄，所以 asdf 的 shim 會失敗。**已實測**：`HOME=<暫存目錄> claude_noninteractive.py --version` 印出「unknown command: python3」。在這台機器上，continue 會叫不起 agent，collect 回傳 None，stdout 是空的，結果 `out.split()[0]` 丟出 IndexError。這個測試應該是在 impl2 的環境（python3 不是走 asdf）才跑得通 | 和 CL3 一起修：fixture 在 tmp 底下產生 `#!/bin/sh\nexec {sys.executable} claude_noninteractive.py "$@"`，再讓 `AGORA_CLAUDE_CMD` 指向它 |
| E2 | Medium | **`show` 和 `sync` 沒有經過 cli.main**：`read_header` 直接呼叫 `store.sync`，`show`、`show --raw` 都沒有測到。merge 只測了 `id1,id2`，沒有測使用者原本的寫法 `id1, id2`（shell 拿到的參數是 `id1,`、`id2`）。exit 3（outbox）與 `--no-sync` 也沒有測到 | 加一步 `run_main("show", id2)`，斷言第一行是 id2，而且內容有 header；加一步 `run_main("show", id1, "--raw")`，斷言 `format == claude-jsonl/1`；`read_header` 改用 `run_main("sync")` 加上 `store.Index`；merge 改成 `run_main("merge-session", f"{id1},", id2)` |

### C-2. e2e：清理是否完整

| # | 嚴重度 | 問題 | 建議 |
|---|---|---|---|
| E3 | Medium | **id 是在斷言成功之後才記下來的**。如果中途失敗，就會漏掉清理：(a) `run_main` 先 `assert rc == 0` 才 return，所以 import 如果回 3（outbox）或 2，那個 ULID 不會被記下（之後 sync 有可能把它推上 Drive）；(b) uuid2、uuid3 是從 agora 的 header 讀出來的，如果 continue 失敗（例如 E1、CL1），agora 在 `start_native` 已經寫好的 `<uuid2>.jsonl`，以及 claude 建的檔，都不會被刪掉，就留在真實的 `~/.claude/projects/` 裡 | teardown 一律從 wrapper 的 `e2e-args.log` 收集所有 `--resume`／`--session-id` 後面的 uuid（這份紀錄在 agent 啟動前就寫好了），再加上 uuid1。ULID 則是在 teardown 時，從 `AGORA_CACHE_DIR` 的索引和 `AGORA_STATE_DIR/outbox` 收集「這次的 cache 建的全部 ULID」（cache 和 state 都是這個測試專用的，所以這樣收集是安全的） |
| E4 | Medium（待驗證） | 真的 claude 除了 `projects/<編碼>/<uuid>.jsonl` 之外，通常還會依 session 寫出其他檔案，例如 `~/.claude/todos/<uuid>-*.json`、`~/.claude/session-env/<uuid>/`、`~/.claude/file-history/<uuid>/`，以及 `~/.claude.json` 裡以專案路徑為 key 的設定。teardown 只刪了 jsonl、sidecar 和空的專案資料夾。**我自己在 CL1 的實驗也只刪了 jsonl 和資料夾，同樣可能留下這幾類檔案**；那次的 uuid 已經沒有記下來了，所以沒辦法逐一核對 | 用 uuid 對這幾個路徑做 `test -e`（不要列出資料夾）。存在的話，就按 uuid 刪掉。`~/.claude.json` 不要自動改，在 README 寫一句「整合測試會在 `~/.claude.json` 留下 `/private/tmp/agora-it-*` 的專案設定」就好。請 PM 決定我那次實驗留下的檔要怎麼處理（我需要 PM 同意，才能用時間範圍去找，因為那等於是在列出真實的資料夾） |
| E5 | Low | Drive 的 purge 用的是 `capture_output=True`，而且沒有檢查 rc，所以清理失敗時不會有任何訊息。`/tmp/agora-it-e2e/proj` 也只在開始時刪，結束時沒有刪 | rc ≠ 0 時用 `warnings.warn` 印出 ULID；teardown 時順手 `shutil.rmtree(proj)` |

### C-3. e2e：有沒有讀到真實資料的可能

| # | 嚴重度 | 問題 | 建議 |
|---|---|---|---|
| E6 | Medium | 三次 `claude -p` 都只靠 prompt 裡的「不要呼叫任何工具」，**沒有**用旗標限制工具。在 `-p` 模式下，Read／Glob／Grep 這類唯讀工具預設是允許的。模型如果決定去讀 cwd 以外的檔案（例如家目錄），是攔不住的 | wrapper 和 P1 的呼叫都加上 `--disallowedTools "Bash Read Glob Grep Edit Write WebFetch WebSearch Task"`。`@<路徑>` 的展開是 CLI 自己做的，不經過工具，所以注入照樣能用。另外建議加 `--model haiku`，既省成本也比較快 |
| E7 | Low | `AGORA_CLAUDE_HOME` 指向真的家目錄，所以 `export` 會走到 `find_jsonl` 的 glob，掃過真實 `~/.claude/projects/*/` 的目錄名稱（CL12）。`REAL_HOME` 是在 import 的時候從 `HOME` 取的；如果有人在已經隔離 HOME 的環境下執行，就會指到錯的地方（只會失敗，不會讀錯資料） | 用 CL12 的 `hint_dir`，讓 export 直接看 `projects/<encode(proj)>/`。`REAL_HOME` 改用 `pwd.getpwuid(os.getuid()).pw_dir`，和 `test_claude_real.py` 一致 |
| E8 | Low | README 的整合測試指令是直接 `uv run pytest -m integration`，但它會用到真的 Drive 和真的 claude，執行時間也超過共通規則的 90 秒 | README 改成 `nohup uv run pytest -q -m integration tests/integration > it.log 2>&1 &`，並且寫明「會呼叫 `claude -p` 約 6 次、只動 `agora-test/`」 |

### C-4. README／design 和 cli.py 是否一致

| # | 嚴重度 | 項目 | README | design 第 3 版 | cli.py 的實際行為 | 建議 |
|---|---|---|---|---|---|---|
| D1 | Medium | merge 的逗號寫法 | 只寫了用空白分隔 | 沒有寫 | `ids` 用 `,` 切開，空的部分會濾掉，所以 `id1, id2, id3` 可以用 | 這是**使用者原本的指令格式**，README 的範例要改成 `agora merge-session agora:01K6…, agora:01K7…`；design 5.3 也補一行，說明兩種寫法都可以 |
| D2 | Medium | exit code | 沒有寫 | 只寫了 outbox 是 3 | 0＝成功；**1**＝「找不到 id」「沒有任何訊息」「至少要兩個」（這些是 `SystemExit("…")`）；**2**＝HeaderError／StoreError／AgentError／非預期的錯誤，argparse 的用法錯誤也是 2；3＝已存進 outbox | README 加一個小表；design 第 5 節加一行。另外可以考慮把「找不到」也統一成 2（或者把它們都定義成 1），讓腳本好判斷 |
| D3 | Low | `--no-sync` | 沒有寫 | 有寫 | 有實作，而且 `--no-sync` 時 pending 只提示、不補存 | README 補上 |
| D4 | Low | search 不給關鍵字 | 沒有寫 | 寫成必填的 `'<關鍵字>'` | `keyword` 是 `nargs="?"`，不給的話就列出全部 | 兩份文件都寫成「可以省略，省略時列出全部」，或者在 cli 改成必填 |
| D5 | Low | state 裡的檔案 | 只寫了 outbox、pending | 只寫了 outbox、pending，以及 4.2 的 `.bad/` | 另外還有 `outbox/.bad`、`pending/.bad`、`reading/<ulid>.md`（注入用的閱讀版，永遠不會被清掉）、`last-sync` | README 的表補上 `.bad`（以及「每個指令都會提示」）與 `reading/`；`reading/` 要不要定期清理，在 design 決定 |
| D6 | Low | Claude 原生接續要改寫的欄位 | — | 5.4 寫「只改頂層 `sessionId`（其他欄位不動）」 | 同時也改寫了 `cwd`（見 CL7） | 照 CL7 的建議，統一成同一種說法 |
| D7 | Low | 根資料夾的名稱 | 寫的是 `agora/` | 寫的是 `agora/` | 預設是 `agora`，可以用 `AGORA_FOLDER_NAME` 改（整合測試設成 `agora-test`） | README 補一句「測試用 `AGORA_FOLDER_NAME=agora-test`」；design 8 的接縫清單也加上 `AGORA_FOLDER_NAME` |
| D8 | — | 其他都一致 | 六個指令、`--header` 的規則、search 的別名、`show --raw`、檔案位置（`config.json`、cache、state）、5 分鐘節流、import 不節流 | 同左 | 同左 | — |

### 這次跑過的指令（e2e 與文件）

| 指令 | 結果（只記形狀） |
|---|---|
| `git show --stat 3bd4823`、讀 e2e、wrapper、README、design、cli 的 parser | 只做靜態檢查 |
| `HOME=<暫存目錄> FAKE_HOME=<暫存目錄> tests/fakes/claude_noninteractive.py --version` | 印出「unknown command: python3. Perhaps you have to reshim?」（E1）。暫存的 FAKE_HOME 已經刪掉 |

沒有執行 e2e，沒有碰 Drive，沒有呼叫 claude，也沒有讀任何真實的 Session 或 rclone.conf。

---

## D. claude 修正確認（impl2 的 `180d658`，對照 CL1–CL14、E1–E8）

**結論：CL1–CL14、E1–E8 幾乎都修對了，單元測試 98 個全部通過。** 但 E3 的修法帶進了一個新的 **High**（D-1）：e2e 的 teardown 會把 `agora-test/sessions/` 裡**所有**的 Session 都 purge 掉，包括其他次執行、其他測試留下來的。

### 逐條確認

| # | 狀態 | 確認的內容 |
|---|---|---|
| CL1 | ✅ | 改成 `re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(...))`；單元測試有 `/tmp/a_b 專案.v2` → `-tmp-a-b----v2`。fake_claude 改成直接 import adapter 的函式，規則因此一致。這樣 fake 雖然抓不到 adapter 的錯，但參數化測試已經把實測值寫死了，可以接受 |
| CL2 | ✅ | `_is_noise` 排除 `isMeta` 行與本機指令行（`isCompactSummary` 保留）；`system` 行加進了靜默清單；計數和閱讀版都走同一套規則；也有測試 |
| CL3 | ✅ | 單元測試改用 `#!/bin/sh exec {sys.executable}` 的 wrapper。在這台機器上，原本失敗的 2 個測試現在都通過了 |
| CL4 | ✅ | 優先順序是 `AGORA_CLAUDE_HOME/.claude` > `CLAUDE_CONFIG_DIR` > `~/.claude`；單元測試會刪掉 `CLAUDE_CONFIG_DIR` |
| CL5 | ⚠️ 仍然開著 | impl2 用 pty 試了 4 輪，都沒辦法驗證互動模式的行為（spike/claude.md 有記錄），所以 collect 沒有改。這條只能留給 **test-plan 的 M-03 人工確認**：在互動模式下說一句話、執行 `/clear`、再說一句話，然後離開。design 5.4 要先寫一句「`/clear` 之後的內容可能不會存回（未驗證）」 |
| CL6 | ✅ | aux 的 `.jsonl` 共用 `_split_lines`，最後的換行也保留了 |
| CL7 | ✅ | 只改原本就有的 `cwd`，並且加了 `ensure_ascii=False`。**test-plan 的 U-CON-04 要跟著改**（「每一行的 `cwd`」改成「原本有 `cwd` 的行」），design 5.4 的說法也要改。這兩份文件留到下一次一起改（test-plan 是我的檔，這次只能 commit 這個檔） |
| CL8 | ✅ | 非 UTF-8 改成丟出 AgentError；中間的空行直接略過 |
| CL9 | ✅ | `before_count=2` |
| CL10 | ⚠️ | `*_tool_use` 會輸出成工具一行，`*_tool_result` 靜默，也有 WebSearch 的測試。**新的小問題**：如果 block 沒有 `type`，`btype.endswith(...)` 會對 None 丟出 AttributeError（已用 `_block_lines([{"text": "x"}], tools=True)` 重現），最後只會被 main 的 catch-all 接住。改成 `btype = str(block.get("type") or "")` 就好。另外，「連續的 assistant 段落合併」沒有做（base.py 的 `format_reading`），維持 Low |
| CL11 | ✅ | `created_at` 取第一個有 timestamp 的行；`agent_version` 取 jsonl 最後一行的 `version`，subprocess 也刪掉了 |
| CL12 | ✅（大部分） | `find_jsonl(session_id, hint_dir)` 會先看指定的資料夾；collect 會傳 `launch.cwd`；整合測試會傳 `hint_dir`。但是 **cli 的 import 呼叫的是 `agent.export(args.session_id)`，沒有 hint**，所以 e2e 的 import 那一步，仍然會用 glob 掃過真實 `projects/*/` 的目錄名稱（E7 的殘留）。可以在 cli 傳入 `hint_dir=os.getcwd()`，或者讓 e2e 在 proj 底下執行 import |
| CL13 | ✅ | 整合測試的資料夾改成 `/tmp/agora-it-claude/p_專案.v2` |
| CL14 | ✅ | 絕對路徑或含有 `..` 的 aux 路徑，都會丟出 AgentError |
| E1 | ✅ | e2e 改用 `#!/bin/sh exec {sys.executable}` 的 wrapper |
| E2 | ✅ | `show`、`show --raw`、`sync` 都經過 cli.main；merge 用的是 `f"{id1},", id2`（使用者的寫法）。exit 3 與 `--no-sync` 仍然沒有測到，可以接受 |
| E3 | ⚠️ 帶進了新的 High | uuid 改成從 wrapper 的紀錄收集（好）。但 **ULID 是從 `store.Index(paths).known()` 收集的**（第 93 行，註解寫「our cache is test-exclusive」）。這個 cache 的確是這個測試專用的，**但它的內容不是**：search／sync 會把 `agora-test/sessions/` 裡**所有**的 Session 都同步進來。結果 teardown 會把整個共用資料夾裡的每一個 Session 都 purge 掉（見 D-1） |
| E4 | ✅（小問題） | 依 uuid 刪掉 `session-env/<uuid>`、`file-history/<uuid>`，也會刪掉空的 `memory/`。但 todos 用的是 `cfgdir.glob(f"todos/{sid}-*.json")`，這等於列出了真實 `~/.claude/todos/` 底下的檔名（D-2） |
| E5 | ✅ | purge 失敗時會發出 `warnings.warn` |
| E6 | ✅ | wrapper 和整合測試都加了 `--disallowedTools "Bash Read Glob Grep Edit Write WebFetch WebSearch Task"`，wrapper 也加了 `--model haiku` |
| E7 | ✅ | `REAL_HOME` 改成從 `pwd.getpwuid` 取 |
| E8 | ✅ | README 改成 `nohup … > it.log 2>&1 &`，並且寫明大約會呼叫 6 次 `claude -p` |

### 新的問題

| # | 嚴重度 | 位置 | 問題 | 建議 |
|---|---|---|---|---|
| D-1 | **High** | `tests/integration/test_e2e_cli.py:93` | `ulids.update(store.Index(paths).known())` 會把這次 sync 看到的**全部** ULID 都拿去 purge。`agora-test/sessions/` 是好幾次執行共用的（test-plan (b) 的規則 (a)～(d)），所以會刪到：其他次執行留下的 Session、**同時在跑的** `test_store_drive.py`／`test_opencode_real.py` 正在用的 Session，以及 impl1 的測試資料。這違反了「只看、只刪自己的 ULID」與「不准對 `agora-test/sessions/` 全部 purge」這兩條規則，也可能讓同時在跑的測試莫名其妙地失敗 | 只收集「確定是這次建立的」：(1) 每個 `run_main` 的 stdout 第一欄（`run_main` 改成先記錄 id，再做斷言）；(2) outbox 裡的 ULID；(3) 索引裡 `source.session_id` 在這次的 uuid 集合裡的 Session，以及 `parents` 裡有 (1)～(3) 的 merge／continue Session。不要直接使用 `known()` 的全部結果 |
| D-2 | Low | `test_e2e_cli.py:117`、`test_claude_real.py:67` | `cfgdir.glob(f"todos/{sid}-*.json")` 會列出真實的 `~/.claude/todos/` 目錄（雖然只會比對自己的 uuid） | 改成用精確的名字 `todos/{sid}-agent-{sid}.json` 做 `is_file()`。如果實際的檔名格式不同，就照實測到的格式寫死，不要用 glob |
| D-3 | Low | claude.py 的 `_block_lines` | 見上表 CL10：沒有 `type` 的 block 會造成 AttributeError | `btype = str(block.get("type") or "")` |

### 這次跑過的指令（claude 修正確認）

| 指令 | 結果（只記形狀） |
|---|---|
| `git diff 9d1d549 180d658 -- src/agora/agents/claude.py tests/…` | 逐條對照 CL／E |
| `.venv/bin/python -m pytest -q tests/unit` | 98 passed（CL3 修好之後，原本失敗的 2 個也通過了） |
| `.venv/bin/python -c '… _block_lines([{"text": "x"}], tools=True)'` | AttributeError（D-3） |

沒有執行 e2e 和整合測試，沒有碰 Drive，沒有呼叫 claude 或 opencode，也沒有讀任何真實的 Session。
