# 架構總圖（agora）

維護者：專案架構師。每一次改到模組切割、依賴方向或公開介面，都和引起它的設計文件放在同一個 PR 更新（AGENTS.md「專案架構師可以 commit 的路徑」）。

- **範圍**：目前收錄 `src/agora` 與 `src/pmo`（PMO skill 見下面「PMO skill」一節）。同一個 repo 裡的 `aicomm`、`ainame`、`angareion`、`atelier`（`pyproject.toml` 的 `[project.scripts]`）還沒收錄，之後再補。
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
| `cli.py` | 938 | `pmo` 的入口與各子指令；把錯誤對到結束碼；不實作行為 |
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
| `decision.py` | 211 | 決定題的建構與作答解析 |
| `sanitize.py` | 99 | 輸出與內容的憑證樣式掃描 |
| `paths.py` | 35 | `PMO_SKILL_CONFIG`／`PMO_SKILL_STATE_DIR`（沒設就拒絕） |
| `errors.py` | 55 | 共用例外（`UsageError`、`SchemaError`、`SentButNotRecorded`…） |
| `_io.py`／`_jsonutil.py`／`_lock.py` | 43／181／145 | 原子寫入、JSON 欄位驗證、`path_lock` |
| `timeutil.py` | 44 | 時刻的格式與解析（一個格式一個函式） |
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
      decision → runtime
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
