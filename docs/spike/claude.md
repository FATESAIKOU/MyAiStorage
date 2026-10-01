# Spike: Claude Code 側驗證（V2 / V3-claude / V5-claude）

impl2，2026-10-01。Claude Code 2.1.286。對應 `docs/design.md` 第 7 節與 review S9/S10 追加題。
測試全在 scratch 目錄 `/tmp/agora-spike-impl2/proj`（git init＋空 commit）用 `claude -p`
跑自編短對話（「CSV 轉 Markdown 步驟」「燈塔守則」等無害內容）；只碰自己建的 uuid 檔。
腳本：`spike/claude/`（`resume-copy.sh`、`fork-continue.sh`、`jsonl-shape.py`）。

## V2：複製 jsonl 成新 id 再 resume —— 可行

做法：`--session-id <uuidA>` 起全新 `-p` 對話 3 輪 → 檔在
`~/.claude/projects/<cwd 編碼>/<uuidA>.jsonl`（cwd `/private/tmp/...` 編碼為
`-private-tmp-agora-spike-impl2-proj`，即絕對路徑的 `/` 換成 `-` 再加前綴 `-`）。
每輪 `--resume <uuidA> -p ...` 都接得上（回答引用前文，檔持續變大）。

- (a) 手動複製：新 uuidB，把 38 行**每一行**頂層 `sessionId` 改成 uuidB，
  `claude --resume <uuidB> -p '你前面在做什麼'` —— **可行**，完整接上 3 輪歷史，
  新 turn 的 `user.parentUuid` 接上舊內容尾端 `uuid`，全檔 sessionId 一致為 uuidB。
  見 `spike/claude/resume-copy.sh`。
- (b) `--resume <id> --fork-session --session-id <uuidC>` —— **可行**，新檔落在同目錄
  `<uuidC>.jsonl`，歷史完整（user 行數與來源相同），原檔完全不動；
  `--fork-session` 不給 `--session-id` 則 CLI 自動配新 uuid。
  `--output-format json` 的 stdout 含 `session_id`（另有 `result`、`total_cost_usd`、
  `is_error`、`usage`、`modelUsage` 等欄），是「結束後知道是哪一份」的機器可讀來源。
- (c) 結論：**啟動時預產生 uuid＋`--fork-session --session-id` 最適合**
  continue-session（路徑啟動前就可知）；次選是解析 `--output-format json` 的
  `session_id`。純 `--resume` 會寫回原檔、不產生新檔，不符合 D4（結果是新 Session）。

## V2-S9/S10 追加

- (a) 附屬資料夾 —— **有條件存在**：純文字／只用過 Bash 的 session 只有
  `<uuid>.jsonl`；用過 Task subagent 的會多出 `<uuid>/subagents/agent-<16hex>.jsonl`
  ＋同名 `.meta.json`（meta 欄位：`agentType`、`description`、`requestNonInteractive`、
  `requestShape`、`spawnDepth`、`toolUseId`）。subagent jsonl 行型只有
  `user/attachment/assistant`，每行 `sessionId` 填的是**父 session uuid**。
  import（design §5.2）遇到有 subagent 的 session 要連目錄一起收，否則丟歷史。
- (b) 複製改寫欄位 —— **只需改每行頂層 `sessionId`**（本版所有行型都有此欄）；
  `uuid/parentUuid` 鏈、`message.id`、`requestId` 保持不動；`cwd` 同目錄時不用動，
  跨目錄見 V5。檔名＝新 uuid，目錄＝cwd 編碼目錄。
- (c) `--resume <id> --session-id <new>`（不加 `--fork-session`）—— **不可行**，
  直接報錯 `--session-id can only be used with --continue or --resume if
  --fork-session is also specified`，且不建檔、原檔不動。
- (d) 進行中讀 jsonl —— **實測無半行**：2 秒間隔輪詢 74 次、逐行 `json.loads`
  全過；streaming 期間檔大小不動、turn 結束才一次 flush。
  但轉換器仍建議防禦性寫法：末行解析失敗就跳過（flush 時機未來可能變）。

## V3-claude：閱讀版注入 —— 可行，最穩是第一則訊息寫路徑

