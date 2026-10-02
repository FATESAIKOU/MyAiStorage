# Agora lite 基本設計

第 4 版（2026-10-02）：標頭改成 OKF frontmatter、命令改成「動作 型態 [id] [選項]」、新增 delete 與 edit、拿掉 sync。第 3 版：第 2 版併入 `docs/review/design.md` 的 S1–S10、H1–H7、L1–L4 與 `docs/review/test-plan.md` 的 T1、T2；第 3 版再併入 spike 結果（`docs/spike/`）與第 2 版確認的 N1–N17。

取代期 1 的實作（約 3.2 萬行程式碼，大多在防「AI 住民篡改真本」與「把 git 放在 Drive 上」）。舊實作留在 git 歷史與 main，這個 branch（`agora-lite`）從零開始。

## 1. 目的

讓 coding agent 的 Session **可以被找到、被合併、被接著做**，而且不綁在哪一個 agent、哪一台機器上。

- **找**：用關鍵字或 header 找到過去的 Session。
- **合**：把幾個 Session 的成果合成一個，交給下一個 agent。
- **接**：用 opencode 或 Claude Code 從某個 Session 接著做；agent 結束時，結果自動存回來。

這是使用者自己每天的 workflow 工具，所以以「指令少、等待短、好理解」為第一優先。

## 2. 前提與決定（使用者確認）

| # | 決定 | 理由／影響 |
|---|---|---|
| D1 | **儲存在 Google Drive 資料夾**，放一般檔案（不用 git-annex） | 已付費的 Drive；用 rclone 存取 |
| D2 | **信任自己的機器** | 不做簽章、收件匣、單一提交者、pin repo。寫入是本機直接寫 |
| D3 | **四個實體的概念全部保留**（MyBrain／Agora／Foundry／Atelier），彼此靠 **header** 參照 | 程式這次只實作 Agora |
| D4 | **continue 的結果是新的 Session**，header 記下來源 | 可以分岔；merge 的結果也有地方放 |
| D5 | Drive 憑證只用 **worker OAuth client（`drive.file`）** | 只看得到自己建的檔案 |
| D6 | **閱讀版＝user／assistant 的文字＋每次工具呼叫一行摘要**；不收工具結果、不收 thinking（2026-10-02） | 搜尋與跨 agent 接續都用它 |

### D5 的注意事項（S8）

- `agora/` 根資料夾第一次執行時建立，把 **folder ID** 寫進 `~/.config/agora/config.json`；之後所有存取都用 ID，不靠名字找（Drive 允許同名資料夾）。`sync` 發現同名資料夾時警告。
- **只能透過 agora 寫入。** 從 Drive 網頁拖進去的檔案，`drive.file` 看不到。
- **不要刪除或重建 worker OAuth client。** 換了 client，以前建的檔案全部看不到（資料還在）。萬一發生，復原方法是用一次性的 `drive` scope client 把 `agora/` 複製成新 client 擁有的檔案。
- OAuth consent screen 要是「In production」，否則 refresh token 7 天失效（V4 確認現況）。

## 3. 共通 header

四個實體的每一個項目都帶同一種 header（YAML front matter，第一個 `---` 區塊）。**實體之間只靠 header 互相指，不共用程式或儲存。**

### 3.1 標頭就是 OKF frontmatter（2026-10-02 使用者決定）

每個項目的標頭**用 MyBrain 筆記同一套 OKF v0.2 frontmatter**，所以 MyBrain 的任何欄位 Agora 都能放，兩邊可以用同一套工具讀。OKF 只要求 `type`，其他欄位都選填，**不認得的欄位一律保留**。

| 欄位 | 來源 | 意思 |
|---|---|---|
| `type` | OKF（必填） | 形式。Agora 的 Session 是 `Session` |
| `title`、`description`、`tags` | OKF | 標題、一句話摘要、標籤（清單） |
| `status` | OKF | `draft`／`stable`／`deprecated` |
| `generated` | OKF | `{by: <actor>, at: <ISO 8601 UTC>}`；actor 照 OKF 慣例：`opencode/<模型>`、`claude-code/<模型>`、`human:fatesaikou` |
| `verified` | OKF | `[{by, at}]`；**只有本人能加** |
| `sources` | OKF | `[{id, title, resource, author, last_modified}]` |
| `stale_after` | OKF | 絕對日期 |
| `id` | 四實體共通 | `<entity>:<不變 id>`；Agora 用 ULID |
| `refs`、`case` | 四實體共通 | 指向其他實體的 ref（3.2）；`case` 是 ref 或 `null` |
| `agora` | Agora 專用 | 系統欄位，見 3.4 |

