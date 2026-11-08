# #443 設計筆記：codex session 的匯入（agora 支援 codex 或 sidecar 不匯入）

- 單：#443（MAS，層級 c，狀態:待開工）。作者：設計作者（隊員，deepseek-v4.1-flash）。2026-11-08。
- 範圍：**只寫設計**，不寫程式、不改 openspec、不新增 CLI 指令。sidecar 在別的 repo（MyLinuxPool），本 note 只寫「對 sidecar 的要求」，不碰它的程式。
- 限制（本次工作）：不讀 `~/.codex`、不讀 `/run/mlp-codex`、不碰憑證與真資料、不碰真機／真 Drive、不 `git switch`。所有 codex 格式的敘述只來自**公開文件**與**本 repo 既有紀錄**；查不到的標「未查」，沒有樣本就不假裝看過。
- 本人原則：介面簡單、不加多餘機制。

## 0. 結論（先講）

- 建議 **(a)：agora 支援 codex，但第一版只做「匯入／讀取／被別種 agent 接續」，不做 codex 自己的 continue／fork／headless／merge**。這是選 (a) 裡最小的那一塊，也是 #439 角色召回真正需要的部分。
- 不建議 (b)（sidecar 對 codex 不 import、只在 `exit.reason` 說明）當長期方案：它讓 codex 的 Session 永遠進不了 Drive，#439 的「記／找／接」對 codex 角色整條斷掉；它唯一的好處是 agora 零改動，適合當 (a) 的**退路**（取樣或格式確認做不到時）。
- 選 (a) 有一個硬前提：**實作前要有一份（或一次授權取得的）codex rollout 樣本**，用來釘住行格式；公開文件只講到「`rollout-*.jsonl`、放在 `$CODEX_HOME/sessions/`」，沒有行 schema（見 §2）。沒有樣本就不應開工，先走 (b) 或先補樣本。

## 1. 現況：agora 目前支援哪些 agent、各自的 import／continue 在哪

### 1.1 一個 agent ＝ 一個 adapter 模組

介面在 `src/agora/agents/base.py:141-203`（`class Agent`）：`export`／`turns`／`native`／`append_native`／`list_sessions`／`last_message`／`search_text`／`summarize`／`start_native`／`collect`。模組頂端就寫明「adding an agent is one new module」（`src/agora/agents/base.py:3-6`）。CLI 只透過這個介面說話，所以「支援 codex」＝新增一個 `src/agora/agents/codex.py` 並把它接進下面幾個列舉面。

| agent | adapter | import（讀 agent 自己的儲存） | continue（載入＋啟動＋回讀） |
|---|---|---|---|
| opencode | `src/agora/agents/opencode.py:891` | `export` `:1061`（叫 `opencode session export`，`_export_file`）、`turns` `:1104` | `start_native` `:1596`（重新編 id、`opencode session import`、回讀驗證；headless 是 `serve --port`，互動是 `--session`）、`collect` `:1632` |
| claude | `src/agora/agents/claude.py:853` | `export` `:860`（直接讀 `~/.claude/projects/*/<id>.jsonl` 與附屬資料夾）、`export_increment` `:866`、`turns` `:911` | `start_native` `:979`（寫 jsonl、`claude --resume <id>`；headless 是 `-p --input-format stream-json`）、`collect` `:1166`、`append_native` `:920` |
| agy | `src/agora/agents/agy.py:152` | `export` `:167`（`$AGORA_AGY_HOME` 的對話 db）、`turns` `:198`、`list_sessions` `:206`、`last_message` `:223`、`search_text` `:253` | `start_native` `:278`（還原 db＋補 summaries 列、`agy --conversation <id>`）、`collect` `:318`；`native`／`append_native`／`summarize` 是拒絕 stub `:267-274` |

agy 是「read-only 加一個方向的 continue」的現成範本：它用常數集中拒絕訊息（`AGY_READ_ONLY`／`AGY_HEADLESS_LATER`／`AGY_CROSS_AGENT`，`src/agora/agents/agy.py:103-120`），CLI 在讀任何東西之前就擋（`src/agora/cli.py:813-820`、`:2206-2219`）。codex 第一版可以照這個形狀，只是把「同 agent continue」拿掉、改成「只能被 claude／opencode 接」。

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

