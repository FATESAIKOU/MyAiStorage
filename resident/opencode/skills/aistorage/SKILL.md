---
name: aistorage
description: 用 Agora 的工具接續、參考與交出工作。什麼時候要讀別人的 Session、什麼時候要交出或認領工作、什麼時候要產出起點包開新的 session、什麼時候要宣告停止，都看這裡。
---

# Agora 工具怎麼用

這份說明給在住民容器裡的 AI 看。

**先講最重要的三件事**：

1. **回報讀到的內容時，一定要帶上快照時間與新鮮度警告。** 每個
   `agora_find`／`agora_read` 的結果都有 `snapshot_at` 與 `freshness`。
   當 `freshness.satisfied` 是 `false`，或 `freshness.warning` 不為空，
   要在回答裡明說「可能不是最新的」，不要假裝它是最新的。
2. **不要為了讀到更新的內容而要求對方同步。** 需要時，由「寫的一方」自己
   執行同步並提交（ADR 0007）。你這邊讀到的永遠是「對方已提交的版本」。
3. **你不再自己認領交接單。** 認領由 `agora_checkout` 在產出起點包時一併
   登記（AI 自己 claim 這條路已經拿掉了，ADR 0010）。

## 工具分成兩組

- **`agora_*`：Agora 的指令**（`python -m aistorage.agora_cli`）。找、讀、
  交出工作、產出起點包。
- **`aistorage_*`：住民工具的其余部分**（`python -m aistorage.skill`）。
  `whoami`、`stop`、`register_artifact` 與較低階的 `split`／`handoff_end`。

## 我是誰

```
aistorage_whoami()
```

回傳 `{session_id, parent_id, is_main}`。`is_main` 是 `false` 表示你在子 Session
（opencode 的 subagent 會開子 Session）。`stop` **只能在主 Session 做**。

## 找與讀

```
agora_find(query: "5.2 同步器")            # 找 session
agora_find(waiting: true)                   # 列出等人接的交接單
agora_show(session_id: "opencode:ses_…")    # 看前後關係
agora_read(session_id: "opencode:ses_…")    # 讀最新已提交的內容
aistorage_list_handoffs(case_id: "…")       # 同 agora_find 的 waiting
```

`agora_read` **會自動留下一條參考關係**（你讀了它、你參考它）。
`session_id` 由工具帶入你現在的 Session，不用你填。

回報讀到的內容時要引用 `snapshot_at`；有警告就說「可能不是最新的」。
需要更完整的閱讀版（整份 `messages`）時直接讀回傳的內容。

已經讀過、而且這次的成果**有賴於**它時（不只是看了一眼），用
`aistorage_reference(session_id, read_snapshot_at)` 補一條參考。
`read_snapshot_at` **必填**，要填你剛剛 `agora_read` 讀到的 `snapshot_at`——
自己抓對方「目前」的時間會讓這筆參考宣稱讀到了一個其實沒讀過的版本。
它只上傳、不觸發提交，由下一輪提交流程收進去；回報時要說「已留下參考，會在
下一輪收進去」，不要說「已經建好 Link 了」。

並行的 Session 可以互相參考；這不會承接對方的工作，也不會把對方鎖住。

## 交出工作

```
agora_handoff(tasks: [
  {title: "寫 5.2 同步器", summary: "…", next_steps: "先做 core.py"},
  {title: "寫 5.4 skill",   summary: "…", next_steps: "先做 tools.py"},
])
```

它會：同步你自己 → 為每一份工作各寫一張交接單 → 一起提交 → 等到全部可見，
並回報每張交接單的 `handoff_id`。**以回報的 id 為準**，逾時的話它會明確說
還沒被收進去——那時不要宣稱工作已經交出去了。

多個 `tasks` ＝分工（1→n）；只有一個 ＝交出末端。交完末端你就可以停了。

低階的 `aistorage_split`／`aistorage_handoff_end` 也在，但它們是同一件事的
較低階介面（自己帶 `parts`／`summary`）。**優先用 `agora_handoff`**。

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

## 接手工作：checkout

這是**接續別人工作的唯一入口**。它產出一個「起點包」目錄，裡面是起點之前的
原始紀錄（**原封不動**），交給轉接器變成一個帶著前面內容的新 session。

```
agora_checkout(startpoints: ["handoff:01ARZ…"], task: "把 5.2 做完")
agora_checkout(startpoints: ["handoff:01AB…", "handoff:01CD…"], task: "整合兩邊的成果")
agora_checkout(startpoints: ["opencode:ses_abc@msg_007"], task: "從這裡接手")
```

`<起點>` 有兩種：

- `handoff:<id>`：接某張交接單。**會一併登記認領**並等讀取介面確認。
  **被拒就不產出起點包**（目錄不會被建立）——那時要停下來把原因回報給使用者，
  不要重試同一批。
- `<session>[@<訊息>]`：直接從任何 session 的任何位置開始。不指定 `@<訊息>`
  就是「最新已提交的那一則」。

多個起點 ＝ 統合（n→1）。這時**最長的一段會被放在最前面**（這樣開頭的 prompt
cache 命中機會最大）。如果合計太長，工具會**明確拒絕、不產出**——不要自己去
截斷別人交出來的工作。

回傳的 `new_session_id` 就是新 session 的 id，接下來由呼叫者決定要不要開：

```
agora-opencode load <起點包目錄>      # 印出新 session id
opencode --session <新 session id>
```

`agora_checkout` **只產出起點包**，不會替你開 session。要不要開、開幾個，
由你（或呼叫者）決定。

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
```

- `produced_by_session_id` **不用填**：由工具帶入你現在的 Session。
- `contained` 的本體是**容器內的本機檔**（`file_path`），單檔上限 100 MiB；
  超過會直接被拒收（訊息會說原因）。不要為了繞過上限而切檔或壓縮。
- 只上傳，**不觸發提交**：由下一輪提交流程收進去。回報時說「已登錄，會在下一輪
  收進去」，不要說「目錄裡已經有了」。

## 不要做的事

- 不要用 bash 直接跑 `python -m aistorage.agora_cli …` 來取代工具（那會繞過
  plugin 的主 Session 檢查，而且不保證帶到 context 的 Session id）。要用手動
  跑的話，`aistorage_*` 的 `--session` 一定要填**你自己現在的** Session id。
- 不要在還沒被提交流程收進去的時候就宣稱工作已經「交出去」了——以
  `agora_handoff` 回報的 `handoff_ids` 為準。
- 不要為了讓別人讀到你的進度而手動觸發提交流程；同步器 daemon 會照排程收斂。
  只有**交出工作**與**接手（checkout）**這兩種要主動同步並提交。
- 不要用 `agora_checkout` 去接一個沒有被交接單指名的位置然後假裝那是有交接的
  ——沒有交接單就不會有接續 Link，那個新 session 是孤兒。
