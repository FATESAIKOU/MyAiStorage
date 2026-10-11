## Context

本人 2026-10-11 定案（設計提案 v4）：Angareion 是獨立的留言工具，使用者是 agent、程式或人；只有四個介面；後端抽象，這次用 GitHub Issues。v1（`angareion-a2a-v0`，branch `aicompany-p1`）有 ack、收件匣重建、限流、`a2a` 指令，這些都不要了。

## Goals / Non-Goals

**Goals：** 四個介面（必要而且只有這些）；後端可以替換；token 不外洩；agent 閒著時能阻塞地等新訊息而不耗模型額度。

**Non-Goals：** 限流、ack、已讀回條、群組成員管理、推播、和其他元件的整合。

## Decisions

### D1 四個介面

| 介面 | 指令 | 參數 | 回傳 |
|---|---|---|---|
| `create_channel` | `angareion channel create <名稱> [--member <位址>]…` | 名稱、成員 | channel（stdout 印 channel 名與編號） |
| `send` | `angareion send --channel <ch> --to <位址> [--urgency N] [--attach …] [--ref …] (--body <文字> \| --body -)` | channel、訊息 | 訊息 id |
| `wait` | `angareion wait [--channel <ch>] [--timeout <秒>]` | 目標、游標、逾時 | 有新訊息 exit 0；逾時沒有 exit 3 |
| `read` | `angareion read [--channel <ch>] [--since <時刻>] [--json]` | 目標、游標或時刻 | 新訊息（由舊到新），並把游標往前推 |

- 目標：給 `--channel` 就是那個 channel 的全部訊息；不給就是「寄給我的」（所有 channel 裡 `to` 是我的身分的訊息）。
- `wait --timeout 0`：查一次就回（不阻塞）；`--timeout N`：最多等 N 秒；不給：一直等。
- `read` 不給 `--since` 時用本機游標並往前推；給了就照給的、不動本機游標（給程式自己管）。`--since` 是**後端記錄的時刻**（RFC 3339），含邊界（`>=`）：可能重複收到同一秒的訊息，要用訊息 `id` 判斷。為了讓程式拿得到這個時刻，`read` 的每則輸出都帶 `received_at`（後端記錄的時刻；`--json` 是欄位，人讀的格式在標頭行），它不是訊息格式的一部分。
- `send` 的內文只有兩種來源：`--body <文字>`，或 `--body -`（明寫才讀 stdin）；沒給 `--body` 就 exit 1（不再自動讀管線，免得 agent 的行程卡在一個開著卻沒資料的 stdin 上）。
- exit code：0 成功（`wait` 是「有新訊息」）；1 用法或輸入錯誤（含群組位址、不存在的 channel）；2 後端、網路或設定錯誤（含 HTTP 逾時與後端回應解析失敗）；3 `wait` 逾時沒有新訊息。stdout 只放結果，訊息與錯誤放 stderr。

### D2 後端介面與 GitHub Issues 實作

- `ChannelBackend`（Protocol）就是那四個動作：`create_channel(name, members) → Channel`、`send(channel, message) → id`、`check(target, cursor) → bool`（確認有沒有新訊息）、`read(target, cursor) → (messages, cursor)`。`cursor` 是和後端無關的不透明值（見 D4），後端自己決定怎麼對應；`read` 另可給「只有時間」的游標（`--since`）。
- **對外的 `wait`＝在後端的 `check` 上輪詢**：`--timeout 0` 就是呼叫 `check` 一次（不阻塞）；給了逾時或不給，就每 15 秒呼叫一次 `check`，直到有新訊息或逾時（阻塞）。後端只要提供「現在有沒有」，阻塞由函式庫層處理；所以對外仍是四個動作。
- `GitHubIssuesBackend`：信道是一個 private repo；channel＝issue（標題是 channel 名，內文 front matter 記成員）；訊息＝issue 的留言。
  - 「寄給我的」：repo 層的留言列表（`/repos/{owner}/{repo}/issues/comments?since=…`），一次就涵蓋所有 channel，再依 `to` 過濾。
  - `check`：條件式請求（`If-None-Match`／ETag），304 就是沒有；不耗 GitHub 的額度。規則：
    1. ETag **只放在這次 `wait` 行程的記憶體**，不寫進游標（否則 `wait` 會動到游標，下一次 `read` 會因為 304 漏讀）。
    2. **只在回應沒有下一頁（單頁）時才記下並使用 ETag**；回應有 `rel="next"` 時不記、下一次照樣完整掃描。（ETag 只代表第 1 頁的內容；落在第 2 頁以後的新留言不會讓第 1 頁改變，會一直得到 304。）
    3. 為了讓掃描不必每次都從游標掃到最新，`wait` 行程內把「已掃過的最後一則的後端時間減 `margin`」記成 `scan_since`，之後的查詢用 `max(游標的 at − margin, scan_since)`；它只存在這次行程的記憶體，也不寫進游標。
  - 阻塞等待時每 15 秒查一次（固定間隔，不另做限流）。
  - **HTTP 逾時**：每個請求的 socket 逾時是 30 秒（固定值）；`wait --timeout N` 時再取 min(30 秒, max(5 秒, 剩餘時間))（下限 5 秒讓慢網路也試得完，上限 30 秒不讓半開連線拖過頭），所以 `wait` 不會超過 `N` 太多。逾時是後端錯誤（exit 2），不自動重試。
  - **回應解析失敗**（不是 JSON、或形狀不是預期的列表／物件）是後端錯誤（exit 2），不是輸入錯誤。
  - **找 channel**：列出 issue 時用 `state=all&sort=created&direction=asc`（最舊的在前），標題相同時**以最舊的為準**（第一個符合的就用，找到就停）；PR 不算。`create_channel` 在建立之後再查一次，如果出現比自己更舊的同名 issue（兩個行程同時建立的競態），回傳那一個、不使用自己剛建的（不去關它）。
