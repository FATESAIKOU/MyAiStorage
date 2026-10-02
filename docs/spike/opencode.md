# Spike：opencode（V1、V3、V5）

2026-10-02，impl1。opencode **1.18.34**（macOS arm64）。模型只用
`opencode/space-bunny-free`。

## 怎麼測（先讀這段，因為它決定了下面每個結論能不能重跑）

opencode 的 session 屬於「專案目錄」，在沒有 commit 的資料夾裡跑任何 opencode
指令都可能碰到使用者的全域 session。所以測試全程都在自己蓋的資料夾裡：

```
/tmp/agora-spike-impl1/          ← 測試的家目錄（結束後可整個刪）
  proj/    git init + commit（V1 主線）
  proj2/   git init + commit（V5 的「匯入到別的專案」、V3 的注入）
  proj3/   git init + commit（V5 的「在不對的目錄繼續」）
  plain/   沒有 git（V5 的 projectID 退化）
```

測試對話是自編的短對話（主題：「把 CSV 轉成 Markdown 表格」，三輪），**沒有讀過
任何真實 Session 或 MyBrain 內容**。測完 9 個自建 session 已逐一用
`opencode session delete <id>` 刪掉（不用批次刪）。

腳本：

| 檔案 | 用途 |
|---|---|
| `spike/opencode/reidentify.py` | 把 export JSON 的 session／message／part id 重編成新 session 的 |
| `spike/opencode/to_reading.py` | 把 export JSON 轉成帶 header 的閱讀版 Markdown |
| `spike/drive/drive_probe.sh` | Drive 那一半（寫在 `docs/spike/drive.md`） |

---

## V1：`opencode import` 一份改過 id 的 export，`-s` 能不能接著做

**結論：可行。** 三種 id 全換新之後 import，新 session 可以直接用
`opencode run -s <新id>` 接著做，模型看得到前面三輪；繼續之後再 export，原本那
三輪**一個位元組都沒變**，新問答接在後面。

### 實際跑過的指令

```bash
# 1. 自編三輪對話（第一輪順便把 session id 從 JSON 事件裡抓出來）
cd /tmp/agora-spike-impl1/proj
opencode run -m opencode/space-bunny-free --format json --title spike-impl1-csv \
  "把 CSV 轉成 Markdown 表格，先列三個步驟就好，不要真的動手做。"
#    → jq 'select(.type=="text")|.sessionID' 得到 ses_f07fc798…（3 次呼叫共用同一個 id）

# 2. export 到**檔案**（stdout 是乾淨 JSON；進度訊息在 stderr）
opencode export ses_f07fc798… > /tmp/agora-spike-impl1/orig.json

# 3. 重編三種 id
python3 spike/opencode/reidentify.py \
  --in orig.json --out imported.json --session-id ses_G4HQCVRHQA0CWV06

# 4. 在**目標專案目錄** import
cd /tmp/agora-spike-impl1/proj && opencode import /tmp/…/imported.json
#    → stdout: Imported session: ses_G4HQCVRHQA0CWV06

# 5. 接著做
opencode run -m opencode/space-bunny-free -s ses_G4HQCVRHQA0CWV06 --format json \
  "你前面在做什麼？用一句話回答。"

# 6. export 新 session 驗證
opencode export ses_G4HQCVRHQA0CWV06 > continued.json
```

### 看到的結果（形狀與欄位名）

匯出檔最外層是 `{info, messages}`：

```
info:  id, slug, projectID, directory, path, title, agent, model{id,providerID,variant},
       version, time{created,updated}, cost, tokens{...}, permission, summary{...}
messages[]: { info{ id, sessionID, role, parentID, time{created,completed}, agent,
                     providerID, modelID, mode, path, tokens{...}, cost, finish, summary? },
              parts[]: { id, sessionID, messageID, type, … } }
parts[].type 實際出現：text（含／不含 time）、reasoning（含 time）、
                        step-start（無 time）、step-finish（含 cost/tokens/reason/snapshot）
```

| 量 | 值 |
|---|---|
| 匯出檔大小 | 11.2K（6 訊息／14 parts） |
| 新 session id | `ses_` ＋ 16 碼，**20 個字元** |
| 重編後 message／part id | `msg_`／`prt_` ＋ 12 碼時間 ＋ 6 碼序號 ＋ 6 碼鹽 ＝ **28 個字元** |
| import 輸出 | `Imported session: ses_…`（stdout），rc=0，stderr 空 |
| import 後 export | 6 訊息／14 parts，role 順序 user,assistant ×3 |
| `-s` 接著做 | rc=0；JSON 事件只有 step_start／step_finish／text，**沒有 tool_use**；模型的回答裡明確提到前兩輪的主題與「不寫檔」的決定 |
| 繼續後 export | **8** 訊息（role user,assistant,user,assistant,user,assistant,user,assistant），原本那 6 則的 id 一個都沒變，新的是 opencode 自己的 `msg_0f80852a4…`／`msg_0f808565e…`（30 字元，含 `part` 事件） |

### 陷阱（照抄就會出事的那幾個）

1. **`export` 不能接 pipe，但也不能用 `2>&1`。**
   `opencode export <id> > f.json` 才是乾淨的 JSON；進度行
   `Exporting session: <id>` 寫在 **stderr**。寫成 `> f.json 2>&1` 會讓第一行變成
   那個進度行，JSON 直接解析失敗（實測 `jq: parse error`）。
2. **id 三種都要重編，而且必須固定寬度。** `opencode import` 對已存在的 id 是
   onConflictDoNothing：**rc=0、stdout 一樣印 `Imported session: …`，沒有任何
   警告，但匯出回來是 0 則訊息**（V1(b) 見下）。所以 import 完**一定要回頭數
   訊息數**，不能只看 rc。
3. **前綴不能改，而且 opencode 只檢查前三個字母、長度不管。** 實測：

   | 匯出檔裡的 id | 結果 |
   |---|---|
   | session id `0123456789ABCDEF…`（無前綴） | rc=1、`Error: Expected a string starting with "ses", got "0123…"` |
   | message id `m0`（無前綴） | rc=1、`Expected a string starting with "msg", got "m0"` |
   | part id `part0-0`（無前綴） | rc=1、`Expected a string starting with "prt", got "part0-0"` |
   | session id `ses_x`（**5** 個字元） | rc=0、6 則訊息進去 |
   | session id `ses_` ＋ 30 碼（34 個字元） | rc=0、6 則訊息進去 |
   | session id `ses_` ＋ 40 個 `z`（44 個字元） | rc=0、6 則訊息進去 |

   → 硬性要求只有「以 `ses`／`msg`／`prt` 開頭」。**沒有長度上限**（期 1 自己的
   `_ID_MAX = 30` 是自訂的保守值，不是 opencode 的限制）。`reidentify.py` 用
   28 字元、`ses_` ＋ 16 碼，形狀已經夠且與期 1 相容。
4. **匯入失敗不是交易式的：會留下半個 session。** 前綴寫錯而 rc=1 的那兩次，
   `opencode export <id>` 仍然讀得到 session——一次是 0 則訊息、一次是 1 則
   （`msg` 過關了但 `prt` 沒過）。所以「import 失敗」之後**那個 id 已經被佔用**，
   重試前要先把它刪掉（`opencode session delete <id>`），否則下一輪
   onConflictDoNothing 又會變成空 session。
5. **id 字典序必須與匯出順序一致**（`ORDER BY time_created, id`）。所以序號要
   依**匯出檔裡的順序**遞增，而不是依 id 排序後遞增——`reidentify.py` 直接照
   陣列順序給序號，時間碼取該則的 `time.created`，兩種排序結果相同。
6. **匯出檔裡的 `info.directory`／`projectID` 會被匯入當下的目錄蓋掉**（見 V5）。

### V1(a)：換新 id 之後，原本那個 session 有沒有被動到

**結論：完全沒動到。** 在 import 前後各 export 一次原 session，比對結果：

