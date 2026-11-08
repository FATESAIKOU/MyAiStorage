# #443 設計筆記：codex session 的匯入（agora 支援 codex 或 sidecar 不匯入）

- 單：#443（MAS，層級 c，狀態:待開工）。作者：設計作者（隊員，deepseek-v4.1-flash）。2026-11-08。
- 範圍：**只寫設計**，不寫程式、不改 openspec、不新增 CLI 指令。sidecar 在別的 repo（MyLinuxPool），本 note 只寫「對 sidecar 的要求」，不碰它的程式。
- 限制（本次工作）：不讀 `~/.codex`、不讀 `/run/mlp-codex`、不碰憑證與真資料、不碰真機／真 Drive、不 `git switch`。所有 codex 格式的敘述只來自**公開文件**與**本 repo 既有紀錄**；查不到的標「未查」，沒有樣本就不假裝看過。
- 本人原則：介面簡單、不加多餘機制。

## 0. 結論（先講）

- 建議 **(a)：agora 支援 codex，但第一版只做「匯入／讀取／被別種 agent 接續」，不做 codex 自己的 continue／fork／headless／merge**。這是選 (a) 裡最小的那一塊，也是 #439 角色召回真正需要的部分。
- 不建議 (b)（sidecar 對 codex 不 import、只在 `exit.reason` 說明）當長期方案：它讓 codex 的 Session 永遠進不了 Drive，#439 的「記／找／接」對 codex 角色整條斷掉；它唯一的好處是 agora 零改動，適合當 (a) 的**退路**（取樣或格式確認做不到時）。
- 選 (a) 有一個硬前提：**實作前要有一份（或一次授權取得的）codex rollout 樣本**，用來釘住行格式；公開文件只講到「`rollout-*.jsonl`、放在 `$CODEX_HOME/sessions/`」，沒有行 schema（見 §2）。沒有樣本就不應開工。
- **次序（一句話，§3 與 §9③ 用同一個講法）**：最終＝(a)-import-only；現在的第一件事是取樣本（U-10）。MLP 現在收到的 argparse exit 2 在行為上已經等同「不 import」（Session 留在本機），所以 **(b) 的工程不是 (a) 的前置步驟**：只有確定長期走 (b)（樣本拿不到、或格式不穩到不值得綁）時才去做 MLP 那個分支與 `exit.reason`。若在拿到樣本之前想讓失敗看得懂，可先加一句 `exit.reason`（(b) 的最小部分），那是 MLP 自己的可選項，不改變上面的次序。
- **第一版的價值要說實話（M-2）**：它讓 codex 的 Session **進得了 Drive、找得到**；「接」是**換 agent 接**（claude／opencode 接回同一個 Session），**不是 codex 角色原樣復原**——用 codex 開的角色自己召回不了自己的記憶（§4.6、§8）。

## 1. 現況：agora 目前支援哪些 agent、各自的 import／continue 在哪

### 1.1 一個 agent ＝ 一個 adapter 模組

介面在 `src/agora/agents/base.py:141-203`（`class Agent`）：`export`／`turns`／`native`／`append_native`／`list_sessions`／`last_message`／`search_text`／`summarize`／`start_native`／`collect`。模組頂端就寫明「adding an agent is one new module」（`src/agora/agents/base.py:3-6`）。CLI 只透過這個介面說話，所以「支援 codex」＝新增一個 `src/agora/agents/codex.py` 並把它接進下面幾個列舉面。

| agent | adapter | import（讀 agent 自己的儲存） | continue（載入＋啟動＋回讀） |
|---|---|---|---|
| opencode | `src/agora/agents/opencode.py:891` | `export` `:1061`（叫 `opencode session export`，`_export_file`）、`turns` `:1104` | `start_native` `:1596`（重新編 id、`opencode session import`、回讀驗證；headless 是 `serve --port`，互動是 `--session`）、`collect` `:1632` |
| claude | `src/agora/agents/claude.py:853` | `export` `:860`（直接讀 `~/.claude/projects/*/<id>.jsonl` 與附屬資料夾）、`export_increment` `:866`、`turns` `:911` | `start_native` `:979`（寫 jsonl、`claude --resume <id>`；headless 是 `-p --input-format stream-json`）、`collect` `:1166`、`append_native` `:920` |
| agy | `src/agora/agents/agy.py:152` | `export` `:167`（`$AGORA_AGY_HOME` 的對話 db）、`turns` `:198`、`list_sessions` `:206`、`last_message` `:223`、`search_text` `:253` | `start_native` `:278`（還原 db＋補 summaries 列、`agy --conversation <id>`）、`collect` `:318`；`native`／`append_native`／`summarize` 是拒絕 stub `:267-274` |

agy 是「read-only 加一個方向的 continue」的現成範本：它用常數集中拒絕訊息（`AGY_READ_ONLY`／`AGY_HEADLESS_LATER`／`AGY_CROSS_AGENT`，`src/agora/agents/agy.py:103-120`），而且**拒絕分四層**，每一層都在自己的位置：

1. adapter 自己的 stub（`native`／`append_native`／`summarize`，`src/agora/agents/agy.py:267-274`）；
2. `merge` 的早拒絕：`src/agora/cli.py:813-820`（`load_agent` 之後、讀任何 id 之前）；
3. `continue` 的 fork／headless：`src/agora/cli.py:2211-2219`（`load_agent` 之後，`_refuse_agent_args` `:2223` 之前）；
4. **跨 agent 方向**：`_refuse_cross_agent_source`（`src/agora/cli.py:403-424`），呼叫點 `:2250`（本機索引、同步前）與 `:2252`（同步後、只為 Drive 才有的 id）。

第 4 層要特別說清楚：它**不在** `cmd_continue` 的那一段判斷裡，條件是 `from_agy != (target_agent == "agy")`（`src/agora/cli.py:421-424`）——也就是「恰好一方是 agy」才拒。所以 `continue <codex 來源> --agent claude|opencode` 這個**要放行的方向**不會被它擋；而反過來 `continue <claude 的 Session> --agent codex` **現在不會被擋**（codex 不是 agy），這是本次要補的（§4.4）。另外這個函式的 docstring（`:405-415`）還留著「跨 agent 是 PR3」的舊敘述，實際判斷已只擋 agy，claude↔opencode 的跨 agent 接續是放行的（`src/agora/cli.py:2121-2122`、`:2544-2553`）；文件不要照 docstring 寫。

codex 第一版可以照這個形狀，只是把「同 agent continue」拿掉、改成「只能被 claude／opencode 接」。

### 1.2 CLI 的 agent 清單與 codex 被擋的位置

- 清單：`AGENTS = ("opencode", "claude", "agy")`（`src/agora/cli.py:52`）；`load_agent` 不在清單就 `InputError`（`src/agora/cli.py:125-129`）。
- `--agent` 的 argparse `choices` 直接吃 `AGENTS`（`src/agora/cli.py:2974-2977`）。
- **exit 2 的出處就是這裡**：`--agent codex` 在 argparse 階段被 `invalid choice` 擋掉，argparse 的用法錯誤是 exit 2（`src/agora/cli.py:54-55` 的註解就寫「argparse usage errors are 2 too」）。`cmd_import` 根本沒被呼叫，所以它自己的 `InputError`（exit 1）與例外對照（`src/agora/cli.py:591-596`）都用不到。

本次在隔離環境（`HOME`、`AGORA_*`、`TMPDIR` 都指到暫存目錄）跑了一次探針，原樣輸出：

```
usage: agora [-h] [--external-session-id EXTERNAL_SESSION_ID]
             [--agent {opencode,claude,agy}] ...
agora: error: argument --agent: invalid choice: 'codex' (choose from 'opencode', 'claude', 'agy')
exit=2
```

