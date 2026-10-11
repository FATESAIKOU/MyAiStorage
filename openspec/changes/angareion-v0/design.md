## Context

本人 2026-10-11 定案（設計提案 v4）：Angareion 是獨立的留言工具，使用者是 agent、程式或人；只有四個介面；後端抽象，這次用 GitHub Issues。v1（`angareion-a2a-v0`，branch `aicompany-p1`）有 ack、收件匣重建、限流、`a2a` 指令，這些都不要了。

## Goals / Non-Goals

**Goals：** 四個介面（必要而且只有這些）；後端可以替換；token 不外洩；agent 閒著時能阻塞地等新訊息而不耗模型額度。

**Non-Goals：** 限流、ack、已讀回條、群組成員管理、推播、和 sidecar 的任何整合。

## Decisions

### D1 四個介面

| 介面 | 指令 | 參數 | 回傳 |
|---|---|---|---|
| `create_channel` | `angareion channel create <名稱> [--member <位址>]…` | 名稱、成員 | channel（stdout 印 channel 名與編號） |
| `send` | `angareion send --channel <ch> --to <位址> [--urgency N] [--attach …] (--body <文字> \| stdin)` | channel、訊息 | 訊息 id |
| `wait` | `angareion wait [--channel <ch>] [--timeout <秒>]` | 目標、游標、逾時 | 有新訊息 exit 0；逾時沒有 exit 3 |
| `read` | `angareion read [--channel <ch>] [--since <游標>] [--json]` | 目標、游標 | 新訊息（由舊到新），並把游標往前推 |

- 目標：給 `--channel` 就是那個 channel 的全部訊息；不給就是「寄給我的」（所有 channel 裡 `to` 是我的身分的訊息）。
- `wait --timeout 0`：查一次就回（不阻塞）；`--timeout N`：最多等 N 秒；不給：一直等。
- `read` 不給 `--since` 時用本機游標並往前推；給了就照給的、不動本機游標（給程式自己管）。
- exit code：0 成功（`wait` 是「有新訊息」）；1 用法或輸入錯誤；2 後端、網路或設定錯誤；3 `wait` 逾時沒有新訊息。stdout 只放結果，訊息與錯誤放 stderr。

### D2 後端介面與 GitHub Issues 實作

- `ChannelBackend`（Protocol）：`create_channel(name, members) → Channel`、`send(channel, message) → id`、`has_new(target, cursor) → bool`、`read(target, cursor) → (messages, cursor)`。`wait` 的阻塞是 CLI／函式庫層用 `has_new` 輪詢做的，後端只要提供「現在有沒有」。
- `GitHubIssuesBackend`：信道是一個 private repo；channel＝issue（標題是 channel 名，內文 front matter 記成員）；訊息＝issue 的留言。
  - 「寄給我的」：repo 層的留言列表（`/repos/{owner}/{repo}/issues/comments?since=…`），一次就涵蓋所有 channel，再依 `to` 過濾。
  - `has_new`：條件式請求（`If-None-Match`／ETag），304 就是沒有；不耗 GitHub 的額度。
  - 阻塞等待時每 15 秒查一次（固定間隔，不另做限流）。
- v1 的 GitHub 後端（HTTP transport 可注入、ETag、分頁、`mask_token`）移植過來；拿掉 ack、`RateLimiter`、`rebuild_from_backend`。

### D3 訊息格式與位址

- 留言內文是 front matter＋本文：`angareion: 1`（格式版本）、`id`（ULID）、`at`（ISO 8601）、`from`、`to`、`channel`、`urgency`（0～9，預設 5），本文是 `content`；附件是 `ref`（連結）或 `inline`（小檔）。v1 的 `messages.py` 移植過來。
- 位址：`{repo, role, name?}`；身分（`from` 的預設）來自設定。群組位址 v0 不支援（送出時拒絕）。

### D4 游標與本機狀態

- 只有游標：`~/.local/state/angareion/cursor-<身分>.json`（`ANGAREION_STATE_DIR` 可覆寫），內容是每個目標最後讀到的時刻與 ETag。刪掉就從頭讀；不需要從信道重建別的東西。

### D5 設定與 token

- 設定：`~/.config/angareion/config.json`（`ANGAREION_CONFIG` 可覆寫）：`channel_repo`、`identity`、`token_file`。
- token 的位置，依序：`ANGAREION_TOKEN_FILE` → 設定的 `token_file` → `~/.config/angareion/token`。檔案只能擁有者有權限（0400 或 0600），否則拒絕（exit 2）。worker 上由 MyLinuxPool 的 Angareion 能力放在 `/run/mlp-secrets/angareion-token`（0400、uid 1000），並設 `ANGAREION_TOKEN_FILE`。
- token 不出現在任何輸出、log、錯誤訊息；v1 的 `load_token`、`mask_token` 移植過來。

### D6 給 agent 的說明（skill）

- `skills/angareion/SKILL.md`：什麼時候用、四個指令的用法、身分與 channel、閒著時用 `angareion wait --timeout <秒>` 阻塞地等（不耗模型額度）、收到 urgency 高的訊息怎麼辦。
- 放到哪裡由 MyLinuxPool 的 Angareion 能力決定（例如 Claude Code 的 `~/.claude/skills/angareion/SKILL.md`；opencode 的位置在實作時確認後寫進 `docs/angareion.md`）。

## Risks / Trade-offs

- [agent 讀得到信道 token] → 本人接受（Q6）；token 的範圍只有信道 repo 的 Issues，爆炸半徑是那個 private repo。
- [阻塞等待用輪詢] → GitHub 沒有推播；條件式請求的 304 不耗額度，15 秒一次可以接受。
- [不限流] → 本人決定；GitHub 次級限制被打到時照 HTTP 回應失敗（exit 2），不自動重試。
