# #30 Angareion v0 的非作者審查

- 對象：分支 `angareion-v0-agy1`（impl1），`c725e15`..`2596601`（tasks 1.1～3.2）；`git archive` 解到 `/tmp/a30` 審查與突變，沒有動 impl1 的 worktree。
- 對照：`openspec/changes/angareion-v0/`（proposal、design D1～D6、specs、tasks）；v4 的決定與 `docs/review/v4-consistency.md`（sidecar-v0 worktree）。
- 審查者：架構師（Claude Sonnet 5.5）。
- 基準：副本上 `-k "angareion or skill"` 110 passed（和 impl1 回報的 angareion 110 個、全部 946 個一致）。最新 commit `5c667d2` 只是刪掉 `state.py` 一個未用的 import，不影響本審查。

## 結論

**要修再合併。** 四個指令、exit code、token 讀取與權限、`wait`＝在 `check` 上輪詢、ETag 只在記憶體、沒有 ack 與限流，都符合 spec，測試也抓得到大多數改壞（24 個有效突變，20 個轉紅）。但游標的「嚴格晚於 `(at, id)`」和設計寫的「依 id 去重」不是同一件事，會**無聲掉訊息**（H1）；另有兩個會讓 `wait` 卡住或拖過逾時的問題（M1、M2）。三項都不大，修完即可合併。

## impl1 自己判斷、和 design 不同的六點

| # | 做法 | 判斷 | 理由 |
|---|---|---|---|
| 1 | 拿掉 v1 的 `send` 去重（v0 不重試） | **可以接受** | design 與 spec 沒有重試，每次執行都產生新的 ULID，後端本來就沒辦法靠 id 去重；真正會重複的是呼叫方自己重跑 `send`，那是呼叫方的事。只要 `docs`／SKILL 不承諾冪等即可（現在沒有）。 |
| 2 | `channel create` 印 `<name> #<number>`；`wait` 的 stdout 全空、結果只看結束碼 | **可以接受** | design D1 的回傳欄就是「channel 名與編號」與「exit 0／3」；spec 要求 stdout 只放結果，`wait` 沒有結果可印。文件與 SKILL 已一致。 |
| 3 | 只有時間的游標（`--since`、沒有 id）時，同一秒的訊息視為不晚於而排除 | **可以接受，建議改成含邊界（Low）** | `--since` 是給程式自己管游標用的，程式拿不到 id，只能把上一則的 `at` 傳回來；嚴格晚於會在那一秒內有第二則時漏掉它，和 H1 同一類（掉訊息）。含邊界（`>=`）頂多讓程式多收到一則同秒的舊訊息，而每則都有 `id`，重複看得出來、掉了看不出來。若維持現在的語意，文件要寫清楚「`--since T` 不含 T 這一秒」。 |
| 4 | 設定檔的身分是群組 → `ConfigError`（exit 2）；`--to`／`--member` 給群組位址 → exit 1；送（或讀、等）不存在的 channel → exit 1 | **可以接受** | 設定錯誤是 exit 2、輸入錯誤是 exit 1，與 spec 的分類一致；群組位址的 exit 1 有 spec 的 Scenario。不存在的 channel 是設計沒寫的選擇（L8），文件已寫明。一個提醒：接收端若在寄件端建好 channel 之前就 `wait --channel X`，會立刻 exit 1 而不是等；SKILL 要教「先 `channel create`（已存在就沿用）再 `wait`」。 |
| 5 | body 用 `--body TEXT`、`--body -` 或管線 stdin；是 tty 又沒給 body → exit 1 | **可以接受（附 L5）** | design D1 就是「`--body` 或 stdin」；`--body -` 只是明寫的 stdin，不是新介面。tty 的處理對。沒有 tty、stdin 是開著卻沒資料的管線時會等不到 EOF，見 L5。 |
| 6 | 沿用 v1 的 `--attach NAME=PATH`（inline）與 `--ref NAME=LOCATOR`（ref） | **可以接受** | design 的訊息格式有 `ref` 與 `inline` 兩種附件，需要兩個選項才產生得出來；它們是 `send` 的參數，不是新的對外介面。檔案太大時以 65536 字元上限擋下並說明，沒問題。 |

