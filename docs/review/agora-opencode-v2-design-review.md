# agora-opencode-v2 設計審查（非作者）

- 對象：`openspec/changes/agora-opencode-v2/`（proposal、design、specs、tasks；commit 7259492）；issue #51
- 依據：MyLinuxPool spike 1.2 報告（§F'、§G；下稱「報告」）、現有 `src/agora/agents/opencode.py`、`tests/fakes/fake_opencode.py`、`tests/fakes/opencode_noninteractive.py`、`tests/conftest.py`、`src/agora/cli.py`
- 審查者：Claude（作者是 DeepSeek V4.1 Flash）。只讀；沒有讀 `~/.config`、憑證、真實 opencode 資料、真實 Session，沒有連網。

## 結論

**還不能開工。** 有 2 項 High、8 項 Medium、7 項 Low。報告 §F' 的 12 項都有落點、沒有誤解；但 (a) 第 1 步單獨合併會把「大聲的失敗」換成報告 §A 階段 2 的「靜默空閱讀版」，(b) 驗證與改壞驗證的隔離只隔了資料目錄、沒隔埠，會碰到使用者真的 managed 服務。兩項都只是改計畫，不用改架構。

## 1. 報告 §F' 十二項對照

| §F' | 落點 | 判定 |
|---|---|---|
| 1 `_export_bytes` | 1.3、1.4 | 對。`model` 讀 assistant 列 `model.id`、`version` 為 None 都對得上 |
| 2 `_import`（`--directory`） | 1.3 | 對 |
| 3 `reidentify` ＋嗅探 | 1.2、1.5 | 對；見 L1（只嗅探第一列）、L6（fixture 可能沒有參照） |
| 4 `native`／`append_native` | 2.2、2.3 | **漏**：`append_native` 底下一串只懂 v1 的 helper 沒列（M1）；`_import_verified` 的 part 計數也沒人改（M2） |
| 5 `turns`／`_lines_of` | 2.4 | 內容對，**順序錯**（H1） |
| 6 互動模式 DB | 3.1–3.4 | 對；但「v2 優先、v1 不看」可能藏掉舊 Session（M4） |
| 7 `summarize` | 4.1 | 對；但切在 PR 4，見 M7 |
| 8 `_delete` | 4.2 | 同上 |
| 9 `start_native` 零改動 | 非目標 | 只對 `serve` 成立；非 headless 的 `opencode --session <id>` 沒人測（M3） |
| 10 `_session_info`／placeholder | 2.2（未點名函式） | 對，隱含在「`info` 照模板」 |
| 11 XDG（部署面） | design §8 | 對；但只講資料目錄，沒講埠（H2） |
| 12 fixture／fake／docs | 1.1、1.6、1.9、3.1、5.2 | 對；漏了 `tests/conftest.py` 的護欄（M5） |

另：報告 §H 末段說 sidecar `new` 模式的 fallback 也要 (1)(3)(6) 的讀取修——對應 PR 1＋2.4＋PR 3，tasks 沒有明講，建議在 PR 說明記一句。

## 2. 發現

### High

**H1　第 1 步單獨合併「不會更糟」不成立：它重現報告 §A 階段 2 的靜默空閱讀版**
- `tasks.md:3`：「閱讀版與 native 還是舊的（第 2 步補），但不會更糟。」
- `tasks.md:6`（1.2）只在「兩者都不是」時報錯；v2 的讀取（`turns()`／`_lines_of()`）在 `tasks.md:25`（2.4），屬 PR 2。
- 報告 `spike-1-2.md:68`：「import『成功』但**閱讀版是空的**——`turns()` 在扁平格式上挑不到 `info.role`……（**靜默丟內容**）」。
- 失敗情境：PR 1 合併後，在 opencode 2.x 上 `agora import session --agent opencode` 以前是大聲失敗（指令打錯），現在 export 成功、嗅探成 v2、`turns()` 回空 → 存進 Drive 的 Session 閱讀版是空的；接著 `agora merge` 把空材料交給 summarizer，要約可能是編出來的。這比現在糟，而且資料已寫進 store。
- 建議：把 2.4（v2 的 `turns()`／`_lines_of()`）移到 PR 1（它本來就靠 1.1 的 fixture）；或 PR 1 內讓 `turns()` 遇到 v2 先丟 `AgentError`。再加一條 PR 1 的測試：匯入 v2 fixture 後 body 有 user／assistant 段落。