| | md5（`md5 -q`） | 訊息／parts |
|---|---|---|
| import 前 | `488bf5fa0610204db4f60e33b808f23c` | 6／14 |
| import 後 | `488bf5fa0610204db4f60e33b808f23c` | 6／14 |

`cmp` 逐位元組相同（`IDENTICAL BYTES`）。而且後續在**同一個匯入出來的** session
上繼續對話之後，再 export 原 session，md5 仍然一樣——匯入與接續都不會回寫來源。

### V1(b)：只換 session id、不換 message／part id

```bash
python3 spike/opencode/reidentify.py --in orig.json --out sessionid-only.json \
  --session-id ses_B1SESSIONONLYTEST1 --only-session-id
cd /tmp/agora-spike-impl1/proj && opencode import /tmp/…/sessionid-only.json
```

| | 結果 |
|---|---|
| import rc / stdout | rc=0 ／ `Imported session: ses_B1SESSIONONLYTEST1` |
| import stderr | 空 |
| import 後 export | **`messages` 長度 0**（匯出檔 1107 bytes，只剩 `info`） |
| 原有訊息去哪了 | 沒有出錯、沒有警告；6 則訊息與 14 個 parts **全部被丟棄** |

所以「id 撞了」的失敗模式是**靜默的資料遺失**，不是錯誤。實作上 `agora import`
必須在 import 後 `opencode export` 回讀一次、比對訊息數，不符就報錯（不要讓
使用者以為匯入成功）。

### 順帶發現：`--fork` 讓「接續」多一條更短的路

`opencode run -s <既有id> --fork` 與 `opencode -s <id> --fork`（TUI）**原生就會
複製成一個新 session**，不用自己重編 id：

```
opencode run -m opencode/space-bunny-free -s ses_f07fc798… --fork --format json "…"
  → 事件裡的 sessionID 是新 id ses_f07e97b89ffejZjTIy1vLr7qBj
  → 新 session 標題自動變成 "spike-impl1-csv (fork #1)"
  → export 新 session：8 則訊息（原本 6 + 新 2），message id 已被 opencode 換新
  → 同時再 export 原 session：md5 與 import 前完全相同（原封不動）
```

這對 design.md 5.4 很重要：**來源 session 就在本機時，走 `--fork`；來源在
Drive／別台機器時才走 export→重編→import**。`--fork` 也剛好符合 D4（continue
的結果是新 Session）。

另外 `opencode export <不存在的id>` 的行為乾淨：rc=1、stdout 空、stderr
`Error: Session not found: <id>`，`agora import` 可以直接把這個訊息轉成
「找不到這個 session」。

---

## V3：閱讀版注入——全新 session 的第一則訊息讀檔

**結論：兩種做法都可行，`--file` 比較穩。**

閱讀版用 `spike/opencode/to_reading.py` 產生（自編內容、2289 bytes、6 輪），
形狀是 design.md §3 的 header ＋ 一段「這是什麼／要接著做先讀完」＋ 逐輪
`## n. user|assistant`。header 用 `yaml.safe_load` 驗過可解析。

> ⚠️ 一個寫 header 的坑：清單欄位不能寫成 `tags:   - a\n  - b`（同行開頭的序列
> 不是合法 YAML，整份 front matter 會讀不出來）。要嘛 `tags: []`，要嘛
> `tags:` 換行再縮排兩格。

### A：`--file`（把檔案掛在第一則訊息上）

```bash
cd /tmp/agora-spike-impl1/proj2
opencode run -m opencode/space-bunny-free --format json --title spike-v3-A1 \
  -f /tmp/agora-spike-impl1/session.md \
  "先讀完我附上的這份 Session 閱讀版，然後用一句話回答：上一輪我們停在什麼主題、你已經決定不做什麼？"
```

| | 結果 |
|---|---|
| rc | 0 |
| 事件種類 | step_start / step_finish / text —— **沒有 tool_use** |
| 模型回答 | 有抓到「上一輪停在引號欄位含逗號的處理方法」與「先不寫檔」 |

重點：**檔案內容直接變成訊息的一部分，模型不需要（也不會）呼叫工具**。而且
`-f` 吃的是**絕對路徑、且在專案目錄之外**也照樣可以（`~/.cache/agora/…` 沒問題）。

### B：訊息裡寫路徑，讓 agent 自己 `read`

```bash
cd /tmp/agora-spike-impl1/proj2
opencode run -m opencode/space-bunny-free --format json --title spike-v3-B1 \
  "先讀 /tmp/agora-spike-impl1/session.md 這份 Session 閱讀版，再用一句話回答：…"
```

| | 結果 |
|---|---|
| rc | 0 |
| 事件種類 | step_start / step_finish / text ×2 / **tool_use ×1** |
| tool_use 的工具 | `read` |
| 權限 | 讀專案目錄**外面**的檔案**沒有**跳出權限詢問、也沒有卡住 |
| 模型回答 | 抓到主題，但把焦點說成「第三個步驟」，比 A 略不精準 |

### 建議（寫進 design.md）

用 **`--file`（`opencode run -f`）**，理由是三個：

1. 不依賴 agent 的 read 工具與權限設定，跨 agent 也一致；
2. 少一個回合＝等待時間短（這是 design.md §1 的第一優先）；
3. 答案比較準（A 抓到了「引號欄位」這個真正的主題，B 抓成「第三個步驟」——
   有工具就有「有沒有真的讀完」的變數）。

但要保留 B 的用途：**閱讀版很長時**（好幾百 KB 的 session），`--file` 會把整份
塞進第一則訊息，會吃掉 context；那時應該只給路徑、讓 agent 自己挑要讀的段落，
或者先切一版摘要當 `--file`、完整版留路徑。這是「有條件」，不是不可行。

實作上 `agora continue-session` 走注入時，檔案放在
`~/.cache/agora/sessions/<ULID>/session.md`（或直接從 Drive 鏡像讀），訊息固定
寫「先讀完這份閱讀版再回答我」，並把 agora id 寫在訊息裡當錨點。

---

## V5：import 掛在哪個專案目錄、能不能指定

**結論：掛在「執行 `opencode import` 當下的 cwd」，無法從匯出檔指定；而
`opencode run -s` 必須在**同一個**目錄開，否則**永久卡住、連一行輸出都沒有**。

### 匯入掛在哪

```bash
# 匯出檔裡故意填假的目錄與假的 git SHA
#   info.directory = /nonexistent/path/that/does/not/exist
#   info.projectID = deadbeef…（40 碼）／ info.path = /nonexistent/…
cd /tmp/agora-spike-impl1/proj2      # 自己的 git repo，HEAD = cec40685…
opencode import /tmp/…/import-proj2.json
opencode export ses_C2PROJ2DIRTEST01 | jq -r '.info.directory, .info.projectID, .info.path'
```

| 欄位 | 匯出檔裡寫的 | import 後實際值 |
|---|---|---|
| `info.directory` | `/nonexistent/path/that/does/not/exist` | `/private/tmp/agora-spike-impl1/proj2` |
| `info.projectID` | `deadbeef…` | `cec4068575d8a2d3ff09a3a67c9f1823425f33a2`（該目錄的 HEAD） |
| `info.path` | `/nonexistent/…` | 空字串 |

→ **匯出檔裡寫的目錄／專案完全被忽略**，opencode 用當下的 cwd 重寫。

非 git 目錄也匯得進去：

```bash
mkdir -p /tmp/agora-spike-impl1/plain   # 沒有 git
cd /tmp/agora-spike-impl1/plain && opencode import … && opencode export …
```
→ `info.directory = /private/tmp/agora-spike-impl1/plain`、
`info.projectID = "global"`（字面值 `global`，不是 null）、`messages` 6 則。

所以「能不能指定」：**不能用匯出檔指定，也沒有 `import --dir` 旗標**
（`opencode import --help` 只有 `<file>` 與全域旗標）。唯一的指定方式是
**`agora` 自己 `cwd=` 進去執行**。`opencode run` 有 `--dir`，但那是給 `run` 用的，
對已經寫進資料庫的 session 沒有用。

### 繼續要在哪個目錄開