另外 impl1 回報「23 個 mutation 全紅」：我用自己的一組 24 個突變，有 3 個沒紅（`check` 不看下一頁、token 順序、`Authorization` header），兩個是等價突變；它的那一組應該沒涵蓋這三處。

## 問題

### High

**H1. 游標用寄件者的 `at` 與 ULID 做「嚴格晚於」，後到但時間較早的訊息會永遠讀不到**
- 位置：`src/angareion/backend.py:68-80`（`is_message_after_cursor`）、`github.py:301-302`（`since=cursor.at`）、`github.py:390-393`（新游標＝排序後最後一則）。
- 設計（D4）寫的是：以游標的 `at` 當 `since` 去查，再**依訊息 `id` 去重**（已讀過的 id 不再回傳，同一秒的多則都要讀到）。實作改成「游標之後的才算新」，只記最後一則，等於假設訊息會按 `(at, id)` 的順序到達。這個假設在這個系統裡不成立：`at` 與 `id` 都是**寄件者本機**產生的，GitHub 是按到達順序存。
- 實測（FakeBackend，同一份比較函式；`/tmp/a30probe`）：
  | 情境 | 結果 |
  |---|---|
  | B 已讀過 `(03:00:00, …09)`；同一秒內另一個寄件者的訊息 `(03:00:00, …01)` 後到（ULID 較小） | `check` False、`read` [] —— 永遠漏掉 |
  | 寄件者的時鐘慢 2 秒：`at` 是 03:00:08，游標在 03:00:10 | 同上，漏掉 |
  | `at` 帶不同時區：游標 `…T17:00:00+09:00`（08:00Z），後到的 `…T09:00:00+00:00`（09:00Z，晚一小時） | 字串比較 `09 < 17`，漏掉 |
- 另外，`at` 只驗證「非空字串」（`messages.py` 的 `validate`），沒有要求格式，所以第三種不是人為製造的。
- 後果：多寄件者的信道（PMO、多個 worker 同時寫）會偶發地靜默掉訊息，urgency 9 的也一樣；`wait` 不會醒。
- 建議（擇一，我偏向第一個）：
  1. 游標改記 **GitHub 的留言 id（單調遞增）** 與 `created_at`；`since` 用留言的時間而不是寄件者的 `at`；去重用「已讀 id 集合」（只需保留游標時間之後的那一段）。順序以到達順序為準，不受時鐘影響。
  2. 保留現在的結構，但改成 design 寫的「`since` ＋ 已讀 id 集合」：游標存 `at` 與「`at` 之後已讀的 id 清單」；比較用 `datetime` 解析後的瞬間而不是字串；`since` 減一個安全邊際（例如 5 分鐘）以容納時鐘差。
  - spec 的「同一秒的兩則」Scenario 要補「後到的 ULID 較小」與「`at` 時區不同」兩個。

### Medium

**M1. `check` 的 ETag 只對第一頁有效；超過一頁的無關留言之後，我的訊息落在第二頁就收不到 304 以外的任何回應**
- 位置：`github.py:319-344`。
- 流程：第 1 次 `check` 取得第 1 頁與 ETag，沒有新訊息就繼續看後面的頁；之後每次只帶 `If-None-Match` 問第 1 頁。「寄給我的」是 repo 層的留言列表，游標只在**我讀到寄給我的訊息時**才前進；別人之間的留言多於 100 則（`per_page=100`）以後，之後的新留言都落在第 2 頁以後，第 1 頁內容不變 → 304 → 回 False。
- 實測（假 transport，ETag 只隨第 1 頁內容改變；**這是我對 GitHub 行為的假設，沒有連網驗證**）：120 則別人的留言、第 1 次 `check` False；我的訊息附加在第 2 頁；第 2 次 `check` 仍 False（只有一次 304 請求）。
- 建議：ETag 只在「沒有下一頁」時才用（或第 1 頁以外的頁不靠它）；或像 H1 的建議 1，在行程內把 `since` 往前推到「已經掃過的最後一則的時間」，這樣第 1 頁永遠是未掃過的那一段。需要一個測試：超過一頁、第 1 頁不變、第 2 頁多一則給我的。

