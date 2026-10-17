# 調查報告：#85「agora 接續的 session 在 opencode 用不了免費模型」

- **調查者**：agy（opencode 免費模型隊員）
- **日期**：2026-10-17（第二階段：根因追查，同日追加「根因」章節）
- **對象**：`agora continue session <claude-session> --agent opencode` 產生的 opencode session
- **環境**：macOS / opencode v2.0.30 / Python 3.12；agora 程式碼 = worktree `agora-free-85`（`src/` 未修改）
- **第二階段結論**：**不是 agora 產出的任何欄位造成的**。在「匯入 0 則訊息」的 session 上，
  opencode 送出的 request 與 opencode 自開 session 的第一輪**逐欄位、逐位元組相同**（body 完全相同、
  header 只差 4 個 session id），Zen 仍然 403。依界線**不再嘗試繞過**，詳見〈根因〉章節。
- **隔離與憑證**：agora 的 `AGORA_CONFIG` / `AGORA_CACHE_DIR` / `AGORA_STATE_DIR` 全部指到一個隔離目錄，
  `AGORA_RCLONE` 指向 repo 內的 `tests/fakes/fake_rclone.py`（假 remote = 本機資料夾），
  `AGORA_FOLDER_NAME=agora-test`，Claude 來源是自造的假 jsonl。
  沒有讀取、匯入任何真的 session 或 agora 的真實設定。**全程不記錄任何憑證**：
  認證 header 的值、長度、雜湊都沒有寫下來，本報告只留假資料與結論。

---

## 結論（TL;DR）

1. **重建出來的 session，assistant 列的 model 欄位是空的**：`{"id": "", "providerID": "opencode", "variant": "default"}`。
   這是 `src/agora/agents/opencode.py:1061`（`native()`）寫死的常數，`info.model` 也沒有。
2. **接續時實際用哪個模型，是 opencode 自己決定的，不是 agora 決定的**：
   - `opencode run --session <agora 重建的 session>`（不帶 `-m`）→ 該輪 assistant 列記的是
     **`opencode/deepseek-v4.1-flash-free`**，agent `build`。
   - TUI（`opencode --session <id>`，也就是 agora 真正啟動的那條路）狀態列顯示 **`Space Bunny Free`**。
   - 兩者都是 Zen（`opencode` provider）的**免費**模型。agora 沒有任何機制指定模型
     （`AGORA_OPENCODE_MODEL` 只用在 `summarize()`，`opencode.py:1355`）。
   - **模型選哪一個都不影響 403**：兩個免費模型各指定一次都是一樣的結果，詳見〈根因〉。
3. **大 context 不是原因，也不是速率限制**：免費模型在**第一個 request** 就被擋，
   錯誤是 **HTTP 403 `FreeTierError`**（不是 429、不是 context overflow）：

   ```
   OpenCode's free tier can only be used from within OpenCode. Download it at https://opencode.ai and keep building.
   ```

   對話只有 8 則／約 2K 字元也一樣被擋；100K、200K 字元的假對話一樣被擋，錯誤訊息一模一樣。
4. **真正的機制（實測二分）**：凡是由 `opencode session import` 產生的 session，
   不論內容多大、model 欄位填什麼、session id 是什麼形狀，
   在 `opencode run` 與 TUI 都**一律**吃這個 403；同一台機器、同一個 provider、同一個模型，
   opencode **自己開出來**的 session（以及它的 fork）就完全正常。
   → 這是 opencode／Zen 對「匯入來的對話」的限制，agora 的 `continue` 目前**每一條路都會踩到**。
5. **繞不過**：`-m` 沒用（指定哪個免費模型都是 403）；先摘要會由 opencode 自己開出一條**可用**的
   session（實測可答），但之後再走一次 `agora continue`（也就是 import）仍然 403。
   → 只有「opencode 自己開的 session」能用。

---

## Q1：重建出來的 session 的 model 欄位是什麼？接續時實際用哪個模型？

### 程式面（`src/agora/agents/opencode.py`）

| 位置 | 寫出來的東西 |
|---|---|
| `native():1061` | assistant 列 `"model": {"id": "", "providerID": "opencode", "variant": "default"}` |
| `_session_info():315` | `info` **不含** `model`（只有 `id/projectID/cost/tokens/time/title/location`） |
| `_append_native_v2():1184` | 同 agent fork 時會抄來源 session 的 model；跨 agent（Claude→opencode）走 `native()`，所以是空字串 |

