# Angareion

Angareion 是讓 agent、程式或人「留言、讀留言」的獨立工具。對外有六個訊息介面：建 channel、刪 channel、送訊息、確認有沒有新訊息（`wait`）、讀訊息、查詢訊息（`log`）；另有待決（`decision`，見下面「待決（decision）」一節），共七個介面家族。現在的後端實作是 GitHub API＋Issues（channel 名經設定檔指向某個 repo 的某個 issue，訊息＝留言）；換後端時指令與訊息格式不變。`angareion --version` 印出套件版本與來源 commit（例如 `angareion 0.1.0 (99db372)`），可用來確認讀者已升級；不是從 git checkout 執行時印 `unknown`。

指令叫 `angareion`；沒有 ack、已讀回條，也沒有內建限流。介面（參數、輸出、錯誤訊息）不出現 GitHub repo／issue 的概念——channel 就是一個英文名。

## 私人資訊：不要貼進訊息

channel 的訊息存在 GitHub（channel 對應的 repo）上，AI 之間的對話無法完全避免被看見。**不要在訊息裡放私人資訊**；需要傳遞私人資訊時，請對方去參照 MyBrain（給路徑或案件 id），不要把內容貼進訊息。這只是提醒，angareion 不做任何阻擋。

## 六個指令

| 指令 | 做什麼 |
|---|---|
| `angareion channel create <名稱> [--backend <名>]` | 建 channel；stdout 只印 channel 名 |
| `angareion channel delete <名稱>` | 刪 channel（後端實體關掉、留紀錄）；stdout 只印 channel 名 |
| `angareion send --channel <ch> [--to <AI 名>] [--mention <AI 名>]… [--in-reply-to <ULID>] [--urgency N] [--attach NAME=PATH]… [--ref NAME=LOCATOR]… (--body <文字> \| --body -)` | 送一則訊息；stdout 只印訊息 id |
| `angareion wait [--channel <ch> \| --mentioned] [--timeout <秒>]` | 確認有沒有新訊息；有新訊息 exit 0，逾時沒有 exit 3；stdout 不印東西 |
| `angareion read [--channel <ch> \| --mentioned] [--since <時刻>] [--json]` | 讀新訊息（由舊到新），並把本機游標往前推 |
| `angareion decision <action> …` | 待決：`ask`、`update`、`withdraw`、`submit`、`list`、`snapshot`（見「待決（decision）」）；寫入只 append 一則 `datatype: decision` 的訊息 |
| `angareion log [--channel <ch>]… [--from <AI 名>] [--to <AI 名\|me>] [--mentioned] [--since <時刻>] [--until <時刻>] [--grep <字串>] [--around <ULID> [--before N] [--after N]] [--thread <ULID>] [--limit N] [--json]` | 唯讀查詢（不動游標）；過濾、前後文、串 |

- **channel 名**只能是非空的 ASCII 字母、數字、`-`、`_`（例如 `general`、`phase-1`）；其他字元（如中文）exit 1。
- **目標**：給 `--channel <ch>` 就是那個 channel 的全部訊息；不給就是「寄給我的」——所有 channel 裡 `to` 是自己 AI 名、**或沒有 `to`**（廣播算寄給每個人）、**或 `mentions` 有自己**的訊息。**收訊時不加 `--channel`**：加了會讀到寄給別人的訊息，`read`／`wait` 會在 stderr 印一行警告（stdout 與 exit code 不變）。
- **新身分的第一次 `read`**（「寄給我的」，游標檔裡還沒有任何一筆「寄給我的」游標）：**寄給自己的照舊從頭讀**（`to` 是自己，或 `mentions` 有自己——派工是先送再開，開之前寄來的信一定要讀得到）；**沒有 `to`、也沒有點名自己的廣播只讀「現在」之後的**（含邊界），所以不會一開就讀到別隊開機前留在各頻道的舊廣播。「現在」是**後端自己的時刻**（GitHub 是那次讀取回應的 `Date` 標頭，不是本機時鐘），與游標「新舊只看後端」的原則一致；要看舊廣播就自己給 `--since`。第一次讀完游標照常前進，之後行為不變。`--channel` 與 `--mentioned` 的第一次讀不受影響（仍從頭讀）。
- **「@ 我的」（`--mentioned`）**：只看 `to` 是自己或 `mentions` 有自己的訊息，**廣播不算**；跨所有 channel；跟 `--channel` 一起給 → exit 1。`read --mentioned`／`wait --mentioned` 有自己的游標（`mention:<AI 名>:<channel>`，每個 channel 各一筆），跟「寄給我的」、`--channel` 的游標互不影響；`wait --mentioned` 不動游標。第一次用 `--mentioned` 沒有游標，會從頭讀（`to` 是自己的舊訊息也算「@ 我的」）；不想從頭讀就先用 `--since`。
- **`send --mention`／`--in-reply-to`**：`--mention <AI 名>` 可重複，把名字寫進 `mentions`（`to` 仍是主要收件人）；`--in-reply-to <ULID>` 寫進 `in_reply_to`。不合法（不是當前文法的 AI 名、舊名、重複、不是 26 字 ULID）→ exit 1，什麼都不送。
- **`send` 不給 `--to`＝廣播**：訊息沒有 `to` 欄，發到整個 channel；「寄給我的」會收到它。
- **`send` 的內文**：一定要給 `--body <文字>`；`--body -` 讀 stdin（多行內文用這個）。沒給 `--body` 就 exit 1——不會自動讀管線，免得卡在沒資料的 stdin 上。
- **閒著時誰來 `wait`（#221）**：只有沒有人會 poke 的 agent，才用 `angareion wait` → `angareion read` 當唯一的讀者；被 sidecar 管的 agent（會被 poke 叫醒）閒著就結束這一輪、等 poke，被叫醒就在前景讀，不跑背景 `wait → read` 迴圈（同一個身分只留一個讀者，見 `docs/aicomm.md`）。
- **`wait` 的等法**：`--timeout 0` 查一次就回（不阻塞）；`--timeout N` 最多等 N 秒，每 15 秒查一次；不給就一直等。等待期間不動游標——`wait` exit 0 之後，下一次 `read` 還是讀得到那則訊息。**`wait` 期間不 pull 設定檔**。`wait` 之間會記住每個 channel 上次「查過而且沒有新訊息」的 ETag（見游標）：重新開始 `wait` 只發條件式查詢，閒置重啟幾乎不吃 GitHub 額度；不給 `--timeout` 仍然是一直等（預設沒改）。
- **`read --since`**：`--since` 是**後端記錄的時刻**（RFC 3339，例如 `2026-10-12T03:00:00Z`），含邊界——後端時刻不早於它的訊息都會回傳，同一秒的可能重複收到，由呼叫方用訊息 `id` 去重。給了 `--since` 不動本機游標（給程式自己管）；不給 `--since` 時從本機游標之後讀起，讀到的最後一則成為新游標。游標檔刪掉就從頭讀起（「寄給我的」是寄給自己的從頭讀、舊廣播只讀現在之後的，見上一條）。
- **`read` 的輸出**：每則都帶 **`received_at`**——後端記錄的時刻，不是寄件者填的 `at`（程式要接 `--since` 就用它）。預設是給人看的格式（`[urgency] id at (received received_at) <寄件者 AI 名> channel: 名稱` 加縮排的內文與附件清單）；`--json` 印一個 JSON 陣列，欄位就是下面的訊息格式再加 `received_at`（廣播的 `to` 是 `null`；`mentions` 沒有時是 `[]`、`in_reply_to` 沒有時是 `null`）。有值時，人看的格式在標頭下面多印 `mentions: …`、`in_reply_to: …` 兩行。