| 情況 | 結果 |
|---|---|
| `cd proj2 && opencode run -s <proj2 的 session>` | rc=0，回答正確（模型說得出「CSV 轉 Markdown 表格，引號欄位處理」） |
| `cd proj3 && opencode run -s <proj2 的 session>` | **rc=124（被 `timeout 80` 砍掉）、stdout 0 bytes、stderr 0 bytes**。等多久都不動 |
| 同上但加 `--auto` | 一樣 rc=124、0 bytes |

`export` 與 `delete` 則是**全域**的：`cd proj && opencode export <proj2 的 session>`
正常列出（`info.directory` 仍是 proj2）。**只有 `run -s`／`-c` 受目錄限制**。

### 對 design.md 的修改建議（V5）

1. `agora continue-session` 必須有 `--dir`，而且**預設要用「匯入時的那個目錄」**
   ——現在 header 裡沒有這個欄位。建議在 common header 裡加：

   ```yaml
   source:
     agent: opencode
     session_id: ses_xxxx
     directory: /Users/…/myproject    # 匯入時的 cwd；接續的預設工作目錄
   ```

   `raw.json` 裡本來就有 `info.directory`，所以這不是新資料，只是把它抄進 header
   讓人不必去挖 raw.json。
2. 續接的**順序**要寫死在程式裡：`mkdir -p <dir>`（存在就跳過）→ 以 `<dir>` 為
   cwd 執行 `opencode import` → 在**同一個** cwd 執行 `opencode run/-s` 或
   `opencode -s`。中間任何一步換目錄，接續會無聲地卡住。
3. `--dir` 給的目錄若不存在要先問（不要默默建立使用者的專案目錄）。

---

## 對 design.md 的修改建議（彙總）

對應 design.md §5.2、§5.4、§7：

1. **§5.2 import**：步驟要寫成「export 到檔案 → 重編三種 id → 在目標目錄
   `opencode import` → **回頭 export 比對訊息數**」。最後一步是新的，理由是
   V1(b)：id 撞了不會有任何錯誤。
2. **§5.2**：三種 id 的硬性要求只有「以 `ses`／`msg`／`prt` 開頭」，長度不限。
   重編要用**固定寬度**（唯一性不能靠運氣），`spike/opencode/reidentify.py` 可
   直接沿用。`export` 的進度訊息在 stderr，**不要 `2>&1`**。
3. **§5.4 的載入方式表**：「單一 opencode → opencode」那一列要分成兩條路徑：
   - 來源 session 在**本機** → `opencode run/-s <id> --fork`（原生、乾淨、來源
     session 不動、agent 自動改名 `… (fork #N)`）。
   - 來源在 **Drive／別台機器** → export→重編→import（本次驗證的那條）。
   兩條都產生新 session，符合 D4。
4. **§5.4**：`continue-session` 結束後要存回的那個新 session id，在
   `--fork` 路徑下**要另外想辦法拿到**（headless 用 `run --format json` 的
   `sessionID` 欄位最省事；TUI 只能靠標題 `… (fork #N)` 加時間去猜，這是
   `--fork` 路徑唯一的弱點）。import 路徑則 id 是自己指定的，沒有這個問題。
5. **§5.4／§3**：header 的 `source` 加 `directory`（見上面 V5 的建議 1）。
6. **§5.3 merge**：merge 出來的多段 Session **不能**用原生 import 續（沒有
   對應的單一來源 session），本來就該走「閱讀版注入」，與 §5.4 的表一致。
7. **§7**：V1 的結論是「可行」，但要補一句「匯入後必須驗證訊息數」，這是
   唯一一個「看起來成功但其實沒資料」的地方。

---

## 結論速查

| 問題 | 結論 | 備註 |
|---|---|---|
| V1 匯入改 id 後能不能接 | **可行** | 三種 id 全重編（28 字元固定寬度）；原 session 位元組不變；新訊息接在後面 |
| V1(a) 原 session 會被動到嗎 | **不會** | import 前後 md5 相同 |
| V1(b) 只換 session id | **不可行（靜默失敗）** | rc=0 但 0 則訊息；必須回讀驗證 |
| id 形狀 | **有條件** | 必須以 `ses`／`msg`／`prt` 開頭；長度無限制；無前綴 rc=1 |
| V3 閱讀版注入 | **可行** | `--file` 最穩；訊息寫路徑也行（會多一個 read 回合） |
| V5 import 掛哪 | **cwd** | 匯出檔裡的 directory／projectID 被忽略；無 `--dir` 旗標 |
| V5 continue 在哪開 | **匯入時的同一個目錄** | 換目錄 → 永久卡住、0 輸出 |
| `--fork` | **可行且更短** | 本機來源用 fork，跨機器才用 import |

## 清理

測完後：`agora-spike-impl1` 底下 3 個專案目錄共 **14 個**自建 session（9 個主線
測試 ＋ 5 個 id 形狀探測）全部以 `opencode session delete <id>` 逐一刪除（三個
目錄各跑一次
`session list --format json | jq 'select(.directory|startswith("/private/tmp/agora-spike-impl1"))'`
確認為空）。沒有碰 `~/.local/share/opencode` 的資料庫、沒有碰 `~/.claude/projects`、
沒有 export 任何不是自己建立的 session。
---

# 實作：`src/agora/agents/opencode.py`（2026-10-02 晚，同一個作者）

把上面 V1／V3／V5 的結論寫成 `src/agora/agents/opencode.py`（361 行，含
docstring），測試在 `tests/unit/test_agent_opencode.py`（30 個）與
`tests/integration/test_opencode_real.py`（真的 opencode ＋ 免費模型，
`/tmp/agora-it-opencode/proj`）。

## 每項規格的結論

| 規格 | 結論 | 備註 |
|---|---|---|
| `export(id)`：stdout 寫檔、stderr 分開、找不到就 raise「找不到…」 | **可行** | 進度行 `Exporting session: …` 在 stderr；不存在的 id 是 rc=1 ＋ `Session not found`，翻成 `AgentError("opencode 找不到 Session …")` |
| `Exported` 的欄位 | **可行** | `dir`＝`info.directory`（N9）、`title`＝`info.title`、`created_at` 由 `info.time.created`（毫秒）轉 UTC `…Z`、`agent_version`＝`opencode --version`、`message_count`＝訊息數 |
| `reading(raw)` | **可行** | 只收 text 與 tool 一行；reasoning／tool 的 output／step-start／step-finish／file **完全不產出任何行**；不認得的型態 → `[skip <type>]`。全部走 `base.format_reading`／`base.tool_line` |
| `start_native`：三種 id 全重編、固定寬度、參照欄位一起改 | **可行** | 28 字元（`msg_`／`prt_` ＋ 12 碼時間 ＋ 6 碼序號 ＋ 6 碼鹽）；`sessionID`／`messageID`／`parentID` 用結構化重寫，深度不拘；鹽由新 session id 推導，所以重跑是 no-op、不同匯入不撞 |
| 匯入後回讀驗證訊息數，不符就刪掉新 id 再 raise | **可行，而且必要** | 見下面「整合測試抓到的兩個問題」 |
| `start_injected`：只有一則 user 訊息的 export，id 由 agora 決定 | **可行（有條件）** | 有條件＝payload 的欄位必須帶齊，否則 import 會過、之後 export 不了 |
| `collect` | **可行** | 訊息數 `<= before_count` 回 `None` |
| TUI 旗標 | **`opencode --session <id>`** | `opencode --help`：`‑s, --session  session id to continue`。`opencode run -s <id>` 是同一個 session id 的 headless 版（整合測試用這個） |

## 實際跑過的指令（形狀，不貼對話內容）

