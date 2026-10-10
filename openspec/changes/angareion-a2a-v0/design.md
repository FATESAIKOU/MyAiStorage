## 交本人確認的問題

issue #30 原文寫的訊息格式與這份設計有三處不同；**這三處都待本人確認，本人沒同意之前以原文為準**。若照原文，要改的地方是：這份 design 的 D1／D2、`specs/angareion/spec.md` 的〈六欄位訊息與 front matter 表示法〉與它的 Scenario、`tasks.md` 的 1.2／2.1（含單元測試的欄位名與附件形狀）；其餘（後端、收件人、ack／游標、token、指令）不受影響。

| # | issue 原文 | 這份設計 | 理由 |
|---|---|---|---|
| 1 | 欄位名 `attachment`（單數） | `attachments`（複數） | 值是陣列，複數名和形狀一致；六欄位、CLI 與 `--json` 都用同一個名字。改成原文只是改字面。 |
| 2 | 每項是 `[名稱, {怎麼取得，或直接放內文}]`（兩元素陣列） | 每項是 mapping：`name` 加恰好一個 `ref` 或 `inline` | mapping 好讀、可擴充（未來加 `sha256`），「恰好一個」能寫成驗證規則；pair 形狀沒有多任何好處。 |
| 3 | `subject／channel`（兩個名字並列） | 只用 `channel` 一個欄位名 | 「訊息掛在哪個話題」用 channel 最直，也不和 issue 標題混在一起；`subject` 留給未來真要「一則訊息的標題」時再加。 |

## Context

- MyAiStorage 目前只有 agora（本機 Session 管理）。Angareion 是新的子系統，見 proposal 的 Why；收信端是 Agent sidecar（#31），人用的端是 PMO 與各 repo 的 PM。
- 需求由本人 2026-10-10 定案（issue #30）：private GitHub repo 當後端、一話題一 issue、一訊息一留言、ETag 輪詢、`a2a send／inbox／ack`、後端可換、程式與部署設定放 MyAiStorage。
- 限制：MyAiStorage 是 public repo，設計與程式 MUST NOT 出現信道 repo 的名字、token 或任何私人資訊；信道 repo 名只存在使用者本機的設定檔。

## Goals / Non-Goals

**Goals**

- 定死六欄位訊息與 front matter 表示法，讓不同實作與人看的內容同一套。
- 定死「角色收件人」與 ack／游標的語意，讓收件人換 session、離線都不丟信。
- 把 GitHub 的細節（issue／comment／ETag／速率）包在後端介面後面。
- 讓 sidecar 可以只用指令或程式介面收信，不必知道後端。

**Non-Goals**

- 群組定址（成員展開、群發）——期 2。v0 只保留格式並拒絕送出。
- 同步溝通、presence、已讀回條以外的即時性。
- 訊息編輯、刪除、收回。
- 第二種後端實作（介面留好，不定義第二種）。
- 端到端加密、簽章；信道 repo 是 private，安全邊界就是 GitHub 的權限模型。
- 叫醒（通知、推播）——那是 Agent sidecar 的投遞政策。

## Decisions

### D1 欄位命名：用 `channel`，內文不進 YAML

issue 原文寫「subject／channel：這則訊息掛在哪個話題」。設計取 **`channel`** 當話題欄位名，值是一個字串；issue 原文的另一個名字 `subject` 不用（留給未來真有「一則訊息的標題」時再加，不屬於 v0 六欄位）。理由：channel 描述「訊息掛在哪個話題」的語意最直，且和 GitHub 的 issue 對應時不會和 issue 標題混在一起。

`content` 是 front matter **之後**的整段 Markdown，不寫進 YAML block scalar。理由：留言在網頁上要看起來像留言；只有 metadata 進 front matter，人看得舒服，程式也少一層 block scalar 的縮排陷阱。

### D2 訊息的具體形狀

```yaml
---
a2a: 1
id: 01JABCDE…
at: 2042-10-11T09:30:00+09:00
from: {repo: owner-org/repo-name, role: PM, name: PMO}
to: {repo: owner-org/worker-team, role: team-pm, name: worker-1}
channel: phase-1-task-42
urgency: 7
attachments:
  - {name: "工單", ref: "https://github.com/owner-org/repo-name/issues/42"}
  - {name: "片段", inline: "…" }
---
這裡是內文，Markdown。
```