**M2. `DefaultHttpTransport` 沒有逾時**
- 位置：`github.py:72-76`，`urllib.request.urlopen(req)` 沒有 `timeout`。
- 後果：半開的連線（NAT、睡眠後恢復）會讓 `wait --timeout 600` 無限期卡在一次 HTTP 上，超過使用者給的逾時，也拖住 agent 的 terminal。`--timeout 0`（不阻塞）也可能卡住。
- 建議：給固定的 socket 逾時（例如 30 秒）；逾時轉成 `BackendError`（exit 2）。補一個用假 transport 丟 `socket.timeout` 的測試。

### Low

- **L1　後端回非 JSON（例如 proxy 的登入頁）時 exit 1 而不是 2。** `resp.json()` 丟 `JSONDecodeError`（`ValueError` 的子類），被 `cli.py:354` 的 `except (MessageValidationError, ValueError)` 當成輸入錯誤。實測：HTML 本文的 200 → exit 1（stderr 「Expecting value…」）。spec：後端錯誤是 exit 2。建議在 `GitHubIssuesBackend` 內把解析失敗轉成 `BackendError`，並讓 CLI 的 `ValueError` 只接 `MessageValidationError`。
- **L2　`read` 先存游標再印出。** `client.py:75-77` 存完才回到 `cli.py:245`；stdout 管線斷掉（`| head`）就掉訊息。建議印完（flush 成功）再存，或讓 `Angareion.read` 回傳一個「提交」的 callable。
- **L3　`urllib` 的轉址會把 `Authorization` 帶到別的主機**（預設行為，Link 標頭的下一頁網址也是用同一組 header 去打，`github.py:338-339,386-388`）。GitHub 一般不轉到別的主機，所以只是防禦：下一頁網址要求以 `base_url` 開頭，轉址不跟隨或丟掉 `Authorization`。
- **L4　`_find_issue` 同名時「最舊的」勝出**（`github.py:225-232`：issues 列表新到舊，後面的覆蓋前面的）。`create_channel` 競態（兩個行程同時建同一個 channel）會產生同名 issue，之後各行程可能對到不同的。建議 `setdefault`（保留最新的），或在 create 的競態之後再查一次。每個 CLI 行程第一次找 channel 都會走完整個 issue 列表（`per_page=100` 的分頁）；channel 多了以後每次 `send` 都是 N 頁請求，v0 可以接受，寫進文件即可。
- **L5　`send` 在沒有 `--body` 又不是 tty 時讀 stdin（`cli.py:151-156`）。** agent 的子行程若 stdin 是開著但沒資料的管線，會無限期等。建議沒有 `--body` 時只在 stdin **有**資料可讀（`select` 或 `--body -` 才讀）；skill 已經教 `--body -`。
- **L6　游標檔的暫存檔名固定是 `.tmp`（`state.py:63`）**，兩個行程同時 `read` 會互相覆蓋（讀改寫也沒有鎖）。最壞是游標退回一點（重讀），不會掉訊息；用 `tempfile.NamedTemporaryFile(dir=…)` 即可。
- **L7　`at` 是無引號的 YAML 時間戳時會被 `safe_load` 轉成 `datetime`**，`validate` 以「不是字串」拒絕，那則訊息被當成不是訊息。自家的序列化有加引號所以不會發生；手寫訊息的人或別的實作會踩到。建議解析端接受 `datetime` 並轉回 ISO 字串（與 H1 一起處理）。
- **L8　`channel` 不存在回 exit 1（文件 12 行）。** 這是設計沒寫的選擇，文件已經明寫，我認為合理（輸入錯誤）；只是請 PM 確認一次，因為後端 404 與「網路不通」都是 2。

## 測試在改壞時會不會紅（突變，副本裡改，只跑 angareion 的測試）