- v1 的 GitHub 後端（HTTP transport 可注入、ETag、分頁、`mask_token`）移植過來；拿掉 ack、`RateLimiter`、`rebuild_from_backend`。

### D3 訊息格式與位址

- 留言內文是 front matter＋本文：`angareion: 1`（格式版本）、`id`（ULID）、`at`（ISO 8601）、`from`、`to`、`channel`、`urgency`（0～9，預設 5），本文是 `content`；附件是 `ref`（連結）或 `inline`（小檔）。v1 的 `messages.py` 移植過來。
- 位址：`{repo, role, name?}`；身分（`from` 的預設）來自設定。群組位址 v0 不支援（送出時拒絕）。

### D4 游標與本機狀態

- 只有游標：`~/.local/state/angareion/cursor-<身分>.json`（`ANGAREION_STATE_DIR` 可覆寫），每個目標一筆；刪掉就從頭讀，不需要從信道重建別的東西。
- **新舊只看後端，不看寄件者**：訊息的 `at` 與 `id`（ULID）是寄件者的機器產生的（時鐘、時區、格式都可能不同），訊息到達的先後也不保證和它們一致，所以 MUST NOT 用它們判斷「新不新」。游標是和後端無關的三個欄位：
  - `position`：後端給的**到達位置**，不透明的字串，只有那個後端自己比得大小；
  - `at`：後端記錄的時刻（RFC 3339，UTC），最後讀到的那則的；
  - `seen`：在 `at − margin` 以內、已經讀過的後端識別（不透明字串）；`margin` 固定 60 秒，超過的就從 `seen` 丟掉。
- **GitHub 的對應**：`position` 是留言 id（十進位字串，單調遞增，依到達順序）；`at` 是留言的 `created_at`；`seen` 是留言 id。一則留言「新」的條件是：它的留言 id 大於 `position`，**或者**（它的 `created_at` 不早於 `at − margin` 而且 id 不在 `seen` 裡）；後者是為了容納 GitHub 偶爾的複寫延遲（較小的 id 比較大的 id 晚出現）。`since` 查詢用 `at − margin`（GitHub 的 `since` 依 `updated_at`、秒級、含邊界；被編輯過的舊留言因為 id 不大於 `position` 且不在視窗內，不會再出現成新訊息）。
- `read` 讀完後的新游標：`position` 取讀到的最大留言 id，`at` 取它的 `created_at`，`seen` 重算。
- `--since <時刻>`（沒有 `position`、不動本機游標）：回傳後端時刻 `>=` 該時刻的訊息，含邊界；同一秒的訊息可能重複收到，由程式用訊息 `id` 判斷。
- ETag 與 `scan_since` 都不屬於游標（D2）。

### D5 設定與 token

- 設定：`~/.config/angareion/config.json`（`ANGAREION_CONFIG` 可覆寫）：`channel_repo`、`identity`、`token_file`。
- token 的位置，依序：`ANGAREION_TOKEN_FILE` → 設定的 `token_file` → `~/.config/angareion/token`。檔案只能擁有者有權限（0400 或 0600），否則拒絕（exit 2）。Angareion 不寫死任何環境專屬的路徑；由外部以 `ANGAREION_TOKEN_FILE` 指定（worker 上怎麼做見 `docs/angareion.md` 的「給 MyLinuxPool 的需求」）。第二、三層只是方便本機使用的實作細節，不是介面的一部分。
- token 不出現在任何輸出、log、錯誤訊息；v1 的 `load_token`、`mask_token` 移植過來。

### D6 給 agent 的說明（skill）

- `skills/angareion/SKILL.md`：什麼時候用、四個指令的用法、身分與 channel、閒著時用 `angareion wait --timeout <秒>` 阻塞地等（不耗模型額度）、收到 urgency 高的訊息怎麼辦。
- 怎麼送到 agent 是使用它的環境的事（需求在 `docs/angareion.md`）。Angareion 不為此加子指令，對外仍只有四個介面。

## Risks / Trade-offs

- [agent 讀得到信道 token] → 本人接受（Q6）；token 的範圍只有信道 repo 的 Issues，爆炸半徑是那個 private repo。
- [阻塞等待用輪詢] → GitHub 沒有推播；條件式請求的 304 不耗額度，15 秒一次可以接受。
- [不限流] → 本人決定；GitHub 次級限制被打到時照 HTTP 回應失敗（exit 2），不自動重試。