這證實 sidecar 看到的「exit 2、session 存不回 Drive」不是 import 邏輯壞掉，而是**介面根本不認得 codex**；訊息是 argparse 的英文用法錯誤，不是給人看的 domain 說明。

### 1.3 import 實際做什麼（決定 codex 要補哪些方法）

`cmd_import`：`src/agora/cli.py:563-598`；每一個 id 走 `_import_one`（`:611-700`）。順序是：

1. `agent.export(external_id)`（或 `export_increment`，`src/agora/cli.py:632-640`）→ 拿到 `Exported`（raw、md5、`message_count`、`turns`、`dir`、`title`…，`src/agora/agents/base.py:42-117`）。
2. 沒有訊息就拒絕：`exported.message_count <= 0`（`src/agora/cli.py:641-642`）。
3. 閱讀版：`reading_of(agent, exported)`（`src/agora/cli.py:643`；`turns_of` 優先用 export 帶來的 turns，`src/agora/agents/base.py:313-321`）。
4. 標頭：`_auto_header` → `_actor(agent.name, exported.model)`（`src/agora/cli.py:157-184`）。**`ACTOR` 沒有 codex 會 KeyError**（`src/agora/cli.py:67`、`:184`），所以加 agent 一定要同時加 `ACTOR["codex"]`，否則匯入會變成「非預期的錯誤」exit 2（`src/agora/cli.py:3137-3141`）。
5. 既有同來源的比對：`index.by_source(agent.name, session_id)`、md5 不變就 no-op、內容變少不覆蓋（`src/agora/cli.py:644-692`）。這一段完全與 agent 種類無關。
6. `_save` 存檔、`_emit` 印 `agora:<ULID>`。

所以 **import 只要求 adapter 的 `export`＋`turns`**；`native`／`append_native`／`summarize`／`start_native`／`collect` 是 continue／merge 才用到。

### 1.4 continue 如何挑 adapter（為什麼 import-only 仍能支援 #439 的「接」）

`_load_for_launch`（`src/agora/cli.py:2077-2163`）分三條：

- 目標 agent 與來源 agent 相同 → 同一份 raw 原樣載入（`:2127-2129`）。
- 來源 agent 不同 → `agent.native(merge_turns([*_converted_turns(src_agent, …), …]))`（`:2121-2122`）；`_converted_turns` 只呼叫**來源** adapter 的 `turns()`（`:2544-2553`）。
- merge 出來的 Session → 用目標 adapter 的 `native()`（`:2106-2120`）。

因此：**只要 codex 的 `turns()` 能讀，`continue … --agent claude|opencode` 就能接 codex 匯入的 Session**（走跨 agent 轉換），完全不需要 codex 的 `native()`／`start_native()`。這正是 #439 召回最需要的路徑（找到 codex 的 Session 後，用 claude／opencode 接回來），也是本設計建議「codex 第一版不做自己的 continue」的根據。

注意這條路**今天只受「目標不是 agy」的檢查保護**：`_refuse_cross_agent_source` 只擋 agy（`:421-424`），所以「來源 codex → 目標 claude／opencode」加進 `AGENTS` 之後自然可行；但「目標 codex」也同樣不會被擋，必須補（§4.4）。

### 1.5 其他會被 agent 清單影響的表面（加 agent 不能只改 parser）

| 表面 | 位置 | 不做會怎樣 |
|---|---|---|
| `ACTOR` | `src/agora/cli.py:67` | import 標頭 KeyError（見 1.3 第 4 點） |
| TUI 的 agent 清單與顏色 | `src/agora/tui.py:51`、`:1117` | 未匯入頁看不到 codex；顏色缺一個 |
| 未匯入頁列 Session | `src/agora/tui.py:112-141`（呼叫 `list_sessions`，單一 adapter 失敗只印一行 stderr、`tui.py:124-129`） | codex 的 session 不會出現在清單（可以接受，但與其他 agent 不一致） |
| 點選預覽最後一則 | `src/agora/tui.py:562`（`last_message`） | 預覽空白（可以接受，但最好有） |
| 未匯入頁內文搜尋 | `src/agora/tui.py:1892`（`search_text`） | 搜不到 codex 的對話（若 rollout 有全文，應做；不做就要像 agy 一樣明說範圍，`src/agora/tui.py:184`） |
| `pull`／`push` 的 agent 前綴 | `src/agora/cache.py:108-129`、`:132-179`、`src/agora/cli.py:2819` | `codex:<id>` 不被接受、本機閱讀版快取不能建 |
| 遷移盤點 | `src/agora/migrate.py:26`、`:200-232` | 只影響舊格式盤點，第一版可以不加（見 §4.4） |
| 文件 | `docs/design.md:206-208`、`:218`、`:219-223`、`:269-273`、`:337`、`:526` | 使用者與 sidecar 看到的契約與現況不符（**共 6 處**，不是 5 處） |

## 2. codex session 格式／位置：公開文件能說的、不能說的

### 2.1 已由公開文件確認（可引用）

| 事實 | 來源（公開文件，2026-11-08 讀） |
|---|---|
| `$CODEX_HOME` 預設 `~/.codex`；設定在 `~/.codex/config.toml` | Codex「Config basics」：`https://developers.openai.com/codex/config-basic`（"Codex stores user-level configuration at `~/.codex/config.toml`"）；「Run Codex with an NFS home directory」：`https://developers.openai.com/codex/environments/network-file-systems`（"a Codex home (`~/.codex` by default)"） |
| 對話事件存在 `rollout-*.jsonl`；`$CODEX_HOME/sessions/` 底下 | 同上 NFS 頁："Codex also saves conversation events in `rollout-*.jsonl` files."；"Keep the rollout under `$CODEX_HOME/sessions/` if you need to inspect the job after the worker exits." |
| 另外有一個 SQLite 索引在列對話（路徑未在該頁寫明） | 同上 NFS 頁："The Codex app and terminal UI use a SQLite index to list conversations."；另有 `CODEX_SQLITE_HOME` |
| session id 是 UUID（`thread_id`）且可以 resume | `https://developers.openai.com/codex/non-interactive-mode`：`codex exec --json` 的樣本 `{"type":"thread.started","thread_id":"0199a213-…"}`；`codex exec resume <SESSION_ID>` |
| 互動版也可以指定 id 接續／分叉 | `https://developers.openai.com/codex/developer-commands`：`codex resume`、`codex fork <SESSION_ID>`、`codex exec fork <SESSION_ID>`；resume 遇到 cwd 不同會問要用哪個目錄（`tui.resume_cwd`） |
| `--ephemeral` 不寫可 resume 的 session | non-interactive 頁："It skips saving a resumable session." |
| 官方 `/export` 只輸出 Markdown，不是 on-disk 格式 | developer-commands 頁的 `/export`："Export the conversation as Markdown." |

### 2.2 未查（本設計沒有樣本，不編造）

本 repo 內沒有任何 codex 的紀錄：`grep -rni "codex" --exclude-dir=.git .` 是 0 筆（本 note 寫下之前）。所以下面這些全部**未查**，也沒有一手樣本：

