# Agora lite 基本設計

2026-10-01。取代期 1 的實作（約 3.2 萬行程式碼，大多在防「AI 住民篡改真本」與「把 git 放在 Drive 上」）。舊實作留在 git 歷史與 main，這個 branch（`agora-lite`）從零開始。

## 1. 目的

讓 coding agent 的 Session **可以被找到、被合併、被接著做**，而且不綁在哪一個 agent、哪一台機器上。

- **找**：用關鍵字或 header 找到過去的 Session。
- **合**：把幾個 Session 的成果合成一個，交給下一個 agent。
- **接**：用 opencode 或 Claude Code 從某個 Session 接著做；agent 結束時，結果自動存回來。

這是使用者自己每天的 workflow 工具（技術取捨準則：能影響個人 workflow 才進 Feature），所以以「指令少、等待短、好理解」為第一優先。

## 2. 前提與決定（2026-10-01 使用者確認）

| # | 決定 | 理由／影響 |
|---|---|---|
| D1 | **儲存在 Google Drive 資料夾**，放一般檔案（不用 git-annex） | 已付費的 Drive；用 rclone 存取 |
| D2 | **信任自己的機器** | 不做簽章、收件匣、單一提交者、pin repo。寫入是本機直接寫，不用等 GitHub Actions |
| D3 | **四個實體的概念全部保留**（MyBrain／Agora／Foundry／Atelier），彼此靠 **header** 參照 | header 仿 MyBrain 的 frontmatter；程式這次只實作 Agora |
| D4 | **continue 的結果是新的 Session**，header 記下來源 | 同一個來源可以分岔很多次；merge 的結果也有地方放 |
| D5 | Drive 憑證只用 **worker OAuth client（`drive.file`）** | 只看得到自己建的檔案，帳號裡其他東西（照片等）碰不到 |

## 3. 共通 header

四個實體的每一個項目都帶同一種 header（YAML front matter）。**實體之間只靠 header 互相指，不共用程式或儲存。**

```yaml
---
entity: agora                 # mybrain | agora | foundry | atelier
type: session                 # 實體自己的型態（agora 只有 session）
id: agora:01K6XYZ...          # <實體>:<不變 id>；agora 用 ULID
title: "CSV 轉 Markdown 的規劃"
created_at: 2026-10-01T12:00:00Z
updated_at: 2026-10-01T12:30:00Z
source:                       # 從哪裡來（agora 專用）
  agent: opencode             # opencode | claude
  session_id: ses_xxxx        # 來源 agent 自己的 id
relation: import              # import | continue | merge
parents: []                   # 接續或合併自哪些 agora id（依序）
refs:                         # 指向其他實體（不透明字串，解讀交給該實體）
  - mybrain:技術/動手做/AiStorage.md
  - foundry:<id>
  - atelier:<職務>@<版本>
case: null                    # 所屬案件（MyBrain 案件 id，之後 MyBrain 加不變 id 再用）
note: "使用者在 --header 給的自由文字"
tags: []
---
```

- **`id` 永遠帶實體前綴**，所以任何一個 `refs` 都看得出指向哪個實體。
- MyBrain 已經有自己的 frontmatter（OKF），它那邊不必改；Agora 指向它時用 `mybrain:<路徑>`，等 MyBrain 有不變 id 之後改用 id。
- Foundry、Atelier 之後實作時沿用同一份 header（`entity`、`id`、`refs`、`case` 這幾個欄位是共通的）。

`--header` 的寫法：
- `--header 'key: value'` 會寫進對應欄位（`title`、`case`、`refs`、`tags`）；
- 其他文字一律放進 `note`；
- 可以給多次。

## 4. 儲存

```
Drive: agora/                         （worker client 建立，drive.file）
  sessions/<ULID>/
    session.md      header ＋ 閱讀版（給人看、給搜尋用）
    raw.json        來源 agent 的原始匯出（opencode export／Claude jsonl 包成 JSON），原封不動
Mac: ~/.cache/agora/                  Drive 的本機鏡像 ＋ index.sqlite（FTS5）
Mac: ~/.config/agora/                 rclone.conf（worker client）、config.toml
```

- **每個 Session 一個資料夾、id 是 ULID**：新增永遠是新路徑，不會有兩台機器搶同一個檔案。同一個 Session 被重新匯入時才會覆寫自己的檔案，最後寫的贏（D2 接受這個風險）。
- **沒有共用的索引檔放在 Drive 上**。搜尋索引是本機從鏡像重建的快取，壞了刪掉重建就好。
- `raw.json` 原封不動，所以同一個 agent 接續時可以原生載入（保留工具呼叫，前綴相同，快取能命中）。

## 5. 指令

```
agora search session '<關鍵字>' [--header key=value]...   # 找
agora import --format opencode|claude --session-id <id> [--header '...']...   # 初次引入
agora merge-session <id1> <id2> [...] [--header '...']    # 合併成一個新 Session
agora continue-session <id> --agent opencode|claude [--dir <專案目錄>]   # 接著做，結束時存回
agora show <id>                                           # 看 header 與閱讀版（驗證用）
agora sync                                                # 把 Drive 拉到本機鏡像並重建索引
```