- 位址：`{repo, role, name}`，`name` 可省略；群組是 `{group: <名>}`。
- `attachments` 每項是 mapping（不是 issue 原文寫的 `[名稱, {…}]` 陣列）：`name` 加恰好一個 `ref` 或 `inline`。理由：mapping 在 YAML 裡可讀、可擴充（未來加 `sha256`），且「恰好一個」能寫成驗證規則；pair 形狀沒有多任何好處。
- `ref` 是不透明字串，v0 文件約定三種常見寫法：`https://…`、`repo:<owner>/<repo>@<ref>:<path>`、`agora:<id>`；解析端不拆解，只有 `name` 必填檢查。
- `id` 是寄件者產生的 ULID；排序用 `at`（同 `at` 用 `id`）。後端的 comment id 不進訊息，是後端自己的事。
- `urgency` 0–9；約定 8–9 給「立刻處理」、4–7 給「儘快」、0–3 給「有空再看」，投遞政策在 sidecar（#31），本系統只保證值原樣送達。

### D3 身分與收件人

- 身分是 **repo × role**；`name` 只影響顯示，不影響送達。一個 role 可以有多個 session／人，共用一個信箱（收件人自己協調誰處理）。
- 群組位址在格式裡保留（`{group: …}` 讀得到、解析不失敗），但 v0：`send` 拒絕、`inbox` 不投遞。理由：群組的成員名單與展開規則是期 2 的設計，先把格式定死，避免以後改格式。
- 送達只看 `repo` 與 `role`；收件人的 inbox 就是「`to` 符合我、且我還沒 ack」。

### D4 後端對應：channel＝issue、訊息＝comment

- 一個 `channel` 名對一個 issue；issue 標題就是 channel 名。新話題要 `--new` 才建立（打錯字不會默默開新話題）。
- 名字是正本、issue number 只是快取：本機記 `channel → issue number`；快取沒了可以用「留言的 `issue_url` 加訊息裡的 `channel`」重建；issue 被人工改標題時也能重建（以訊息裡的名字為準）。
- 風險：兩個寄件者同時 `--new` 同一個名字會開出兩個 issue。v0 接受（單一操作者、低頻），`inbox` 看到兩個同名 issue 時印警告；期 2 再處理。

### D5 讀取與輪詢

- 用 repo 層級的 issue comments 端點（`GET /repos/{owner}/{repo}/issues/comments?since=…&per_page=100`）一次拿一個時間窗的留言，再在客戶端依 `to` 過濾；不逐 issue 打。
- 每個請求帶 `If-None-Match`（ETag 存本機）；304 = 沒有新東西，不重抓。GitHub 對條件式請求的 304 不計入額度。
- 頻道對應與重建才用 `GET /issues`（必要時）。
- `since` 用本機記的最後一次成功時間再往前一點（留言只追加，往前重疊一點安全；重複的用 `id` 去重）。
- 速率：本機記最近的發文時間，超過 80 次／分或 500 次／時就等（等待 ≤ 10 秒）或拒絕（exit 2，訊息沒送出）；後端回 403 + `retry-after` 時等它指定的時間再試一次。讀取走 304，不會是瓶頸。

### D6 ack、游標與重複

- ack 是一則新留言：front matter 有 `ack: <id>`，`from` 是 ack 的人，`to` 照原寄件者，內文可空（可帶 `--note`）。
- `inbox` ＝ 掃留言（時間窗）→ 找 `to` 是我的 → 扣掉「我送出的、`ack` 指向它的」→ 依 `at`、`id` 排序。
- 本機狀態檔（快取）：`<state>/angareion/<identity>.json`：`since`、`etags`、`acked`（加速用）、`channels`。整個檔刪掉只會變慢：從頭掃一遍就能重建，MUST NOT 有「只存在本機」的事實。
- 發文重試：送出後網路失敗時，先用 `id` 查最近的留言；找到就當成功。這是唯一的防重複機制。

### D7 大小與附件

- GitHub 留言上限 65,536 字元；`send` 在送出前算整則留言（front matter + 內文）的長度，超過就拒絕（exit 1），不截斷。
- `inline` 附件直接展開進 front matter；總長算進 65,536。二進位或大檔用 `ref`。
- v0 的 `--attach <name>=<path>` 讀入文字檔轉 `inline`；`--ref <name>=<locator>` 直接放參照。

### D8 token 與能力邊界