**H2　隔離只隔資料目錄、沒隔埠；改壞驗證 5.5 與 1.1 可能打到使用者真的 managed 服務**
- `design.md:52`：「四個 XDG……也指到暫存……不讀憑證、不碰真 store。」
- `tasks.md:69`（5.5）：「……指令不帶 `--standalone`）→ 5.1 必須紅」，用的是真 opencode。
- 報告 `spike-1-2.md:9`：不帶 `--standalone` 的 `opencode run`「曾試圖啟動 managed 背景服務、**撞上 49374 而 bind 失敗**」；`:79`：預設埠 49374，要改得用 `opencode service set port <port>`。
- 失敗情境：XDG 隔離後埠 49374 仍是全機共用的。spike 剛好只是 bind 失敗；沒有證據說「發現既有服務」的路徑不會直接連過去。5.5 刻意拿掉 `--standalone` 跑真 opencode，就是在驗證這個路徑——若連上使用者真的服務，匯入的測試 Session 會進真 store。
- 建議：(1) 隔離環境第一步就在隔離的 XDG_CONFIG 下 `opencode service set port <空閒埠>`（或等價設定），並在 `design.md` §3、§9 與 1.1、5.1 寫明；(2) 5.5 的「不帶 `--standalone`」只對 fake 做，真 opencode 那一項只做「適配關掉」的其他三項；(3) 1.1／5.1 開跑前後各確認 49374 的服務沒被動（只看有沒有在聽、PID／etime，不讀資料）。

### Medium

**M1　`append_native` 底下的 v1 專用 helper 沒有一項被列進任務**
- `tasks.md:24`（2.3）只說「複製原 raw……接上新訊息列」。
- 這些函式全部讀 `message["info"]`：`opencode.py:387-403`（`_apply_revert` 讀 `info.revert`、`messages[found]["parts"]`）、`:420-439`（`_role_fields` 讀 `info.role`）、`:448-449`（`_unused_stem`）、`:736-739`、`:758`（`said`、`last_question`、`_created_ms(m.get("info"))`）、`:483-488`（`_last_model` 讀 `info.modelID`）。
- 失敗情境：v2 扁平列沒有 `info`，這些全部不報錯、回空／None → fork 的新列沒有 model、`parentID` 鏈指到空字串、`_unused_stem` 認不得既有 id。結果是 import 被 `SchemaError` 拒絕（響亮）或匯入一個鏈不對的 Session（靜默）。
- 建議：2.3 逐個點名這些 helper 的 v2 版本，2.6 加「fork v2 raw：新列的 `model`／鏈指對」的斷言。

**M2　`_import_verified` 的 part 計數在 v2 變成恆等 0=0，spec 的回讀驗證沒人實作**
- `opencode.py:605`：`parts = sum(len(m.get("parts") or []) for m in messages)`；`:608` 同樣讀 `parts`；v2 扁平列沒有 `parts`。
- `specs/session-continue/spec.md:18`：「載入後 MUST 回讀驗證（訊息數與 part 數）」；`tasks.md:27`（2.6）只寫「回讀驗證……仍成立」，沒有任務改它。
- 失敗情境：part 層的比對靜默失效（docstring 自己說「colliding part id leaves the message count intact」）。v2 的 `content[]` 項目有沒有 id 要由 fixture 決定。
- 建議：在 2.x 加一項「v2 的『part』定義（`content` 的項目數，或明講 v2 沒有 part 層、只比列數）」＋測試，並把 spec 這句改成能被滿足的說法。

**M3　非 headless 的 `opencode --session <id>` 與「standalone 匯入 vs 運行中的服務」沒人測**
- 報告 `spike-1-2.md:76`、`:81-82` 只驗了私有 `serve`；`:181`（§G-6）：「生產（使用者正常環境、有既有服務）下的行為未測」。
- `opencode.py:1031`：`argv = [agent_cmd(self.name), "--session", session_id]`；e2e 的替身把它翻成別的指令：`opencode_noninteractive.py:148-151`（`--session` → `run -s`）。所以 5.1 即使全綠，`agora continue` 的 TUI 路徑也沒被跑到。
- 另外 `spike-1-2.md:92` 的 headless 實驗用 `--sync-period 0`；`tasks.md:65`（5.1）寫「SIGTERM 後溫和停下、寫回」沒要求 `--sync-period` > 0 的定期寫回，但 `design.md:134` 的風險欄要靠它。
- 建議：5.1 或另列一條手動驗收：隔離 XDG＋空閒埠的 managed 服務先跑著，`session import --standalone` 之後，用 `opencode --session <id>`（或 attach）確認看得到；5.1 加 `--sync-period` > 0 的定期寫回。