自編 `reading.md`（四條守則，含可驗證暗語），全新 session 第一則訊息問檔內才有的答案：

| 做法 | 結果 |
|---|---|
| 第一則訊息寫絕對路徑（`請先讀 /path/reading.md…`，`--allowedTools=Read`） | 可行：jsonl 出現 `Read` tool_use（`input.file_path`），答案正確 |
| 第一則訊息 `@/path/reading.md` | 可行且**最穩**：零 tool_use 照樣答對（CLI 端展開、不需工具權限，-p 最適用） |
| `--append-system-prompt="$(cat reading.md)"` | 可行、零 tool_use；內容進 system prompt（snapshot 錄一次、resume 沿用），適合短內容，長閱讀版會每輪佔 context |

本版（2.1.286）**沒有 `--append-system-prompt-file`**（只有 `--system-prompt`／
`--append-system-prompt` 純文字版）；`--system-prompt` 會蓋掉預設 prompt，不建議。
建議 continue-session 閱讀版注入預設用「第一則訊息 `@<絕對路徑>`」，短版備援
`--append-system-prompt`。

## V5-claude：session 綁 cwd 編碼目錄 —— 跨目錄 resume 可行，但新檔位置看啟動目錄

- 在目錄 Q 從 P 目錄生的 session 做 `--resume <uuidA> -p`：**成功**，
  有完整歷史；新 turn **寫回 P 目錄原檔**（行數 38→47），Q 目錄不建同名檔；
  新 `user` 行 `cwd` 記 Q，舊行留 P（單檔多 cwd）。
- 在 Q `--resume <uuidA> --fork-session --session-id <uuidH>`：成功，新檔落在
  **Q 編碼目錄** `<uuidH>.jsonl`（全檔 sessionId＝H、`cwd` 全改 Q），P 原檔不動。
- 結論：**continue 時一律在 `--dir`（目標專案目錄）開**——fork 產物的位置＝啟動 cwd；
  plain resume 雖跨目錄能寫，但工具執行目錄與新行 cwd 都會是錯的目錄。

## jsonl 行型與欄位（converter 用；只記形狀）

主檔行型（`spike/claude/jsonl-shape.py` 可重印）：

- `user`：`agentId cwd entrypoint gitBranch isSidechain message parentUuid permissionMode
  promptId promptSource sessionId sourceToolAssistantUUID timestamp toolUseResult
  turnOrigin turnPosition type userType uuid version`；
  `message`＝`{content role}`，純文字時 content 是字串，有工具結果時附 `toolUseResult`。
- `assistant`：`agentId apiBlockIndex attributionAgent cwd effort entrypoint gitBranch
  isSidechain message parentUuid perTurnEffort requestId sessionId timestamp type
  userType uuid version wireToolInputs`；
  `message`＝`{container content context_management diagnostics id
  input_transformations model role stop_details stop_reason stop_sequence type usage}`；
  content block 見過 `tool_use`（`caller id input name type`）與純文字 block。
- `attachment`：`agentId attachment cwd entrypoint gitBranch isSidechain parentUuid
  rendered renderedRole sessionId timestamp type userType uuid version`；
  `attachment`＝`{snapshot type}`（見過 `type`＝file／environment／model／
  instructions／session_context 等，`renderedRole`＝system／user），`rendered` 是 list。
- `queue-operation`：`content operation sessionId timestamp type`。
- `atis-latch`：`atis sessionId type`。`last-prompt`：`lastPrompt leafUuid sessionId type`。
- `cost-state`：`hasUnknownModelCost modelUsage sessionId startTime totalAPIDuration
  totalAPIDurationWithoutRetries totalCostUSD totalDuration totalLinesAdded
  totalLinesRemoved totalToolDuration type`。`mode`：`mode sessionId type`。
- 閱讀版轉換器：照 `parentUuid→uuid` 鏈走 `user/assistant` 行即可；
  `attachment(renderedRole=user)`、`toolUseResult`、`tool_use(input)` 視需求展開；
  `queue-operation/atis-latch/last-prompt/cost-state/mode` 是 bookkeeping，可丟。

## 跑過的指令（形狀記錄）