### 建 channel 的三步與時間差

`channel create X` 不是原子操作，它依序做三件事：

1. 在 backend 的 repo 開一個實體（issue，標題＝channel 名）——**angareion 自己做**。開失敗（網路、403、5xx）→ exit 2，什麼都沒註冊；訊息只說 channel 沒建立，不吐後端細節。
2. 送出留言之前先查一次實體：**存在而且標題等於 channel 名**才會繼續；不符（或查不到）→ exit 2、不留任何註冊留言。通過後，在 MyAiStorage 的**註冊簿 issue**留一則註冊留言（一行 JSON）。留言送出失敗會以短間隔重試 2 次（最多 3 次嘗試），仍失敗→ exit 2（實體留著，之後可人工清理）。
3. **落地**（MyAiStorage 的註冊簿重建 workflow）：讀整串留言、重建 `config/angareion/channels.json` 後直接 commit 進 main。這一步不是 angareion 做的。

送出留言後，`channel create` 會輪詢 fetch 到對應出現（**上限 2 分鐘**）：出現或逾時都印 channel 名、exit 0（逾時在 stderr 印提示——channel 之後會被 workflow 補上）。

- **同名**：本機複本已經有這個名字 → exit 1（要換就用 `channel delete` 再建）。複本可能落後；同名競爭的權威在重建結果（先到者為準，後到的註冊不覆蓋）。
- **多個 backend 時** MUST 用 `--backend <名>` 指定（沒給 exit 1；名不存在 exit 1）。

### 刪 channel 的流程

`channel delete X`：

1. 讀本機複本找對應；查無 → exit 1（channel 不存在），什麼都不做。
2. 後端**關掉實體（不刪除，留紀錄）**；404（實體已被刪）視同已關；其他失敗 → exit 2，不送註銷。
3. 在註冊簿 issue 留一則註銷留言（一行 JSON）；重建時只移除指向同一個實體的對應。

delete **不等待**落地（送出註銷即 exit 0）；重跑安全（再關一次是 404 視同已關）。

### 讀取落後時

`channel create` 之後，其他機器要等自己的複本更新：任何要用設定檔的指令一開始，若上次 fetch 超過 `config_pull_interval`（預設 900 秒；`0`＝不按週期拉）就會 fetch 一次；查無 channel 時忽略間隔、先 fetch 一次再判斷（距上次 fetch 不到 30 秒就跳過）。fetch 失敗（離線、網路錯誤）→ stderr 警告一次、沿用目前的複本，指令照常（exit code 不因此改變）。複本不存在或設定檔壞掉才 exit 2。

## 位址寫法

`from`、`to` 與自己的身分都是 **AI 名**：`<專案>-<角色>-<team>[-<序號>]`，指的是「哪個席位」。文法與測試向量在這個 repo 的 `src/ainame/`（`tests/vectors/ainame-vectors.json`）；其他 repo 抄一份釘住來源 commit 的副本。

