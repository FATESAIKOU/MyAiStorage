# Design: Angareion 待決（pending decision）（#468，b，作者模式）

作者：專案架構師（作者模式，親寫）。核准方式：非作者的審查者照本文 §15 的架構清單審，PMO 向本人說明，本人同意後才實作。
基準：main `0577584`。文中的 `file:line` 都以這個 commit 為準。
修訂：第 1 輪修正（回應非作者審查 REVIEW-468 的 H1、H2、M1～M4、L1）。

---

## §0 給本人看的一頁

**結論**：待決就是一則一般的 Angareion 訊息，只是 front matter 多了 `datatype: decision`，內文是固定格式的 YAML。新增、更新、撤回、作答、送出、snapshot 都是**另外 append 一則**這種訊息，已經送出的訊息永遠不改。Angareion 讀回整串訊息，照到達順序**匯總**出每一題目前的狀態。後端只需要「append 一則」和「依序讀回」兩種能力，沒有用到 GitHub 的 label、關閉、編輯、reaction。

**做法（我的建議，都已寫進本文）**

1. 格式：front matter 加一個欄位 `datatype`。一般訊息寫 `message`；舊訊息沒有這個欄位，讀的時候當成 `message`。版本號 `angareion: 1` **不升**：舊讀者遇到不是 1 的版本會整則丟掉（§2 L-2）；新增欄位則會被舊讀者略過，訊息照樣送到。
2. 指令：`angareion decision <action>`，共 7 個 action：`ask`、`update`、`withdraw`、`answer`、`submit`、`list`、`snapshot`。現有的 `send`／`read`／`wait`／`log` 就是 `message` 這個 datatype 的動作，不另外加 `angareion message …` 這種別名。
3. 待決存在**提問頻道**裡，綁定（頻道, 提問者）。每一題的 id 就是出題那則訊息的 id（ULID）。
4. 作答是草稿，也 append 進 Angareion，所以 App 或 CLI 等任何畫面看到的都是同一份。本人按「送出」時，Angareion 把**已答**的題按（頻道, 提問者）分組，每組 append 一則 `submit`，寄給提問者（`to` 與 `mentions` 都是提問者）。這一則就是「答案回到提問頻道並 mention 提問者」。未答的題留著不動。
5. 權限：在不改後端權限的前提下，靠**匯總規則**判定。只有出題者能更新或撤回；只有題目指定的作答者能作答或送出。不符的紀錄照樣留在後端（append-only），匯總時略過，並列在診斷裡。這只防誤用，不防冒名，因為 Angareion 本來就不做身分認證（spec「訊息格式」）。
6. snapshot：每個（頻道, 提問者）各自寫，只有提問者能寫。紀錄數超過門檻時自動寫，也可以手動寫。snapshot 存的是「到 `through` 為止的完整前綴」匯總出來的 open 題目（含去重需要的已見 id），以及所有已結束題目的 id（墓碑）。讀的時候找最新一則有效的 snapshot，從它記的 `through` 之後往下匯總。snapshot 壞掉、找不到，或發現 `through` 之前有晚到的紀錄時，退回上一則有效的 snapshot 或從頭匯總。**`list --all`（看已結束的題目與答案）一律從頭完整匯總，不用 snapshot**，所以 PMO 確認答案的路徑不受 snapshot 影響。

**要本人決定的點**（細節見 §17）

- **Q-1** 作答者身分：App 用哪一個 AI 名當 `answerer`。建議由 PMO 指定一個固定的名字。
- **Q-2** v1 規定「只有 PMO 出題」，這條限制放在哪裡。建議不放進 Angareion（通訊層不認識 PMO），改由 App 的顯示過濾和 PMO 的工作規則守。
- **Q-3** 作答可不可以只寫一段文字、不選任何選項（相當於「其他」）。建議可以：`choice` 可以不給，但那樣 `note` 必填。
- **Q-4** 送出時要不要叫醒提問者（poke）。建議 v1 不叫醒；PMO 自己會 `wait` 或 tick 收信。
- **Q-5** 會寫出 `datatype` 的兩步（PR-2 一般訊息寫 `datatype: message`、PR-4 的 `decision` 指令寫 `datatype: decision`），都要等 MyAiEntry 確認它的讀者能略過未知欄位（§12 R-A1）才能 merge。這是對 App 的要求，要由 PMO 去問。
- **Q-8 App 草稿的差異**：MyAiEntry 已在 #468 貼了待決欄位草稿，和本設計有幾處不同（X-1～X-14，見 §20），其中 X-12、X-13（App 的「不多一條送出路徑」「列表由 App 自己推導」）和本文的 P-3、R-A2、R-A3 直接衝突，要本人裁定。

---

## §1 要件對照表

| # | 要件／已定事項 | 本文位置 |
|---|---|---|
| 1 | 待決綁定「頻道＋提出者」 | §4.3（`ask_channel`＋`asker`；v1 必須等於 front matter 的 `channel`＋`from`）、§6.1（匯總的鍵是（頻道, 提問者））、C-6 |
| 2 | 待決是一般訊息的特殊格式；一般訊息也有自己的格式，兩者可以區分 | §4.1、§4.2（`datatype: message`／`decision`）、C-1～C-3 |
| 3 | 寫入、刪除、更新都是 append；禁止改動已送出的訊息 | §3 P-1、§4.3（每個 op 都是一則新訊息）、§11（不用編輯或刪除）、C-4 |
| 4 | CRUD 由 Angareion 處理；後端只讀寫訊息，不依賴後端特有能力；Angareion 盡量簡單 | §5、§11 後端中立性表、§13 模組切割（後端介面不變）、C-20 |
| 5 | 用 datatype 區分；介面是 `angareion <datatype> <action>`；歷史匯總成目前狀態；太長時寫 snapshot，從 snapshot 往後匯總 | §4.1、§5、§6、§7、C-7～C-17 |
| 已定 | 目前只有 PMO 出題，格式仍保留「提問頻道＋提問者」 | 格式保留：§4.3（欄位保留；v1 限定相等）。**「只有 PMO 出題」這條限制本文沒有落實**：Angareion 允許任何身分出題（§10），守在哪裡待本人決定（Q-2） |
| 已定 | 一題一題作答，不是點了就送 | §4.3 `answer`（草稿）、§9、C-12 |
| 已定 | 按送出時，已答的一次送出、未答的留著 | §9、C-13、C-14 |
| 已定 | 答案回到提問頻道，並 mention 提問者 | §4.3 `submit`（`channel`＝提問頻道，`to`／`mentions`＝提問者）、C-13 |
| 已定 | 待決內容是選擇題：背景、內文、選項、推薦選項、理由 | §4.3 `ask` 欄位（`background`、`body`、`options`、`recommended`、`reason`） |
| 已定 | App 只讀 Angareion 的匯總，不能反過來影響 Angareion | §12 R-A2～R-A4 |

---

## §2 現況與限制（原樣引用）

- **L-1：訊息有六個欄位，再加 id 和 at。** `openspec/specs/angareion/spec.md:125`：「一則訊息 MUST 有 `from`、`to`、`channel`、`urgency`（0～9 的整數，預設 5）、`content`、`attachments`，加上寄件者產生的 `id`（ULID）與 `at`（含時區）；`id` 與 `at` 只用來辨識與顯示，MUST NOT 用來判斷新舊。」
- **L-2：版本號不是 1 的訊息，舊讀者整則丟掉。** `src/angareion/messages.py:195-198`：
  ```python
  if self.angareion != ANGAREION_VERSION:
      raise MessageValidationError(
          f"Unsupported angareion version: {self.angareion} (expected {ANGAREION_VERSION})"
      )
  ```
  非 strict 的 `parse_message` 遇到這個錯誤會回 `None`（`messages.py:433-436`），讀者就把那則跳過（`src/angareion/github.py:374-376`：`if parsed is None or not scope_matches(...): continue`）。所以**升版本號會讓所有舊讀者收不到新訊息**。
- **L-3：舊讀者會略過未知的 front matter 欄位。** `messages.py:405`：`required = {"angareion", "id", "at", "from", "channel", "urgency", "attachments"}`。只檢查這些欄位有沒有；`Message(...)` 也只取已知的鍵（`messages.py:418-430`）。多出來的鍵不會讓解析失敗。
- **L-4：log 快取只存 `message_to_dict`。** `src/angareion/log_cache.py:182`：`"message": message_to_dict(parsed),`。`message_to_dict`（`messages.py:235-248`）只輸出已知欄位，所以 front matter 的新欄位進不了舊快取，`content` 則完整保留。另外 `log_cache.py:18` 是 `LOG_CACHE_VERSION = 1`，spec 規定「版本不認得…MUST 整檔視為空並全量重建」（`spec.md:407`）。
- **L-5：收件範圍只看位址。** `src/angareion/backend.py:304-308`：
  ```python
  def recipient_matches(message: Message, recipient: str | None) -> bool:
      """Whether a message counts as "to me": addressed to me, a broadcast, or it mentions me."""
      if recipient is None:
          return True
      return message.to is None or message.to == recipient or recipient in message.mentions
  ```
  沒有 `to` 就是廣播，「寄給我的」每個人都會收到；也不會排除自己寄的。