- 憑證是本人設定的 **細粒度 PAT**：只勾信道 repo 的 `Issues: Read and write`（`Metadata: Read` 由 GitHub 必附），不給 Contents／Actions／其他 repo。
- 存放：預設 `~/.config/angareion/token`（0600）；可用 `A2A_TOKEN_FILE` 指向別處。程式不列印、不記 log、不放進錯誤訊息，只在對 `api.github.com` 的 `Authorization` 標頭出現。
- 隊員（AI）開發與測試 MUST NOT 拿到 token、MUST NOT 連真信道；GitHub 後端的測試注入假 transport（把 request 收下來、回預設回應），單元測試不開網路。
- 設定檔：`~/.config/angareion/config.json`：`channel_repo`、`identity`（預設 `from`）、`token_file`；`A2A_CONFIG`、`A2A_STATE_DIR` 可覆寫（給測試與多身分機器用）。範例（不含秘密）放 MyAiStorage。
- 輪替步驟寫進文件：GitHub 上重建 PAT → 換檔案 → 舊的撤銷；信道 repo 的存取只經過這一個檔案。

### D9 程式位置與部署設定

- 程式：MyAiStorage `src/angareion/`（`messages.py`、`backend.py`（介面＋假後端）、`github.py`、`cli.py`），`pyproject.toml` 加 package 與 `a2a` console script。
- 部署設定：`docs/angareion.md`（格式、channel 對應、token 步驟、狀態檔位置、輪替）＋ `deploy/angareion/config.example.json`。worker 上的安裝（含 token 的放置）由 #31 的 shared-config 單元負責，本 change 只提供程式與文件。
- 測試：`tests/unit/test_angareion_*.py`；用假後端與假 transport，不碰真 repo；路徑全走 `A2A_*`／`HOME`，測試時指到 tmp（沿用 repo 的 conftest 隔離）。

### D10 CLI 介面細節

- `a2a send …`：stdout 只印 `id` 與 channel（一行），其餘訊息進 stderr；exit 0／1／2。`--id` 可以指定訊息 id（預設自己產 ULID），給 sidecar 這種要重試的呼叫端沿用。
- `a2a inbox --json`：陣列，每項 `{id, at, from, to, channel, urgency, content, attachments, acked, comment_url}`；`--all` 連 ack 過的也列（audit 用）。
- `a2a ack <id>…`：一次多則；找不到 id 的訊息 → exit 1，其餘照做（部分失敗）。
- 所有子指令都讀同一份設定；`--identity`／`--from` 覆寫設定裡的身分。

## Risks / Trade-offs

- [兩個寄件者同時建同名 channel 會開兩個 issue] → v0 單一操作者接受；`inbox` 印警告；期 2 在後端介面加 unique 檢查。
- [issue 標題被人工改掉，channel 對應失效] → 以訊息裡的 `channel` 為正本，從留言重建對應；文件寫明「改標題會讓對應重建，不建議」。
- [GitHub 是第三方，信道的內容對 GitHub 可見] → 私人 repo、不放秘密；真正的秘密走本人設定的 capability，不進訊息。
- [token 洩漏的爆炸半徑＝一個 private repo 的 issues] → 範圍只勾 issues、只放本機 0600、不進 agent 環境（#31）；輪替步驟文件化。
- [留言端點的 `since` 以更新時間計，人工編輯舊留言會讓它再出現] → 以 `id` 去重，重複的忽略。
- [500 次／時的發文上限在大量派工時可能不夠] → 本機節流＋等待／拒絕；真的不夠時是期 2 換後端的訊號（介面已包好）。
- [front matter 在 GitHub 網頁上會被當成普通文字] → 接受；audit 的原始形狀比網頁美觀重要（issue 原文就指定 front matter）。

## Migration Plan

1. 本人建立 private 信道 repo 與細粒度 PAT，放到 `~/.config/angareion/token`（0600），設定 `channel_repo`。
2. 用 `a2a send --new` 送第一則、在另一台 `a2a inbox` 讀到、`a2a ack`，完成 smoke test（本人執行，隊員不執行）。
3. 回滾：本 change 不改任何既有行為；不想要就停用（不跑 `a2a`、撤銷 PAT、封存 repo）。

## Open Questions

- issue 被 close 要不要當成「話題結束」的訊號？v0 不依賴 issue 狀態（inbox 照列）；要用的時候再加，不影響格式與指令。
- 多台機器共用一個身分（同 repo × role）時的 token 是一台一份還是共用一把？v0 讓本人自由（PAT 可以重建多把），能力設計留給期 2。