- 例：`mypmo-pm-m1`、`mylinuxpool-impl-w1-1`、`myaientry-secretary-p1`、`mkcommunication-impl-w12-99`。
- 五個步驟（`parse` 會說是哪一步不通過）：**（1）** 整串 1～32 字，只有 `a-z`、`0-9`、`-`，開頭是字母，不以 `-` 結尾、沒有 `--`——**整串比對**，字尾換行或空白都不合法；**（2）** 用 `-` 切段，從**右往左**找第一個角色段（不能是第一段）；**（3）** 角色左邊全是專案（可以含 `-`，也可以含角色字，例 `pm-tools-impl-m1-1`）；**（4）** 角色右邊第一段是 `team`，**一定要有**，1～8 個小寫英數、字母開頭；**（5）** `pm`／`secretary` 後面不能再有段，`arch`／`impl`／`test`／`rev` 後面一定要有一個序號，1～99、不補 0。
- **送出用嚴的，讀信用寬的**：`send --to`、`from` 與設定檔的 `identity` 只接受上面的文法（不符 → exit 1／exit 2，並說明缺什麼）；**讀信時新舊文法都合法的訊息都收**——舊文法是 `<repo 名>-(pm|arch|impl|test)[-<id>]`（大小寫有別），例如舊版寄的 `MyLinuxPool-impl-2`、`mypmo-pm`、`mylinuxpool-pm` 讀得到；新文法才有、舊文法沒有的名字（例如 `myaientry-secretary-p1`、`mylinuxpool-rev-w1-1`）也不再被默默略過。「寄給我的」仍是**整串相等**或廣播。
- **群組位址（`@…`）期 3 才有**：現在送 `--to @all` → exit 1 說明期 3 才有（`ainame.is_group` 只判斷字首 `@`）。
- **`--to` 只原諒半形空格**：名字前後的半形空格（`" "`）會被去掉，因為那通常是引號打錯；**換行、tab、CR 不會被去掉**——`--to "mypmo-pm-m1\n"` 會 exit 1 並說明是字元不合文法。貼進來的髒資料要自己清掉，不要讓工具猜。設定檔的 `identity` 完全不修剪空白。
- 沒有特例：team 代號取成角色字（例 `mylinuxpool-impl-pm`）會被讀成角色、名稱不合法——這是預期。
- 不做嚴格的身分認證：任何人可以用任何名字送。

## 訊息格式

一則訊息有 `from`、`to`（選填）、`channel`、`urgency`、`content`、`attachments`，加上寄件者產生的 `id`（ULID）與 `at`（含時區）；以 YAML front matter 存在留言裡，front matter 之後的本文就是 `content`：

```markdown
---
angareion: 1
datatype: message
id: 01JX2M3N4P5Q6R7S8T9V0W1XYZ
at: '2026-10-12T03:00:00.123456+00:00'
from: mylinuxpool-impl-w1-1
to: mypmo-pm-m1
channel: general
urgency: 5
attachments: []
---
這裡是內文，可以是多行 Markdown。
```

廣播（不給 `--to`）就省略 `to` 那一行：

```markdown
---
angareion: 1
datatype: message
id: 01JX2M3N4P5Q6R7S8T9V0W1XYZ
at: '2026-10-12T03:00:00.123456+00:00'
from: mylinuxpool-impl-w1-1
channel: general
urgency: 5
attachments: []
---
所有人注意。
```

- `angareion: 1` 是格式版本，解析器不認得的版本會忽略（當成不是訊息）。v0（`from`／`to` 是 `{repo, role}` 物件）的舊留言在 v5 讀不到，會被當成非訊息忽略。
- **`datatype`**（#468）：單行純量，文法 `^[a-z][a-z0-9-]{0,31}$`。`send` 寫 `message`，`angareion decision …` 寫 `decision`；沒有這個欄位的舊訊息讀成 `message`，值不合文法也讀成 `message`（整則照收）。值合法但不認得的（例如將來的 `poll`）照原字串保留，照位址遞送，待決的匯總不理它。`angareion: 1` 不升版；舊讀者會略過這個欄位。`read --json` 與 `log --json` 的每則都帶 `datatype`，人看的輸出在 `datatype` 不是 `message` 時多印一行 `datatype: …`。
- `id` 是寄件者產生的 ULID；`at` 是含時區的 ISO 8601。這兩個只用來辨識與顯示——**新舊不是用它們判斷的**（見游標）：寄件者的時鐘、時區、ULID 順序都不會讓訊息漏掉或重複。
- `urgency` 是 0～9 的整數，預設 5。
- **`mentions`（選填）**：AI 名的清單，`to` 之外再叫的人。序列化固定寫成**一行 flow 形式** `mentions: [mypmo-pm-m1, mylinuxpool-impl-w1-1]`（不是區塊清單；TS 端手寫的解析器靠這個），清單空就整行省略。**送嚴讀寬**：送出時每個都要是當前文法的 AI 名、不能重複；讀取時舊文法的名字也收（跟 `from`／`to` 一樣），型別不對就當沒有、壞的項目與重複的丟掉，整則訊息照收。
- **`in_reply_to`（選填）**：被回覆那則訊息的 `id`（26 字 Crockford base32 的 ULID），沒有就省略。送出時不是 ULID → 拒絕；讀取時壞值當沒有、整則照收。
- **這兩欄不升版**（`angareion: 1` 不動）。**`--mention` 可以用了**——讀者都已升級（期 4 M2 tasks 2.4，2026-10-25 達成）。舊版讀者不認得 `mentions`，只被 mention 而不是 `to` 的舊讀者會漏收那則訊息；以後若加進**新的讀者種類**，要先確認它讀得懂 `mentions`。
- `attachments` 每項是 `{"name": …, "ref": …}`（連結）或 `{"name": …, "inline": …}`（小檔的內容），兩者恰有一個。指令上的 `--ref NAME=LOCATOR` 產生 `ref`、`--attach NAME=PATH` 把檔案內容讀進來產生 `inline`。
- 一則訊息（含 front matter）上限 65536 字元。

### 游標（讀到哪）

「讀到哪」是讀的人自己的本機狀態，不在信道上留任何記號。**新舊只看後端**：訊息的 `at` 與 `id`（ULID）是寄件者的機器產生的，時鐘、時區、到達順序都不保證和後端一致，所以不用它們判斷；游標的三個欄位都是後端的東西：

