# Agora 的 session 操作：從場景推出的指令

> 2026-09-28 本人確認（v2，含轉接器命名 `agora-<coding agent 名稱>`）。取代 ADR 0010 裡暫定的 `agora init session`。ADR 0010 的原則不變：新 session 帶著前面的內容開始、開頭原樣保留以命中 KV cache、AI 不再自己 claim。
>
> 2026-09-29 追加（impl2 M6）：**接續不需要交接單**。Session 之間只有四種關係
> （1→1、1→n、n→1、n↔m），**每一次 `checkout` 都記錄接續 Link**。交接單只是可選的
> 便利（持有者事先寫好任務，而且只能被接一次）。直接起點走新的**接續單**項目。

## 誰會用

- **你**：在終端機裡開新的 AI agent。
- **正在工作的 AI**：工作中要分工、交出成果、查別的 session，或自己開新的 agent。

能不能開 agent、能開幾個，由那台機器決定（例如 MyLinuxPool 的一個 worker 就是一個團隊，裡面可以自由開）。這不歸 Agora 管，所以 AI 與你用的是同一組指令。

## 場景

| # | 場景 | 心裡想的 | 關係 |
|---|---|---|---|
| A | 接著做 | 「開一個新 agent，接著 S1 做下去」 | 1→1 |
| B | 分工 | 「S1 規劃好了，拆成 n 份，各開一個 agent 做」 | 1→n |
| C | 匯總 | 「S2、S3 做完了，開一個 agent 把兩邊的成果整合起來」 | n→1 |
| D | 互相參照 | 「S2、S3 同時在做，要能看對方做到哪」 | n↔m |
| E | 找 | 「上週那個 session 在哪？有哪些工作在等人接？」 | — |

**只有這四種關係，而且一律留下記錄。** 前三種是接續 Link（由 `agora checkout`
寫下），第四種是參考 Link（由 `agora read` 寫下）。分岔、收斂、統合、相互參照
都是這四種的組合，不是新的關係。

**接續不需要交接單。** 交接單只是可選的便利：持有者事先把任務寫好，而且只能被
接一次。所以 A 場景（1→1）完全可以 `agora checkout S1` 就開工，不必先寫交接單。

## 基本概念

- **起點**：一個 session 的某個位置（快照＋那一則訊息）。不指定位置，就是最新已提交的那一則。
- **交接單**：持有者寫下的「起點＋要交代的任務」。只能被接一次，接之前會出現在「等人接的工作」清單裡。
- **接續單**：沒有交接單時，`checkout` 為那個起點記的「由我接手」。和交接單一樣帶著新 session 的空紀錄（預留），被拒就不產出起點包。
- **起點包**：`agora checkout` 產出的一個目錄，內含起點之前的**原始紀錄（原封不動）**、要交代的任務，以及來源 session 的 id。它不屬於任何一個 coding agent。
- **轉接器**：每個 coding agent 一個，命名為 `agora-<coding agent 名稱>`（`agora-opencode`、之後的 `agora-claude-code`…），和同步器放在一起。它負責把起點包載入成那個 agent 的原生 session。

Agora 本身不依賴任何 coding agent：它只管讀、寫交接單、產出起點包。載入與開 agent 是轉接器與呼叫者的事。

**實作**（期 1，2026-09-28）：

| 指令 | 程式 |
|---|---|
| `agora find / show / read / handoff / checkout` | `src/aistorage/agora_cli/`（單一入口，`pyproject.toml` 的 console script `agora`） |
| `agora-opencode load` | `src/aistorage/adapters/opencode/`（console script `agora-opencode`） |

起點包格式：`schemas/context-package.schema.json`，取捨說明見
`schemas/context-package.md`。

**原始紀錄的讀法**：讀取視圖**只發佈閱讀版**（閱讀版會把工具呼叫的輸入輸出壓成
摘要，還原不了位元組相同的開頭），而它**不發佈 raw 的位元組**——那等於把真本的
位元組複製一份到衍生物裡。所以 `checkout` 走「位址 → 位元組」：讀取介面給該快照的
**annex key**（`snapshots` 表的 `annex_key`，內容定址所以 key 本身就是位址）→
唯讀身分（對 Agora 真本前綴有唯讀分享）自己去取回 → **用 key 內嵌的 sha256 與
size 驗證**，對不上就明確拒絕、不產出起點包。唯讀就夠，不需要寫入權限。

## 指令

```
agora find [關鍵字] [--case <案件>] [--waiting]      # E：找 session；--waiting 列出等人接的交接單
agora show <session>                                  # E：看內容與前後關係
agora read <session>                                  # D：讀最新已提交的內容，並記下一條參考關係
agora handoff <session> [--at <訊息>] (--tasks-file <json> | --task "…" [--task "…" …])
                                                      # B、C：每個工作一張交接單
agora checkout <起點>… [--task "…"] [-–resume] -o <目錄>  # A、B、C：產出起點包
```