### 3.2 ref 的語法（H2）

```
ref    = entity ":" locator [ "@" rev ] [ "#" fragment ]
entity = "mybrain" | "agora" | "foundry" | "atelier"
```

- 只用**第一個** `:` 切開；locator 裡的 `@`、`#` 要百分比編碼。
- 例：`mybrain:技術/動手做/AiStorage.md`、`agora:01K6XYZ...`、`atelier:reviewer@v3`、`foundry:<id>#sec2`。
- MyBrain 已經有自己的 frontmatter（OKF），那邊不改；Agora 指向它時用路徑，等 MyBrain 有不變 id 之後改用 id。
- 反向連結（「哪些 Session 參照了這篇」）由各實體自己建索引，不寫進 header。

### 3.3 放不了 YAML 的項目（H5）

項目本身放不了 front matter（Google Docs、二進位檔）時，旁邊放一個 sidecar `<檔名>.header.md`，內容只有 header。

### 3.4 Agora 的標頭

```yaml
---
type: Session
title: "CSV 轉 Markdown 的規劃"
description: "把 CSV 轉成 Markdown 表格，先列三個步驟"   # 自動：第一則 user 訊息的前 80 字
tags: []
generated: {by: "opencode/space-bunny-free", at: 2026-10-01T11:00:00Z}
sources:
  - {id: "opencode:ses_xxxx", title: "opencode session", author: "opencode/space-bunny-free", last_modified: 2026-10-01}
id: agora:01K6XYZ...
refs: ["mybrain:技術/動手做/AiStorage.md"]
case: null
agora:                                # 系統欄位，使用者不能改
  header: 2
  created_at: 2026-10-01T12:00:00Z    # 存進 agora 的時間
  updated_at: 2026-10-01T12:30:00Z
  relation: import                    # import | continue | merge
  parents: [{id: "agora:01K5...", raw_md5: "…"}]
  source: {agent: opencode, session_id: ses_xxxx, dir: /Users/me/proj, host: mbp, agent_version: 1.18.34, created_at: 2026-10-01T11:00:00Z}
  raw: {file: raw-0123456789ab.json, md5: "…", size: 12345}
---
```

**自動填的欄位**（import／merge／continue）：`type`、`title`（來源的標題）、`description`、`generated`（agent 與模型、來源的建立時間）、`sources`（來源 session；continue／merge 另外列出父 Session），以及整個 `agora` 區塊。`status` 不自動填（OKF 預設 `stable`），`verified` 永遠不自動填。

### 3.5 使用者給的標頭：差分疊加

順序是 **自動填 → `--header-file <yaml>` → `--header key=value`（依序）**，後面的覆蓋前面的：

- `--header-file`：一份 YAML（只有標頭，可以有也可以沒有 `---`）。對應的 mapping 會遞迴合併，清單與純量整個覆蓋。
- `--header key=value`：可以給多次。key 可以用點路徑（`generated.by=human:fatesaikou`），value 用 YAML 解析，所以 `tags=[csv, 表格]`、`sources=[{id: x, title: y}]` 都可以。**同一個 key 就覆蓋**。
- 不含 `=` 的參數是錯誤（請寫成 `description=…`）。
- **不能改的**：`id` 與整個 `agora` 區塊（系統欄位）；`type` 只能是 `Session`。違反時報錯。
- `refs`、`case` 的值要符合 ref 語法（3.2）。

## 4. 儲存

```
Drive: agora/                              （worker client 建立；以 folder ID 存取）
  sessions/<ULID>/
    session.md                header ＋ 閱讀版；「這個版本完成了」的標記
    raw-<md5 前 12 碼>.json   來源 agent 的原始匯出（N15）：
                              opencode＝export 的 JSON 原封不動；
                              claude＝{"format":"claude-jsonl/1","main":[每一行原字串],"aux":{"<相對路徑>":"內容"}}
Mac:
  ~/.config/agora/            rclone.conf（worker client）、config.json（folder ID）
  ~/.cache/agora/             鏡像（只有 session.md）＋ index.sqlite；壞了刪掉重建
  ~/.local/state/agora/       outbox/、pending/ ——不能刪
```

### 4.1 寫入順序（S1）

1. 先把整個 Session 寫進 outbox（`~/.local/state/agora/outbox/<ULID>/`）。
2. 上傳 raw（檔名帶 md5，所以重新匯入時是新檔名，不會蓋掉舊的）。
3. 最後上傳 session.md（指向剛才那份 raw）。
4. 用 Drive 的 md5 確認兩個檔都對了，才移出 outbox；舊的 raw 這時才刪。