| # | 改壞什麼 | 結果 |
|---|---|---|
| 1 | token 權限只擋 `0o007`（群組可讀放行） | 紅（0640、0660 的參數） |
| 2 | 權限檢查整個拿掉 | 紅（7 個） |
| 3 | 游標 `id >=`（重複回傳最後一則） | 紅（11 個） |
| 4 | 游標 `at >=` | 沒紅——**等價突變**（`at` 不同時 `>` 與 `>=` 一樣，相同時走 id） |
| 5 | `wait` 逾時 exit 2 | 紅 |
| 6 | channel 不存在 exit 2 | 紅 |
| 7 | 304 當成有新訊息 | 紅 |
| 8 | 不送 `If-None-Match` | 紅 |
| 9 | `check` 不看下一頁 | **沒紅（漏抓）** |
| 10 | `read` 不跟下一頁 | 紅 |
| 11 | `--since` 也存游標 | 紅 |
| 12 | `check` 存游標（wait 動游標） | 紅（5 個） |
| 13 | `mask_token` 不遮 | 紅（4 個） |
| 14 | token 順序改成設定先於 `ANGAREION_TOKEN_FILE` | **沒紅（漏抓）** |
| 15 | CLI 的 urgency 上限 10 | 沒紅——下游的 `Message.validate` 一樣擋，**等價**（行為相同，exit 1、沒送） |
| 16 | 不帶 `since` | 紅 |
| 17 | 回傳不按時間排序 | 紅 |
| 18 | 輪詢間隔 5 秒 | 紅（3 個） |
| 19 | 送訊息接受群組位址 | 紅 |
| 20 | 不送 `Authorization` | **沒紅（漏抓）** |
| 21 | 回傳的訊息不去重 | 紅 |
| 22 | 「寄給我的」不過濾收件人 | 紅 |
| 23 | 負的 `--timeout` 放行 | 紅 |
| 24 | 後端錯誤印到 stdout | 紅（2 個） |

（原本的第 23 號「游標存檔」突變寫壞了，已丟棄。）

漏抓的三項要補測試（建議）：
1. `check` 多頁：第 1 頁沒有、第 2 頁才有新訊息 → True（同時涵蓋 M1）；
2. token 順序：`ANGAREION_TOKEN_FILE` 與設定的 `token_file` 同時存在時用前者，兩者都沒有時用預設；
3. 請求帶 `Authorization: Bearer <token>` 與 `Accept`、`X-GitHub-Api-Version`（用假 transport 記下 headers）。
另外沒有任何測試涵蓋 H1 的三種情境、非 JSON 回應（L1）、transport 逾時（M2）。

## 安全

- token 的值：只在 `load_token` 回傳、`GitHubIssuesBackend.token`、`Authorization` header；錯誤與例外訊息只含路徑與狀態碼；`_request` 與 CLI 的 `except` 都過 `mask_token`；`HTTPError` 的本文沒有放進訊息。`test_token_never_*` 三個測試涵蓋輸出、例外與 log。突變 13 轉紅證明有效。
- repr：`GitHubIssuesBackend`、`Config` 沒有自訂 `__repr__`，`Config` 是 dataclass 但只含路徑，不含 token；`GitHubIssuesBackend.token` 是一般屬性，若有人 `print(vars(backend))` 會看到，現在沒有人這樣做。
- URL 與 header：`/repos/{owner}/{repo}/issues[/…]/comments`，`since` 有 `urllib.parse.quote(..., safe='')`；`channel_repo` 只檢查含 `/`（不擋 `..` 或空白，會變成請求路徑的一部分；來源是本人的設定檔，可以不處理）。`X-GitHub-Api-Version: 2022-11-28`、`Accept: application/vnd.github+json` 正確。
- 轉址與下一頁網址帶 `Authorization`：見 L3。
- token 檔：0600／0400 通過，群組或其他人有任何位元就拒絕（比 spec 嚴：0o070 也擋）；只檢查模式不檢查擁有者，擁有者不是自己時讀不到會得到 `TokenError`，可以接受。

## 文件與 skill