- `$CODEX_HOME/sessions/` **底下的目錄分層**（是否 `YYYY/MM/DD/`）、`rollout-*.jsonl` 的**檔名規則**（時間戳與 UUID 的排列）、**一行 JSON 的 schema**（有哪些 `type`／`payload`、user／assistant 文字與工具呼叫怎麼放、是否含 reasoning／加密內容）。
- SQLite 索引的檔名、表名、欄位；以及「只靠 rollout 檔掃描」能不能列出全部 Session（可能要讀索引才完整）。
- rollout 是否自足（把檔案搬到另一台、放到同一 `$CODEX_HOME/sessions/` 路徑就能 `codex resume <uuid>`）——公開文件只說到 resume 用 id 與 cwd，**未保證**以檔為單位可攜。
- 檔案是否 append-only、有沒有輪替／壓縮、`codex` 執行中讀取是否安全。
- `agent_version`／`created_at`／`model`／`cwd` 是否有可靠欄位。

**設計含意**：這些不是「查一下就好」的細節，而是 `turns()` 與 `list_sessions()` 的實作前提。§0 的硬前提就是為此：**沒有樣本，codex adapter 不應開工**。

### 2.3 可以從公開文件推得的設計約束（推論，標明推論）

- rollout 是 **JSONL**、UTF-8 文字，適合原樣存進 agora 的 raw（`store.copy_atomic(utf8=True)` 只收 UTF-8，`src/agora/agents/agy.py:56` 提到同一個限制）。
- 它是**日誌型**檔案，最自然的增量模型是「前綴不變、尾端追加」，與 claude 的 jsonl 同型（`src/agora/agents/claude.py:866-899` 的 `append_from`／`seal_upto`）。第一版可以不做增量（不做 `export_increment`），整份重傳，等格式穩了再加。
- 同一個 session 的檔案可能很大（工具輸出、reasoning），但 agora 的既有規則就是「raw 原樣、閱讀版不含工具結果」（`docs/design.md` 5.2 的 claude／agy 段），照抄即可。

## 3. 二選一比較

| 面向 | (a) agora 支援 codex（本設計建議：只做 import／讀取／被跨 agent 接） | (b) sidecar 對 codex 不 import，`exit.reason` 明說 |
|---|---|---|
| agora 介面變化 | `--agent codex` 變成合法值；新增一個 adapter 模組；import／show／search／pull／TUI 可用；continue／merge 帶 `--agent codex` 明確拒絕（exit 1） | **零**（不改 `AGENTS`／parser）；若 sidecar 仍呼叫，exit 2 照舊 |
| sidecar 變化 | 不用改（現有的 import 呼叫照舊，exit 0） | 要改：close 流程辨識 codex、跳過 import、寫固定的 `exit.reason`；不要呼叫 `agora import --agent codex` |
| #439 的「記」 | 有：codex Session 進 Drive、可帶 `--add-tag role:R` | 沒有：codex 角色永遠沒有 role tag 的 Session |
| #439 的「找」 | 有：`search --filter tags=role:R` 找得到 codex Session | 沒有 |
| #439 的「接」 | 有，但是**換 agent 接**：找到後用 claude／opencode 接回同一個 Session（只需 codex 的 `turns()`），接完 `source.agent` 變成 claude／opencode；codex 自己接是後續。**用 codex 開的角色自己召回不了自己的記憶**（§8） | 沒有（沒有 Session 可接）；角色只能每次全新開 |
| 主要風險 | rollout 格式未文件化、會隨 codex 版本變；需要一份授權樣本；raw 內含完整對話（與 claude／agy 同等隱私層級） | codex 隊員的記憶與 Session 永久丟失（容器重建就沒了）；#439 對 codex 角色靜默失效，只能靠 `exit.reason` 讓人看到 |
| 成本 | agora 一個 read-only adapter（估計約 300–500 行＋測試）；格式確認一次；文件 **6 處** | agora 0；MLP 一個分支＋測試；之後若要 (a) 仍要重做一次（且舊 Session 已丟） |
| 可驗性 | 可用假 rollout 檔在 temp `CODEX_HOME` 全驗（§7）；fixture 的行格式要取自 U-10 的樣本，否則只驗到自己的猜測 | 只能驗「sidecar 沒有呼叫、`exit.reason` 正確」；agora 這邊沒有行為可驗 |
| 與現有架構 | 完全照 agy PR1／PR2b 的形狀（read-only adapter＋集中拒絕常數） | 繞過 adapter 架構，問題被移到另一個 repo |

**建議理由（依本人「介面簡單、不加多餘機制」）**：

1. (a) 的「只 import」是**既有介面的用法**，不是新機制：`--agent` 已經存在、adapter 介面已經存在、跨 agent continue 已經存在（`src/agora/cli.py:2121-2122`、`:2544-2553`）。新增的只是一個模組與幾處列舉。
2. 它把 #439 的三步都補齊，而且**不用等 codex 自己的 continue**：召回後用 claude／opencode 接，符合 #439 U-9「取最新的合格者、跨 agent 可接」。
3. (b) 的節省只在 agora，代價是 codex 角色的記憶整條消失；而且 `exit.reason` 只是一行字，不會把 Session 變回來。
4. (b) 仍應保留為**退路**：若本人不同意取樣、或樣本顯示格式不穩到不值得綁，就把 (b) 當長期方案（那時才做 MLP 的那個分支），並把「codex 只當短命隊員、不進角色召回」寫成營運約定（#439 U-6 本來就只召回長期角色）。**這條不改變次序**：目標仍是 (a)，(b) 是要裁定「樣本拿不到」時才啟用。

## 4. 建議方案 (a) 的最小介面

### 4.1 CLI 用法（不新增旗標、不新增指令）

```bash
# 匯入（sidecar close 時照舊呼叫，只是 --agent 換成 codex）
agora import session --external-session-id <uuid> --agent codex [--add-tag role:<R>]

# 讀、找、同步：與其他 agent 完全相同
agora show session <agora id> [--raw]
agora search session --filter tags=role:<R> --strict --sort updated --json
agora pull session codex:<uuid>
```

- `--agent codex` 加入 `AGENTS`（`src/agora/cli.py:52`）與 `ACTOR`（`:67`，值 `codex`；有 model 時 `codex/<model>`）。
- **不新增** `--codex-home` 之類的旗標。根目錄的**契約**（M-1b；照 `AGORA_AGY_HOME`／`AGORA_CLAUDE_HOME` 的既有慣例，`src/agora/agents/agy.py:5-8`）：

  > **讀取順序**：`AGORA_CODEX_HOME` → `CODEX_HOME` → `~/.codex`。
  > adapter 只在**每一個**碰 codex 檔案的地方讀這個解析結果（照 agy 的寫法），所以測試指到哪裡、codex 自己的設定是什麼，兩件事分得開。
  > `AGORA_CODEX_HOME` 是 agora 專用的覆寫（不叫 codex 的程式去認），`CODEX_HOME` 是 codex 自己的介面（sidecar／使用者設的），`~/.codex` 是公開文件的預設。

  理由：只用 `CODEX_HOME` 的話，worker 環境裡可能已經設著一個指到共用 codex 目錄的值（例如 `/run/mlp-codex` 那類共用登入），測試只要有一個地方忘了覆寫就會讀到真的目錄；用 agora 自己的覆寫變數，測設隔離（§4.5 第 5 點）與 codex 的設定互不干擾。

### 4.2 adapter 的責任（第一版）