任何時候 Drive 上的 session.md 都指向一份完整存在的 raw。讀的一方看到 raw 的 md5 和 header 對不上，就當作「還沒寫完」：跳過、不建索引、下次再試。

### 4.2 outbox 與 pending（S2、S3）

- **reading**：注入用的閱讀版暫存在 `reading/<ulid>.md`，接續結束就刪（D5）。
- **outbox**：上傳失敗時留在這裡。`sync` 一律**先推再拉**；每個指令開始時，outbox 不是空的就印一行提示。
- **pending**：continue-session 啟動 agent **之前**寫一份 `{agora_id, parent, agent, agent_session_id, dir, started_at, before_count}`，建立時就已經上鎖（`flock`），並把鎖用 `pass_fds` 交給 agent 一起持有（C1、C3）。agent 結束後收尾成功才刪。任何 agora 指令開始時檢查 pending：**拿得到 flock**（表示 agora **和** agent 都已經不在了）才補做收尾；`--no-sync` 時只提示不補存；`agora_id` 事先決定，所以補存是冪等的（N2）。
- 上傳失敗、留在 outbox 時 exit code 是 3；outbox 裡的 Session 在這台機器上照樣搜得到（N13）。
- 壞掉的本機檔案（pending 不是合法 JSON、outbox 缺 session.md、header 的 YAML 壞了）移到 `.bad/`，不刪、不讓其他指令跟著壞；Drive 上的壞檔只跳過（C2）。
- header 沒有 `raw` 的 session.md（merge）本身就是完整的；讀的一方比對 raw 用的是 `lsjson --hash` 列出的 md5，不下載 raw（N5、N11）。

### 4.3 sync（S5、L2）

- 列檔一律 `rclone lsjson -R --fast-list --hash`，一次拿到所有檔案的 md5；用 md5 判斷要不要下載（不用時間）。
- 鏡像只抓 `session.md`；raw 在 continue 或 `show --raw` 用到時才抓。
- Drive 上已經不存在的 Session，從鏡像和索引刪掉（outbox、pending 不動）。但 Drive 上**找不到 `sessions/`**（folder ID 或 token 出問題）時不刪，只警告（N12）。
- continue 時抓不到 raw（另一台機器重新匯入、刪了舊 raw），先重抓那一份 session.md 再試一次（N8）。
- 上傳一律用 `rclone copyto`：`rclone copy` 目的地寫成檔名時會靜默建成同名資料夾（spike V4）。
- search 前的 sync 有節流：距離上次成功 sync 不到 5 分鐘就跳過；`--no-sync` 直接跳過；網路不通時查本機，印一行警告。
- import 前一律做一次不節流的 sync（S6）。

### 4.4 閱讀版（D6、L1）

```markdown
## user
<文字>

## assistant
<文字>
[tool] <名稱> <參數摘要，最多 200 字，超過標「…」>
```

- 只收 user／assistant 的文字與工具呼叫的一行摘要；工具結果、thinking／reasoning 不收。
- 認不得的型態輸出 `[skip <型態>]`，不讓程式失敗。
- merge 出來的閱讀版，每一段前面標 `# from agora:<id>`。

### 4.5 搜尋索引（T1、T2）

- SQLite FTS5 trigram，索引閱讀版與 header 的文字欄位。
- 建索引與查詢前都做 **NFKC 正規化**（全形／半形）。
- **關鍵字少於 3 個字時**（例如「表格」），trigram 比不到，改用 `instr()` 掃描閱讀版的表。
- 搜尋結果的片段取自正規化後的文字，所以是小寫、可能混著 title／note。

## 5. 指令（2026-10-02 使用者重新整理）

```
agora <動作> <型態> [session_id] [選項]

agora search   session [--filter KEY=VALUE | --filter KEY~=TEXT]... [--no-sync]
agora import   session --external-session-id <id> --agent opencode|claude [--header-file F] [--header K=V]...
agora merge    session <id>, <id>, ... [--header-file F] [--header K=V]...
agora continue session <id> --agent opencode|claude [--dir <專案目錄>] [--header-file F] [--header K=V]...
agora delete   session <id> --yes
agora edit     session <id> [--header-file F] [--header K=V]...
agora show     session <id> [--raw]
```