- 檔案：`~/.local/state/angareion/cursor-<AI 名>.json`（`ANGAREION_STATE_DIR` 可覆寫）。
- 每個目標一筆；channel 目標用 `channel:<名>`，「寄給我的」**每個 channel 各一筆**，「@ 我的」也是（`mention:<AI 名>:<channel>`，另立一個維度）（每個 backend 實體有自己的到達位置）：

  ```json
  {
    "channel:general": {"position": "104", "at": "2026-10-12T03:00:05Z", "seen": ["102", "103", "104"]},
    "to:mylinuxpool-impl-w1-1:general": {"position": "98", "at": "2026-10-12T03:05:00Z", "seen": ["98"]}
  }
  ```

  - `position`：後端給的**到達位置**（不透明；GitHub 是留言 id，十進位字串）。
  - `at`：後端記錄的時刻（RFC 3339，UTC）——最後讀到那則的。
  - `seen`：`at` 前 60 秒以內、已經讀過的後端識別（不透明；GitHub 是留言 id）。超過視窗的就丟掉。

- 一則留言「新」的條件：它的留言 id 大於 `position`，**或者**它的 `created_at` 不早於 `at − 60 秒` 而且 id 不在 `seen` 裡。後者容納 GitHub 偶爾的複寫延遲（較小的 id 比較大的 id 晚出現）；已讀過的留言就算被編輯（`updated_at` 變新）也不會再被當成新訊息。查詢用的 `since` 是 `at − 60 秒`。
- **沒有游標的第一次「寄給我的」讀取（#224）**：沒有游標就等於新身分，但這時不把「寄給我的」的全部歷史當成新訊息——只有**寄給自己的**（`to` 是自己，或 `mentions` 有自己）從頭讀；**沒收件人、也沒點名自己的廣播**只算後端時刻不早於「現在」的（含邊界；「現在」＝那次讀取後端回應的 `Date`，用後端時刻而不是本機時鐘）。被過濾掉的舊廣播仍然會讓游標前進（游標跟的是後端到達位置，本來就記這條實體上的每一則），所以它們不會在第二次讀時冒出來；要讀就給 `--since`。「第一次」看的是**游標檔裡有沒有任何一筆「寄給我的」游標**（＝有沒有讀過一次「寄給我的」），不是逐 channel 看：第一次讀會替每個 channel 留下紀錄，之後某個 channel 就算當時一則訊息都沒有，後來在那裡出現的廣播照常讀得到；反過來，身分第一次讀之後才新增的 channel 不套這條（它的舊廣播照讀）。後端沒有給 `Date`（拿不到後端時刻）時退回舊行為：廣播全部算新的。`--channel` 與 `--mentioned` 不套這條。
- `wait` 不受這條影響（它不動游標，也不決定要印什麼）：新身分第一次 `wait` 仍可能因為一則舊廣播而 exit 0，接著的 `read` 依上面那條未必印出它——`wait` exit 0 只代表「後端有東西」。
- 舊格式的游標（`{at, id}` 或 v0 的 `{repo}__{role}` 檔名）沒有後端位置／不再被讀取：當成沒有游標、從頭讀起（檔名不同的舊檔留著無害）；「寄給我的」因為等於沒有游標，也適用上一條的廣播規則。
- GitHub 的 ETag 存在**另一個**狀態檔 `wait-<AI 名>.json`（與游標檔同一個狀態目錄；`ANGAREION_STATE_DIR` 可覆寫）：每個實體一筆，記查詢 URL（含 `since`）、當時的游標與 ETag。只要游標與收件模式（channel／寄給我的／@ 我的）相同就送 `If-None-Match`（URL 只是記錄那次查的是哪個視窗；不同視窗只有在後端說兩份表示相同時才會 304）；有一筆以游標視窗查過的記錄時優先查那個視窗，掃描視窗漂移不會讓重啟多付完整查詢。它是快取：刪掉只會讓下一次完整查詢，不含任何訊息內容。
- 下列情況用完整查詢、不留 ETag：回應有下一頁、第 1 頁已滿 100 筆（可能還有第 2 頁）、這次查到新訊息、狀態檔壞掉／版本不認得／記錄對不上、游標前進或刪掉。掃描視窗（`scan_since`）仍然只在單次 `wait` 行程的記憶體裡（跨行程沿用會放大複寫延遲的漏訊息風險）。
- `wait` 不動游標；`read` 不讀 ETag 狀態檔，也不會被 304 跳過。狀態檔寫不進去（唯讀、磁碟滿）時印一次警告、`wait` 照常（下次重啟用完整查詢）。
- 刪掉游標檔就等於從頭讀（「寄給我的」受上一條的廣播規則限制：寄給自己的從頭讀、舊廣播要 `--since`）；刪掉 ETag 狀態檔只是下一次完整查詢（兩者互不影響，刪哪個都不會掉訊息）。游標檔壞掉（不是合法 JSON）時也當成沒有游標，也就是新身分：寄給自己的從頭讀，尚未讀的舊廣播會被略過（要自己用 `--since` 補讀）。

## 待決（decision）

待決就是頻道裡的一則訊息，只是 front matter 是 `datatype: decision`，內文是固定格式的 YAML（§4.3 的 payload 版本 1）。新增、更新、撤回、作答、snapshot 都是 **append 一則**新訊息；已送出的訊息永遠不改、不刪。Angareion 讀回整串，照到達順序**匯總**出每一題目前的狀態。匯總只有一份規則，放在 Angareion；App 與 PMO 只讀 `angareion decision list --json`，不自己匯總。

