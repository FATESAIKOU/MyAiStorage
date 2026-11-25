# 架構總圖（agora）

維護者：專案架構師。每一次改到模組切割、依賴方向或公開介面，都和引起它的設計文件放在同一個 PR 更新（AGENTS.md「專案架構師可以 commit 的路徑」）。

- **範圍**：目前收錄 `src/agora` 與 `src/pmo`（PMO skill 見下面「PMO skill」一節）。同一個 repo 裡的 `aicomm`、`ainame`、`atelier`（`pyproject.toml` 的 `[project.scripts]`）還沒收錄，之後再補；`angareion` 只收錄待決（#468）與頻道換頁（#542）兩節，見下面「angareion」。
- **基準**：#342 的 branch fe89ab6（#342 實作，加上 main f194fd7，含 #340、#341、#343）。行數都是實數。`src/pmo` 的基準是 main 495cec0（見該節）。

## 模組地圖

| 模組 | 行數 | 職責 |
|---|---|---|
| `cli.py` | 3116 | `agora` 指令的入口與各子指令；`continue` 的前景流程（代理監看、輪的結果、最後一次寫回、寫回目標解析） |
| `continue_shell.py`（#341） | 274 | `continue` 兩條子行程邊界在外殼這一側的協議：啟動、逾時、收行程群組、錯誤對應、diff 套回、狀態編解碼 |
| `errors.py`（#341） | 5 | 全 CLI 共用的 `InputError` |
| `round_child.py` | 109 | periodic round 的子行程本體（`python -m agora.round_child`） |
| `start_child.py` | 59 | startup load 的子行程本體（`python -m agora.start_child`） |
| `store.py` | 3212 | 本機 outbox、mirror、索引（`Index`）、Drive（`Drive`，包 rclone）、上傳、`sync` |
| `header.py` | 264 | 共用 header（OKF frontmatter）的讀寫與驗證 |
| `background.py` | 143 | 背景上傳器 |
| `cache.py` | 455 | 預覽、快取 |
| `tui.py` | 2278 | 互動介面 |
| `migrate.py` | 495 | 舊格式盤點與遷移 |
| `orphan_guard.py` | 298 | agora 被 `kill -9` 時不讓 agent 變成孤兒 |
| `private_tmp.py` | 259 | agora 自己 state 底下的私有暫存目錄與殘留清掃 |
| `credscan.py` | 221 | 匯入前的憑證樣式掃描（只報名稱和數量） |
| `agents/base.py` | 334 | agent 介面（`Agent` Protocol、`Exported`、`Launch`、`AgentError`、`env_seconds`…） |
| `agents/claude.py` | 1222 | Claude Code adapter：找檔、session 解析（turns／標題／`_Facts`）、一趟式匯出 `_stream_raw`、啟動、還原 |
| `agents/claude_raw.py`（#342） | 185 | claude raw 封包格式：`FORMAT`、`_pack_raw`／`_unpack_raw`、`_RAW_HEAD`／`_AUX_SEP`、aux 串流編碼 `_iter_aux`、`_aux_files`、`PackedRaw` |
| `agents/opencode.py` | 1648 | opencode adapter |
| `agents/agy*.py` | 339＋363＋165＋582＋592 | agy adapter，分成 agy／agy_store／agy_wire／agy_restore／agy_summaries |

## 依賴方向

`A → B` 表示 A import B。「lazy」是函式內的 import。下面是 fc5fe98 **逐檔 grep 的完整結果**，頂層 import 和縮排的 lazy import 都算在內。標 `[#341]` 的是 #341 新增的部分，已經實作。