| 方法 | 第一版 | 說明 |
|---|---|---|
| `export(session_id)` | **做** | 在 `<root>/sessions/` 找到該 uuid 的 `rollout-*.jsonl`；raw 原樣（見 4.3）；`message_count` 由 turns 算；缺檔／讀不到 → `AgentError`（exit 2，不是空清單） |
| `turns(raw)` | **做** | 逐行 JSON；`user`／`assistant` 文字與工具呼叫行照 `design 4.4`；認不得的 `type` → `[skip <type>]`；最後一行不完整就丟掉並警告（照 claude S10，`docs/design.md:270`） |
| `list_sessions()` | **做（最小）** | 掃 `<root>/sessions/` 的 rollout 檔：id 從檔名／首行取、`updated_at` 從 mtime 或首行時間、`title` 取第一則 user 文字（截斷）；**是否必須讀 SQLite 索引才完整 → 未查**（§2.2），若不完整就照實說「只列 rollout 檔看得到的」。**S-1 契約**：根目錄或 `sessions/` 不存在 → 回 `[]`，**不丟 `AgentError`**；目錄存在、但 rollout 檔讀不到 → 才丟 `AgentError`。理由：多數機器沒有 codex，丟錯會讓每個 TUI 使用者都看到一行 stderr（`src/agora/tui.py:124-129`） |
| `last_message()` | **做（可選）** | 只讀該檔尾端，回最後一則 user／assistant 文字；做不到就先回 `None`（TUI 預覽空白，不擋功能） |
| `search_text()` | **做** | rollout 有全文，照 claude 的做法掃檔；若第一版不做，必須像 agy 一樣在 `--help`／TUI 明說「只搜標題」（`src/agora/tui.py:184`、`src/agora/agents/agy.py:118-120`） |
| `native()` | **拒絕 stub（要有）** | `raise AgentError(CODEX_CONTINUE_LATER)`；`continue --agent codex` 在 CLI 邊界就擋（4.4） |
| `append_native()` | **拒絕 stub（要有）** | 同上（codex 的 fork／同 agent 接續是後續） |
| `summarize()` | **拒絕 stub（要有）** | `merge --agent codex` 在 CLI 邊界就擋；adapter 自己也拒絕（寫要約要跑 codex headless，第一版不做） |
| `start_native()`／`collect()` | **拒絕 stub（要有）** | 不再寫「不做（可留 stub）」：**S-2**——Protocol 的方法要齊全，萬一有路徑繞過 CLI 的拒絕，結果是 exit 2 加上明確訊息，而不是 `AttributeError`。codex 自己的 continue／headless／fork 是後續；跨 agent 接不需要它們 |
| `export_increment`／`collect_increment` | **不做** | 第一版整份 raw；等確定 rollout append-only 再加（U-6） |

### 4.3 raw 與標頭

- raw：**rollout 檔原樣**（一個位元組都不改），或最小信封 `{"format":"codex-rollout/1","main":[...]}`。建議**原樣**：codex 的 rollout 已經是 UTF-8 JSONL，沒有像 claude 的 aux 需要另存，信封只會多一層解析。若日後要存多檔（例如索引片段），再升級成信封；`store.raw_kind` 只認 chunks manifest（`src/agora/store.py:436-452`），raw 內部格式由 adapter 自己認，改信封不會動到 store。
- `agora.source`：`agent: codex`、`session_id: <uuid>`、`dir`（rollout 有 `cwd` 才填，否則 null）、`agent_version`（有才填）、`created_at`（有才填）（`src/agora/cli.py:187-192`）。
- `generated.by`：`codex` 或 `codex/<model>`（`src/agora/cli.py:182-184`）；`ACTOR` 要有 `codex`。
- 隱私：rollout 與 claude 的 jsonl 同等級（完整對話、工具輸出、可能的 reasoning）；照既有規則上 Drive、照既有規則不進日誌。**不讀 `~/.codex/auth.json` 等憑證檔**；adapter 只碰 `<root>/sessions/`。
- **S-4 先例說明**：讀 `<root>/sessions/` 的是 **sidecar 呼叫的 agora 程式**，不是 agent 自己去讀；這和 agy adapter 預設讀 `~/.gemini/antigravity-cli/` 是同一個先例。worker 的「不准讀 `~/.codex`、`/run/mlp-codex`」紀律**仍然約束所有 agent**（含 U-10 的取樣），不因為 agora 有這個 adapter 而放寬；adapter 也只讀 `sessions/`，不讀 `auth.json`、`config.toml`、SQLite 索引（若 U-4 選只掃 rollout）。

### 4.4 邊界：哪些 codex 操作要明確拒絕

- `agora continue session <id> --agent codex`（含 `--fork`／`--headless`／`--harness-file`／`--agent-arg`）：**exit 1**，在同步、鎖檢查、寫 pending、開 agent 之前。位置照 **S-3**：`load_agent` 之後、`_refuse_agent_args` 之前（`src/agora/cli.py:2210-2223`），這樣帶 `--agent-arg`／`--harness-file` 時看到的也是 codex 那句（`_refuse_agent_args` 在 `:2223`，`--harness-file` 的 `agent.name not in ("claude","opencode")` 在 `:2236`，兩個都更晚）。訊息固定、集中一個常數：

  ```
  codex 第一版只支援 import／讀取（#443）；接續 codex 匯入的 Session 請用 claude 或 opencode（跨 agent，會寫回同一個 Session）
  ```

- `agora continue <claude|opencode 的 Session> --agent codex`：**拒絕**（同一句）。這條要落在 **`_refuse_cross_agent_source`**（`src/agora/cli.py:403-424`，呼叫點 `:2250`／`:2252` 不變）：現在的條件只看 agy（`:421-424`），所以這個方向**今天不會被擋**；要加一條 `target_agent == "codex"` → `raise InputError(CODEX_CONTINUE_LATER)`。這也是 M-2 那句話在程式裡的落點（「codex 目標接不了任何來源」）。邊界（上一條）會先擋同一個情形，兩層並存和 agy 的四層拒絕同型（§1.1），不是新機制。
- `agora continue <codex 匯入的 Session> --agent claude|opencode`：**允許**。這是 #439 的召回路徑，現有程式就會走 `_converted_turns`（`src/agora/cli.py:2544-2553`）；測試要釘住。**helper 那條規則不能誤擋這個方向**（來源是 codex、目標不是 codex → 放行）。
- `agora merge --agent codex`：**exit 1**，在讀任何東西之前（照 agy，`src/agora/cli.py:813-820`）：`codex 第一版不寫要約；請用 opencode 或 claude`。
- merge 的**來源**是 codex Session：**允許**（只讀 `turns()`，與 claude／opencode 當來源一樣）。
- `migrate`：第一版**不加** `codex` 到 `AGENT_NAMES`（`src/agora/migrate.py:26`）；它只影響舊格式盤點，不影響新匯入。列為 U-7。
- **S-5：兩處人話提示要一起改**（否則使用者會以為 codex 能用）：
  - `src/agora/cli.py:815`，現況原樣：`raise InputError("merge 要給 --agent opencode|claude（由誰來寫要約；agy 第一版不支援）")` → 改成 `"merge 要給 --agent opencode|claude（由誰來寫要約；agy／codex 第一版不支援）"`。
  - `src/agora/cli.py:2208-2209`，現況原樣：`if not args.agent:` → `raise InputError("continue 要給 --agent opencode|claude|agy")` → 改成 `"continue 要給 --agent opencode|claude|agy（codex 第一版只支援 import／讀取）"`。
  - `src/agora/cli.py:2236` 的 `--harness-file` 對 codex 已自然拒絕（`agent.name not in ("claude","opencode")`），**無需改**；但 codex 會在更早的邊界被擋，走不到那裡。
  - `src/agora/cli.py:2974-2977` 的 `--agent` help 文字要加一句 codex 的範圍（現在只寫 agy）。

### 4.5 要改的檔案（實作清單，供日後開單）