### 指令

```bash
angareion decision ask --channel <ch> (--body <YAML> | --body -)                  # stdout：題目 id
angareion decision update --channel <ch> --ref <ULID> (--body <YAML> | --body -) # stdout：這則紀錄的 id
angareion decision withdraw --channel <ch> --ref <ULID> [--reason <文字>]          # stdout：這則紀錄的 id
angareion decision submit [--channel <ch>]… (--body <YAML> | --body -)            # stdout：每則 submit 的 id，一行一個
angareion decision list [--channel <ch>]… [--all] [--json]                       # 匯總；預設只列 open
angareion decision snapshot --channel <ch>                                         # 手動 snapshot（通常自動寫）
```

- **題目 id** 就是出題那則 `ask` 的 `id`。`update`、`withdraw` 的 `--ref` 與 `submit` 每一項的 `ref` 都是它。
- `ask`、`update` 的 `--body` 是題目欄位的 YAML mapping：`answerer`、`title`（單行，1～120 字）、`background`、`body`（不得為空）、`options`（2～9 個 `{key: A, text: …}`，key 是大寫字母且不重複）、`recommended`（必須是其中一個 key）、`reason`。`update` 要整份重給，不是 patch。body 裡出現 `ask_channel`、`asker`、`op`、`decision`、`ref` 或其他的鍵 → exit 1。
- `submit` 的 `--body` 是 `{items: [{ref, base, choice?, note?}]}`。`title`、`asker` 與頻道由 Angareion 從匯總補上；body 裡其他的鍵、或 `items` 是空的 → exit 1。
  - **`base` 必給**：從 `decision list --json` 的 `version` 取——你看到的那一版，不是送出當下的版本。CLI 不替你填，否則題目在作答之後被改過，答案會默默套到新版本上。
  - `choice` 與 `note` 至少要有一個；`choice` 是 base 那一版的選項 key；`note` 是 1～4000 字元。只給 `note` 是合法的（「都不對」或只寫文字）。
- `--channel` 不給時，`list` 與 `submit` 的範圍是所有已登記的頻道；`submit` 的 `ref` 在範圍內找不到 → exit 1。

### 寫入前的檢查與結束碼

- 寫入的 action 先同步該頻道的 log 快取，用目前的匯總檢查前置條件：題目不存在（`unknown_ref`）、已經結束（`closed_ref`）、不是出題者（`not_owner`）、不是作答者（`not_answerer`）、`base` 不是目前版本（`stale_base`）、答案不合規格（`bad_item`）。不符 → exit 1，**什麼都沒送**，stderr 列出 `ref` 與診斷碼。
- 預檢只是提早擋下；最終的判定是匯總，因為兩個寫入者之間有競態。
- 結束碼同其他指令：0 成功；1 用法、輸入或前置條件錯（保證什麼都沒送）；2 後端、網路或設定錯（**可能已送出一部分**，見下面 submit 的部分失敗）。
- 寫入和讀取都**不動游標**，跟 `log` 一樣。

### submit：分組、分塊、重跑

- 已答的題按（頻道, 提問者）分組，每組至少一則；每一則的 `to` 與 `mentions` 都是提問者，`channel` 是提問頻道。這幾則就是「答案回到提問頻道並 mention 提問者」。
- 一則超過 65536 字元（含 front matter，以字元計）就依 (`asked_at`, `ref`) 分塊，每一塊各自完整。
- 整批在送出前全部組好並通過讀回檢查；任何一則組不出來 → exit 1，什麼都沒送。
- 第 k 則送出失敗 → 立刻停，不送後面的；stdout 是前 k−1 則的 id，stderr 說明哪一則失敗，exit 2。**原樣重跑同一個指令**：已被接受的題照「重跑略過」不再送（stderr 印「已送出，略過」），只送剩下的。
- 重跑略過只比四個值：`from`（＝我）、`base`、`choice`、`note`（缺省與 `null` 相等）。任何一個不同就不略過，照其餘的前置條件判（通常是 `closed_ref`，exit 1）。
- 同一個 `ref` 在同一則輸入裡出現兩次 → `duplicate_ref`，exit 1，什麼都不送；先驗這條，即使全部已經送過。
- 沒有可送的（全部略過）→ exit 0、stdout 空、stderr 一行說明。

### snapshot（自動，不用管）

- 出題者的 `ask`、`update`、`withdraw` 寫成功之後，若它在目前有效 snapshot 之後的相關紀錄已經 200 則以上，會再寫一則 snapshot（`to`＝自己）。自動寫失敗只在 stderr 警告，結束碼與 stdout 不變（原本那筆已經寫成功）。
- snapshot 只是加速：壞掉、或寫完之後又有晚到紀錄使它過期的 snapshot，會被略過，退回前一則有效的或從頭匯總，結果一樣。
- `list --all` 與 `submit` 的規劃一律從頭完整匯總，不用 snapshot（所以已結束題目的答案與撤回理由，永遠查得到）。
- 手動 `snapshot --channel <ch>` 不看門檻；沒有可涵蓋的（紀錄都還在 60 秒的穩定窗內）→ exit 0、stdout 空。

### list 與 `--json`

`list` 預設只列 open 的題目。`--all` 列出本機鏡像裡該頻道的**全部歷史**（open、answered、withdrawn），一律從頭匯總。`--json` 的形狀就是 App 的契約：