`<起點>` 可以是 `handoff:<id>`（接某張交接單），也可以是 `<session>[@<訊息>]`（直接從任何 session 的任何位置開始）。

`agora handoff` 的工作清單有兩種來源：`--tasks-file` 是一個 JSON 陣列，**每個物件一張交接單**（plugin 走這條，把模型給的 `[{title, summary, next_steps}]` 原樣寫進去）；`--task` 是**每個字串一張**。兩者可以混著給。

`agora checkout` 做的事：
1. 從讀取介面取得起點與被釘住的快照，把原始紀錄原封不動放進起點包；
2. **先把所有本機檢查做完**——輸出目錄可寫且為空、`raw/` 位元組與釘住快照的 sha256 相符、Agora 物件讀得到；
3. **為每一個起點記錄接續**（見下），等讀取介面確認之後才產出起點包；
4. 認領／接續通過才寫 `package.json` 並把暫存目錄改名成起點包；
5. 多個起點（n→1）時，最長的一段放最前面，並檢查總長度沒有超過目標模型的上限，超過就明確拒絕。

### 每一次 checkout 都記錄接續

| 起點 | 放進收件匣的項目 | 提交流程做的事 |
|---|---|---|
| `handoff:<id>` | **認領**（claim）——認領本身就代表接續，不再另外記一筆 | 認領那張交接單 ＋ 建接續 Link |
| `<session>[@<訊息>]` | **接續單**（continuation） | 建接續 Link（新 session → 被接續的 session） |

所以四種關係都留下記錄：

| 關係 | 怎麼做 | Agora 裡留下什麼 |
|---|---|---|
| 1→1 | `agora checkout S1` | 一條接續 Link，指向 S1，接續點就是那個位置 |
| 1→n | 同一個起點 checkout n 次 | n 條接續 Link（每個新 session 各一條） |
| n→1 | `agora checkout S2 S3` | 一個新 session 兩條接續 Link |
| n↔m | 工作中的 AI 用 `agora read S3` | 參考 Link（`agora checkout` 不參與） |

兩種項目都**自帶新 session 的空紀錄**（預留），所以被拒時 Agora 裡連預留都不會
有——沒有「已記錄接續卻沒有人開工」的新 session。

**接續點**：接續 Link 記著它所依據的**那份快照**（不是「最新」）與該快照裡的
那一則訊息。那份快照因此被釘住，發佈階段一定會發出來。驗證規則兩邊不同：

- **交接單**的接續點必須是該快照**最後一則已完成**的訊息（交接單是「交出手上做到
  哪」）；已經寫好的交接單被接時，接續點是照抄的，不再重驗。
- **直接起點**可以停在快照裡的**任何**已完成、未撤銷的訊息（`checkout <s>@<訊息>`）。

**冪等與去重**：同一個新 session 對**同一個被接續 session** 只保留一條 Link。
同一個起點重送（換 `item_key`、`--resume`）是冪等；同一個來源的第二個起點——不管
是直接起點還是同一個 session 的第二張交接單——會被明確拒收（`duplicate_link`）。
所以「哪一條留下來」不取決於套用順序。`agora checkout` 更早一步就擋掉：同一批起點裡
同一個被接續 session 出現兩次（`handoff:H` 與 `S1@某訊息` 混用也算）→ 直接拒絕，
連接續記錄都不送。1→n 是**各跑一次** checkout，n→1 是接**不同**的來源。

**接續目標只能是主 Session**（與交接單一致）：子 session 是母 session 內部的一段
工作，要接就接那個母 session。

**別人可以接任何人的任何快照**（2026-09-30 裁決）：任何 profile 都能為任何一份既有
快照送接續，而那份快照因此被釘住、發佈出閱讀版。這沒有擴大讀取邊界（讀取身分本來
就能讀整個 Agora 前綴，見 ADR 0010 與 2026-09-28 的決定），所以接受；**用成本封頂**
而不是用權限封：每個 profile 每輪能建立的預留／接續數量有上限（可設定，預設 20，
`max_links_per_profile_per_round`），超過就明確拒收並發佈原因。冪等重送不佔額度。
「一輪」是提交流程的一輪（一個 `AgoraStore`），所以額度每輪歸零、不會把某個 profile
永久鎖死；這個計數刻意不寫進真本（寫入端的成本限制不是內容，見 decision log）。

### 認領卡住怎麼重跑

交接單只能被認領一次，換一個 id 重來只會得到 `already_claimed`。直接起點沒有這個
問題，但重跑會多留一筆沒有人開工的預留 session（提交流程只會留一條 Link，那筆多
出來的預留就成了沒有人接手的空 session）。所以 `checkout` 在把項目送出**之前**就
把這次用的項目 id 與預留 id 寫進本機記錄（`~/.aistorage/checkout-claims/`，**一個
起點一個檔案**）。逾時或中斷之後重跑會**自動沿用**記錄裡那一組 id——不需要任何
額外參數（`--resume` 只是把它寫成明示）。