### 1.5 其他會被 agent 清單影響的表面（加 agent 不能只改 parser）

| 表面 | 位置 | 不做會怎樣 |
|---|---|---|
| `ACTOR` | `src/agora/cli.py:67` | import 標頭 KeyError（見 1.3 第 4 點） |
| TUI 的 agent 清單與顏色 | `src/agora/tui.py:51`、`:1117` | 未匯入頁看不到 codex；顏色缺一個 |
| 未匯入頁列 Session | `src/agora/tui.py:112-141`（呼叫 `list_sessions`） | codex 的 session 不會出現在清單（可以接受，但與其他 agent 不一致） |
| 點選預覽最後一則 | `src/agora/tui.py:562`（`last_message`） | 預覽空白（可以接受，但最好有） |
| 未匯入頁內文搜尋 | `src/agora/tui.py:1892`（`search_text`） | 搜不到 codex 的對話（若 rollout 有全文，應做；不做就要像 agy 一樣明說範圍，`src/agora/tui.py:184`） |
| `pull`／`push` 的 agent 前綴 | `src/agora/cache.py:108-129`、`:132-179`、`src/agora/cli.py:2819` | `codex:<id>` 不被接受、本機閱讀版快取不能建 |
| 遷移盤點 | `src/agora/migrate.py:26`、`:200-232` | 只影響舊格式盤點，第一版可以不加（見 §4.4） |
| 文件 | `docs/design.md:206-208`、`:218`、`:219-223`、`:269-273`、`:337`、`:526` | 使用者與 sidecar 看到的契約與現況不符 |

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
| #439 的「接」 | 有：找到後用 claude／opencode 跨 agent 接（只需 codex 的 `turns()`）；codex 自己接是後續 | 沒有（沒有 Session 可接）；角色只能每次全新開 |
| 主要風險 | rollout 格式未文件化、會隨 codex 版本變；需要一份授權樣本；raw 內含完整對話（與 claude／agy 同等隱私層級） | codex 隊員的記憶與 Session 永久丟失（容器重建就沒了）；#439 對 codex 角色靜默失效，只能靠 `exit.reason` 讓人看到 |
| 成本 | agora 一個 read-only adapter（估計約 300–500 行＋測試）；格式確認一次；文件 5 處 | agora 0；MLP 一個分支＋測試；之後若要 (a) 仍要重做一次（且舊 Session 已丟） |
| 可驗性 | 可用假 rollout 檔在 temp `CODEX_HOME` 全驗（§7） | 只能驗「sidecar 沒有呼叫、`exit.reason` 正確」；agora 這邊沒有行為可驗 |
| 與現有架構 | 完全照 agy PR1／PR2b 的形狀（read-only adapter＋集中拒絕常數） | 繞過 adapter 架構，問題被移到另一個 repo |

**建議理由（依本人「介面簡單、不加多餘機制」）**：

1. (a) 的「只 import」是**既有介面的用法**，不是新機制：`--agent` 已經存在、adapter 介面已經存在、跨 agent continue 已經存在（`src/agora/cli.py:2121-2122`、`:2544-2553`）。新增的只是一個模組與幾處列舉。
2. 它把 #439 的三步都補齊，而且**不用等 codex 自己的 continue**：召回後用 claude／opencode 接，符合 #439 U-9「取最新的合格者、跨 agent 可接」。
3. (b) 的節省只在 agora，代價是 codex 角色的記憶整條消失；而且 `exit.reason` 只是一行字，不會把 Session 變回來。
4. (b) 仍應保留為**退路**：若本人不同意取樣、或樣本顯示格式不穩到不值得綁，就走 (b)，並把「codex 只當短命隊員、不進角色召回」寫成營運約定（#439 U-6 本來就只召回長期角色）。

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
- **不新增** `--codex-home` 之類的旗標：根目錄就用 codex 自己的 `$CODEX_HOME`（預設 `~/.codex`），測試用環境變數指到暫存目錄（與 `AGORA_AGY_HOME` 同一個做法，`src/agora/agents/agy.py:5-8`）。若擔心 `$CODEX_HOME` 被別的東西設著，實作可以再加一個 `AGORA_CODEX_HOME` 覆寫；但那是**選用**，不是第一版必需（列在 U-5）。