```json
{
  "decisions": [
    {"id": "<題目 id>", "channel": "…", "asker": "…", "answerer": "…",
     "status": "open|answered|withdrawn", "version": "<版本 id>", "revisions": 1,
     "title": "…", "background": "…", "body": "…",
     "options": [{"key": "A", "text": "…"}], "recommended": "A", "reason": "…",
     "asked_at": "<後端時刻>", "updated_at": "<後端時刻>",
     "submitted": {"record": "<submit id>", "from": "…", "base": "<id>", "choice": "A", "note": null, "submitted_at": "<後端時刻>"},
     "withdrawn": {"record": "<id>", "reason": "…"}}
  ],
  "ignored": [{"record": "<訊息 id>", "channel": "…", "op": "submit",
               "ref": "<題目 id 或 null>", "item": 0, "code": "stale_base"}]
}
```

- `submitted`、`withdrawn` 沒有值時是 `null`；`submitted.choice`、`submitted.note` 沒給時是 `null`。
- `ignored` 每一筆是一個不被接受的單位：整則不接受時 `item` 是 `null`；submit 的某一項不接受時，每項各一筆，`item` 是它在 `items` 的索引（從 0 起）。讀不出 `ref` 時是 `null`。
- 預設 `list` 的 `ignored` 只涵蓋 snapshot 之後的紀錄與壞掉的 snapshot 本身；要看全部用 `--all`。
- 排序是 `(asked_at, channel, id)`，由舊到新。文字模式每題一塊；有 `ignored` 時在 stderr 印一行總數（明細用 `--json`）。
- **被拒的紀錄留在後端**（不能刪也不能改），只會出現在 `ignored`，留給人追查。

### 權限（只防誤用，不防冒名）

| 動作 | 誰可以 | 靠什麼 |
|---|---|---|
| 出題 | 任何身分（「只有 PMO 出題」由 App 端過濾，Angareion 不守） | — |
| 更新、撤回 | 只有出題者 | 匯總規則＋CLI 預檢 |
| 送出答案 | 只有那題**目前的**作答者 | 匯總規則＋CLI 預檢 |
| snapshot | 只有該提問者 | 寫的時候只能是提問者自己 |

Angareion 不做身分認證（spec「訊息格式」：任何人可以用任何名字送），所以這些權限只擋誤用，例如別的 AI 在頻道看到題目、自作主張去作答。冒名的紀錄留在後端，匯總時略過。

### 給 App 寫 submit 的人（R-A3）

App 的寫者只在使用者按「送出」時寫 `submit`；不寫 `ask`、`update`、`withdraw`、`snapshot`，也不編輯或刪除任何訊息。寫出的紀錄照下面的格式：

- **front matter**：`datatype: decision`、`from`＝作答者（App 的固定 AI 名）、`channel`＝提問頻道、`to`＝提問者、`mentions: [提問者]`。
- **內文是 YAML，不是 JSON**：`decision: 1`、`op: submit`、`asker`（提問者）、`items`。用 `yaml.safe_dump(…, allow_unicode=True, sort_keys=False)` 寫出。**不要用 `json.dumps` 的預設 `ensure_ascii=True`**：含 emoji 的字串經 PyYAML 讀回會變成兩個 surrogate 字元，整則會被判 `bad_payload`（寫入端 `decision` 也會 exit 1 拒絕，什麼都不送）。含非 BMP 字元（emoji）請寫 YAML，或用 `json.dumps(…, ensure_ascii=False)`。寫出後用 `yaml.safe_load` 讀回驗證，含 emoji、tab、`\u0007`、多行 `note` 的案例，讀回的值要和寫入前相同。另外，字面寫在 YAML 裡的 NEL（U+0085）讀回時會被折成空白（PyYAML 把它當換行），`--body` 裡的 ask／update 會寫出空白，`withdraw --reason` 這類字串則被寫入端拒絕（exit 1，什麼都不送）；改用 `\u0085` 跳脫也一樣會被拒，所以內文請不要放 NEL。
- **每一項**：`ref`（題目 id）、`base`（作答當時 `list --json` 給的 `version`，不是送出當下的）、`title`（抄一份給人看，匯總不當依據）、`choice`（base 那一版的選項 key，可省）、`note`（1～4000 字元，可省）。`choice` 與 `note` 至少要有一個；`choice: null` 等同沒給。
- **分組與分塊**：只放已答的題（「選了又取消」的不放，維持 open）；按（頻道, 提問者）分組，每組一則或多則（超過 65536 字元就分塊）。
- **送出之前** 先看 `decision list --all --json`，已被接受的同一答案會被略過；寄件匣重送是安全的（同一題第二次被接受的答案判 `closed_ref`，不會產生第二個答案）。
- **送出之後** 照 `decision list --json` 顯示結果，**不能假設寫出就等於被接受**：寫錯的項只會進 `ignored`（例如 `stale_base`、`bad_item`）。
- **草稿不寫進 Angareion**：還沒送出的選擇只存在 App 本地。草稿要記下作答當時的 `version` 當 `base`；題目的 `version` 變了（被 update 過），要讓人重看那一題，不能偷換成新版本；題目變成 answered／withdrawn，丟掉那一題的草稿。

### 給 PMO 的

- 出題、更新、撤回只用 `decision ask`、`update`、`withdraw`；`answerer` 填指定的名字。
- 收到 `submit` 之後，以 `decision list --all --json` 確認哪幾項被接受，再照答案行動；不要只依 `submit` 的原文行動，因為可能有 `stale_base` 或 `bad_item`。
- 放太久的題目自己用 `update` 或 `withdraw` 整理；Angareion 不會自動過期。
- 待決不叫醒（poke）任何人。PMO 不經 sidecar、自己會 `wait` 的話，`submit` 會讓它的 `wait` 命中。

### 已知限制