1. 新檔 `src/agora/agents/codex.py`：adapter（`export`／`turns`／`list_sessions`／`last_message`／`search_text`）＋五個拒絕 stub＋集中常數（`CODEX_CONTINUE_LATER`、`CODEX_MERGE_LATER`、`SEARCH_SCOPE` 若需要），根目錄解析只在這裡（§4.1 的契約）。
2. `src/agora/cli.py`：`:52`（`AGENTS`）、`:67`（`ACTOR`）、`:813-820`（merge 拒絕，含 `:815` 的提示文字）、`:2211-2223`（continue 的 codex 目標拒絕，S-3 的位置；含 `:2208-2209` 的提示文字）、`:403-424`（`_refuse_cross_agent_source` 加 codex 目標規則；呼叫點 `:2250`／`:2252` 不變）、`:2974-2977`（`--agent` 的 help 文字）。
3. `src/agora/tui.py`：`:51`（顏色；可選）、`:1117`（agent 清單）、`:184`（搜尋範圍說明，若 `search_text` 第一版不做）。
4. 測試：`tests/unit/test_agent_codex.py`（新）、`tests/fixtures/codex/`（假 rollout，格式取自 U-10 樣本）；跨 agent 的案例加在 `tests/unit/test_continue_*` 或新檔。
5. **測試隔離（M-1a，必須和 adapter 同一個 PR 或更早）**：
   - `tests/conftest.py` 的 `isolated_home`（`:121-177`）：**無條件**設 `AGORA_CODEX_HOME` 到該測試的暫存目錄（照 `AGORA_AGY_HOME` 的位置 `:176`，但**不要**放進 `if "integration" not in request.keywords` 裡面——沒有任何測試該讀真的 codex 目錄），並 `monkeypatch.delenv("CODEX_HOME", raising=False)`（外面的 shell 可能設著，照 `ANGAREION_IDENTITY`／`OPENCODE_CONFIG_CONTENT` 的處理，`:140`、`:149`）。
   - `openspec/specs/test-isolation/spec.md`：在「測試 helper 只在隔離的環境執行」加一條 requirement／scenario：`isolated_home` MUST 把 codex 的根目錄（`AGORA_CODEX_HOME`）指到暫存目錄並清掉 `CODEX_HOME`，測試 MUST NOT 讀寫真的 `~/.codex`。
   - `AGENTS.md` 的變數清單：`:27`（`isolated_home` 的清單）加 `AGORA_CODEX_HOME`（設）與 `CODEX_HOME`（清）；`:28`（沒有 pytest 的探針清單）加同樣兩個。
6. 文件：`docs/design.md:206-208`、`:218`、`:219-223`、`:269-273`、`:337`、`:526`（新增 codex 段與拒絕範圍；共 6 處）。

### 4.6 對 sidecar（MyLinuxPool）的要求

**(a) 選定時：close／import 不用改。** sidecar 現有的 close → `agora import … --agent <agent> --external-session-id <sid>` 照舊；codex 的 `--agent` 就是 `codex`，exit 0，stdout 最後一行是 `agora:<ULID>`。**證據（推論，但很強）**：sidecar 現在看到的錯誤是 `invalid choice: 'codex'`，這只有在它已經送了 `--agent codex` 時才會出現；所以它已經會把 codex 的 id 送進 import，只差 agora 不認得這個值。加進 `AGENTS` 之後同一個呼叫就會成功，MLP 的 close 流程不必改。

要請 MLP **確認或保證**的三件事（第 1、2 條是原本就有的，第 3 條是 M-1(c) 補的；未讀 MLP 程式，所以是要求不是描述）：

1. close 流程不要因為「agent 名字不在某個白名單」而跳過 import。
2. 不要解析 agora 的英文 stderr 當契約（exit code 才是契約，`docs/design.md` 對 exit 4 已是這個慣例）。
3. **`agora import` 讀的根目錄，必須和那個 codex agent 執行時的 `CODEX_HOME` 是同一個**。sidecar 若替每個 agent 設不同的 `CODEX_HOME`，跑 import 時要帶同一個值（或把 `AGORA_CODEX_HOME` 設成同一個目錄），否則 `export` 會找不到檔（`AgentError`，exit 2）。這條是風險也是要求：worker 環境說明提到共用登入在 `/run/mlp-codex`、`~/.codex`，但**沒有說** sidecar 起的 codex 隊員用哪一個 `CODEX_HOME`（未讀 MLP），所以不能假設它們一致。

**範圍（M-2，照實寫）**：選 (a) 之後，**codex 的 Session 進得了 Drive、`search` 找得到**；「接」是**換 agent 接**——用 claude／opencode 開同一個角色，接回那個 Session（`source.agent` 變成 claude／opencode，`previous_sources` 記 codex）。**用 codex 開的角色，自己召回不了自己的記憶**：初篩的目標是 codex，而 codex 目標接不了任何來源，所以每次都全新開，之後再 import。這不會失敗，只是那個角色永遠從空白開始。若 sidecar 之後真的要用 #439 召回把「找到的 codex Session」當 `open` 的 `session` 再開一個 **codex** agent，那會走 `agora continue … --agent codex`，第一版 exit 1（§4.4）；所以召回要麼（i）用 claude／opencode 接（跨 agent），要麼（ii）等 codex 自己的 continue（U-2(ii)）。這不影響 close／import，但影響「召回後用什麼 agent 開」。

**(b) 選定時：**

- codex agent 的 close **不要呼叫** `agora import --agent codex`。
- `exit.reason` 用一個固定、機器可讀的值（例：`codex-import-unsupported`，實際字串由 MLP 與 PMO 定），並附一句人看得懂的說明：「codex 不匯入（#443 (b)）；session 只留在本機，容器重建會消失」。
- 保留該 codex session id 與路徑到日誌，方便日後改走 (a) 時補匯入（但檔案可能已被清掉，不保證）。
- **不要**替 codex 角色寫 `role:` tag（沒有 Session 可標，寫了會讓 #439 的召回找錯）。

### 4.7 模組切割、依賴方向、SOLID 逐項（M-3）

**模組切割**：只新增 `src/agora/agents/codex.py`，負責 codex 格式的讀取（`export`／`turns`／`list_sessions`／`last_message`／`search_text`）、根目錄解析與拒絕常數／stub。它**不上傳**（那是 `store`／`cache` 的事）、**不寫 codex 的儲存**（不寫 `sessions/`、不寫索引、不還原 db）、**不讀 `sessions/` 以外的檔**（`auth.json`、`config.toml`、SQLite 索引——若 U-4 選只掃 rollout）。

**依賴方向**：`cli`／`cache`／`tui` → `agents.codex` → `agents.base`；`agents.codex` **不 import** `cli`／`tui`／`cache`，與 agy 同型、不成環（`base.py:3-6`「adding an agent is one new module」）。

**SOLID 逐項**：

- **單一職責**：codex 的格式知識只放在 `agents/codex.py`；`cli` 不知道 rollout 的一行長什麼樣，只知道「這個 agent 是唯讀、目標接不了」。
- **依賴反轉／方向**：CLI 依賴 `Agent` Protocol（`base.py:141-203`），不依賴 codex 的實作；新模組由 `load_agent` 用 `importlib` 依名字載入（`cli.py:125-129`）。
- **開放封閉**：新增 agent 不改既有 adapter；要改的是列舉面（`AGENTS`／`ACTOR`／TUI 顏色）與兩處依名字的拒絕分支。若之後有第三個「不能當目標」的 agent，再把名字分支收成 adapter 宣告的屬性（照 `restores_in_place` 的做法，`cli.py:2139-2140`）；這次照 agy 的先例分支即可，不先加機制（S-3）。
- **介面隔離（Liskov／契約）**：codex 只實作 `Agent` Protocol；不支援的操作一律用**拒絕 stub** 補齊並丟 `AgentError`（集中常數），不是留空、不是 `NotImplementedError`、也不是 `AttributeError`（S-2）。`list_sessions` 的契約另立（根目錄不存在回 `[]`，存在但讀不到才丟錯，S-1）。
- **契約編號**：§7.1 的 12 項＝**C-1～C-12**，實作報告逐條對到測試名稱。