被**明確拒收**時只刪被拒那幾筆的記錄，可以乾淨地從頭來。n→1 時可能只有一筆被拒而
其餘被接受：此時已經被接受的那幾筆**留著記錄**（它們的起點已被預留的 session 接走，
記錄是那個預留日後唯一的線索），並以「部分被接受」明確報出哪幾筆被接走、預留的
session 是哪個。記錄檔壞掉時會**出聲**而不是當成空的——當成空的會讓下一次寫入覆蓋掉
其他起點的記錄。

**轉接器（以 opencode 為例）：**

```
agora-opencode load <起點包>        # 截斷、重編 id、opencode import → 印出新 session id
opencode --session <新 session id>      # 誰要開、在哪開，由呼叫者決定
```

同一個 agent 之間接續時，轉接器直接使用起點包裡的原始紀錄，新 session 送給模型的開頭會與原 session 位元組相同（技術驗證 `docs/spike/session-import.md`）。跨 agent（例如 opencode → Claude Code）時，改走共通閱讀版轉換，開頭會改寫，並在 metadata 標記。

**轉接器不換 id**：`agora-opencode load --session-id` 只接受**等於**起點包預留的
那個 id，否則明確拒絕。`checkout` 已經把接續 Link 與那筆預留記進 Agora，那個 id
是提交流程認得出預留的唯一線索；換一個 id 匯入進去，Agora 裡就多一筆沒有人負責的
空 session，而真正開工的那個 session 沒有接續 Link。要指定別的 id，請在
`checkout` 時就用 `--new-session-id` 指定，讓它預留你要的那一個。

| 場景 | 怎麼下 |
|---|---|
| A 接著做 | `agora checkout S1 -o p/` → `agora-opencode load p/` |
| B 分工 | `agora handoff S1 --task "做前端" --task "做後端"` → 每張各跑一次 `checkout handoff:Hx` 加 `load`；不想寫交接單也可以直接對同一個起點各 checkout 一次 |
| C 匯總 | `agora checkout handoff:H2 handoff:H3 --task "整合兩邊的成果" -o p/` → `load p/` |
| D 互相參照 | 工作中的 AI 用 `agora read S3` |

AI 在 session 裡用的是同一組指令，透過 skill 包成工具：`agora_find`、`agora_show`、
`agora_read`、`agora_handoff`、`agora_checkout`（`resident/opencode/plugin/aistorage.ts`
把它們轉呼叫 `agora` CLI）。**沒有認領工具**——認領（交接單起點）或接續記錄（直接
起點）都由 `agora checkout` 在產出起點包時一併登記，被拒就不產出。

轉接器拿到的 `new_session.session_id` 是**預留**的：它在 Agora 裡已經是一個零則訊息的
空 session（還帶著指向被接續 session 的接續 Link），`agora-opencode load` 匯入之後
第一則真訊息會接在它後面。**所以 `checkout` 需要可用的寫入身分**：沒有簽章金鑰就
連起點包都產不出來——沒有接續 Link 的新 session 沒有人負責。

**預留 id 的格式規則**（2026-09-30）：預留只能佔用**呼叫端自己產生的** session id，
格式是 `agora checkout` 產生的那個形狀（`<source>:ses_` ＋ ULid 後 16 碼），
或呼叫端在 checkout 時用 `--new-session-id` 指定、而**那個 agent 真的會用**的那個
id（轉接器沿用它匯入）。預留不會去猜別人的 id：opencode 的 id 帶 ULID 尾巴，猜不到，
所以實務上沒有佔用問題；但**這是規則不是保障**——一個 profile 若預留了某個它不會
真正建立的 id，那個 id 之後被真正的持有者第一次上傳時會被判定為 collision，而且
永遠如此（那個預留已經寫進真本、提交流程不會自動刪除）。所以：**一次 checkout 沒
被接下來就重跑時要沿用同一組預留**（`--resume`／本機記錄自動沿用），不要清記錄
重來，也不要用 `--new-session-id` 換一個。

**預留不是「運作中」**：預留出來的 session 狀態是 `reserved`，並帶一個期限
（預設 7 天）。有人真的載入它（第一份真實快照）之後，它回到一般的 `running`／
`stopped`，期限欄消失。讀取介面把兩者分開：`agora find --status reserved` 只列出
預留，`agora show` 會顯示期限，過期就標 `expired`。**期 1 不自動刪除過期的預留**——
刪除真本裡的項目是管理操作（`admin erase`／rollback），要人決定；期限只是顯示與
管理用的訊號。
