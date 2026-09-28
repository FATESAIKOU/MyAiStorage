# 起點包（`agora checkout` 的產物）

規格 `schemas/context-package.schema.json`（`aistorage.contextpackage/v1`）。

## 它是什麼

一個**目錄**，`agora checkout <起點>… [--task "…"] [--resume] -o <目錄>` 產出：

```
<目錄>/
  package.json                              # 這個規格描述的檔案
  raw/01-opencode-ses_abc123-3f9a2b1c8d4e.json   # 原始紀錄，原封不動
  raw/02-opencode-ses_def456-7c1e9a0b5d2f.json
```

起點包**不屬於任何一個 coding agent**。它只帶三樣東西：

1. **起點之前的原始紀錄**（`raw/` 下，位元組原封不動，沒有截斷、沒有轉換）；
2. **要交代的任務**（`package.json` 的 `task`）；
3. **來源 Session 的 id**（每個 `segments[]` 一個）。

由 `agora-<coding agent 名稱>` 轉接器把它載入成該 agent 的原生 session
（`agora-opencode load <起點包>`）。Agora 本身不 import 任何 agent 專屬的東西。

## 為什麼是「原始紀錄」而不是閱讀版

閱讀版是為了**跨來源應用**閱讀而生的：它把工具呼叫的輸入輸出壓成摘要、拿掉
thinking 段落。同一個 agent 接續時要的是「新 session 送給模型的開頭與原 session
位元組相同」（ADR 0010 的 prompt cache 要求），閱讀版做不到。

**原始紀錄從哪裡來**：讀取視圖**不發佈** raw 的位元組——那等於把真本的位元組複製
一份到衍生物裡（衍生物該能被隨時重建，真本不該）。所以路徑是「位址 → 位元組」：

1. 讀取介面給該快照的 **annex key**（`snapshots` 表的 `annex_key`；
   key 形狀是 `SHA256E-s<size>--<sha256>`，**內容定址，所以 key 本身就是位址**）；
2. 唯讀身分對 **Agora 真本前綴**有唯讀分享，所以能依 key 直接取回物件；
3. **用 key 內嵌的 sha256 與 size 驗證**。這是那個設計的安全關鍵：key 是內容的
   位址，所以「塞一份同名假檔進物件資料夾」過不了這一步。取不到或對不上就明確
   拒絕、**不產出起點包**。

唯讀權限就夠（分享步驟見 `docs/runbooks/deploy.md` 步驟 2）。起點包裡
`raw_sha256` 仍然必須等於 `snapshot_sha256`，所以起點包被搬來搬去之後仍然能自己驗。

## 各欄位在做什麼

| 欄位 | 說明 |
|---|---|
| `format` | 固定 `aistorage.contextpackage/v1` |
| `created_at` / `created_by` | 建立時間與建立者的 profile（`profile:mac-opencode`），不是自由文字 |
| `generator` | 一定是 `{tool: "agora", version: "1"}`——本格式由 `agora checkout` 產生 |
| `task` | `--task` 給的任務字串，沒有就是 `null`。**不是**要送進模型的訊息 |
| `segments[]` | 要帶進新 session 的片段，**陣列順序就是送進模型的順序** |
| `segments[].message_id` | 接續點：截到哪一則為止（含該則）。`null` 代表不截斷 |
| `segments[].raw_file` / `raw_size` / `raw_sha256` | 原始紀錄本體的路徑與指紋；`raw_sha256` 必須等於 `snapshot_sha256` |
| `new_session.session_id` | 為這個起點**預留**的新 Session id（認領單裡帶著它，轉接器必須沿用） |
| `new_session.claimed_handoffs` | 這次已放進收件匣並被讀取介面確認的認領 |
| `totals` | 片段數、訊息數、原始紀錄位元組、純文字字元數 |
| `context_limit` | 上限、計量單位、是否在上限內 |

## 三個設計決定

### 1. 順序：最長的一段放最前面

n→1（統合）時，呼叫端給的次序不代表什麼，所以 `agora checkout` 把**最長的一段
放最前面**（ADR 0010 已定）。理由是 prompt cache：開頭越長，命中的機會越大。
長度是 `text_chars`（接續點之前的純文字字元數），平手時維持呼叫端給的次序
（`order` 仍記錄呼叫端的原次序以外的最終次序，寫進檔案的 `order` 欄位）。

其餘片段維持呼叫端給的次序接在後面。parent 由轉接器手工鏈成一串
（opencode 匯入時不驗 parent，見 `docs/spike/session-import.md` Q4）。

### 2. 長度：超過就明確拒絕，不產出