```bash
# 單元（30 個，全過）
uv run pytest -q tests/unit/test_agent_opencode.py

# 全單元（98 個，全過）
uv run pytest -q

# 整合（真的 opencode；免費模型不穩，會 skip）
uv run pytest -q -m integration tests/integration/test_opencode_real.py
#   → 3 passed, 2 skipped in 20m27s（skip 的兩個是真正需要模型回答的）

# 端對端手動重跑（模型有回應時；都在 /tmp/agora-it-opencode/proj）
opencode run -m opencode/space-bunny-free --format json --title manual-rt "把 CSV …"
opencode export <source id> > m1raw.json
python3 -c "…oc.ADAPTER.start_native(open('m1raw.json','rb').read(), Path('/tmp/agora-it-opencode/proj'))"
#   → NEW_ID ses_JFE2Y1Z0PPE09ERV before 2     （import ＋ 回讀驗證都過了）
opencode run -m opencode/space-bunny-free -s ses_JFE2Y1Z0PPE09ERV --format json "你前面在做什麼？"
#   → rc=0；模型答得出前一輪的主題與「沒動任何檔案」
python3 -c "…oc.ADAPTER.collect(Launch(argv=[], cwd=…, agent_session_id='ses_JFE2Y1Z0PPE09ERV', before_count=2))"
#   → collected=True messages=4 dir=/private/tmp/agora-it-opencode/proj
#     agent_version=1.18.34 created_at=2026-10-01T17:09:18Z 閱讀版 4 輪

# 欄位集合是逐個試出來的（import 缺一個欄位就整份拒絕）
opencode import <payload.json>            # 一次加一個欄位，看錯誤訊息往前進到哪裡
opencode export <new session id>          # import 過了之後一定要 export 得到
```

## 端對端（真的 opencode、真的模型）確認過的形狀

| 量 | 值 |
|---|---|
| `export` 的 `dir` | `/private/tmp/agora-it-opencode/proj`（V5：就是匯入時的目錄） |
| `agent_version` | `1.18.34` |
| `created_at` | `2026-10-01T17:09:18Z`（由 `info.time.created` 的毫秒換算） |
| `start_native` 回讀 | 匯入前後訊息數相同（2 → 2），檢查通過 |
| `opencode run -s <新 id>` | rc=0，模型**答得出**前一輪的主題與「沒有動任何檔案」 |
| `collect` | `messages=4`（原本 2 + 新 2），`session_id` 與匯入時的新 id 相同 |
| 閱讀版 | 4 個 `## user`／`## assistant` 段落（本輪沒有工具呼叫，所以沒有 `[tool]` 行） |
| 清理 | 兩個 session 各自用 `opencode session delete <id>` 刪掉 |

## 形狀：opencode 匯出檔必須長什麼樣才會被接受

`info`（缺任何一個，`import` rc=1 並印 `Missing key at ["…"]`）：

```
id, slug, projectID, directory, path, title, agent, version, permission,
model{…}, summary{additions,deletions,files}, cost,
tokens{input,output,reasoning,cache{read,write}}, time{created,updated}
```

型態上也有坑：`permission` 是**陣列**（`[{permission,pattern,action}]`）不是字串；
`tokens.cache` 一定要有 `read`／`write`；`tokens` 少一個 `cache` 會被拒。

user 訊息的 `info`：`id, sessionID, role, time{created}, agent,
model{providerID,modelID}, summary{diffs}`——**沒有 `path`**（只有 assistant 訊息有）。

## 整合測試抓到的兩個問題

### 1. 注入路徑會「匯入成功但存不回來」（真 bug，已修）

最初的 `_injected_payload` 只給 `{id, title, time}`。結果：

| 步驟 | 結果 |
|---|---|
| `opencode import` | **rc=0**、stdout `Imported session: ses_…` |
| agent 在裡面工作 | 正常 |
| `opencode export <id>`（`collect` 做的事） | **rc≠0**、`Error: Unexpected error / Missing key at ["slug"]` |

`collect()` 因此拋錯 → CLI 走「這次沒有新內容，沒有存」那條路 → **整段工作
消失，而且使用者看不到任何錯誤**。逐欄位試出完整的集合後補齊（上一節），並加了一個
單元測試把「注入 payload 的 `info`／訊息 `info` 欄位集合 == 真實 export 的欄位集合」
鎖住，免得日後 opencode 改版時重演。

→ 這是「import 後一定要回讀驗證」最實際的 justification：**rc=0 不等於匯入成功，
而 export 才是後面每一個步驟的入口**。

### 2. 免費模型會無聲卡住超過 7 分鐘

今天對 `opencode/space-bunny-free` 發了數十次請求，觀察到：

- 正常 45 秒；
- 也觀察到**超過 420 秒、stdout／stderr 一個位元組都沒有**就停在那裡（第一次
  整合測試就是這樣逾時）；
- 同一條指令在十分鐘後手動重跑就正常。

整合測試因此用 `AGORA_TEST_TIMEOUT`（預設 300 秒）與 `AGORA_TEST_ATTEMPTS`
（預設 3），連續無回應就 **skip** 而不是 fail——第三方模型慢不是 adapter 的缺陷，
但要在報告裡誠實寫出來。

同一條指令在超時後手動重跑就正常，所以**不是 adapter 的問題**；端對端那條路徑
（export → start_native → `run -s` → collect）已經手動完整跑過一次並通過
（見上一節）。

## 對 design.md 的修改建議

1. **§5.4 的「閱讀版注入」那一列要加一句**：注入用的 export payload 必須帶齊
   opencode 的 `info`／訊息 `info` 欄位，否則 `import` 會成功而 `export` 會失敗，
   等於整次 continue 成果蒸發且無錯誤。並列在上面那個欄位清單裡。
2. **§5.4 第 1 步的「原生」要寫明驗證迴圈**：`import` → `export` 回讀比對訊息數
   → 不符就 `opencode session delete <新id>` 再報錯（`import` 不是交易式的，
   失敗也會留下半個 session）。
3. **§7／test-plan 的整合測試**：免費模型的逾時要設得寬（≥ 300 秒）並允許 skip，
   否則同一份程式碼會 intermittent fail。
4. **§5.4 第 3 步**：`Launch.argv` 給的是 TUI（`opencode --session <id>`），
   所以「前景執行、使用者照常操作」與「headless 跑」是兩件事；若要加 headless
   模式，`cli` 需要一個旗標把 argv 換成 `opencode run -s <id>`（本模組已支援）。

## 要 PM 動的地方（我沒有改 `cli.py`／`base.py`／`store.py`）

讀過 `cmd_import`、`cmd_continue`、`_finish` 之後，介面是對得上的，**沒有必須改的
地方**。只有兩個建議：

1. `cli.py` 若要加 `--headless`／`--print`：本模組已備好
   `opencode run --session <id>`，在 `Launch.argv` 換一個元素即可，其他不用動。
2. `store.fetch_raw` 回傳的 `bytes` 若有為空的情形（`{}`），`start_native` 會丟
   `AgentError("匯出檔缺少 messages 清單")`——訊息清楚，但若 PM 想要一句更像
   「這個 Session 沒有 raw」，可以在 `store` 端先擋。

---

## code-adapters.md OC1–OC11 的修法（2026-10-02 晚）

`docs/review/code-adapters.md` B 節的十一項。OC11 是 conftest（PM 的檔案），而且
PM 已經自己加上了（`XDG_*`／`CLAUDE_CONFIG_DIR` 的 `delenv`，加上單元測試預設把
`AGORA_OPENCODE_CMD`／`AGORA_CLAUDE_CMD` 指向不存在的路徑），所以這一項不用動。