也就是說：**跨 agent 接續（`native()`）產生的 session 完全沒有「用過哪個模型」這件事**。
opencode 只能退回自己的預設；`_last_model()`（`:720`）讀這種 session 也只會讀到空字串。

### 實測：`agora continue` 產生的 session（隔離環境，headless 模式）

（session id 與 message id 都是測試時自建的，這裡用佔位符表示）

```
$ opencode session export <ses_<測試 session>> --standalone
info keys: ['cost','id','location','projectID','time','title','tokens']
info.model: null
user      msg_<第 1 則>  model=null
assistant msg_<第 2 則>  model={"id": "", "providerID": "opencode", "variant": "default"}  agent=build
assistant msg_<第 3 則>  model={"id": "", "providerID": "opencode", "variant": "default"}  agent=build
```

### 實測：不帶 `-m` 接續，opencode 記了哪個模型

```
$ opencode run --standalone --session <ses_<測試 session>> --format json "請只回答兩個字：測試"
# 該輪 assistant 列（讀自 opencode DB）：
{"id": "deepseek-v4.1-flash-free", "providerID": "opencode", "variant": "default"}   agent="build"
```

TUI（agora 實際啟動的那條：`opencode --session <id>`）狀態列：`Build · Space Bunny Free`。

> 兩個免費模型都不是 agora 選的；`agora` 對「接續用哪個模型」沒有任何輸入。

---

## Q2：大 context 接續時，免費模型是什麼錯？

**錯誤是 Zen 免費層的 403，不是 context 超限、也不是速率限制。**

完整錯誤（`opencode run --format json` 的 error event；不含任何憑證）：

```json
{"type":"error","sessionID":"ses_<測試 session>","error":{
  "type":"provider.auth",
  "message":"OpenCode's free tier can only be used from within OpenCode. Download it at https://opencode.ai and keep building.",
  "status":403,
  "response":{"body":"{\"type\":\"error\",\"error\":{\"type\":\"FreeTierError\",\"message\":\"OpenCode's free tier can only be used from within OpenCode. Download it at https://opencode.ai and keep building.\"}}"}}}
```

`--print-logs --log-level trace` 的 server log：

```
level=ERROR message="Failed to drain Session" cause="AI.Error: OpenCode's free tier can only be used from within OpenCode. …"
```

TUI 裡也是同一句（畫面：`…de's free tier can only be used from within OpenCode. / Download it at https://opencode.ai …`）。

### 對照組（都在同一台機器、同一個 provider、同一個模型 `opencode/space-bunny-free`、同一分鐘內）

| 對話 | session 來源 | 結果 |
|---|---|---|
| 約 2K 字元 / 8 則 | `agora continue`（import） | ❌ 403 FreeTierError |
| 約 100K 字元 / 約 7.3 萬 token | `agora continue`（import） | ❌ 403 FreeTierError（`space-bunny-free` 與 `deepseek-v4.1-flash-free` 兩個都試） |
| 約 200K 字元 / 約 14.6 萬 token | `agora continue`（import） | ❌ 403 FreeTierError |
| 只有 1 則 user 列 | 手自組 payload → `session import` | ❌ 403 FreeTierError |
| opencode **自己**開的新 session（空目錄） | `opencode run` | ✅ 正常回答 |
| 同一個 session 的第二輪 | `opencode run --session` | ✅ 正常回答 |
| opencode 自己開的 session 的 **fork** | `opencode run --fork` | ✅ 正常回答 |
| opencode 自己匯出、**再 import 回來** | `session import` | ❌ 403 FreeTierError |
| import 出來的 session 的 **fork** | `opencode run --fork` | ❌ 403 FreeTierError |
| `agora` 的 `summarize()`（`opencode run` 自開 session） | opencode 自建 | ✅ 正常回答 |

→ 與對話大小、model 欄位、session id 形狀都無關；**與「這個 session 是不是 opencode 自己開的」有關**，
而且 fork 會繼承這個性質。

### 二分過程（每一步都實測過）

1. `projectID` 清空 → 仍 403；`outcome` 補上 → 仍 403。
2. session id 換成六種形狀：`ses_` + 24 個小寫 hex（＝opencode 原生形狀）、+ 16 個小寫 hex、
   + 32 個小寫 hex、+ 24 個 base62 混合大小寫、+ 24 個純數字、+ 16 個大寫 base62
   （＝ agora 現在寫的形狀）→ 六種全部仍 403。