### 4.2 adapter 的責任（第一版）

| 方法 | 第一版 | 說明 |
|---|---|---|
| `export(session_id)` | **做** | 在 `$CODEX_HOME/sessions/` 找到該 uuid 的 `rollout-*.jsonl`；raw 原樣（見 4.3）；`message_count` 由 turns 算；缺檔／讀不到 → `AgentError`（不是空清單） |
| `turns(raw)` | **做** | 逐行 JSON；`user`／`assistant` 文字與工具呼叫行照 `design 4.4`；認不得的 `type` → `[skip <type>]`；最後一行不完整就丟掉並警告（照 claude S10，`docs/design.md:270`） |
| `list_sessions()` | **做（最小）** | 掃 `$CODEX_HOME/sessions/` 的 rollout 檔：id 從檔名／首行取、`updated_at` 從 mtime 或首行時間、`title` 取第一則 user 文字（截斷）；**是否必須讀 SQLite 索引才完整 → 未查**（§2.2），若不完整就照實說「只列 rollout 檔看得到的」 |
| `last_message()` | **做（可選）** | 只讀該檔尾端，回最後一則 user／assistant 文字；做不到就先回 `None`（TUI 預覽空白，不擋功能） |
| `search_text()` | **做** | rollout 有全文，照 claude 的做法掃檔；若第一版不做，必須像 agy 一樣在 `--help`／TUI 明說「只搜標題」（`src/agora/tui.py:184`、`src/agora/agents/agy.py:118-120`） |
| `native()` | **拒絕 stub** | `AgentError(CODEX_CONTINUE_LATER)`；`continue --agent codex` 在 CLI 邊界就擋（4.4） |
| `append_native()` | **拒絕 stub** | 同上（codex 的 fork 是後續） |
| `summarize()` | **拒絕 stub** | `merge --agent codex` 在 CLI 邊界就擋（寫要約要跑 codex headless，第一版不做） |
| `start_native()`／`collect()` | **不做**（可留 stub） | codex 自己的 continue／headless／fork 是後續；跨 agent 接不需要它們 |
| `export_increment`／`collect_increment` | **不做** | 第一版整份 raw；等確定 rollout append-only 再加（U-6） |

### 4.3 raw 與標頭

- raw：**rollout 檔原樣**（一個位元組都不改），或最小信封 `{"format":"codex-rollout/1","main":[...]}`。建議**原樣**：codex 的 rollout 已經是 UTF-8 JSONL，沒有像 claude 的 aux 需要另存，信封只會多一層解析。若日後要存多檔（例如索引片段），再升級成信封；`store.raw_kind` 只認 chunks manifest（`src/agora/store.py:436-452`），raw 內部格式由 adapter 自己認，改信封不會動到 store。
- `agora.source`：`agent: codex`、`session_id: <uuid>`、`dir`（rollout 有 `cwd` 才填，否則 null）、`agent_version`（有才填）、`created_at`（有才填）（`src/agora/cli.py:187-192`）。
- `generated.by`：`codex` 或 `codex/<model>`（`src/agora/cli.py:182-184`）；`ACTOR` 要有 `codex`。
- 隱私：rollout 與 claude 的 jsonl 同等級（完整對話、工具輸出、可能的 reasoning）；照既有規則上 Drive、照既有規則不進日誌。**不讀 `~/.codex/auth.json` 等憑證檔**；adapter 只碰 `$CODEX_HOME/sessions/`。

### 4.4 邊界：哪些 codex 操作要明確拒絕

- `agora continue session <id> --agent codex`（含 `--fork`／`--headless`／`--harness-file`／`--agent-arg`）：**exit 1**，在同步、鎖檢查、寫 pending、開 agent 之前（照 agy 的位置，`src/agora/cli.py:2206-2219`），訊息固定、集中一個常數：

  ```
  codex 第一版只支援 import／讀取（#443）；接續 codex 匯入的 Session 請用 claude 或 opencode（跨 agent，會寫回同一個 Session）
  ```