| # | 修法 | 新的測試 |
|---|---|---|
| OC1 | part id 改成 `prt_` + time(12) + **訊息序號(6)** + part 序號(4) + salt(6)；message id 是 `msg_` + time(12) + 訊息序號(6) + salt(6)。訊息序號是「在匯出檔裡的位置」，所以**兩則訊息同一毫秒、缺 `time.created`、全部都缺 time 三種情況都不會撞**。回讀驗證從「只數訊息」改成**訊息數與 part 數都數** | `test_part_ids_are_unique_when_two_messages_share_a_millisecond`、`…_when_a_message_has_no_created_time`、`…_when_no_message_has_a_time`、`test_part_ids_sort_by_message_then_by_part`、`test_import_verification_counts_parts_not_only_messages`（假 agent 用 `FAKE_OPENCODE_DROP_PART` 留下全部訊息但少一個 part） |
| OC2 | 遞迴找 key 的寫法刪掉，改成明確的位置：`info.id`、`messages[].info.{id,sessionID,parentID}`、`messages[].parts[].{id,sessionID,messageID}`、`info.revert.{messageID,partID}`。`state`／`metadata`／`input`／`output` 一律不動 | `test_a_subagent_session_id_in_a_tool_part_is_left_alone`（task part 的 `state.metadata.sessionId` 與工具 input 裡的 `sessionId`／`messageID` 都保持原值）、`test_nothing_outside_the_structural_positions_changes`（去掉 id 欄位後整棵樹等於原檔） |
| OC3 | `start_native`／`start_injected` 的匯入＋回讀＋數量檢查＋刪除抽成 `_import_verified()`，兩邊共用 | `test_start_injected_verifies_and_cleans_up`（`FAKE_OPENCODE_DROP=0` → 空 session → 報錯並刪掉）、`test_a_failed_delete_is_reported_as_such` |
| OC4 | 整合測試改成**刻意隔離**：module 專用的 HOME（`tmp_path_factory`）＋ function scope 的 `monkeypatch.setenv("HOME", …)`，`XDG_*` 全部 `delenv`；`trash` 只刪測試自己記下的 id（`source` fixture 自己 append）。原來的寫法是 module scope 改 `os.environ`，被 conftest 的 autouse fixture 蓋掉，導致測試本體與 teardown 用到兩個不同的 opencode 資料庫 | —（整合測試） |
| OC5 | 注入的 part 加上 `"synthetic": true` 與 `metadata.agora = "agora-injected"`；`_lines_of` 遇到自己注入的只輸出一行 `[注入的閱讀版]`，遇到 opencode 自己 inline 的 attachment（也是 `synthetic`）**完全不輸出** | `test_our_injected_reading_version_is_one_line`（50 段閱讀版只變一行）、`test_an_injected_part_is_marked_synthetic`、`test_an_attachment_opencode_inlined_produces_no_line` |
| OC6 | 保留 `part_map`，`info.revert.partID` 也照對應改寫 | `test_a_reverted_session_keeps_its_undo_pointer` |
| OC7 | 所有對 opencode 的 subprocess 都加 timeout（`AGORA_OPENCODE_TIMEOUT`，預設 60 秒），逾時丟 `AgentError`，所以會走 R4 的「下次再試」而不是把每一個後續 agora 指令一起卡住。timeout 每次呼叫再讀（不是 import 時讀），測試才改得動 | `test_a_hanging_opencode_becomes_an_error_not_a_hang` |
| OC8 | `agent_version` 改成用 `info.version`（那才是**產生這個 session** 的版本），拿不到才叫 `opencode --version` | `test_export_fills_every_field`（改成斷言 1.18.34）、`test_the_version_falls_back_to_the_cli_only_when_the_export_has_none` |
| OC9 | `_delete` 檢查 rc；失敗時訊息改成「刪除也失敗了，請手動 `opencode session delete <id>`」 | `test_a_failed_delete_is_reported_as_such` |
| OC10 | `collect` 沒有 `agent_session_id` 時丟 `AgentError`（與 claude 一致），不再回 None——回 None 會讓 CLI 刪掉 pending，整個 Session 就沒了 | `test_collect_without_a_session_id_raises` |
| OC11 | conftest 是 PM 的檔案，不動。PM 已經自己處理了 | — |

fixture 也補齊（review 指出的三個缺口）：`info.revert`、task tool part（含
`state.metadata.sessionId` 與使用者形狀的 input）、`synthetic` text part、
`compaction`／`subtask` part、兩則同一毫秒的訊息、一則沒有 `time.created` 的訊息。

單元測試 44 個、全單元 128 個全過。

---

# e2e：真的 Drive ＋ 真的 opencode ＋ 真的 claude（2026-10-02 晚）

`tests/integration/test_e2e_opencode.py`，照 impl2 的 `test_e2e_cli.py` 的做法：
`AGORA_CONFIG` 是暫存目錄、`rclone.conf` symlink 到 `~/.config/agora/rclone.conf`、
每一個指令都經過 `agora.cli.main`。專案目錄是 `/tmp/agora-it-e2e-oc/p_專案.v2`
（底線、中文、點，故意），開頭是 `git init` ＋ 空 commit。

| # | 做什麼 | 結論 |
|---|---|---|
| 1 | 免費模型建自編短對話 → `agora import --format opencode` → `agora search session 表格` | **可行** |
| 2 | `agora continue-session <id> --agent opencode`（原生匯入） | **可行，但要靠包裝腳本** |
| 3 | 同一個 Session 用 `--agent claude` 接（閱讀版注入） | **可行** |
| 4 | 清理（Drive ULID、opencode session id、claude jsonl 與空目錄） | **可行**，第一版有兩個洞，都已補 |

## 實際跑過的指令

```bash
nohup uv run pytest -q -m integration tests/integration/test_e2e_opencode.py > log 2>&1 &
# → 1 passed in 67～77 秒（三種 agent 的步驟各呼叫一次模型）
```

包裝腳本：`tests/fakes/opencode_noninteractive.py`，用 `sys.executable` 叫
（`#!/bin/sh` + `exec`），收到 `--session <id>` 就改跑
`opencode run -s <id> -m opencode/space-bunny-free <固定自編問題>`；
`--version`／`export`／`import`／`session delete` 直接往真的 opencode 送。
claude 端用 impl2 的 `tests/fakes/claude_noninteractive.py`（裡面本來就有
`--disallowedTools Bash Read Glob Grep Edit Write WebFetch WebSearch Task`），
`claude -p` 只呼叫 1 次。

## 看到的結果（形狀與欄位名）

匯入後的 header：

```
source: {agent: opencode, session_id: ses_…, dir: /private/tmp/agora-it-e2e-oc/p_專案.v2,
         host: …, agent_version: 1.18.34, created_at: 2026-…Z}
relation: import      parents: []
raw: {file: raw-<md5 前12>.json, md5: …, size: …}
title: e2e 表格      （--header title= 進來的）
```

`continue-session` 之後（原生）：

```
relation: continue
parents: [{id: agora:<匯入那個>, raw_md5: <該 session 當時的 md5>}]
source.session_id: 新的 ses_…（和來源不同）
source.dir: /private/tmp/agora-it-e2e-oc/p_專案.v2
```

驗到的幾件事：

- **`opencode run -s <新 id>` 真的接得上**：新 session 的訊息數是來源 ＋ 2，
  `info.directory` 就是匯入時的目錄（V5）。
- **V1(a)**：`source` 那個 opencode session 的訊息數在 continue 前後**一樣**。
- **跨 agent**：wrapper 的 log 顯示 claude 收到的是 `--session-id`（注入）而不是
  `--resume`（原生），而且只有一次啟動。
- wrapper 的 log 裡 opencode 只收到一次 `--session <新 id>`：`agora import` 走的是
  `export`，不是 TUI。

## 兩個環境陷阱（第一版就踩到，第二個差點讓清理變成假的）

### 陷阱 A：opencode 的專案是從 `$PWD` 決定的，不是從行程的 cwd

實測：`cwd=X`、`$PWD=Y` 時，

| 指令 | 結果 |
|---|---|
| `opencode run …`（建新 session） | session 落在 **Y** 的專案 |
| `opencode import <payload>` | session 落在 **X** 的專案 |
| `opencode export <id>` | 照 session 自己的目錄，與呼叫位置無關 |

第一版測試就是這樣：session 落在 repo 目錄，於是 `source.dir` 變成 repo、
`session delete` 說「Session not found」（它在自己看不到的專案裡找）。

→ 對測試：每個 opencode 呼叫都要帶 `PWD`（包裝腳本裡 `env["PWD"] = os.getcwd()`，
測試裡顯式給）。