```
header          → （無）
errors          → （無）                                               [#341]
orphan_guard    → （無）
credscan        → （無）
private_tmp     → （無）；lazy: agents.base（只為 AgentError）
agents/base     → private_tmp
store           → header；lazy: background（background 頂層 import store，所以這裡用 lazy）
background      → store
cache           → header, store, agents.base
migrate         → header, store；lazy: agents.base, agents.claude, agents.agy_store, agents.opencode
tui             → cache, header, store, agents.agy；lazy: agents.base, cli（為了避開循環）
continue_shell  → header, store, errors, agents.base                   [#341；不得 import cli／round_child／start_child]
cli             → header, orphan_guard, private_tmp, store, agents.agy, agents.opencode, agents.base,
                  continue_shell[#341], errors[#341]
                  lazy: background, cache, migrate, tui, agents.claude, agents.agy_store
round_child     → cli, private_tmp, store, continue_shell[#341]
start_child     → cli, store
agents/claude   → agents.base, private_tmp, store, agents.claude_raw[#342]；lazy: store
agents/claude_raw → agents.base（只拿 AgentError）              [#342；不得 import agents.claude、store]
agents/opencode → agents.base, private_tmp, store；lazy: header
agents/agy      → agents.base, agents.agy_wire, agents.agy_store, agents.agy_summaries, agents.agy_restore,
                  private_tmp, store
agents/agy_wire → agents.base
agents/agy_store     → agents.agy_wire, agents.base, private_tmp
agents/agy_summaries → agents.agy_store, agents.base, private_tmp
agents/agy_restore   → store, agents.agy_store, agents.agy_summaries, agents.agy_wire, agents.base
```

**已知的雙向**：
- `agents/base ⇄ private_tmp`：base 頂層 import private_tmp，private_tmp 用 lazy import base 拿 `AgentError`。
- `store ⇄ background`：store 用 lazy import background。

兩者都靠一邊 lazy 避開 import 時的循環。

**continue_shell 的傳遞依賴**：header、store、errors、agents.base、private_tmp，執行時再加上 store lazy 的 background。不含 cli、round_child、start_child（由 #341 的 C-3／C-4 鎖住）。

**規則**：
1. 低層（header、errors、private_tmp、agents/base、store、background）不 import cli。
2. 子行程本體（round_child、start_child）可以依賴 cli，因為它們在子行程裡跑 cli 的 round 或 load 本體。外殼（cli）和子行程共同依賴的協議，放在 continue_shell。
3. `tui → cli` 只能 lazy。
4. 新增雙向依賴時，要在這裡記一筆，並寫明哪一邊是 lazy。

## 公開介面清單（目前收錄的部分）

- **指令**：`agora = "agora.cli:main"`；`pmo = "pmo.cli:main"`（PMO skill，見下節）。
- **agent 介面**：`agents/base.py` 的 `Agent`（Protocol）：`export`、`turns`、`native`、`append_native`、`list_sessions`、`last_message`、`search_text`、`summarize`、`start_native`、`collect`；`Exported`（raw 的載體：`raw_file`／`raw_md5`／`raw_size`／`raw`，以及 `turns`；`raw_digest()`、`raw_source()` 統一「有檔用檔、否則 bytes」，#340）；模組函式 `turns_of`、`reading_of`、`reading`。
- **store**：`Paths`、`StoreError`、`Drive`（`folder_id`、`list_sessions` …）、`Index`、`stage`、`remember`、`index_pending`、`upload_batch`、`push_outbox`、`kick_uploader`、`hold_upload_lock`、`sync`。不變式見 decisions D-1～D-7。
- **continue 的子行程協議**（在 continue_shell）：stdin 送一份 JSON 狀態，stdout 回一份 JSON payload；round 的 payload 必含 `ROUND_DIFF_KEYS`；load 的錯誤用 `{"err": {"kind", "text"}}` 回報。

## PMO skill（#110；`docs/design-notes/110-pmo-skill.md`）— 定案（本人 2026-11-02～03）；T-1～T-5 已 merge，T-6 是 `skills/pmo/SKILL.md`、環境說明範本與本節