- `agora continue <codex 匯入的 Session> --agent claude|opencode`：**允許**。這是 #439 的召回路徑，現有程式就會走 `_converted_turns`（`src/agora/cli.py:2544-2553`）；測試要釘住。
- `agora continue <claude|opencode 的 Session> --agent codex`：**拒絕**（同上一條的訊息）。
- `agora merge --agent codex`：**exit 1**，在讀任何東西之前（照 agy，`src/agora/cli.py:813-820`）：`codex 第一版不寫要約；請用 opencode 或 claude`。
- merge 的**來源**是 codex Session：**允許**（只讀 `turns()`，與 claude／opencode 當來源一樣）。
- `migrate`：第一版**不加** `codex` 到 `AGENT_NAMES`（`src/agora/migrate.py:26`）；它只影響舊格式盤點，不影響新匯入。列為 U-7。

### 4.5 要改的檔案（實作清單，供日後開單）

1. 新檔 `src/agora/agents/codex.py`（adapter＋`CODEX_CONTINUE_LATER` 等常數）。
2. `src/agora/cli.py:52`（`AGENTS`）、`:67`（`ACTOR`）、`:813-820`（merge 拒絕）、`:2206-2219`（continue 拒絕；只擋 `--agent codex`，不要誤擋跨 agent 來源）。
3. `src/agora/tui.py:51`（顏色；可選）、`--help` 的 agent 說明（`src/agora/cli.py:2974-2977`）。
4. 文件：`docs/design.md:206-208`、`:218`、`:219-223`、`:269-273`、`:337`、`:526`（新增 codex 段與拒絕範圍）。
5. 測試：`tests/unit/test_agent_codex.py`（新）、`tests/fixtures/codex/`（假 rollout）；跨 agent 的案例加在 `tests/unit/test_continue_*` 或新檔。

### 4.6 對 sidecar（MyLinuxPool）的要求

**(a) 選定時：close／import 不用改。** sidecar 現有的 close → `agora import … --agent <agent> --external-session-id <sid>` 照舊；codex 的 `--agent` 就是 `codex`，exit 0，stdout 最後一行是 `agora:<ULID>`。**證據（推論，但很強）**：sidecar 現在看到的錯誤是 `invalid choice: 'codex'`，這只有在它已經送了 `--agent codex` 時才會出現；所以它已經會把 codex 的 id 送進 import，只差 agora 不認得這個值。加進 `AGENTS` 之後同一個呼叫就會成功，MLP 的 close 流程不必改。

**但有一個未來的限制要先講**：若 sidecar 之後要用 #439 的角色召回，把「找到的 codex Session」當 `open` 的 `session` 再開一個 **codex** agent，那會走 `agora continue … --agent codex`，第一版會 exit 1（§4.4）。所以 codex 角色的召回要麼（i）用 claude／opencode 接（跨 agent），要麼（ii）等 codex 自己的 continue（U-2(ii)）。這不影響 close／import，但影響「召回後用什麼 agent 開」。另請 MLP 確認（未讀 MLP 程式）：close 流程不要因為「agent 名字不在某個白名單」而跳過 import；也不要解析 agora 的英文 stderr 當契約（exit code 才是契約，`docs/design.md` 對 exit 4 已是這個慣例）。

**(b) 選定時：**

- codex agent 的 close **不要呼叫** `agora import --agent codex`。
- `exit.reason` 用一個固定、機器可讀的值（例：`codex-import-unsupported`，實際字串由 MLP 與 PMO 定），並附一句人看得懂的說明：「codex 不匯入（#443 (b)）；session 只留在本機，容器重建會消失」。
- 保留該 codex session id 與路徑到日誌，方便日後改走 (a) 時補匯入（但檔案可能已被清掉，不保證）。
- **不要**替 codex 角色寫 `role:` tag（沒有 Session 可標，寫了會讓 #439 的召回找錯）。

## 5. 風險與代價（選 (a) 時要接受的）