3. assistant 列的 `model.id` 從空字串改成 `space-bunny-free` → 仍 403。
4. 把 opencode 自己成功過的 session 原封不動 export 出來（訊息內容、id 以外全部相同）再 import → 仍 403。
5. fork 測試：fork「自己開的」成功；fork「import 來的」失敗 → 性質隨對話繼承，不隨 id 走。
6. DB 逐欄比對（`session_v2` 全部欄位、關聯表 `session_message` / `instruction_state`）：
   成功與失敗的 session 在 DB 裡**沒有可分辨的欄位**（只有 `path`、`title`、`model.variant` 這類無關差異）。
   → 判別依據不在 agora 或 opencode 存下來的東西裡，Zen 是從 request 本身判的；
   而 request 裡與 session 綁定的只有 `x-opencode-session-id` / `x-opencode-session`(affinity) /
   `x-opencode-project` / `x-opencode-client` 這組 header（已從 opencode 2.0.30 的 bundle 讀出來），
   這次沒有再往底層追。

### 與本人回報的差異

本人說的是「Space Bunny **連續限流**」。我這邊量到的是**第一次請求就 403**，
兩者不是同一個錯誤。限流（429）我無法在這台機器上重現（沒有把免費額度用光），
但兩件事可以同時成立：Zen 免費層對「匯入來的對話」本來就 403，
而在自己開的 session 上確實可能另外遇到額度／限流。
這一點報告裡標為**未實測**。

---

## Q3：三個免費模型的 context 上限

來源：opencode 自己的 models.dev 快取（`models.json`）＋ 當日 `https://models.dev/api.json`。
`opencode models --verbose` 在 2.0.30 **不存在**（`Unrecognized flag: --verbose`），所以改用這兩份。

| 模型（`opencode models` 裡的名稱） | context | input | output | 本機可選 |
|---|---|---|---|---|
| `opencode/space-bunny-free` | 1,048,576 | 524,288 | 524,288 | ✅ |
| `opencode/muse-spark-1.3-contributor-free` | 1,048,576 | — | 131,072 | ❌ models.dev 有，但這台機器的 `opencode models` 沒列 |
| `opencode/deepseek-v4.1-flash-free` | 200,000 | — | 32,000 | ✅ |

對照（同一份快取裡 Zen 的付費版）：`space-bunny` 1M、(`input` 524,288)、
`muse-spark-1.3-contributor` 1M/131,072、`deepseek-v4.1-flash` 1,000,000/384,000。
免費版 `deepseek-v4.1-flash-free` 的視窗只有 **200K**，是三個裡最小的；
若哪一天 403 消失，約 14.6 萬 token 的對話加上系統提示與工具（約 9K）
就會逼近它的視窗上限，這是第二顆地雷。

token 換算（實測）：70,000 個中文字 ≈ 51,000 token（該輪 `input` 記 59,897，扣掉系統提示與工具約 8.7K）
→ 約 **0.73 token／中文字**。所以本報告的「100K 字元」≈ 7.3 萬 token、「200K 字元」≈ 14.6 萬 token。

---

## Q4：`-m` 或先摘要能不能繞過？

| 做法 | 結果 |
|---|---|
| `-m opencode/space-bunny-free` | ❌ 仍 403 |
| `-m opencode/deepseek-v4.1-flash-free` | ❌ 仍 403 |
| 不帶 `-m`（用 opencode 自己的預設） | ❌ 仍 403 |
| 對話縮到很小（1 則訊息） | ❌ 仍 403 |
| `agora merge`／`summarize()`（opencode 自己開 session） | ✅ 可以答（實測 `AGORA_OPENCODE_MODEL=opencode/space-bunny-free` 的 summarize 正常回覆，且沒留下 pending/material） |
| 摘要完再 `continue` | ❌ 又回到 import → 403 |
| `continue --fork` | ❌ 仍 403（fork 繼承同一個性質） |
| 同一個 session 開兩輪以上 | ❌ 每輪都 403 |

**結論：`-m` 與縮 context 都不管用；唯一確定能跑的是「讓 opencode 自己開 session」的路徑**，
而 agora 的 `continue` 目前只有 import 一條路。

---

## 修法選項（給 PM 裁）

### 選項 A：先摘要再接續（agora 端可做，改動小）
`continue` 跨 agent 時不要把整段對話 import 進去，改成：
1. 用 `summarize()`（opencode 自開 session，免費模型可跑，已實測）產出摘要；
2. 用 `opencode run`（**不帶 `--session`**）開一個新 session，把摘要當第一句餵進去；
3. 之後 `collect()` 記這個 session 的 id。
- 優點：不碰 opencode 的匯入路徑，馬上能用免費模型。
- 缺點：歷史變成摘要（工具呼叫全失，這正是 design 5.4 想避免的）；對話一大就要先摘要，摘要本身要花額度。