→ **⚠️ 這是 cli.py 的 bug**（不是我的檔案，請 PM 改）：`cmd_continue` 現在是
`subprocess.run(launch.argv, cwd=launch.cwd, env=...)`，**沒有設定 `PWD`**。
`agora continue-session X --dir /other/project`（從別的目錄執行）時，
`start_native` 會把 session 匯入到 `/other/project`（import 用 cwd），
但接著啟動的 opencode 會因為 `$PWD` 還是呼叫者的目錄而**在別的專案裡**開一個
session——使用者看到的是空的對話，而 pending 記的 id 在那個專案裡根本不存在。
修法：`env` 裡加上 `"PWD": launch.cwd`。
（`--dir` 與呼叫者目錄相同的情況不會出事，所以一般用法看不出來。）

### 陷阱 B：建立與清理用了兩個不同的 opencode 資料庫

第一版在測試本體把 `HOME` 指回真實家目錄（照 spike 的習慣），teardown 卻繼承
conftest 的暫存 HOME，於是「刪掉」刪的是一個從來沒寫進去的資料庫——兩個
`Session not found` 警告。改成**全程用 conftest 的暫存 HOME**（免費模型不需要
auth），`AGORA_REAL_HOME` 不設，teardown 就刪得到。順帶的好處是這個測試完全不會
碰到真的 opencode 資料庫。

清理另外補了 claude 留下的空目錄：`~/.claude/projects/<編碼後>` 底下空的
`memory/`，以及空的專案目錄本身。Drive 那邊只 purge 這次建立的 ULID，沒有動
`agora-test/` 以外任何東西（`agora-test/sessions/` 空目錄會留下，和 impl2 的 e2e
一致）。

## code-adapters.md Q1–Q9（精簡）與 E-2

`src/agora/agents/opencode.py` 466 行 → **393 行**，`src` 合計 1,973 → **1,900 行**。

| # | 做法 |
|---|---|
| Q1 | 模組 docstring 51 行 → 20 行摘要＋「詳見 docs/spike/opencode.md 陷阱 1–6」 |
| Q2 | `_MESSAGE_REFERENCES`／`_PART_REFERENCES` 從「定義了沒人用」改成 `reidentify` 真的呼叫它們（`sessionID` 仍然顯式賦值） |
| Q3 | 各個 `#:` 區塊壓成一行 |
| Q4 | `_run` 多收一個 `stdout=`，`export` 也走 `_run`，刪掉重複的 `TimeoutExpired` 處理 |
| Q5 | `_delete` 與 `_discard` 合併成 `_discard(session_id, cwd) -> str`（直接檢查 rc） |
| Q6 | `_injected_payload` 用暫時 id 組 payload 再交給 `reidentify`，id 規則只剩一個地方 |
| Q7 | `_stamp`／`_message_id`／`_part_id` 合併成 `_id(prefix, *positions, time_ms, salt)`，每個 position 自帶寬度；順手加了一個 `assert`（E-3）防止溢出成五位破壞排序 |
| Q8 | 拿掉 `_cli_version()`：匯出檔一定帶 `info.version`，沒有就是 `None`，不拿 CLI 的版本冒充（測試改成斷言 `None`） |
| Q9 | `reidentify` 改呼叫 `_payload_messages()`，形狀檢查只留一份 |
| E-2 | 整合測試的注入那條加了寬鬆斷言：回答裡必須出現 `CSV` 或 `表格`（閱讀版才有的字），不然只斷言 `message_count > 1` 的話，opencode 哪天改成不送 synthetic part 也看不出來 |

單元測試 154 個全過（含 adapter 的 44 個）。

## 對 design.md 的修改建議

1. **§5.4 第 3 步要加一句**：啟動 agent 時除了 `cwd`，**環境裡的 `PWD` 也要設成
   同一個目錄**。opencode 的 TUI／`run` 是從 `$PWD` 決定專案的，而 `import` 是從
   cwd；兩者不一致時會開到別的專案去。
2. **§5.4 第 1 步**：「message id＝time＋訊息序號；part id＝time＋**訊息序號**＋
   part 序號」，並寫明「回讀時同時比對訊息數與 part 數」（OC1）。
3. **§5.4 同一列**：「只改結構位置上的 id 與參照，`state`／`metadata`／`input`／
   `output` 不動」（OC2）。
4. **§5.4 注入那一列**：注入同樣要回讀驗證；注入的 part 帶 `synthetic` 標記，
   閱讀版只輸出一行 `[注入的閱讀版]`，否則每接一次就把上一次整份包進來（OC3、OC5）。
5. **§8**：`src` 的 2,000 行目標建議寫清楚怎麼算（review 也提了）。現在用
   `wc -l` 算是 1,900 行，其中說明文字（docstring、註解、空行）大約 200 行；
   真正的程式碼遠低於 1,700，說明文字不必為了行數去砍。

---

## final.md P1–P3 與模型 fallback（2026-10-02 深夜）

### P1（High）：e2e 的 teardown 只收這個測試自己印出來的 id

原本寫 `ulids.update(store.Index(paths).known())` 想「保險一點全刪」，但 `sync`
早就把共用 `agora-test/sessions/` 裡**所有人**的 Session 同步進這個 cache，
所以那行等於「把別人的資料刪掉」（和 impl2 的 D-1 同一個錯）。改成照
`test_e2e_cli.py`：只有 `run_main` 在 `import`／`continue-session`／
`merge-session` 的 stdout 印出來的那個 id 會被記下（`created["printed"]`，
`len(token) == len("agora:") + 26` 才算），teardown 只 purge 這些。
`search`／`show` 的輸出永遠不掃——那裡面會有別人的 Session。

### P2（Medium）：claude 的 uuid 也從 wrapper 的參數紀錄收

原本只在 `header_of(id3)` 的斷言成功之後才 append，所以 continue 一失敗，
claude 寫在真實 `~/.claude/projects/` 裡的 jsonl 就留下來。改成 teardown 也讀
`$FAKE_HOME/e2e-args.log`，把 `--session-id`／`--resume` 後面的 uuid 收進來。

順手把刪除改成**精確路徑**（D-2／CL12 那一類寫法）：`projects_dir()/<encode(proj)>/<uuid>.jsonl`
與同名 sidecar 目錄、`config_dir()/session-env/<uuid>`、`file-history/<uuid>`。
不再 `glob("*/<uuid>.jsonl")`（會掃過真實的專案目錄清單）、不再 glob `todos/`
（工具都擋掉了，不會有）、也不再用子字串比對專案資料夾名稱後整個刪 `memory/`——
只有 `encode(proj()` 這一個精確名字，而且是 `rmdir`（非空就不動）。

### P3（Medium）：用設定擋掉 opencode 的工具，不只靠 prompt

所有測試裡的 `opencode run`（wrapper 與兩個測試的 `opencode_run`／`ask`）都在
env 裡帶 `OPENCODE_PERMISSION='{"*":"deny"}'`，放在 `tests/fakes/opencode_noninteractive.py`
的 `PERMISSION` 常數，不給環境變數覆寫（免得 CI 上不小心關掉）。

**怎麼確認它真的擋住了**（在 `/private/tmp/agora-it-x/p_專案.v2`，免費模型，
問題是「請用 read 工具讀取 /tmp/agora-it-x/不存在的檔案.txt」，
以 `--format json` 看事件）：

| 設定 | `tool_use` 事件 | 模型回答 |
|---|---|---|
| 不設（對照組） | **1 個**：`{tool: read, status: error}`（`File not found`） | 說檔案不存在，並列出專案目錄 |
| `OPENCODE_PERMISSION='{"*":"deny"}'` | **0 個** | 直接說無法讀取該檔案 |

也就是說：沒有這個設定時，模型會照著提示去呼叫 `read`，而 opencode 的 read／glob／
grep／bash 都能用絕對路徑讀到 `~/` 底下的任何檔案（HOME 被隔離只會改變 `~` 指向哪裡）；
有了這個設定，模型**連嘗試都沒有**。這比「在 prompt 裡拜託它不要用工具」可靠——
claude 那一側本來就是用 `--disallowedTools` 擋的，現在兩邊一致了。