## 5. 風險與代價（選 (a) 時要接受的）

1. **格式未文件化**：`turns()` 依賴 rollout 行 schema；codex 版本一改就可能讀不到。緩解：raw 原樣保存（最壞情況只是閱讀版變差，Session 還在 Drive）、未知行 `[skip]`、把 `agent_version` 記進標頭、測試用固定 fixture。**這是本設計最大的不確定性。**
2. **列舉可能不完整**：若 codex 的 Session 清單只存在 SQLite 索引，單靠 `sessions/` 掃檔可能漏列。第一版接受「只列 rollout 檔看得到的」，並在文件與 TUI 說明；要不要讀索引列 U-4。
3. **隱私**：rollout 有完整對話與工具輸出；上 Drive 的風險與 claude／agy 相同，沒有新增機制，但要在文件寫明（`docs/design.md` 的 import 段）。讀 `sessions/` 的是 sidecar 呼叫的 agora 程式，不是 agent 自己讀（S-4）；「不准讀 `~/.codex`」的紀律仍約束所有 agent，含取樣（U-10）。
4. **匯入時 codex 還在寫**：rollout 可能正在追加。照 claude 的規則「最後一行不完整就丟掉並警告」，並在檔案大小／mtime 前後不一致時重試一次（推論，需樣本驗）。
5. **跨 agent 接續的品質**：codex 的 turns 轉成 claude／opencode 的 native 後，工具呼叫只剩一行摘要（既有 `design 4.4`），不是無損搬移。這是既有跨 agent 的限制，不是 codex 特有；文件要說清楚。
6. **不做 codex 自己的 continue**：codex 角色召回後若堅持用 codex 接，會 exit 1；PM 要改用 claude／opencode（或等後續）。這是刻意的範圍，不是 bug。
7. **sidecar 起的 codex 可能用不同的根目錄**（M-1c 的風險面）：agora 以 `AGORA_CODEX_HOME` → `CODEX_HOME` → `~/.codex` 找 `sessions/`；若 sidecar 開 codex 時用的 `CODEX_HOME` 和呼叫 import 時的環境不同，`export()` 會找不到檔（exit 2）。緩解只有一條：MLP 保證兩者一致（§4.6 (a) 第 3 條）；agora 這邊不猜、也不去掃別的目錄。

## 6. 未決項（需本人／PMO 裁定）

| # | 問題 | 選項 | 建議 |
|---|---|---|---|
| U-1 | (a) 還是 (b) | (a) agora 支援 codex（只 import）；(b) sidecar 不 import＋`exit.reason` | **現在先做什麼**：取樣本（U-10）；MLP 的現況（argparse exit 2、Session 留本機）在行為上已等同「不 import」，所以 (b) 的工程**不先做**（要讓人看得懂可自己加一句 `exit.reason`，非前置）。**最終做什麼**：**(a)-import-only**，理由見 §3；只有樣本拿不到、或格式不穩到不值得綁時，才把 (b) 當長期方案 |
| U-2 | (a) 第一版範圍 | (i) 只 import／讀取／被跨 agent 接；(ii) 加 codex 自己的 continue（`codex resume`／`codex exec resume`）；(iii) 全做（fork／headless／merge 寫要約） | **(i)**；`start_native` 要處理 cwd 不同時的 resume 詢問（公開文件明說會問），是另一個設計題。**若 U-3 的答案是「codex 角色要用 codex 接」，就必須是 (ii)，(i) 不夠** |
| U-3 | codex 會不會擔任長期角色（#439 召回對象），而且要不要用 codex 接 | (a) 會，且用 claude／opencode 接；(b) 會，且要用 codex 自己接；(c) 只有短命隊員 | 請 PMO／本人確認目前的池怎麼用 codex；本設計不猜。**這題直接連到 §4.6／§8**：(a) 第一版夠用（接＝換 agent 接）；(b) 第一版不夠，U-2 必須 (ii)；(c) 那 (b) 也可接受 |
| U-4 | 清單要不要讀 codex 的 SQLite 索引 | 只掃 rollout 檔；或讀索引補齊 | 第一版**只掃 rollout 檔**，不夠再說；讀索引是另一個未文件化格式 |
| U-5 | 根目錄用哪個變數 | 只用 `CODEX_HOME`；或加一個 agora 專用的 `AGORA_CODEX_HOME` 覆寫 | **加 `AGORA_CODEX_HOME`，讀取順序 `AGORA_CODEX_HOME` → `CODEX_HOME` → `~/.codex`**（M-1b）。照 `AGORA_CLAUDE_HOME`／`AGORA_AGY_HOME` 的既有慣例；測試隔離才不會被 worker 的 `CODEX_HOME` 影響（§4.1、§4.5 第 5 點） |
| U-6 | 要不要做增量（`export_increment`） | 第一版整份；或照 claude 做前綴增量 | **整份**；等確認 rollout append-only 再加 |
| U-7 | `migrate` 要不要認 codex | 加 `AGENT_NAMES`；不加 | 不加（第一版不影響新匯入） |
| U-8 | (b) 的 `exit.reason` 字串與可見性 | 固定代碼；或只寫人話 | 由 MLP 與 PMO 定；本設計只要求「固定、機器可讀、人看得懂」 |
| U-9 | codex 的 raw 要不要包信封 | 原樣；或 `codex-rollout/1` 信封 | **原樣**（少一層；日後需要多檔再升級） |
| U-10 | 取樣由誰做、怎麼做 | 本人在有 codex 的機器上取一份去識別化 rollout；或在隔離容器跑一次 codex 產生 | 需要本人單次授權（真機規矩）；**不要**由隊員自行讀 `~/.codex`。這份樣本同時是 §7 fixture 的唯一合法來源 |

## 7. 可驗的完成判準（日後實作怎麼驗，全部用假資料）

測試照 `AGENTS.md` 的隔離規則：`tests/conftest.py` 的 `isolated_home` 會把 `HOME`、`AGORA_*` 指到暫存目錄，**並且（M-1a）把 `AGORA_CODEX_HOME` 指到同一個暫存目錄、清掉外面的 `CODEX_HOME`**——codex 的測試不再各自設根目錄。測試模組第一行 `import _guard`。**不連真 Drive**（`AGORA_RCLONE=tests/fakes/fake_rclone.py`、`AGORA_UPLOAD=inline`）、**不讀真的 `~/.codex`**（用一個記錄開啟路徑的 hook 斷言，照 `tests/unit/test_agent_agy.py:855-859` 的做法）。

**fixture 的來源（硬條件，C 判準的一部分）**：C-1／C-3／C-4／C-5／C-8／C-9／C-11 的假 rollout，**行格式必須取自 U-10 授權取得的去識別化真樣本**（同一份樣本的複本：改內容、不改結構）。**在樣本取得之前，這 7 項不得宣告通過**——自編 fixture 只驗到自己的猜測（§2.2 全部未查），不是驗 codex 真的寫得出那種行。

### 7.1 選 (a) 時（agora 側）

