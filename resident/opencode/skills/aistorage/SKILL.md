---
name: aistorage
description: 用 AiStorage 的工具接續、參考與交出工作（Agora）。什麼時候要讀別人的 Session、什麼時候要交出或認領工作、什麼時候要宣告停止，都看這裡。
---

# AiStorage 工具怎麼用

這份說明給在住民容器裡的 AI 看。工具名稱都是 `aistorage_` 開頭。

**先講最重要的兩件事**：

1. **回報讀到的內容時，一定要帶上快照時間與新鮮度警告。** 每個
   `aistorage_find`／`aistorage_read` 的結果都有 `snapshot_at` 與 `freshness`。
   當 `freshness.satisfied` 是 `false`，或 `freshness.warning` 不為空，
   要在回答裡明說「可能不是最新的」，不要假裝它是最新的。
2. **不要為了讀到更新的內容而要求對方同步。** 需要時，由「寫的一方」自己
   執行同步並提交（ADR 0007）。你這邊讀到的永遠是「對方已提交的版本」。

## 我是誰

```
aistorage_whoami()
```

回傳 `{session_id, parent_id, is_main}`。`is_main` 是 `false` 表示你在子 Session
（opencode 的 subagent 會開子 Session）。`claim` 與 `stop` **只能在主 Session 做**。

## 交出工作

### 分裂（1→n）：把一件工作切給幾個 Session

```
aistorage_split(parts: [
  {title: "寫 5.2 同步器", summary: "…", next_steps: "先做 core.py"},
  {title: "寫 5.4 skill",   summary: "…", next_steps: "先做 tools.py"},
])
```

它會：同步你自己 → 為每一份工作各寫一張交接單 → 一起提交 → 等到全部可見。
**一定要用這個工具，不要手寫項目檔**（簽章、快照雜湊、接續點都要對）。

### 交出末端（只寫一張）

```
aistorage_handoff_end(summary: "…", next_steps: "…")
```

分岔的末端要交給別人接著做時用這個。交完之後你就可以停了。

### 宣告停止

```
aistorage_stop()
```

設定 `time.archived = now` 然後同步並提交。只在主 Session 可用。

呼叫的時候你這次的回覆**正在生成中**，這是正常的：停止的判定看的是「封存之後
還有沒有**新建立**的訊息」，而你這一則是在封存之前就開始寫的，所以不會被算成
新訊息。回覆寫完之後，同步器仍然會把它當成停止中。

之後如果你又開新的對話（訊息在封存之後建立），它會自動恢復成運作中，而且只會
觸發**一次**立刻的同步並提交。

## 接手工作

### 認領交接單（主 Session 限定）

```
aistorage_list_handoffs()                  # 先看有什麼可認領
aistorage_claim(handoff_ids: ["handoff:…"])  # 多張＝統合
```

它會：同步你自己 → 上傳所有認領 → 一起提交 → **等 Link 屬於自己之後**才回傳
交接單內容與接續點之前的閱讀版。

**只要有任何一張被拒收，就停下。** 工具的錯誤訊息會告訴你被拒收的原因
（例如 `stale`：你讀到的快照已經過期，要重新 `aistorage_read` 再來）。
被拒收時不要重試同一批，把原因回報給使用者。

## 參考別人的 Session

```
aistorage_find(query: "5.2 同步器")
aistorage_read(session_id: "opencode:ses_…", max_lag: "5m")
aistorage_reference(session_id: "opencode:ses_…", read_snapshot_at: "…")
```

流程是「先讀，再留下參考」：

1. `aistorage_read` 讀對方，記下回傳的 `snapshot_at`。
2. 回答使用者時引用那個快照時間；有警告就說「可能不是最新的」。
3. 真的用到對方的內容、而且這次的成果有賴於它時，用 `aistorage_reference`
   留下參考 Link，**一定要把你剛讀到的 `snapshot_at` 當 `read_snapshot_at`
   帶進去**（它是必填的）。

   為什麼必填：`read_snapshot_at` 記的是「我讀到的時候它的那個版本」。
   如果留空、由工具自己去抓對方「目前」的時間，等於宣稱你讀到了一個其實
   沒讀過的版本——那個時間之後別人又改過，你並沒有看過那段。

`aistorage_reference` **只上傳，不觸發提交**（PM 決定 4）：由下一輪提交流程收進去。
所以回報的時候要說「已留下參考，會在下一輪收進去」，不要說「已經建好 Link 了」。

平行中的 Session 可以互相參考；這不會承接對方的工作，也不會把對方鎖住。

## 登錄產出（Foundry）

做出一份要能被人與其他 AI 找回來的東西（文件、程式碼、圖片、簡報）時，
用這個工具把它登錄到 Foundry 產出目錄：

```
# 收容產出：真本沒有自己的家，本體放進 Foundry
aistorage_register_artifact(kind: "contained", name: "architecture-summary.pdf",
    content_type: "application/pdf", file_path: "/work/report.pdf")

# 原處產出：真本留著自己的專案 repo，Foundry 只登錄出處
aistorage_register_artifact(kind: "link", name: "PR #42 的報告",
    link: "https://github.com/owner/repo/pull/42")
aistorage_register_artifact(kind: "link", name: "模組設計文件",
    link: "https://github.com/owner/repo/blob/main/docs/design.md",
    repo: "owner/repo", path: "docs/design.md")
```

- `produced_by_session_id` **不用填**：由工具帶入你現在的 Session。
- `contained` 的本體是**容器內的本機檔案**（`file_path`），單檔上限 100 MiB；
  超過會直接被拒收（訊息會說原因）。不要為了繞過上限而切檔或壓縮。
- 產出要是你自己做出來的、或真的在你手上的東西；不確定時先用 `aistorage_find`
  看看是不是已經有人登錄過。
- 只上傳，**不觸發提交**：跟 `aistorage_reference` 一樣，由下一輪提交流程收進去。
  回報時說「已登錄，會在下一輪收進去」，不要說「目錄裡已經有了」。

## 不要做的事

- 不要用 bash 直接跑 `python -m aistorage.skill …` 來取代工具（那會繞過 plugin
  的主 Session 檢查，而且不保證帶到 context 的 Session id）。要用手動跑的話，
  `--session` 一定要填**你自己現在的** Session id。
- 不要在還沒被提交流程收進去的時候就宣稱工作已經「交出去」了——以工具回傳的
  `handoff_ids` 為準，逾時的話它會明確說還沒被收進去。
- 不要為了讓別人讀到你的進度而手動觸發提交流程；同步器 daemon 會照排程收斂。
  只有「接續、認領」這兩種要主動同步並提交。
