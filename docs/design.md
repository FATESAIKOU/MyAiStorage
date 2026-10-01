# Agora lite 基本設計

第 2 版（2026-10-02）：併入 `docs/review/design.md`（S1–S10、H1–H7、L1–L4）與 `docs/review/test-plan.md`（T1、T2）的意見。spike（第 7 節）出來之後再更新標「待 spike」的地方。

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

- `agora/` 根資料夾第一次執行時建立，把 **folder ID** 寫進 `~/.config/agora/config.toml`；之後所有存取都用 ID，不靠名字找（Drive 允許同名資料夾）。`sync` 發現同名資料夾時警告。
- **只能透過 agora 寫入。** 從 Drive 網頁拖進去的檔案，`drive.file` 看不到。
- **不要刪除或重建 worker OAuth client。** 換了 client，以前建的檔案全部看不到（資料還在）。萬一發生，復原方法是用一次性的 `drive` scope client 把 `agora/` 複製成新 client 擁有的檔案。
- OAuth consent screen 要是「In production」，否則 refresh token 7 天失效（V4 確認現況）。

## 3. 共通 header

四個實體的每一個項目都帶同一種 header（YAML front matter，第一個 `---` 區塊）。**實體之間只靠 header 互相指，不共用程式或儲存。**

### 3.1 頂層保留欄位（四個實體共通，H3）

| 欄位 | 意思 |
|---|---|
| `header` | header 的版本，目前 `1`（H4）。讀到不認得的版本：警告，只讀共通欄位 |
| `entity` | `mybrain`｜`agora`｜`foundry`｜`atelier` |
| `type` | 實體自己的型態（agora 只有 `session`） |
| `id` | `<entity>:<不變 id>`；agora 用 ULID |
| `title` | 標題 |
| `created_at`／`updated_at` | **這個項目**在該實體裡建立／更新的時間（H7） |
| `refs` | 指向其他項目的 ref 清單 |
| `case` | 所屬案件，是一個 ref 或 `null`（H2） |
| `note` | 自由文字 |
| `tags` | 標籤 |

**其他頂層欄位都是實體專用的**，只有該實體的程式解讀。讀取時保留不認得的欄位，寫回時原樣保留。

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

### 3.4 Agora 的 header

```yaml
---
header: 1
entity: agora
type: session
id: agora:01K6XYZ...
title: "CSV 轉 Markdown 的規劃"
created_at: 2026-10-01T12:00:00Z      # 存進 agora 的時間
updated_at: 2026-10-01T12:30:00Z
refs: ["mybrain:技術/動手做/AiStorage.md"]
case: null
note: "使用者給的自由文字"
tags: []
# ↓ agora 專用
source:
  agent: opencode                     # opencode | claude
  session_id: ses_xxxx                # 來源 agent 自己的 id
  dir: /Users/me/proj                 # 來源 session 所屬的專案目錄（H1）
  host: mbp                           # 產生的機器
  agent_version: 1.18.34
  created_at: 2026-10-01T11:00:00Z    # 來源 session 建立的時間（H7）
relation: import                      # import | continue | merge
parents:                              # 接續或合併自誰，記當時的版本（S7）
  - {id: "agora:01K5...", raw_md5: "…"}
raw: {file: raw-0123456789ab.json, md5: "…", size: 12345}   # S1；merge 沒有 raw
---
```

- header 一律用 `yaml.safe_dump` 產生、`yaml.safe_load` 讀，不用字串拼接。

### 3.5 `--header` 的寫法（H6）

- 一律 `--header key=value`，可以給多次；`refs`、`tags` 給多次就是多筆。
- 可以設定的 key：`title`、`case`、`refs`、`tags`、`note`。
- 不含 `=` 的文字整段當成 `note`（使用者原本的用法 `--header '一些 meta 資訊'` 照樣可以用）。
- search 的 `--header` 只接受這些扁平別名：`agent`（＝`source.agent`）、`relation`、`case`、`tag`、`ref`、`title`；其他 key 報錯並列出可用的 key。

## 4. 儲存