### 選項 B：把 import 出來的 session 當唯讀，模型換別的 provider
- 實測無法驗證（這份調查只用三個免費模型，不能拿付費模型去測 Zen 付費層是否也吃 403）。
- 若 Zen 的 403 只針對免費層，這就是最省事的解：文件寫清楚「Zen 免費模型不能接續」。

### 選項 C：向 opencode／Zen 回報這個 import 限制
- 證據已經夠硬（自開 session 正常、匯入必 403、fork 繼承）。
- 這是上游行為，agora 端繞不繞得掉要看對方怎麼答。

### 選項 D：`continue` 之前先做健康檢查
- `start_native()` import 完之後，用一次免費模型的 `opencode run`（或摘要）探一下，
- 拿不到就立刻用選項 A 的路徑，並在 stderr 講清楚「Zen 免費層不收匯入的對話，已改用摘要接續」。
- 優點：使用者不會在 TUI 裡撞到看不懂的 403。

### 順帶要修的小洞（與本 issue 同一條路）
`native()` 寫空 model（`opencode.py:1061`、`_append_native_v2():1184` 的 fallback），
會讓 `_last_model()` 讀到空字串、`collect()` 的 `generated.by` 沒有模型名。
即使 403 解決了，接續後的 session 也「不知道自己用過哪個模型」。
`_role_fields_v2()` 抄模型的那段（`:573`）可以借用：至少抄一個 `AGORA_OPENCODE_MODEL` 或 opencode 的預設。

---

## 實測步驟（可重跑；腳本都放在隔離目錄裡，沒有進 repo）

```sh
# 0. 隔離環境：agora 的 config/cache/state 指到隔離目錄、假 rclone、假 claude
sh <隔離目錄>/setup.sh && . <隔離目錄>/env.sh

# 1. 造假 claude session（約 N 字元）→ 匯入 agora
python3 <隔離目錄>/bigsession.py 100000 k100      # 約 7.3 萬 token
python3 <隔離目錄>/bigsession.py 200000 k200      # 約 14.6 萬 token

# 2. 續：免費模型跑一輪，記錯誤
python3 <隔離目錄>/tryrun.py <ses_<測試 session>> opencode/space-bunny-free k100-bunny

# 3. 對照組：opencode 自己開的 session（同一個模型、同一分鐘）
cd <隔離目錄>/empty && opencode run --standalone -m opencode/space-bunny-free \
  --format json "請只回答兩個字：測試"

# 4. 二分：把 opencode 自己成功的匯出再 import 回去 → 403
python3 <隔離目錄>/import_with_id.py ses_<自訂的 24 位小寫 hex>
```

其他事實：
- `agora continue --headless --headless-port` 會在 stdout 印 opencode server 的密碼，
  自動化跑 headless 的時候要記得不要把它寫進 log（本報告不含該值）。
- 三個階段合計 50 個測試用 opencode session，都用 `opencode session delete <id>`
  一個一個刪完；DB 裡以測試目錄為名的 session 數已為 0。

---

# 根因（第二階段，2026-10-17 追加）

## 一句話

**Zen 的免費層拒絕的是「不是 opencode 自己開的那個 session」；這件事和 agora 的 payload 沒有任何關係。**
在「匯入 0 則訊息」的 session 上，opencode 送出的 request 與 opencode 自開 session 的第一輪
**逐欄位、逐位元組相同**（body 完全相同、header 只差 4 個 session id 值、認證 header 也相同），
Zen 對前者 403、對後者正常回答。依界線（不得偽造 header、不繞過 provider 刻意設的限制）**到此為止，不繞**。

## 怎麼做的（觀察手段）

Zen 是 OpenAI 相容的。我用 `OPENCODE_CONFIG_CONTENT` 把 **provider `opencode` 的 `baseURL` 指到本機**，
在本機起一個假 endpoint 記下 opencode 實際送出的 method／path／header／body，
再回一個最小的 SSE 答案讓 opencode 能跑完。
這樣看到的就是「opencode 真的會送到 Zen 的那份 request」，沒有任何偽造，也沒有把任何東西轉送到 Zen。
**全程不記錄任何憑證**：認證 header 的值、長度、雜湊都沒有落盤、沒有進這份報告。

