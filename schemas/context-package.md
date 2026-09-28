# 起點包（`agora checkout` 的產物）

規格 `schemas/context-package.schema.json`（`aistorage.contextpackage/v1`）。

## 它是什麼

一個**目錄**，`agora checkout <起點>… [--task "…"] -o <目錄>` 產出：

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

所以閱取視圖除了閱讀版，還發佈每個快照的**原始紀錄本體**（索引的 `raws` 表、
manifest 信任集合內的 raw 檔）。`agora checkout` 取的就是它，並且用
`snapshot_sha256` 驗過——`raw_sha256` 必須等於它，所以起點包被搬來搬去之後
仍然能自己驗。

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
| `new_session.session_id` | 為這個起點**預留**的新 Session id |
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

### 3. 認領：先認領、後產出

起點是交接單時，`agora checkout` 會把一筆認領放進收件匣，並等讀取介面確認
（照同步並提交的規則：等每一個項目看得到或有拒收原因）。

**被拒就不產出起點包**，目錄不會被建立。理由：接續 Link 屬於自己之後才開工
（AGENTS.md 的「認領」），一個沒有 Link 的新 session 是孤兒。

為什麼一個新 session 的認領能被接受：提交流程要求「認領者必須已經在 Agora 裡」，
所以 `agora checkout` 會把新 session 的第一份快照（`new_session.session_id`
那一個空 session 的匯出檔）與認領**同一批**提交，`apply` 的順序是 session 在前。
`new_session.session_id` 必須是轉接器匯入時會用的那個 id，所以轉接器不能自己編。

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