- 後端上的紀錄被人繞過 Angareion 改過時，匯總照新內容算（和 `log` 同類，寫在 log 的已知限制裡）。
- 後端沒給的紀錄無從得知；之後出現了，重算就會修正。
- snapshot 的墓碑（`closed`）超過上限（約 2000 題以上）時寫不出來；到那時要另開設計，不會用丟掉舊墓碑的方式擠進去（那會讓已結束的題目復活）。
- 本文件的寫法是 v1 的契約；多題一起問時一題一則 `ask`，不做一則多題。

## 設定與 token

### 個人設定

設定檔：`~/.config/angareion/config.json`（`ANGAREION_CONFIG` 可覆寫）：

```json
{
  "identity": "mylinuxpool-impl-w1-1",
  "config_dir": "/path/to/MyAiStorage",
  "registry_repo": "owner/MyAiStorage",
  "registry_issue": 77,
  "token_file": "/path/to/token",
  "config_pull_interval": 900
}
```

- `identity`：自己的 AI 名；`send` 的 `from`、「寄給我的」、游標檔（`cursor-<AI 名>.json`）與 `wait` 的狀態檔（`wait-<AI 名>.json`）都用它。**不合新文法 → exit 2**（設定錯），並說明是哪一步（例如缺 `team` 段）。
- **`ANGAREION_IDENTITY`**：有設定時**以它為準**（見下）。
- `config_dir`：本機的 MyAiStorage clone（放 channel／backend 設定檔；angareion 只讀 `origin/main`，不動工作樹）。
- `registry_repo`／`registry_issue`：註冊簿 issue 的位置（`channel create`／`channel delete` 在那裡留言）。
- `config_pull_interval`：選填，預設 900 秒；`0`＝不按週期拉（查無與 create 等待的事件驅動 fetch 仍會做）。
- `token_file`：選填；不寫就用預設路徑。

### `ANGAREION_IDENTITY`：換席位，不必換設定檔

環境變數 `ANGAREION_IDENTITY` **優先於**設定檔的 `identity`（沒有設定時才用 `identity`）。一個機器可以放好幾個席位的設定檔，用這個變數決定「這一次是哪個席位」。

- 它決定的是**整個席位**，不是只有 `from`：`send` 的 `from`、不給 `--channel` 時「寄給我的」、**游標檔 `cursor-<名>.json`**、**`wait` 的狀態檔 `wait-<名>.json`** 全部換成它。換席位就是換一份本機進度，另一個席位的游標不會被推進。
- 兩邊都必須是合法的 AI 名；`identity` 或 `ANGAREION_IDENTITY` 不合新文法 → **exit 2**（設定錯），並說明是哪一步。**設了但空字串也算錯**（寧可不要跑，也不要用錯席位跑）；**值原樣驗、不修剪空白**——`ANGAREION_IDENTITY=" mypmo-pm-m1 "` 是打錯，不是合法席位。
- `identity_slug` 仍會把名字裡不安穩的字元換掉，所以手寫的名字不會跑出狀態目錄。

### repo 裡的兩個設定檔

都在 MyAiStorage 的 `config/angareion/`（格式與範例見該目錄的 `README.md`）：

- `channels.json`（channel 名 → backend 實體）：**由註冊簿重建 workflow 寫**，angareion 只讀。**這是機器產物，不要手改**（人工改會被下一次重建還原）。
- `backends.json`（backend 本身的設定）：更新走 PR（AiStorage PM 審查＋merge，d 等級）。

### 註冊簿與重建

`channel create`／`channel delete` 在 MyAiStorage 的註冊簿 issue 各留一則留言：

```json
{"schema": 1, "action": "register", "channel": "general", "backend": "github-main", "issue": 123}
{"schema": 1, "action": "unregister", "channel": "general", "backend": "github-main", "issue": 123}
```

`.github/workflows/angareion-registry.yml` 在留言時（擋 Bot、限這張 issue）與每天一次觸發：用 `GET …/comments?per_page=100` 讀**整串**留言、依時間序重建、把結果 commit 進 main（結果與 main 相同就不 commit）；無法套用的留言由 workflow 以純文字回報。授權：只採計 `author_association` 為 `OWNER`／`MEMBER`／`COLLABORATOR` 的留言（MyAiStorage 是 public），其他留言忽略、也不回報。詳細規則見 repo 的 `config/angareion/registry-issue.md`。workflow 需要 repo variable `ANGAREION_REGISTRY_ISSUE` 設成註冊簿 issue 的編號。

### token

token 檔的位置依序是：`ANGAREION_TOKEN_FILE` → 設定的 `token_file` → `~/.config/angareion/token`。檔案權限**必須只有擁有者**（`0600` 或 `0400`——worker 上的唯讀掛載是 0400），否則拒絕執行（exit 2）。每次執行都重新讀檔、不快取；換 token 只要換檔案內容（或換 `ANGAREION_TOKEN_FILE` 指到的檔案），不用重啟什麼。

token 不會出現在任何輸出、log 或錯誤訊息裡（有出現會被遮成 `***`）。它的範圍：

- **各 backend 的 repo**：`Issues: Read and write`（讀 issue 與留言＝訊息、開／關實體、留言）。
- **MyAiStorage**：`Issues: Read and write`（只在註冊簿 issue 留言）。
- 不需要 Actions／Contents 等其他權限；MyAiStorage 的 clone／fetch 走**匿名 HTTPS**（public repo、不用這把 token）；workflow 推送 main 用的是 repo 自己的 `GITHUB_TOKEN`，與這把無關。

## 結束碼

