# 架構總圖（agora）

維護者：專案架構師。每一次改到模組切割、依賴方向或公開介面，都和引起它的設計文件放在同一個 PR 更新（AGENTS.md「專案架構師可以 commit 的路徑」）。

- **範圍**：目前只收錄 `src/agora`。同一個 repo 裡的 `aicomm`、`ainame`、`angareion`、`atelier`（`pyproject.toml` 的 `[project.scripts]`）還沒收錄，之後再補。
- **基準**：main e57eaad，加上 #341 這張單的設計（`docs/design-notes/121-continue-shell.md`）。行數是 e57eaad 的實數；cli.py 標了 #341 之後的預估。

## 模組地圖

| 模組 | 行數 | 職責 |
|---|---|---|
| `cli.py` | 3346（#341 後約 3110） | `agora` 指令的入口與各子指令；`continue` 的前景流程（代理監看、輪的結果、最後一次寫回、寫回目標解析） |
| `continue_shell.py`（#341 新增） | 約 240 | `continue` 兩條子行程邊界在外殼這一側的協議：啟動、逾時、收行程群組、錯誤對應、diff 套回、狀態編解碼 |
| `errors.py`（#341 新增） | 數行 | 全 CLI 共用的 `InputError` |
| `round_child.py` | 130 | periodic round 的子行程本體（`python -m agora.round_child`） |
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
| `agents/base.py` | 308 | agent 介面（`Agent` Protocol、`Exported`、`Launch`、`AgentError`、`env_seconds`…） |
| `agents/claude.py` | 1385 | Claude Code adapter（含 raw 格式與串流，見 decisions 的待決 121-E） |
| `agents/opencode.py` | 1648 | opencode adapter |
| `agents/agy*.py` | 339＋363＋165＋582＋592 | agy adapter，分成 agy／agy_store／agy_wire／agy_restore／agy_summaries |

## 依賴方向

`A → B` 表示 A import B。以下是頂層 import，函式內的 lazy import 另外標「lazy」。都是 main e57eaad 實際 grep 的結果，#341 新增的部分已標出。

```
header        ← （不 import agora）
errors        ← （不 import agora）                                  [#341]
agents/base   ← （不 import agora）
store         → header
continue_shell→ header, store, errors, agents/base                   [#341；不得 import cli／round_child／start_child]
cli           → header, store, orphan_guard, private_tmp, agents/*, continue_shell[#341], errors[#341]
                lazy: background, cache, migrate, tui, agents.claude, agents.agy_store
round_child   → cli, continue_shell[#341], private_tmp, store
start_child   → cli, store
background    → store
cache         → header, store, agents/base
migrate       → header, store
tui           → cache, header, store, agents.agy；lazy: cli（為了避開循環）
agents/claude, agents/opencode, agents/agy → store（只用 normalize）
```

**規則**：
1. 低層（header、errors、agents/base、store）不 import cli。
2. 子行程本體（round_child、start_child）可以依賴 cli，因為它們在子行程裡跑 cli 的 round 或 load 本體。外殼（cli）和子行程共同依賴的協議，放在 continue_shell。
3. `tui → cli` 只能 lazy。

## 公開介面清單（目前收錄的部分）

- **指令**：`agora = "agora.cli:main"`。
- **agent 介面**：`agents/base.py` 的 `Agent`（Protocol）：`export`、`turns`、`native`、`append_native`、`list_sessions`、`last_message`、`search_text`、`summarize`、`start_native`、`collect`；`Exported`（raw 的載體：`raw_file`／`raw_md5`／`raw_size`／`raw`，以及 `turns`）。
- **store**：`Paths`、`StoreError`、`Drive`（`folder_id`、`list_sessions` …）、`Index`、`stage`、`remember`、`index_pending`、`upload_batch`、`push_outbox`、`kick_uploader`、`hold_upload_lock`、`sync`。不變式見 decisions D-1～D-7。
- **continue 的子行程協議**（#341 之後在 continue_shell）：stdin 送一份 JSON 狀態，stdout 回一份 JSON payload；round 的 payload 必含 `ROUND_DIFF_KEYS`；load 的錯誤用 `{"err": {"kind", "text"}}` 回報。