- **形狀**：新指令 `pmo`（套件 `src/pmo`；Q-5 (a)）加上 `skills/pmo/SKILL.md`；PMO 的 harness 只寫能力（Q-1 (a)）。
- **依賴方向**：核心只依賴 `ainame`，不碰外部。指令透過四個埠碰外部：Angareion（`angareion` 子行程）、aicomm（`aicomm send` 子行程）、sidecar（HTTP）、GitHub（`gh` 子行程，唯讀）。**不 import** `agora`、`atelier`、`aicomm`、`angareion` 的 Python 模組，也不依賴 MyPMO 的程式。套件內：`cli → tick → {round, inbox, handoff, wake, due, teamstate, tickstate, …}`、`cli → wake → {due, ops, teammsg, teamstate, tickstate, errors, ports, runtime, timeutil, _lock}`；`due` 是純函式（執行期只依賴 `timeutil`，`tickstate` 只在 `TYPE_CHECKING` 下 import）；功能模組（`handoff`、`wake`）執行期不 import `tick`，只有 `cli` import（D-22）。
- **公開介面**：`pmo tick`、`pmo inbox read|list|ack|report|decision-send|rebuild|handoff-close`、`pmo check|send|open|reply|poke|status|close`、`pmo wake set|off|show`（PMO 自己開、調、停 sidecar 上的定時 poke job；頻率由 PMO 定，Q-8 (a)）；teams 設定、team 狀態、收件帳本、`tick.json` 的 schema（資料由環境變數指到本機）；tick 的事件標籤沿用現行工具。
- **第一階段不做**：群請求（Q-4）、worker team 的動作（Q-3 (a)）、挑模型、MyPMO 的工單維護工具；這些留在 MyPMO，和 `pmo tick` 用 id 交接（Q-9 (a)）。
- **實作狀態**：T-1～T-5 的 PR（`pmo` 的入口、收信、重建、`check`／`send`／`open`、`reply`／`poke`／`status`／`close`、tick 骨架與看門狗、群請求交接、`due` 與 `wake`）都已在 main：`pmo wake set|off|show`、tick 的叫醒 job 健康、`pmo/due.py` 都包含在內。T-6（#359）是 `skills/pmo/SKILL.md`、環境說明範本與本節，T-7 是切換單。每個 PR 都要經架構師結構審。決策見 decisions D-15～D-20、D-22、D-23。

### `src/pmo` 模組地圖（行數是 main 495cec0 的 `wc -l src/pmo/*.py`）

| 模組 | 行數 | 職責 |
|---|---|---|
| `cli.py` | 946 | `pmo` 的入口與各子指令；把錯誤對到結束碼；不實作行為 |
| `inbox.py` | 669 | 派工信／叫人／急件／決定題的解析、去重、ack 與回報的本文、事件行 |
| `dispatch.py` | 494 | `check` 的逐項預檢、`send` 的指示信組裝與 team 狀態寫入、`[wake-hint]` |
| `ops.py` | 650 | `reply`／`poke`／`status`／`close`（Close SOP）與 job 的辨識 |
| `open.py` | 306 | `POST /agents` 的請求組裝、第一則輸入、開完寫回 team 狀態 |
| `rebuild.py` | 438 | `inbox rebuild`：由 `angareion log` 重建收件帳本 |
| `round.py` | 145 | 一輪收信：身分守門 → 預檢 → `angareion read` → 秘書階段 → 團隊訊息階段（`tick` 與 `inbox read` 共用） |
| `teammsg.py` | 135 | `[msg]` 事件與「收到 ‹工單›」的 ack 比對 |
| `handoff.py` | 171 | 群請求的交接記錄與 `[group-sweep-due]`（`handoff-close` 刪除） |
| `due.py` | 107 | 純函式：tick 與 `wake show` 共用的「最早到期」（含看門狗），以及「應該在跑」與看門狗時刻 |
| `tick.py` | 704 | `pmo tick` 的一輪：收信、agents 比對、看門狗、worker poll、交接、叫醒 job 健康、`tick.json` 寫入 |
| `wake.py` | 563 | `wake set`／`off`／`show`（sidecar 上恰好一個自己的 poke job）與 tick 的 job 健康／孤兒檢查 |
| `ledger.py` | 252 | 收件帳本的 schema、原子寫入與鎖 |
| `teamstate.py` | 223 | team 狀態的 schema、原子寫入與每 team 一把鎖 |
| `tickstate.py` | 282 | `tick.json`（`last_tick_at`、`handoffs`、`worker_poll`、`wake`）的 schema 與寫入 |
| `config.py` | 339 | teams 設定的 schema 與驗證 |
| `runtime.py` | 122 | `Ctx`／`Deps`、身分守門、`sidecar_port` |
| `ports.py` | 415 | 四個埠的介面與資料形狀（Angareion、aicomm、sidecar、GitHub） |
| `adapters.py` | 634 | 四個埠的實作（子行程與 HTTP） |
| `decision.py` | 212 | 決定題的建構與作答解析 |
| `sanitize.py` | 99 | 輸出與內容的憑證樣式掃描 |
| `paths.py` | 35 | `PMO_SKILL_CONFIG`／`PMO_SKILL_STATE_DIR`（沒設就拒絕） |
| `errors.py` | 55 | 共用例外（`UsageError`、`SchemaError`、`SentButNotRecorded`…） |
| `_io.py`／`_jsonutil.py`／`_lock.py` | 43／181／145 | 原子寫入、JSON 欄位驗證、`path_lock` |
| `timeutil.py` | 74 | 時刻的格式與解析（一個格式一個函式）；#522 起多一個 `received_at` 的排序鍵 `received_at_key(value) -> tuple`（沒有時刻的最前、讀得出的依時刻比較而不轉 UTC、讀不出的最後且彼此依原字串；同鍵由呼叫方決勝），`cli._items_by_creation` 與 `decision.latest_decision_answers` 用它；與 angareion `messages.received_at_key` 的差別見 decisions D-33 |
| `fakes.py` | 404 | 測試用的同名方法假物件（只依賴 `ports`） |