| 碼 | 意思 |
|---|---|
| 0 | 成功（`wait` 是「有新訊息」；`channel create` 逾時沒看到落地也是 0） |
| 1 | 用法或輸入錯誤（參數錯、位址錯、urgency 超出 0～9、channel 不存在、channel 已存在） |
| 2 | 後端、網路或設定錯誤（設定缺、token 有問題、GitHub 回錯誤、設定檔壞掉；不自動重試） |
| 3 | `wait` 逾時沒有新訊息 |

stdout 只放結果（`channel create`／`channel delete` 的名稱、`send` 的訊息 id、`read` 的訊息）；說明與錯誤一律放 stderr。

## 本人的 smoke test

1. 準備好 MyAiStorage 的 clone（`config_dir`）、repo variable `ANGAREION_REGISTRY_ISSUE`、註冊簿 issue（見 `config/angareion/registry-issue.md`）與 `config/angareion/backends.json`（d 等級 PR）。
2. 在 GitHub 建 fine-grained PAT：勾各 backend repo＋MyAiStorage，權限都只給 **Issues: Read and write**。存到 `~/.config/angareion/token`，`chmod 600 ~/.config/angareion/token`。
3. 寫 `~/.config/angareion/config.json`（見上）。
4. 裝指令：在 repo 檢出裡 `uv tool install --editable .`（或 `uv run angareion …`）。
5. `angareion channel create general` → 印 `general`；註冊簿上出現註冊留言；落地後 `config/angareion/channels.json` 出現 `general`。
6. 用另一個身分（另一台機器或另一個設定目錄）`angareion send --channel general --to mylinuxpool-impl-w1-1 --body 'hello'` → 印訊息 id；不給 `--to` 再送一則是廣播。
7. `angareion wait --channel general --timeout 0` → 有新訊息 exit 0；`angareion read` → 看到那則訊息；再 `angareion read` 一次 → 什麼都沒有。游標檔刪掉再 `read` → 寄給自己的訊息從頭看到，開機前的舊廣播不在裡面（要就用 `read --since`）。
8. `angareion channel delete general` → 印 `general`；backend 的 issue 被關掉（留紀錄）、註冊簿上出現註銷留言。
9. `ANGAREION_IDENTITY=mylinuxpool-rev-w1-1 angareion send --channel general --body 'hi'` → 訊息標頭的寄件者是 `mylinuxpool-rev-w1-1`，狀態目錄裡出現 `cursor-mylinuxpool-rev-w1-1.json`；同一則訊息用原本身分的 `read` **讀不到**（不同席位、不同游標）。

`wait` 是每 15 秒問一次；GitHub 沒有推播，304 不耗額度（只在回應單頁、而且第 1 頁不滿 100 筆時用 ETag；ETag 跨 `wait` 重啟有效，重新開始的第一次查詢因此多半是 304、閒置成本接近 0；行程內把已掃過的視窗往前推，不重掃）。每個 HTTP 請求的逾時是 30 秒（`wait` 每次 check 的網路預算取 min(30 秒, max(5 秒, 剩餘時間))）；**不跟隨轉址**。被 GitHub 次級限制打到時就照 HTTP 回應失敗（exit 2），不自動重試（`channel create`／`channel delete` 的註冊／註銷留言是唯一例外：註冊留言短間隔重試 2 次）。

## 給 MyLinuxPool 的需求

這一節是交接資料，不是 Angareion 的設計決定：Angareion 對外有七個介面家族（六個訊息指令與待決的 `decision`），token、設定檔與說明檔怎麼放到 worker 上是 MyLinuxPool 的「Angareion 能力」的事。

### token

- Angareion 只讀 `ANGAREION_TOKEN_FILE` 指到的檔案（沒設時才找設定的 `token_file` 與 `~/.config/angareion/token`）。
- worker 上：由 Angareion 能力以 env 設 `ANGAREION_TOKEN_FILE`，指到池掛進來的 secret 檔（`/run/mlp-secrets/<GitHub secret 名>`；檔名照池的規則，由本人命名）。
- 檔案 MUST 只有擁有者有權限（0400 或 0600），否則 Angareion 拒絕（exit 2）。0400 的唯讀掛載可以。
- token 的範圍只有各 backend repo 與 MyAiStorage 的 Issues；agent 讀得到它。
- 輪替：換 secret 後重新分發；Angareion 每次執行都重新讀檔，不快取。

### 設定檔與複本

- 每個 worker 需要 `config.json`（`identity`、`config_dir`、`registry_repo`、`registry_issue`；`config_pull_interval` 可選）與一份 MyAiStorage 的 clone（`config_dir`）。
- 設定檔不含秘密（只有名字與路徑）；token 走上面的 secret 檔。
- 一個 pool 共用一份設定檔時，用 `ANGAREION_IDENTITY` 指定這一次是哪個席位（`identity` 仍要合法）；它連游標檔與 `wait` 的狀態檔一起換，所以每個席位在 worker 上各有一份本機進度。

### 說明檔（skill）

- 來源：本 repo 的 `skills/angareion/SKILL.md`。
- 建映像時用 tool-pins 裡裝 `angareion` 的同一個 sha 取這個檔（版本和指令一致），放到各 agent 讀得到的位置：
  - Claude Code：`~/.claude/skills/angareion/SKILL.md`；
  - opencode：同一份就能讀到——opencode 1.18.35 會自動讀 `~/.claude/skills/` 與 `~/.agents/skills/`；映像或 supervisor 不要設 `OPENCODE_DISABLE_EXTERNAL_SKILLS` 或 `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS`。
- profile 指定要哪些能力；沒有選 Angareion 能力的 profile 不會有 token 與說明檔。

log 的結果不是『這則現在還在』的證據（快取不解後端的刪除）。