1. **C-1 匯入成功**：在 `<AGORA_CODEX_HOME>/sessions/<假路徑>/rollout-….jsonl` 放一個 rollout（格式取自 U-10 樣本；含 user、assistant、一個工具呼叫、一個認不得的 type）→ `agora import session --external-session-id <uuid> --agent codex` → exit 0、stdout 最後一行 `agora:<ULID>`；`show` 的標頭 `source.agent=codex`、`source.session_id=<uuid>`、`generated.by=codex`；raw 的 md5 與 fixture 檔相同（原樣、不改位元組）；閱讀版有 user／assistant 文字與 `[tool]`，認不得的 type 是 `[skip …]`。
2. **C-2 沒有訊息**（先把「空檔」定義清楚）：檔**存在、是可解析的 JSONL**，但沒有任何 user／assistant 文字（0 位元組、或只有認不得的行）→ `export` 成功、`message_count=0` → 既有 `cli.py:641-642` 的 exit 1「沒有任何訊息，不匯入」。檔**不存在、讀不到、或整檔不是 JSONL** → `AgentError` → exit 2（C-11）。分界是「讀得到且解析得出來」；空檔不是 export 失敗。
3. **C-3 重匯不變**：同一個 fixture 再匯一次 → 同一個 agora id、不新增版本（`src/agora/cli.py:661-664`）。
4. **C-4 變少不覆蓋**：fixture 改成比較少內容再匯 → exit 1，訊息是既有的「內容比原本少」（`src/agora/cli.py:681-688`）。
5. **C-5 被跨 agent 接**：`agora continue <codex 匯入的 Session> --agent claude`（用 `tests/fakes/fake_claude.py`）→ exit 0，寫回同一個 agora id、`source.agent` 變成 claude、`previous_sources` 有 codex（照 `docs/design.md` 5.4 第 6 點）；`--agent opencode` 同理。**這條是 #439 的關鍵驗收。**
6. **C-6 拒絕**：`continue … --agent codex`（含 `--fork`／`--headless`／`--harness-file`／`--agent-arg`）與 `merge … --agent codex` → exit 1、固定訊息，且**沒有**同步／開 agent／寫 pending。**這三件事要新增斷言，不能照抄既有測試**：`tests/unit/test_agent_agy.py:706-718` 原樣只斷言 `code == EXIT_INPUT`、`not out`、訊息在 err 三件（`assert code == cli.EXIT_INPUT`／`assert not out`／`assert message in err`），**沒有**斷言「沒同步／沒開 agent／沒寫 pending」。要另外加：fake rclone（`tests/fakes/fake_rclone.py`）的呼叫紀錄為空、假 agent 的呼叫紀錄為空、`<state>` 底下沒有 `pending/*.json`。
7. **C-7 反方向拒絕**：`continue <claude 的 Session> --agent codex` → exit 1 同訊息。走 `_refuse_cross_agent_source`（`src/agora/cli.py:403-424`，呼叫點 `:2250`／`:2252`）新增的 codex 目標規則；現況該函式只看 agy（`:421-424`），所以這條現在會紅，是新增行為。
8. **C-8 merge 來源是 codex**：`merge session <codex 的> <claude 的> --agent claude`（用假 claude 寫要約）→ exit 0；來源清單有 codex。
9. **C-9 TUI／cache 三個表面**（照 `tests/unit/test_agent_agy.py:867-905`）：未匯入頁列出 fixture 的 codex session；內文搜尋找得到 rollout 裡的 body 字串；`cache.pull` 的 `codex:<id>` 產生本機閱讀版 `.md`，第二次是 skip。**另加 S-1**：根目錄不存在、或根目錄在但 `sessions/` 不存在 → `list_sessions()` 回 `[]`（不是 `AgentError`），TUI 的未匯入頁不因此印 stderr 行；`sessions/` 存在但某個 rollout 讀不到 → 丟 `AgentError`。
10. **C-10 統一隔離（不碰真家目錄）**：驗的是 **conftest 的統一隔離**，不再靠每個測試自己設——斷言 `os.environ["AGORA_CODEX_HOME"]` 在 `tmp_path` 底下、`CODEX_HOME` 不在環境裡；另開一個 hook 記錄所有被打開的路徑，斷言沒有 `<真實 HOME>/.codex`、沒有 `auth.json`（照 agy 的 `tests/unit/test_agent_agy.py:855-859`）。再加一條：把外面的 `CODEX_HOME` 設成一個不存在也碰不到的路徑（`monkeypatch.setenv`），斷言 adapter 仍只看 `AGORA_CODEX_HOME`。
11. **C-11 格式壞掉**：fixture 最後一行截斷 → 丟掉並在 stderr 警告一次，exit 0（照 claude S10）；整檔不是 JSONL → `AgentError`（exit 2），不存半個 Session。
12. **C-12 假 secret 不入檔**：fixture 不放符合掃描器樣式的字串（`AGENTS.md` 的測試資料規則）。

### 7.2 選 (b) 時（不在本 repo）

- MLP 的測試（假 agora／假 agent）：close 一個 codex agent → **沒有** `agora import` 呼叫；`exit.reason` 等於約定的固定值；PM 看得到說明。
- agora 這邊**沒有行為可驗**；頂多把現況釘成一個記錄性測試（`--agent codex` → exit 2、argparse 訊息），但那是記錄症狀，不是修正，建議不寫。

## 8. 與 #439 的關係（一句話版）

#439 的三步是「記（import 帶 `role:` tag）→ 找（`search --strict`）→ 接（continue，可跨 agent）」。選 (b) 等於 codex 角色的「記」直接不存在，後兩步無從發生；選 (a) 的 import-only 則「記」與「找」成立，而「接」是**換 agent 接**：用 claude／opencode 開同一個角色，接回那個 codex Session（寫回同一個 agora id、`source.agent` 換成 claude／opencode、`previous_sources` 記 codex），不必等 codex 自己的 continue。**所以第一版的價值要這樣講**：codex 的 Session 進得了 Drive、由 claude／opencode 開的同角色接得回；**不是** codex 角色原樣復原、也不是 codex 角色自己召回（用 codex 開的角色，初篩目標是 codex，接不了任何來源 → 每次全新開，之後再 import）。**這是本設計比 (b) 多出來的價值，也是它唯一比 (b) 多的價值。**

**要帶到 #439 與 MLP 的一句話**（#439 第 2 步初篩規則的補充，現在寫的是「同 agent，或 claude↔opencode；agy 只能接 agy」，`docs/design-notes/439-role-session-recall.md` §3）：**codex 來源可以被 claude／opencode 接；codex 目標接不了任何來源。** 這句由 PM 帶到 #439 與 MLP 的 U-14 對接。

#439 U-9 已定「跨 agent 接續合格、取最新的合格者」（`439-role-session-recall.md:94`），但沒有處理「角色本身綁哪個 agent」——上面那句補的就是這個缺口：合格者的判斷要加「目標 agent 接得了來源」這一維，codex 當目標時沒有任何來源合格。

## 9. 專家視角四問

### ① 頂尖專家會怎麼做

- 先把「**存檔**」與「**接續**」拆開：存檔只需要讀，接續才需要寫回 agent 的儲存。大多數人會先做存檔（本設計的 (a)-import-only），因為那是不可逆的資料保存；接續可以之後再說。
- 先把**格式不確定**變成可管理的風險：raw 原樣保存、解析失敗只降級閱讀版、把 agent 版本記下來。專家不會讓「解析器不認得新格式」變成「Session 丟掉」。
- 用**一份授權樣本**釘住格式，而不是讀使用者的家目錄；測試用固定 fixture，正式路徑用環境變數隔離。

### ② 我們缺什麼