### `src/pmo` 的依賴方向

`A → B` 表示 A import B；`TYPE_CHECKING` 只給型別用。逐檔 grep 的結果：

```
低層（不 import 其他 pmo 模組）：errors、timeutil、sanitize、ports
schema／I/O：_io → errors；_jsonutil → errors；_lock → errors；paths → errors
             config → ainame, _io, _jsonutil
             ledger → _io, _jsonutil, _lock, sanitize, timeutil
             teamstate → _io, _jsonutil, _lock, sanitize
             tickstate → _io, _jsonutil, _lock, timeutil
埠與執行環境：adapters → ports；runtime → ainame, config, errors, ports, sanitize, timeutil
              fakes → ports（測試用）
核心：inbox → ledger, sanitize, decision, errors, ports, runtime, timeutil
      decision → runtime, timeutil
      dispatch → ainame, teamstate, tickstate, config, errors, ports, runtime, timeutil
      open → teamstate, config, errors, ports, runtime
      ops → teamstate, config, errors, ports, runtime, timeutil
      rebuild → inbox, ledger, runtime, timeutil
      round → inbox, ledger, teammsg, teamstate, errors, ports, runtime
      teammsg → inbox, teamstate, _lock, runtime, timeutil
      handoff → tickstate, _lock, timeutil（TYPE_CHECKING: tick）
      due → timeutil（TYPE_CHECKING: tickstate）
上層：tick → handoff, inbox, round, teammsg, teamstate, tickstate, config, errors, ports, runtime,
             timeutil, _lock, due, wake
      wake → ops, teammsg, teamstate, tickstate, due, errors, ports, runtime, timeutil, _lock
             （TYPE_CHECKING: tick）
      cli → 各指令模組（inbox、dispatch、open、ops、rebuild、decision、handoff、tick、wake）、round、ledger、adapters、runtime、config、paths、sanitize、errors、ports、timeutil、_lock
```

- 不 import `agora`、`atelier`、`aicomm`、`angareion` 的 Python 模組，也不依賴 MyPMO 的程式（D-16）；`config`／`runtime`／`dispatch` 用到的 `ainame` 是同一個 distribution 的核心套件，也在 D-16 允許的範圍。
- 狀態檔的讀寫只在各自的 schema 模組（`ledger`／`teamstate`／`tickstate`）；鎖只用 `_lock.path_lock`；`pmo` 的公開介面是指令，模組不留別名（decisions 的「pmo 的通則」）。例外是 `handoff.close` 與 `wake._write_wake` 自己拿 `tick.json.lock` 做鎖內 read-modify-write（收斂到 `tickstate` 是待決的 P-14）。