- `docs/angareion.md`「給 MyLinuxPool 的需求」與我寫的版本逐字一致（v4-consistency 之後 PM 放進 PM 分支的那一節）。檔案其他部分（格式、游標、設定、smoke test）與實作相符，只有一處要跟著 H1 的修法更新（77 行「新訊息的判斷是嚴格晚於 `(at, id)`」）。
- `TODO（給 PM）`那一節（opencode 的 skill 位置）可以依下面的 spike 結論補掉。
- `SKILL.md` 對 agent 夠用：四個指令、`wait --timeout` 的用法與「不要反覆問模型」、exit code、urgency 7 以上先處理、回覆時 channel 與 `--to` 怎麼填，都有。可以補兩行：(1) `wait` exit 2 時（後端或網路錯）要稍等再試，不要當成沒新訊息；(2) 自己的位址在設定檔（agent 通常不需要知道，但「不確定對方位址先問」之外，回覆對象的位址就是訊息裡 `<repo×role>` 那一段，`repo×role` 可以直接貼給 `--to`，實測 `parse_address` 接受）。

## Spike：opencode 1.18.35 讀 skill 的位置

方法：`HOME`、`XDG_CONFIG_HOME`、`XDG_DATA_HOME`、`XDG_STATE_HOME`、`XDG_CACHE_HOME` 全部指到 `/tmp` 底下的空目錄；在各候選位置放一個名字互不相同的 `SKILL.md`；跑 `opencode debug skill`（只列出載入的 skill，不呼叫模型）。完全沒有讀真實的 `~/.config/opencode`、`~/.local/share/opencode`、`~/.claude`。

| 位置 | 被讀到？ |
|---|---|
| `~/.claude/skills/<name>/SKILL.md` | **是**（`<name>` 與 frontmatter 的 `name` 一致） |
| `~/.agents/skills/<name>/SKILL.md` | 是 |
| `$XDG_CONFIG_HOME/opencode/skills/<name>/SKILL.md`（預設 `~/.config/opencode/skills/`） | 是 |
| `$XDG_CONFIG_HOME/opencode/skill/<name>/SKILL.md`（單數） | 是 |
| 專案的 `.opencode/skills/`、`.claude/skills/`、`.agents/skills/` | 是（以工作目錄為準） |
| `~/.config/opencode/skills/`（在 `XDG_CONFIG_HOME` 指到別處時） | 否——跟著 `XDG_CONFIG_HOME`，不是寫死的 `~/.config` |

- 內建的 `customize-opencode` skill 本身的說明也這樣寫（「External skills (auto-loaded): `~/.claude/skills/…`、`~/.agents/skills/…`」），並列出兩個關閉的環境變數：`OPENCODE_DISABLE_EXTERNAL_SKILLS=1`、`OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1`。實測：設任一個，放在 `~/.claude/skills/` 的 `angareion` 就不再被載入。
- 把 repo 的 `skills/angareion/SKILL.md` 原樣放到 `~/.claude/skills/angareion/SKILL.md`，opencode 載入成功（name、description、content 都正確）。

**結論**：放一份在 **`~/.claude/skills/angareion/SKILL.md`** 就同時給 Claude Code 與 opencode（opencode 會自動讀 Claude Code 的目錄）。MyLinuxPool 不需要為 opencode 另外放一份；若要明確也可再放 `~/.config/opencode/skills/angareion/SKILL.md`，兩邊同名時不衝突（沒有測重複的行為，不建議）。注意：映像或 supervisor 不要設上面兩個 `OPENCODE_DISABLE_*` 變數。`docs/angareion.md` 的 TODO 與「opencode：實作時確認後在這裡補上」可以換成這段結論。

## 建議（順序）

1. 修 H1（游標），連同 spec 的 Scenario 與 `docs/angareion.md` 77 行；這是會掉訊息的。
2. M1、M2（check 多頁與 transport 逾時）。
3. 補三個漏抓的測試（check 多頁、token 順序、Authorization header）與 H1、L1、M2 的測試。
4. L1、L2、L5 順手修；L3、L4、L6、L7 可以排後。
5. 補 `docs/angareion.md` 的 opencode 位置，並更新 `SKILL.md` 的兩行。