**M4　「v2 優先、v1 後備」在舊 Session 沒被遷移時會把它們從未匯入清單藏起來**
- `design.md:69`：「兩者都在時以 `session_v2` 為準……合併只會列出重複的舊資料」；`design.md:151-152`（Open Q 3、4）：遷不遷、舊表留不留「不影響設計」。
- 失敗情境：如果升級沒把舊 `session` 遷進 `session_v2`，清單只列 v2 → 升級前的所有 Session 在未匯入頁消失、沒有警告；使用者會以為資料沒了。
- 建議：把 Open Q 3、4 變成 3.1 的第一步（在隔離環境量），而不是「開工時量一次」；或改成以 id 聯集（v2 勝）。

**M5　舊 raw 的「重新匯入」補救路徑與「原地更新」宣稱不全；conftest 的護欄在 v2 會靜默失明**
- `specs/session-continue/spec.md:40`、`design.md:82`：「來源還在時原地更新」。但 `cli.py:402`：`if unchanged or not _branched(index, existing[0])`——被 merge／continue 分支過的 Session 會走「保留舊版、另開分支」（`cli.py:408`），不是原地。使用者照訊息做，會得到一個新 Session，舊的仍永遠是 v1 raw。
- 原地更新（`cli.py:395-409`）只擋 `message_count <= 0`，沒有「變少」的保護：v2 的匯出若比 store 裡的 v1 raw 少（遷移丟了東西），v1 raw 被覆蓋。這是 `design.md:85` 說「不是資料遺失」的反例，需實測確認舊 raw 會不會留存。
- `design.md:52`／`:94`：「測試（conftest）……已經在做這件事」。但 `tests/conftest.py:33-39`（`_real_env`）、`:45`（`opencode session list --format json`）、`:73`（`opencode session delete`）是用**真環境**、v1 語法、不帶 `--standalone` 的護欄；`:48-49`（`if proc.returncode != 0: return set()`）在 v2 旗標不同時就回空集合 → 護欄靜默失明，且在 v2 上這兩個指令本身就會去連真的 managed 服務。tasks 沒有任何一項處理 conftest。
- 建議：拒絕訊息改成誠實（可能是新 Session；來源不在本機時只能跨 agent 接續）；PR 2 加「原地更新前後 turns 數不能變少，否則拒絕或另存」的檢查（或明講不做）；PR 1 加一項改 conftest 護欄（v2 語法、`--standalone`、非 0 時要大聲失敗而不是回空）。

**M6　「自建 fake provider」是缺的交付物；真模型路徑讓改壞驗證可能是 skip 而不是紅**
- `design.md:54`、`tasks.md:5`（1.1）：「自建 fake provider（或免費模型）」；報告 `spike-1-2.md:11`、`:176`：fake provider 只有「一句話規模」、工具呼叫未測，真模型被限流；spike 的 fake upstream 在 MyLinuxPool 目錄，不在這個 repo。
- `tasks.md:66`（5.2）：「沒有 opencode 或模型不可用時照舊 skip」；5.5 的改壞驗證若 skip，等於沒驗。
- 建議：(1) 把「能發 tool call 的本機 fake upstream（放 `tests/fakes/`）」列成 1.1 的前置任務，並寫明「做不到時的出口」（先收純文字 fixture、工具形狀記為 open，閱讀版對工具先 `[skip tool]`）；(2) 5.1／5.5 必須用這個 fake，不用免費模型；(3) 5.5 要的是「紅」，不是「skip」。

**M7　PR 切法：`--standalone` 的一半留在 PR 4，讓 PR 1–3 出現混用，且 spec 的 MUST 要等到 PR 4**
- `tasks.md:52-56`（4.1、4.2）；`specs/session-continue/spec.md:21`：「每一個自動化呼叫（匯出、匯入、刪除、寫要約）MUST 帶明確的 server 上下文」。
- PR 1 自己的失敗清理就走 `_discard`→`_delete`（`opencode.py:610`、`:583`），PR 1 合併後匯入帶 `--standalone`、同一條流程的清理卻不帶；`merge` 的 `summarize`（`opencode.py:907`）到 PR 4 前也一直不帶。
- 另外 `tests/fakes/opencode_noninteractive.py` 到 `tasks.md:66`（5.2，PR 5）才改，但 PR 4 已經改了 `run` 的 argv。
- 建議：4.1、4.2（約三行，同一個 `_server_args()`）併入 PR 1；`opencode_noninteractive.py` 的透傳隨 PR 1／4 一起改。PR 3 與 PR 4 在 PR 1 之後可並行。