1. **格式未文件化**：`turns()` 依賴 rollout 行 schema；codex 版本一改就可能讀不到。緩解：raw 原樣保存（最壞情況只是閱讀版變差，Session 還在 Drive）、未知行 `[skip]`、把 `agent_version` 記進標頭、測試用固定 fixture。**這是本設計最大的不確定性。**
2. **列舉可能不完整**：若 codex 的 Session 清單只存在 SQLite 索引，單靠 `sessions/` 掃檔可能漏列。第一版接受「只列 rollout 檔看得到的」，並在文件與 TUI 說明；要不要讀索引列 U-4。
3. **隱私**：rollout 有完整對話與工具輸出；上 Drive 的風險與 claude／agy 相同，沒有新增機制，但要在文件寫明（`docs/design.md` 的 import 段）。
4. **匯入時 codex 還在寫**：rollout 可能正在追加。照 claude 的規則「最後一行不完整就丟掉並警告」，並在檔案大小／mtime 前後不一致時重試一次（推論，需樣本驗）。
5. **跨 agent 接續的品質**：codex 的 turns 轉成 claude／opencode 的 native 後，工具呼叫只剩一行摘要（既有 `design 4.4`），不是無損搬移。這是既有跨 agent 的限制，不是 codex 特有；文件要說清楚。
6. **不做 codex 自己的 continue**：codex 角色召回後若堅持用 codex 接，會 exit 1；PM 要改用 claude／opencode（或等後續）。這是刻意的範圍，不是 bug。

## 6. 未決項（需本人／PMO 裁定）

| # | 問題 | 選項 | 建議 |
|---|---|---|---|
| U-1 | (a) 還是 (b) | (a) agora 支援 codex（只 import）；(b) sidecar 不 import＋`exit.reason` | **(a)**，理由見 §3；若取樣不可得或格式太不穩，退回 (b) |
| U-2 | (a) 第一版範圍 | (i) 只 import／讀取／被跨 agent 接；(ii) 加 codex 自己的 continue（`codex resume`／`codex exec resume`）；(iii) 全做（fork／headless／merge 寫要約） | **(i)**；`start_native` 要處理 cwd 不同時的 resume 詢問（公開文件明說會問），是另一個設計題 |
| U-3 | codex 會不會擔任長期角色（#439 召回對象） | 會（那 (a) 是必要的）；只有短命隊員（那 (b) 可接受） | 請 PMO／本人確認目前的池怎麼用 codex；本設計不猜 |
| U-4 | 清單要不要讀 codex 的 SQLite 索引 | 只掃 rollout 檔；或讀索引補齊 | 第一版**只掃 rollout 檔**，不夠再說；讀索引是另一個未文件化格式 |
| U-5 | 根目錄要不要加 `AGORA_CODEX_HOME` | 只用 `$CODEX_HOME`；或加一個 agora 專用覆寫 | 只用 `$CODEX_HOME`（codex 自己的介面，少一個旗標）；測試用環境變數指到暫存目錄即可 |
| U-6 | 要不要做增量（`export_increment`） | 第一版整份；或照 claude 做前綴增量 | **整份**；等確認 rollout append-only 再加 |
| U-7 | `migrate` 要不要認 codex | 加 `AGENT_NAMES`；不加 | 不加（第一版不影響新匯入） |
| U-8 | (b) 的 `exit.reason` 字串與可見性 | 固定代碼；或只寫人話 | 由 MLP 與 PMO 定；本設計只要求「固定、機器可讀、人看得懂」 |
| U-9 | codex 的 raw 要不要包信封 | 原樣；或 `codex-rollout/1` 信封 | **原樣**（少一層；日後需要多檔再升級） |
| U-10 | 取樣由誰做、怎麼做 | 本人在有 codex 的機器上取一份去識別化 rollout；或在隔離容器跑一次 codex 產生 | 需要本人單次授權（真機規矩）；**不要**由隊員自行讀 `~/.codex` |

## 7. 可驗的完成判準（日後實作怎麼驗，全部用假資料）