- 位置參數固定是「動作、型態、session id」。型態目前只有 `session`。
- `--agent`：import 時是「這是哪個 agent 的 session」，continue 時是「用哪個 agent 接」。不再有 `--format`。
- `--external-session-id`：只有 import 用，是 agent 自己的 session id。
- `sync` 拿掉了：每個指令開頭都會自動推 outbox、需要時拉 Drive（4.3），使用者不必自己下。
- 所有寫入指令的輸出第一欄都是 agora id。exit code：0 成功；1 做不到（找不到 id、沒有訊息、merge 少於兩個、delete 沒加 `--yes`、有子 Session）；2 錯誤；3 已存進 outbox、還沒上傳。

### 5.1 search

- `--filter KEY=VALUE`：**全等**；`--filter KEY~=TEXT`：**包含**。可以給多次，全部都要成立（AND）。
- KEY 是標頭的點路徑（`type`、`tags`、`status`、`generated.by`、`agora.source.agent`…），另外有兩個特別的 key：
  - `text`：閱讀版加上標頭裡的文字欄位（全文；`text~=表格`）；
  - `agent`：`agora.source.agent` 的簡寫。
- 清單欄位（`tags`、`refs`）：`=` 是「清單裡有一個全等」，`~=` 是「有一個包含」。
- 沒有 filter 就列出全部。之後要加更複雜的比較時，再加運算子（例如 `>`、`<`）。
- 全文 `~=` 用 FTS5 trigram，少於 3 個字時改用掃描（4.5）。

### 5.2 import

- `--agent opencode`：`opencode export <id> > 檔案`，stderr 另外導走、**不能 `2>&1`**。檢查 JSON 能解析、message 數大於 0（S10）。`agora.source.dir` 取 `info.directory`（N9）。
- `--agent claude`：`~/.claude/projects/*/<id>.jsonl` 加上 `<id>/` 附屬資料夾（S9）；最後一行不完整就丟掉並警告（S10）。`agora.source.dir` 取 jsonl 的 `cwd`（N9）。
- 只上傳指定的那一個 Session。
- 同一個來源 Session 再匯入：內容沒變、也沒有 `--header`，就不做事；內容沒變但有 `--header`，只更新標頭；內容變了而且還沒有子 Session，就更新同一個 agora id；已經有子 Session，就建一個新的（`relation: import`、`parents: [舊 id]`，S7）。

### 5.3 merge

- 產生一個新的 Session：`relation: merge`、`parents` 依給的順序。id 用空白或逗號分隔都可以（`agora merge session id1, id2, id3`）。
- 閱讀版是各來源依序串接，每段標出來源；沒有 raw，接續時一律用閱讀版注入。

### 5.4 continue

（流程同第 3 版：原生載入或閱讀版注入、pending＋flock、PWD、/clear 的處理。）

1. 決定載入方式：

   | 來源 | 目標 | 方式 |
   |---|---|---|
   | 單一 opencode | opencode | **原生**：三種 id 全部重編後 `opencode import`，回頭 export 比對訊息數 |
   | 單一 claude | claude | **原生**：新 uuid、改寫 `sessionId`（有 `cwd` 的行改成工作目錄），`claude --resume <新uuid>` |
   | 跨 agent，或 merge 出來的 | opencode | **閱讀版注入**：一則 user 訊息帶全文的 export，`opencode import` 後開啟 |
   | 跨 agent，或 merge 出來的 | claude | **閱讀版注入**：`claude --session-id <新uuid> "@<閱讀版絕對路徑> …"` |

2. 工作目錄：`--dir`，預設是 `agora.source.dir`（這台機器上存在的話），否則是目前目錄。agent 啟動時同時設定 cwd 與 `PWD`。
3. 寫 pending 並持有 flock（交給 agent 繼承），在前景啟動 agent；Ctrl-C 只給 agent。
4. agent 結束後，內容比啟動前多才存成新 Session（`relation: continue`、`parents: [{id, raw_md5}]`）。Claude 用過 `/clear` 時，照常存 `/clear` 之前的部分，並印出之後那個 session 要用的 import 指令。

### 5.6 delete

- 把 Drive 上 `sessions/<ULID>/` 整個**移到 Drive 垃圾桶**（30 天內可以在 Drive 網頁還原），本機的鏡像與索引一起拿掉。
- 一定要加 `--yes`，沒加就只印出會刪什麼，exit 1。
- 有子 Session（別的 Session 的 `parents` 指向它）時不刪，列出那些子 Session，exit 1。

### 5.7 edit

- 只改標頭，不動 raw 與閱讀版；改完存成同一個 agora id 的新版本 session.md。
- 有給 `--header`／`--header-file` 就照 3.5 疊加；**都沒給就用 `$EDITOR` 打開標頭的 YAML**，存檔關掉之後驗證並上傳，驗證不過就不存並印出原因。
- `id`、`agora` 區塊同樣不能改。`verified` 由本人在這裡加。