```
Drive: agora/                              （worker client 建立；以 folder ID 存取）
  sessions/<ULID>/
    session.md                header ＋ 閱讀版；「這個版本完成了」的標記
    raw-<md5 前 12 碼>.json   來源 agent 的原始匯出，原封不動
Mac:
  ~/.config/agora/            rclone.conf（worker client）、config.toml（folder ID）
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

- **outbox**：上傳失敗時留在這裡。`sync` 一律**先推再拉**；每個指令開始時，outbox 不是空的就印一行提示。
- **pending**：continue-session 啟動 agent **之前**寫一份 `{agora_id, parent, agent, agent_session_id, dir, started_at, before}`。agent 結束後收尾成功才刪。任何 agora 指令開始時檢查 pending：對應的 agent 已經不在執行，就補做收尾。

### 4.3 sync（S5、L2）

- 列檔一律 `rclone lsjson -R --fast-list --hash`，一次拿到所有檔案的 md5；用 md5 判斷要不要下載（不用時間）。
- 鏡像只抓 `session.md`；raw 在 continue 或 `show --raw` 用到時才抓。
- Drive 上已經不存在的 Session，從鏡像和索引刪掉（outbox、pending 不動）。
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

## 5. 指令

```
agora search session '<關鍵字>' [--header key=value]... [--no-sync]
agora import --format opencode|claude --session-id <id> [--header ...]...
agora merge-session <id1> <id2> [...] [--header ...]...
agora continue-session <id> --agent opencode|claude [--dir <專案目錄>]
agora show <id> [--raw]
agora sync
```

所有指令的輸出第一欄都是 agora id（`agora:<ULID>`），方便接到下一個指令。

### 5.1 search

```
$ agora search session 'CSV'
agora:01K6...  2026-10-01  opencode  …把 CSV 轉成 Markdown 表格…
```

- 日期用 `source.created_at`。
- 同一個來源有多個 agora id 時，顯示最新的那個並警告（S6）。

### 5.2 import

- opencode：`opencode export <id>` 寫到檔案（不接 pipe；stderr 另外導走）。檢查 JSON 能解析、message 數大於 0（S10）。
- claude：`~/.claude/projects/*/<id>.jsonl`，加上附屬資料夾（待 spike V2，S9）；最後一行不完整就丟掉並警告（S10）。
- 只上傳指定的那一個 Session。
- 同一個來源 Session 再匯入：內容沒變就不做事；內容變了而且**還沒有子 Session** 就更新同一個 agora id；**已經有子 Session**（本機索引查得到）就建一個新的 Session，`relation: import`、`parents: [舊 id]`（S7）。

### 5.3 merge-session

- 產生一個新的 Session：`relation: merge`、`parents` 依給的順序，各記當時的 raw md5。
- 閱讀版是各來源的閱讀版依序串接，每段標出來源。
- **沒有 raw**（L3）：merge 出來的 Session 一律用閱讀版注入接續。

### 5.4 continue-session

1. 決定載入方式：

   | 來源 | 目標 | 方式 |
   |---|---|---|
   | 單一 opencode | opencode | **原生**：把 export 裡**所有** id（session、每個 message 與 part 的 id 與它們的 `sessionID`）換成新產生的 id 之後 `opencode import`（S4；細節待 spike V1） |
   | 單一 claude | claude | **原生**：複製 jsonl（與附屬檔）成新 uuid，改寫必要的欄位，`claude --resume`（細節待 spike V2） |
   | 跨 agent，或 merge 出來的 | 任一 | **閱讀版注入**（細節待 spike V3） |

2. 工作目錄：`--dir` 預設用 `source.dir`（這台機器上存在的話），否則用目前目錄；印出實際用的目錄（H1、待 spike V5）。
3. 寫 pending（4.2），再在前景啟動 agent。agora 在 agent 執行期間忽略 SIGINT，Ctrl-C 只給 agent（S3）。
4. agent 結束後：取得那一次的 Session，內容比啟動前多才存（S9）；存成新的 agora Session，`relation: continue`、`parents: [{id: <來源>, raw_md5}]`；刪掉 pending；印出新的 agora id。

### 5.5 Session 壓縮

不自己做，用各 agent 內建的：opencode 的 `/compact`（context 快滿時也會自動壓縮），Claude Code 的 `/compact [要保留什麼]`。壓縮後的結果會在 agent 結束時被存回去。

## 6. 不做的事

- 簽章、收件匣、提交流程、pin repo、隔離、抹除流程（D2）。要刪 Session 就是刪掉 Drive 上那個資料夾，下一次 sync 時鏡像跟著刪。
- 背景同步 daemon。
- Foundry、Atelier 的程式（只保留 header 規則）。
- 跨機器同時改同一個 Session 的衝突處理（最後寫的贏；S7 讓舊版本不會被子 Session 弄丟）。

## 7. 要先驗證的事（spike）

| # | 問題 | 負責 |
|---|---|---|
| V1 | opencode：所有 id 換新之後 import，能接著做，**原本的 session 完全沒變**；只換 session id 會怎樣（S4） | impl1 |
| V2 | Claude Code：複製 jsonl 成新 uuid 之後 `--resume`；附屬資料夾；要改寫哪些欄位；`--session-id` 能否和 `--resume` 一起用；寫到一半的最後一行（S9、S10） | impl2 |
| V3 | 閱讀版注入：兩個 agent 都能在第一則訊息讀檔並接著做 | impl1、impl2 |
| V4 | rclone＋`drive.file`：建立、上傳、`lsjson --hash`、增量下載、同名資料夾、folder ID 存取、token 狀態（S8） | impl1 |
| V5 | 兩個 agent 的 session 與工作目錄的關係，接續時要在哪個目錄開 | impl1、impl2 |

**測試只用自編的 Session 與 Drive 上的 `agora-test/`。** 真實的 Session 與 MyBrain 內容不交給隊員的外部模型處理。

## 8. 實作規模

Python（uv）＋ rclone ＋ SQLite FTS5。**`src/` 的程式碼目標 2,000 行以內，不含測試與 fixture**（L4）。

| 模組 | 內容 | 估計行數 |
|---|---|---|
| `header` | header 讀寫、驗證、ref 解析、`--header` 解析 | 150 |
| `store` | rclone 呼叫、outbox、鏡像、索引（含 NFKC 與二字詞退路） | 400 |
| `agents/opencode` | export、id 改寫後 import、閱讀版、找出結束後的 session | 300 |
| `agents/claude` | 找 jsonl 與附屬檔、改寫欄位、閱讀版、找出結束後的 session | 350 |
| `cli` | 六個指令、continue 流程、pending 補存、merge | 400 |

測試照 `docs/review/test-plan.md`。測試用的接縫：`AGORA_CONFIG`、`AGORA_CACHE_DIR`、`AGORA_STATE_DIR`、`AGORA_RCLONE`、`AGORA_OPENCODE_CMD`、`AGORA_CLAUDE_CMD`、`AGORA_CLAUDE_HOME`、`AGORA_TEST_FAULT`、`AGORA_NOW`。