### 模型 fallback：依序試，全部沒回答才 skip

`tests/fakes/opencode_noninteractive.py` 的 `model_chain()` 定義順序：

1. `opencode/space-bunny-free`
2. `opencode/muse-spark-1.3-contributor-free`
3. `ollama-cloud/deepseek-v4.1-flash --variant max`——**只在這台機器真的有這個
   provider 時**才列入，用 `opencode models ollama-cloud` 判斷（結果快取）。
   實測：真的 HOME 底下列得出來（`opencode providers list` 也有 Ollama Cloud 的
   credential），但**整合測試刻意用隔離的 HOME**，那裡沒有 credential，
   `opencode models ollama-cloud` 會回答 `Provider not found`，所以第三個模型會被
   自動略過——測試因此**不會**用到使用者自己的第三方額度。要驗證第三個模型，
   得用真的 HOME 跑，那就不在測試裡做了。

`ask()` 逐個試，每個都有 `AGORA_TEST_TIMEOUT`（預設 300 秒）的期限；只有**全部**
都逾時或 rc≠0 才 `pytest.skip`，而且 skip 訊息會列出試過哪些模型、每個的錯誤是什麼
（`opencode/space-bunny-free: 逾時（300 秒，沒有任何輸出）；…`）。回答的模型會印一行
出來，e2e 也把它記進 wrapper 的 log（`{"model": …, "failures": […]}`）。

### R-1：這兩條一直 skip 的真正原因

`test_opencode_real.py` 的 `ask()` 只給了 `cwd=PROJ`，**沒有給 `PWD`**
（$PWD 那個陷阱，上面「陷阱 A」）。所以：

1. `opencode run` 產生的 source session 落在**測試啟動時的目錄**（repo），不是
   `/tmp/agora-it-opencode/proj`；
2. `start_native` 把 `import` 匯入 PROJ（import 用 cwd），新 session 在 PROJ；
3. 接著 `opencode run -s <新 id>` 帶著舊的 `$PWD`，在 repo 專案裡找這個 id
   ——**就是 V5 量到的那個「永久卡住、零輸出」**。

於是每次都是 300 秒逾時，看起來像「免費模型沒回答」。修正：`ask()` 與
`agent.ask()` 都把 `PWD` 設成傳給它的 cwd（wrapper 本來就設了），並加了一個會
直接抓到這個錯的斷言——source session 的 `info.directory` 必須等於 PROJ。
順手把該檔案裡其他呼叫 opencode 的地方（`export`、`session delete`）也補上 `PWD`。


---

## design v4：`Exported.model`（2026-10-03）

design 第 4 版把標頭改成 OKF 形狀，其中 `generated.by` 是
`opencode/<模型>`、`sources[].author` 也是 `opencode/<模型>`，所以 adapter 要回報
「這個 Session 最近用哪個模型」。結論：**可行，欄位來源唯一**。

- **來源**：opencode 的 export 裡**只有 assistant 訊息**的 `info` 有 `modelID`
  （旁邊是 `providerID`）；user 訊息是巢狀的 `info.model` 物件
  （`{providerID, modelID}`），形狀不同，不能混用。
- **取法**：由**最後一則** assistant 的 `modelID`；沒有 assistant 訊息、或該欄位是
  空字串，就回 `None`（不留空字串，避免 header 出現 `by: "opencode/"`）。
- `export()` 與 `collect()` 都填：`collect()` 是走 `export()` 回讀整個 session，
  所以「接續之後」拿到的是**剛才回答的那個模型**，不是匯入時 transcript 最後那個。
- 形狀驗證（自編 fixture，兩則 assistant 的 `modelID` 故意不同）：
  `oc-basic.json` 第 1 則 assistant 是 `space-bunny-free`、最後一則是
  `muse-spark-1.3-contributor-free` → `export()` 回後者，證明是「最後一則」而不是
  「第一則」或「任何一則」。
- 假的 opencode（`FAKE_OPENCODE_MODEL`，預設 `space-bunny-free`）會在 append 的
  assistant 訊息上寫 `modelID`，所以 `collect()` 的兩種模型都能測。
- 單元測試 5 個（四個 model 欄位 ＋ collect），`tests/unit/test_agent_opencode.py`
  49 個全過。

**不需要改 design.md。** 只有一點值得記：這個欄位描述的是「**最近一次**使用的
模型」，不是 session 建立時的模型，所以 `generated.by` 對一個被接續過很多次的
Session 會反映最後一次。名稱 `model`（不是 `created_with_model`）已經表達了這件事。

---

## design v4：e2e 改成新語法與 OKF 標頭（2026-10-03）

`195827d` 之後，e2e 的指令與標頭全換了。結論：**可行**，e2e 66 秒通過、跑完不留東西。

### 指令（每個都實際跑過）

| 舊 | 新 |
|---|---|
| `agora import --format opencode --session-id <ses>` | `agora import session --external-session-id <ses> --agent opencode --header title=…` |
| `agora search session 表格` | `agora search session --filter text~=表格` |
| `agora continue-session <id> --agent opencode --dir <dir>` | `agora continue session <id> --agent opencode --dir <dir>` |
| `agora sync`（測試靠它讀回索引） | 沒有這個指令了；`header_of()` 改成直接呼叫 `store.sync(paths)`，和每個指令自己做的事一樣 |
| — | 多加一條 `agora search session --filter agent=opencode --filter generated.by~=opencode/<模型>`（點路徑 ＋ 包含，同時成立） |

`run_main()` 記 id 的條件也跟著改：動作在 `argv[0]`、型態在 `argv[1]`，所以只認
`("import", "continue", "merge")` ＋ 第二個字是 `session`（P1 的「只收自己印的 id」照舊）。

### 標頭（形狀）

```
type: Session
title: e2e 表格                     ← --header title=
description: …第一則 user 訊息的前 80 字（自動）
generated: {by: "opencode/<模型>", at: <來源建立時間，Z 結尾>}
sources:
  - {id: "opencode:<ses_id>", title: "opencode session",
     author: "opencode/<模型>", last_modified: YYYY-MM-DD}
id: agora:<ULID>
agora:                            ← 系統欄位全在這裡
  header: 2
  created_at / updated_at
  relation: import | continue | merge
  parents: [{id: agora:…, raw_md5: …}]
  source: {agent, session_id, dir, host, agent_version, created_at}
  raw: {file: raw-<md5前12>.json, md5, size}
```

驗到的重點（自編測試資料，不貼對話內容）：

- `generated.by` == `opencode/<來源 export 最後一則 assistant 的 modelID>`，而且
  `sources[0].author` 是同一個字串、`sources[0].id` == `opencode:<來源 ses id>`。
- 用 claude 接續的那一筆：`agora.source.agent == "claude"`、
  `generated.by` 以 **`claude-code/`** 開頭（前綴兩個 agent 不同）。
- `agora.parents[0].raw_md5` == 來源那一筆的 `agora.raw.md5`。
- opencode 原生接續後來的那個 `ses_` 與來源不同，而且原始 session 的訊息數沒變（V1a）。

### 對 design.md 的建議

1. **§3.4 補一句 `sources[].id` 的前綴**：它是 `<agent>:<該 agent 自己的 id>`
   （`opencode:ses_xxxx`），**不是** `agora:`——只有 agora 自己的項目才帶 `agora:`，
   這兩個前綴在同一個檔案裡很容易看錯。
2. **§3.4 補一句 `generated.by` 的前綴依 agent 而異**：opencode 是 `opencode/`，
   claude 是 `claude-code/`（`ACTOR` 表）。搜尋與顯示都會用到，值得寫在設計裡。
3. **§7／test-plan 給測試作者一句**：`sync` 拿掉之後，整合測試想讀回索引要自己
   `store.sync(paths)`（或直接讀本機 cache）；`agora show` 只印文字，不能拿來斷言
   標頭欄位。


---

## 那個 stray session 是誰留下的（2026-10-03）