```bash
claude -p --session-id <uuidA> '<自編短對話>'                                   # V2 建檔
claude --resume <uuidA> -p '<追問>'                                            # V2 接續（多輪）
python3 jsonl-shape.py <uuid>.jsonl                                            # 欄位盤點
./resume-copy.sh <uuidA> <uuidB> && claude --resume <uuidB> -p '<追問>'          # V2-a
claude --resume <uuidA> --fork-session --session-id <uuidC> -p '<追問>'         # V2-b 指定新 id
claude --resume <uuidA> --fork-session --output-format json -p '<追問>'         # V2-b 自動 id＋stdout session_id
claude --resume <uuidA> --session-id <uuidD> -p '<追問>'                        # V2-S9-c：報錯，不建檔
claude -p --session-id <uuidE> --allowedTools=Bash '<用Bash做自編小事>'         # V2-S9-a 工具型
claude -p --session-id <uuidF> --allowedTools=Task,Bash '<派subagent做自編小事>' # V2-S9-a subagent型
claude -p --session-id <uuidV3a> --allowedTools=Read '請先讀 /path/reading.md…' # V3 路徑
claude -p --session-id <uuidV3b> --allowedTools=Read '請讀 @/path/reading.md…'  # V3 @路徑
claude -p --session-id <uuidV3c> --append-system-prompt="$(cat reading.md)" '…' # V3 sysprompt
(cd <Q>; claude --resume <uuidA> -p '<追問>')                                  # V5 跨目錄 resume
(cd <Q>; claude --resume <uuidA> --fork-session --session-id <uuidH> -p '…')    # V5 跨目錄 fork
```

注意：`--allowedTools` 是 variadic，吃掉後面的 prompt——寫 `--allowedTools=Bash`（`=` 式）。

## 附記：`src/agora/agents/claude.py` 實作（2026-10-02，334 行）

照本報告＋N 項實作，單元 17 個全過、整合（真實 3 次 `claude -p`）通過：

- 編碼規則以實測修正：`/`、`.` → `-`，leading `/` 轉完本來就是 `-` 開頭，
  **不要**再加前綴（`/tmp/my-proj.v2` → `-tmp-my-proj-v2`）。
- `start_native` 改寫每行 `sessionId`，`cwd` 逐行覆成 workdir
  （U-CON-04 要求連 bookkeeping 行也有 cwd）；aux 的 `.jsonl` 同樣改寫。
- 真實 2 輪 `-p` 的 user＋assistant 行數是 5 不是 4（resume 疑似多記一 user 行），
  整合測試只斷言「成長＋2」，不寫死行數。
- 需 PM 處理（不改別人的檔）：U-CON-05 說 noop 時「pending 留著」，但現行
  `cli.py` `cmd_continue` 在 `_finish` 回 `None` 後仍會刪 pending——留或刪請 PM 定。

## 附記2：e2e（`tests/integration/test_e2e_cli.py`，2026-10-02）

經 `agora.cli.main` 跑真 Drive（`agora-test/`）＋真 claude，全過（約 70 秒）：
import → search（第一欄即新 id）→ continue 原生（wrapper 收到 `--resume`）
→ merge（逗號寫法）→ continue 注入（wrapper 收到 `--session-id`，回覆引用閱讀版）。
新 Session 的 `relation=continue`、`parents[0].id` 正確、`source.session_id` 為新 uuid。
真實 `claude -p` 共 3 次。cli／store 側沒發現 bug（U-CON-05 pending 留刪問題 PM 已定為刪除）。
小發現：adapter 的 `claude --version` 也走 `AGORA_CLAUDE_CMD`，wrapper 需透傳 `--version`。

## 附記3：CL1–CL14、P 精簡、E1–E8（2026-10-02，claude.py 355 行，src 合計 1840）

單元 27 個全過；整合（中文目錄 `p_專案.v2`＋hint）與 e2e（haiku＋E 修正）各跑一次通過。
真實 `claude -p`：CL5 spike 用 1 次 seed，兩個整合測試各 3 次。

- CL1 可行：`re.sub(r"[^A-Za-z0-9]", "-", abspath)`；fake 改 import 同一函式；
  中文目錄編碼 `-private-tmp-agora-it-claude-p----v2`，session 正常找到。
  emoji（BMP 外）與超長路徑截斷未驗證，留待 V5 spike。