所有指令的輸出第一欄都是 agora id（`agora:<ULID>`），方便接到下一個指令。

### 5.1 search

```
$ agora search session 'CSV'
agora:01K6...  2026-10-01  opencode  …把 CSV 轉成 Markdown 表格…
agora:01K7...  2026-10-02  claude    …讀取 CSV 的部分…
```

- 先做一次增量 `sync`（只下載 Drive 上比鏡像新的檔案），再查本機 FTS5。
- 關鍵字比對閱讀版與 header；`--header key=value` 只比 header 欄位（例如 `--header case=xyz`、`--header agent=claude`）。
- 中日文：FTS5 trigram 分詞（期 1 驗證過可行）。

### 5.2 import

```
$ agora import --format opencode --session-id ses_xxxx --header 'title: 規劃' --header '先做讀取'
agora:01K6...
```

- opencode：`opencode export <id>`（寫到檔案，不接 pipe——pipe 會截斷）。
- claude：在 `~/.claude/projects/*/<id>.jsonl` 找到那一份。
- 只上傳指定的那一個 Session。同一個來源 Session 再匯入一次，對到同一個 agora id、更新內容（以 `source.agent`＋`source.session_id` 對應）。

### 5.3 merge-session

```
$ agora merge-session agora:01K6... agora:01K7...
agora:01K8...
```

- 產生一個新的 Session：`relation: merge`、`parents` 依給的順序。
- 閱讀版是各來源依序串接，每段前面標出來自哪個 Session。
- `raw.json` 記錄各來源的 raw（原封不動），接續時才決定怎麼載入。

### 5.4 continue-session

```
$ agora continue-session agora:01K6... --agent opencode
（進入 opencode；結束後）
agora:01K9...
```

1. 依來源與目標 agent 決定載入方式：

   | 來源 | 目標 | 載入方式 |
   |---|---|---|
   | 單一 opencode | opencode | **原生**：`opencode import` 成新 session（新 id），`opencode --session <id>` 開啟 |
   | 單一 claude | claude | **原生**：複製 jsonl 成新 id，`claude --resume <id>` 開啟 |
   | 跨 agent，或 merge 出來的 | 任一 | **閱讀版注入**：把閱讀版寫成檔案，用初始訊息請 agent 先讀它 |

2. agent 在前景執行，使用者照常操作（包括內建的壓縮指令，見 5.5）。
3. agent 結束後，取得那一次的 Session（opencode 用新 id export；claude 啟動時用 `--session-id` 指定 id），存成新的 agora Session：`relation: continue`、`parents: [<來源>]`，印出新的 agora id。

### 5.5 Session 壓縮

不自己做，用各 agent 內建的：

| agent | 指令 |
|---|---|
| opencode | `/compact`（也會在 context 快滿時自動壓縮） |
| Claude Code | `/compact [要保留什麼的提示]` |

壓縮後的結果會在 agent 結束時被存回去，所以 Agora 裡的接續版本就是壓縮過的。

## 6. 不做的事

- 簽章、收件匣、提交流程、pin repo、隔離、抹除流程（D2）。要刪 Session 就是刪掉 Drive 上那個資料夾。
- 背景同步 daemon：只有 `import` 與 `continue-session` 結束時寫入，使用者明確指定才上傳。
- Foundry、Atelier 的程式（只保留 header 規則）。
- 跨機器同時改同一個 Session 的衝突處理。

## 7. 要先驗證的事（spike）

| # | 問題 | 怎麼驗 |
|---|---|---|
| V1 | `opencode import` 一份 export、改成新 id 後能不能用 `opencode --session` 繼續 | 用自編的測試 session |
| V2 | Claude Code：複製 jsonl 成新 id 後 `claude --resume` 能不能接；`--session-id` 能不能和 `--resume`／`--fork-session` 一起用、結束後的 jsonl 在哪 | 用一段自編的短對話（`claude -p`） |
| V3 | 閱讀版注入：兩個 agent 都能在第一則訊息讀檔案並接著做 | 自編內容 |
| V4 | rclone 用 `drive.file` client 建 `agora/`、上傳、列出、增量下載 | Drive 上的測試資料夾 `agora-test/` |
| V5 | 兩個 agent 的「工作目錄」問題：opencode 的 session 屬於專案目錄、Claude 的屬於 `~/.claude/projects/<路徑>`，接續時要放在哪個目錄 | 同上 |

**測試只用自編的 Session 與 Drive 上的 `agora-test/`。** 真實的 Session 與 MyBrain 內容不交給隊員的外部模型處理。

## 8. 實作規模的目標

Python（uv）＋ rclone ＋ SQLite FTS5，**程式碼目標 2,000 行以內**，模組：

| 模組 | 內容 |
|---|---|
| `header` | header 的讀寫與驗證 |
| `store` | Drive（rclone）與本機鏡像、索引 |
| `agents/opencode`、`agents/claude` | 匯出、原生載入、閱讀版轉換、找出結束後的 session |
| `cli` | 六個指令 |