- **L-6：判斷新舊只看後端。** `spec.md:66`：「判斷訊息新不新 MUST 只看後端（到達位置與後端記錄的時刻），MUST NOT 使用寄件者填的 `at` 或 `id`。」游標容許晚到的範圍是 `CURSOR_SEEN_MARGIN_SECONDS = 60.0`（`backend.py:19`）。
- **L-7：對外的介面現在固定只有六個。** `spec.md:224`：「Angareion 對外 MUST 只有六個介面…」。`spec.md:292-294` 的 scenario 也斷言頂層子指令「恰好是 `channel`、`send`、`wait`、`read`、`log`」。**本設計會改這條**（delta 見 §19 PR-4）。
- **L-8：後端協定。** `backend.py:247-300` 的 `ChannelBackend`（Protocol）只有六個方法：`create_channel`、`verify_channel`、`close_channel`、`send`、`check`、`read`。**列舉不在協定裡**：`enumerate_conditional` 只有實作，在 `FakeBackend`（`backend.py:546-564`，回 `(到達位置, 後端時刻, 解析結果)`，依到達位置排序）和 `GitHubIssuesBackend`（`github.py:482`）；`log_cache.py:168` 直接呼叫 `backend.enumerate_conditional(...)`，靠的是 duck typing，型別上沒有宣告（spec「後端可以替換」`spec.md:205` 有寫列舉這個動作）。這是現有的型別契約缺口，處置見 §13.1、C-20。
- **L-9：訊息長度有上限，單位是字元。** `messages.py:32`：`MAX_MESSAGE_LENGTH = 65536`；檢查在 `messages.py:336`：`if len(text) > MAX_MESSAGE_LENGTH:`，`text` 是含 front matter 的整則文字，`len` 算的是 Python 字元數，不是 UTF-8 位元組。本文說「長度上限」一律指 65536 字元（含 front matter）。
- **L-10：送出前會讀回檢查。** `messages.py:344-359`：送出前先把訊息讀回來，任何欄位不同就拒送。新增欄位必須一起進 `message_to_dict`，否則讀回比對時會少一欄。
- **L-11：TS 側也有讀者和寫者。** `messages.py:300-302`（`serialize_message` 的 docstring）：「`mentions` is written as one flow-style line (`mentions: [a, b]`; the TS side's hand-written parser relies on it)」。`messages.py:342-343`：「The TS side (MyAiEntry 801013c) only checks field by field and does not read it back」。TS 讀者遇到未知欄位會怎樣，本文**沒驗到**，列為對 MyAiEntry 的要求 R-A1。
- **L-12：PMO 的收信輪。** `src/pmo/round.py:66-71`：team 階段收的是「不是秘書、也不是 PMO 自己寄的」訊息（`return sender != ctx.config.pmo_identity`）。讀 JSON 用的是 `data.get(...)`，多出來的欄位不影響。

---

## §3 原則

- **P-1：一切都是 append。** Angareion 不編輯、不刪除、不關閉任何訊息，也不改後端的任何屬性。狀態完全由「依序讀回的訊息」算出來。
- **P-2：後端不認識 datatype。** 對後端來說，待決就是一則訊息。後端介面（`ChannelBackend`）一個方法都不加，也不改。
- **P-3：只有一份匯總規則，放在 Angareion。** App、PMO、`pmo` 模組都不自己匯總，只讀 `angareion decision list --json`。
- **P-4：寫入嚴格，讀取寬鬆**（沿用 `messages.py:7-13` 的規矩）。寫的時候欄位不合就拒送；讀的時候壞紀錄只略過那一則並記診斷，整份匯總不因此失敗。
- **P-5：位址規則不變。** `read`／`wait`／`aicomm inbox` 仍然只看位址，只多一條小規則：自己寫的待決紀錄不遞送給自己（C-3）。待決紀錄都有 `to`，**不是廣播**，所以不會吵到頻道裡的其他成員。
- **P-6：順序只看到達位置**（L-6）。`at` 和 ULID 不參與排序。

---

## §4 訊息格式

### 4.1 datatype 欄位

- front matter 加一個欄位 `datatype`，值的文法是 `^[a-z][a-z0-9-]{0,31}$`。v1 只有兩個值：`message` 和 `decision`。
- **寫入**：所有新寫的訊息一律寫 `datatype`。`send` 寫 `message`，`angareion decision …` 寫 `decision`。序列化時 front matter 的鍵順序是 `angareion`、`datatype`、`id`、`at`、`from`、`to`（有才寫）、`mentions`（有才寫）、`in_reply_to`（有才寫）、`channel`、`urgency`、`attachments`；也就是在現有順序（`messages.py:305-330`：`head`、手寫的 `mentions`／`in_reply_to` 兩行、`tail`）的 `angareion` 後面插入 `datatype`。
- **讀取**：
  - 沒有這個欄位，讀成 `message`（舊訊息）。
  - 值不是字串，或不符文法，也讀成 `message`，跟 `mentions` 一樣「壞值丟掉、訊息保留」（`messages.py:363-380`）。
  - 值合法但不認得（例如將來的 `poll`），照原字串保留：照位址遞送，匯總不理它。
- `angareion: 1` **不升版**（理由見 L-2）。
- `message_to_dict` 加 `"datatype"` 鍵，所以 `read --json` 和 `log --json` 都會輸出它。log 快取的版本升到 2，舊快取會整檔重建（L-4）。

### 4.2 一般訊息的格式

就是現在的格式，只是明寫 `datatype: message`：

```text
---
angareion: 1
datatype: message
id: 01J…
at: '2026-11-15T03:00:00+00:00'
from: myaistorage-pm-w1
to: myaistorage-arch-w1-23
channel: team-myaistorage-w1
urgency: 5
attachments: []
---
內文
```

**怎麼區分兩種訊息**：`datatype` 是 `decision` 的才是待決，其他都是一般訊息（含沒有這個欄位的舊訊息）。只看 front matter，不必解析內文。

### 4.3 待決的格式（payload 版本 1）

待決訊息的 front matter 照一般訊息寫（`datatype: decision`）。**內文是一個 YAML mapping**，用 `yaml.safe_dump(…, allow_unicode=True, sort_keys=False)` 寫出。前兩個鍵固定是 `decision: 1`（payload 版本）和 `op`。payload 放在內文而不是 front matter，有兩個原因：

- 舊讀者和舊快取會完整保留內文（L-4）；
- TS 的 front matter 解析器是手寫的（L-11），放在內文就不必讓它學巢狀結構（推論：L-11 只說它依賴 flow 格式的 `mentions`，它能不能讀巢狀結構沒驗到）。

舊讀者看到的是可讀的 YAML 文字。

| op | `from` | front matter 的 `to`／`mentions` | `in_reply_to` | 內文的鍵（* 為必填） |
|---|---|---|---|---|
| `ask` | 提問者 | `to`＝作答者 | 無 | `decision`*、`op`*、`ask_channel`*、`asker`*、`answerer`*、`title`*、`background`*、`body`*、`options`*、`recommended`*、`reason`* |
| `update` | 提問者 | `to`＝更新後的作答者 | 題目 id | `decision`*、`op`*、`ref`*，以及 ask 的題目欄位全部重給（`answerer`*、`title`*、`background`*、`body`*、`options`*、`recommended`*、`reason`*）。不能改 `ask_channel` 和 `asker` |
| `withdraw` | 提問者 | `to`＝目前的作答者 | 題目 id | `decision`*、`op`*、`ref`*、`reason` |
| `answer` | 作答者 | `to`＝作答者自己 | 題目 id | `decision`*、`op`*、`ref`*、`base`*、`choice`、`note`、`clear` |
| `submit` | 作答者 | `to`＝提問者、`mentions: [提問者]` | 無 | `decision`*、`op`*、`asker`*、`items`*（每一項：`ref`*、`base`*、`title`*、`choice`、`note`；同一則裡 `ref` 不得重複） |
| `snapshot` | 提問者 | `to`＝提問者自己 | 無 | `decision`*、`op`*、`asker`*、`through`*、`covered`*、`items`*（每一題 open 題目的完整狀態）、`closed`*（已結束題目的 id），見 §7.2 |

欄位規則（寫入時嚴格檢查，任何一條不符就 exit 1、什麼都不送）：

- **題目 id**：等於出題那則 `ask` 訊息的 `id`。`ref` 必須是 26 字的 ULID。
- **`ask_channel`、`asker`（綁定）**：v1 必須等於這則紀錄的 front matter `channel` 和 `from`。這兩個欄位**保留在格式裡**，將來放寬（例如由 PMO 代別人出題）時不必改格式，只要放寬這條等式（Q-6）。
- **`answerer`**：一個合法的 AI 名（當前文法）。
- **`title`**：單行，1～120 字。`background`、`body`、`reason` 是字串，`body` 不得為空。
- **`options`**：2～9 個 `{key, text}`。`key` 符合 `^[A-Z]$`，不得重複；`text` 不得為空。`recommended` 必須是其中一個 `key`。
- **`base`**：作答時看到的那一版的 id。`ask` 本身算第一版，之後每個被接受的 `update` 是新的一版，版本 id 就是那則紀錄的 `id`。
- **`answer`**：恰好是下面三種之一：
  - 給 `choice`（`options` 裡的某個 `key`），`note` 可有可無；
  - 給 `clear: true`（清掉草稿），不得同時有 `choice` 或 `note`；
  - 只給 `note`、不給 `choice`，僅限 Q-3 採「可以」時。
- **`note`**：1～`NOTE_MAX`（4000）字元。設上限是為了讓任何一題的 submit 項單獨一則一定放得下（§9 的分塊靠這一點）。
- **`submit.items[].title`**：只是抄一份給人看，匯總時不當依據。
- **整則長度**：不得超過 `MAX_MESSAGE_LENGTH`，即 65536 字元，含 front matter（L-9）。
- **讀取時**：不認得的 `decision` 版本，整則紀錄略過，診斷 `unsupported_version`；payload 裡多出來的鍵略過不理。

範例（`ask` 的內文）：

```yaml
decision: 1
op: ask
ask_channel: <提問頻道>
asker: <提問者的 AI 名>
answerer: <Q-1 決定的 AI 名>
title: 要不要先合 PR-1
background: …
body: …
options:
- key: A
  text: 先合
- key: B
  text: 等 App 確認
recommended: B
reason: …
```

---

## §5 指令：`angareion decision <action>`

這是新的第七個介面家族（L-7 要改）。旗標與輸出照現有的寫法，不出現後端的概念（spec「六個介面」第二段）。

| action | 用法 | stdout | 為什麼需要（能不加就不加） |
|---|---|---|---|
| `ask` | `--channel <ch> (--body <YAML> \| --body -)` | 新題目的 id | 新增。少了它就沒有待決 |
| `update` | `--channel <ch> --ref <ULID> (--body <YAML> \| --body -)` | 這則紀錄的 id | 本人要求「放太久的要能更新」。不能拿「撤回＋重出」代替：那樣題目 id 會變，已有的草稿和討論串都會斷 |
| `withdraw` | `--channel <ch> --ref <ULID> [--reason <文字>]` | 這則紀錄的 id | 刪除。append-only 的世界裡，刪除就是一則撤回紀錄 |
| `answer` | `--channel <ch> --ref <ULID> (--choice <鍵> [--note <文字>] \| --clear)` | 這則紀錄的 id | 一題一題作答（本人要求「不是點了就送」）。`--clear` 讓已答的題可以退回成未答，不另外開一個 action |
| `submit` | `[--channel <ch>]…` | 每個 submit 紀錄的 id，一行一個；沒有可送的就不印 | 本人要求「一次送出」 |
| `list` | `[--channel <ch>]… [--all] [--json]` | 匯總結果 | App 和 PMO 只讀這個（P-3）。不給 `--all` 只列 open 的題，可以用 snapshot；給 `--all` 列出本機鏡像裡**整個頻道歷史**的所有題目（open、answered、withdrawn），一律從頭完整匯總、不用 snapshot（§7.6） |
| `snapshot` | `--channel <ch>` | snapshot 紀錄的 id；沒有可涵蓋的就不印 | 手動壓縮，也方便測試。自動觸發見 §7 |

**不加的動作**：

- `show`：查一題的歷史，用 `angareion log --thread <題目 id>`，因為 `update`／`withdraw`／`answer` 的 `in_reply_to` 都是題目 id。
- `reopen`：已答或已撤回的題，要再問就重出一題。
- `angareion message <action>` 別名：現有的頂層指令就是 `message` 的動作。

**共通規則**：

- `ask`／`update` 的 `--body` 給的是題目欄位的 YAML mapping（§4.3 那一列除了 `decision`、`op`、`ask_channel`、`asker`、`ref` 之外的鍵）。那幾個鍵由 Angareion 填；body 裡出現它們，或出現未知的鍵，都 exit 1。沒給 `--body` 也 exit 1（同 `send`，`spec.md:125`）。
- **寫入前的預檢**：寫入的 action 會先照 `log` 的方式同步該頻道的 log 快取（`spec.md:399-402`），用目前的匯總檢查前置條件，不符就 exit 1、什麼都不送。前置條件包括：題目不存在、已經結束、不是出題者、不是作答者、沒有草稿可以清。預檢只是提早擋下；**最終的判定是匯總**（§6），因為兩個寫入者之間有競態。
- `answer` 的 `base` 由 Angareion 填入預檢時看到的目前版本 id。`submit` 的每一項，也用預檢時匯總裡的草稿填。
- **結束碼**同 `spec.md:171`：0 成功；1 用法、輸入或前置條件錯（**保證什麼都沒送**）；2 後端、網路或設定錯（可能已送出一部分）。`submit` 的完整規則（分塊、整批預檢、部分已送、重送）見 §9.1。
- 寫入和讀取都**不讀、不動游標**，跟 `log` 一樣（`spec.md:301`）。
- 身分照 `send`：`from` 是解析出的身分（`src/angareion/cli.py:335`）。

`list --json` 的形狀（App 的契約）：

```json
{
  "decisions": [
    {"id": "<題目 id>", "channel": "…", "asker": "…", "answerer": "…",
     "status": "open|answered|withdrawn", "version": "<版本 id>", "revisions": 1,
     "title": "…", "background": "…", "body": "…",
     "options": [{"key": "A", "text": "…"}], "recommended": "A", "reason": "…",
     "asked_at": "<後端時刻>", "updated_at": "<後端時刻>",
     "draft": {"record": "<id>", "base": "<id>", "choice": "A", "note": "…"},
     "answer": {"record": "<submit id>", "choice": "A", "note": "…", "submitted_at": "<後端時刻>"},
     "withdrawn": {"record": "<id>", "reason": "…"}}
  ],
  "ignored": [{"record": "<訊息 id>", "channel": "…", "op": "submit",
               "ref": "<題目 id 或 null>", "item": 0, "code": "stale_base"}]
}
```

- `draft`、`answer`、`withdrawn` 沒有值時是 `null`。
- `ignored` 每一筆是一個不被接受的單位：整則不接受時一筆（`item` 是 `null`）；`submit` 逐項判時，每個不被接受的項目各一筆，`item` 是它在 `items` 裡的索引（從 0 起），`ref` 是那一項的 `ref`。`ref` 讀不出來（例如 `bad_payload`）時是 `null`。
- 不給 `--all` 時，`ignored` 只涵蓋實際被匯總的紀錄（snapshot 的 `through` 之後的，加上壞掉的 snapshot 本身）；要看全部用 `--all`。
- 排序：`(asked_at, channel, id)`，由舊到新，跟 `log` 跨頻道的排序鍵同一套（`spec.md:312`）。
- 文字模式：每題一塊，含狀態、題目、選項、推薦、草稿。`ignored` 只在 stderr 印一行總數。

---

## §6 匯總規則

### 6.1 輸入、順序與鍵

- **輸入**：一個頻道裡，所有 `datatype: decision` 的訊息，照**到達位置**排序（log 快取已經是這個順序，`log_cache.py:197`）。`at` 和 ULID 不參與排序（L-6，P-6）。
- **鍵**：每一題以題目 id 識別，歸屬於（`ask_channel`, `asker`）。v1 的 `ask_channel` 一定是紀錄所在的頻道，所以各頻道分開匯總。
- 匯總是**純函式**：`fold(依到達順序排好的紀錄) → (題目狀態, 診斷)`。每次都用整個快照重算，不做增量累加；同一組輸入一定得到同一個結果。

### 6.2 每一題的狀態

狀態轉移見 §14 圖 3。

| 收到的紀錄（依到達順序處理） | 接受條件 | 效果 | 不接受時的診斷碼 |
|---|---|---|---|
| `ask` | payload 合法；`ask_channel`＝`channel`；`asker`＝`from`；題目 id 不在「已知題目 id」裡（§6.4） | 新增 open 題目；版本＝這則的 id | `bad_payload`／`binding_mismatch`／`duplicate_id` |
| `update` | 題目存在而且是 open；`from`＝出題者；這則的 id 不在該題的已見 id 裡（§6.4） | 換掉題目欄位；版本＝這則的 id；**清掉草稿** | `unknown_ref`／`closed_ref`／`not_owner`／`duplicate_id` |
| `withdraw` | 題目存在而且是 open；`from`＝出題者 | 狀態變 withdrawn（結束） | `unknown_ref`／`closed_ref`／`not_owner` |
| `answer` | 題目是 open；`from`＝目前的作答者；`base`＝目前的版本；這則的 id 不在該題的已見 id 裡 | 設定草稿，後到的蓋掉先到的；`clear` 把草稿設成無 | `unknown_ref`／`closed_ref`／`not_answerer`／`stale_base`／`duplicate_id` |
| `submit` | **逐項判**，依 `items` 的順序：`ref` 沒在本則前面的項出現過；題目是 open；`from`＝那題的作答者；`asker`＝那題的提問者；`base`＝目前的版本。整則還要求 front matter 的 `to`＝`asker`，而且 `mentions` 含 `asker`，否則整則都不接受 | 被接受的那幾項：狀態變 answered（結束），答案取 submit 項裡的值（不是取草稿） | 整則：`bad_payload`；逐項：`duplicate_ref`（同一則裡第二次以後出現的 `ref`，不論第一次接不接受）／`unknown_ref`／`closed_ref`／`not_answerer`／`stale_base` |
| `snapshot` | 見 §7.3 | 決定該提問者從哪裡開始匯總 | `bad_snapshot` |

### 6.3 邊界情況

- **同一題多次更新**：依到達順序逐一套用，最後一則被接受的就是目前版本。`revisions` 是被接受的 ask 加 update 的數目。
- **同一秒**：到達位置是全序，同一秒也分得出先後。不看 `at`。
- **兩則互相競爭**（例如 `update` 和 `submit`、`withdraw` 和 `submit`、兩個畫面同時 `submit`）：先到達的勝。後到的照上表，判為 `stale_base`／`closed_ref`，題目狀態不會被它改掉。重複的 submit 不會產生第二個答案（C-11）。
- **亂序晚到**（到達位置比較小的紀錄晚一點才出現在列表裡）：因為每次都重算（6.1），它會被放回正確的位置。結果可能和前一次匯總不同，但一定等於「完整歷史的匯總」。晚到的紀錄落在某則 snapshot 的 `through` 之前時，那則 snapshot 的 `covered` 對不上而被判 `snapshot_stale`，退回從頭匯總（§7.4）；只有 log 鏡像本身收不到的晚到（超出增量同步的 `since` 窗，`spec.md:402`）才看不到，這和 `log` 的限制相同。
- **缺漏**：後端少給了某一則，Angareion 無從得知。引用不存在題目的紀錄判 `unknown_ref` 並略過；之後那一則出現了，重算就會修正。
- **後端上的內容被人改過**：Angareion 自己永遠不改（P-1）。若有人繞過 Angareion 在後端改了某則留言，log 快取會用新內容覆蓋（`spec.md:402`），匯總就照新內容算。這是**已知限制**，跟 log 的已知限制同類（`spec.md:410`），寫進 skill 說明。
- **不是 decision 的訊息**（一般訊息、不認得的 datatype）不參與匯總。

### 6.4 重複與 id 衝突

後端可能把同一則訊息給兩次（不同到達位置、相同 `id`），人也可能把同一段文字重送。規則只依「被接受過的紀錄」去重，不記被拒的紀錄：

- **已知題目 id**＝目前所有題目（open 和已結束）的 id。`ask` 的 id 在裡面 → `duplicate_id`，先到的那題不變；不論兩則內容是否相同（id 衝突也是先到者勝）。
- **每題的已見 id**＝這一題的 id，加上被接受的 `update` 的 id（即所有版本 id），加上**目前版本**下被接受的 `answer` 的 id。`update`／`answer` 的 id 在 ref 指向那題的已見 id 裡 → `duplicate_id`，題目不變。換了新版本之後，舊版本的 answer id 就不必再記：舊版本的 answer 重送一定是 `stale_base`。
- **已結束的題目**：任何引用它的 `update`／`withdraw`／`answer`／`submit` 項都是 `closed_ref`，所以已結束題目的重送不必另記 id。
- **被拒的紀錄不留痕跡**：它重送時照當下的狀態重新判。例：`withdraw Q` 先到（題目還沒出現，`unknown_ref`），之後 `ask Q`，再來同一則 withdraw 重送 → 這次接受，Q 變 withdrawn。從頭匯總和從 snapshot 匯總都這樣判，所以兩條路徑一致。
- **檢查的範圍只到 ref 指向的那一題**：`update`／`answer` 的 id 和別題的已見 id 撞到，不算重複（那是冒名或偽造，不防，§10）。
- 依這些規則，snapshot 只要帶「open 題目的已見 id」和「已結束題目的 id」（§7.2），從 snapshot 往後匯總就和從頭匯總判得一樣，已結束的題目不會因為重送而復活（C-16、C-26）。

---

## §7 snapshot

### 7.1 誰寫、什麼時候寫

- 每個（頻道, 提問者）各自有 snapshot，**只能由那位提問者寫**（`from`＝`asker`）。這樣跟「只有出題者能改題目」是同一條權限線。
- **自動觸發**：出題者的 `ask`／`update`／`withdraw` 寫成功之後，若該（頻道, 提問者）在目前有效 snapshot 的 `through` 之後的相關紀錄已經 ≥ `SNAPSHOT_EVERY`（200 則），就再寫一則 snapshot。
  - 「相關紀錄」指：該提問者的 ask，以及 ref 指向該提問者題目的 update、withdraw、answer、submit 項。
  - 自動寫失敗只在 stderr 警告一次，結束碼和 stdout 不變（原本那筆寫入已經成功）。
  - 自動寫出的 snapshot id **不印**在 stdout（stdout 仍只有原本那筆的 id）。
- **手動**：`angareion decision snapshot --channel <ch>`，不看門檻。

### 7.2 格式與內容（前綴投影）

```yaml
decision: 1
op: snapshot
asker: <提問者>
through: <最後一則被涵蓋的紀錄 id>
covered: <到達位置 ≤ through 的 decision 紀錄則數（整個頻道，不分提問者）>
items:                     # open 的題目，依 (asked_at, id)
- id: <題目 id>
  version: <版本 id>
  revisions: 2
  versions: [<題目 id>, <update id>]   # 已見 id 的一部分：所有版本 id
  answers: [<answer id>, …]            # 已見 id 的一部分：目前版本下被接受的 answer id
  answerer: …
  title: …
  background: …
  body: …
  options: [...]
  recommended: A
  reason: …
  asked_at: <後端時刻>
  updated_at: <後端時刻>
  draft: {record: <id>, base: <id>, choice: A, note: …}   # 沒有就寫 null
closed: [<題目 id>, …]     # 已結束（answered／withdrawn）題目的 id（墓碑），依 (asked_at, id)
```

- **內容的定義**：令 P＝列表中到達位置 ≤ `through` 的位置的所有紀錄（完整前綴，含不是這位提問者的）。`items` 和 `closed` **等於** `fold(P)` 裡屬於這位提問者的題目的投影：open 的題目進 `items`（帶上表的全部欄位），已結束的題目只留 id 進 `closed`。**不是**用「目前的匯總結果」去抄，`through` 之後的紀錄一筆都不能影響 snapshot 的內容（C-16 的交錯案例）。
- `closed` 只留 id，答案與撤回理由不放：snapshot 只服務「目前還 open 的題目」這個視圖。已結束題目的內容由 `list --all` 從頭匯總取得（§7.6）。
- snapshot 本身也是一則 append 的 decision 訊息，同樣受 65536 字元的上限（L-9）。超過就不寫：手動觸發時 exit 1；自動觸發時 stderr 警告，匯總照舊從上一則有效 snapshot 算起。snapshot 只是加速，寫不出來不影響正確性。容量的粗估：一個已結束題目的 id 約 30 字元，`closed` 一項不到 30 字元，約 2000 題以上才會因為墓碑寫不出來；到那時候要再設計（分段 snapshot 或修剪），列為已知限制。

### 7.3 讀者怎麼找到最新的 snapshot

- 讀者手上本來就有頻道的完整鏡像（log 快取）。匯總時，從尾端往前找該提問者**最新一則有效的** snapshot。有效的條件：
  - payload 合法；
  - `from`＝`asker`；
  - `through` 指向的紀錄在列表裡**恰好一則**（重複出現就判無效），而且排在這則 snapshot **之前**；
  - **列表中到達位置 ≤ `through` 的 decision 紀錄則數＝`covered`**。不相等代表 snapshot 寫出之後，`through` 之前又冒出（或少了）紀錄，這則 snapshot 不再等於前綴，判無效（診斷 `snapshot_stale`）。
- 不必去後端額外找「最新 snapshot 的指標」。因為只能 append，也不能有可改動的指標。
- 最新那則無效時，記診斷（`bad_snapshot` 或 `snapshot_stale`），改用前一則有效的；全部無效或根本沒有，就從頭匯總。
- 使用方式：該提問者的題目狀態以 snapshot 的 `items`（含已見 id）和 `closed` 為起點，再依到達順序套用 `through` **之後**的所有紀錄（§6 的規則，含 §6.4 的去重）。`through` 之前的紀錄一概不看（「不無限往前追溯」）。
- 有效的定義只看列表裡有什麼，所以手上列表相同的讀者算出來都一樣。

### 7.4 涵蓋範圍、穩定窗與晚到的紀錄

- **cutoff 是時刻，`through` 是位置**，兩者的關係要明寫：cutoff＝「列表中最新一則訊息的後端時刻 − 60 秒」；`through` 取**最大的**到達位置 p，使得列表中**所有**到達位置 ≤ p 的紀錄（不只 p 那一則）後端時刻都 ≤ cutoff，而且 p 那一則是 decision 紀錄。不能只看 `through` 那一則的時刻：後端的時刻和位置不一定同步，位置小的紀錄時刻可能比較晚。找不到這樣的 p 就不寫（手動：exit 0、stdout 空）。
- **後端假設 A-1（新增依賴，明列）**：「一則紀錄 r 出現在列表裡之後，若列表最新的後端時刻已經比 r 晚 60 秒以上，到達位置比 r 小的紀錄都已經出現在列表裡。」60 秒沿用游標的 margin（L-6）；log 快取的增量同步本來就帶 `since = 上次的後端時刻 − 60 秒`（`spec.md:402`），也就是 log 鏡像本身已經有類似的依賴，但 snapshot 是**又一處**依賴它，所以列在這裡，不當成已知事實。
- **A-1 失效時的語意**：有一則紀錄在 snapshot 寫出之後才出現，而且位置 ≤ `through`。
  - 這則 snapshot 的 `covered` 和列表對不上，讀者判 `snapshot_stale`，退回前一則有效的 snapshot 或從頭匯總。結果等於「用目前列表從頭匯總」，**不會**默默忽略晚到的紀錄。
  - 還在用舊列表的讀者（尚未同步到那則晚到紀錄）看不到它，這和沒有 snapshot 時一樣，同步之後就一致。
  - 偵測不到的情況：同時有一則晚到、一則從列表消失（則數相抵）。這需要後端刪除或改寫，屬 §6.3「後端上的內容被人改過」那一類已知限制。
- 後端若給不出後端時刻（將來別種後端），就不寫 snapshot，一律完整匯總（§11）。

### 7.5 誠實說明 snapshot 省下了什麼

在「只能依序讀回」的限制下，全新的讀者第一次還是要把整串讀一遍（log 快取的全量同步），snapshot 省不掉這一步。它省的是：

1. 預設的 `list`（只看 open）、寫入前的預檢、`submit` 分組，匯總時不必從頭走過所有歷史；
2. 很久以前的壞紀錄不再出現在預設 `list` 的 `ignored` 裡。

它**不**省 `list --all` 的匯總（§7.6）。將來 log 快取要不要修剪 snapshot 以前的紀錄，不在本單範圍；`list --all`、`covered` 檢查和退回從頭匯總都需要完整前綴，所以在另有設計之前**不能**修剪。

### 7.6 `list --all` 與 PMO 確認答案的路徑

- `list --all` **一律從頭完整匯總**本機鏡像裡該頻道的全部紀錄，不使用任何 snapshot。時間範圍＝頻道從建立到最近一次同步的整段歷史（log 快取是全量鏡像，spec「log 的本機快取」）。
- 所以已結束的題目和它們的答案、撤回理由，永遠查得到，不會因為 snapshot 而消失。PMO 照 R-P2 確認哪幾項被接受，走的就是這條路徑。
- 等價性因此分兩段寫：
  - 預設 `list` 的結果（open 題目，含草稿、版本、`revisions`、已見 id）＝`list --all` 結果裡 status 是 open 的那幾題（C-16）；
  - 從 snapshot 往後匯總時，已結束題目的集合（`closed` 加上 `through` 之後結束的）＝從頭匯總的已結束題目的 id 集合（C-16）。
  - `ignored` 不在等價的範圍內：兩條路徑看的紀錄範圍本來就不同（§5）。

---

## §8 相容矩陣

讀者：

- 「舊 py」＝main `0577584` 的 `angareion`；
- 「新 py」＝本設計完成後的版本；
- 「TS」＝MyAiEntry 的讀者，現況沒驗到。

| 訊息 \ 讀者 | 舊 py `read`／`wait` | 舊 py `log`（快取 v1） | 新 py `read`／`wait` | 新 py `log` | 新 py `decision list` | TS 讀者 |
|---|---|---|---|---|---|---|
| 舊訊息（沒有 `datatype`） | 照舊 | 照舊 | 照舊；`datatype` 讀成 `message` | 同左；JSON 多一個 `datatype: "message"` | 不參與 | 照舊 |
| 新一般訊息（`datatype: message`） | 照舊（略過未知欄位，L-3） | 照舊（欄位不進快取，L-4） | 照舊 | 照舊 | 不參與 | **要求 R-A1**：必須略過未知欄位；否則這類訊息會不見，所以 PR-2 要等它確認 |
| `ask`／`update`／`withdraw`（`to`＝作答者） | 作答者收到一則內文是 YAML 的訊息；其他人收不到（不是廣播） | 看得到，顯示成一般訊息 | 作答者收到（`wait` 會醒） | 看得到，標 `decision` | 匯總 | R-A1；App 改用 `decision list` |
| `answer`（`to`＝作答者自己） | 作答者自己的 `read` 會收到自己的草稿（雜訊、無害） | 看得到 | **不遞送給自己**（C-3） | 看得到 | 匯總 | 同上 |
| `submit`（`to`／`mentions`＝提問者） | 提問者收到（內文是 YAML，可讀） | 看得到 | 提問者收到；`--mentioned` 也收到 | 看得到 | 匯總 | 同上 |
| `snapshot`（`to`＝提問者自己） | 提問者自己的 `read` 會收到（雜訊，200 則才一次） | 看得到 | 不遞送給自己（C-3） | 看得到 | 匯總 | 同上 |
| `datatype` 是不認得的值 | 照舊 | 照舊 | 照位址遞送 | 照舊，標原值 | 不參與 | R-A1 |

其他讀者：

- **游標**：游標的格式和推進規則都不變。`decision` 的所有 action 不讀也不動游標（C-18）。`read`／`wait` 多的那條規則（自己寫的待決不遞送給自己）只影響「寄給我的」和「@ 我」兩種目標；`--channel` 仍然讀到全部（`spec.md:66`）。
- **`aicomm`**：`aicomm send` 呼叫 `angareion send`，所以寫的是 `datatype: message`。`aicomm inbox` 和 `aicomm log` 是薄包裝，照轉 JSON，不受影響（`src/aicomm/cli.py:682` 只判斷結果是不是空陣列）。待決紀錄由 `angareion decision` 寫，不經過 aicomm，所以**不會主動叫醒（poke）任何人**（Q-4）。但「不 poke」不等於「不會讓人醒」：已經阻塞在 `angareion wait` 或 `aicomm inbox --wait` 的讀者，只要紀錄符合它的目標就會命中返回。具體是：`ask`／`update`／`withdraw` 讓作答者的 wait 命中；`submit` 讓提問者的 wait 命中（「寄給我的」和「@ 我」都算）；`answer`、`snapshot` 在新 py 不讓寫的人自己的「寄給我的」／「@ 我」wait 命中（C-3），但用 `--channel` 等整個頻道的 wait，任何一則紀錄都會命中（含自己寫的）。
- **`pmo` 模組**：PMO 是提問者，它收到的 `submit` 會在 team 階段印成 `[msg]`（`src/pmo/round.py:66-71`），有去處（D-18）。它自己寫的 snapshot 在新 py 不會遞送給自己；舊 py 會遞送，但 team 階段會把它濾掉（`sender != pmo_identity`）。
- **舊版寫者**：還沒升級的 py 或 TS 寫者寫出沒有 `datatype` 的訊息，新讀者讀成 `message`，不受影響。舊版寫者寫不出 decision。

---

## §9 作答與送出

1. 作答者對每一題執行 `answer`，append 一則草稿（`to` 是自己；不 poke 任何人，也不讓自己的「寄給我的」wait 命中，見 §8）。改答案就再 append 一則，後到的蓋掉先到的；退回成未答用 `--clear`。
2. 按「送出」等於執行 `submit`：
   - 用目前的匯總，取出「作答者是我、狀態 open、有草稿、草稿的 `base` 等於目前版本」的題目；
   - 按（`ask_channel`, `asker`）分組，每組在 `ask_channel` append **一則或多則** `submit`（多則只在超長時，見 9.1）。每一則的 `to` 和 `mentions` 都是提問者，`items` 是那一組已答的題目；
   - 這幾則就是「答案回到提問頻道並 mention 提問者」；
   - 未答的題不在任何 `items` 裡，維持 open。
3. 提問者用平常的 `read`、`aicomm inbox` 或 `pmo tick` 收到 `submit`。要確認哪幾項真的被接受（有沒有因為競態判成 `stale_base`），一律以 `decision list --all` 為準（R-P2；`--all` 從頭完整匯總，§7.6）。
4. 一次送出寫成好幾則（每組至少一則），**不是原子操作**。但每一則各自完整，而且重跑不會重送（已經被接受的題已經結束），所以不需要兩階段提交。

### 9.1 容量、分塊與部分失敗

- **長度單位**：65536 字元，算整則文字（含 front matter），即 `messages.py:336` 的 `len(text)`（L-9）。
- **單題一定放得下**：`title` ≤ 120 字元、`note` ≤ `NOTE_MAX`（4000）字元（§4.3），所以只含一項的 submit 一定不超長。這是寫入 `answer` 時就檢查的。
- **分塊**：每組的項目依 (`asked_at`, `ref`) 排好，從頭依序裝進目前這一則；再加一項會超長時，這一則就結束，從下一項開一則新的。每一則各自完整（自己的 `to`、`mentions`、`asker`），項目逐項判（§6.2），所以分成幾則不改語意。
- **整批預檢**：先算出所有組、所有分塊、每一則完整的訊息文字（含序列化與讀回檢查，L-10），全部成功才開始送。任何一則組不出來 → exit 1，**什麼都沒送**。
- **送的順序**：組依 (`channel`, `asker`)，組內依分塊的順序，一則一則送。
- **部分失敗**：第 k 則送出時後端錯 → 立刻停，不送後面的；stdout 是前 k−1 則的 id（一行一個），stderr 說明哪一則失敗；exit 2。
- **重送**：重跑 `submit`，從匯總重算。已被接受的項已經結束，不會再出現；剩下的照上面的規則重新分組、分塊。後端其實收到了、但回應失敗的那一則：同步之後它在列表裡，它的項已經結束，不會重送；同步還看不到它（A-1 的時窗內）的話，重跑可能再送一次，這時後到的那則逐項判 `closed_ref`，**不會產生第二個答案**（C-11）。
- **沒有可送的**：exit 0、stdout 空、stderr 一行說明。

---

## §10 權限

| 動作 | 誰可以 | 靠什麼 |
|---|---|---|
| 出題 | 任何身分（v1 的「只有 PMO 出題」不放在 Angareion，見 Q-2） | — |
| 更新、撤回 | 只有出題者（`from`＝那題 ask 的 `from`） | 匯總規則（§6.2）＋CLI 預檢 |
| 作答、送出 | 只有那題**目前的** `answerer` | 同上 |
| snapshot | 只有該提問者 | §7.1、§7.3 |

- **不靠後端權限**：不用 issue 的鎖定、指派、label 等任何功能。
- **不防冒名**：spec 明寫「不做嚴格的身分認證（任何人可以用任何名字送）」（`spec.md:125`）。這裡的權限擋的是**誤用**，例如別的 AI 在頻道看到題目、自作主張去作答。題目指定 `answerer`，就是為了擋這種事。
- **snapshot 也可以被冒名**：任何人都能用提問者的名字寫一則 snapshot，`items` 寫成假的內容（語法合法、`covered` 也對得上）。有效的條件（§7.3）只檢查形狀、`from` 和位置，**檢查不出內容是錯的**。後果是預設 `list`、預檢和 `submit` 分組看到假的 open 狀態；`list --all` 不用 snapshot，不受影響，所以 PMO 照 R-P2 確認答案的路徑仍然可信（在「不防冒名」的前提下，指的是不受假 snapshot 影響）。這和題目、答案本身可以被冒名是同一類，不另外防。
- 不接受的紀錄**留在後端**，因為不能刪也不能改。`list --json` 的 `ignored` 列出它們，留給人追查。

---

## §11 後端中立性

只准用兩種能力：「append 一則」和「依序讀回」。下表逐項列出本設計對後端的使用：

| 使用的能力 | 用在哪 | 是否 GitHub 特有 | 現有協定是否已有 |
|---|---|---|---|
| append 一則訊息到頻道 | 所有寫入的 action | 否 | 是（`ChannelBackend.send`，`backend.py:262`） |
| 依到達順序讀回頻道的全部訊息，附到達位置 | 匯總、預檢、找 snapshot | 否（任何 append log 都有順序） | 有實作、沒宣告：列舉在 `FakeBackend`（`backend.py:546-564`）和 GitHub 後端（`github.py:482`）都有，但不在 `ChannelBackend` 協定裡（L-8）。本設計把它宣告成另一個協定 `EnumerableBackend`（§13.1），行為不變 |
| 每則訊息的後端時刻 | 只用在 snapshot 的 cutoff（§7.4）。後端給不出時就不寫 snapshot，功能不受影響 | 否 | 是（游標已經在用，`spec.md:66`） |
| 後端假設 A-1：最新後端時刻超過某則 60 秒以上時，位置比它小的紀錄都已可見 | 只用在 snapshot（§7.4）。失效時由 `covered` 偵測、退回從頭匯總；`list --all` 不依賴它 | 否 | **新增的依賴**（log 增量同步已有類似假設，`spec.md:402`），未對真的 GitHub 實測 |

**沒有用到**：label、關閉或重開實體、編輯留言、刪除留言、reaction、指派、鎖定、issue 標題或內文、搜尋 API、留言 URL、任何 webhook。分頁：decision 層只拿 log 快取排好的整份列表，不看頁面的順序或大小。

`ChannelBackend` 協定不加也不改任何方法；列舉另立協定，不擴大 `ChannelBackend`（C-20）。

---

## §12 接法：對 MyAiEntry 與 MyPMO 的要求

只寫對它們的要求，不描述它們的現況。

**MyAiEntry（App，作答的畫面之一）**

- **R-A1**：讀者必須能略過 front matter 裡未知的鍵（至少要能略過 `datatype`，值是 `message` 或 `decision` 都要能讀），而且把沒有 `datatype` 的訊息當成一般訊息。**PR-2 和 PR-4 都要等這條確認才能 merge**（Q-5）：PR-2 讓所有一般訊息帶 `datatype: message`；PR-4 讓 `decision` 紀錄帶 `datatype: decision`，App 要收得到題目、作答者要看得到題目的原文，同樣需要 TS 讀者不拒收。
  - **驗收方式**：MyAiEntry 在它的 repo 裡加一個測試（或指出既有的），把本文 §4.2 的一般訊息範例，以及一則 `datatype: decision` 的 `ask`、一則 `submit`（內文照 §4.3），交給 TS 讀者，斷言三則都解析成功、`from`／`to`／`mentions`／`content` 與 py 讀到的相同。由 PMO 把那個測試的 commit 連結貼到 #468，PM 看到連結才 merge PR-2、PR-4。
  - **沒有過渡模式**：不採用「先寫不帶 `datatype` 的 decision」之類的過渡，因為那樣 py 讀者分不出待決和一般訊息（要件 2）。R-A1 沒確認之前，PR-1、PR-3 照常可以合（不寫出 `datatype`），PR-2、PR-4 等著。
- **R-A2**：待決的清單和狀態只來自 `angareion decision list --json`（或將來由 Angareion 提供的同一份匯總）。App 不自己匯總，也不對待決訊息做任何後端操作。
- **R-A3**：作答和送出只透過 `angareion decision answer` 和 `submit`。App 的 TS 寫者不得寫出 `datatype: decision` 的訊息，也不得編輯或刪除任何訊息。
- **R-A4**：App 自己的介面狀態（例如目前選的是哪一題、還沒按下的選擇）只留在 App 裡，不寫進 Angareion，也不改變 Angareion 的行為。只有按下「作答」才 append。
- **R-A5**：App 用一個固定的 AI 名當作答者（Q-1），並可以照 Q-2 的決定，只顯示允許的提問者出的題。
- 「與 App 草稿對照」：見 §20。

**MyPMO（出題端，現在唯一的提問者）**

- **R-P1**：出題、更新、撤回只用 `angareion decision ask`／`update`／`withdraw`；`answerer` 填 Q-1 定的名字。
- **R-P2**：收到 `submit` 之後，以 `angareion decision list --all --json` 確認哪幾項被接受，再照答案行動；不要只依 `submit` 的原文行動，因為可能有 `stale_base`。
- **R-P3**：放太久的題目自己用 `update` 或 `withdraw` 整理；Angareion 不會自動過期。

**本 repo 的 `pmo` 模組**：現狀已經能收到 `submit`（§8）。要不要給它一個專用的事件標籤（例如 `[待決已答]`）列為 Q-7，不在本單必做的範圍。

---

## §13 模組切割、介面、依賴方向、SOLID

### 13.1 模組

| 模組 | 職責 | 不做什麼 |
|---|---|---|
| `angareion/messages.py`（改） | `Message` 加 `datatype` 欄位（預設 `message`）；寫入時嚴格、讀取時寬鬆；`message_to_dict` 加這個鍵；常數 `SELF_SILENT_DATATYPES = frozenset({"decision"})`（「寫的人自己不收」的 datatype 表，格式層擁有） | 不解析 decision 的 payload |
| `angareion/decision.py`（新） | 純函式：payload 的結構與驗證（寫入嚴格、讀取寬鬆）；`fold(records, *, use_snapshot) -> FoldResult`；`snapshot_for(...)`；`submit_batches(...)` | 不碰後端、不碰快取、不碰 CLI、不碰時鐘 |
| `angareion/decision_cli.py`（新） | `angareion decision <action>` 的處理函式：同步 log 快取 → 匯總 → 預檢 → 組 `Message` → `backend.send` | 不實作匯總規則 |
| `angareion/cli.py`（改） | 註冊 `decision` 子指令並轉交；`_format_message` 在 `datatype` 不是 `message` 時多印一行 `datatype: …` | 不含待決的邏輯 |
| `angareion/backend.py`（改） | `recipient_matches`／`mention_matches` 加一條：`message.datatype in SELF_SILENT_DATATYPES` 而且 `from` 是讀者自己時不算；新增協定 `EnumerableBackend`（只有 `enumerate_conditional` 一個方法，簽名照現有實作，`@runtime_checkable`） | `ChannelBackend` 的方法不變；不認得任何特定 datatype 的名字 |
| `angareion/log_cache.py`（改） | `LOG_CACHE_VERSION` 升到 2；還原 `Message` 時帶 `datatype`；`sync` 的 `backend` 參數標成 `EnumerableBackend` | — |

**列舉協定的裁決（L-8 的缺口）**：另立 `EnumerableBackend`，不併進 `ChannelBackend`。理由：`ChannelBackend` 的 docstring 寫的是「the same operations as the five interfaces」，列舉是 `log` 和匯總才需要的唯讀能力；分開符合 I（介面隔離），也讓「後端只要 append 和依序讀回」這件事在型別上看得見。這只是把現有的 duck typing 寫成宣告，`FakeBackend` 和 `GitHubIssuesBackend` 都不必改行為，放在 PR-1。

### 13.2 依賴方向（不得有環）

```text
cli → decision_cli → {decision, log_cache, backend, messages}
decision → messages        （只用 Message 型別與 AI 名、ULID 的驗證）
log_cache → {backend, messages}（不變；backend 只多用 EnumerableBackend 型別）
backend → messages          （不變；多讀 SELF_SILENT_DATATYPES）
```

`decision` 不 import `backend`、`log_cache`、`cli`；`decision_cli` 不被 `decision` import。`aicomm` 和 `pmo` 仍然只透過子行程呼叫 `angareion`，不 import 它（不變）。

### 13.3 公開介面（Python）

```python
# decision.py
PAYLOAD_VERSION = 1
SNAPSHOT_EVERY = 200
NOTE_MAX = 4000
STABLE_WINDOW_SECONDS = 60.0   # A-1；與 CURSOR_SEEN_MARGIN_SECONDS 同值
OPS = ("ask", "update", "withdraw", "answer", "submit", "snapshot")

@dataclass(frozen=True)
class Record:            # 一則 decision 訊息加上它的位置資訊
    message: Message
    position: int             # 到達位置（log 快取的排序鍵）；只用來排序與數 covered，不出現在 payload
    received_at: str | None   # 後端時刻；後端給不出時為 None

@dataclass
class FoldResult:
    decisions: list[dict]     # §5 list --json 的 decisions 元素
    ignored: list[dict]       # {"record", "channel", "op", "ref", "item", "code"}
    seen: dict[str, frozenset[str]]  # 每一題 open 題目的已見 id（§6.4）；不輸出到 JSON，給 C-16 比對
    closed: frozenset[str]    # 已結束題目的 id

def parse_payload(message: Message) -> dict            # 寬鬆讀；不合法時丟 PayloadError（帶診斷碼）
def build_payload(op: str, **fields) -> str           # 嚴格寫；回傳內文 YAML；不合法時丟 MessageValidationError
def fold(records: Sequence[Record], *, use_snapshot: bool) -> FoldResult
    # records 是一個頻道裡依到達位置排好的「所有」紀錄（含一般訊息，用來算 cutoff；只匯總 decision）；純函式
    # use_snapshot=False 給 list --all；True 給預設 list、預檢、submit
def snapshot_for(records, asker) -> str | None
    # 要寫的 snapshot 內文：先依 §7.4 選 through，再對「位置 ≤ through 的前綴」做 fold(use_snapshot=True) 取投影
    # 找不到 through 或後端給不出時刻時回 None
def submit_batches(result: FoldResult, answerer: str, *, render) -> list[tuple[str, str, list[dict]]]
    # (channel, asker, items)；已依 §9.1 分好塊。render(channel, asker, items) -> str 回傳整則文字，用來量長度；
    # 單項就放不下時丟 MessageValidationError（照 NOTE_MAX 不該發生）
```

### 13.4 SOLID

- **S**：每個模組只有一個改變的理由。格式和匯總規則在 `decision.py`；I/O 和預檢在 `decision_cli.py`；位址遞送在 `backend.py`。符合。
- **O**：將來要加新的 datatype，只要新增 `<datatype>.py` 和 `<datatype>_cli.py`，`messages.py` 不必再改（它已經會保留不認得的 datatype）。符合。
- **L**：`Message` 加了一個有預設值的欄位，現有的建構呼叫照樣成立；`FakeBackend` 和 GitHub 後端的列舉行為一致（`spec.md:219-222`）。符合。
- **I**：後端協定沒有因為待決而變寬；`decision.py` 只拿到排好序的 `Record`，不知道快取的存在。符合。
- **D**：`decision_cli` 依賴的是 `ChannelBackend` 協定和 `LogCache`，不依賴 GitHub 的實作。符合。
- **I（補）**：列舉另立 `EnumerableBackend`，`ChannelBackend` 不變寬；`log_cache` 和 `decision_cli` 只依賴它們真正用到的那個協定。符合。
- **O 的例外（重判）**：前一版讓 `backend.recipient_matches` 直接認得 `"decision"` 這個字，我列為例外。重判之後**不再接受這個例外**：代價只是一個常數，不需要註冊機制。改成 `messages.SELF_SILENT_DATATYPES`（格式層擁有「哪些 datatype 寫的人自己不收」這張表），遞送層只讀這張表，不認得任何特定 datatype。將來新的 datatype 要同樣的規則，只改這張表，`backend.py` 不動。符合。

---

## §14 圖（mermaid；語法未機器驗證）

圖 1：訊息種類與匯總流程

```mermaid
flowchart TD
  A[後端：依到達順序讀回] --> B{front matter 有 datatype?}
  B -- 沒有 --> M[一般訊息 message]
  B -- message --> M
  B -- 不認得的值 --> U[保留原值：照位址遞送，不匯總]
  B -- decision --> D[待決紀錄]
  M --> R[read / wait / log：只看位址]
  D --> R2[read / wait：只看位址，但自己寫的 decision 不算寄給自己 C-3；log：全部]
  D --> F[decision.fold]
  F --> A2{list --all?}
  A2 -- 是 --> S2
  A2 -- 否 --> S{該提問者有有效 snapshot? covered 對得上?}
  S -- 有 --> S1[以 snapshot.items 為起點，套用 through 之後的紀錄]
  S -- 沒有、壞掉或 stale --> S2[從頭套用全部紀錄]
  S1 --> O[decisions + ignored]
  S2 --> O
  O --> L[decision list --json：App 與 PMO 只讀這個]
```

圖 2：一次「出題 → 更新 → 作答 → 送出」

```mermaid
sequenceDiagram
  participant P as 提問者 (PMO)
  participant A as Angareion
  participant B as 後端 (append log)
  participant H as 作答者 (本人的 App)
  P->>A: decision ask --channel X
  A->>B: append ask (to=作答者)  [題目 id = Q]
  H->>A: decision list --json
  A->>B: 依序讀回
  A-->>H: Q open, version=Q
  P->>A: decision update --ref Q
  A->>B: append update (in_reply_to=Q) [version=U1]
  H->>A: decision answer --ref Q --choice B
  A->>B: append answer (base=U1, to=自己)
  Note over H: 其他題未答，留著
  H->>A: decision submit
  A->>B: append submit (channel=X, to=提問者, mentions=[提問者], items=[Q:B])
  P->>A: read（或 aicomm inbox／pmo tick）
  A-->>P: 收到 submit
  P->>A: decision list --all --json
  A-->>P: Q answered = B
```

圖 3：一題的狀態

```mermaid
stateDiagram-v2
  [*] --> open: ask
  open --> open: update 清草稿、answer、answer 清除
  open --> answered: submit 項被接受
  open --> withdrawn: withdraw
  answered --> [*]
  withdrawn --> [*]
```

---

## §15 架構清單：C-編號契約

每條都寫成「輸入 → 輸出或錯誤」，並註明在哪一層測。測試層的說明：

- **單元**＝`decision.py`、`messages.py` 的純函式；
- **CLI**＝用 `FakeBackend` 跑 `angareion` 的 main；
- 都照 `tests/conftest.py` 的隔離規矩。

| C | 契約 | 測試層 |
|---|---|---|
| C-1 | 文字沒有 `datatype` 的訊息 → `parse_message` 得到 `datatype == "message"`；`message_to_dict` 有 `"datatype": "message"` | 單元 |
| C-2 | `datatype` 值是 `5`、`Decision`、`a b`、長度 33 → 讀成 `message`，訊息照樣讀到；`datatype: poll` → 讀成 `"poll"` | 單元 |
| C-3 | `recipient_matches`／`mention_matches`：`datatype=decision` 而且 `from`＝讀者 → False；`datatype=message` 而且 `from`＝讀者、`to`＝讀者 → True（一般訊息行為不變）；`decision` 而且 `to`＝讀者、`from` 是別人 → True；monkeypatch `SELF_SILENT_DATATYPES` 加入 `poll` 後，`datatype=poll` 而且 `from`＝讀者 → False（遞送層只讀這張表）；`backend.py` 的原始碼不含字串 `"decision"` | 單元＋CLI（`read` 不給 `--channel`） |
| C-4 | 任何 `angareion decision` 寫入的 action 跑完之後，FakeBackend 只多了 N 則新留言，既有的留言內容和數目都不變。N 的值：`submit` 是分塊後的總則數（§9.1），`snapshot` 是 0 或 1，其他是 1；本條的測試把 `SNAPSHOT_EVERY` monkeypatch 成極大值以關掉自動 snapshot（自動 snapshot 的 +1 由 C-19 另測）。FakeBackend 不提供編輯和刪除，測試斷言 `close_channel` 沒被呼叫 | CLI |
| C-5 | `send` 寫出的 front matter，鍵的順序恰好是 `angareion`、`datatype`、`id`、`at`、`from`、[`to`]、[`mentions`]、[`in_reply_to`]、`channel`、`urgency`、`attachments`（方括號＝有才寫；用 YAML 讀出鍵的順序比對，不比行號），`datatype` 的值是 `message`；讀回比對包含 `datatype`（L-10） | 單元 |
| C-6 | `ask` 時 body 裡出現 `ask_channel`、`asker`、`op`、`decision`、`ref` 或未知的鍵 → exit 1，什麼都沒送。payload 是手工組的、`asker`≠`from`（直接 deliver 到 FakeBackend）→ 匯總判 `binding_mismatch`，不出現在 decisions | CLI＋單元 |
| C-7 | `ask` 的欄位違規（`options` 只有 1 個或有 10 個、`key` 重複、`recommended` 不在 `options` 裡、`title` 多行或 121 字、`body` 是空的、`answerer` 不是 AI 名、整則 65537 字元（含 front matter；用中文內文造，確認是算字元不是位元組：65536 字元的中文內文要能送）→ exit 1，什麼都沒送；合法 → stdout 印題目 id，等於那則訊息的 `id` | CLI |
| C-8 | 紀錄依到達位置是 ask(Q)、update₁、update₂ → `version`＝update₂ 的 id、`revisions`＝3、題目欄位等於 update₂ 的；**同一組紀錄把 `at` 倒過來、ULID 倒過來**，結果相同 | 單元 |
| C-9 | update 或 withdraw 的 `from` 不是出題者 → 預檢 exit 1；直接 deliver 進後端 → 匯總判 `not_owner`，題目不變 | CLI＋單元 |
| C-10 | 有 answer(base=V1)，之後 update(V2) → 草稿變成 `null`；之後 answer(base=V1) → `stale_base`；answer(base=V2) → 接受 | 單元 |
| C-11 | 同一題兩則 submit 都合法（模擬兩個畫面）→ 只有到達位置在前的那則被接受，答案取它的；後一則判 `closed_ref`；withdraw 先到、submit 後到 → withdrawn，submit 項判 `closed_ref`；submit 先到、withdraw 後到 → answered，withdraw 判 `closed_ref`；一則 submit 的 `items` 裡同一個 `ref` 出現兩次 → 寫入端 exit 1、什麼都沒送；直接 deliver → 第一項照常判，第二項 `ignored` 有一筆 `code=duplicate_ref`、`item=1`、`ref`＝那題 | 單元＋CLI |
| C-12 | `answer --choice Z`（不在 `options` 裡）→ exit 1；`answer` 的身分不是目前的 `answerer` → exit 1；`answer --clear`，但沒有草稿 → exit 1；`--choice` 和 `--clear` 一起給 → exit 1；`--note` 4001 字元 → exit 1，4000 字元 → 成功 | CLI |
| C-13 | 兩個頻道 X、Y：X 有 Q1、Q2（都已答），Y 有 Q3（已答），X 另有 Q4（未答）→ `submit` 寫出 2 則：X 一則（`items`＝Q1、Q2，`to`＝`mentions[0]`＝提問者，`channel`＝X）、Y 一則；Q4 仍是 open；stdout 有兩行 id，順序是 X、Y | CLI |
| C-14 | FakeBackend 在寫第 2 則 submit 時注入錯誤 → exit 2，stdout 恰好有第 1 則的 id，stderr 指出第 2 則失敗，第 3 則以後沒有送；重跑 `submit` → 只寫出剩下的，第 1 則的題目不重送；FakeBackend 注入「已收下但回應失敗」→ exit 2；重跑時同步看得到它 → 那幾題不重送；以 FakeBackend 讓它暫時不出現在列舉裡再重跑 → 會再送一則，那一則的項全部判 `closed_ref`，`list --all` 每題只有一個答案；沒有可送的時候 → exit 0、stdout 空、stderr 有說明 | CLI |
| C-15 | 匯總：壞掉的 YAML、`decision: 2`、未知的 `op` → 各自進 `ignored`（`bad_payload`／`unsupported_version`／`bad_payload`，`ref` 是 `null`、`item` 是 `null`），其他紀錄照常匯總，結束碼 0；一則 submit 有 3 項、第 2 項 `stale_base` → `ignored` 恰好一筆，`item=1`、`ref`＝那題、`op=submit` | 單元＋CLI（`list`） |
| C-16 | **等價性**（性質測試，seed 固定）：隨機產生一串合法和不合法的紀錄（含重複投遞、id 衝突、各種 op，後端時刻遞增，A-1 成立），在隨機位置用 `snapshot_for` 產生 snapshot 插入 → (a) `fold(use_snapshot=True)` 的 `decisions`＝`fold(use_snapshot=False)` 的 `decisions` 裡 status 是 open 的那幾項（逐鍵相同，含 `draft`、`version`、`revisions`、`asked_at`、`updated_at`）；(b) 兩者的 `seen`（open 題目的已見 id）相同；(c) 前者的 `closed` ＝後者已結束題目的 id 集合；`ignored` 不比；(d) `through` 之前的紀錄確實沒有被匯總（用計數斷言）。**固定交錯案例**（逐一寫成測試）：ask Q、answer Q=A 在 cutoff 前，之後依序是 answer Q=B／update Q／submit Q／withdraw Q 其中之一，再寫 snapshot（`through`＝answer A 那則）→ snapshot 的 `items` 裡 Q 的 `draft` 是 A、`revisions`＝1、不在 `closed` 裡（**前綴投影**，不受 `through` 之後影響），而且 (a)～(c) 成立；把那一則改放到 snapshot 之後，(a)～(c) 同樣成立；H1 的序列（ask、answer、submit、61 秒後的一般訊息、snapshot）→ `list --all` 有 Q answered、答案是 A，預設 `list` 沒有 Q | 單元 |
| C-17 | snapshot 無效的情況：`through` 指不到、`through` 在列表裡出現兩次、`through` 排在 snapshot 之後、`from`≠`asker`、YAML 壞掉 → `bad_snapshot`；`covered` 和列表對不上 → `snapshot_stale`；以上都改用前一則有效的；全部無效 → 結果等於從頭匯總。**A-1 失效**：先寫 snapshot S（`through`＝位置 5，`covered`＝5），之後用 FakeBackend 的 `deliver` 補一則位置 3 的 withdraw Q（後端時刻落在 log 增量同步的 `since` 窗內，確保鏡像收得到）→ 預設 `list` 判 S 為 `snapshot_stale`，Q 是 withdrawn（等於從頭匯總），不是 open | 單元 |
| C-18 | 每一個 `decision` action（含寫入）跑完之後，游標檔逐位元相同；`wait` 的 ETag 狀態檔不被讀寫 | CLI |
| C-19 | 自動 snapshot：`SNAPSHOT_EVERY` 用 monkeypatch 設成 3，出題者連寫 4 則相關紀錄之後再一次 `ask` → 多寫一則 snapshot（只涵蓋整個前綴都過了穩定窗的部分，§7.4），stdout 只有 `ask` 的 id；snapshot 超過 65536 字元 → stderr 警告、`ask` 仍然 exit 0；手動 `snapshot`，但沒有可涵蓋的 → exit 0、stdout 空；手動 `snapshot` 超過 65536 字元 → exit 1；**through 的選法**：位置 1、2、3 的時刻是 t、t+90、t+10，最新時刻 t+100（cutoff＝t+40）→ `through` 是位置 1，不是位置 3（位置 3 本身早於 cutoff，但它的前綴裡有位置 2 晚於 cutoff） | 單元＋CLI |
| C-20 | (a) `ChannelBackend` 協定的方法集合和 main `0577584` 相同（用 `inspect` 斷言方法名恰好六個）；(b) `EnumerableBackend` 的方法集合恰好是 `enumerate_conditional`；(c) `isinstance(FakeBackend(None), EnumerableBackend)` 和 `isinstance(<GitHubIssuesBackend 以假設定建出>, EnumerableBackend)` 都是 True（`runtime_checkable`）；(d) `decision.py` 不 import `backend`、`log_cache`、`cli`、`github`（import 圖測試）；(e) `decision_cli` 呼叫的後端方法只有 `send`（ChannelBackend）和經由 `LogCache.sync` 的 `enumerate_conditional`（用記錄呼叫的假後端斷言） | 單元 |
| C-21 | `log` 快取檔的 `version: 1` → 整檔重建；重建後 `log --json` 每則都有 `datatype` | CLI |
| C-22 | `angareion --help` 的頂層子指令恰好是 `channel`、`send`、`wait`、`read`、`log`、`decision`；`angareion decision --help` 恰好是 7 個 action；`decision` 的旗標、stdout、stderr 都沒有 repo、issue、comment 這些字 | CLI |
| C-23 | `list` 的預設只列 open；`--all` 包含 answered 和 withdrawn，範圍是本機鏡像裡頻道的全部歷史，而且不用 snapshot（把一則有效 snapshot 的 `items` 竄改成假的 open 題目 → 預設 `list` 受影響、`--all` 不受影響）；`--json` 的鍵恰好是 §5 列的（含 `ignored` 的 `op`、`ref`、`item`）；排序是 `(asked_at, channel, id)`；`--channel` 指定未登記的頻道 → exit 1；同步失敗 → exit 2，stdout 空（同 `spec.md:404`） | CLI |
| C-24 | 舊讀者相容：把新寫出的 (1) 一般訊息（含 `datatype: message`）、(2) `ask`、(3) `submit`（含 `mentions`）的文字，交給 main `0577584` 的 `parse_message`、`recipient_matches`、`mention_matches`、`_format_message`（以那個 commit 的程式碼釘成測試資料，或在測試裡從 git 取出）→ 三則都解析成功、`content` 原樣保留；`ask` 只對作答者算「寄給我的」；`submit` 對提問者算「寄給我的」和「@ 我」；第三者兩者都不算 | 單元 |
| C-25 | **submit 容量**（§9.1）：同一組 100 題、每題 `note` 1000 字元 → 寫出 ≥ 2 則，每則 ≤ 65536 字元，每則都有自己的 `to`／`mentions`＝提問者，所有則的 `items` 合起來恰好是那 100 題、沒有重複，順序依 (`asked_at`, `ref`)；`list --all` 100 題都 answered。跨組：X 組要 2 則、Y 組 1 則 → 預檢全過才送，stdout 3 行，順序 X1、X2、Y1。預檢失敗（monkeypatch `render` 讓 Y 組丟 `MessageValidationError`）→ exit 1，FakeBackend 一則都沒多。第 2 則（X2）注入後端錯 → exit 2，stdout 只有 X1，重跑只送 X2 剩下的題和 Y 組 | CLI |
| C-26 | **去重與不復活**（§6.4），每一案都分別在「沒有 snapshot」和「重複那則在 snapshot 之後、原本那則在 `through` 之前」兩種擺法下跑，結果相同：(1) ask Q、withdraw Q、[snapshot]、ask Q 原樣重送（相同 id）→ Q 仍 withdrawn，重送判 `duplicate_id`；(2) 同 (1) 但 withdraw 換成 submit → Q 仍 answered；(3) ask Q、update U1、update U2、[snapshot]、U1 重送 → `version`＝U2、`revisions`＝3，U1 重送判 `duplicate_id`；(4) ask Q、answer A1、answer A2、[snapshot]、A1 重送 → `draft` 仍是 A2；(5) ask Q、submit S、[snapshot]、S 重送 → 每項 `closed_ref`，答案不變；(6) 兩則 ask 不同內容、相同 id → 先到者勝，後者 `duplicate_id`；(7) withdraw Q 先到（`unknown_ref`）、ask Q、[snapshot]、withdraw 重送 → 接受，Q withdrawn | 單元 |

---

## §16 被否決的方案

- **把版本號升成 `angareion: 2`**：所有舊讀者會整則丟掉新訊息（L-2），連一般訊息也收不到。否決。
- **payload 放在 front matter**：舊快取只存已知欄位，待決的資料會在舊 `log` 裡消失（L-4）；TS 的手寫解析器可能也得學巢狀 YAML（推論，L-11 沒驗到）。否決。
- **用後端的功能表達狀態**（label 標狀態、關 issue 表示撤回、編輯留言表示更新、reaction 表示作答）：違反要件 3 和 4。否決。
- **待決另外開一個專用頻道**：待決就不是「綁定提問頻道」了，答案還要另外轉回提問頻道，多一次搬運，也多一個失敗點。否決（將來真的要集中，用 `ask_channel` 放寬，Q-6）。
- **草稿只存在 App 裡**：換個畫面或裝置，草稿就看不到；而且作答變成 App 的機制，CLI 無法參與。否決，草稿 append 進 Angareion（不 poke 任何人，見 §8）。
- **submit 拆成「decision 紀錄＋一則一般訊息」兩次寫入**：兩次寫入不是原子的，中間失敗就要對帳。改成 submit 本身就是寄給提問者的那一則。否決。
- **新的 `read`／`wait` 完全不遞送 decision**：那 submit 就到不了提問者，還得另外寫一則一般訊息。否決，改成只靠位址（P-5），加 C-3 那一條小規則。
- **update 只帶改動的欄位（patch）**：合併規則複雜，同時兩個 patch 的結果難以說清楚。否決，一律整份替換。
- **後端放一個「最新 snapshot」的指標**：那是可以改動的狀態，違反 append-only。否決（§7.3）。
- **有人讀的時候自動寫 snapshot**：讀取引起寫入，多個讀者會同時寫出重複的 snapshot，而且權限也不清楚。否決，只在出題者寫入時觸發（§7.1）。

---

## §17 未決項

| Q | 問題 | 選項 | 建議 | 誰決定 |
|---|---|---|---|---|
| Q-1 | App 用哪一個 AI 名當 `answerer` | a. PMO 指定一個固定的名字；b. 每個裝置一個名字 | a。b 會讓「作答者是誰」變成好幾個，submit 的權限判斷要改成集合 | PMO 提案，本人同意 |
| Q-2 | 「只有 PMO 出題」守在哪 | a. App 只顯示允許的提問者出的題（設定在 App）；b. Angareion 加一份提問者白名單 | a。通訊層不認識組織角色；b 會讓 Angareion 多一份設定和一條規則 | 本人 |
| Q-3 | 可不可以只寫文字、不選選項 | a. 可以（不給 `choice` 時 `note` 必填）；b. 不可以，一定要選 | a。「都不對」是常見的答案 | 本人 |
| Q-4 | submit 要不要叫醒提問者 | a. v1 不叫醒；b. 由 App 寫完之後另外 poke；c. 在 aicomm 加 `aicomm decision submit` 包裝 | a。PMO 不經 sidecar，自己會 `wait`；要叫醒時再選 c，Angareion 不必改 | PMO |
| Q-5 | PR-2（寫 `datatype: message`）和 PR-4（寫 `datatype: decision`）的閘門 | a. 兩個都等 MyAiEntry 確認 R-A1（驗收方式見 §12）之後才 merge；b. 直接 merge；c. 只擋 PR-2 | a。TS 讀者若拒絕未知欄位，PR-2 會讓 App 看不到所有新訊息，PR-4 會讓 App 看不到題目。c 是前一版的寫法，漏了 PR-4 | PM 經 PMO 問 MyAiEntry |
| Q-6 | 將來放寬「提問頻道＋提問者」的規則 | 放寬 §4.3 的等式，例如 `from` 可以是代理人；權限改由 `from` 判斷，回覆寄到 `ask_channel` | 本單不做，格式已經保留欄位 | 本人（將來另開單） |
| Q-7 | `pmo` 模組要不要給 `submit` 一個專用的事件標籤 | a. 照現在印成 `[msg]`；b. 新增 `[待決已答]` | a。現在已經有去處；要做的話另開 d 單 | PM |
| Q-8 | App 草稿與本設計的差異（§20 X-1～X-14） | 逐項見 §20 | X-12（作答／送出路徑）、X-13（列表由誰匯總）是衝突，要本人裁定；X-4（過期）、X-6（複選）、X-8（選項命名）要本人選；其餘照本設計 | 本人（PMO 說明） |

---

## §18 測試策略

- **單元**：`decision.py` 是純函式，測 C-8、C-10、C-11、C-15、C-16、C-17、C-19（through 的選法）、C-20、C-26。C-16 用 seed 固定的隨機產生器（不新增依賴）。
- **CLI**：用 FakeBackend（`backend.py:421-441` 的 `deliver` 可以指定到達位置和時刻，用來造同一秒、亂序、晚到、重複投遞、競態的情況），跑 C-3、C-4、C-6、C-7、C-9、C-11～C-14、C-18、C-19、C-21～C-23、C-25。
- **相容**：C-1、C-2、C-5、C-24 在 `messages` 的單元層。
- **不測**：真的 GitHub、真的 TS 讀者。TS 那邊交給 MyAiEntry，見 R-A1。
- 驗收門檻由 PM 定。

---

## §19 分 PR 切法

| PR | 內容 | 只合這一步，main 會不會更糟 |
|---|---|---|
| PR-1 | 讀取端：`Message.datatype`（讀取寬鬆、預設 `message`）、`message_to_dict`、log 快取升到 v2、`_format_message` 標出 datatype、`SELF_SILENT_DATATYPES` 與 C-3 的遞送規則、`EnumerableBackend` 協定。`send` **還不寫** `datatype`。測 C-1～C-3、C-20(a)～(c)、C-21、C-24 | 不會。舊訊息照舊；還沒有人寫 decision；快取重建一次 |
| PR-2 | 寫入端：`send` 寫 `datatype: message`。測 C-5。**閘門：Q-5（R-A1）** | 只要 R-A1 成立就不會。不成立的話，App 會看不到新訊息，所以要等確認 |
| PR-3 | `decision.py` 純函式（格式、匯總、去重、snapshot、分組與分塊），沒有接上 CLI。測 C-8、C-10、C-11（單元部分）、C-15～C-17、C-19（through 的選法）、C-20(d)、C-26 | 不會。沒有人呼叫它 |
| PR-4 | `decision_cli.py` 和 `cli.py` 的 `decision` 子指令（7 個 action，含自動 snapshot）；`skills/angareion/SKILL.md` 和 `docs/angareion.md` 的說明；由 PM 寫 spec delta：`angareion` 的「六個介面」改成七個介面家族、「訊息格式」加 datatype，新增 `angareion-pending-decision` capability。測 C-4、C-6、C-7、C-9、C-11～C-14、C-18、C-19、C-20(e)、C-22、C-23、C-25。**閘門：Q-5（R-A1）** | R-A1 成立就不會。新指令只在有人呼叫時才寫入；需要 PR-1（遞送規則）先合，否則草稿會遞送給自己（只是雜訊）。R-A1 不成立的話，App 會看不到題目，所以要等確認 |
| PR-5（選做，Q-7） | `pmo` 的事件標籤 | 不會 |

依賴：PR-1 → PR-3 → PR-4。PR-2 可以和 PR-3、PR-4 並行。PR-2 和 PR-4 都受 Q-5 的閘門控制（前一版說 PR-4 不受影響，是錯的：decision 紀錄同樣帶一個 TS 讀者不認得的鍵）。PR-4 不依賴 PR-2 的程式碼。

架構總圖（`docs/architecture/overview.md`）和決策清單（`docs/architecture/decisions.md`）：本單照 brief 只交 design.md。核准之後，在 PR-1 的同一個 PR 補上總圖的 `angareion` 一節，以及決策 D-30「待決＝append-only 的 decision datatype；匯總在 Angareion；後端只用 append 和依序讀回」。

---

## §20 與 App 草稿對照

來源：MyAiStorage#468 的留言「來自 MyAiEntry PM（#356 的 App 端）」所貼的 App 端欄位草稿（App 端註明草稿暫停、等本單定案）。下表的「App 草稿」欄是那則留言的摘要；差異列成 X-編號，交本人和 PMO 決定。在本人裁定之前，本設計以 Angareion 為準（P-3、R-A2）。

| X | 項目 | App 草稿 | 本設計 | 建議 |
|---|---|---|---|---|
| X-1 | 待決的標記 | 一般訊息的內文＝人看的說明＋唯一一個 JSON 區塊（`"v":1`） | front matter `datatype: decision`；內文是 YAML payload（`decision: 1`） | 本設計。要件 2 要求「只看格式就能區分」，front matter 不必掃內文 |
| X-2 | 一則幾題 | 一則 1～4 題（`questions[]`） | 一則 `ask` 一題 | 本設計。一題一則，題目 id 就是訊息 id，更新、撤回、作答都直接 `in_reply_to` 那一題；多題一起問時連下幾次 `ask` |
| X-3 | 題目 id | 訊息內的 `id`（`[A-Za-z0-9_-]+`，該則內唯一） | 出題那則訊息的 ULID | 本設計（跟 X-2 一起） |
| X-4 | 過期 | `expires_at`，到期未答＝已過期 | 沒有；放太久由提問者 `update` 或 `withdraw`（R-P3） | 待本人決定。要加的話，「過期」要用後端時刻判，不能用寄件時間（L-6）；建議 v1 不加 |
| X-5 | 取代 | `replaces`：另出一則，舊的整則變「已取代」 | `update`：同一題 id 換新版本，草稿清掉 | 本設計。題目 id 不變，討論串和 `log --thread` 不斷 |
| X-6 | 複選 | `multiple`（布林，必填） | 只有單選 | 待本人決定；建議 v1 單選，要複選時 payload 版本升到 2 |
| X-7 | 「其他」 | `allow_other`，預設 true | Q-3（不給 `choice` 時 `note` 必填），建議可以 | 同一件事，跟 Q-3 一起裁 |
| X-8 | 選項 | `id`（小寫字母，`other` 保留）、`label`、選填 `description` | `key`（大寫 A～Z）、`text` | 待本人決定；只是命名差異，建議照本設計 |
| X-9 | 必填 | `header`／`background`／`question`／`recommended`／`reason` 都選填 | `title`、`body`、`recommended` 必填；`background`、`reason` 必給但可為空字串 | 本設計。本人的要件是「背景、內文、選項、推薦選項、理由」都要有 |
| X-10 | 狀態 | 已撤回 → 已取代 → 已答 → 已過期 → 待答 | `open`／`answered`／`withdrawn` | 跟 X-4、X-5 一起定；照本設計就只有三種 |
| X-11 | 撤回的形式 | 一則首行 `# 撤回決定題：<id>`、`in_reply_to` 指向題目 | `op: withdraw` 的 decision 紀錄，`in_reply_to` 指向題目 | 本設計（跟 X-1 一起） |
| X-12 | 作答與送出的路徑 | 「不多一種送出路徑」：答案走一般送出（寄件匣＋客戶端 id 冪等）；同一人對同一題最新的一則作答就是目前答案；沒有草稿這一層 | 作答是 append 進 Angareion 的草稿（`answer`），送出是 `submit`；App 只透過 `angareion decision answer`／`submit` 寫（R-A3） | **衝突，要本人裁定**。本設計的理由：本人要求「一題一題答、按送出才一次送」，草稿若只在 App 裡，換畫面就看不到，CLI 也不能參與（§16）；冪等靠「已接受的題已結束」和 §6.4 的去重。若本人要 App 的做法，submit 可以由 App 用一般送出寫出 `datatype: decision` 的訊息，但那樣 TS 寫者要照 §4.3 組 payload，R-A3 要改 |
| X-13 | 列表從哪裡來 | 待決列表是 App 訊息庫的純函式推導（App 自己匯總） | 只有一份匯總規則，在 Angareion；App 讀 `decision list --json`（P-3、R-A2） | **衝突，要本人裁定**。兩份匯總規則（py 和 TS）遲早不一致，snapshot、去重、競態的判法都要兩邊各寫一次。若本人要 App 自己推導，至少要求 TS 實作以本設計 §6、§7 為規格，並用同一組測試資料（C-16、C-26 的案例）驗 |
| X-14 | 不合法的欄位 | 新欄位不合只忽略那個欄位；舊欄位不合，整則當一般訊息顯示 | 寫入嚴格（不合就不送）；讀取時 payload 不合法，整則紀錄不參與匯總、列進 `ignored`，但照位址遞送，在 `read`／`log` 裡仍看得到原文 | 本設計。匯總的結果不能因為某個欄位被各自忽略而在不同讀者間不同 |

App 草稿提出的四個問題，本設計的回答：

1. **有沒有獨立的類型標記；append 語意與 id 關係**：有，`datatype: decision`（§4.1）。題目／更新／撤回／作答／送出都各是一則新訊息；題目 id＝出題訊息的 id，其餘紀錄以 `ref`（和 `in_reply_to`）指向它；版本 id＝被接受的 `update` 訊息的 id（§4.3）。
2. **一次提交多題：一則還是每題一則**：每個（提問頻道, 提問者）一則，含那一組所有已答的題；超長時才分成幾則（§9.1）。冪等不靠訊息 id，靠「已接受的題已結束，再送判 `closed_ref`」（§6.2、C-11、C-14）。
3. **出題的 CLI 形狀**：`angareion decision ask --channel <ch> --body -`（YAML 從 stdin 讀），一題一個指令（§5）。
4. **過期／撤回後遲到的作答**：保留在後端、不改題目狀態，列在 `list --json` 的 `ignored`（`closed_ref`，帶 `ref` 和 `item`），App 可以據此標「題目已失效」（§5、§6.2）。過期見 X-4。