## 關鍵實驗：匯入「0 則訊息」的 session

`opencode session import` 接受 `messages: []` 的 payload，匯入後的 session 在 DB 裡也是 0 則訊息。
對它下第一個問題，送出的 request 是：

```
request messages: [('system', 'You are an AI …'), ('user', '請只回答兩個字：測試')]
session headers:  x-opencode-client=cli
                  x-opencode-org-id=<自開與匯入兩次相同>
                  x-opencode-project=<自開與匯入兩次相同>
                  x-opencode-session=ses_<匯入的 id>
                  x-opencode-session-id=同一個
                  x-session-affinity=同一個
                  x-session-id=同一個
真 Zen:  answered=False  err=OpenCode's free tier can only be used from within OpenCode…
```

同一個 project、同一個 client、認證也相同，而 **opencode 自開的新 session：
`answered=True`**。兩者唯一差別是 session id 的值。

## 逐欄位 diff（自開第一輪 vs 匯入 0 訊息第一輪）

用同一個假 endpoint 依次跑兩次，把兩次 request 逐欄位比對（`b3`／`traceparent`／`Content-Length`
這類每次都不一樣的追蹤欄位排除）：

```
=== request 差異（自開第一輪 vs 匯入 0 訊息第一輪）===
  header 不同的欄位:
    x-opencode-session:     ses_<自開的 id> / ses_<匯入的 id>
    x-opencode-session-id:  ses_<自開的 id> / ses_<匯入的 id>
    x-session-affinity:     ses_<自開的 id> / ses_<匯入的 id>
    x-session-id:           ses_<自開的 id> / ses_<匯入的 id>
  body 相同: True
  （User-Agent 與認證 header 也相同；憑證本身沒有記錄）

=== 對真 Zen（依序，同一分鐘）===
  自開新 session                answered=True
  匯入 0 訊息                    answered=False  err=…free tier can only be used from within OpenCode.
  自開新 session（再來一次）      answered=True
```

中間那一次失敗之後，自開的 session 馬上又正常——所以不是額度、不是速率限制、不是「第一個請求特殊待遇」。

## 排除掉的每一個候選（都實測過）

| 候選假設 | 測試 | 結果 |
|---|---|---|
| 對話內容／tool 呼叫被 Zen 拒收 | 匯入 0 則訊息的 session | ❌ 仍 403（body 與自開完全相同） |
| 對話太大（context） | 約 2K 字元到約 200K 字元的匯入 session（Q2 表那一列） | ❌ 全部第一個請求就 403 |
| assistant 列的 `model.id` 是空字串 | 把它改成 `space-bunny-free` | ❌ 仍 403 |
| `info.model` 缺 | 補上 | ❌ 仍 403 |
| `projectID` 空 | 補成真的 project id | ❌ 仍 403 |
| `outcome` / `time.idle` 缺 | 補上 | ❌ 仍 403 |
| session id 的**形狀**（大小寫、長度、字元集） | 與上面二分過程同一組六種：24 個小寫 hex（＝opencode 原生形狀）、16 個小寫 hex、32 個小寫 hex、24 個 base62 混合大小寫、24 個純數字、16 個大寫 base62 | ❌ 全部 403 |
| session id 的**內容**（與 opencode 原生隨機 id 同形狀） | 自訂一個 24 位小寫 hex 的字串 | ❌ 403 |
| 工作目錄不同 | 匯入到與自開 session **同一個** project | ❌ 仍 403 |
| provider／model 不同 | `space-bunny-free` 與 `deepseek-v4.1-flash-free` | ❌ 兩個都 403 |
| `agora continue` 特有的東西 | 用 agora 自己的 `native()`＋`reidentify` 走 `start_native` | ❌ 403（與自組 payload 相同） |

## 那到底是什麼在分辨？

從客戶端能觀察到的範圍內：**兩個 request 的差別只剩 session id 的值**。
但「值」不是形狀（24 個小寫 hex 也被拒），所以 Zen 不可能是靠正則判斷形狀；
它必須是**伺服器端有狀態**——最自然的解釋是 Zen 對「對話 id（就是 `x-session-affinity`）」
有自己的登錄，只有 opencode 真正開出來並送過的對話才在名單裡。

一個佐證：**fork 會沿用父 session 的對話 id**。實測四個情境的 header：