## angareion（#468；待決 datatype，PR-1～PR-4）

設計全文在 `openspec/changes/archive/2026-11-15-angareion-pending-decision/design.md`（§19 是分 PR 切法）；決定見 decisions D-30。本節寫 PR-1～PR-4 在 `src/angareion` 實作的部分。PR-4（#474）加入 `decision_cli.py` 與 `angareion decision` 子指令。#509／#510（設計 `docs/design-notes/509-510-yaml-depth-and-time-order.md`，決定 D-31；#516～#519）把 YAML flow 深度上限與後端時刻的排序鍵收進 `messages`，見下面各條。

- **`messages.py`**：`Message.datatype`（預設 `message`）。讀取寬鬆：沒有這個鍵、不是字串、或不符 `[a-z][a-z0-9-]{0,31}` 的值都讀成 `message`；合法但不認得的值（例如 `poll`）照原字串保留。寫入嚴格：`Message.validate` 拒絕不符文法的值。常數 `DEFAULT_DATATYPE`、`SELF_SILENT_DATATYPES`（`frozenset({"decision"})`，「寫的人自己不收」的 datatype 表，格式層擁有）。`message_to_dict` 多了 `"datatype"` 鍵，所以 `read --json`、`log --json` 也多這一鍵。PR-2 起 `serialize_message` 把 `datatype` 寫在 `angareion` 之後，`send` 寫 `datatype: message`。#509 起擁有 YAML flow 深度上限：常數 `FLOW_DEPTH_MAX`（32）、例外 `FlowTooDeep`（`yaml.YAMLError` 的子類）、`FlowDepthLoader`（`yaml.SafeLoader` 的子類，在 scanner 開 flow collection 時檢查，超過上限丟 `FlowTooDeep`；依賴 PyYAML 的 scanner 內部，由 `tests/unit/test_angareion_decision.py` 釘住）；`parse_message` 的 front matter 用它載入，太深的讀成非訊息（`strict` 時是 `MessageValidationError`）。#510 起擁有後端時刻的排序鍵：`parse_received_at(text: str) -> datetime | None`（帶時區的 RFC 3339，`Z` 視為 UTC；沒有時區或解析不出回 `None`）、`received_at_key(value: str | None) -> tuple`（`None` 最前；帶時區的依時刻比較、不轉 UTC，同一時刻不同寫法鍵相同；沒有時區或解析不出的最後、彼此依原字串；同鍵由呼叫方決勝）。`messages` 不 import `backend`、`decision`、`log_query`、`client`。
- **`backend.py`**：`recipient_matches`、`mention_matches` 多一條：讀者自己寫的、datatype 在 `messages.SELF_SILENT_DATATYPES` 裡的訊息不算寄給讀者。表在每次呼叫時從 `messages` 模組讀，遞送層本身不認得任何 datatype 的名字。`ChannelBackend` 的六個方法不變。新增協定 `EnumerableBackend`（`@runtime_checkable`，只有 `enumerate_conditional`），`FakeBackend` 與 `GitHubIssuesBackend` 都符合。
- **`log_cache.py`**：`LOG_CACHE_VERSION` 是 2；版本 1 的快取整檔重建。`LogCache.sync` 的 `backend` 型別標成 `EnumerableBackend`。
- **`cli.py`**：`_format_message` 在 datatype 不是 `message` 時多印一行 `  datatype: …`。PR-4 起註冊 `decision` 子指令（六個 action），只把參數轉交 `decision_cli.cmd_decision`。
- **`decision.py`**（PR-3）：純函式，不做 I/O、不讀時鐘，只 import `messages`、標準庫和 `yaml`（C-20(d)）。公開介面照設計 §13.3：`Record`、`FoldResult`（多一個只給 `snapshot_for` 用的 `snapshot_closed`）、`SubmitPlan`、`PayloadError`、`SubmitRejected`；`parse_payload`（讀取寬鬆）、`build_payload`（寫入嚴格）、`fold(records, *, use_snapshot)`、`snapshot_for(records, asker)`、`plan_submit(results, answerer, entries, *, render)`。#482 PR-3 起多一個公開的排序鍵 `order_key(q: Decision) -> tuple`，回傳 `(時間鍵, channel, id)`：時間鍵以 `asked_at` 解析後的時刻排序（`Z`、不同時區、有無小數秒都比時刻；同一時刻不同寫法鍵相同），`None` 最前，解析不出的（含沒有時區的）最後、彼此依原字串；同時刻依 channel、id 決勝。`fold` 與 `decision_cli` 的 `list` 都用它，`plan_submit` 批次內用同一個時間鍵加 `ref`。#510 PR-3（#517）起時間鍵是 `messages.received_at_key`，`snapshot_for` 的時刻解析是 `messages.parse_received_at`（沒有時區視為解析不出，與先前 `decision.py` 自己那一份行為相同）；仍不 import `backend.parse_backend_time`，依賴方向不變（`decision → messages`，不依賴 `backend`；設計 `docs/design-notes/482-decision-hardening.md` §2.2、D-31）。#509 PR-1（#516）起 payload 用 `messages.FlowDepthLoader` 載入（讀取端太深是 `bad_payload`，寫入端讀回檢查也用它），`decision.py` 不再有自己的 loader。PR-4 起 `FoldResult`、`SubmitPlan` 是 `frozen` 的 dataclass，只用關鍵字建構；`decisions`、`ignored`、`submitted` 的元素以 `TypedDict` 宣告（只給型別，執行期仍是 dict）。PR-4 起讀寫兩邊共用同一個 UTF-8 檢查（`_utf8_text`：寫入端拒送、讀取端整則 `bad_payload`），視圖的題目欄位一律經 `_question_fields` 投影（`options` 每項恰好 `{key, text}`）；`ignored` 的 `ref` 只會是字串或 `null`。§7.1 的相關紀錄計數是 `related_count(records, asker)`（PR-4 結構審 M-A1 從 `decision_cli` 移入；`decision_cli` 只做門檻比較）。`SubmitRejected.problems` 的 `ref` 照項目原樣，型別是 `Any`。snapshot 只省「套用」不省「解析」（設計 §7.5）。
- **`decision_cli.py`**（PR-4）：`cmd_decision` 與六個 action（`ask`、`update`、`withdraw`、`submit`、`list`、`snapshot`）。流程：同步頻道的 log 快取（`LogCache.sync`）→ `decision.fold` 或 `plan_submit` 預檢 → 組 `datatype: decision` 的 `Message` → 寫入前 `serialize_message` 讀回檢查 → `backend.send`。自動 snapshot 在 ask／update／withdraw 寫成功後依 `decision.SNAPSHOT_EVERY` 判斷，失敗只警告。後端參數的型別是 `DecisionBackend`（在 `decision_cli.py` 宣告、使用端擁有的窄協定：繼承 `EnumerableBackend` 取得 `enumerate_conditional`，自己只宣告 `send`，C-20(e)），它不呼叫其他後端方法；離開碼常數在本模組另寫一份（`cli` import 它，不能反向 import），值與 `cli.py` 相同；不讀寫游標檔與 `wait` 狀態檔。
- **`log_query.py`**（`angareion log` 的篩選）：`select(messages_by_channel, selected_channels, *, me, …)` 跨頻道（選了不只一個頻道）時依 `(messages.received_at_key(received_at), channel, id)` 排序；只選一個頻道時仍依 `comment_id`（#510 PR-4，#519）。
- **`client.py`**：`Angareion.read(target, since=None) -> ReadBatch` 把各頻道讀到的訊息合併後依 `(messages.received_at_key(received_at), channel, id)` 排序（#510 PR-4，#519；以前依時刻字串）。
- **依賴方向**：`messages` 只依賴 `ainame`、標準庫和 `yaml`，不 import angareion 的其他模組；`backend → messages`；`log_cache → {backend, messages}`；`decision → messages`；`log_query → {backend, messages}`；`client → {backend, messages, state}`；`decision_cli → {decision, log_cache, backend, messages}`；`cli → decision_cli`。沒有環（設計 §13.2）。