**M8　`message_count` 含不含 `idle` 列沒有決定**
- `opencode.py:651`（`message_count=len(_payload_messages(payload))`）、`:1047`（`exported.message_count <= launch.before_count`）、`cli.py:393`（`message_count <= 0`）、`cli.py:1378`。
- v2 每一回合結尾有 `idle` 列（報告 `spike-1-2.md:48`）；`native()` 不寫 idle（`tasks.md:23`）。
- 失敗情境：(a) 只是開了 agent 又離開，若 opencode 補一列 idle，`collect()` 會把「沒說話」當成有新內容並寫回；(b) `native()` → import → export 的列數若因 idle 而不同，`_import_verified` 會誤判並刪掉剛匯入的 Session。
- 建議：定義 `message_count` 為「user／assistant 列數」（與 `turns()` 一致），並在 2.6／2.7 各加一個案例；至少在 2.7 用真 opencode 驗一次。

### Low

- **L1　嗅探只看 `messages[0]`**（`tasks.md:6`、`design.md:32`）：混合列的 payload（例如 M1 的 `append_native` 在 v2 raw 上接了 v1 形狀的列）會被當成 v2 通過。建議對每一列檢查同質，不同質就 `AgentError`。
- **L2　去識別化清單太窄**（`tasks.md:5`「一般化路徑」；`design.md:56`）：v2 匯出還有 `projectID`、`providerState`、id、時間戳、`title`（報告 `spike-1-2.md:40-47`）。這是 public repo，建議加一個 grep 清單（`providerState`、`projectID`、HOME 路徑、主機名、email）並以 fake upstream 產生，確保沒有真模型輸出。
- **L3　隔離環境只提 XDG＋HOME**：`opencode.py:534` 的 `_run` 把整個 `os.environ` 傳下去，shell 裡的 `OPENCODE_*` 會漏進測試；agora 自己的 `AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR`、`AGORA_RCLONE`、`RCLONE_CONFIG` 在 pytest 外（1.1 的產 fixture、5.1 的驗收腳本）要自己設（`tests/conftest.py:83-87` 只在 pytest 內）。建議 1.1、5.1 點名這些變數。
- **L4　`--server` 要早點量**（`design.md:45`、`:149`）：若 `--standalone` 與運行中的 serve 衝突，`_server_args()` 的形狀會變；留到 PR 5 才發現代價最高。建議 PR 1 內用隔離環境量一次。
- **L5　PR 1–4 期間真 opencode 的整合測試仍是 v1 語法**（`tasks.md:66`）：在裝了 2.x 的機器上它們不是 skip，而是真的跑（並去撞 49374）。建議 PR 1 加一個語法／版本探測，v2 上先 skip 到 PR 5。
- **L6　改壞驗證有一項可能是空的**：`tasks.md:12`（1.8）「reidentify 少改一個 fixture 上的參照」；報告 `spike-1-2.md:158` 說單輪假資料沒有跨訊息參照。若 fixture 沒有參照，這項改不出紅。1.1 應該加一個斷言：fixture 至少有一個跨訊息參照。
- **L7　「作者提議、本人未指示」一節不完整**（`design.md:115-127`）：已列的都對，缺下列幾項本人需要知道的：(1) PR 1 之後**不再支援 opencode 1.x 的 CLI**（`session export／import` 在 1.x 不存在，報告 `spike-1-2.md:19`；嗅探只保住 payload，沒保住指令，`tasks.md:7` 沒有 fallback；Linux worker 上的版本也未知）；(2) 重新匯入會用 v2 的匯出覆蓋 store 裡的 v1 raw（M5）；(3) `message_count` 的 idle 語意（M8）；(4) conftest 護欄的改動（M5）；(5) 驗收對真模型／fake upstream 的選擇（M6）。

## 3. 其他三問

**一步一個 PR 是否每步都能單獨合併、不讓 main 變更糟**：PR 2、3、4、5 各自成立。PR 1 不成立（H1）。順序 1→2→5 與 3、4 對 1 的相依合理；M7 的調整後，3 與 4 可以在 1 之後並行。

**格式嗅探、v1 讀取保留／寫入只 v2、舊 raw 同 agent 接續拒絕**：讀寫分離的設計本身是對的；拒絕發生在 `start_native`／`append_native` 內（`cli.py:1350-1365`），那時 pending record 還沒寫（`cli.py:1413`），所以「原 Session 與 raw 不被改動」成立。會出事的是：PR 1 與 2 之間的靜默空閱讀版（H1）、`append_native` 的 v1 helper（M1）、part 計數（M2）、補救路徑的宣稱（M5）、1.x CLI 的消失沒被宣告（L7）。

**測試與改壞驗證是否可行、隔離 XDG 對不對**：單元層（fake＋fixture）可行。真 opencode 層：XDG＋HOME 是對的方向但不夠，要加埠（H2）、環境變數清單（L3）、conftest 護欄（M5）、可重現的 fake upstream（M6）。
