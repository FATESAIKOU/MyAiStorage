## Context

`cmd_continue`（`src/agora/cli.py`）：取原始 raw（merge 出來的是各節要約＋`MERGE_NOTE`／`MERGE_READY`），跨 agent 時轉換，`agent.start_native()` 載入，寫 `pending/<ULID>.json` 並持有 flock，前景跑 agent，結束後 `_finish()` 把對話寫回 `record["agora_id"]`。接續途中那個 Session 被刪掉時，`_finish` 另存成新的（`relation: continue`、`parents` 指回去）。

## Goals / Non-Goals

**Goals：** 從任何 Session（普通、merge 出來的、fork 出來的）分岔出一個新 Session 繼續；原 Session 完全不動。

**Non-Goals：** 接續期間定期寫回（#28）；agy（#26）；新增本人看得到的 header 欄位。

## Decisions

### 新 Session 在啟動 agent 之前就建好並存檔
- 順序：
  1. 照現在的做法組出要載入的 raw（同 agent 用原 raw；跨 agent 轉換；merge 用要約）；
  2. 在最後加上 fork 的兩則；
  3. `start_native()`；
  4. 用這個 raw 與啟動的 agent session 建新 Session 的 header 並 `_save(update=False)`；
  5. pending 記錄的 `agora_id` 寫新 Session 的 id；
  6. 跑 agent；
  7. `_finish()` 寫回新 Session。
- 理由：
  - 工單要的是「先複製出一個新 session」；
  - #28 的定期寫回需要寫回的目標一開始就存在；
  - 沒有新內容時（打開就離開），fork 仍然留著（含 fork 說明），這是 fork 的本意。
- 新 Session 的 `agora.source` **一定是這次啟動的 agent session**，不能照抄原 Session 的：`search` 會把來源相同的列當成重複、只列一筆（T7 F2）。
- 存檔走 local-first（`_save` → 本機鏡像＋outbox，背景上傳），和 import 一樣。

### header：照抄使用者欄位，系統欄位重新產生
- 照抄：`h.SYSTEM_KEYS`（`id`、`agora`）以外的所有欄位，`title` 後面加 `-fork`（原本沒有 title 時用 agent 給的標題再加 `-fork`）。
- 重新產生：
  - `id`：新的 ULID；
  - `agora`：`relation: fork`，`parents: [{id: 原 id, raw_md5: 這次實際取來的原始 raw 的 md5（merge 時為 None）}]`，`source` 照上一節，`raw` 照 `_save`，時間戳記；
  - `generated`、`sources`：照 `_finish` 的寫法，換成這次的 agent。
- `verified`（本人審過的標記）照不照抄，**等本人決定**（已經透過 PMO 詢問）。PM 的建議是不抄：fork 之後內容會變，本人審過的是原本那一份。實作先寫成一個常數清單（`FORK_DROP_KEYS`），決定之後只改這裡。
- `relation: fork` 對其他地方的影響（實作時逐一確認並補測試）：
  - `search` 第三欄印 `source.agent`，有 source 就不會印 relation；
  - `delete`：有子 Session（`parents` 指向它）時拒絕刪除，所以原 Session 在 fork 還在時刪不掉。這和 merge／救回一樣，照舊；
  - `_rescued_pair`（T7 F2）：fork 與原 Session 的 source 不同，不會被比對到；
  - 對 fork 再普通 continue：照現在的寫回，`relation` 保持 `fork`；
  - 對 fork 再 merge：照 merge 的做法，`relation` 不是 merge，所以取原始 raw。

### fork 的說明：一則 user、一則固定的 assistant
- user：`FORK_NOTE = "從 {id}（{header}）fork"`。`{header}` 是原 Session 的使用者欄位（`SYSTEM_KEYS` 以外）用一行 YAML flow 表示，例如 `title: …, case: …, tags: […]`。這行**不能**含 `agora` 區塊。
- assistant：`FORK_READY = "（知道了：這是從上面那個 Session 分出來的新 Session，原本那個不會被改動。等你的指示。）"`。
- 和 `MERGE_NOTE`／`MERGE_READY` 一樣，交給 `native()` 的一定是 user／assistant 交替，`before_count` 因此把這兩則算進去：打開就離開時，`_finish` 不會再存一次（fork 已經在步驟 4 存好了）。
- 同 agent 時原 raw 不經轉換，所以兩則要用目標 agent 的格式接在原 raw 後面。轉接器若沒有「在原生 raw 後面接幾輪」的方法，就補一個（例如 `agent.append_native(raw, turns)`）；不要為了接這兩則把整段 raw 轉成共通格式再轉回去（會丟掉工具呼叫與前綴，快取也不會命中，design 5.4 第 2 點）。

### pending 與寫回
- `record["agora_id"]` 是新 Session；`record["parent"]` 是 `{id: 新 Session 的 id, raw_md5: 新 Session 存檔時的 raw md5}`。這樣 `_finish` 的「途中被刪掉就另存」照樣適用於 fork。
- 原 Session 從頭到尾不寫、不改 `updated_at`。

## Risks / Trade-offs

- [打開就離開也會留下一個 fork] → 這是 fork 的本意；不要可以 `agora delete`。
- [原 Session 在 fork 還在時刪不掉] → 和 merge 一樣；要刪就先刪 fork，或一次一起刪（delete 會先刪子 Session）。
- [`verified` 照不照抄] → 等本人決定，實作只改一個常數。