`agora checkout` 會把 `totals.text_chars` 與上限比。上限預設是
`DEFAULT_MAX_CONTEXT_CHARS`，呼叫端可用 `--max-chars` 覆寫。

**超過就直接失敗，目錄不會被建立**。`context_limit.within_limit` 寫在檔案裡
而且**一定是 true**——留在檔案裡是為了讓載入端不必再猜。ADR 0010 說得很直接：
期 1 先偵測並明確拒絕，不默默截斷。

### 3. 認領：先認領、後產出；重跑要沿用同一個認領

起點是交接單時，`agora checkout` 會把一筆認領放進收件匣，並等讀取介面確認
（照同步並提交的規則：等每一個項目看得到或有拒收原因）。

**被拒就不產出起點包**，目錄不會被建立。理由：接續 Link 屬於自己之後才開工
（AGENTS.md 的「認領」），一個沒有 Link 的新 session 是孤兒。

為什麼認領會被接受：提交流程要求「認領者必須已經在 Agora 裡」，而新 session 這時
還不存在於任何來源應用裡。所以**那一筆認領自己帶著新 session 的預留**——一個
**零則訊息**的空匯出檔：

- **不是**來源 session 的截斷副本。舊的作法是把來源 raw 當成新 session 的第一份
  快照送出去，於是 Agora 裡會出現一份掛在新 id 底下、內容與標題全是錯的紀錄；被拒
  時那份複製還會變成沒有人接手的孤兒。
- 預留在 `apply_claim` 的**寫入階段**才落進真本，所以被拒時 Agora 裡**什麼都沒
  多**——連那個空紀錄都沒有。
- 寫入順序是 預留 → link → handoff → claim。
- `new_session.session_id` 必須是轉接器匯入時會用的那個 id，所以轉接器不能自己編。

**逾時或中斷之後重跑會自動沿用同一個認領**（不需要額外參數）。交接單只能被認領一次，
所以重跑若換一個新的 claim id 或新的預留 session id，只會得到 `already_claimed`，而那張
單永遠沒有 session 接手。`agora checkout` 在送出之前就把這次的 claim id、item_key、
預留時間與新 session id 寫進**本機認領記錄**（`$AISTORAGE_CHECKOUT_CLAIMS`，預設是
`~/.aistorage/checkout-claims/` 底下**一張交接單一個檔案**）；之後只要記錄裡有這張單，
就自動沿用它們，不論有沒有帶 `--resume`（`--resume` 只是把這個行為寫成明示）。
提交流程因此會把重送的那一筆當成同一個 item（冪等）。

- 記錄**不得被覆蓋**：`put` 遇到既有記錄會拒絕，因為舊記錄對應的認領可能已經被接受。
- 認領被**明確拒收**時，**只有被拒的那幾張**的記錄會被刪掉。n→1 時可能只有一張被拒
  （那張已被別人接走），此時已經被接受的那幾張**要留著記錄**——它們的交接單已被預留
  的 session 接走，記錄是那個預留日後唯一的線索。這種情形會以
  `PartialClaimAccepted` 明確報出「哪幾張被接走、預留的 session 是哪個」。
- 逾時**不刪**記錄：那筆可能下一輪就被接受。
- 認領**成立之後**才發生的失敗（例如產出目錄被佔）會以 `ClaimAlreadyCommitted`
  明確報出「認領已經成立」，不要重送新的認領。

**n→1 的預留必須一致**：一次 checkout 的所有起點共用同一個新 session id，所以記錄裡
各張單預留的 id 若不一致，代表其中某一張已被另一個 checkout 預留走，明確拒絕。

**所有本機檢查都在登記認領之前完成**：輸出目錄可寫而且是空的、每段 raw 的 SHA-256
等於快照雜湊、Agora 的物件讀得到。任一項失敗就停下——那時這張交接單還沒被動過。
反過來說，認領之後只剩下寫 `package.json` 與一次改名。

## 轉接器要做的（以 opencode 為例）

1. 讀 `package.json`，照 `segments` 的順序；
2. 每段讀 `raw_file`、**先驗 `raw_sha256`**；
3. 截到 `message_id`（`null` 就不截）；
4. `session`／`message`／`part` 的 id **全部重編**（沿用 id 匯入會被靜默丟棄，
   見 `docs/spike/session-import.md` Q1-1），session id 用 `new_session.session_id`；
5. n→1 時第二段起首則的 parent 手工鏈到前一段末則；
6. 在**目標專案目錄**執行 `opencode import`（匯入會把 directory／project 強制
   改寫為當下目錄，Q1-2）；
7. 印出新 session id。

之後 `opencode --session <新 id>` 就帶著前面的內容了。開在哪、由誰開，
是呼叫者的事。