測試照 `AGENTS.md` 的隔離規則：`tests/conftest.py` 的 `isolated_home` 會把 `HOME`、`AGORA_*` 指到暫存目錄；codex 的根目錄用 `$CODEX_HOME` 指到同一個暫存目錄。測試模組第一行 `import _guard`。**不連真 Drive**（`AGORA_RCLONE=tests/fakes/fake_rclone.py`、`AGORA_UPLOAD=inline`）、**不讀真的 `~/.codex`**（用一個記錄開啟路徑的 hook 斷言，照 `tests/unit/test_agent_agy.py:855-859` 的做法）。

### 7.1 選 (a) 時（agora 側）

1. **匯入成功**：在 `$CODEX_HOME/sessions/<假路徑>/rollout-….jsonl` 放一個自編 rollout（含 user、assistant、一個工具呼叫、一個認不得的 type）→ `agora import session --external-session-id <uuid> --agent codex` → exit 0、stdout 最後一行 `agora:<ULID>`；`show` 的標頭 `source.agent=codex`、`source.session_id=<uuid>`、`generated.by=codex`；raw 的 md5 與 fixture 檔相同（原樣、不改位元組）；閱讀版有 user／assistant 文字與 `[tool]`，認不得的 type 是 `[skip …]`。
2. **沒有訊息**：空檔或只有認不得的行 → exit 1「沒有任何訊息，不匯入」（`src/agora/cli.py:641-642` 的既有行為）。
3. **重匯不變**：同一個 fixture 再匯一次 → 同一個 agora id、不新增版本（`src/agora/cli.py:661-664`）。
4. **變少不覆蓋**：fixture 改成比較少內容再匯 → exit 1，訊息是既有的「內容比原本少」（`src/agora/cli.py:681-688`）。
5. **被跨 agent 接**：`agora continue <codex 匯入的 Session> --agent claude`（用 `tests/fakes/fake_claude.py`）→ exit 0，寫回同一個 agora id、`source.agent` 變成 claude、`previous_sources` 有 codex（照 `docs/design.md` 5.4 第 6 點）；`--agent opencode` 同理。**這條是 #439 的關鍵驗收。**
6. **拒絕**：`continue … --agent codex`（含 `--fork`／`--headless`）與 `merge … --agent codex` → exit 1、固定訊息、且**沒有**同步／開 agent／寫 pending（可用既有的 `run_cli` 與假 agent 斷言呼叫紀錄，照 `tests/unit/test_agent_agy.py:706-718`）。
7. **反方向拒絕**：`continue <claude 的 Session> --agent codex` → exit 1 同訊息。
8. **merge 來源是 codex**：`merge session <codex 的> <claude 的> --agent claude`（用假 claude 寫要約）→ exit 0；來源清單有 codex。
9. **TUI／cache 三個表面**（照 `tests/unit/test_agent_agy.py:867-905`）：未匯入頁列出 fixture 的 codex session；內文搜尋找得到 rollout 裡的 body 字串；`cache.pull` 的 `codex:<id>` 產生本機閱讀版 `.md`，第二次是 skip。
10. **不碰真家目錄**：把 `$CODEX_HOME` 指到暫存目錄，另開一個 hook 記錄所有被打開的路徑，斷言沒有 `~/.codex`、沒有 `auth.json`（照 agy 的 `test_agent_agy.py:855-859`）。
11. **格式壞掉**：fixture 最後一行截斷 → 丟掉並在 stderr 警告一次，exit 0（照 claude S10）；整檔不是 JSONL → `AgentError`（exit 2），不存半個 Session。
12. **假 secret 不入檔**：fixture 不放符合掃描器樣式的字串（`AGENTS.md` 的測試資料規則）。

### 7.2 選 (b) 時（不在本 repo）

- MLP 的測試（假 agora／假 agent）：close 一個 codex agent → **沒有** `agora import` 呼叫；`exit.reason` 等於約定的固定值；PM 看得到說明。
- agora 這邊**沒有行為可驗**；頂多把現況釘成一個記錄性測試（`--agent codex` → exit 2、argparse 訊息），但那是記錄症狀，不是修正，建議不寫。

## 8. 與 #439 的關係（一句話版）