`ses_DAK29P8XD5BE4K17`（標題「Agora 接續（閱讀版）」、`directory` 是 repo、建立於
2026-10-02T00:33:50Z）**不是測試留下的**，是 `docs/acceptance.md` 第 6 節那一行：

```bash
agora merge-session <B>, <C>
agora continue-session <merge 印出的 id> --agent opencode   ← 沒有 --dir，而且是舊語法
```

三件事疊在一起：

1. **沒有 `--dir`** → 工作目錄退回 `os.getcwd()`，而驗收是**在 repo 裡**跑的；
2. 來源是 **merge**（`relation: merge`、沒有 `raw`）→ 走**閱讀版注入**路徑；
3. 目標是 **opencode** → `start_injected` 用 `opencode import` 建了一個新 session，
   標題就是 `_injected_payload` 的「Agora 接續（閱讀版）」。

`opencode import` 用的是 **cwd**（實測：`cwd=A`、`$PWD=B` 時 session 落在 A），
所以「注入 + 沒給 `--dir`」就足以把 session 建在使用者正在看的專案裡。

（同一份 acceptance.md 的其他步驟也還是舊語法 `continue-session`／`merge-session`／
`import --format … --session-id`，建議一併更新成第 4 版的 `continue session`／
`merge session`／`import session --external-session-id … --agent …`，並且每一步都
補上 `--dir`。）

### 測試這邊的稽核結果

`tests/integration/` 與 `tests/fakes/` 裡所有會碰到 opencode 的呼叫，**本來就都在
自己的 scratch 目錄**，沒有一個用 cwd：

| 檔案 | 呼叫 | 工作目錄 |
|---|---|---|
| `test_opencode_real.py` | `opencode run`／`export`／`session delete`／`start_native`／`start_injected` | `/tmp/agora-it-opencode/proj`（`cwd` 與 `PWD` 都給） |
| `test_e2e_opencode.py` | `agora import/continue`（都有 `--dir`）／wrapper／`export`／`delete` | `/tmp/agora-it-e2e-oc/p_專案.v2` |
| `fakes/opencode_noninteractive.py` | wrapper 轉成 `opencode run` | `env["PWD"] = os.getcwd()` |

而且這些測試**用 conftest 隔離的 HOME**，所以就算寫錯了路徑，也只會寫進暫存 HOME
的資料庫，不會碰到使用者真正的 opencode store（實測：`HOME=<tmp>` 時
`opencode import` 的 session 落在暫存 store，repo 的 session 數不變）。

### 新增的保護：repo 目錄的 session 數不允許變多

`tests/conftest.py` 加了一個 autouse fixture（只對 `integration` 生效）：

- 測試**開始前**用**真的 HOME** 數一次 repo 這個 project 的 session id
  （`opencode session list --format json`，project-scoped，只留 id、不留標題或內文）；
- 測試**結束後**再數一次；
- 多出來的話：逐個 `opencode session delete <id>` 刪掉，然後 `pytest.fail` 並把
  新增的 id 印出來（「請找出是哪條指令忘了 `--dir`」）。

**實測有效**：寫了一個臨時的整合測試，故意用真的 HOME 在 repo 裡
`opencode import` 一個注入用的 session，guard 偵測到、刪掉、並讓測試失敗：

```
Failed: 整合測試在 repo 目錄留下 opencode session（已刪掉，請找出是哪條指令忘了 --dir）：ses_RC76X8AH5J9SYPD4
1 passed, 1 error
```

刪掉之後 repo 的 session 數回到原來的 36，沒有殘留。臨時測試檔已移除。

順帶一提：驗收腳本（`/tmp/agora-acc/env.sh` 那種）**不會**被這個 guard 保護——
guard 只在 pytest 裡生效。若要連驗收也保護，`agora continue` 在 `source.dir`
不存在又沒給 `--dir` 時印一行警告（說「將使用目前目錄 <cwd>」）會更直接。
---

## design v5：轉接器只剩 turns() 與 native()（2026-10-03）

`301b265`／`084986d`：continue ＝ cli 取 raw → 轉接器轉成自己的格式 → 一律
`start_native`。轉接器只有兩個方向，`reading()` 與 `start_injected()` 都拿掉了
（閱讀版改用 `base.reading(agent, raw)`）。

### 結論

| 問題 | 結論 |
|---|---|
| `native(turns)` 產生的 raw 能不能直接餵 `start_native` | **可行**（真的 opencode 匯入＋匯出驗過） |
| `native(turns(raw))` 再 `turns()` 會不會一樣 | **一模一樣**（單元測試＋真的 opencode 都驗過） |
| 程式碼有變少嗎 | **幾乎持平**：opencode.py 檔案行數 409 → 436（多了 `native_of`、少了 `reading_of`／`_injected_payload`／`start_injected`）；**程式碼行**（扣掉空行、註解、docstring）257 → **261**，+4 行 |

### `native()` 組出來的形狀

```
info   : _session_info(...)        ← 沿用原本注入用的欄位集合（slug、permission、
                                     tokens.cache、summary…一個都沒少）
messages[i]:
  info  : {id, sessionID, role, time{created[,completed]}, agent, model{providerID,modelID}}
          user      另外 summary{diffs: []}
          assistant 另外 parentID（指上一則）、mode、finish、providerID、modelID、cost、
                          path{cwd,root}、tokens{total,input,output,reasoning,cache{read,write}}
  parts : 每個**非空行一個** text part（{id, sessionID, messageID, type: "text", text}）
```

- **一行一個 part**：這是 opencode 自己存多段回覆的方式，所以
  `turns(native(turns(raw)))` 連行的切法都一樣（若把整輪併成一個 part，N 行會變
  1 行，round trip 就不相等）。
- **沒有 synthetic、沒有 attachment**：v5 的一個重點就是「打開就看得得到前文」，
  附件或 synthetic part 在畫面上不是一般的訊息。
- assistant 的 `parentID` 不能是 null（`/undo` 要靠它），所以每則 assistant 都指到
  上一則。`cli._converted_turns` 保證 user 開頭、嚴格交替、每則非空、沒有 `[skip `
  行，`native()` 因此不需要合併也不需要補訊息，照順序組即可（只留一行防呆，避免
  違約的呼叫端生出空訊息）。

### import 對 assistant 訊息比想像的嚴（一次講清楚，省得下次再猜）

用真的 opencode 逐欄位試出來的，缺一個就是整份拒絕：

| 缺什麼 | 錯誤 |
|---|---|
| `info.path`（assistant） | `Missing key at ["path"]` |
| `info.tokens`（assistant） | 同上，只是換一個 key |
| assistant 的 `summary` 放成 `{"diffs": []}` | `Expected boolean | undefined, got {"diffs":[]}`（`{diffs}` 是 **user** 訊息的形狀） |

補齊之後：`import rc=0`、`export` 回來訊息數相同、`turns` 完全一致。

### 實際跑過的指令

```bash
# 單元：round trip、assistant 的 parentID、時間遞增、可見 text part、native→start_native
uv run pytest -q tests/unit/test_agent_opencode.py          # 52 passed

# 真的 opencode（隔離的 HOME、/tmp/agora-it-opencode/proj、免費模型）
uv run pytest -q -m integration tests/integration/test_opencode_real.py
#   → 5 passed（其中 test_real_native_round_trip：turns → native → start_native
#     → opencode run -s 一輪 → collect，回答裡有前文的關鍵字）

# 欄位需求是這樣試出來的（不叫模型，2 秒一輪）
python3 /tmp/agora-it-opencode/probe_assistant.py '{"path": …, "tokens": …}'
```

### 對 design.md 的建議

1. **§5.4 第 2 點可以寫得更硬**：交給 `native()` 的 turns 保證 user 開頭、嚴格交替、
   每則非空、沒有 `[skip ` 行——這是 `cli._converted_turns` 的既有行為，值得寫成
   adapter 可以依賴的前提，不然每個 adapter 都會 defensive 地重建一遍。
2. **§5.4 加一句「一行一個 text part」**：這是 opencode 端 round trip 能成立的關鍵，
   也是畫面上看起來像原對話的原因。