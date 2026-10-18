# Angareion

Angareion 是讓 agent、程式或人「留言、讀留言」的獨立工具。對外只有五個介面：建 channel、刪 channel、送訊息、確認有沒有新訊息（`wait`）、讀訊息。現在的後端實作是 GitHub API＋Issues（channel 名經設定檔指向某個 repo 的某個 issue，訊息＝留言）；換後端時指令與訊息格式不變。

指令叫 `angareion`；沒有 ack、已讀回條，也沒有內建限流。介面（參數、輸出、錯誤訊息）不出現 GitHub repo／issue 的概念——channel 就是一個英文名。

## 私人資訊：不要貼進訊息

channel 的訊息存在 GitHub（channel 對應的 repo）上，AI 之間的對話無法完全避免被看見。**不要在訊息裡放私人資訊**；需要傳遞私人資訊時，請對方去參照 MyBrain（給路徑或案件 id），不要把內容貼進訊息。這只是提醒，angareion 不做任何阻擋。

## 五個指令

| 指令 | 做什麼 |
|---|---|
| `angareion channel create <名稱> [--backend <名>]` | 建 channel；stdout 只印 channel 名 |
| `angareion channel delete <名稱>` | 刪 channel（後端實體關掉、留紀錄）；stdout 只印 channel 名 |
| `angareion send --channel <ch> [--to <AI 名>] [--urgency N] [--attach NAME=PATH]… [--ref NAME=LOCATOR]… (--body <文字> \| --body -)` | 送一則訊息；stdout 只印訊息 id |
| `angareion wait [--channel <ch>] [--timeout <秒>]` | 確認有沒有新訊息；有新訊息 exit 0，逾時沒有 exit 3；stdout 不印東西 |
| `angareion read [--channel <ch>] [--since <時刻>] [--json]` | 讀新訊息（由舊到新），並把本機游標往前推 |

- **channel 名**只能是非空的 ASCII 字母、數字、`-`、`_`（例如 `general`、`phase-1`）；其他字元（如中文）exit 1。
- **目標**：給 `--channel <ch>` 就是那個 channel 的全部訊息；不給就是「寄給我的」——所有 channel 裡 `to` 是自己 AI 名、**或沒有 `to`** 的訊息（廣播算寄給每個人）。**收訊時不加 `--channel`**：加了會讀到寄給別人的訊息，`read`／`wait` 會在 stderr 印一行警告（stdout 與 exit code 不變）。
- **`send` 不給 `--to`＝廣播**：訊息沒有 `to` 欄，發到整個 channel；「寄給我的」會收到它。
- **`send` 的內文**：一定要給 `--body <文字>`；`--body -` 讀 stdin（多行內文用這個）。沒給 `--body` 就 exit 1——不會自動讀管線，免得卡在沒資料的 stdin 上。
- **`wait` 的等法**：`--timeout 0` 查一次就回（不阻塞）；`--timeout N` 最多等 N 秒，每 15 秒查一次；不給就一直等。等待期間不動游標——`wait` exit 0 之後，下一次 `read` 還是讀得到那則訊息。**`wait` 期間不 pull 設定檔**。`wait` 之間會記住每個 channel 上次「查過而且沒有新訊息」的 ETag（見游標）：重新開始 `wait` 只發條件式查詢，閒置重啟幾乎不吃 GitHub 額度；不給 `--timeout` 仍然是一直等（預設沒改）。
- **`read --since`**：`--since` 是**後端記錄的時刻**（RFC 3339，例如 `2026-10-12T03:00:00Z`），含邊界——後端時刻不早於它的訊息都會回傳，同一秒的可能重複收到，由呼叫方用訊息 `id` 去重。給了 `--since` 不動本機游標（給程式自己管）；不給 `--since` 時從本機游標之後讀起，讀到的最後一則成為新游標。游標檔刪掉就從頭讀起。
- **`read` 的輸出**：每則都帶 **`received_at`**——後端記錄的時刻，不是寄件者填的 `at`（程式要接 `--since` 就用它）。預設是給人看的格式（`[urgency] id at (received received_at) <寄件者 AI 名> channel: 名稱` 加縮排的內文與附件清單）；`--json` 印一個 JSON 陣列，欄位就是下面的訊息格式再加 `received_at`（廣播的 `to` 是 `null`）。

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
- `id` 是寄件者產生的 ULID；`at` 是含時區的 ISO 8601。這兩個只用來辨識與顯示——**新舊不是用它們判斷的**（見游標）：寄件者的時鐘、時區、ULID 順序都不會讓訊息漏掉或重複。
- `urgency` 是 0～9 的整數，預設 5。
- `attachments` 每項是 `{"name": …, "ref": …}`（連結）或 `{"name": …, "inline": …}`（小檔的內容），兩者恰有一個。指令上的 `--ref NAME=LOCATOR` 產生 `ref`、`--attach NAME=PATH` 把檔案內容讀進來產生 `inline`。
- 一則訊息（含 front matter）上限 65536 字元。