- CL2 可行：`isMeta`、`<command-name>／<local-command-stdout>／<local-command-caveat>`
  開頭的 user 行、`system` 行不計數、不進閱讀版；`isCompactSummary` 保留。
- CL3 可行：單元改 sh wrapper（`exec sys.executable fake`），不再依賴 shebang。
- CL4 可行：`AGORA_CLAUDE_HOME/.claude` ＞ `CLAUDE_CONFIG_DIR` ＞ `~/.claude`；
  單元固定刪掉 `CLAUDE_CONFIG_DIR` 保隔離。
- CL5 **驗證不了**：pty（`pty.fork`＋固定時長＋non-blocking 讀）試了 4 輪——信任框可用
  Down＋Enter 回答；但之後 TUI 零渲染、行程靜默退出、jsonl 行數不變，
  全新 session 與 `--resume` 皆然。`/clear` 完全沒測到。依指示不猜：collect 不改；
  跨目錄找新檔（掃專案目錄）在 adapter 裡也不做（違反不列出規則）。
- CL6 可行：aux `.jsonl` 共用 `_split_lines`（半行丟掉＋警告）並保留尾換行；有單元測試。
- CL7 可行：只改既有 `cwd`＋`ensure_ascii=False`。U-CON-04 需 review 跟著改
  （「`cwd` 都是」→「有 `cwd` 的行才是」）。
- CL8 可行：非 UTF-8 包成帶檔名的 AgentError；中間空行略過。
- CL9 可行：`start_injected` 的 `before_count=2`。
- CL10 可行：`*_tool_use`→`tool_line`、`*_tool_result` 靜默。連續同角色段落合併要改
  `base.format_reading`，請 PM 改（不在我的檔案）。
- CL11 可行：`created_at` 取第一個有的 timestamp；P3 後 `agent_version` 只取 jsonl
  最後一行的 `version`，不再跑 CLI（單元用壞掉的 CMD 證明不呼叫）。
- CL12 可行：`find_jsonl` 先試 hint 目錄再 glob；`export` 加可選 `hint_dir`；
  整合測試傳 proj。e2e 經 cli 無法傳 hint，glob 只會開啟自己 uuid 的檔。
- CL13 可行：見 CL1。
- CL14 可行：aux 的 `..` 與絕對路徑擋成 AgentError；有單元測試。
- P1／P2／P3／P12／P13／P18 照做（`_block_lines`、`_user_text` 吃 `_user_lines`、
  刪 `_agent_version`、`_unpack_aux(rewrite=)`、分派表、一行 regex）。
- E1 可行：e2e 改 sh wrapper。E2 可行：加 `show`、`show --raw`（輸出是文件本身，
  斷言改成含 `id: <id>`）、`sync`＋Index 讀 header、merge 用 `'id1,' 'id2'` 兩參數。
  E3 可行：teardown 從 wrapper log＋自家 cache 索引＋outbox 回收 id。E4 有條件：
  實測每個 `-p` 都會建 `session-env/<uuid>/`（附帶發現），teardown 按 uuid 清
  jsonl／sidecar／session-env／file-history／todos／空目錄；舊 run 殘留的 12 個
  session-env 已按記下的 uuid 清掉，更早成功 run 的 uuid 已無記錄，需 PM 決定才能掃。
  E5 可行：purge 失敗警告＋收掉 proj。E6 可行：全部真 `-p` 加 `--disallowedTools`
 （Bash Read Glob Grep Edit Write WebFetch WebSearch Task）＋haiku；
  `@<路徑>` 展開不受影響（注入步通過）。E7 可行：`pwd` 取 REAL_HOME＋hint
  （e2e 限制見 CL12）。E8 的 README 由 PM 改。
- 跑過的指令（形狀）：`uv run pytest -q`（97 過）；`uv run pytest -q -m integration
  tests/integration/test_claude_real.py`（1 過）；同上 e2e（1 過，約 70 秒）；
  `claude -p --session-id <uuid>` seed 1 次；pty 驅動 4 輪（信任框回答＋靜默退出）；
  按記下 uuid 的 `test -e`＋刪除（session-env 12 個、jsonl、空目錄 `rmdir`）。