### angareion 頻道換頁（#542；PR-1 已 merge，PR-2／PR-3 待實作）

設計全文在 `docs/design-notes/542-channel-rollover.md`（§6.3 是 PR-2 的 2a／2b／2c 切法與介面簽名）；決定見 decisions D-34。本節寫 PR-1（#544，main `e324857`）已經在 `src/angareion` 的部分；PR-2／PR-3 合上時再補。行數是 main `e324857` 的 `wc -l`。

- **`backend.py`**（718 行）：頁的門檻常數 `PAGE_WARN_AT`（2000）、`PAGE_ROLLOVER_AT`（2400）、`PAGE_COMMENT_LIMIT`（2500）與 `page_level(count) -> str`（`ok`／`warn`／`rollover`／`full`，`None` 是 `unknown`）。例外 `RateLimitError`（`kind` 是 `primary`／`secondary`，帶 `reset_at` 或 `retry_after`）與 `ChannelFullError`（`channel`、`count`、`presumed`；`presumed=True` 是 2400～2499 被拒，訊息是 `looks full (or the token was refused)`），兩者都是 `BackendError` 的子類，CLI 照舊 exit 2。`http_cause(status, detail) -> str` 組訊息尾。新協定 `PageBackend`（`@runtime_checkable`：`page_size(entity) -> int`、`page_size_at(entity) -> tuple[int, str | None]`，後者帶回應的 `Date`）；`ChannelBackend` 不加方法。`FakeBackend` 多 `page_size`／`page_size_at`／`set_page_size` 與 `comment_limit`（`send` 到上限丟 `ChannelFullError`）；留言仍存在單一 list（依頁存放是 PR-2 的 2a）。
- **`github.py`**：`_request` 對 403／429 先判 rate limit（`x-ratelimit-remaining: 0` → primary；有 `retry-after` 或訊息含 `secondary rate limit` → secondary），其他 403 是 `BackendError`；POST 留言被拒（403／422）時 GET 一次 issue 的 `comments` 決定是不是 `ChannelFullError`。GitHub 的 `message` 單行、≤200 字、遮蔽 token、repo 名與 `#<數字>` 去識別。`page_size`／`page_size_at` 是 GET issue 的 `comments`。
- **`registry.py`**：新協定 `RegistryCounter`（`comment_count() -> int`，GET 註冊簿 issue）；`RegistryClient` 與 `FakeRegistryClient` 都實作。
- **`channel_cli.py`**（新，245 行）：`channel create`／`delete`（從 `cli.py` 原樣搬來）與新的 `channel status [NAME…] [--json]`（唯讀：每個頻道的現頁則數、上限、等級、頁數、旗標，最後一列是註冊簿；查不到的列等級 `unknown` 並 exit 2；未註冊的名字 exit 1）。PR-1 的頁數恆為 1、旗標恆為空（2c 補）。離開碼常數與 `mask_token` 的用法照 `decision_cli.py` 的先例，不 import `cli`。
- **`cli.py`**（837 行；A-3 搬出 `channel` 家族之前是 919 行）：`channel` 的三個 action 只轉交 `channel_cli`；`cmd_send` 在 `app.send` 之前讀一次現頁則數，≥2000 在 stderr 印一行警告（讀不到或後端不是 `PageBackend` 就略過）。`decision` 的寫入在 PR-1 沒有這個警告（PR-3 的 C-20 統一）。
- **依賴方向**（PR-1 後；逐檔 grep）：`channel_cli → {backend, channel_config, client, config, messages, registry}`；`cli → channel_cli`；`channel_cli` 不 import `cli`。其餘邊不變，沒有環。PR-2／PR-3 要加的邊（`registry_rules → registry`、`registry_rebuild → registry_rules`、`channel_cli → registry_rules`、`rollover → {backend, registry, registry_rules}`、`client → rollover`、`channel_cli → rollover`）見設計 §8.2，合上時再搬進本節。