- 缺 codex rollout 的**一手樣本與 schema**（§2.2 全部未查）；沒有它，adapter 只能靠猜，§7 的 C-1 等項也不能宣告通過。
- 缺「codex 在本池擔任哪些角色、要不要用 codex 接」的事實（U-3）；這決定 (a)/(b) 的急迫性，也決定 U-2 是不是 (i) 就夠。
- 缺 sidecar close 流程對「不支援的 agent」的既有處理、以及它給 codex 隊員的 `CODEX_HOME`（MLP 未讀），所以 §4.6 只能提要求，不能斷言它現在長什麼樣。
- 缺「匯入後跨 agent 接續的品質」量測：工具呼叫只剩摘要（既有設計），codex 的 rollout 可能有很多這種內容；沒有實際資料可評估。

### ③ 怎麼補

- 由本人在有 codex 的機器上取一份去識別化 rollout（U-10），放進 `tests/fixtures/codex/`，先寫一個只驗「能解析出 user／assistant／工具呼叫」的測試，再寫 adapter。**這是現在的第一件事**（次序見 §0）。(b) 的工程不是它的前置：MLP 現在的 argparse exit 2 在行為上已經等同「不 import」。
- 若在拿到樣本之前就要讓 codex 隊員的失敗看得懂，那是 MLP 自己的一行 `exit.reason`（(b) 的最小部分），和 (a) 不衝突，也不是 (a) 的步驟。**真正的長期方案只有一個**：樣本到 → (a)-import-only；樣本拿不到或格式太不穩 → 才把 (b)（含 MLP 那個分支）當長期方案。
- 把 codex 的 agent 版本、rollout 檔大小、解析失敗行數寫進 `agora` 的既有 stderr 警告（不新增欄位、不新增檔案），讓格式漂移看得見。
- 在 `docs/design.md` 的 import 段加一段 codex，明說「第一版只 import、接續請用 claude／opencode」，避免使用者以為 `--agent codex` 什麼都能做。

### ④ 怎麼驗證

- 照 §7.1 的 C-1～C-12；關鍵三項是：**raw 原樣**（md5 相同）、**跨 agent 接得起來**（#439 的「接」）、**拒絕在讀任何東西之前**（不會留下半個 Session）。三項都依賴 fixture 的行格式，所以都要等 U-10 的樣本。
- 格式漂移的驗證：用兩個版本的 fixture（舊 schema、新 schema）各跑一次；新的認不得的行只能變成 `[skip]`，不能讓整個匯入失敗。
- 隱私的驗證：用 hook 斷言只開 `<root>/sessions/` 底下的檔，沒有 `auth.json`、沒有 `config.toml`、沒有 SQLite 索引（若 U-4 選「只掃 rollout」）。
- 隔離的驗證：C-10 驗的是 conftest 的統一隔離本身（`AGORA_CODEX_HOME` 在暫存目錄、`CODEX_HOME` 被清），不是每個測試自己設對。

## 10. 沒驗到／未知（誠實清單）

- 沒有讀任何真實 codex session、`~/.codex`、`/run/mlp-codex`；§2 的「未查」全部未查。
- 沒有讀 MyLinuxPool 的程式；§4.6 對 sidecar 的要求是**要求**，不是對現況的描述（只有 worker 環境說明與本 repo `docs/design.md` 的 sidecar 引用可佐證它會呼叫 agora）。sidecar 給 codex 隊員的 `CODEX_HOME` 是否與呼叫 import 時一致，未查。
- 沒有跑任何測試（本單只寫設計）；§7 的測試都還沒存在。
- 沒有驗證 `continue` 從 codex 匯入的 Session 到 claude／opencode 的實際結果；§1.4 是讀程式碼得到的路徑（`src/agora/cli.py:2121-2122`、`:2544-2553`），不是實跑。
- 沒有驗證 codex rollout 與 `codex resume <uuid>` 的可攜性；公開文件沒有保證（§2.2）。
- 探針只驗了 `--agent codex` 的 exit 2；其他 codex 行為都沒有真的跑過。

## 11. fix1 修訂紀錄（逐條對到審查）

回 repo 原文驗過才改；以下行號都以本 worktree 的現況（`b95433b` 之後未變的檔案）為準。

| 審查項 | 處理 | 落點 |
|---|---|---|
| M-1(a) 測試隔離 | `isolated_home` 統一設 `AGORA_CODEX_HOME`（無條件）＋清 `CODEX_HOME`；列進要改的檔（conftest／test-isolation spec／AGENTS.md 變數清單）；第 10 項改驗統一隔離 | §4.1、§4.5 第 5 點、§7 前言、C-10 |
| M-1(b) 根目錄形狀 | 讀取順序 `AGORA_CODEX_HOME` → `CODEX_HOME` → `~/.codex`；U-5 改成「加覆寫」 | §4.1、U-5 |
| M-1(c) 對 sidecar 第三條 | import 讀的根目錄須與該 codex agent 執行時的 `CODEX_HOME` 一致 | §4.6 (a) 第 3 條、§5 第 7 條 |
| M-2 #439 對接 | 照實寫「接＝換 agent 接、codex 目標自己召回不了」；列 #439 初篩要補的一句；U-3 連過去；§0／§8 明講 | §0 最後兩點、§4.6 範圍段、§8、U-3 |
| M-3 架構清單 | 新增 §4.7（模組切割／依賴方向／SOLID 逐項／契約編號）；§7.1 改名 C-1～C-12 | §4.7、§7.1 |
| 中：審查原文的 `cli.py:2206-2219` 誤述 | 改成四層：stub `agy.py:267-274`、merge `cli.py:813-820`、fork／headless `:2211-2219`、跨 agent `_refuse_cross_agent_source` `:403-424`（呼叫點 `:2250`／`:2252`）；並寫明 `continue <claude> --agent codex` 現在不會被擋、要在該 helper 補 codex 目標規則 | §1.1、§1.4、§4.4、§4.5 第 2 點、C-7 |
| 中：(a)/(b) 次序矛盾 | 統一成「最終 (a)；現在第一件事＝取樣本；(b) 工程非前置」；U-1 拆成「現在先做什麼」與「最終做什麼」 | §0、§3 第 4 點、§9③、U-1 |
| 中：§7 判準要綁樣本 | 明寫 fixture 行格式須取自 U-10 樣本；未取得前 C-1／3／4／5／8／9／11 不得宣告通過 | §7 前言、§9④ |
| 低：第 6 項斷言 | 改寫成「要新增斷言」，並引現況三行斷言說明不能照抄 | C-6 |
| 低：第 2 項「空檔」 | 先定義：可解析但 0 訊息 → exit 1；讀不到／不是 JSONL → exit 2 | C-2 |
| 低：「文件 5 處」 | 改成 6 處（與 §1.5／§4.5 的六段行號一致） | §1.5、§3 成本列 |
| 低：sidecar 的 `CODEX_HOME` 風險 | 併進 M-1(c)：§4.6 第 3 條＋§5 第 7 條 | §4.6、§5 |
| S-1 `list_sessions` | 根目錄／`sessions/` 不存在回 `[]`；存在但讀不到才丟錯；寫成契約並加測試 | §4.2、C-9 |
| S-2 拒絕 stub | 五個方法一律「拒絕 stub（要有）」；移除「可留」 | §4.2、§4.7 |
| S-3 拒絕位置 | `load_agent` 之後、`_refuse_agent_args` 之前（`cli.py:2210-2223`）；第三個 agent 才收成屬性（`cli.py:2139-2140` 先例） | §4.4、§4.7 |
| S-4 隱私先例 | 讀 `sessions/` 的是 sidecar 呼叫的 agora 程式；紀律仍約束所有 agent | §4.3、§5 第 3 條 |
| S-5 兩處提示文字 | `cli.py:815`、`:2208-2209` 現況與改法都列；`:2236` 不需改 | §4.4 末、§4.5 第 2 點 |