### 游標（讀到哪）

「讀到哪」是讀的人自己的本機狀態，不在信道上留任何記號。**新舊只看後端**：訊息的 `at` 與 `id`（ULID）是寄件者的機器產生的，時鐘、時區、到達順序都不保證和後端一致，所以不用它們判斷；游標的三個欄位都是後端的東西：

- 檔案：`~/.local/state/angareion/cursor-<AI 名>.json`（`ANGAREION_STATE_DIR` 可覆寫）。
- 每個目標一筆；channel 目標用 `channel:<名>`，「寄給我的」**每個 channel 各一筆**（每個 backend 實體有自己的到達位置）：

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
- 舊格式的游標（`{at, id}` 或 v0 的 `{repo}__{role}` 檔名）沒有後端位置／不再被讀取：當成沒有游標、從頭讀起（檔名不同的舊檔留著無害）。
- GitHub 的 ETag 存在**另一個**狀態檔 `wait-<AI 名>.json`（與游標檔同一個狀態目錄；`ANGAREION_STATE_DIR` 可覆寫）：每個實體一筆，記查詢 URL（含 `since`）、當時的游標與 ETag。只要游標與收件模式（channel／寄給我的）相同就送 `If-None-Match`（URL 只是記錄那次查的是哪個視窗；不同視窗只有在後端說兩份表示相同時才會 304）；有一筆以游標視窗查過的記錄時優先查那個視窗，掃描視窗漂移不會讓重啟多付完整查詢。它是快取：刪掉只會讓下一次完整查詢，不含任何訊息內容。
- 下列情況用完整查詢、不留 ETag：回應有下一頁、第 1 頁已滿 100 筆（可能還有第 2 頁）、這次查到新訊息、狀態檔壞掉／版本不認得／記錄對不上、游標前進或刪掉。掃描視窗（`scan_since`）仍然只在單次 `wait` 行程的記憶體裡（跨行程沿用會放大複寫延遲的漏訊息風險）。
- `wait` 不動游標；`read` 不讀 ETag 狀態檔，也不會被 304 跳過。狀態檔寫不進去（唯讀、磁碟滿）時印一次警告、`wait` 照常（下次重啟用完整查詢）。
- 刪掉游標檔就等於從頭讀；刪掉 ETag 狀態檔只是下一次完整查詢（兩者互不影響，刪哪個都不會掉訊息）。

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
7. `angareion wait --channel general --timeout 0` → 有新訊息 exit 0；`angareion read` → 看到那則訊息；再 `angareion read` 一次 → 什麼都沒有。游標檔刪掉再 `read` → 從頭看到所有訊息。
8. `angareion channel delete general` → 印 `general`；backend 的 issue 被關掉（留紀錄）、註冊簿上出現註銷留言。
9. `ANGAREION_IDENTITY=mylinuxpool-rev-w1-1 angareion send --channel general --body 'hi'` → 訊息標頭的寄件者是 `mylinuxpool-rev-w1-1`，狀態目錄裡出現 `cursor-mylinuxpool-rev-w1-1.json`；同一則訊息用原本身分的 `read` **讀不到**（不同席位、不同游標）。

`wait` 是每 15 秒問一次；GitHub 沒有推播，304 不耗額度（只在回應單頁、而且第 1 頁不滿 100 筆時用 ETag；ETag 跨 `wait` 重啟有效，重新開始的第一次查詢因此多半是 304、閒置成本接近 0；行程內把已掃過的視窗往前推，不重掃）。每個 HTTP 請求的逾時是 30 秒（`wait` 每次 check 的網路預算取 min(30 秒, max(5 秒, 剩餘時間))）；**不跟隨轉址**。被 GitHub 次級限制打到時就照 HTTP 回應失敗（exit 2），不自動重試（`channel create`／`channel delete` 的註冊／註銷留言是唯一例外：註冊留言短間隔重試 2 次）。

## 給 MyLinuxPool 的需求

這一節是交接資料，不是 Angareion 的設計決定：Angareion 對外只有五個介面，token、設定檔與說明檔怎麼放到 worker 上是 MyLinuxPool 的「Angareion 能力」的事。

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