#439 的三步是「記（import 帶 `role:` tag）→ 找（`search --strict`）→ 接（continue，可跨 agent）」。選 (b) 等於 codex 角色的「記」直接不存在，後兩步無從發生；選 (a) 的 import-only 則三步都在，且「接」可以借 claude／opencode 完成，不必等 codex 自己的 continue。**這就是本設計建議 (a) 的唯一理由，也是它比 (b) 多出來的價值。**

## 9. 專家視角四問

### ① 頂尖專家會怎麼做

- 先把「**存檔**」與「**接續**」拆開：存檔只需要讀，接續才需要寫回 agent 的儲存。大多數人會先做存檔（本設計的 (a)-import-only），因為那是不可逆的資料保存；接續可以之後再說。
- 先把**格式不確定**變成可管理的風險：raw 原樣保存、解析失敗只降級閱讀版、把 agent 版本記下來。專家不會讓「解析器不認得新格式」變成「Session 丟掉」。
- 用**一份授權樣本**釘住格式，而不是讀使用者的家目錄；測試用固定 fixture，正式路徑用環境變數隔離。

### ② 我們缺什麼

- 缺 codex rollout 的**一手樣本與 schema**（§2.2 全部未查）；沒有它，adapter 只能靠猜。
- 缺「codex 在本池擔任哪些角色」的事實（U-3）；這決定 (a)/(b) 的急迫性。
- 缺 sidecar close 流程對「不支援的 agent」的既有處理（MLP 未讀），所以 (b) 的 `exit.reason` 只能提要求，不能斷言它現在長什麼樣。
- 缺「匯入後跨 agent 接續的品質」量測：工具呼叫只剩摘要（既有設計），codex 的 rollout 可能有很多這種內容；沒有實際資料可評估。

### ③ 怎麼補

- 由本人在有 codex 的機器上取一份去識別化 rollout（U-10），放進 `tests/fixtures/codex/`，先寫一個只驗「能解析出 user／assistant／工具呼叫」的測試，再寫 adapter。
- 先用 (b) 止血（MLP 不呼叫、`exit.reason` 明說），同時開 (a) 的實作單；兩者不衝突：(b) 是營運行為，(a) 是資料保存能力。
- 把 codex 的 agent 版本、rollout 檔大小、解析失敗行數寫進 `agora` 的既有 stderr 警告（不新增欄位、不新增檔案），讓格式漂移看得見。
- 在 `docs/design.md` 的 import 段加一段 codex，明說「第一版只 import、接續請用 claude／opencode」，避免使用者以為 `--agent codex` 什麼都能做。

### ④ 怎麼驗證

- 照 §7.1 的 12 項；關鍵三項是：**raw 原樣**（md5 相同）、**跨 agent 接得起來**（#439 的「接」）、**拒絕在讀任何東西之前**（不會留下半個 Session）。
- 格式漂移的驗證：用兩個版本的 fixture（舊 schema、新 schema）各跑一次；新的認不得的行只能變成 `[skip]`，不能讓整個匯入失敗。
- 隱私的驗證：用 hook 斷言只開 `$CODEX_HOME/sessions/` 底下的檔，沒有 `auth.json`、沒有 `config.toml`、沒有 SQLite 索引（若 U-4 選「只掃 rollout」）。

## 10. 沒驗到／未知（誠實清單）

- 沒有讀任何真實 codex session、`~/.codex`、`/run/mlp-codex`；§2 的「未查」全部未查。
- 沒有讀 MyLinuxPool 的程式；§4.6 對 sidecar 的要求是**要求**，不是對現況的描述（只有 worker 環境說明與本 repo `docs/design.md` 的 sidecar 引用可佐證它會呼叫 agora）。
- 沒有跑任何測試（本單只寫設計）；§7 的測試都還沒存在。
- 沒有驗證 `continue` 從 codex 匯入的 Session 到 claude／opencode 的實際結果；§1.4 是讀程式碼得到的路徑（`src/agora/cli.py:2121-2122`、`:2544-2553`），不是實跑。
- 沒有驗證 codex rollout 與 `codex resume <uuid>` 的可攜性；公開文件沒有保證（§2.2）。
- 探針只驗了 `--agent codex` 的 exit 2；其他 codex 行為都沒有真的跑過。