| 情境 | `x-opencode-session-id` | `x-session-affinity` / `x-session-id` / `x-opencode-session` |
|---|---|---|
| 自開 session 第二輪 | 自己 | 自己 |
| fork 自開的 session | fork 自己 | **父 session** |
| 匯入的 session 第一輪 | 自己 | 自己 |
| fork 匯入的 session | fork 自己 | **父（匯入的那個）** |

所以「fork 自開的」能用（沿用父 id，父 id 在 Zen 那邊是登錄過的），
「fork 匯入的」不能用（沿用父 id，那個 id 從沒被 Zen 服務過）——完全一致。

**我沒有辦法證明伺服器端那一側是怎麼記的**（那要存取 Zen 的後端，而這條路正當性我守住了：
不偽造 header、不改 client 身分、不繞）。所以嚴謹的說法是：

> 觸發條件 = 「這個 session 不是 opencode 自己開的」。
> 從 agora 這邊看，這個條件**沒有任何 payload 欄位可以改變**——
> 連「匯入 0 則訊息、body 與自開 session 逐位元組相同」都被拒。
> 唯一能改變它的，是「不要用 `opencode session import` 造這個 session」。

## agora 端能不能修？

| 做法 | 可行嗎 | 證據 |
|---|---|---|
| 改 `native()` 產出的欄位（id 形狀、model、time、project、outcome…） | **不可行** | 逐欄位二分全部無效；0 訊息匯入已經證明 payload 完全不影響 |
| 用 `-m` 指定免費模型 | 不可行 | 指定了仍 403 |
| 縮 context／先摘要再 import | 不可行 | 0 訊息也 403 |
| `continue --fork` | 不可行 | fork 沿用父 id，父 id 沒登錄過 |
| **不要 import：讓 opencode 自己開 session**（`summarize()` 用 `opencode run` 開新 session） | **可行** | 實測 `AGORA_OPENCODE_MODEL=opencode/space-bunny-free` 的 `summarize()` 正常回答，且沒留下 pending／material |
| 匯入後**先跑一個自己的 session**，再把對話灌進去 | 不可行 | opencode 沒有「append 歷史而不呼叫模型」的 API（`session` 子指令只有 list／delete／export／import；`opencode api` 也沒有可用的 append 端點） |

所以修法只能落在產品層（見下），不是 bug fix。

## 建議（給 PM 裁）

1. **跨 agent 接續時不要走 `session import`**：改成
   「先 `summarize()`（opencode 自開 session，免費模型可用）→ 再用 `opencode run` 開一個新 session，
   把摘要當第一句餵進去 → 之後 `collect()` 記這個 session」。
   代價：歷史變成摘要，工具呼叫全失（design 5.4 原本想避免的）。
2. **同 agent 接續（opencode → opencode）也會中招**（已實測）：`continue` 走的是 `raw = own` ＋
   `session import`（見 `cli.py:1625` 與 `opencode.py:1481`），所以把一個 opencode session
   匯入成 agora Session 再接續，產生的 session 一樣 403：

   ```
   1) opencode 自開一個 session                                → 免費模型正常回答
   2) agora import session --agent opencode --external-session-id <上面那個>
   3) agora continue session <那個 agora id> --agent opencode
   4) 對 continue 產生的 opencode session 打 opencode/space-bunny-free  → answered=False
                                                                          err=…free tier can only be used…
   ```

   也就是說**不只是跨 agent，連「同一個 session 接續自己」都會中招**。第一階段沒講清楚，這裡補上。
3. **健康檢查**：import 之後立刻用一次免費模型探一下，拿不到就在 stderr 講清楚
   「Zen 免費層不收匯入的對話，已改用摘要接續」，不要讓使用者在 TUI 裡撞 403。
4. **回報上游**：證據夠硬（自開正常、匯入必 403、fork 繼承），這是 opencode／Zen 的行為。

## 附帶發現（與本 issue 同一條路的小洞）

- fork 的 header 分工值得記一筆：`x-opencode-session-id` 是 fork 自己的 id，
  但 `x-session-affinity` / `x-session-id` / `x-opencode-session` 全部是**父 session 的 id**。
  如果日後要寫測試或做 cache 判斷，這三個欄位不是同一件事。
- `native()` 寫空 model（`opencode.py:1061`、`_append_native_v2():1184` 的 fallback）
  仍是個洞：即使 403 解決了，這種 session 也「不知道自己用過哪個模型」，
  `_last_model()` 讀到空字串、`collect()` 的 `generated.by` 沒有模型名。
  可以考慮抄 `AGORA_OPENCODE_MODEL`，或至少抄來源 session 的模型——但這是另一個工單。