### 5.8 show

- 印出標頭與閱讀版；`--raw` 印出原始匯出。

### 5.5 Session 壓縮

不自己做，用各 agent 內建的：opencode 的 `/compact`（context 快滿時也會自動壓縮），Claude Code 的 `/compact [要保留什麼]`。壓縮後的結果會在 agent 結束時被存回去。

## 6. 不做的事

- 簽章、收件匣、提交流程、pin repo、隔離、抹除流程（D2）。要刪 Session 就是刪掉 Drive 上那個資料夾，下一次 sync 時鏡像跟著刪。
- 背景同步 daemon。
- Foundry、Atelier 的程式（只保留 header 規則）。
- 跨機器同時改同一個 Session 的衝突處理（最後寫的贏；S7 讓舊版本不會被子 Session 弄丟）。

## 7. spike 的結論（2026-10-02，詳見 `docs/spike/`）

| # | 結論 |
|---|---|
| V1 | **可行。** 三種 id 全部重編後 import，原本的 session 位元組不變，新訊息接在後面。只換 session id 會靜默失敗（rc=0、0 則訊息），所以一定要回讀驗證 |
| V2 | **可行。** 只需改每行頂層 `sessionId`；`--session-id` 搭 `--resume` 必須加 `--fork-session`；用過 Task 的 session 有 `subagents/` 附屬檔；進行中讀 jsonl 沒看到半行（仍防禦性處理） |
| V3 | **可行。** opencode 用一則 user 訊息帶全文；claude 用 `@<路徑>` |
| V4 | **可行。** `drive.file` 下建立、上傳、列出、增量、刪除都行；要用 `copyto`；access token 約 1 小時、rclone 自己刷新，agora 不碰 token |
| V5 | opencode 的 session 屬於 import 時的 cwd；claude 的新檔落在啟動 cwd 的編碼目錄。continue 一律在工作目錄開 |

## 7.1 驗證時的原問題（保留）

| # | 問題 | 負責 |
|---|---|---|
| V1 | opencode：所有 id 換新之後 import，能接著做，**原本的 session 完全沒變**；只換 session id 會怎樣（S4） | impl1 |
| V2 | Claude Code：複製 jsonl 成新 uuid 之後 `--resume`；附屬資料夾；要改寫哪些欄位；`--session-id` 能否和 `--resume` 一起用；寫到一半的最後一行（S9、S10） | impl2 |
| V3 | 閱讀版注入：兩個 agent 都能在第一則訊息讀檔並接著做 | impl1、impl2 |
| V4 | rclone＋`drive.file`：建立、上傳、`lsjson --hash`、增量下載、同名資料夾、folder ID 存取、token 狀態（S8） | impl1 |
| V5 | 兩個 agent 的 session 與工作目錄的關係，接續時要在哪個目錄開 | impl1、impl2 |

**測試只用自編的 Session 與 Drive 上的 `agora-test/`。** 真實的 Session 與 MyBrain 內容不交給隊員的外部模型處理。

## 8. 實作規模

Python（uv）＋ rclone ＋ SQLite FTS5。**`src/` 的程式碼目標 2,000 行以內，不含測試與 fixture**（L4）。**算的是程式碼行**（不含空行、註解、docstring；2026-10-03 決定）：說明安全機制的 docstring 不該為了行數砍掉。第 4 版時檔案總行數約 2,200、程式碼行約 1,500。

| 模組 | 內容 | 估計行數 |
|---|---|---|
| `header` | header 讀寫、驗證、ref 解析、`--header` 解析 | 150 |
| `store` | rclone 呼叫、outbox、鏡像、索引（含 NFKC 與二字詞退路） | 400 |
| `agents/opencode` | export、id 改寫後 import、閱讀版、找出結束後的 session | 300 |
| `agents/claude` | 找 jsonl 與附屬檔、改寫欄位、閱讀版、找出結束後的 session | 350 |
| `cli` | 六個指令、continue 流程、pending 補存、merge | 400 |

測試照 `docs/review/test-plan.md`。測試用的接縫：`AGORA_FOLDER_NAME`（整合測試設成 `agora-test`）、`AGORA_CONFIG`、`AGORA_CACHE_DIR`、`AGORA_STATE_DIR`、`AGORA_RCLONE`、`AGORA_OPENCODE_CMD`、`AGORA_CLAUDE_CMD`、`AGORA_CLAUDE_HOME`、`AGORA_TEST_FAULT`、`AGORA_NOW`。