## 附記4：code-pm 第 (2) 節缺的測試（2026-10-02，只寫測試不改 src）

新增 `tests/unit/test_store_more.py`（13 個）與 `tests/unit/test_cli_more.py`
（27 個），全套 `uv run pytest -q` 154 過＋1 xfail。`fake_rclone.py` 加了每次呼叫後
快照（`snapshots/<NNN>/` 整份遠端複製）與 `FAKE_RCLONE_DUP`。

- U-HDR-08 可行：經 cli import（claude fixture），`source.dir／host／agent_version／
  source.created_at／header: 1` 都有值；壞 YAML→HeaderError；非 UTF-8 的 session.md
  在 `read_entry` 即包成 HeaderError 進 `.bad/`。
- U-ST：03（逐快照無懸空 raw 指標）／04（缺 raw 不建索引、補上可搜）／07（只剩 raw、
  另一台不建索引）／16（同名→`folder_id` 報錯）／18（無 raw 的 merge 可索引）／
  19（calls.log 無 raw 下載）／20（sessions 不見／列檔失敗都不刪鏡像）／21（重抓
  session.md 再試＋C9）／壞 outbox 進 `.bad`／push 只列自己資料夾——全部可行。
- 指令層：CON-06／08／10／11／11b／13／14／16／17、IMP-08b／09／10、C8、MRG-02b／03、
  SHW-01–03、SRC-01／07／09／10／12／13——全部可行（11／11b／16／17 用真子行程跑
  `python -m agora.cli`；C11／C12 已有測試故未重寫）。
- 1 個 xfail（strict）：開頭是 `-` 的關鍵字（`-x`）被 argparse 當成旗標 exit 2。
  要支援需改 cli（`--` 分隔或自行解析），由 PM 決定。
- 兩個測試寫法修正（非 src 問題）：`show`／`merge` 找不到 id 走 `InputError`→rc 2
  （不是 SystemExit）；`Drive._run` 會在參數前加 `--config`，看 calls.log 要用 `in`。
- 發現但超出本次範圍（請 PM 決定）：① 刪掉 `index.sqlite` 後 `--no-sync` 的 search
  不會從鏡像重建（要等節流過後才從 Drive 重建）；② U-IMP-10 的重複來源靠排序取最新，
  同 `updated_at` 時退化成 ulid 排序。
- 跑過的指令：`uv run pytest -q`（154 passed, 1 xfailed）；單檔重跑若干次。
沒碰 Drive／真 agent／真 Session／rclone.conf。

## 對 design.md 的修改建議

## 附記5：安裝測試（2026-10-02，`tests/integration/test_install.py`）

`uv tool install --editable` 裝進暫存的 `UV_TOOL_DIR／UV_TOOL_BIN_DIR`，裝出來的
`agora`：`--help` 有六個指令；暫存 AGORA 目錄＋`AGORA_RCLONE=/usr/bin/false`
下 `search --no-sync` 回 0、`show` 不存在的 id 回 1；`~/.local/bin` 前後一致。
約 2 秒跑完（快取是暖的）。全套單元 156 過。
沒碰 Drive／真 agent／真 Session／rclone.conf。

1. §5.4「單一 claude→claude 原生」改為 `--resume <id> --fork-session --session-id <新uuid>`
   （官方做法，原檔不動；§5.2 import 同理），手動複製改 sessionId 只當備援。
2. §5.4 步驟 3 加一句：claude 啟動前預產生新 uuid（或解析 `--output-format json`
   的 `session_id`），結束後新 jsonl 路徑＝`~/.claude/projects/<啟動cwd編碼>/<新uuid>.jsonl`。
3. §5.2 claude import 來源：`<id>.jsonl`＋同名 `<id>/` 附屬目錄（subagent 歷史）一起收。
4. §5.3 merge 產物的「接續時才決定怎麼載入」：建議預設走閱讀版注入（第一則訊息
   `@<閱讀版絕對路徑>`），本版無 `--append-system-prompt-file`。
5. §7 V2 加註：`--resume`＋`--session-id` 必須配 `--fork-session`；V5 加註：
   fork 新檔位置＝啟動 cwd，continue 務必在 `--dir` 開。
